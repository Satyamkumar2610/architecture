"""
Stage 2 — Per-layer topology audit (V1, V2, V4).

Runs BEFORE any cross-vintage work, per the redesign doc: if a layer
self-overlaps materially, nothing computed from it downstream can be
trusted, and that must be known up front, not discovered later as a
mysterious raw-weight-sum-of-5.32 crosswalk defect.

V1 — self-overlap area within a layer: sum(area_i) - area(unary_union).
     Exact (no double-counting), unlike a naive pairwise sum.
V2 — total layer area trend across vintages (informational; no external
     national-boundary reference file exists in Bronze to compare against).
V4 — stored vs recomputed area: trivially satisfied here because this
     pipeline uses exactly one area method everywhere (lib.geodesic_area_km2);
     recorded as a smoke test so a future method change cannot silently drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def audit_layer(path: Path) -> dict:
    gdf = gpd.read_parquet(path)
    source, year = path.stem.rsplit("_", 1)
    sum_area = float(gdf["area_km2"].sum())

    union_geom = unary_union(gdf.geometry.values)
    union_area = lib.geodesic_area_km2(union_geom)

    overlap_area = max(0.0, sum_area - union_area)
    overlap_frac = overlap_area / sum_area if sum_area > 0 else 0.0

    # V4 smoke test: recompute area from stored geometry independently
    recomputed = gdf.geometry.apply(lib.geodesic_area_km2)
    method_delta_pct = (
        100.0 * (recomputed - gdf["area_km2"]).abs() / gdf["area_km2"].replace(0, pd.NA)
    )
    max_method_delta_pct = float(method_delta_pct.max(skipna=True) or 0.0)

    if overlap_frac >= lib.SELF_OVERLAP_ERROR_FRACTION:
        status = "ERROR_SELF_OVERLAP"
    elif overlap_frac >= lib.SELF_OVERLAP_WARN_FRACTION:
        status = "WARNING_SELF_OVERLAP"
    else:
        status = "PASS"

    return {
        "source": source, "year": int(year), "n_districts": len(gdf),
        "sum_polygon_area_km2": round(sum_area, 2),
        "union_area_km2": round(union_area, 2),
        "self_overlap_area_km2": round(overlap_area, 4),
        "self_overlap_fraction_pct": round(100 * overlap_frac, 6),
        "max_area_method_delta_pct": round(max_method_delta_pct, 8),
        "status": status,
    }


def main() -> None:
    rows = []
    for path in sorted(lib.SILVER_GEOM_DIR.glob("*.geoparquet")):
        print(f"auditing {path.name} ...")
        row = audit_layer(path)
        rows.append(row)
        print(f"  {row}")

    report = pd.DataFrame(rows).sort_values("year")
    out_csv = lib.OUTPUT_DIR / "s2_topology_audit.csv"
    report.to_csv(out_csv, index=False)

    n_error = (report["status"] == "ERROR_SELF_OVERLAP").sum()
    n_warn = (report["status"] == "WARNING_SELF_OVERLAP").sum()

    md = [
        "# Topology Audit (V1 / V2 / V4)\n",
        f"Layers audited: {len(report)}\n",
        f"ERROR (self-overlap >= {lib.SELF_OVERLAP_ERROR_FRACTION*100:.1f}%): {n_error}\n",
        f"WARNING (self-overlap >= {lib.SELF_OVERLAP_WARN_FRACTION*100:.2f}%): {n_warn}\n",
        "\n## Per-vintage results\n",
        report.to_markdown(index=False),
        "\n\n## Interpretation\n",
        "`self_overlap_area_km2` is exact: sum of individual polygon areas minus "
        "the area of their union, so overlaps of any multiplicity are counted once. "
        "A layer with material self-overlap cannot be used as a partition for the "
        "transition matrix in Stage 4 without repair — this is the gate the old "
        "pipeline never ran, which is why pairwise intersection weight sums reached "
        "5.32 and 24.25 (see docs/architecture/lineage_area_redesign.md section 2.2).\n"
        "`max_area_method_delta_pct` recomputes area independently from the stored "
        "geometry; nonzero drift here would mean two area methods are in play "
        "(see redesign doc section 2.8) — it is 0 by construction in this pipeline.\n",
    ]
    out_md = lib.OUTPUT_DIR / "s2_topology_audit.md"
    out_md.write_text("".join(md), encoding="utf-8")

    print(f"\nWrote {out_csv}")
    print(f"Wrote {out_md}")
    print(report.to_string(index=False))

    if n_error:
        print(f"\nERROR: {n_error} layer(s) exceed self-overlap tolerance. "
              f"Downstream matrix construction should not proceed for these vintages "
              f"without repair.")


if __name__ == "__main__":
    main()
