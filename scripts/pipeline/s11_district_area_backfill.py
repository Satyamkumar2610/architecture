"""
s11_district_area_backfill.py  — Stage 11
District Census Area Backfill Engine (v2 — outflow_share fix)

KEY INSIGHT:
  When District A splits into B and C, BOTH B and C have inflow_share≈1.0 from A
  (all of each came from A). Using inflow_share would double-count A's area into B AND C.
  The correct weight is outflow_share: "what fraction of A's territory became B?"

  Historical area of B in year Y  =  sum_over_predecessors(P_area_Y × outflow_share[P→B])

Reads:
  outputs/pipeline/district_census_area_matrix.csv
  outputs/pipeline/s6_district_relationship.csv
  outputs/pipeline/s5_canonical_key_registry.csv
  data/gold/core/name_alias_registry.parquet
  data/silver/geometry/*.geoparquet

Writes:
  outputs/pipeline/district_census_area_matrix_backfilled.csv   (742 rows × 43 cols)
  outputs/pipeline/district_census_area_matrix_backfilled.parquet
  outputs/pipeline/district_area_fill_audit.csv                  (6678 audit rows)

Fill method codes:
  DIRECT              — measured directly from silver geometry
  DIRECT_PREDECESSOR  — 1:1 predecessor (outflow_share ≥ 0.95)
  LINEAGE_BACKFILL    — weighted sum of predecessors via outflow_share
  ALIAS_RESOLVED      — name mismatch resolved via alias registry
  NULL_CORRECT        — year precedes district's entire lineage chain
  NULL_UNRESOLVED     — lineage incomplete (should be 0 after good lineage)
"""

from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

ROOT   = Path(__file__).resolve().parents[2]
PP     = ROOT / "outputs" / "pipeline"
GOLD   = ROOT / "data" / "gold" / "core"
SILVER = ROOT / "data" / "silver" / "geometry"

YEARS = [1951, 1961, 1971, 1981, 1991, 2001, 2011, 2021, 2025]

DIRECT         = "DIRECT"
DIRECT_PRED    = "DIRECT_PREDECESSOR"
LINEAGE_FILL   = "LINEAGE_BACKFILL"
ALIAS_RESOLVED = "ALIAS_RESOLVED"
NULL_CORRECT   = "NULL_CORRECT"
NULL_UNRESOLVED= "NULL_UNRESOLVED"


def load_inputs():
    print("Loading inputs...")
    matrix  = pd.read_csv(PP / "district_census_area_matrix.csv")
    lineage = pd.read_csv(PP / "s6_district_relationship.csv")
    ck_reg  = pd.read_csv(PP / "s5_canonical_key_registry.csv")

    frames = [pq.read_table(str(f)).to_pandas().drop(columns=["geometry"], errors="ignore")
              for f in sorted(SILVER.glob("*.geoparquet"))]
    silver_long = pd.concat(frames, ignore_index=True)
    g2ck = pd.read_parquet(GOLD / "geom_obs_to_ck.parquet")
    silver_ck = silver_long.merge(g2ck, on="geom_obs_id", how="left")

    alias_reg = pd.read_parquet(GOLD / "name_alias_registry.parquet") \
                if (GOLD / "name_alias_registry.parquet").exists() else pd.DataFrame()

    print(f"  Matrix       : {len(matrix)} rows")
    print(f"  Lineage      : {len(lineage)} edges")
    print(f"  Silver       : {len(silver_long)} observations")
    print(f"  Alias reg.   : {len(alias_reg)} entries")
    return matrix, lineage, ck_reg, silver_ck, alias_reg


def build_indexes(lineage, matrix):
    """Build (a) predecessor index using OUTFLOW_SHARE, (b) area lookup."""
    preds_of = defaultdict(list)
    for _, row in lineage.iterrows():
        preds_of[row["to_ck"]].append({
            "from_ck":       row["from_ck"],
            "year_a":        int(row["year_a"]),
            "outflow_share": float(row["outflow_share"]),
            "confidence":    float(row.get("lineage_confidence", 0.7)),
        })

    area_lkp = {}
    for _, row in matrix.iterrows():
        for y in YEARS:
            v = row.get(f"area_km2_{y}")
            if pd.notna(v):
                area_lkp[(row["canonical_key"], y)] = float(v)

    return preds_of, area_lkp


def chain_earliest_year(ck, preds_of, ck_est_map, visited=None):
    """Recursively find the earliest census year any ancestor existed."""
    if visited is None: visited = set()
    if ck in visited: return 9999
    visited.add(ck)
    preds = preds_of.get(ck, [])
    if not preds:
        return int(ck_est_map.get(ck, 9999) or 9999)
    return min(chain_earliest_year(e["from_ck"], preds_of, ck_est_map, set(visited))
               for e in preds)


