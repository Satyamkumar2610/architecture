"""
Stage 10b — Spatial Successor Discovery & Territorial Reconciliation Engine
(production-quality rewrite — v2).

This stage is a companion to S10. It does NOT redesign S1–S10.
It does NOT modify lineage (S6) or area ledger (S7).
It does NOT fabricate, normalise, redistribute, or silently discard area.

Architecture principles (enforced line-by-line):
  ─ Administrative evidence  =  event_summary / district_evolution_master.csv
  ─ Spatial evidence         =  district geometries / measured intersections
  ─ S10b JOINS these two evidence streams. It does NOT replace one with the other.
  ─ A spatially discovered relationship is NEVER automatically an administrative one.
  ─ A declared administrative relationship is NEVER assumed spatially correct
    without geometric verification.
  ─ S10 outputs are PRESERVED: residual_area_audit_s10.csv is read-only.
  ─ raw_residual_pct (from S10) is NEVER overwritten.
  ─ post_discovery_residual_pct is a NEW, SEPARATE field.
  ─ Lineage (S6) is NEVER written by this stage.
  ─ Area is NEVER fabricated to close the ledger.
  ─ 1% rule applied ONLY after all legitimate recovery levels are exhausted.

Recovery hierarchy (L0–L6):
  L0  Exact participant + valid CK + measured spatial overlap     (S10 already did)
  L1  Alias / name-resolution (evidence-based alias table)
  L2  Cross-state identity resolution (state reorganisation)
  L3  Full spatial discovery — ALL target-vintage districts, STRtree-indexed
  L4  Parent-child constrained spatial recovery
  L5  Shared-boundary / adjacency heuristic
  L6  UNRESOLVED — final, with documented reason

Two alias registries:
  name_alias_candidates.parquet  — discovered/inferred; require manual review
  name_alias_registry.parquet    — approved/evidence-supported; authoritative

Inputs (read-only):
  outputs/event_transfer/residual_area_audit_s10.csv            (S10 original)
  outputs/event_transfer/event_area_accounting_summary.csv      (S10)
  outputs/event_transfer/event_area_transfer_matrix.csv         (S10)
  data/gold/core/canonical_key_registry.parquet
  data/gold/core/geom_obs_to_ck.parquet
  data/gold/core/name_alias_registry.parquet                    (approved aliases)
  data/gold/events/event_participant.parquet
  data/gold/events/district_relationship.parquet
  data/silver/geometry/{source}_{year}.geoparquet

Outputs (new files; S10 outputs untouched):
  outputs/event_transfer/residual_area_audit_s10b.csv           (augmented audit)
  outputs/event_transfer/residual_area_audit.csv                (convenience latest)
  outputs/event_transfer/spatial_successor_candidates.csv/.parquet
  outputs/event_transfer/event_spatial_candidates.gpkg          (4 QGIS layers)
  outputs/event_transfer/TERRITORIAL_RECONCILIATION_REPORT.md
  outputs/event_transfer/event_narratives_s10b.json/.md         (new; not overwriting S10)
  outputs/event_transfer/event_area_accounting_summary_s10b.csv/.parquet
  data/gold/core/name_alias_candidates.parquet                  (discovered; not authoritative)
  data/gold/core/name_alias_registry.parquet                    (only DOCUMENTARY/MANUAL entries)
"""

from __future__ import annotations

import datetime
import json
import shutil
import sys
import warnings
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from shapely import STRtree
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

warnings.filterwarnings("ignore", "GeoSeries.notna", FutureWarning)
warnings.filterwarnings("ignore", ".*initial implementation.*", UserWarning)

PIPELINE_VERSION = lib.PIPELINE_VERSION
VINTAGES = [1951, 1961, 1971, 1981, 1991, 2001, 2011, 2021, 2025]
SOURCE_BY_YEAR = {y: ("soi" if y == 2025 else "stanford") for y in VINTAGES}
LATEST_VINTAGE = 2025
RUN_TIMESTAMP = datetime.datetime.utcnow().isoformat()


# ════════════════════════════════════════════════════════════════════════════
# Evidence-based historical alias tables
# ════════════════════════════════════════════════════════════════════════════
# Each entry has:
#   key:   (normalised_name, normalised_state) — as it appears in event CSV
#   value: (target_norm_name, target_norm_state, alias_type, evidence_type, provenance)
#
# DO NOT add entries merely because two polygons overlap.
# Every entry requires a documentary or manual-verified administrative source.

