"""
Stage 5 — Continuity-based identity (canonical keys).

Fixes the root cause in section 2.4/RC-3 of the redesign doc: the old
pipeline assigned identity by exact (name, state) string match, with zero
spatial or temporal evidence, and hardcoded "confidence" literals. Identity
IS a territorial question — this stage answers it from the transition
matrix built in Stage 4, treating name similarity only as a corroborating
signal recorded alongside the decision, never as the decision itself.

For source i (vintage t) and target j (vintage t+1):
    r_i = M[i, best_j] / area_i(t)       -- outflow retention share
    h_j = M[best_i, j] / area_j(t+1)     -- inflow inheritance share

Same CK continues i -> j only when i and j are each other's best match AND
both shares clear CONTINUITY_THRESHOLD (0.90 default, configured in lib.py
with rationale). Otherwise j gets a new CK, and the (possibly several)
sources with material inflow into j are recorded as lineage evidence for
Stage 6 -- this is what replaces the old ambiguous-CK/vintage problem
(105 groups in the old system) structurally: a target's continuity match
is derived from area shares, not from two rows racing to claim one name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

VINTAGES = [
    ("stanford", 1951), ("stanford", 1961), ("stanford", 1971),
    ("stanford", 1981), ("stanford", 1991), ("stanford", 2001),
    ("stanford", 2011), ("stanford", 2021), ("soi", 2025),
]


def load_layer_slim(source: str, year: int) -> pd.DataFrame:
    gdf = gpd.read_parquet(lib.SILVER_GEOM_DIR / f"{source}_{year}.geoparquet")
    return pd.DataFrame(gdf.drop(columns="geometry"))


def best_matches(matrix: pd.DataFrame, area_a: pd.Series, area_b: pd.Series) -> pd.DataFrame:
    """For every source, its best target and retention share; vice versa."""
    m = matrix.copy()
    m["area_a"] = m["geom_obs_id_1"].map(area_a)
    m["area_b"] = m["geom_obs_id_2"].map(area_b)
    m["outflow_share"] = m["transition_area_km2"] / m["area_a"]
    m["inflow_share"] = m["transition_area_km2"] / m["area_b"]

    best_from_a = m.loc[m.groupby("geom_obs_id_1")["outflow_share"].idxmax()].set_index("geom_obs_id_1")
    best_from_b = m.loc[m.groupby("geom_obs_id_2")["inflow_share"].idxmax()].set_index("geom_obs_id_2")
    return m, best_from_a, best_from_b


def main() -> None:
    # canonical_key_registry
    registry_rows = []
    # mapping: geom_obs_id -> canonical_key  (per vintage, filled as we go)
    ck_for_obs: dict[str, str] = {}
    # continuity edges for Stage 6 lineage: (source_obs, target_obs, kind)
    continuity_rows = []
    ck_seq = 0

    def new_ck() -> str:
        nonlocal ck_seq
        ck_seq += 1
        return f"IND-{ck_seq:06d}"

    # --- Anchor year: 1951 ---
    src, yr = VINTAGES[0]
    anchor = load_layer_slim(src, yr)
    for _, row in anchor.iterrows():
        ck = new_ck()
        ck_for_obs[row["geom_obs_id"]] = ck
        registry_rows.append({
            "canonical_key": ck, "established_year": yr, "established_source": src,
            "established_source_pk": row["source_pk"],
            "display_name": row["district_name_std"], "state_at_creation": row["state_name_std"],
            "closed_year": None, "is_active": True,
            "origin_kind": "ANCHOR",
        })
    print(f"[{yr}] anchor year: {len(anchor)} CKs allocated")

    match_diagnostics = []

    for i in range(len(VINTAGES) - 1):
        src_a, yr_a = VINTAGES[i]
        src_b, yr_b = VINTAGES[i + 1]
        a = load_layer_slim(src_a, yr_a).set_index("geom_obs_id")
        b = load_layer_slim(src_b, yr_b).set_index("geom_obs_id")
        matrix = pd.read_parquet(lib.GOLD_SPATIAL_DIR / f"matrix_{yr_a}_{yr_b}.parquet")

        m, best_from_a, best_from_b = best_matches(matrix, a["area_km2"], b["area_km2"])

        n_continuous = 0
        n_new = 0
        for obs_b, row in b.iterrows():
            candidate = best_from_b.loc[obs_b] if obs_b in best_from_b.index else None
            is_continuous = False
            if candidate is not None:
                obs_a = candidate["geom_obs_id_1"]
                h_j = candidate["inflow_share"]
                # mutual check: is obs_b also obs_a's best outflow target?
                if obs_a in best_from_a.index:
                    reciprocal = best_from_a.loc[obs_a]
                    if reciprocal["geom_obs_id_2"] == obs_b:
                        r_i = reciprocal["outflow_share"]
                        if h_j >= lib.CONTINUITY_THRESHOLD and r_i >= lib.CONTINUITY_THRESHOLD:
                            is_continuous = True

            if is_continuous:
                ck = ck_for_obs.get(obs_a)
                if ck is None:
                    # source wasn't tracked (shouldn't happen after anchor year) - defensive
                    ck = new_ck()
                ck_for_obs[obs_b] = ck
                n_continuous += 1
                match_diagnostics.append({
                    "year_a": yr_a, "year_b": yr_b, "geom_obs_id_a": obs_a, "geom_obs_id_b": obs_b,
                    "name_a": a.loc[obs_a, "district_name_std"], "name_b": row["district_name_std"],
                    "outflow_share": round(float(r_i), 4), "inflow_share": round(float(h_j), 4),
                    "name_match": a.loc[obs_a, "district_name_norm"] == row["district_name_norm"],
                    "decision": "CONTINUITY",
                })
            else:
                ck = new_ck()
                ck_for_obs[obs_b] = ck
                n_new += 1
                registry_rows.append({
                    "canonical_key": ck, "established_year": yr_b, "established_source": src_b,
                    "established_source_pk": row["source_pk"],
                    "display_name": row["district_name_std"], "state_at_creation": row["state_name_std"],
                    "closed_year": None, "is_active": True,
                    "origin_kind": "NEW",
                })

        # sources with no continuity successor -> closed at yr_b
        this_pair_continuous_sources = {
            row["geom_obs_id_a"] for row in match_diagnostics
            if row["year_a"] == yr_a and row["year_b"] == yr_b
        }
        for obs_a in a.index:
            if obs_a not in this_pair_continuous_sources:
                ck = ck_for_obs.get(obs_a)
                if ck is not None:
                    for r in registry_rows:
                        if r["canonical_key"] == ck and r["is_active"]:
                            r["is_active"] = False
                            r["closed_year"] = yr_b

        # record ALL material transitions (not just continuity-selected ones)
        # as evidence for Stage 6 lineage, tagged with the CKs on each side.
        material = m[m["transition_area_km2"] >= lib.SLIVER_ABS_KM2]
        for _, mrow in material.iterrows():
            continuity_rows.append({
                "year_a": yr_a, "year_b": yr_b,
                "from_ck": ck_for_obs.get(mrow["geom_obs_id_1"]),
                "to_ck": ck_for_obs.get(mrow["geom_obs_id_2"]),
                "from_geom_obs_id": mrow["geom_obs_id_1"], "to_geom_obs_id": mrow["geom_obs_id_2"],
                "transition_area_km2": mrow["transition_area_km2"],
                "outflow_share": mrow["outflow_share"], "inflow_share": mrow["inflow_share"],
            })

        print(f"[{yr_a}->{yr_b}] continuity={n_continuous} new_ck={n_new} "
              f"closed={len(a) - len(this_pair_continuous_sources)}")

    registry_df = pd.DataFrame(registry_rows)
    registry_df.to_parquet(lib.GOLD_CORE_DIR / "canonical_key_registry.parquet")
    registry_df.to_csv(lib.OUTPUT_DIR / "s5_canonical_key_registry.csv", index=False)

    # obs -> ck mapping for every vintage (needed by later stages)
    obs_ck_rows = [{"geom_obs_id": k, "canonical_key": v} for k, v in ck_for_obs.items()]
    obs_ck_df = pd.DataFrame(obs_ck_rows)
    obs_ck_df.to_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")

    diag_df = pd.DataFrame(match_diagnostics)
    diag_df.to_csv(lib.OUTPUT_DIR / "s5_continuity_diagnostics.csv", index=False)

    continuity_df = pd.DataFrame(continuity_rows)
    continuity_df.to_parquet(lib.GOLD_SPATIAL_DIR / "ck_transitions.parquet")

    print(f"\nTotal CKs allocated: {ck_seq}")
    print(f"Active CKs at 2025: {registry_df['is_active'].sum()}")
    print(f"Closed CKs: {(~registry_df['is_active']).sum()}")
    if not diag_df.empty:
        print(f"Continuity decisions where name also matched: "
              f"{diag_df['name_match'].sum()} / {len(diag_df)} "
              f"({100*diag_df['name_match'].mean():.1f}%)")
        print(f"Continuity decisions where name DIFFERED (rename-with-continuity): "
              f"{(~diag_df['name_match']).sum()}")

    print(f"\nWrote {lib.GOLD_CORE_DIR / 'canonical_key_registry.parquet'}")
    print(f"Wrote {lib.GOLD_CORE_DIR / 'geom_obs_to_ck.parquet'}")
    print(f"Wrote {lib.GOLD_SPATIAL_DIR / 'ck_transitions.parquet'}")


if __name__ == "__main__":
    main()
