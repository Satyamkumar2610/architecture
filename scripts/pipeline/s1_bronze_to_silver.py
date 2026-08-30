"""
Stage 1 — Bronze to Silver.

Reads each source GPKG, standardizes to a common schema, reprojects to
EPSG:4326 where needed (SOI only), repairs invalid geometry, computes
geodesic area with the one method used everywhere (lib.geodesic_area_km2),
and writes one Parquet file per vintage to data/silver/geometry/.

No source metadata is overwritten (Bronze is untouched). This stage only
reads the GPKGs under data/bronze/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from shapely import make_valid, wkb as shapely_wkb
from shapely.geometry.base import BaseGeometry

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

CONFIG_PATH = lib.PROJECT_ROOT / "config" / "sources.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


def repair(geom: BaseGeometry) -> tuple[BaseGeometry | None, bool]:
    if geom is None or geom.is_empty:
        return None, False
    if geom.is_valid:
        return geom, False
    fixed = make_valid(geom)
    return fixed, True


def to_multipolygon(geom: BaseGeometry):
    from shapely.geometry import MultiPolygon, Polygon, GeometryCollection

    if geom is None:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None
        flat = []
        for g in polys:
            if isinstance(g, MultiPolygon):
                flat.extend(g.geoms)
            else:
                flat.append(g)
        return MultiPolygon(flat)
    return None


def ingest_stanford(year: str, cfg: dict) -> gpd.GeoDataFrame:
    path = lib.PROJECT_ROOT / cfg["path"]
    layer = cfg["primary_layer"]
    gdf = gpd.read_file(path, layer=layer)
    pk_field = cfg["source_pk"]
    name_field = cfg["name_field"]

    state_series: pd.Series
    if cfg.get("state_field"):
        state_series = gdf[cfg["state_field"]]
    elif cfg.get("state_code_field"):
        codes = gdf[cfg["state_code_field"]].astype(str).str.zfill(2)
        state_series = codes.map(lib.PC11_STATE_CODES)
        unmapped = codes[~codes.isin(lib.PC11_STATE_CODES)]
        if len(unmapped):
            print(f"  WARNING [{year}]: {len(unmapped)} rows with unmapped state code")
    else:
        raise ValueError(f"No state field configured for {year}")

    out = gpd.GeoDataFrame({
        "source_pk": gdf[pk_field].astype(str),
        "district_name_original": gdf[name_field].astype(str),
        "state_name_original": state_series.astype(str),
        "geometry": gdf.geometry,
    }, geometry="geometry", crs=gdf.crs)
    return out


def ingest_soi(year: str, cfg: dict) -> gpd.GeoDataFrame:
    path = lib.PROJECT_ROOT / cfg["path"]
    layer = cfg["primary_layer"]
    gdf = gpd.read_file(path, layer=layer)
    out = gpd.GeoDataFrame({
        "source_pk": gdf[cfg["source_pk"]].astype(str),
        "district_name_original": gdf[cfg["name_field"]].astype(str),
        "state_name_original": gdf[cfg["state_field"]].astype(str),
        "geometry": gdf.geometry,
    }, geometry="geometry", crs=gdf.crs)
    # Bronze preserves original CRS; reprojection to EPSG:4326 is a Silver
    # operation per config/sources.yaml notes.
    out = out.to_crs(epsg=4326)
    return out


def process_vintage(source: str, year: str, cfg: dict) -> pd.DataFrame:
    print(f"[{source} {year}] ingesting...")
    if source == "stanford":
        gdf = ingest_stanford(year, cfg)
    else:
        gdf = ingest_soi(year, cfg)

    n_raw = len(gdf)

    # Exclude unnamed / sentinel-named features. Verified by inspection (see
    # docs/architecture/lineage_area_redesign.md discussion / pipeline run
    # log): these are non-administrative placeholder polygons — a
    # ~80,520 km2 feature recurs in J&K across 1961/1971 (literal name
    # "DATA NOT AVAILABLE") and 1981/1991/2001 (blank name), consistently
    # matching the unadministered/claimed territory shown for cartographic
    # completeness but never subdivided into real districts (a ~108,672 km2
    # variant in 2011); a ~270 km2 unnamed Gujarat feature in 1991/2001
    # matches the disputed Rann of Kutch strip; two small stray slivers
    # appear in 1971 Andaman. A district identity requires a name (no name
    # -> no CK). These are logged, not silently dropped, per architecture
    # Principle 10.
    name_raw = gdf["district_name_original"].astype(str).str.strip()
    is_sentinel = name_raw.str.lower().str.match(
        r"^(nan||data not available|not available|n/?a|unknown|no data|unnamed|-+)$"
    )
    is_unnamed = name_raw.eq("") | is_sentinel | gdf["district_name_original"].isna()
    excluded = gdf.loc[is_unnamed].copy()
    gdf = gdf.loc[~is_unnamed].copy()

    excluded_records = []
    for _, row in excluded.iterrows():
        area = lib.geodesic_area_km2(row.geometry) if row.geometry is not None else 0.0
        excluded_records.append({
            "source_dataset": source, "source_year": int(year),
            "source_pk": row["source_pk"], "state_name_original": row["state_name_original"],
            "area_km2": round(area, 2), "reason": "UNNAMED_NON_ADMINISTRATIVE_FEATURE",
        })
    if excluded_records:
        print(f"  EXCLUDED {len(excluded_records)} unnamed non-administrative feature(s): "
              f"{[(r['state_name_original'], r['area_km2']) for r in excluded_records]}")

    records = []
    n_repaired = 0
    n_empty = 0
    n_multipart = 0
    for _, row in gdf.iterrows():
        geom, was_repaired = repair(row.geometry)
        if geom is None or geom.is_empty:
            n_empty += 1
            continue
        area_before = lib.geodesic_area_km2(row.geometry) if row.geometry is not None else 0.0
        mp = to_multipolygon(geom)
        if mp is None or mp.is_empty:
            n_empty += 1
            continue
        area_after = lib.geodesic_area_km2(mp)
        repair_delta_pct = (
            100.0 * abs(area_after - area_before) / area_before if area_before > 0 else 0.0
        )
        if len(mp.geoms) > 1:
            n_multipart += 1
        if was_repaired:
            n_repaired += 1

        records.append({
            "source_dataset": source,
            "source_year": int(year),
            "source_pk": row["source_pk"],
            "district_name_original": row["district_name_original"],
            "district_name_std": lib.display_name(row["district_name_original"]),
            "district_name_norm": lib.normalize_name(row["district_name_original"]),
            "state_name_original": row["state_name_original"],
            "state_name_std": lib.canonical_state(row["state_name_original"]),
            "state_name_norm": lib.normalize_name(lib.canonical_state(row["state_name_original"])),
            "area_km2": area_after,
            "was_repaired": was_repaired,
            "repair_area_delta_pct": repair_delta_pct,
            "is_multipart": len(mp.geoms) > 1,
            "n_parts": len(mp.geoms),
            "geometry": mp,
        })

    out = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    out["geom_obs_id"] = [
        lib.stable_id(source, year, r["source_pk"]) for r in records
    ]

    # Exact-duplicate WKB check within this vintage (data audit requirement)
    wkb_hashes = out.geometry.apply(lambda g: hash(shapely_wkb.dumps(g)))
    dup_wkb = wkb_hashes.duplicated().sum()

    # Duplicate source_pk check
    dup_pk = out["source_pk"].duplicated().sum()

    print(
        f"  raw={n_raw} usable={len(out)} empty_dropped={n_empty} "
        f"repaired={n_repaired} multipart={n_multipart} "
        f"dup_wkb={dup_wkb} dup_source_pk={dup_pk}"
    )
    if dup_pk:
        print(f"  WARNING: {dup_pk} duplicate source_pk values in {source} {year} — not unique in source")

    out_path = lib.SILVER_GEOM_DIR / f"{source}_{year}.geoparquet"
    out.to_parquet(out_path)
    print(f"  wrote {out_path}")

    return pd.DataFrame({
        "source": [source], "year": [int(year)], "raw": [n_raw], "usable": [len(out)],
        "excluded_unnamed": [len(excluded_records)], "empty_dropped": [n_empty],
        "repaired": [n_repaired], "multipart": [n_multipart],
        "dup_wkb": [int(dup_wkb)], "dup_source_pk": [int(dup_pk)],
    }), excluded_records


def main() -> None:
    cfg = load_config()
    summaries = []
    all_excluded = []
    for year, dcfg in cfg["sources"]["stanford"]["datasets"].items():
        s, excl = process_vintage("stanford", year, dcfg)
        summaries.append(s)
        all_excluded.extend(excl)
    for year, dcfg in cfg["sources"]["soi"]["datasets"].items():
        s, excl = process_vintage("soi", year, dcfg)
        summaries.append(s)
        all_excluded.extend(excl)

    summary = pd.concat(summaries, ignore_index=True)
    summary_path = lib.OUTPUT_DIR / "s1_bronze_to_silver_summary.csv"
    summary.to_csv(summary_path, index=False)
    print(f"\nSummary written to {summary_path}")
    print(summary.to_string(index=False))

    if all_excluded:
        excl_df = pd.DataFrame(all_excluded)
        excl_path = lib.OUTPUT_DIR / "s1_excluded_non_administrative.csv"
        excl_df.to_csv(excl_path, index=False)
        print(f"\nExcluded non-administrative features written to {excl_path}")
        print(excl_df.to_string(index=False))


if __name__ == "__main__":
    main()