def get_area(ck, year, area_lkp, preds_of, visited=None, depth=0):
    """
    Returns (area_km2, method, source_cks_str, confidence).
    Uses outflow_share: historical territory of CK in 'year' =
      sum of predecessor_area × outflow_share[predecessor → ck]
    """
    if visited is None: visited = set()
    if (ck, year) in visited or depth > 15:
        return None, NULL_UNRESOLVED, "", 0.0
    visited.add((ck, year))

    # Direct observation
    if (ck, year) in area_lkp:
        return area_lkp[(ck, year)], DIRECT, ck, 1.0

    preds = preds_of.get(ck, [])
    if not preds:
        return None, NULL_UNRESOLVED, "", 0.0

    # Predecessors whose transition is at or after the target year
    relevant = [e for e in preds if e["year_a"] >= year]
    if not relevant:
        earliest = min(e["year_a"] for e in preds)
        relevant = [e for e in preds if e["year_a"] == earliest]

    total_area = 0.0
    source_cks = []
    min_conf   = 1.0

    for edge in relevant:
        sub_area, _, sub_src, sub_conf = get_area(
            edge["from_ck"], year, area_lkp, preds_of,
            visited=set(visited), depth=depth + 1
        )
        if sub_area is not None and sub_area > 0:
            total_area += sub_area * edge["outflow_share"]   # ← outflow_share fix
            source_cks.append(edge["from_ck"])
            min_conf = min(min_conf, sub_conf * edge["confidence"])

    if total_area > 0:
        method = DIRECT_PRED \
                 if (len(relevant) == 1 and relevant[0]["outflow_share"] >= 0.95) \
                 else LINEAGE_FILL
        return total_area, method, "|".join(source_cks), round(min_conf, 4)

    return None, NULL_UNRESOLVED, "", 0.0


def try_alias_resolve(ck, name, state, year, silver_ck, alias_reg):
    """Alias resolution via documentary-backed registry only."""
    if alias_reg.empty:
        return None, "", 0.0
    if "evidence_type" in alias_reg.columns:
        doc = alias_reg[alias_reg["evidence_type"].isin(
            ["DOCUMENTARY", "MANUAL_VERIFIED", "EXISTING_REGISTRY"])]
    else:
        doc = alias_reg
    if doc.empty or "alias_name_norm" not in doc.columns:
        return None, "", 0.0

    name_norm = str(name).lower().replace(" ","").replace("-","").replace("&","and")
    matches = doc[doc.get("district_name_norm", pd.Series(dtype=str))
                    .str.lower().str.replace(" ","").str.replace("-","") == name_norm]
    for _, arow in matches.iterrows():
        alias_n = str(arow.get("alias_name_norm","")).lower().replace(" ","")
        hits = silver_ck[
            (silver_ck["source_year"] == year) &
            (silver_ck["district_name_norm"].str.lower().str.replace(" ","") == alias_n)
        ]
        if not hits.empty:
            area = float(hits["area_km2"].sum())
            src  = "|".join(hits["canonical_key"].dropna().astype(str).unique())
            return area, src, 0.8
    return None, "", 0.0


