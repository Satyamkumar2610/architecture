"""
Stage 8 — Event area accounting: resolve each real administrative event
(Stage 3) to the CKs it involves and read the transferred area directly off
the transition matrix (Stage 4/5), which is a true geometric partition and
therefore does not require the old system's per-event pairwise-intersection-
then-normalize workaround.

For each event, participant names are resolved to CKs using a per-vintage
(state, name) index -- predecessors at the pre-event vintage, successors at
the post-event vintage -- with the same two-tier (state-scoped, then name-
only) fallback used for corroboration in Stage 6, for the same reason:
state-name drift across sources is real and should not manufacture
UNRESOLVED findings.

measurement_status is honest about what the evidence supports:
  MEASURED               all participants resolved to a CK; area read from
                          the matrix for at least one resolved pair
  RESOLVED_NO_OVERLAP     participants resolved but no material transition
                          exists between them in this window (a real
                          finding -- not fabricated, not silently dropped)
  PARTIALLY_RESOLVED      some but not all participants resolved
  UNRESOLVED_NAME         no participant name resolved to a CK
  NO_BRACKETING_VINTAGE   event year falls outside [1951, 2025]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

VINTAGES = [1951, 1961, 1971, 1981, 1991, 2001, 2011, 2021, 2025]


def build_vintage_name_index() -> dict[int, dict]:
    """For each vintage: (state_norm, name_norm) -> ck, and name_norm -> [cks] fallback."""
    obs_ck = pd.read_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")
    obs_to_ck = dict(zip(obs_ck["geom_obs_id"], obs_ck["canonical_key"]))

    sources_by_year = {
        1951: "stanford", 1961: "stanford", 1971: "stanford", 1981: "stanford",
        1991: "stanford", 2001: "stanford", 2011: "stanford", 2021: "stanford", 2025: "soi",
    }
    index = {}
    for year in VINTAGES:
        src = sources_by_year[year]
        gdf = pd.read_parquet(lib.SILVER_GEOM_DIR / f"{src}_{year}.geoparquet").drop(columns="geometry")
        by_state: dict[tuple[str, str], str] = {}
        by_name: dict[str, list[str]] = {}
        for _, row in gdf.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck is None:
                continue
            key = (row["state_name_norm"], row["district_name_norm"])
            by_state[key] = ck
            by_name.setdefault(row["district_name_norm"], []).append(ck)
        index[year] = {"by_state": by_state, "by_name": by_name}
    return index


def resolve(name: str, state: str, year: int, index: dict) -> tuple[str | None, str]:
    idx = index.get(year)
    if idx is None:
        return None, "NO_VINTAGE"
    name_norm = lib.normalize_name(name)
    state_norm = lib.normalize_name(state)
    ck = idx["by_state"].get((state_norm, name_norm))
    if ck:
        return ck, "STATE_SCOPED"
    candidates = idx["by_name"].get(name_norm, [])
    if len(candidates) == 1:
        return candidates[0], "NAME_ONLY"
    return None, "UNRESOLVED"


def bracket(effective_year: int) -> tuple[int | None, int | None]:
    pre = max((v for v in VINTAGES if v < effective_year), default=None)
    post = min((v for v in VINTAGES if v > effective_year), default=None)
    return pre, post


def main() -> None:
    events = pd.read_parquet(lib.GOLD_EVENTS_DIR / "boundary_event.parquet")
    participants = pd.read_parquet(lib.GOLD_EVENTS_DIR / "event_participant.parquet")
    name_index = build_vintage_name_index()

    matrices = {}
    for i in range(len(VINTAGES) - 1):
        ya, yb = VINTAGES[i], VINTAGES[i + 1]
        matrices[(ya, yb)] = pd.read_parquet(lib.GOLD_SPATIAL_DIR / f"matrix_{ya}_{yb}.parquet").set_index(
            ["geom_obs_id_1", "geom_obs_id_2"]
        )["transition_area_km2"]

    obs_ck = pd.read_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")
    ck_to_obs: dict[tuple[str, int], str] = {}
    sources_by_year = {y: ("stanford" if y != 2025 else "soi") for y in VINTAGES}
    for year in VINTAGES:
        gdf = pd.read_parquet(lib.SILVER_GEOM_DIR / f"{sources_by_year[year]}_{year}.geoparquet").drop(columns="geometry")
        obs_to_ck_map = dict(zip(obs_ck["geom_obs_id"], obs_ck["canonical_key"]))
        for _, row in gdf.iterrows():
            ck = obs_to_ck_map.get(row["geom_obs_id"])
            if ck:
                ck_to_obs[(ck, year)] = row["geom_obs_id"]

    rows = []
    by_event = participants.groupby("event_id")
    for _, ev in events.iterrows():
        event_id = ev["event_id"]
        year = int(ev["effective_year"])
        pre, post = bracket(year)

        if pre is None or post is None:
            rows.append({
                "event_id": event_id, "event_type": ev["event_type"], "effective_year": year,
                "pre_vintage": pre, "post_vintage": post,
                "predecessor": None, "successor": None,
                "predecessor_ck": None, "successor_ck": None,
                "transferred_area_km2": None, "measurement_status": "NO_BRACKETING_VINTAGE",
            })
            continue

        parts = by_event.get_group(event_id) if event_id in by_event.groups else pd.DataFrame()
        preds = parts[parts["role"] == "PREDECESSOR"]
        succs = parts[parts["role"] == "SUCCESSOR"]

        pred_resolved = [(r["district_name_raw"], *resolve(r["district_name_raw"], r["state"], pre, name_index))
                          for _, r in preds.iterrows()]
        succ_resolved = [(r["district_name_raw"], *resolve(r["district_name_raw"], r["state"], post, name_index))
                          for _, r in succs.iterrows()]

        n_total = len(pred_resolved) + len(succ_resolved)
        n_resolved = sum(1 for *_, ck, _ in pred_resolved if ck) + sum(1 for *_, ck, _ in succ_resolved if ck)

        if n_resolved == 0:
            status_all = "UNRESOLVED_NAME"
        elif n_resolved < n_total:
            status_all = "PARTIALLY_RESOLVED"
        else:
            status_all = None  # determined per-pair below

        any_measured = False
        pair_rows = []
        for pred_name, pred_ck, pred_method in pred_resolved:
            for succ_name, succ_ck, succ_method in succ_resolved:
                if not pred_ck or not succ_ck:
                    continue
                pred_obs = ck_to_obs.get((pred_ck, pre))
                succ_obs = ck_to_obs.get((succ_ck, post))
                area = None
                if pred_obs and succ_obs:
                    area = matrices.get((pre, post), pd.Series(dtype=float)).get((pred_obs, succ_obs))
                pair_status = "MEASURED" if area is not None and area > 0 else "RESOLVED_NO_OVERLAP"
                if pair_status == "MEASURED":
                    any_measured = True
                pair_rows.append({
                    "event_id": event_id, "event_type": ev["event_type"], "effective_year": year,
                    "pre_vintage": pre, "post_vintage": post,
                    "predecessor": pred_name, "successor": succ_name,
                    "predecessor_ck": pred_ck, "successor_ck": succ_ck,
                    "predecessor_match_method": pred_method, "successor_match_method": succ_method,
                    "transferred_area_km2": round(float(area), 4) if area is not None else None,
                    "measurement_status": status_all or pair_status,
                })

        if pair_rows:
            rows.extend(pair_rows)
        else:
            rows.append({
                "event_id": event_id, "event_type": ev["event_type"], "effective_year": year,
                "pre_vintage": pre, "post_vintage": post,
                "predecessor": "; ".join(n for n, *_ in pred_resolved) or None,
                "successor": "; ".join(n for n, *_ in succ_resolved) or None,
                "predecessor_ck": None, "successor_ck": None,
                "transferred_area_km2": None, "measurement_status": status_all or "UNRESOLVED_NAME",
            })

    out = pd.DataFrame(rows)
    out.to_parquet(lib.PRODUCTS_DIR / "event_area_accounting.parquet")
    out.to_csv(lib.PRODUCTS_DIR / "event_area_accounting.csv", index=False)

    print(f"event_area_accounting rows: {len(out)} (from {len(events)} events)")
    print(out["measurement_status"].value_counts())
    measured = out[out["measurement_status"] == "MEASURED"]
    print(f"\nTotal measured transferred area across all events: {measured['transferred_area_km2'].sum():,.1f} km2")
    print(f"Events with >=1 MEASURED pair: {out.loc[out.measurement_status=='MEASURED','event_id'].nunique()} / {events['event_id'].nunique()}")

    print(f"\nWrote {lib.PRODUCTS_DIR / 'event_area_accounting.csv'}")


if __name__ == "__main__":
    main()
