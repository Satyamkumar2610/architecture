"""
Stage 7 — District area ledger: how did each district's territory change,
decade by decade, and where did it go.

Built entirely from the transition matrix (Stage 4) and CK resolution
(Stage 5) -- no CSV event matching required, so this product is exact by
construction: for every source CK in every window,

    area_retained + area_relinquished_as_lineage + area_transferred_out
        + lost_to_unmapped + sliver  ==  area(source)

(within the closure tolerance established empirically in Stage 4 / lib.py).
This is the "one primitive, three products" idea from the redesign doc:
lineage (Stage 6) and this ledger are two views of the same matrix, so they
cannot disagree with each other the way the old three-code-path pipeline did.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

VINTAGES = [1951, 1961, 1971, 1981, 1991, 2001, 2011, 2021, 2025]


def main() -> None:
    registry = pd.read_parquet(lib.GOLD_CORE_DIR / "canonical_key_registry.parquet")
    obs_ck = pd.read_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")
    obs_to_ck = dict(zip(obs_ck["geom_obs_id"], obs_ck["canonical_key"]))

    transitions = pd.read_parquet(lib.GOLD_SPATIAL_DIR / "ck_transitions.parquet")
    relationship = pd.read_parquet(lib.GOLD_EVENTS_DIR / "district_relationship.parquet")
    transfer = pd.read_parquet(lib.GOLD_SPATIAL_DIR / "measured_area_transfer.parquet")
    lineage_ids = set(zip(relationship["from_ck"], relationship["to_ck"], relationship["year_a"], relationship["year_b"]))

    rows = []
    for i in range(len(VINTAGES) - 1):
        year_a, year_b = VINTAGES[i], VINTAGES[i + 1]
        source = "stanford" if year_a <= 2021 else "soi"
        source_prev = "stanford" if year_a != 2025 else "soi"
        source_b = "stanford" if year_b <= 2021 else "soi"

        a = pd.read_parquet(lib.SILVER_GEOM_DIR / f"{source_prev}_{year_a}.geoparquet").drop(columns="geometry")
        residual = pd.read_parquet(lib.GOLD_SPATIAL_DIR / f"residual_{year_a}_{year_b}.parquet")

        m = transitions[(transitions["year_a"] == year_a) & (transitions["year_b"] == year_b)].copy()
        m["kind"] = m.apply(
            lambda r: "SELF" if r["from_ck"] == r["to_ck"]
            else ("LINEAGE" if (r["from_ck"], r["to_ck"], year_a, year_b) in lineage_ids else "TRANSFER"),
            axis=1,
        )

        by_source = m.groupby(["from_ck", "kind"])["transition_area_km2"].sum().unstack(fill_value=0.0)
        for col in ("SELF", "LINEAGE", "TRANSFER"):
            if col not in by_source.columns:
                by_source[col] = 0.0

        res_by_obs = residual.set_index("geom_obs_id")
        for _, row in a.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck is None:
                continue
            retained = float(by_source.loc[ck, "SELF"]) if ck in by_source.index else 0.0
            relinquished_lineage = float(by_source.loc[ck, "LINEAGE"]) if ck in by_source.index else 0.0
            transferred_out = float(by_source.loc[ck, "TRANSFER"]) if ck in by_source.index else 0.0
            lost = float(res_by_obs.loc[row["geom_obs_id"], "lost_to_unmapped_km2"]) if row["geom_obs_id"] in res_by_obs.index else 0.0
            sliver = float(res_by_obs.loc[row["geom_obs_id"], "sliver_area_km2"]) if row["geom_obs_id"] in res_by_obs.index else 0.0
            area_before = float(row["area_km2"])
            closure = retained + relinquished_lineage + transferred_out + lost + sliver
            closure_error = abs(closure - area_before)

            rows.append({
                "record_kind": "OUTBOUND", "canonical_key": ck, "district_name": row["district_name_std"],
                "state": row["state_name_std"], "year_a": year_a, "year_b": year_b,
                "area_before_km2": round(area_before, 4),
                "area_retained_km2": round(retained, 4),
                "area_relinquished_lineage_km2": round(relinquished_lineage, 4),
                "area_transferred_out_km2": round(transferred_out, 4),
                "area_lost_to_unmapped_km2": round(lost, 4),
                "area_sliver_km2": round(sliver, 4),
                "closure_error_km2": round(closure_error, 6),
                "area_received_transfer_km2": None,
            })

        # inbound: area received into continuing (non-new) CKs this window
        transfer_win = transfer[(transfer["year_a"] == year_a) & (transfer["year_b"] == year_b)]
        received_map = transfer_win.groupby("to_ck")["transferred_area_km2"].sum().to_dict()

        b = pd.read_parquet(lib.SILVER_GEOM_DIR / f"{source_b}_{year_b}.geoparquet").drop(columns="geometry")
        for _, row in b.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck is None:
                continue
            established = registry.loc[registry["canonical_key"] == ck, "established_year"].iloc[0]
            if established == year_b:
                continue  # newly formed this window -- area accounted for on the lineage side, not as a "receipt"
            recv = float(received_map.get(ck, 0.0))
            if recv > 0:
                rows.append({
                    "record_kind": "INBOUND", "canonical_key": ck, "district_name": row["district_name_std"],
                    "state": row["state_name_std"], "year_a": year_a, "year_b": year_b,
                    "area_before_km2": None, "area_retained_km2": None,
                    "area_relinquished_lineage_km2": None, "area_transferred_out_km2": None,
                    "area_lost_to_unmapped_km2": None, "area_sliver_km2": None,
                    "closure_error_km2": None, "area_received_transfer_km2": round(recv, 4),
                })

    ledger = pd.DataFrame(rows)
    ledger.to_parquet(lib.PRODUCTS_DIR / "district_area_ledger.parquet")
    ledger.to_csv(lib.PRODUCTS_DIR / "district_area_ledger.csv", index=False)

    outbound = ledger[ledger["record_kind"] == "OUTBOUND"]
    print(f"Ledger rows (outbound accounting): {len(outbound)}")
    print(f"Mean closure error: {outbound['closure_error_km2'].mean():.4f} km2")
    print(f"Max closure error: {outbound['closure_error_km2'].max():.4f} km2")
    bad = outbound[outbound["closure_error_km2"] > np.maximum(
        lib.CLOSURE_TOLERANCE_KM2, outbound["area_before_km2"] * lib.CLOSURE_TOLERANCE_PCT / 100.0
    )]
    print(f"Closure violations beyond tolerance: {len(bad)}")
    if len(bad):
        print(bad[["district_name", "state", "year_a", "year_b", "closure_error_km2"]].head(10).to_string())

    print(f"\nWrote {lib.PRODUCTS_DIR / 'district_area_ledger.csv'} ({len(ledger)} rows)")


if __name__ == "__main__":
    main()
