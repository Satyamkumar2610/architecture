"""
Stage 4 — The transition matrix: the one primitive lineage, area accounting,
and statistical harmonization all derive from (redesign doc section 4.1).

For each consecutive vintage pair (V_t, V_{t+1}), compute the planar overlay
of the two layers. Each output parcel belongs to exactly one source district
and one target district, so the parcels partition the overlap: this is what
gives conservation "by construction" instead of the old pipeline's pairwise-
intersection-then-normalize approach (section 2.2/2.3), which had no
conservation property and produced weight sums of 5.32 and 24.25.

Per-source-district identity:
    sum(parcel areas to all targets) + lost_to_unmapped + sliver = area(source)

Sliver parcels (topological noise from two independently-digitized layers
sharing a boundary) are filtered by lib.sliver_threshold_km2 and reported,
not silently dropped — this is the "ignore small differences, but as a
named edge case" requirement.
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


def load_layer(source: str, year: int) -> gpd.GeoDataFrame:
    path = lib.SILVER_GEOM_DIR / f"{source}_{year}.geoparquet"
    gdf = gpd.read_parquet(path)
    return gdf


def compute_pair(source_a: str, year_a: int, source_b: str, year_b: int) -> dict:
    print(f"\n=== Transition {year_a} -> {year_b} ===")
    a = load_layer(source_a, year_a)
    b = load_layer(source_b, year_b)

    a_slim = a[["geom_obs_id", "source_pk", "district_name_std", "state_name_std",
                "area_km2", "geometry"]].rename(columns={"area_km2": "area_km2_a"})
    b_slim = b[["geom_obs_id", "source_pk", "district_name_std", "state_name_std",
                "area_km2", "geometry"]].rename(columns={"area_km2": "area_km2_b"})

    overlay = gpd.overlay(
        a_slim, b_slim, how="intersection", keep_geom_type=True
    )
    print(f"  raw overlay parcels: {len(overlay)}")

    overlay["parcel_area_km2"] = overlay.geometry.apply(lib.geodesic_area_km2)
    overlay = overlay[overlay["parcel_area_km2"] > 0].copy()

    threshold = overlay.apply(
        lambda r: lib.sliver_threshold_km2(r["area_km2_a"], r["area_km2_b"]), axis=1
    )
    overlay["is_sliver"] = overlay["parcel_area_km2"] < threshold

    parcels_path = lib.GOLD_SPATIAL_DIR / f"parcels_{year_a}_{year_b}.parquet"
    out_cols = [
        "geom_obs_id_1", "source_pk_1", "district_name_std_1", "state_name_std_1", "area_km2_a",
        "geom_obs_id_2", "source_pk_2", "district_name_std_2", "state_name_std_2", "area_km2_b",
        "parcel_area_km2", "is_sliver",
    ]
    overlay_out = overlay.rename(columns={
        "geom_obs_id_1": "geom_obs_id_1", "geom_obs_id_2": "geom_obs_id_2",
    })
    # geopandas overlay suffixes duplicate column names with _1 / _2
    overlay_out[out_cols].to_parquet(parcels_path)
    print(f"  wrote {parcels_path}")

    real = overlay[~overlay["is_sliver"]]
    sliver = overlay[overlay["is_sliver"]]

    matrix = (
        real.groupby(["geom_obs_id_1", "geom_obs_id_2"], as_index=False)["parcel_area_km2"]
        .sum()
        .rename(columns={"parcel_area_km2": "transition_area_km2"})
    )

    sliver_by_source = (
        sliver.groupby("geom_obs_id_1", as_index=False)["parcel_area_km2"]
        .sum()
        .rename(columns={"parcel_area_km2": "sliver_area_km2"})
    )

    covered_by_source = (
        overlay.groupby("geom_obs_id_1", as_index=False)["parcel_area_km2"]
        .sum()
        .rename(columns={"parcel_area_km2": "covered_area_km2"})
    )
    residual_a = a_slim[["geom_obs_id", "source_pk", "district_name_std", "state_name_std", "area_km2_a"]].merge(
        covered_by_source, left_on="geom_obs_id", right_on="geom_obs_id_1", how="left"
    )
    residual_a["covered_area_km2"] = residual_a["covered_area_km2"].fillna(0.0)
    residual_a["lost_to_unmapped_km2"] = (
        residual_a["area_km2_a"] - residual_a["covered_area_km2"]
    ).clip(lower=0.0)

    covered_by_target = (
        overlay.groupby("geom_obs_id_2", as_index=False)["parcel_area_km2"]
        .sum()
        .rename(columns={"parcel_area_km2": "covered_area_km2"})
    )
    residual_b = b_slim[["geom_obs_id", "source_pk", "district_name_std", "state_name_std", "area_km2_b"]].merge(
        covered_by_target, left_on="geom_obs_id", right_on="geom_obs_id_2", how="left"
    )
    residual_b["covered_area_km2"] = residual_b["covered_area_km2"].fillna(0.0)
    residual_b["gained_from_unmapped_km2"] = (
        residual_b["area_km2_b"] - residual_b["covered_area_km2"]
    ).clip(lower=0.0)

    matrix_path = lib.GOLD_SPATIAL_DIR / f"matrix_{year_a}_{year_b}.parquet"
    matrix.to_parquet(matrix_path)

    residual_path = lib.GOLD_SPATIAL_DIR / f"residual_{year_a}_{year_b}.parquet"
    residual_a_out = residual_a[["geom_obs_id", "source_pk", "district_name_std", "state_name_std",
                                   "area_km2_a", "lost_to_unmapped_km2"]].merge(
        sliver_by_source, left_on="geom_obs_id", right_on="geom_obs_id_1", how="left"
    )
    residual_a_out["sliver_area_km2"] = residual_a_out["sliver_area_km2"].fillna(0.0)
    residual_a_out = residual_a_out.drop(columns=["geom_obs_id_1"], errors="ignore")
    residual_a_out.to_parquet(residual_path)

    residual_b_path = lib.GOLD_SPATIAL_DIR / f"residual_target_{year_a}_{year_b}.parquet"
    residual_b[["geom_obs_id", "source_pk", "district_name_std", "state_name_std",
                "area_km2_b", "gained_from_unmapped_km2"]].to_parquet(residual_b_path)

    # V3 closure check: Σ transition + lost + sliver == area(source), per source
    check = matrix.groupby("geom_obs_id_1", as_index=False)["transition_area_km2"].sum()
    check = check.merge(residual_a_out[["geom_obs_id", "area_km2_a", "lost_to_unmapped_km2", "sliver_area_km2"]],
                         left_on="geom_obs_id_1", right_on="geom_obs_id", how="right")
    check["transition_area_km2"] = check["transition_area_km2"].fillna(0.0)
    check["closure_total"] = (
        check["transition_area_km2"] + check["lost_to_unmapped_km2"] + check["sliver_area_km2"]
    )
    check["closure_error_km2"] = (check["closure_total"] - check["area_km2_a"]).abs()
    max_closure_error = float(check["closure_error_km2"].max())
    closure_tol = np.maximum(lib.CLOSURE_TOLERANCE_KM2,
                              check["area_km2_a"] * lib.CLOSURE_TOLERANCE_PCT / 100.0)
    bad_closure = check[check["closure_error_km2"] > closure_tol]

    total_a_area = float(a_slim["area_km2_a"].sum())
    total_matrix = float(matrix["transition_area_km2"].sum())
    total_lost = float(residual_a_out["lost_to_unmapped_km2"].sum())
    total_sliver = float(residual_a_out["sliver_area_km2"].sum())
    total_gained = float(residual_b["gained_from_unmapped_km2"].sum())

    summary = {
        "year_a": year_a, "year_b": year_b,
        "n_source": len(a_slim), "n_target": len(b_slim),
        "n_parcels_total": len(overlay), "n_parcels_sliver": len(sliver),
        "total_source_area_km2": round(total_a_area, 2),
        "total_transition_area_km2": round(total_matrix, 2),
        "total_lost_km2": round(total_lost, 2),
        "total_sliver_km2": round(total_sliver, 2),
        "total_gained_km2": round(total_gained, 2),
        "max_closure_error_km2": round(max_closure_error, 4),
        "n_closure_violations": len(bad_closure),
    }
    print(f"  {summary}")

    if len(bad_closure):
        print(f"  WARNING: {len(bad_closure)} source districts fail closure beyond tolerance")

    return summary


def main() -> None:
    summaries = []
    for i in range(len(VINTAGES) - 1):
        source_a, year_a = VINTAGES[i]
        source_b, year_b = VINTAGES[i + 1]
        summaries.append(compute_pair(source_a, year_a, source_b, year_b))

    report = pd.DataFrame(summaries)
    out_csv = lib.OUTPUT_DIR / "s4_transition_summary.csv"
    report.to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv}")
    print(report.to_string(index=False))


if __name__ == "__main__":
    main()