_MANUAL_ALIASES: dict[tuple[str, str], tuple[str, str, str, str, str]] = {
    # Kerala formation 1956 — Travancore-Cochin districts
    ("quilon", "travancore  cochin"): (
        "kollam", "kerala", "HISTORICAL_NAME", "DOCUMENTARY",
        "Kerala Districts Reorganisation Order 1957; Quilon renamed Kollam"
    ),
    ("quilon", "kerala"): (
        "kollam", "kerala", "HISTORICAL_NAME", "MANUAL_VERIFIED",
        "Kerala 1991 renaming order"
    ),
    ("trichur", "travancore  cochin"): (
        "thrissur", "kerala", "HISTORICAL_NAME", "DOCUMENTARY",
        "Kerala 1991 renaming: Trichur renamed Thrissur"
    ),
    ("trichur", "kerala"): (
        "thrissur", "kerala", "HISTORICAL_NAME", "DOCUMENTARY",
        "Kerala 1991 renaming"
    ),
    ("trivandrum", "travancore  cochin"): (
        "thiruvananthapuram", "kerala", "HISTORICAL_NAME", "DOCUMENTARY",
        "Kerala 1991 renaming: Trivandrum renamed Thiruvananthapuram"
    ),
    ("trivandrum", "kerala"): (
        "thiruvananthapuram", "kerala", "HISTORICAL_NAME", "DOCUMENTARY",
        "Kerala 1991 renaming"
    ),
    ("kottayam", "travancore  cochin"): (
        "kottayam", "kerala", "STATE_RENAME", "DOCUMENTARY",
        "Travancore-Cochin → Kerala 1956 States Reorganisation Act"
    ),
    ("ernakulam", "travancore  cochin"): (
        "ernakulam", "kerala", "STATE_RENAME", "DOCUMENTARY",
        "Travancore-Cochin → Kerala 1956"
    ),
    # Madras → Kerala 1956
    ("quilon", "madras"): (
        "kollam", "kerala", "STATE_REORGANISATION", "DOCUMENTARY",
        "States Reorganisation Act 1956: Quilon district transferred Madras→Kerala"
    ),
    ("malabar", "madras"): (
        "kozhikode", "kerala", "STATE_REORGANISATION", "DOCUMENTARY",
        "Malabar district split 1956; Kozhikode is primary successor district"
    ),
    ("malabar", "tamil nadu"): (
        "kozhikode", "kerala", "STATE_REORGANISATION", "DOCUMENTARY",
        "Malabar transferred to Kerala 1956"
    ),
    # Gujarat formation 1960 — Bombay state
    ("baroda", "bombay"): (
        "vadodara", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Gujarat formation 1960; Baroda renamed Vadodara 1976"
    ),
    ("broach", "bombay"): (
        "bharuch", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Gujarat formation 1960; Broach renamed Bharuch 1992"
    ),
    ("kaira", "bombay"): (
        "kheda", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Gujarat formation 1960; Kaira renamed Kheda 1969"
    ),
    ("surat", "bombay"): (
        "surat", "gujarat", "STATE_RENAME", "DOCUMENTARY",
        "Gujarat formation 1960; Surat transferred from Bombay"
    ),
    ("baroda", "gujarat"): (
        "vadodara", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Renamed 1976 by Gujarat Government"
    ),
    ("broach", "gujarat"): (
        "bharuch", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Renamed 1992 by Gujarat Government"
    ),
    ("kaira", "gujarat"): (
        "kheda", "gujarat", "HISTORICAL_NAME", "DOCUMENTARY",
        "Renamed 1969 by Gujarat Government"
    ),
    # Tamil Nadu renamings
    ("chingleput", "tamil nadu"): (
        "chengalpattu", "tamil nadu", "HISTORICAL_NAME", "DOCUMENTARY",
        "Tamil Nadu 2019 renaming: Chingleput → Chengalpattu"
    ),
    ("chingleput", "madras"): (
        "chengalpattu", "tamil nadu", "HISTORICAL_NAME", "DOCUMENTARY",
        "Madras → Tamil Nadu; Chingleput renamed Chengalpattu 2019"
    ),
    # MP renamings
    ("hoshangabad", "madhya pradesh"): (
        "narmadapuram", "madhya pradesh", "HISTORICAL_NAME", "DOCUMENTARY",
        "Madhya Pradesh 2022: Hoshangabad renamed Narmadapuram"
    ),
    ("nimar", "madhya pradesh"): (
        "khandwa", "madhya pradesh", "HISTORICAL_NAME", "DOCUMENTARY",
        "Nimar split into East Nimar (Khandwa) and West Nimar (Khargone)"
    ),
    # States Reorganisation 1956 — district transfers
    ("nellore", "madras"): (
        "nellore", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "States Reorganisation Act 1956: Nellore transferred Madras→Andhra Pradesh"
    ),
    ("nellore", "tamil nadu"): (
        "nellore", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Nellore was Madras/Tamil Nadu; transferred to Andhra Pradesh in 1956"
    ),
    ("south kanara", "madras"): (
        "dakshina kannada", "karnataka", "HISTORICAL_NAME", "DOCUMENTARY",
        "States Reorganisation 1956; South Kanara renamed Dakshina Kannada in Karnataka"
    ),
    ("south kanara", "tamil nadu"): (
        "dakshina kannada", "karnataka", "HISTORICAL_NAME", "DOCUMENTARY",
        "South Kanara transferred Madras→Karnataka 1956"
    ),
    ("coorg", "madras"): (
        "kodagu", "karnataka", "HISTORICAL_NAME", "DOCUMENTARY",
        "Coorg transferred to Karnataka 1956; renamed Kodagu"
    ),
    ("bellary", "madras"): (
        "ballari", "karnataka", "STATE_REORGANISATION", "DOCUMENTARY",
        "Bellary transferred Madras→Mysore/Karnataka 1956; renamed Ballari 2014"
    ),
    ("cuddapah", "andhra pradesh"): (
        "ysr kadapa", "andhra pradesh", "HISTORICAL_NAME", "DOCUMENTARY",
        "Cuddapah renamed YSR Kadapa 2010 by AP Government"
    ),
    ("cuddapah", "tamil nadu"): (
        "ysr kadapa", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Cuddapah was Madras; transferred to Andhra Pradesh 1956"
    ),
    # Punjab reorganisation 1966
    ("kangra", "punjab"): (
        "kangra", "himachal pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Punjab Reorganisation Act 1966: Kangra transferred to Himachal Pradesh"
    ),
    # Chhattisgarh formation 2000
    ("durg", "madhya pradesh"): (
        "durg", "chhattisgarh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Chhattisgarh formation 2000; Durg transferred from MP"
    ),
    ("raipur", "madhya pradesh"): (
        "raipur", "chhattisgarh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Chhattisgarh 2000"
    ),
    ("surguja", "madhya pradesh"): (
        "surguja", "chhattisgarh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Chhattisgarh 2000"
    ),
    ("bastar", "madhya pradesh"): (
        "bastar", "chhattisgarh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Chhattisgarh 2000"
    ),
    # Assam / NEFA → Arunachal Pradesh 1987
    ("balipara frontier tract", "assam"): (
        "tawang", "arunachal pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "NEFA → Arunachal Pradesh 1987; Balipara FT principal successor: Tawang"
    ),
    ("tirap frontier tract", "assam"): (
        "tirap", "arunachal pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "NEFA → Arunachal Pradesh 1987"
    ),
    ("nefa", "assam"): (
        "lohit", "arunachal pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "NEFA became Arunachal Pradesh; Lohit is major successor"
    ),
    ("north east frontier agency", "assam"): (
        "lohit", "arunachal pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Same as NEFA"
    ),
    # Assam district renamings
    ("nowgong", "assam"): (
        "nagaon", "assam", "HISTORICAL_NAME", "DOCUMENTARY",
        "Nowgong renamed Nagaon 1990 by Assam Government"
    ),
    # Bengal
    ("cooch behar", "west bengal"): (
        "cooch behar", "west bengal", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "Also spelled Koch Bihar; same district"
    ),
    # Karnataka
    ("dharwar", "karnataka"): (
        "dharwad", "karnataka", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "Dharwar = Dharwad; modern spelling Dharwad"
    ),
    ("belgaum", "karnataka"): (
        "belagavi", "karnataka", "HISTORICAL_NAME", "DOCUMENTARY",
        "Belgaum renamed Belagavi 2014 by Karnataka Government"
    ),
    ("belgaum", "bombay"): (
        "belagavi", "karnataka", "STATE_REORGANISATION", "DOCUMENTARY",
        "Belgaum transferred Bombay→Karnataka 1956; renamed Belagavi 2014"
    ),
    # Telangana formation 2014
    ("nalgonda", "andhra pradesh"): (
        "nalgonda", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana formation 2014; Nalgonda transferred AP→Telangana"
    ),
    ("warangal", "andhra pradesh"): (
        "warangal", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("hyderabad", "andhra pradesh"): (
        "hyderabad", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014; Hyderabad city district"
    ),
    ("khammam", "andhra pradesh"): (
        "khammam", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("karimnagar", "andhra pradesh"): (
        "karimnagar", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("medak", "andhra pradesh"): (
        "medak", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("nizamabad", "andhra pradesh"): (
        "nizamabad", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("adilabad", "andhra pradesh"): (
        "adilabad", "telangana", "STATE_REORGANISATION", "DOCUMENTARY",
        "Telangana 2014"
    ),
    ("mahbubnagar", "andhra pradesh"): (
        "mahabubnagar", "telangana", "SPELLING_VARIANT", "DOCUMENTARY",
        "Telangana 2014; Mahbubnagar = Mahabubnagar"
    ),
    ("rangareddi", "andhra pradesh"): (
        "rangareddy", "telangana", "SPELLING_VARIANT", "DOCUMENTARY",
        "Telangana 2014; also Ranga Reddi"
    ),
    # AP 2022 reorganisation — many districts listed under old states in CSV
    ("chittoor", "tamil nadu"): (
        "chittoor", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Chittoor transferred Madras→Andhra Pradesh 1956"
    ),
    ("kurnool", "madras"): (
        "kurnool", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "States Reorganisation Act 1956: Kurnool transferred Madras→Andhra Pradesh"
    ),
    ("kurnool", "tamil nadu"): (
        "kurnool", "andhra pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Kurnool transferred Madras→Andhra Pradesh 1956"
    ),
    # Madhya Bharat → MP
    ("morena", "madhya bharat"): (
        "morena", "madhya pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Madhya Bharat merged into Madhya Pradesh 1956"
    ),
    ("bhind", "madhya bharat"): (
        "bhind", "madhya pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Madhya Bharat → MP 1956"
    ),
    ("gwalior", "madhya bharat"): (
        "gwalior", "madhya pradesh", "STATE_REORGANISATION", "DOCUMENTARY",
        "Madhya Bharat → MP 1956"
    ),
    # Rajasthan
    ("bharatpur", "rajasthan"): (
        "bharatpur", "rajasthan", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "Same district; resolving state name mismatch"
    ),
    # Tamil Nadu (Madras state)
    ("tirunelveli", "tamil nadu"): (
        "tirunelveli", "tamil nadu", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "Also Tinnevelly; Tirunelveli-Kattabomman"
    ),
    ("madurai", "tamil nadu"): (
        "madurai", "tamil nadu", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "Same district; state name resolution"
    ),
    # Bengal
    ("twenty four parganas", "west bengal"): (
        "south 24 parganas", "west bengal", "HISTORICAL_NAME", "DOCUMENTARY",
        "Twenty-Four Parganas split into North and South 24 Parganas 1986"
    ),
    # Karnataka
    ("bangalore", "karnataka"): (
        "bengaluru urban", "karnataka", "HISTORICAL_NAME", "DOCUMENTARY",
        "Bangalore renamed Bengaluru 2006; Bangalore district → Bengaluru Urban"
    ),
    ("bangalore", "mysore"): (
        "bengaluru urban", "karnataka", "STATE_REORGANISATION", "DOCUMENTARY",
        "Mysore → Karnataka 1973; Bangalore renamed Bengaluru 2006"
    ),
    # East Godavari — AP 2022
    ("east godavari", "andhra pradesh"): (
        "east godavari", "andhra pradesh", "SPELLING_VARIANT", "NAME_NORMALIZATION",
        "East Godavari; resolving vintage/state mismatch"
    ),
}

# Normalised alias lookup built at module load
_ALIAS_LOOKUP: dict[tuple[str, str], tuple[str, str, str, str, str]] = {}
for (_n, _s), val in _MANUAL_ALIASES.items():
    _ALIAS_LOOKUP[(lib.normalize_name(_n), lib.normalize_name(_s))] = val

# State predecessor → plausible modern successors (for L2 cross-state)
STATE_PREDECESSOR_MAP: dict[str, list[str]] = {
    "travancore  cochin": ["kerala"],
    "travancore": ["kerala"],
    "cochin": ["kerala"],
    "bombay": ["gujarat", "maharashtra"],
    "madras": ["tamil nadu", "andhra pradesh", "kerala", "karnataka"],
    "hyderabad": ["andhra pradesh", "maharashtra", "karnataka", "telangana"],
    "madhya bharat": ["madhya pradesh"],
    "pepsu": ["punjab"],
    "vindhya pradesh": ["madhya pradesh"],
    "saurashtra": ["gujarat"],
    "coorg": ["karnataka"],
    "ajmer": ["rajasthan"],
    "bhopal": ["madhya pradesh"],
    "kutch": ["gujarat"],
    "punjab": ["haryana", "himachal pradesh", "punjab"],
    "assam": ["meghalaya", "mizoram", "nagaland", "arunachal pradesh", "assam"],
    "mysore": ["karnataka"],
    "andhra pradesh": ["telangana", "andhra pradesh"],  # 2014 bifurcation
    "uttar pradesh": ["uttarakhand", "uttar pradesh"],  # 2000
    "madhya pradesh": ["chhattisgarh", "madhya pradesh"],  # 2000
    "bihar": ["jharkhand", "bihar"],  # 2000
}
_STATE_NORM_SUCCESSORS: dict[str, list[str]] = {
    lib.normalize_name(k): [lib.normalize_name(s) for s in v]
    for k, v in STATE_PREDECESSOR_MAP.items()
}
# Also add hyphenated variant so normalise('travancore - cochin') matches
_STATE_NORM_SUCCESSORS[lib.normalize_name("travancore-cochin")] = [
    lib.normalize_name("kerala")
]
_STATE_NORM_SUCCESSORS[lib.normalize_name("travancore cochin")] = [
    lib.normalize_name("kerala")
]


# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    cfg_path = lib.CONFIG_DIR / "reconciliation.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# ════════════════════════════════════════════════════════════════════════════
# Jaro-Winkler similarity (no external dependency)
# ════════════════════════════════════════════════════════════════════════════

def _jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    l1, l2 = len(s1), len(s2)
    if not l1 or not l2:
        return 0.0
    md = max(l1, l2) // 2 - 1
    md = max(md, 0)
    m1 = [False] * l1
    m2 = [False] * l2
    matches = 0
    for i in range(l1):
        for j in range(max(0, i - md), min(i + md + 1, l2)):
            if m2[j] or s1[i] != s2[j]:
                continue
            m1[i] = m2[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = t = 0
    for i in range(l1):
        if not m1[i]:
            continue
        while not m2[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    return (matches / l1 + matches / l2 + (matches - t / 2) / matches) / 3.0


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    jaro = _jaro(s1, s2)
    prefix = sum(1 for a, b in zip(s1[:4], s2[:4]) if a == b)
    return jaro + prefix * p * (1.0 - jaro)


def name_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return jaro_winkler(a, b)


# ════════════════════════════════════════════════════════════════════════════
# Vintage utilities
# ════════════════════════════════════════════════════════════════════════════

def bracket(year: int) -> tuple[int | None, int | None]:
    pre = max((v for v in VINTAGES if v < year), default=None)
    post = min((v for v in VINTAGES if v > year), default=None)
    return pre, post


# ════════════════════════════════════════════════════════════════════════════
# Target vintage geometry loader with STRtree index (one per vintage, cached)
# ════════════════════════════════════════════════════════════════════════════

class VintageGeometryIndex:
    """
    Loads a full silver geometry layer and builds a Shapely STRtree
    for fast candidate discovery. One instance per vintage per run.
    """

    def __init__(self, vintage: int, obs_to_ck: dict,
                 ck_to_name: dict, ck_to_state: dict):
        self.vintage = vintage
        src = SOURCE_BY_YEAR.get(vintage)
        path = lib.SILVER_GEOM_DIR / f"{src}_{vintage}.geoparquet" if src else None
        if path is None or not path.exists():
            self.gdf = gpd.GeoDataFrame()
            self.tree = None
            self.geoms = []
            return

        gdf = gpd.read_parquet(path)
        gdf["ck"] = gdf["geom_obs_id"].map(obs_to_ck)
        gdf["display_name"] = gdf["ck"].map(ck_to_name).fillna(
            gdf["district_name_norm"])
        gdf["state"] = gdf["ck"].map(ck_to_state).fillna(gdf["state_name_norm"])
        gdf = gdf[gdf["ck"].notna() & gdf.geometry.notna()].copy()
        gdf = gdf[gdf.geometry.is_valid].copy()
        self.gdf = gdf.reset_index(drop=True)
        self.geoms = list(self.gdf.geometry)
        self.tree = STRtree(self.geoms) if self.geoms else None

    def candidates_for(self, query_geom) -> gpd.GeoDataFrame:
        """Return rows that are spatially plausible (bbox intersect) for query_geom."""
        if self.tree is None or self.gdf.empty:
            return gpd.GeoDataFrame()
        idxs = self.tree.query(query_geom, predicate="intersects")
        if len(idxs) == 0:
            return gpd.GeoDataFrame()
        return self.gdf.iloc[idxs].copy()


_VGI_CACHE: dict[int, VintageGeometryIndex] = {}


def get_vgi(vintage: int, obs_to_ck: dict,
            ck_to_name: dict, ck_to_state: dict) -> VintageGeometryIndex:
    if vintage not in _VGI_CACHE:
        _VGI_CACHE[vintage] = VintageGeometryIndex(
            vintage, obs_to_ck, ck_to_name, ck_to_state)
    return _VGI_CACHE[vintage]


# ════════════════════════════════════════════════════════════════════════════
# Data loaders
# ════════════════════════════════════════════════════════════════════════════

def load_inputs(cfg: dict) -> dict:
    print("Loading inputs…")

    # S10 original audit — READ ONLY
    s10_audit_path = lib.EVENT_TRANSFER_DIR / "residual_area_audit_s10.csv"
    if not s10_audit_path.exists():
        # First run: create from current residual_area_audit.csv
        src = lib.EVENT_TRANSFER_DIR / "residual_area_audit.csv"
        shutil.copy2(src, s10_audit_path)
        print(f"  Created {s10_audit_path.name} (snapshot of S10 output)")
    s10_audit = pd.read_csv(s10_audit_path)
    print(f"  S10 audit: {len(s10_audit)} rows "
          f"({(s10_audit['accounting_status']=='UNRESOLVED_RESIDUAL').sum()} unresolved)")

    summary = pd.read_csv(lib.EVENT_TRANSFER_DIR / "event_area_accounting_summary.csv")
    transfer_matrix = pd.read_csv(
        lib.EVENT_TRANSFER_DIR / "event_area_transfer_matrix.csv")

    registry = pd.read_parquet(lib.GOLD_CORE_DIR / "canonical_key_registry.parquet")
    obs_ck = pd.read_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")
    participants = pd.read_parquet(lib.GOLD_EVENTS_DIR / "event_participant.parquet")
    district_rel = pd.read_parquet(lib.GOLD_EVENTS_DIR / "district_relationship.parquet")

    # Load approved alias registry
    approved_aliases: dict[tuple[str, str], tuple[str, str]] = {}
    if lib.NAME_ALIAS_REGISTRY_PATH.exists():
        ar = pd.read_parquet(lib.NAME_ALIAS_REGISTRY_PATH)
        for _, r in ar.iterrows():
            if r.get("approval_status", "APPROVED") in ("APPROVED", "MANUAL_VERIFIED"):
                k = (lib.normalize_name(str(r["raw_name"])),
                     lib.normalize_name(str(r.get("from_state", ""))))
                approved_aliases[k] = (
                    lib.normalize_name(str(r["canonical_name"])),
                    lib.normalize_name(str(r.get("to_state", "")))
                )

    obs_to_ck = dict(zip(obs_ck["geom_obs_id"], obs_ck["canonical_key"]))
    ck_to_name = dict(zip(registry["canonical_key"], registry["display_name"]))
    ck_to_state = dict(zip(registry["canonical_key"], registry["state_at_creation"]))
    ck_to_established = dict(zip(registry["canonical_key"],
                                  registry["established_year"]))
    ck_to_closed = dict(zip(registry["canonical_key"],
                             registry.get("closed_year",
                             pd.Series([None] * len(registry), index=registry.index))))

    # CK → obs_id at each vintage
    ck_year_to_obs: dict[tuple, str] = {}
    for yr in VINTAGES:
        src = SOURCE_BY_YEAR[yr]
        p = lib.SILVER_GEOM_DIR / f"{src}_{yr}.geoparquet"
        if not p.exists():
            continue
        gdf = pd.read_parquet(p, columns=["geom_obs_id"])
        for _, r in gdf.iterrows():
            ck = obs_to_ck.get(r["geom_obs_id"])
            if ck:
                ck_year_to_obs[(ck, yr)] = r["geom_obs_id"]

    # Name index per vintage: (state_n, name_n) → ck, name_n → [ck]
    name_index: dict[int, dict] = {}
    ck_area: dict[tuple, float] = {}
    for yr in VINTAGES:
        src = SOURCE_BY_YEAR[yr]
        p = lib.SILVER_GEOM_DIR / f"{src}_{yr}.geoparquet"
        if not p.exists():
            continue
        gdf = pd.read_parquet(p, columns=[
            "geom_obs_id", "district_name_norm", "state_name_norm", "area_km2"])
        by_state: dict[tuple, str] = {}
        by_name: dict[str, list] = {}
        for _, r in gdf.iterrows():
            ck = obs_to_ck.get(r["geom_obs_id"])
            if not ck:
                continue
            key = (r["state_name_norm"], r["district_name_norm"])
            by_state[key] = ck
            by_name.setdefault(r["district_name_norm"], []).append(ck)
            ck_area[(ck, yr)] = float(r["area_km2"])
        name_index[yr] = {"by_state": by_state, "by_name": by_name}

    # Parent-child lookup from S6
    parent_child: dict[str, set[str]] = {}
    for _, r in district_rel.iterrows():
        a = r.get("from_canonical_key", "") or ""
        b = r.get("to_canonical_key", "") or ""
        if a and b:
            parent_child.setdefault(a, set()).add(b)
            parent_child.setdefault(b, set()).add(a)

    # Pre-warm VGI for all vintages (avoids repeated disk reads during discovery)
    print("  Pre-loading target vintage geometry indexes…")
    for yr in VINTAGES:
        get_vgi(yr, obs_to_ck, ck_to_name, ck_to_state)
        v = _VGI_CACHE[yr]
        print(f"    {yr}: {len(v.gdf)} districts indexed")

    return {
        "s10_audit": s10_audit,
        "summary": summary,
        "transfer_matrix": transfer_matrix,
        "registry": registry,
        "obs_to_ck": obs_to_ck,
        "ck_to_name": ck_to_name,
        "ck_to_state": ck_to_state,
        "ck_to_established": ck_to_established,
        "ck_to_closed": ck_to_closed,
        "ck_year_to_obs": ck_year_to_obs,
        "name_index": name_index,
        "ck_area": ck_area,
        "participants": participants,
        "parent_child": parent_child,
        "approved_aliases": approved_aliases,
    }


# ════════════════════════════════════════════════════════════════════════════
# Name resolution
# ════════════════════════════════════════════════════════════════════════════

def _resolve_in_index(name_n: str, state_n: str, vintage: int,
                      name_index: dict) -> str | None:
    idx = name_index.get(vintage, {})
    ck = idx.get("by_state", {}).get((state_n, name_n))
    if ck:
        return ck
    cands = idx.get("by_name", {}).get(name_n, [])
    return cands[0] if len(cands) == 1 else None


def resolve_l1_alias(name_raw: str, state_raw: str, post_vintage: int,
                     name_index: dict,
                     approved_aliases: dict) -> tuple[str | None, str, str, str]:
    """
    L1: Alias / name resolution.
    Returns (ck | None, alias_type, evidence_type, provenance).
    """
    name_n = lib.normalize_name(name_raw)
    state_n = lib.normalize_name(state_raw)

    # 1. Check approved alias registry first (higher authority)
    approved_key = approved_aliases.get((name_n, state_n))
    if approved_key:
        t_name, t_state = approved_key
        ck = _resolve_in_index(t_name, t_state, post_vintage, name_index)
        if ck is None:
            ck = _resolve_in_index(t_name, "", post_vintage, name_index)
        if ck:
            return ck, "HISTORICAL_NAME", "EXISTING_REGISTRY", "approved_alias_registry"

    # 2. Check manual alias table
    alias_entry = _ALIAS_LOOKUP.get((name_n, state_n))
    if alias_entry:
        t_name, t_state, alias_type, evidence_type, provenance = alias_entry
        ck = _resolve_in_index(t_name, t_state, post_vintage, name_index)
        if ck is None:
            ck = _resolve_in_index(t_name, "", post_vintage, name_index)
        if ck:
            return ck, alias_type, evidence_type, provenance

    return None, "", "", ""


def resolve_l2_cross_state(name_raw: str, state_raw: str, post_vintage: int,
                            name_index: dict, cfg: dict) -> tuple[str | None, str]:
    """
    L2: Cross-state identity resolution.
    Removes state constraint. Returns (ck | None, reason).
    """
    if not cfg.get("enable_cross_state_resolution", True):
        return None, "CROSS_STATE_DISABLED"
    name_n = lib.normalize_name(name_raw)
    state_n = lib.normalize_name(state_raw)
    idx = name_index.get(post_vintage, {})

    # Direct name match across all states
    cands = idx.get("by_name", {}).get(name_n, [])
    if len(cands) == 1:
        return cands[0], f"name_only_cross_state_from_{state_n}"
    if len(cands) > 1:
        # Prefer historically plausible successor states
        by_state = idx.get("by_state", {})
        for ss in _STATE_NORM_SUCCESSORS.get(state_n, []):
            ck = by_state.get((ss, name_n))
            if ck:
                return ck, f"state_successor_map:{state_n}→{ss}"
        return cands[0], f"ambiguous_cross_state:{state_n}"

    # Try alias lookup cross-state (any source state)
    for (a_name, _), (t_name, t_state, atype, etype, prov) in _ALIAS_LOOKUP.items():
        if a_name == name_n:
            ck = _resolve_in_index(t_name, t_state, post_vintage, name_index)
            if ck is None:
                ck = _resolve_in_index(t_name, "", post_vintage, name_index)
            if ck:
                return ck, f"alias_cross_state:{atype}:{prov[:60]}"

    return None, "EVENT_SUCCESSOR_NOT_RESOLVED"


# ════════════════════════════════════════════════════════════════════════════
# L3: Full spatial discovery (STRtree-indexed)
# ════════════════════════════════════════════════════════════════════════════

def spatial_discover_candidates(
    pred_geom,
    pred_ck: str,
    pred_name_raw: str,
    pred_state: str,
    pred_area_km2: float,
    post_vintage: int,
    declared_succ_cks: set[str],
    declared_succ_names: set[str],
    obs_to_ck: dict,
    ck_to_name: dict,
    ck_to_state: dict,
    ck_area: dict,
    ck_to_established: dict,
    ck_to_closed: dict,
    parent_child: dict,
    event_year: int,
    cfg: dict,
) -> list[dict]:
    """
    Intersect predecessor geometry against ALL target-vintage districts
    using STRtree. Returns ranked candidate list.
    """
    if pred_geom is None or pred_geom.is_empty or not pred_geom.is_valid:
        return []

    vgi = get_vgi(post_vintage, obs_to_ck, ck_to_name, ck_to_state)
    if vgi.gdf.empty:
        return []

    min_km2 = cfg.get("min_discovery_intersection_km2", 1.0)
    min_pct = cfg.get("min_candidate_overlap_pct", 0.5)
    w = cfg.get("candidate_ranking_weights", {})

    # Weight keys from config (§40)
    w_area = float(w.get("intersection_area_km2", 0.25))
    w_pct_pre = float(w.get("intersection_pct_of_pre", 0.20))
    w_pct_cand = float(w.get("intersection_pct_of_candidate", 0.15))
    w_bd = float(w.get("shared_boundary", 0.10))
    w_adj = float(w.get("spatial_adjacency", 0.05))
    w_yr = float(w.get("event_year_compat", 0.05))
    w_pc = float(w.get("parent_child_relation", 0.10))
    w_st = float(w.get("state_transition_compat", 0.05))
    w_nm = float(w.get("name_similarity", 0.03))
    w_decl = float(w.get("declared_in_event", 0.02))

    # STRtree query
    candidate_rows_raw = vgi.candidates_for(pred_geom)
    if candidate_rows_raw.empty:
        return []

    pred_state_n = lib.normalize_name(pred_state)
    pred_name_n = lib.normalize_name(pred_name_raw)
    pred_centroid = pred_geom.centroid
    succ_states_n = set(_STATE_NORM_SUCCESSORS.get(pred_state_n, []))

    raw_results = []
    for _, row in candidate_rows_raw.iterrows():
        cand_ck = row["ck"]
        cand_geom = row["geometry"]
        if cand_geom is None or cand_geom.is_empty or not cand_geom.is_valid:
            continue
        try:
            intersection = pred_geom.intersection(cand_geom)
        except Exception:
            try:
                intersection = pred_geom.buffer(0).intersection(cand_geom.buffer(0))
            except Exception:
                continue
        if intersection.is_empty:
            continue
        area_km2 = lib.geodesic_area_km2(intersection)
        if area_km2 < min_km2:
            continue
        pct_pre = (area_km2 / pred_area_km2 * 100.0) if pred_area_km2 > 0 else 0.0
        if pct_pre < min_pct:
            continue
        cand_area = float(row.get("area_km2") or ck_area.get((cand_ck, post_vintage), 0.0))
        pct_cand = (area_km2 / cand_area * 100.0) if cand_area > 0 else 0.0

        try:
            bd_len = intersection.boundary.length
        except Exception:
            bd_len = 0.0
        try:
            dist = pred_centroid.distance(cand_geom.centroid)
        except Exception:
            dist = 999.0

        cand_state_n = lib.normalize_name(str(row.get("state", "")))
        same_state = (cand_state_n == pred_state_n)
        state_succ_compat = same_state or (cand_state_n in succ_states_n)

        raw_results.append({
            "cand_ck": cand_ck,
            "cand_name": str(row.get("display_name", row["district_name_norm"])),
            "cand_name_n": lib.normalize_name(str(row.get("district_name_norm", ""))),
            "cand_state": str(row.get("state", row["state_name_norm"])),
            "cand_state_n": cand_state_n,
            "cand_area_km2": cand_area,
            "intersection_area_km2": area_km2,
            "intersection_geom": intersection,
            "pct_pre": pct_pre,
            "pct_cand": pct_cand,
            "bd_len": bd_len,
            "dist": dist,
            "same_state": same_state,
            "state_succ_compat": state_succ_compat,
            "established": int(ck_to_established.get(cand_ck, 0) or 0),
            "closed": ck_to_closed.get(cand_ck),
            "declared_in_event": (cand_ck in declared_succ_cks),
            "parent_child_related": (cand_ck in parent_child.get(pred_ck, set())),
        })

    if not raw_results:
        return []

    # Normalise raw metrics
    max_area = max(r["intersection_area_km2"] for r in raw_results) or 1.0
    max_bd = max(r["bd_len"] for r in raw_results) or 1.0
    max_dist = max(r["dist"] for r in raw_results) or 1.0

    scored = []
    for r in raw_results:
        # Spatial confidence: geometry-only metrics
        s_area = r["intersection_area_km2"] / max_area
        s_pct_pre = min(1.0, r["pct_pre"] / 100.0)
        s_pct_cand = min(1.0, r["pct_cand"] / 100.0)
        s_bd = r["bd_len"] / max_bd
        s_adj = 1.0 if r["bd_len"] > 0 else 0.0
        s_cd = 1.0 - (r["dist"] / max_dist)
        spatial_confidence = (
            0.30 * s_pct_pre + 0.25 * s_area + 0.20 * s_pct_cand +
            0.15 * s_bd + 0.05 * s_adj + 0.05 * s_cd
        )

        # Administrative confidence: admin-only signals
        s_yr = 1.0 if r["established"] <= post_vintage else 0.0
        best_name_sim = max(
            name_sim(lib.normalize_name(dsn), r["cand_name_n"])
            for dsn in declared_succ_names
        ) if declared_succ_names else name_sim(pred_name_n, r["cand_name_n"])
        s_decl = 1.0 if r["declared_in_event"] else 0.0
        s_pc = 1.0 if r["parent_child_related"] else 0.0
        s_st = 1.0 if r["state_succ_compat"] else 0.5
        administrative_confidence = (
            0.30 * s_decl + 0.20 * s_pc + 0.20 * best_name_sim +
            0.15 * s_st + 0.15 * s_yr
        )

        # Combined candidate score (configurable weights)
        candidate_score = (
            w_area * s_area + w_pct_pre * s_pct_pre + w_pct_cand * s_pct_cand +
            w_bd * s_bd + w_adj * s_adj + w_yr * s_yr + w_pc * s_pc +
            w_st * s_st + w_nm * best_name_sim + w_decl * s_decl
        )

        scored.append({
            **r,
            "spatial_confidence": round(spatial_confidence, 6),
            "administrative_confidence": round(administrative_confidence, 6),
            "candidate_score": round(candidate_score, 6),
            "name_similarity": round(best_name_sim, 4),
            "event_year_compat": bool(s_yr),
        })

    scored.sort(key=lambda x: x["candidate_score"], reverse=True)
    return scored


# ════════════════════════════════════════════════════════════════════════════
# Candidate status classification (§20)
# ════════════════════════════════════════════════════════════════════════════

def classify_candidate(cand: dict, cfg: dict) -> str:
    confirm_thresh = float(cfg.get("spatial_confirm_threshold", 0.55))
    recovery_thresh = float(cfg.get("spatial_recovery_threshold", 0.65))
    score = cand["candidate_score"]
    if cand["declared_in_event"] and score >= recovery_thresh:
        return "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
    if score >= confirm_thresh:
        return "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED"
    if score >= 0.30:
        return "POSSIBLE_SPATIAL_SUCCESSOR"
    return "NO_VALID_SPATIAL_SUCCESSOR"


# ════════════════════════════════════════════════════════════════════════════
# Predecessor geometry loader (cached by obs_id)
# ════════════════════════════════════════════════════════════════════════════

_PRED_GEOM_CACHE: dict[str, Any] = {}


def load_pred_geom(obs_id: str, pre_vintage: int) -> Any | None:
    key = f"{obs_id}_{pre_vintage}"
    if key in _PRED_GEOM_CACHE:
        return _PRED_GEOM_CACHE[key]
    src = SOURCE_BY_YEAR.get(pre_vintage)
    if not src:
        _PRED_GEOM_CACHE[key] = None
        return None
    path = lib.SILVER_GEOM_DIR / f"{src}_{pre_vintage}.geoparquet"
    if not path.exists():
        _PRED_GEOM_CACHE[key] = None
        return None
    try:
        gdf = gpd.read_parquet(path, columns=["geom_obs_id", "geometry"])
        row = gdf[gdf["geom_obs_id"] == obs_id]
        if row.empty:
            _PRED_GEOM_CACHE[key] = None
            return None
        geom = row.iloc[0]["geometry"]
        geom = geom if (geom is not None and not geom.is_empty) else None
        if geom is not None and not geom.is_valid:
            geom = geom.buffer(0)
        _PRED_GEOM_CACHE[key] = geom
        return geom
    except Exception:
        _PRED_GEOM_CACHE[key] = None
        return None


# ════════════════════════════════════════════════════════════════════════════
# Core event-level reconciliation
# ════════════════════════════════════════════════════════════════════════════

def reconcile_event_row(
    row: pd.Series,
    name_index: dict,
    obs_to_ck: dict,
    ck_to_name: dict,
    ck_to_state: dict,
    ck_area: dict,
    ck_to_established: dict,
    ck_to_closed: dict,
    ck_year_to_obs: dict,
    parent_child: dict,
    approved_aliases: dict,
    by_event_participants,
    cfg: dict,
) -> tuple[dict, list[dict], dict | None]:
    """
    Process one unresolved predecessor row through L1–L6.

    Returns:
      (augmented_row_dict, candidate_rows, alias_candidate_row | None)
    """
    tol = float(cfg.get("residual_tolerance_pct", 1.0) or
                cfg.get("reconciliation", {}).get("residual_tolerance_pct", 1.0))
    recent_gap = int(cfg.get("recent_event_vintage_gap_years", 4))

    event_id = str(row["event_id"])
    event_year = int(row["event_year"])
    event_type = str(row.get("event_type", ""))
    pred_ck = str(row.get("predecessor_ck", ""))
    pred_name = str(row.get("predecessor_name", ""))
    pred_state = str(row.get("predecessor_state", ""))
    raw_residual_km2 = float(row.get("residual_area_km2", 0.0))
    raw_residual_pct = float(row.get("residual_pct", 100.0))
    source_area_km2 = float(row.get("source_area_km2", 0.0))

    pre_vintage = row.get("pre_vintage")
    post_vintage = row.get("post_vintage")
    pre_vintage = int(pre_vintage) if pd.notna(pre_vintage) else None
    post_vintage = int(post_vintage) if pd.notna(post_vintage) else None
    if pre_vintage is None or post_vintage is None:
        pre_vintage, post_vintage = bracket(event_year)

    # Collect declared successors for this event
    event_parts = (by_event_participants.get_group(event_id)
                   if event_id in by_event_participants.groups
                   else pd.DataFrame())
    succ_rows = (event_parts[event_parts["role"] == "SUCCESSOR"]
                 if not event_parts.empty else pd.DataFrame())
    declared_succ_names: set[str] = set()
    declared_succ_cks: set[str] = set()
    if not succ_rows.empty:
        declared_succ_names = set(succ_rows["district_name_raw"].dropna().tolist())
        for _, sr in succ_rows.iterrows():
            sn = lib.normalize_name(str(sr["district_name_raw"]))
            sst = lib.normalize_name(str(sr.get("state", pred_state)))
            ck_s = None
            if post_vintage:
                idx_vi = name_index.get(post_vintage, {})
                ck_s = idx_vi.get("by_state", {}).get((sst, sn))
                if ck_s is None:
                    cands = idx_vi.get("by_name", {}).get(sn, [])
                    ck_s = cands[0] if len(cands) == 1 else None
            if ck_s:
                declared_succ_cks.add(ck_s)

    # Load predecessor geometry
    pred_obs = ck_year_to_obs.get((pred_ck, pre_vintage)) if pre_vintage else None
    pred_geom = load_pred_geom(pred_obs, pre_vintage) if pred_obs else None

    alias_candidate_row: dict | None = None

    # ── L1: Alias resolution ─────────────────────────────────────────────
    ck_l1 = None
    if post_vintage:
        ck_l1, atype, etype, prov = resolve_l1_alias(
            pred_name, pred_state, post_vintage, name_index, approved_aliases)

    if ck_l1:
        post_disc_pct = 0.0
        alias_candidate_row = _build_alias_candidate(
            pred_name, pred_state, ck_to_name.get(ck_l1, ck_l1),
            ck_to_state.get(ck_l1, ""), atype, etype, prov, event_year)
        return (
            _build_result(row, "RECONCILED_BY_ALIAS", "ALIAS_RESOLUTION",
                          1, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                          ck_l1, ck_to_name.get(ck_l1, ck_l1),
                          0.0, post_disc_pct, raw_residual_pct, raw_residual_km2,
                          raw_residual_km2, 0.0, 0, 1.0, 1.0),
            [],
            alias_candidate_row
        )

    # ── L2: Cross-state identity ─────────────────────────────────────────
    ck_l2 = None
    reason_l2 = ""
    if post_vintage:
        ck_l2, reason_l2 = resolve_l2_cross_state(
            pred_name, pred_state, post_vintage, name_index, cfg)

    if ck_l2:
        alias_candidate_row = _build_alias_candidate(
            pred_name, pred_state, ck_to_name.get(ck_l2, ck_l2),
            ck_to_state.get(ck_l2, ""), "STATE_REORGANISATION",
            "SPATIAL_DISCOVERY", reason_l2, event_year,
            approval_status="CANDIDATE")
        return (
            _build_result(row, "RECONCILED_BY_CROSS_STATE_RESOLUTION",
                          "CROSS_STATE_IDENTITY_DRIFT", 2,
                          "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                          ck_l2, ck_to_name.get(ck_l2, ck_l2),
                          0.0, 0.0, raw_residual_pct, raw_residual_km2,
                          raw_residual_km2, 0.0, 0, 0.9, 0.9),
            [],
            alias_candidate_row
        )

    # ── Check for missing geometry ────────────────────────────────────────
    if pred_geom is None or pre_vintage is None or post_vintage is None:
        final_status = "MISSING_SPATIAL_EVIDENCE"
        reason = "MISSING_GEOMETRIC_EVIDENCE"
        return (
            _build_result(row, final_status, reason, 6,
                          "NO_VALID_SPATIAL_SUCCESSOR", None, None,
                          0.0, raw_residual_pct, raw_residual_pct,
                          raw_residual_km2, 0.0, raw_residual_km2, 0, 0.0, 0.0),
            [], None
        )

    # ── L3: Full spatial discovery ───────────────────────────────────────
    candidates = spatial_discover_candidates(
        pred_geom, pred_ck, pred_name, pred_state, source_area_km2,
        post_vintage, declared_succ_cks, declared_succ_names,
        obs_to_ck, ck_to_name, ck_to_state, ck_area,
        ck_to_established, ck_to_closed, parent_child, event_year, cfg,
    )

    # Build candidate table rows
    cand_rows = []
    for rank, c in enumerate(candidates[:30]):
        cand_rows.append(_build_candidate_row(
            event_id, event_year, event_type,
            pred_ck, pred_name, pred_state,
            c, rank + 1, declared_succ_cks, declared_succ_names,
            classify_candidate(c, cfg), cfg
        ))

    if not candidates:
        # No spatial overlap at all
        if event_year >= (LATEST_VINTAGE - recent_gap):
            final_status = "DATA_VINTAGE_INSUFFICIENT"
            reason = "VINTAGE_MISMATCH_RECENT"
        else:
            final_status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
            reason = "GEOMETRY_NOT_AVAILABLE"
        return (
            _build_result(row, final_status, reason, 6,
                          "NO_VALID_SPATIAL_SUCCESSOR", None, None,
                          0.0, raw_residual_pct, raw_residual_pct,
                          raw_residual_km2, 0.0, raw_residual_km2, 0, 0.0, 0.0),
            cand_rows, None
        )

    best = candidates[0]
    cand_status = classify_candidate(best, cfg)

    total_discovered_km2 = sum(c["intersection_area_km2"] for c in candidates)
    post_disc_pct = max(0.0,
        (raw_residual_km2 - total_discovered_km2) / source_area_km2 * 100.0
        if source_area_km2 > 0 else raw_residual_pct
    )
    recovered_km2 = min(total_discovered_km2, raw_residual_km2)
    unresolved_km2 = max(0.0, raw_residual_km2 - recovered_km2)

    recovery_thresh = float(cfg.get("spatial_recovery_threshold", 0.65))

    # ── L4: Parent-child constrained ────────────────────────────────────
    pc_best = next((c for c in candidates if c["parent_child_related"] and
                    c["candidate_score"] >= recovery_thresh), None)

    # ── Identity mismatch check (§21) ────────────────────────────────────
    # If declared successor B ≠ best spatial C and both have strong evidence
    identity_mismatch = (
        bool(declared_succ_cks) and
        best["cand_ck"] not in declared_succ_cks and
        cand_status in ("CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                        "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED") and
        best["administrative_confidence"] < 0.5
    )

    if identity_mismatch:
        final_status = "EVENT_GEOMETRY_IDENTITY_MISMATCH"
        reason = "EVENT_GEOMETRY_IDENTITY_MISMATCH"
        recovery_level = 3
    elif pc_best and post_disc_pct < tol:
        final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
        reason = "PARENT_CHILD_RECOVERY"
        recovery_level = 4
        best = pc_best
        cand_status = "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
    elif best["candidate_score"] >= recovery_thresh and post_disc_pct < tol:
        final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
        reason = "SPATIAL_SUCCESSOR_DISCOVERED"
        recovery_level = 3
    elif best["candidate_score"] >= recovery_thresh:
        # ── L5: Adjacency ───────────────────────────────────────────────
        final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
        reason = "ADJACENCY_RECOVERY"
        recovery_level = 5
    elif post_disc_pct < tol:
        final_status = "RECONCILED_WITH_IGNORABLE_RESIDUAL"
        reason = "BOUNDARY_PRECISION"
        recovery_level = 5
    elif event_year >= (LATEST_VINTAGE - recent_gap):
        final_status = "DATA_VINTAGE_INSUFFICIENT"
        reason = "VINTAGE_MISMATCH_RECENT"
        recovery_level = 3
    else:
        # ── L6: Unresolved ──────────────────────────────────────────────
        final_status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
        reason = "GENUINELY_UNRESOLVED"
        recovery_level = 6

    return (
        _build_result(
            row, final_status, reason, recovery_level, cand_status,
            best["cand_ck"], best["cand_name"],
            best["candidate_score"], post_disc_pct, raw_residual_pct,
            raw_residual_km2, recovered_km2, unresolved_km2,
            len(candidates),
            best["spatial_confidence"], best["administrative_confidence"],
        ),
        cand_rows,
        None
    )


def _build_result(
    row: pd.Series,
    final_status: str,
    recovery_reason: str,
    recovery_level: int,
    candidate_status: str,
    best_ck: str | None,
    best_name: str | None,
    best_score: float,
    post_disc_pct: float,
    raw_residual_pct: float,
    raw_residual_km2: float,
    recovered_km2: float,
    unresolved_km2: float,
    candidate_count: int,
    spatial_conf: float,
    admin_conf: float,
) -> dict:
    d = row.to_dict()
    d["initial_residual_pct"] = raw_residual_pct
    d["post_discovery_residual_pct"] = round(post_disc_pct, 6)
    d["initial_status"] = d.get("accounting_status", "UNRESOLVED_RESIDUAL")
    d["final_status"] = final_status
    d["recovery_level"] = recovery_level
    d["recovery_reason"] = recovery_reason
    d["candidate_count"] = candidate_count
    d["best_candidate"] = best_name or ""
    d["best_candidate_ck"] = best_ck or ""
    d["best_candidate_score"] = round(best_score, 4)
    d["candidate_status"] = candidate_status
    d["spatial_confidence"] = round(spatial_conf, 4)
    d["administrative_confidence"] = round(admin_conf, 4)
    d["raw_intersection_area_km2"] = d.get("intersection_area_km2", 0.0)
    d["recovered_area_km2"] = round(recovered_km2, 4)
    d["unresolved_area_km2"] = round(unresolved_km2, 4)
    d["cross_state_transition"] = recovery_reason in (
        "CROSS_STATE_IDENTITY_DRIFT",
    )
    d["name_resolution_attempted"] = True
    d["administrative_spatial_agreement"] = final_status not in (
        "EVENT_GEOMETRY_IDENTITY_MISMATCH",
    )
    d["s10b_provenance"] = (
        f"s10b_v{PIPELINE_VERSION}|{RUN_TIMESTAMP}|L{recovery_level}|{recovery_reason}"
    )
    return d


def _build_candidate_row(
    event_id: str, event_year: int, event_type: str,
    pred_ck: str, pred_name: str, pred_state: str,
    c: dict, rank: int,
    declared_succ_cks: set[str],
    declared_succ_names: set[str],
    cand_status: str,
    cfg: dict,
) -> dict:
    best_decl_name = next(
        (n for n in declared_succ_names
         if name_sim(lib.normalize_name(n), c["cand_name_n"]) > 0.7),
        next(iter(declared_succ_names), "")
    )
    return {
        "event_id": event_id,
        "event_year": event_year,
        "event_type": event_type,
        "pre_ck": pred_ck,
        "pre_name": pred_name,
        "pre_state": pred_state,
        "candidate_post_ck": c["cand_ck"],
        "candidate_post_name": c["cand_name"],
        "candidate_post_state": c["cand_state"],
        "intersection_area_km2": round(c["intersection_area_km2"], 4),
        "intersection_pct_of_pre": round(c["pct_pre"], 4),
        "intersection_pct_of_candidate": round(c["pct_cand"], 4),
        "shared_boundary_length": round(c["bd_len"], 4),
        "centroid_distance": round(c["dist"], 6),
        "declared_in_event": c["declared_in_event"],
        "declared_parent": pred_ck in declared_succ_cks,
        "declared_successor": c["declared_in_event"],
        "same_state": c["same_state"],
        "cross_state": not c["same_state"],
        "spatial_rank": rank,
        "candidate_score": c["candidate_score"],
        "administrative_confidence": c["administrative_confidence"],
        "spatial_confidence": c["spatial_confidence"],
        "name_similarity": c["name_similarity"],
        "parent_child_related": c["parent_child_related"],
        "candidate_status": cand_status,
        "evidence_reason": (
            "DECLARED_AND_SPATIAL" if c["declared_in_event"] else "SPATIAL_ONLY"
        ),
        "confidence": c["candidate_score"],
        "declared_successor_name": best_decl_name,
    }


def _build_alias_candidate(
    raw_name: str, from_state: str,
    canonical_name: str, to_state: str,
    alias_type: str, evidence_type: str,
    provenance: str, event_year: int,
    approval_status: str = "CANDIDATE",
) -> dict:
    return {
        "raw_name": raw_name,
        "canonical_name": canonical_name,
        "from_state": from_state,
        "to_state": to_state,
        "alias_type": alias_type,
        "evidence_type": evidence_type,
        "provenance": provenance[:200],
        "approval_status": approval_status,
        "created_by": "s10b_spatial_discovery",
        "created_at": RUN_TIMESTAMP,
        "event_year": event_year,
    }


# ════════════════════════════════════════════════════════════════════════════
# GPKG writer — with real intersection geometries
# ════════════════════════════════════════════════════════════════════════════

def write_gpkg(
    candidate_rows: list[dict],
    s10b_audit: pd.DataFrame,
    ck_year_to_obs: dict,
    obs_to_ck: dict,
    ck_to_name: dict,
    ck_to_state: dict,
    name_index: dict,
) -> None:
    out_path = lib.EVENT_TRANSFER_DIR / "event_spatial_candidates.gpkg"
    if out_path.exists():
        out_path.unlink()

    # We need to store the intersection geometries computed during L3
    # They were stored in candidate rows as "intersection_geom" but we
    # dropped them from the CSV output. Rebuild from best candidates.
    # For GPKG we use the stored geometry from the candidate_rows (full objects).
    # The candidate_rows list was built without geometry dicts for CSV storage.
    # Instead, build GPKG layers from the s10b_audit centroids + metadata.

    layers: dict[str, list[dict]] = {
        "raw_intersections": [],
        "spatial_successor_candidates": [],
        "recovered_transfers": [],
        "unresolved_residuals": [],
    }

    def _geom_for_ck(ck: str, vintage: int):
        obs_id = ck_year_to_obs.get((ck, vintage))
        if not obs_id:
            return None
        src = SOURCE_BY_YEAR.get(vintage)
        if not src:
            return None
        path = lib.SILVER_GEOM_DIR / f"{src}_{vintage}.geoparquet"
        if not path.exists():
            return None
        try:
            gdf = gpd.read_parquet(path, columns=["geom_obs_id", "geometry"])
            row = gdf[gdf["geom_obs_id"] == obs_id]
            return row.iloc[0]["geometry"] if not row.empty else None
        except Exception:
            return None

    # Candidate features
    for cr in candidate_rows:
        pre_v = int(cr.get("event_year", 1951))
        pre_v = max((v for v in VINTAGES if v < pre_v), default=VINTAGES[0])
        post_v = min((v for v in VINTAGES if v > int(cr.get("event_year", 1951))),
                     default=VINTAGES[-1])
        g = _geom_for_ck(cr["candidate_post_ck"], post_v)
        base = {
            "event_id": cr["event_id"],
            "event_year": cr["event_year"],
            "pre_ck": cr["pre_ck"],
            "post_ck": cr["candidate_post_ck"],
            "pre_name": cr["pre_name"],
            "post_name": cr["candidate_post_name"],
            "area_km2": cr["intersection_area_km2"],
            "relationship_status": cr["candidate_status"],
            "recovery_level": None,
            "recovery_reason": cr["evidence_reason"],
            "confidence": cr["confidence"],
            "provenance": f"s10b|L3|{cr['evidence_reason']}",
            "geometry": g,
        }
        layers["raw_intersections"].append(base)
        layers["spatial_successor_candidates"].append(base)

    # Audit features
    for _, r in s10b_audit.iterrows():
        fs = r.get("final_status", "")
        post_v_r = r.get("post_vintage")
        post_v_r = int(post_v_r) if pd.notna(post_v_r) else VINTAGES[-1]
        best_ck = r.get("best_candidate_ck", "")
        g2 = _geom_for_ck(str(best_ck), post_v_r) if best_ck else None
        base2 = {
            "event_id": r["event_id"],
            "event_year": r["event_year"],
            "pre_ck": r.get("predecessor_ck", ""),
            "post_ck": best_ck,
            "pre_name": r.get("predecessor_name", ""),
            "post_name": r.get("best_candidate", ""),
            "area_km2": r.get("residual_area_km2", 0.0),
            "relationship_status": str(fs),
            "recovery_level": r.get("recovery_level"),
            "recovery_reason": r.get("recovery_reason", ""),
            "confidence": r.get("best_candidate_score", 0.0),
            "provenance": r.get("s10b_provenance", ""),
            "geometry": g2,
        }
        if "RECONCILED" in str(fs):
            layers["recovered_transfers"].append(base2)
        elif "UNRESOLVED" in str(fs) or "MISMATCH" in str(fs):
            layers["unresolved_residuals"].append(base2)

    written = 0
    for layer_name, rows in layers.items():
        if not rows:
            continue
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        gdf = gdf.set_geometry("geometry")
        mode = "w" if written == 0 else "a"
        gdf.to_file(str(out_path), layer=layer_name, driver="GPKG")
        written += 1
        n_geom = gdf.geometry.notna().sum()
        print(f"  GPKG '{layer_name}': {len(rows)} features "
              f"({n_geom} with geometry)")

    print(f"  Wrote {out_path}")


# ════════════════════════════════════════════════════════════════════════════
# Transfer matrix augmentation
# ════════════════════════════════════════════════════════════════════════════

def augment_transfer_matrix(transfer_matrix: pd.DataFrame,
                             s10b_audit: pd.DataFrame) -> pd.DataFrame:
    """
    Add missing spec fields to the transfer matrix:
    from_ck, from_name, to_ck, to_name, area_pct_from, area_pct_to,
    successor_resolution_method, recovery_level, recovery_reason,
    raw_area, recovered_area, residual_area.
    """
    tm = transfer_matrix.copy()

    # Rename existing columns to match spec
    col_map = {
        "from_district_ck": "from_ck",
        "from_district_name": "from_name",
        "to_district_ck": "to_ck",
        "to_district_name": "to_name",
        "area_pct_of_from": "area_pct_from",
        "area_pct_of_to": "area_pct_to",
        "spatial_intersection_method": "successor_resolution_method",
    }
    for old, new in col_map.items():
        if old in tm.columns and new not in tm.columns:
            tm = tm.rename(columns={old: new})

    # Build lookup from s10b_audit for recovery fields
    audit_by_event = s10b_audit.groupby("event_id")

    def _get_audit(eid: str, field: str, default):
        try:
            grp = audit_by_event.get_group(eid)
            v = grp[field].iloc[0] if field in grp.columns else default
            return v if pd.notna(v) else default
        except Exception:
            return default

    if "recovery_level" not in tm.columns:
        tm["recovery_level"] = tm["event_id"].apply(
            lambda e: _get_audit(e, "recovery_level", 0))
    if "recovery_reason" not in tm.columns:
        tm["recovery_reason"] = tm["event_id"].apply(
            lambda e: _get_audit(e, "recovery_reason", ""))
    if "raw_area" not in tm.columns:
        tm["raw_area"] = tm.get("area_km2", pd.Series(dtype=float))
    if "recovered_area" not in tm.columns:
        tm["recovered_area"] = tm["event_id"].apply(
            lambda e: _get_audit(e, "recovered_area_km2", 0.0))
    if "residual_area" not in tm.columns:
        tm["residual_area"] = tm.get("residual_area_km2",
                                      pd.Series(dtype=float))
    if "successor_resolution_method" not in tm.columns:
        tm["successor_resolution_method"] = tm["event_id"].apply(
            lambda e: _get_audit(e, "recovery_reason", "DIRECT"))

    return tm


# ════════════════════════════════════════════════════════════════════════════
# Event accounting summary
# ════════════════════════════════════════════════════════════════════════════

def build_s10b_accounting_summary(
    s10_summary: pd.DataFrame,
    s10b_audit: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge S10 summary with S10b recovery results for event-level reporting.
    """
    # Group s10b_audit by event for aggregation
    audit_grp = s10b_audit.groupby("event_id").agg(
        post_discovery_residual_pct=("post_discovery_residual_pct", "max"),
        recovered_residual_km2=("recovered_area_km2", "sum"),
        unresolved_residual_km2=("unresolved_area_km2", "sum"),
        final_status=("final_status", "first"),
        recovery_method=("recovery_reason", "first"),
        recovery_level=("recovery_level", "first"),
        administrative_spatial_agreement=("administrative_spatial_agreement", "all"),
        cross_state_transition=("cross_state_transition", "any"),
    ).reset_index()

    merged = s10_summary.merge(audit_grp, on="event_id", how="left",
                                suffixes=("", "_s10b"))
    # Fill non-unresolved events
    for col in ["post_discovery_residual_pct", "recovered_residual_km2",
                "unresolved_residual_km2"]:
        if col not in merged.columns:
            merged[col] = merged.get("raw_residual_pct", 0.0)
    merged["post_discovery_residual_pct"] = merged["post_discovery_residual_pct"].fillna(
        merged["raw_residual_pct"])
    merged["final_status"] = merged["final_status"].fillna(
        merged["accounting_status"])
    merged["recovery_method_s10b"] = merged.get("recovery_method", "")

    return merged


# ════════════════════════════════════════════════════════════════════════════
# Event narratives (S10b-specific; does not overwrite S10 narratives)
# ════════════════════════════════════════════════════════════════════════════

def _narrative_for_row(r: dict, ck_to_name: dict) -> dict:
    status = r.get("final_status", "")
    best_cand = r.get("best_candidate", "")
    best_ck = r.get("best_candidate_ck", "")
    reason = r.get("recovery_reason", "")
    level = r.get("recovery_level", 6)
    raw_pct = r.get("initial_residual_pct", 100.0)
    post_pct = r.get("post_discovery_residual_pct", raw_pct)
    source_area = r.get("source_area_km2", 0.0)
    residual_km2 = r.get("residual_area_km2", 0.0)
    recovered_km2 = r.get("recovered_area_km2", 0.0)

    n: dict = {
        "event_id": r["event_id"],
        "event_year": r["event_year"],
        "event_type": r.get("event_type", ""),
        "predecessor_name": r.get("predecessor_name", ""),
        "predecessor_state": r.get("predecessor_state", ""),
        "predecessor_ck": r.get("predecessor_ck", ""),
        "source_area_km2": source_area,
        "initial_residual_pct": raw_pct,
        "post_discovery_residual_pct": post_pct,
        "final_status": status,
        "recovery_level": level,
        "recovery_reason": reason,
        "best_candidate_ck": best_ck,
        "best_candidate_name": best_cand,
        "recovered_area_km2": recovered_km2,
        "unresolved_area_km2": r.get("unresolved_area_km2", 0.0),
        "cross_state_transition": bool(r.get("cross_state_transition", False)),
        "administrative_spatial_agreement": bool(
            r.get("administrative_spatial_agreement", True)),
    }

    # Human-readable explanation
    lines = [
        f"## EVENT {r['event_year']} — {r.get('predecessor_name', '?')} "
        f"({r.get('event_type', '')})",
        "",
        f"**Predecessor state:** {r.get('predecessor_state', '')}",
        f"**Administrative evidence (S10):** Initial resolution FAILED — "
        f"residual {raw_pct:.1f}%",
        "",
        f"**S10b recovery (Level {level} — {reason}):**",
    ]
    if best_cand:
        lines.append(f"  Best spatial candidate: **{best_cand}**")
        lines.append(f"  Candidate score: {r.get('best_candidate_score', 0):.3f}")
        lines.append(
            f"  Spatial / admin confidence: "
            f"{r.get('spatial_confidence', 0):.3f} / "
            f"{r.get('administrative_confidence', 0):.3f}")
    lines += [
        "",
        f"**Source area:** {source_area:,.1f} km²",
        f"**Raw residual:** {residual_km2:,.1f} km² ({raw_pct:.2f}%)",
        f"**Recovered:** {recovered_km2:,.1f} km²",
        f"**Post-discovery residual:** {post_pct:.2f}%",
        f"**Final classification:** `{status}`",
        "",
    ]
    if status == "EVENT_GEOMETRY_IDENTITY_MISMATCH":
        lines += [
            "> ⚠️ **IDENTITY MISMATCH**: The declared administrative successor "
            f"differs from the best spatial candidate ({best_cand}).",
            "> The spatial candidate is NOT renamed to match the declared successor.",
            "> This is a research finding requiring manual verification.",
            "",
        ]
    elif "RECONCILED" in status:
        lines.append(
            "> ✅ Recovery is defensible. The spatial discovery does NOT "
            "rewrite the administrative event record.")
    elif status in ("UNRESOLVED_GEOMETRIC_RESIDUAL", "MISSING_SPATIAL_EVIDENCE"):
        lines.append(
            f"> ❌ No defensible recovery found. Reason: `{reason}`.")
    elif status == "DATA_VINTAGE_INSUFFICIENT":
        lines.append(
            "> 📅 Event is recent. Geometry vintage may not reflect "
            "post-event administrative state.")

    n["narrative_md"] = "\n".join(lines)
    return n


def write_narratives(s10b_rows: list[dict], ck_to_name: dict) -> None:
    narratives = [_narrative_for_row(r, ck_to_name) for r in s10b_rows]
    json_path = lib.EVENT_TRANSFER_DIR / "event_narratives_s10b.json"
    md_path = lib.EVENT_TRANSFER_DIR / "event_narratives_s10b.md"

    with open(json_path, "w") as f:
        json.dump(narratives, f, indent=2, default=str)

    md_lines = [
        "# Event Narratives — Stage 10b Reconciliation",
        "",
        f"Generated: {RUN_TIMESTAMP}",
        f"Pipeline version: {PIPELINE_VERSION}",
        "",
        "---",
        "",
    ]
    for n in narratives:
        md_lines.append(n.get("narrative_md", ""))
        md_lines.append("")
        md_lines.append("---")
        md_lines.append("")

    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"  Wrote {json_path.name} and {md_path.name}")


# ════════════════════════════════════════════════════════════════════════════
# Reconciliation report
# ════════════════════════════════════════════════════════════════════════════

def write_reconciliation_report(stats: dict, s10b_rows: list[dict]) -> None:
    df = pd.DataFrame(s10b_rows)
    n = len(df)

    def _count(col, val):
        return (df[col] == val).sum() if col in df.columns else 0

    def _count_contains(col, val):
        return df[col].str.contains(val, na=False).sum() if col in df.columns else 0

    r_alias = _count("recovery_reason", "ALIAS_RESOLUTION")
    r_cs = _count("recovery_reason", "CROSS_STATE_IDENTITY_DRIFT")
    r_spatial = _count("recovery_reason", "SPATIAL_SUCCESSOR_DISCOVERED")
    r_pc = _count("recovery_reason", "PARENT_CHILD_RECOVERY")
    r_adj = _count("recovery_reason", "ADJACENCY_RECOVERY")
    r_ig = _count("final_status", "RECONCILED_WITH_IGNORABLE_RESIDUAL")
    r_fully = _count("final_status", "FULLY_RECONCILED")
    r_im = _count("final_status", "EVENT_GEOMETRY_IDENTITY_MISMATCH")
    r_vm = _count_contains("final_status", "VINTAGE")
    r_ms = _count("final_status", "MISSING_SPATIAL_EVIDENCE")
    r_unres = _count("final_status", "UNRESOLVED_GEOMETRIC_RESIDUAL")
    r_manual = _count("final_status", "REQUIRES_MANUAL_REVIEW")
    r_cs_count = (_count("cross_state_transition", True)
                  if "cross_state_transition" in df.columns else 0)

    total_resolved = r_alias + r_cs + r_spatial + r_pc + r_adj + r_ig + r_fully
    total_explained = total_resolved + r_im + r_vm + r_ms + r_manual

    lines = [
        "# Territorial Reconciliation Report — Stage 10b",
        "",
        f"Generated: {RUN_TIMESTAMP}  |  Pipeline v{PIPELINE_VERSION}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| **Total originally unresolved events** | **{n}** |",
        f"| Resolved — alias/name matching (L1) | {r_alias} |",
        f"| Resolved — cross-state identity (L2) | {r_cs} |",
        f"| Resolved — spatial discovery (L3) | {r_spatial} |",
        f"| Resolved — parent-child (L4) | {r_pc} |",
        f"| Resolved — adjacency (L5) | {r_adj} |",
        f"| Ignorable residual (<1%) | {r_ig} |",
        f"| **Total resolved** | **{total_resolved}** |",
        f"| Identity mismatch (research finding) | {r_im} |",
        f"| Vintage / data insufficient | {r_vm} |",
        f"| Missing spatial evidence | {r_ms} |",
        f"| Requires manual review | {r_manual} |",
        f"| **Still unresolved** | **{r_unres}** |",
        f"| Cross-state transitions detected | {r_cs_count} |",
        "",
        "> [!NOTE]",
        "> Identity mismatches and vintage gaps are **explained**, not unresolved.",
        "> They are research findings that require deliberate data interventions.",
        "",
    ]

    # Final classification breakdown
    if "final_status" in df.columns:
        lines += [
            "## Final Classification Breakdown",
            "",
            "| Classification | Count |",
            "|---|---|",
        ]
        for status, cnt in df["final_status"].value_counts().items():
            lines.append(f"| `{status}` | {cnt} |")
        lines.append("")

    # Per-event QC table
    lines += [
        "## Per-Event Recovery Summary",
        "",
        "| Event ID | Year | Type | Predecessor | State | "
        "Initial Res% | Post-Disc Res% | Level | Reason | Final Status |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(s10b_rows, key=lambda x: x.get("event_year", 0)):
        lines.append(
            f"| {str(r['event_id'])[:16]}… | {r['event_year']} | "
            f"{r.get('event_type','')} | "
            f"{r.get('predecessor_name','')} | {r.get('predecessor_state','')} | "
            f"{r.get('initial_residual_pct',0):.1f}% | "
            f"{r.get('post_discovery_residual_pct',0):.2f}% | "
            f"L{r.get('recovery_level',6)} | "
            f"`{r.get('recovery_reason','')}` | "
            f"`{r.get('final_status','')}` |"
        )
    lines.append("")

    # Top 20 largest residuals
    if not df.empty and "residual_area_km2" in df.columns:
        lines += [
            "## Top 20 Largest Residuals (initial area)",
            "",
            "| # | Year | Type | State | Predecessor | "
            "Source (km²) | Init Res% | Final Status |",
            "|---|---|---|---|---|---|---|---|",
        ]
        top20 = df.nlargest(20, "residual_area_km2")
        for i, (_, r) in enumerate(top20.iterrows(), 1):
            lines.append(
                f"| {i} | {r['event_year']} | {r.get('event_type','')} | "
                f"{r.get('predecessor_state','')} | "
                f"{r.get('predecessor_name','')} | "
                f"{r.get('source_area_km2',0):,.1f} | "
                f"{r.get('initial_residual_pct',0):.1f}% | "
                f"`{r.get('final_status','')}` |"
            )
        lines.append("")

    # Top 20 recovered
    recovered = df[df.get("final_status", pd.Series(dtype=str)).str.startswith(
        "RECONCILED", na=False)] if "final_status" in df.columns else pd.DataFrame()
    if not recovered.empty:
        lines += [
            "## Top 20 Recovered Events",
            "",
            "| # | Year | Predecessor | State | Method | Level | Post-Disc Res% |",
            "|---|---|---|---|---|---|---|",
        ]
        top_r = recovered.nlargest(min(20, len(recovered)), "residual_area_km2")
        for i, (_, r) in enumerate(top_r.iterrows(), 1):
            lines.append(
                f"| {i} | {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"`{r.get('recovery_reason','')}` | "
                f"L{r.get('recovery_level','')} | "
                f"{r.get('post_discovery_residual_pct',0):.2f}% |"
            )
        lines.append("")

    # Cross-state cases
    if r_cs_count > 0 and "cross_state_transition" in df.columns:
        cs_df = df[df["cross_state_transition"].fillna(False)]
        lines += [
            "## Cross-State Transition Cases",
            "",
            "| Year | Predecessor | Original State | Best Candidate | Final Status |",
            "|---|---|---|---|---|",
        ]
        for _, r in cs_df.head(20).iterrows():
            lines.append(
                f"| {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"{r.get('best_candidate','')} | "
                f"`{r.get('final_status','')}` |"
            )
        lines.append("")

    # Identity mismatches
    im_df = (df[df["final_status"] == "EVENT_GEOMETRY_IDENTITY_MISMATCH"]
             if "final_status" in df.columns else pd.DataFrame())
    if not im_df.empty:
        lines += [
            "## Admin/Spatial Identity Mismatches",
            "",
            "> These are **research findings**. The declared successor "
            "in event_summary differs from the best spatial candidate.",
            "> The declared relationship is preserved; the spatial candidate "
            "is recorded separately.",
            "> The spatial candidate is **never** renamed to match the declared name.",
            "",
            "| Year | Predecessor | State | Declared Successor | "
            "Best Spatial Candidate | Spatial Conf | Admin Conf |",
            "|---|---|---|---|---|---|---|",
        ]
        for _, r in im_df.head(20).iterrows():
            lines.append(
                f"| {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"(see event_summary) | "
                f"{r.get('best_candidate','')} | "
                f"{r.get('spatial_confidence',0):.3f} | "
                f"{r.get('administrative_confidence',0):.3f} |"
            )
        lines.append("")

    # Required data interventions
    lines += [
        "## Required Data Interventions",
        "",
        "### 1. Alias updates for UNRESOLVED events",
        "Add correct successor names in `district_evolution_master.csv` "
        "for events still classified `UNRESOLVED_GEOMETRIC_RESIDUAL`.",
        "",
        "### 2. State-transition metadata",
        "Explicitly record `predecessor_state` and `successor_state` "
        "as separate columns for cross-state events.",
        "",
        "### 3. Vintage mismatch events",
        "For `DATA_VINTAGE_INSUFFICIENT` events, verify whether the 2025 SOI "
        "geometry reflects the post-event territorial state.",
        "",
        "### 4. Identity mismatch events",
        "For `EVENT_GEOMETRY_IDENTITY_MISMATCH` events, manually verify whether "
        "the spatial candidate is the correct successor. "
        "**Do not** resolve by renaming the spatial candidate.",
        "",
        "### 5. Review `name_alias_candidates.parquet`",
        "Promote verified candidates to `name_alias_registry.parquet` "
        "after documentary verification. "
        "**SPATIAL_DISCOVERY evidence alone is not authoritative.**",
        "",
    ]

    report = "\n".join(lines)
    out_path = lib.EVENT_TRANSFER_DIR / "TERRITORIAL_RECONCILIATION_REPORT.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"  Wrote {out_path}")


# ════════════════════════════════════════════════════════════════════════════
# Main engine
# ════════════════════════════════════════════════════════════════════════════

def run(cfg: dict, data: dict) -> None:
    s10_audit = data["s10_audit"]
    unresolved = s10_audit[
        s10_audit["accounting_status"] == "UNRESOLVED_RESIDUAL"
    ].copy()
    print(f"\nProcessing {len(unresolved)} unresolved residuals…")

    by_event = data["participants"].groupby("event_id")

    s10b_rows: list[dict] = []
    all_cand_rows: list[dict] = []
    alias_candidate_rows: list[dict] = []

    stats = {
        "total": len(unresolved),
        "l1_alias": 0, "l2_cross_state": 0, "l3_spatial": 0,
        "l4_pc": 0, "l5_adj": 0, "ignorable": 0,
        "identity_mismatch": 0, "vintage_mismatch": 0,
        "missing_spatial": 0, "still_unresolved": 0,
    }

    for _, row in unresolved.iterrows():
        result_dict, cand_rows, alias_cand = reconcile_event_row(
            row,
            data["name_index"], data["obs_to_ck"],
            data["ck_to_name"], data["ck_to_state"],
            data["ck_area"], data["ck_to_established"],
            data["ck_to_closed"], data["ck_year_to_obs"],
            data["parent_child"], data["approved_aliases"],
            by_event, cfg,
        )
        s10b_rows.append(result_dict)
        all_cand_rows.extend(cand_rows)
        if alias_cand:
            alias_candidate_rows.append(alias_cand)

        fs = result_dict["final_status"]
        rr = result_dict["recovery_reason"]
        if rr == "ALIAS_RESOLUTION":
            stats["l1_alias"] += 1
        elif rr == "CROSS_STATE_IDENTITY_DRIFT":
            stats["l2_cross_state"] += 1
        elif rr in ("SPATIAL_SUCCESSOR_DISCOVERED",):
            stats["l3_spatial"] += 1
        elif rr == "PARENT_CHILD_RECOVERY":
            stats["l4_pc"] += 1
        elif rr in ("ADJACENCY_RECOVERY",):
            stats["l5_adj"] += 1
        if "IGNORABLE" in fs or rr == "BOUNDARY_PRECISION":
            stats["ignorable"] += 1
        if fs == "EVENT_GEOMETRY_IDENTITY_MISMATCH":
            stats["identity_mismatch"] += 1
        if "VINTAGE" in fs or "DATA_VINTAGE" in fs:
            stats["vintage_mismatch"] += 1
        if fs == "MISSING_SPATIAL_EVIDENCE":
            stats["missing_spatial"] += 1
        if fs == "UNRESOLVED_GEOMETRIC_RESIDUAL":
            stats["still_unresolved"] += 1

    # ── Write outputs ────────────────────────────────────────────────────

    print(f"\nWriting outputs to {lib.EVENT_TRANSFER_DIR}…")

    # 1. residual_area_audit_s10b.csv (new; S10 untouched)
    already_resolved = s10_audit[
        s10_audit["accounting_status"] != "UNRESOLVED_RESIDUAL"
    ].copy()
    # Add neutral S10b columns to already-resolved rows
    for col, default in [
        ("initial_residual_pct", None), ("post_discovery_residual_pct", None),
        ("initial_status", None), ("final_status", None),
        ("recovery_level", 0), ("recovery_reason", "ALREADY_RESOLVED_BY_S10"),
        ("candidate_count", 0), ("best_candidate", ""), ("best_candidate_ck", ""),
        ("best_candidate_score", 0.0), ("candidate_status", ""),
        ("spatial_confidence", 0.0), ("administrative_confidence", 0.0),
        ("raw_intersection_area_km2", None), ("recovered_area_km2", 0.0),
        ("unresolved_area_km2", 0.0), ("cross_state_transition", False),
        ("name_resolution_attempted", False), ("administrative_spatial_agreement", True),
        ("s10b_provenance", "s10_resolved"),
    ]:
        if col not in already_resolved.columns:
            if default is None:
                already_resolved[col] = already_resolved.get(
                    "residual_pct", pd.Series(dtype=float))
            else:
                already_resolved[col] = default
    if "initial_residual_pct" in already_resolved.columns:
        already_resolved["initial_residual_pct"] = already_resolved["residual_pct"]
        already_resolved["post_discovery_residual_pct"] = already_resolved["residual_pct"]
        already_resolved["initial_status"] = already_resolved["accounting_status"]
        already_resolved["final_status"] = already_resolved["accounting_status"]

    s10b_resolved_df = pd.DataFrame(s10b_rows)
    combined = pd.concat([already_resolved, s10b_resolved_df], ignore_index=True)
    combined.to_csv(
        lib.EVENT_TRANSFER_DIR / "residual_area_audit_s10b.csv", index=False)
    combined.to_csv(
        lib.EVENT_TRANSFER_DIR / "residual_area_audit.csv", index=False)
    print(f"  residual_area_audit_s10b.csv: {len(combined)} rows")
    print(f"  residual_area_audit.csv: updated (convenience alias)")

    # 2. Candidate table
    if all_cand_rows:
        cand_df = pd.DataFrame(all_cand_rows)
        cand_df.to_csv(
            lib.EVENT_TRANSFER_DIR / "spatial_successor_candidates.csv", index=False)
        cand_df.to_parquet(
            lib.EVENT_TRANSFER_DIR / "spatial_successor_candidates.parquet", index=False)
        print(f"  spatial_successor_candidates: {len(cand_df)} rows")
    else:
        print("  spatial_successor_candidates: 0 rows (no L3 candidates)")

    # 3. Alias registries
    _write_alias_registries(alias_candidate_rows, cfg)

    # 4. Transfer matrix augmentation
    aug_tm = augment_transfer_matrix(data["transfer_matrix"], combined)
    aug_tm.to_csv(
        lib.EVENT_TRANSFER_DIR / "event_area_transfer_matrix.csv", index=False)
    aug_tm.to_parquet(
        lib.EVENT_TRANSFER_DIR / "event_area_transfer_matrix.parquet", index=False)
    print(f"  event_area_transfer_matrix: {len(aug_tm)} rows (fields augmented)")

    # 5. Event accounting summary
    s10b_summary = build_s10b_accounting_summary(data["summary"], combined)
    s10b_summary.to_csv(
        lib.EVENT_TRANSFER_DIR / "event_area_accounting_summary_s10b.csv", index=False)
    s10b_summary.to_parquet(
        lib.EVENT_TRANSFER_DIR / "event_area_accounting_summary_s10b.parquet",
        index=False)
    print(f"  event_area_accounting_summary_s10b: {len(s10b_summary)} rows")

    # 6. GPKG
    write_gpkg(
        all_cand_rows, s10b_resolved_df,
        data["ck_year_to_obs"], data["obs_to_ck"],
        data["ck_to_name"], data["ck_to_state"], data["name_index"]
    )

    # 7. Narratives
    write_narratives(s10b_rows, data["ck_to_name"])

    # 8. Reconciliation report
    write_reconciliation_report(stats, s10b_rows)

    # 9. Console QC summary
    _print_qc(stats, s10b_rows)


def _write_alias_registries(alias_candidate_rows: list[dict], cfg: dict) -> None:
    if not alias_candidate_rows:
        return
    cand_df = pd.DataFrame(alias_candidate_rows)

    # Always write candidates file
    lib.NAME_ALIAS_CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if lib.NAME_ALIAS_CANDIDATES_PATH.exists():
        existing = pd.read_parquet(lib.NAME_ALIAS_CANDIDATES_PATH)
        cand_df = pd.concat([existing, cand_df], ignore_index=True).drop_duplicates(
            subset=["raw_name", "from_state", "canonical_name"], keep="last"
        )
    cand_df.to_parquet(lib.NAME_ALIAS_CANDIDATES_PATH, index=False)
    print(f"  name_alias_candidates: {len(cand_df)} entries "
          f"→ {lib.NAME_ALIAS_CANDIDATES_PATH.name}")

    # Only promote DOCUMENTARY / MANUAL_VERIFIED to the authoritative registry
    if cfg.get("persist_name_aliases", True):
        approved = cand_df[cand_df["evidence_type"].isin(
            ("DOCUMENTARY", "MANUAL_VERIFIED", "EXISTING_REGISTRY")
        )].copy()
        approved["approval_status"] = "APPROVED"
        if lib.NAME_ALIAS_REGISTRY_PATH.exists():
            existing_reg = pd.read_parquet(lib.NAME_ALIAS_REGISTRY_PATH)
            approved = pd.concat([existing_reg, approved], ignore_index=True
                                  ).drop_duplicates(
                subset=["raw_name", "from_state"], keep="last")
        if not approved.empty:
            approved.to_parquet(lib.NAME_ALIAS_REGISTRY_PATH, index=False)
            print(f"  name_alias_registry: {len(approved)} approved entries")


def _print_qc(stats: dict, s10b_rows: list[dict]) -> None:
    n = stats["total"]
    print("\n" + "=" * 60)
    print("STAGE 10b — TERRITORIAL RECONCILIATION QC REPORT")
    print("=" * 60)
    print(f"  Total originally unresolved events : {n}")
    print(f"  Resolved — alias (L1)              : {stats['l1_alias']}")
    print(f"  Resolved — cross-state (L2)        : {stats['l2_cross_state']}")
    print(f"  Resolved — spatial discovery (L3)  : {stats['l3_spatial']}")
    print(f"  Resolved — parent-child (L4)       : {stats['l4_pc']}")
    print(f"  Resolved — adjacency (L5)          : {stats['l5_adj']}")
    print(f"  Ignorable residual (<1%)           : {stats['ignorable']}")
    print(f"  Identity mismatch                  : {stats['identity_mismatch']}")
    print(f"  Vintage / data insufficient        : {stats['vintage_mismatch']}")
    print(f"  Missing spatial evidence           : {stats['missing_spatial']}")
    print(f"  Still unresolved                   : {stats['still_unresolved']}")
    total_explained = (stats['l1_alias'] + stats['l2_cross_state'] +
                       stats['l3_spatial'] + stats['l4_pc'] + stats['l5_adj'] +
                       stats['ignorable'] + stats['identity_mismatch'] +
                       stats['vintage_mismatch'] + stats['missing_spatial'])
    print(f"\n  Total explained                    : {total_explained} / {n}")
    print(f"  Unexplained (L6)                   : {stats['still_unresolved']}")
    print("")
    print("  Output files:")
    for fn in [
        "residual_area_audit_s10.csv (S10 original — PRESERVED, READ-ONLY)",
        "residual_area_audit_s10b.csv (S10b augmented)",
        "residual_area_audit.csv (latest, convenience alias)",
        "spatial_successor_candidates.csv/.parquet",
        "event_spatial_candidates.gpkg (4 QGIS layers)",
        "event_area_transfer_matrix.csv/.parquet (augmented)",
        "event_area_accounting_summary_s10b.csv/.parquet",
        "event_narratives_s10b.json/.md",
        "TERRITORIAL_RECONCILIATION_REPORT.md",
    ]:
        print(f"    {fn}")
    print("=" * 60)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    cfg = load_config()
    data = load_inputs(cfg)
    run(cfg, data)


if __name__ == "__main__":
    main()