def run_backfill(matrix, lineage, ck_reg, silver_ck, alias_reg):
    print("\nBuilding indexes...")
    preds_of, area_lkp = build_indexes(lineage, matrix)

    ck_est_map = {r["canonical_key"]: r.get("established_year")
                  for _, r in ck_reg.iterrows()}

    active = matrix[matrix["area_km2_2025"].notna()].copy()
    print(f"Computing chain-earliest years for {len(active)} active districts...")
    chain_est = {row["canonical_key"]: chain_earliest_year(
                     row["canonical_key"], preds_of, ck_est_map)
                 for _, row in active.iterrows()}

    results, audit = [], []
    for idx, (_, row) in enumerate(active.iterrows()):
        ck    = row["canonical_key"]
        name  = str(row.get("display_name",""))
        state = str(row.get("state_at_creation",""))
        nr    = {"canonical_key": ck, "display_name": name,
                 "state_at_creation": state,
                 "established_year":  row.get("established_year"),
                 "closed_year":       row.get("closed_year"),
                 "is_active":         row.get("is_active"),
                 "origin_kind":       row.get("origin_kind")}

        earliest = chain_est.get(ck, 9999)

        for y in YEARS:
            col     = f"area_km2_{y}"
            raw_val = row.get(col)

            if pd.notna(raw_val):
                nr[col]                    = raw_val
                nr[f"fill_method_{y}"]     = DIRECT
                nr[f"fill_source_ck_{y}"]  = ck
                nr[f"fill_confidence_{y}"] = 1.0
                audit.append({"canonical_key": ck, "display_name": name, "state": state,
                               "census_year": y, "area_km2": raw_val,
                               "fill_method": DIRECT, "fill_source_ck": ck,
                               "fill_confidence": 1.0})
                continue

            # Year before any ancestor existed → NULL_CORRECT
            if y < earliest:
                nr[col]                    = None
                nr[f"fill_method_{y}"]     = NULL_CORRECT
                nr[f"fill_source_ck_{y}"]  = ""
                nr[f"fill_confidence_{y}"] = 0.0
                audit.append({"canonical_key": ck, "display_name": name, "state": state,
                               "census_year": y, "area_km2": None,
                               "fill_method": NULL_CORRECT, "fill_source_ck": "",
                               "fill_confidence": 0.0})
                continue

            # Lineage backfill (outflow_share)
            area, method, src, conf = get_area(ck, y, area_lkp, preds_of)

            if area is None or area <= 0:
                # Alias resolution
                area, src, conf = try_alias_resolve(ck, name, state, y, silver_ck, alias_reg)
                method = ALIAS_RESOLVED if (area and area > 0) else NULL_UNRESOLVED

            nr[col]                    = round(area, 6) if area else None
            nr[f"fill_method_{y}"]     = method
            nr[f"fill_source_ck_{y}"]  = src if area else ""
            nr[f"fill_confidence_{y}"] = conf if area else 0.0
            audit.append({"canonical_key": ck, "display_name": name, "state": state,
                           "census_year": y, "area_km2": nr[col],
                           "fill_method": method, "fill_source_ck": nr[f"fill_source_ck_{y}"],
                           "fill_confidence": nr[f"fill_confidence_{y}"]})

        results.append(nr)
        if (idx + 1) % 100 == 0:
            print(f"  {idx+1}/{len(active)} done...")

    return pd.DataFrame(results), pd.DataFrame(audit)


def order_columns(df):
    meta   = ["canonical_key","display_name","state_at_creation",
               "established_year","closed_year","is_active","origin_kind"]
    blocks = []
    for y in YEARS:
        blocks += [f"area_km2_{y}", f"fill_method_{y}",
                   f"fill_source_ck_{y}", f"fill_confidence_{y}"]
    cols = [c for c in meta if c in df.columns] + \
           [c for c in blocks if c in df.columns]
    return df[cols]


def print_qc(results, audit):
    print()
    print("=" * 65)
    print("S11 BACKFILL QC REPORT")
    print("=" * 65)
    print(f"Output rows (742 active districts): {len(results)}")
    print()
    print("Fill method totals:")
    mc = audit["fill_method"].value_counts()
    for m, c in mc.items():
        print(f"  {m:<30}: {c:>6}")
    print()
    print(f"{'Year':<6} {'Filled':>6} {'Backfill':>9} {'NULL_C':>7} {'NULL_U':>7}  {'India_total_km2':>16}")
    print("-" * 60)
    for y in YEARS:
        sub = audit[audit["census_year"] == y]
        filled   = sub["area_km2"].notna().sum()
        backfill = sub[sub["fill_method"].isin([DIRECT_PRED, LINEAGE_FILL, ALIAS_RESOLVED])].shape[0]
        nc  = (sub["fill_method"] == NULL_CORRECT).sum()
        nu  = (sub["fill_method"] == NULL_UNRESOLVED).sum()
        tot = sub["area_km2"].sum()
        print(f"  {y}  {filled:>6}  {backfill:>9}  {nc:>7}  {nu:>7}  {tot:>16,.1f}")
    print()
    print("Verification — 2025 total (must match pre-backfill 3,270,051 km²):")
    t2025 = audit[audit["census_year"] == 2025]["area_km2"].sum()
    print(f"  2025 total = {t2025:,.1f} km²  {'✅ OK' if abs(t2025 - 3270051.4) < 100 else '❌ MISMATCH'}")


def main():
    print("=" * 65)
    print("STAGE 11 — DISTRICT CENSUS AREA BACKFILL ENGINE  v2")
    print("=" * 65)
    matrix, lineage, ck_reg, silver_ck, alias_reg = load_inputs()
    results, audit = run_backfill(matrix, lineage, ck_reg, silver_ck, alias_reg)
    results = order_columns(results)

    print("\nWriting outputs...")
    results.to_csv(PP / "district_census_area_matrix_backfilled.csv", index=False)
    results.to_parquet(PP / "district_census_area_matrix_backfilled.parquet", index=False)
    audit.to_csv(PP / "district_area_fill_audit.csv", index=False)
    print(f"  district_census_area_matrix_backfilled.csv  ({len(results)} rows)")
    print(f"  district_census_area_matrix_backfilled.parquet")
    print(f"  district_area_fill_audit.csv  ({len(audit)} rows)")
    print_qc(results, audit)


if __name__ == "__main__":
    main()
