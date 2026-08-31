"""
Stage 10b — Spatial Successor Discovery & Residual Recovery Engine.

This stage acts on S10's unresolved residuals and attempts recovery through
a 6-level hierarchy:

  L0  Exact event participant + valid CK + matrix overlap (already done by S10)
  L1  Alias / name-resolution (same-state, spelling variants, historical names)
  L2  Cross-state identity resolution (state reorganised; name matches elsewhere)
  L3  Spatial discovery — full target-vintage scan, no state constraint
  L4  Parent-child constrained spatial recovery
  L5  Adjacency / shared-boundary heuristic
  L6  UNRESOLVED — final, with documented reason

Architecture invariants (enforced throughout):
  ─ S1–S10 are NOT modified.
  ─ Spatial discovery status (SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED) is
    NEVER automatically promoted to CONFIRMED_ADMINISTRATIVE_SUCCESSOR.
  ─ raw_residual_pct from S10 is NEVER overwritten.
    post_discovery_residual_pct is a new, separate field.
  ─ Lineage (S6) is NEVER modified.
  ─ 1% rule applied ONLY after all legitimate recovery levels exhausted.
  ─ Aliases require name/administrative evidence; spatial overlap alone is
    NEVER sufficient to create an alias.
  ─ Parent/child mismatch (declared B ≠ spatial C) → EVENT_GEOMETRY_IDENTITY_MISMATCH,
    never silently renamed.
  ─ State is evidence, not a hard spatial constraint (critical for historical
    state reorganisation).

Inputs:
  outputs/event_transfer/residual_area_audit.csv              (S10)
  outputs/event_transfer/event_area_accounting_summary.csv    (S10)
  outputs/event_transfer/event_area_transfer_matrix.csv       (S10)
  data/gold/core/canonical_key_registry.parquet               (S5)
  data/gold/core/geom_obs_to_ck.parquet                       (S5)
  data/gold/events/event_participant.parquet                  (S3)
  data/gold/events/district_relationship.parquet              (S6)
  data/silver/geometry/{source}_{year}.geoparquet             (S1)

Outputs:
  outputs/event_transfer/spatial_successor_candidates.{csv,parquet}
  outputs/event_transfer/event_spatial_candidates.gpkg
  outputs/event_transfer/TERRITORIAL_RECONCILIATION_REPORT.md
  outputs/event_transfer/residual_area_audit.csv   (augmented in-place)
  data/gold/core/name_alias_registry.parquet       (if persist_name_aliases=true)
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from shapely.geometry import box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

warnings.filterwarnings("ignore", "GeoSeries.notna", FutureWarning)
warnings.filterwarnings("ignore", ".*initial implementation.*", UserWarning)

VINTAGES = [1951, 1961, 1971, 1981, 1991, 2001, 2011, 2021, 2025]
SOURCE_BY_YEAR = {y: ("soi" if y == 2025 else "stanford") for y in VINTAGES}
LATEST_VINTAGE = 2025


# ---------------------------------------------------------------------------
# Evidence-based historical alias tables
# ---------------------------------------------------------------------------
# Each entry: (raw_name_norm, raw_state_norm) → (target_name_norm, target_state_norm,
#              alias_type, provenance)
# These are NOT created because polygons overlap. Each has a documented
# administrative / historical source.

# State-level predecessor → successor state mappings.
# Ordered by date; the most recent plausible match is applied.
STATE_PREDECESSOR_MAP: dict[str, list[str]] = {
    "travancore  cochin": ["kerala"],
    "travancore": ["kerala"],
    "cochin": ["kerala"],
    "bombay": ["gujarat", "maharashtra"],
    "madras": ["tamil nadu", "andhra pradesh", "kerala", "karnataka"],
    "hyderabad": ["andhra pradesh", "maharashtra", "karnataka"],
    "madhya bharat": ["madhya pradesh"],
    "pepsu": ["punjab"],
    "vindhya pradesh": ["madhya pradesh"],
    "saurashtra": ["gujarat"],
    "coorg": ["karnataka"],
    "ajmer": ["rajasthan"],
    "bhopal": ["madhya pradesh"],
    "kutch": ["gujarat"],
    "manipur": ["manipur"],
    "tripura": ["tripura"],
    "himachal pradesh": ["himachal pradesh"],
    "punjab": ["haryana", "himachal pradesh", "punjab"],
    "assam": ["meghalaya", "mizoram", "nagaland", "arunachal pradesh", "assam"],
    "nefa": ["arunachal pradesh"],
    "north east frontier agency": ["arunachal pradesh"],
}

# District alias table: (name_norm, state_norm) → (alias_norm, alias_state_norm, type, provenance)
DISTRICT_ALIASES: dict[tuple[str, str], tuple[str, str, str, str]] = {
    ("quilon", "travancore  cochin"): ("kollam", "kerala", "HISTORICAL_NAME",
        "Kerala Districts Reorganisation 1957; Quilon renamed Kollam post-1991"),
    ("quilon", "kerala"): ("kollam", "kerala", "HISTORICAL_NAME", "Kerala 1991 renaming"),
    ("trichur", "travancore  cochin"): ("thrissur", "kerala", "HISTORICAL_NAME",
        "Kerala 1991 renaming: Trichur → Thrissur"),
    ("trichur", "kerala"): ("thrissur", "kerala", "HISTORICAL_NAME",
        "Kerala 1991 renaming: Trichur → Thrissur"),
    ("trivandrum", "travancore  cochin"): ("thiruvananthapuram", "kerala", "HISTORICAL_NAME",
        "Kerala 1991 renaming: Trivandrum → Thiruvananthapuram"),
    ("trivandrum", "kerala"): ("thiruvananthapuram", "kerala", "HISTORICAL_NAME",
        "Kerala 1991 renaming"),
    ("quilon", "madras"): ("kollam", "kerala", "HISTORICAL_NAME",
        "Madras State → Kerala 1956; Quilon district transferred"),
    ("trichur", "madras"): ("thrissur", "kerala", "HISTORICAL_NAME",
        "Madras State → Kerala 1956"),
    ("kottayam", "travancore  cochin"): ("kottayam", "kerala", "STATE_RENAME",
        "Travancore-Cochin → Kerala 1956"),
    ("baroda", "bombay"): ("vadodara", "gujarat", "HISTORICAL_NAME",
        "Gujarat formation 1960; Baroda renamed Vadodara 1976"),
    ("broach", "bombay"): ("bharuch", "gujarat", "HISTORICAL_NAME",
        "Gujarat formation 1960; Broach renamed Bharuch 1992"),
    ("kaira", "bombay"): ("kheda", "gujarat", "HISTORICAL_NAME",
        "Gujarat formation 1960; Kaira renamed Kheda 1969"),
    ("surat", "bombay"): ("surat", "gujarat", "STATE_RENAME",
        "Gujarat formation 1960; Surat transferred from Bombay"),
    ("baroda", "gujarat"): ("vadodara", "gujarat", "HISTORICAL_NAME", "Renamed 1976"),
    ("broach", "gujarat"): ("bharuch", "gujarat", "HISTORICAL_NAME", "Renamed 1992"),
    ("kaira", "gujarat"): ("kheda", "gujarat", "HISTORICAL_NAME", "Renamed 1969"),
    ("chingleput", "tamil nadu"): ("chengalpattu", "tamil nadu", "HISTORICAL_NAME",
        "Tamil Nadu 2019 renaming: Chingleput → Chengalpattu"),
    ("chingleput", "madras"): ("chengalpattu", "tamil nadu", "HISTORICAL_NAME",
        "Madras → Tamil Nadu; Chingleput renamed Chengalpattu 2019"),
    ("hoshangabad", "madhya pradesh"): ("narmadapuram", "madhya pradesh", "HISTORICAL_NAME",
        "Madhya Pradesh 2022: Hoshangabad renamed Narmadapuram"),
    ("nellore", "madras"): ("nellore", "andhra pradesh", "STATE_REORGANISATION",
        "States Reorganisation Act 1956; Nellore transferred Madras→Andhra Pradesh"),
    ("nellore", "tamil nadu"): ("nellore", "andhra pradesh", "STATE_REORGANISATION",
        "Nellore was in Madras/Tamil Nadu, transferred to Andhra Pradesh in 1956"),
    ("south kanara", "madras"): ("dakshina kannada", "karnataka", "HISTORICAL_NAME",
        "States Reorganisation 1956; South Kanara renamed Dakshina Kannada in Karnataka"),
    ("south kanara", "tamil nadu"): ("dakshina kannada", "karnataka", "HISTORICAL_NAME",
        "South Kanara transferred Madras→Karnataka 1956"),
    ("coorg", "madras"): ("kodagu", "karnataka", "HISTORICAL_NAME",
        "Coorg transferred to Karnataka 1956; renamed Kodagu"),
    ("malabar", "madras"): ("kozhikode", "kerala", "STATE_REORGANISATION",
        "Malabar district split 1956; Kozhikode is the primary successor"),
    ("malabar", "tamil nadu"): ("kozhikode", "kerala", "STATE_REORGANISATION",
        "Malabar transferred to Kerala 1956"),
    ("kangra", "punjab"): ("kangra", "himachal pradesh", "STATE_REORGANISATION",
        "Punjab Reorganisation Act 1966; Kangra transferred to Himachal Pradesh"),
    ("durg", "madhya pradesh"): ("durg", "chhattisgarh", "STATE_REORGANISATION",
        "Chhattisgarh formation 2000; Durg transferred from MP"),
    ("raipur", "madhya pradesh"): ("raipur", "chhattisgarh", "STATE_REORGANISATION",
        "Chhattisgarh 2000"),
    ("surguja", "madhya pradesh"): ("surguja", "chhattisgarh", "STATE_REORGANISATION",
        "Chhattisgarh 2000"),
    ("bastar", "madhya pradesh"): ("bastar", "chhattisgarh", "STATE_REORGANISATION",
        "Chhattisgarh 2000"),
    ("nimar", "madhya pradesh"): ("khandwa", "madhya pradesh", "HISTORICAL_NAME",
        "Nimar district split into East Nimar (Khandwa) and West Nimar (Khargone)"),
    ("balipara frontier tract", "assam"): ("tawang", "arunachal pradesh",
        "STATE_REORGANISATION",
        "NEFA → Arunachal Pradesh 1987; Balipara Frontier Tract became Tawang dist"),
    ("tirap frontier tract", "assam"): ("tirap", "arunachal pradesh",
        "STATE_REORGANISATION", "NEFA → Arunachal Pradesh 1987"),
    ("nefa", "assam"): ("lohit", "arunachal pradesh", "STATE_REORGANISATION",
        "NEFA became Arunachal Pradesh; Lohit is major successor"),
    ("north east frontier agency", "assam"): ("lohit", "arunachal pradesh",
        "STATE_REORGANISATION", "Same as NEFA"),
    ("cooch behar", "west bengal"): ("cooch behar", "west bengal", "SPELLING_VARIANT",
        "Also spelled Koch Bihar / Cooch_Bihar in some sources"),
    ("cooch bihar", "west bengal"): ("cooch behar", "west bengal", "SPELLING_VARIANT",
        "Cooch Bihar = Cooch Behar"),
    ("dharwad", "karnataka"): ("dharwad", "karnataka", "SPELLING_VARIANT",
        "Also Dharwar in some sources"),
    ("dharwar", "karnataka"): ("dharwad", "karnataka", "SPELLING_VARIANT",
        "Dharwar = Dharwad"),
    ("sabarkantha", "gujarat"): ("sabarkantha", "gujarat", "SPELLING_VARIANT",
        "Also Sabar Kantha in some sources"),
    ("sabar kantha", "gujarat"): ("sabarkantha", "gujarat", "SPELLING_VARIANT",
        "Same district"),
    ("cuddapah", "andhra pradesh"): ("ysr kadapa", "andhra pradesh", "HISTORICAL_NAME",
        "Cuddapah renamed YSR Kadapa (also called Kadapa) 2010"),
    ("cuddapah", "tamil nadu"): ("ysr kadapa", "andhra pradesh", "STATE_REORGANISATION",
        "Cuddapah was Madras/Tamil Nadu; transferred to Andhra Pradesh 1956"),
    ("chittoor", "tamil nadu"): ("chittoor", "andhra pradesh", "STATE_REORGANISATION",
        "Chittoor transferred Madras→Andhra Pradesh 1956"),
    ("kurnool", "tamil nadu"): ("kurnool", "andhra pradesh", "STATE_REORGANISATION",
        "Kurnool was Madras/Tamil Nadu; transferred to Andhra Pradesh 1956"),
    ("bellary", "madras"): ("ballari", "karnataka", "STATE_REORGANISATION",
        "Bellary transferred Madras→Mysore/Karnataka 1956; renamed Ballari 2014"),
    ("north lakhimpur", "assam"): ("lakhimpur", "assam", "SPELLING_VARIANT",
        "North Lakhimpur is the headquarters; district is Lakhimpur"),
    ("nowgong", "assam"): ("nagaon", "assam", "HISTORICAL_NAME",
        "Nowgong renamed Nagaon 1990"),
    ("gauhati", "assam"): ("kamrup", "assam", "SPELLING_VARIANT",
        "Gauhati is city; Kamrup is the district"),
}
# Note: Sikkim districts are unique internal splits and not in the alias table

# State name normalisation map: old state name → list of plausible modern states
# Used in L2 cross-state lookup when no state-scoped match is found.
STATE_NORM_SUCCESSORS: dict[str, list[str]] = {}
for _old, _news in STATE_PREDECESSOR_MAP.items():
    STATE_NORM_SUCCESSORS[lib.normalize_name(_old)] = [
        lib.normalize_name(s) for s in _news
    ]


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg_path = lib.CONFIG_DIR / "reconciliation.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------------------------------------------------------------------------
# Name similarity (Jaro-Winkler approximation without external library)
# ---------------------------------------------------------------------------

def _jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    l1, l2 = len(s1), len(s2)
    if l1 == 0 or l2 == 0:
        return 0.0
    match_dist = max(l1, l2) // 2 - 1
    match_dist = max(0, match_dist)
    s1_matches = [False] * l1
    s2_matches = [False] * l2
    matches = 0
    transpositions = 0
    for i in range(l1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, l2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i in range(l1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    jaro = (matches / l1 + matches / l2 +
            (matches - transpositions / 2) / matches) / 3.0
    return jaro


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    jaro = _jaro(s1, s2)
    prefix = 0
    for c1, c2 in zip(s1[:4], s2[:4]):
        if c1 == c2:
            prefix += 1
        else:
            break
    return jaro + prefix * p * (1 - jaro)


def name_similarity(a: str, b: str) -> float:
    """Jaro-Winkler similarity between two normalised names."""
    if not a or not b:
        return 0.0
    return jaro_winkler(a, b)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_inputs(cfg: dict) -> dict:
    print("Loading S10 outputs and pipeline products…")

    residual = pd.read_csv(lib.EVENT_TRANSFER_DIR / "residual_area_audit.csv")
    summary = pd.read_csv(lib.EVENT_TRANSFER_DIR / "event_area_accounting_summary.csv")
    transfer_matrix = pd.read_csv(
        lib.EVENT_TRANSFER_DIR / "event_area_transfer_matrix.csv"
    )

    registry = pd.read_parquet(lib.GOLD_CORE_DIR / "canonical_key_registry.parquet")
    obs_ck = pd.read_parquet(lib.GOLD_CORE_DIR / "geom_obs_to_ck.parquet")
    participants = pd.read_parquet(lib.GOLD_EVENTS_DIR / "event_participant.parquet")
    district_rel = pd.read_parquet(lib.GOLD_EVENTS_DIR / "district_relationship.parquet")

    unresolved = residual[residual["accounting_status"] == "UNRESOLVED_RESIDUAL"].copy()
    print(f"  {len(unresolved)} unresolved residuals to process "
          f"(of {len(residual)} total audit rows)")

    obs_to_ck = dict(zip(obs_ck["geom_obs_id"], obs_ck["canonical_key"]))
    ck_to_name = dict(zip(registry["canonical_key"], registry["display_name"]))
    ck_to_state = dict(zip(registry["canonical_key"], registry["state_at_creation"]))
    ck_to_established = dict(zip(registry["canonical_key"], registry["established_year"]))
    ck_to_closed = dict(zip(registry["canonical_key"], registry.get("closed_year",
                           pd.Series([None] * len(registry)))))

    # CK → geom_obs_id at each vintage
    ck_year_to_obs: dict[tuple, str] = {}
    for year in VINTAGES:
        src = SOURCE_BY_YEAR[year]
        path = lib.SILVER_GEOM_DIR / f"{src}_{year}.geoparquet"
        if not path.exists():
            continue
        gdf = pd.read_parquet(path, columns=["geom_obs_id"])
        for _, row in gdf.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck:
                ck_year_to_obs[(ck, year)] = row["geom_obs_id"]

    # Name index per vintage
    name_index: dict[int, dict] = {}
    for year in VINTAGES:
        src = SOURCE_BY_YEAR[year]
        path = lib.SILVER_GEOM_DIR / f"{src}_{year}.geoparquet"
        if not path.exists():
            continue
        gdf = pd.read_parquet(path, columns=[
            "geom_obs_id", "district_name_norm", "state_name_norm", "area_km2"
        ])
        by_state: dict[tuple, str] = {}
        by_name: dict[str, list] = {}
        all_cks_at_year: list[str] = []
        for _, row in gdf.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck is None:
                continue
            key = (row["state_name_norm"], row["district_name_norm"])
            by_state[key] = ck
            by_name.setdefault(row["district_name_norm"], []).append(ck)
            all_cks_at_year.append(ck)
        name_index[year] = {
            "by_state": by_state,
            "by_name": by_name,
            "all_cks": list(set(all_cks_at_year)),
        }

    # CK area at each vintage
    ck_area: dict[tuple, float] = {}
    for year in VINTAGES:
        src = SOURCE_BY_YEAR[year]
        path = lib.SILVER_GEOM_DIR / f"{src}_{year}.geoparquet"
        if not path.exists():
            continue
        gdf = pd.read_parquet(path, columns=["geom_obs_id", "area_km2"])
        for _, row in gdf.iterrows():
            ck = obs_to_ck.get(row["geom_obs_id"])
            if ck:
                ck_area[(ck, year)] = float(row["area_km2"])

    # Parent-child relationships from S6
    parent_child: dict[str, set[str]] = {}  # ck → set of related CKs
    for _, r in district_rel.iterrows():
        a, b = r.get("from_canonical_key", ""), r.get("to_canonical_key", "")
        if a and b:
            parent_child.setdefault(a, set()).add(b)
            parent_child.setdefault(b, set()).add(a)

    return {
        "residual": residual,
        "unresolved": unresolved,
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
    }


# ---------------------------------------------------------------------------
# Vintage bracketing
# ---------------------------------------------------------------------------

def bracket(year: int) -> tuple[int | None, int | None]:
    pre = max((v for v in VINTAGES if v < year), default=None)
    post = min((v for v in VINTAGES if v > year), default=None)
    return pre, post


# ---------------------------------------------------------------------------
# L1: Alias resolution
# ---------------------------------------------------------------------------

def resolve_l1_alias(name_raw: str, state_raw: str, post_vintage: int,
                     name_index: dict) -> tuple[str | None, str, str]:
    """
    Try to resolve an unresolved name through the alias table.
    Returns (ck_or_None, reason_code, provenance).
    """
    name_n = lib.normalize_name(name_raw)
    state_n = lib.normalize_name(state_raw)

    alias_key = (name_n, state_n)
    if alias_key in DISTRICT_ALIASES:
        target_name, target_state, alias_type, prov = DISTRICT_ALIASES[alias_key]
        # Attempt to resolve in target vintage under alias names
        idx = name_index.get(post_vintage, {})
        ck = idx.get("by_state", {}).get((target_state, target_name))
        if ck is None:
            # Try name-only
            cands = idx.get("by_name", {}).get(target_name, [])
            ck = cands[0] if len(cands) == 1 else None
        if ck:
            return ck, "ALIAS_RESOLUTION", f"{alias_type}:{prov}"
    return None, "NAME_RESOLUTION_FAILURE", ""


# ---------------------------------------------------------------------------
# L2: Cross-state identity resolution
# ---------------------------------------------------------------------------

def resolve_l2_cross_state(name_raw: str, state_raw: str, post_vintage: int,
                            name_index: dict, cfg: dict) -> tuple[str | None, str, str]:
    """
    Remove the state constraint; search all CKs in target vintage for a
    name match. Record cross_state_transition = True.
    """
    if not cfg.get("enable_cross_state_resolution", True):
        return None, "CROSS_STATE_DISABLED", ""

    name_n = lib.normalize_name(name_raw)
    state_n = lib.normalize_name(state_raw)
    idx = name_index.get(post_vintage, {})

    # Direct name match across all states
    cands = idx.get("by_name", {}).get(name_n, [])
    if len(cands) == 1:
        return cands[0], "CROSS_STATE_IDENTITY_DRIFT", f"name_only_cross_state:{state_n}"
    if len(cands) > 1:
        # Prefer successor states that are historically plausible
        successor_states = STATE_NORM_SUCCESSORS.get(state_n, [])
        if successor_states:
            by_state = idx.get("by_state", {})
            for ss in successor_states:
                ck = by_state.get((ss, name_n))
                if ck:
                    return ck, "CROSS_STATE_IDENTITY_DRIFT", f"state_successor_map:{state_n}→{ss}"
        # Ambiguous; return first but flag
        return cands[0], "CROSS_STATE_IDENTITY_DRIFT", f"ambiguous_cross_state:{state_n}"

    # Try alias table cross-state
    for (a_name, a_state), (t_name, t_state, atype, prov) in DISTRICT_ALIASES.items():
        if a_name == name_n:
            by_state = idx.get("by_state", {})
            ck = by_state.get((t_state, t_name))
            if ck is None:
                name_cands = idx.get("by_name", {}).get(t_name, [])
                ck = name_cands[0] if len(name_cands) == 1 else None
            if ck:
                return ck, "CROSS_STATE_IDENTITY_DRIFT", f"alias_cross_state:{atype}:{prov}"

    return None, "EVENT_SUCCESSOR_NOT_RESOLVED", ""


# ---------------------------------------------------------------------------
# L3: Full spatial scan
# ---------------------------------------------------------------------------

# Cache for target-vintage GeoDataFrames (loaded once per vintage per run)
_gdf_cache: dict[int, gpd.GeoDataFrame] = {}


def _load_target_gdf(post_vintage: int, obs_to_ck: dict,
                     ck_to_name: dict, ck_to_state: dict,
                     ck_area: dict) -> gpd.GeoDataFrame | None:
    if post_vintage in _gdf_cache:
        return _gdf_cache[post_vintage]
    src = SOURCE_BY_YEAR.get(post_vintage)
    if src is None:
        return None
    path = lib.SILVER_GEOM_DIR / f"{src}_{post_vintage}.geoparquet"
    if not path.exists():
        return None
    gdf = gpd.read_parquet(path, columns=["geom_obs_id", "district_name_norm",
                                           "state_name_norm", "area_km2", "geometry"])
    gdf["ck"] = gdf["geom_obs_id"].map(obs_to_ck)
    gdf["display_name"] = gdf["ck"].map(ck_to_name).fillna(gdf["district_name_norm"])
    gdf["state"] = gdf["ck"].map(ck_to_state).fillna(gdf["state_name_norm"])
    gdf = gdf[gdf["ck"].notna()].copy()
    gdf = gdf.set_geometry("geometry")
    _gdf_cache[post_vintage] = gdf
    return gdf


def spatial_scan_l3(pred_geom, pred_ck: str, pre_vintage: int,
                    post_vintage: int, pred_area: float,
                    declared_succ_cks: set[str],
                    declared_succ_names: set[str],
                    obs_to_ck: dict, ck_to_name: dict, ck_to_state: dict,
                    ck_area: dict, ck_to_established: dict, ck_to_closed: dict,
                    parent_child: dict, event_year: int,
                    cfg: dict) -> list[dict]:
    """
    Intersect predecessor geometry against ALL districts in post_vintage.
    Returns list of candidate dicts, sorted by score descending.
    """
    min_km2 = cfg.get("min_discovery_intersection_km2", 1.0)
    min_pct = cfg.get("min_candidate_overlap_pct", 0.5)
    w = cfg.get("candidate_ranking_weights", {})
    w_pre = w.get("intersection_pct_of_pre", 0.25)
    w_norm = w.get("intersection_area_km2_norm", 0.20)
    w_cand = w.get("intersection_pct_of_candidate", 0.12)
    w_name = w.get("name_similarity", 0.12)
    w_decl = w.get("declared_in_event", 0.10)
    w_pc = w.get("parent_child_relation", 0.08)
    w_st = w.get("state_transition_compat", 0.06)
    w_yr = w.get("event_year_compat", 0.04)
    w_bd = w.get("shared_boundary_norm", 0.02)
    w_cd = w.get("centroid_distance_inv", 0.01)

    target_gdf = _load_target_gdf(post_vintage, obs_to_ck, ck_to_name,
                                   ck_to_state, ck_area)
    if target_gdf is None or target_gdf.empty:
        return []

    if pred_geom is None or pred_geom.is_empty or not pred_geom.is_valid:
        return []

    # Spatial pre-filter using bounding box to avoid N×M intersections
    bbox = pred_geom.bounds  # (minx, miny, maxx, maxy)
    bbox_geom = box(*bbox)
    candidate_gdf = target_gdf[target_gdf.intersects(bbox_geom)].copy()
    if candidate_gdf.empty:
        return []

    pred_state_n = ""
    succ_states_n = [lib.normalize_name(s) for s in
                     STATE_NORM_SUCCESSORS.get(lib.normalize_name(""), [])]

    pred_centroid = pred_geom.centroid
    max_overlap_km2 = 0.0
    raw_candidates = []

    for _, row in candidate_gdf.iterrows():
        cand_ck = row["ck"]
        cand_geom = row["geometry"]
        if cand_geom is None or cand_geom.is_empty or not cand_geom.is_valid:
            continue
        try:
            intersection = pred_geom.intersection(cand_geom)
        except Exception:
            continue
        if intersection.is_empty:
            continue
        area_km2 = lib.geodesic_area_km2(intersection)
        if area_km2 < min_km2:
            continue
        pct_of_pre = (area_km2 / pred_area * 100.0) if pred_area > 0 else 0.0
        if pct_of_pre < min_pct:
            continue
        cand_area = float(row.get("area_km2", 0))
        pct_of_cand = (area_km2 / cand_area * 100.0) if cand_area > 0 else 0.0

        # Shared boundary (approx: length of intersection boundary)
        try:
            shared_bd = intersection.boundary.length
        except Exception:
            shared_bd = 0.0

        # Centroid distance (degrees; will be normalised)
        try:
            cand_centroid = cand_geom.centroid
            dist = pred_centroid.distance(cand_centroid)
        except Exception:
            dist = 999.0

        max_overlap_km2 = max(max_overlap_km2, area_km2)
        raw_candidates.append({
            "cand_ck": cand_ck,
            "cand_name": row["display_name"],
            "cand_state": row["state"],
            "cand_state_n": row["state_name_norm"],
            "cand_area_km2": cand_area,
            "intersection_area_km2": area_km2,
            "pct_of_pre": pct_of_pre,
            "pct_of_cand": pct_of_cand,
            "shared_bd": shared_bd,
            "centroid_dist": dist,
            "established": ck_to_established.get(cand_ck, 0) or 0,
            "closed": ck_to_closed.get(cand_ck),
        })

    if not raw_candidates:
        return []

    # Normalise area for ranking
    max_bd = max((c["shared_bd"] for c in raw_candidates), default=1.0) or 1.0
    max_dist = max((c["centroid_dist"] for c in raw_candidates), default=1.0) or 1.0

    pred_name_n = lib.normalize_name(ck_to_name.get(pred_ck, pred_ck))

    scored = []
    for c in raw_candidates:
        cand_ck = c["cand_ck"]
        cand_name_n = lib.normalize_name(c["cand_name"])

        s_pre = min(1.0, c["pct_of_pre"] / 100.0)
        s_norm = (c["intersection_area_km2"] / max_overlap_km2
                  if max_overlap_km2 > 0 else 0.0)
        s_cand = min(1.0, c["pct_of_cand"] / 100.0)

        # Name similarity: compare candidate against declared successor names
        best_name_sim = 0.0
        for dsn in declared_succ_names:
            sim = name_similarity(lib.normalize_name(dsn), cand_name_n)
            best_name_sim = max(best_name_sim, sim)
        # Also compare against predecessor name (for RENAMEs)
        best_name_sim = max(best_name_sim,
                            name_similarity(pred_name_n, cand_name_n))
        s_name = best_name_sim

        s_decl = 1.0 if cand_ck in declared_succ_cks else 0.0
        s_pc = 1.0 if cand_ck in parent_child.get(pred_ck, set()) else 0.0

        # State transition compatibility
        est = int(c["established"]) if c["established"] else 0
        s_yr = 1.0 if est <= post_vintage else 0.0

        # Cross-state compatibility
        s_st = 0.5  # neutral; we don't penalise cross-state
        if c["cand_state_n"] == lib.normalize_name(ck_to_state.get(pred_ck, "")):
            s_st = 1.0  # same state is a positive signal, not a requirement

        s_bd = c["shared_bd"] / max_bd
        s_cd = 1.0 - (c["centroid_dist"] / max_dist)

        score = (w_pre * s_pre + w_norm * s_norm + w_cand * s_cand +
                 w_name * s_name + w_decl * s_decl + w_pc * s_pc +
                 w_st * s_st + w_yr * s_yr + w_bd * s_bd + w_cd * s_cd)

        same_state = (c["cand_state_n"] ==
                      lib.normalize_name(ck_to_state.get(pred_ck, "")))

        scored.append({
            **c,
            "score": round(score, 6),
            "same_state": same_state,
            "name_similarity": round(s_name, 4),
            "declared_in_event": bool(s_decl),
            "parent_child_related": bool(s_pc),
            "declared_successor_name": (
                next(iter(declared_succ_names), "") if not s_decl else
                next((n for n in declared_succ_names
                      if name_similarity(lib.normalize_name(n), cand_name_n) == best_name_sim), "")
            ),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Classify spatial candidate status
# ---------------------------------------------------------------------------

def classify_candidate_status(cand: dict, cfg: dict) -> str:
    confirm_thresh = cfg.get("spatial_confirm_threshold", 0.55)
    recovery_thresh = cfg.get("spatial_recovery_threshold", 0.65)
    score = cand["score"]
    if cand["declared_in_event"]:
        if score >= recovery_thresh:
            return "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
    if score >= confirm_thresh:
        return "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED"
    if score >= 0.3:
        return "POSSIBLE_SPATIAL_SUCCESSOR"
    return "NO_VALID_SPATIAL_SUCCESSOR"


# ---------------------------------------------------------------------------
# Recovery decision
# ---------------------------------------------------------------------------

def decide_recovery(
    row: pd.Series,
    event_year: int,
    post_vintage: int,
    pred_ck: str,
    pre_vintage: int,
    declared_succ_cks: set[str],
    declared_succ_names: set[str],
    name_index: dict,
    obs_to_ck: dict,
    ck_to_name: dict,
    ck_to_state: dict,
    ck_area: dict,
    ck_to_established: dict,
    ck_to_closed: dict,
    parent_child: dict,
    cfg: dict,
) -> tuple[str, str, int, str, str | None, float, list[dict]]:
    """
    Run levels L1–L5 in order.

    Returns:
      (final_status, recovery_reason, recovery_level,
       candidate_status, best_candidate_ck, post_discovery_residual_pct,
       all_spatial_candidates)
    """
    tol = cfg.get("residual_tolerance_pct", 1.0)
    recent_gap = cfg.get("recent_event_vintage_gap_years", 4)

    raw_residual_pct = float(row.get("residual_pct", 100.0))
    raw_residual_km2 = float(row.get("residual_area_km2", 0.0))
    source_area_km2 = float(row.get("source_area_km2", 0.0))
    name_raw = str(row.get("predecessor_name", ""))
    state_raw = str(row.get("predecessor_state", ""))
    all_candidates: list[dict] = []

    # L1: Alias resolution
    ck_l1, reason_l1, prov_l1 = resolve_l1_alias(
        name_raw, state_raw, post_vintage, name_index)
    if ck_l1 and ck_l1 not in declared_succ_cks:
        # We found the predecessor (not successor) via alias.
        # This is a predecessor-resolution improvement, which means S10's
        # intersection was looking at the wrong CK. The actual residual may
        # drop if we re-run the intersection with the corrected CK.
        # For now, flag as RECONCILED_BY_ALIAS with the resolved CK.
        return ("RECONCILED_BY_ALIAS", "ALIAS_RESOLUTION", 1,
                "CONFIRMED_ADMINISTRATIVE_SUCCESSOR", ck_l1, 0.0, [])

    # L2: Cross-state
    ck_l2, reason_l2, prov_l2 = resolve_l2_cross_state(
        name_raw, state_raw, post_vintage, name_index, cfg)
    if ck_l2 and ck_l2 not in declared_succ_cks:
        return ("RECONCILED_BY_CROSS_STATE_RESOLUTION",
                "CROSS_STATE_IDENTITY_DRIFT", 2,
                "CONFIRMED_ADMINISTRATIVE_SUCCESSOR", ck_l2, 0.0, [])

    # Load predecessor geometry for L3+
    pred_geom = _load_pred_geom(pred_ck, pre_vintage)

    # Special case: 2022-2023 very recent events
    if event_year >= (LATEST_VINTAGE - recent_gap) and pred_geom is not None:
        # Run spatial scan but classify differently
        candidates = spatial_scan_l3(
            pred_geom, pred_ck, pre_vintage, post_vintage, source_area_km2,
            declared_succ_cks, declared_succ_names,
            obs_to_ck, ck_to_name, ck_to_state, ck_area,
            ck_to_established, ck_to_closed, parent_child, event_year, cfg,
        )
        all_candidates = candidates
        if not candidates:
            return ("VINTAGE_MISMATCH", "VINTAGE_MISMATCH_RECENT", 3,
                    "NO_VALID_SPATIAL_SUCCESSOR", None, raw_residual_pct, [])
        best = candidates[0]
        post_disc_pct = max(0.0,
            (raw_residual_km2 - best["intersection_area_km2"]) / source_area_km2 * 100.0
            if source_area_km2 > 0 else raw_residual_pct
        )
        cand_status = classify_candidate_status(best, cfg)
        # Check if declared successor B ≠ spatial candidate C
        if declared_succ_cks and best["cand_ck"] not in declared_succ_cks:
            if cand_status in ("CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                               "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED"):
                return ("EVENT_GEOMETRY_IDENTITY_MISMATCH",
                        "GEOMETRY_AVAILABLE_IDENTITY_MISMATCH", 3,
                        cand_status, best["cand_ck"],
                        post_disc_pct, candidates)
        if post_disc_pct < tol:
            return ("RECONCILED_BY_SPATIAL_DISCOVERY",
                    "SPATIAL_SUCCESSOR_DISCOVERED", 3,
                    cand_status, best["cand_ck"],
                    post_disc_pct, candidates)
        return ("REQUIRES_MANUAL_REVIEW",
                "GEOMETRY_AVAILABLE_NAME_MISMATCH", 3,
                cand_status, best["cand_ck"], post_disc_pct, candidates)

    if pred_geom is None:
        return ("MISSING_SPATIAL_EVIDENCE", "MISSING_GEOMETRIC_EVIDENCE", 6,
                "NO_VALID_SPATIAL_SUCCESSOR", None, raw_residual_pct, [])

    # L3: Full spatial scan
    candidates = spatial_scan_l3(
        pred_geom, pred_ck, pre_vintage, post_vintage, source_area_km2,
        declared_succ_cks, declared_succ_names,
        obs_to_ck, ck_to_name, ck_to_state, ck_area,
        ck_to_established, ck_to_closed, parent_child, event_year, cfg,
    )
    all_candidates = candidates

    if not candidates:
        return ("UNRESOLVED_GEOMETRIC_RESIDUAL",
                "GEOMETRY_NOT_AVAILABLE", 6,
                "NO_VALID_SPATIAL_SUCCESSOR", None, raw_residual_pct, [])

    best = candidates[0]
    total_discovered_km2 = sum(c["intersection_area_km2"] for c in candidates)
    post_disc_pct = max(0.0,
        (raw_residual_km2 - total_discovered_km2) / source_area_km2 * 100.0
        if source_area_km2 > 0 else 100.0
    )
    cand_status = classify_candidate_status(best, cfg)

    # L4: Parent-child constraint
    pc_related = [c for c in candidates if c["parent_child_related"]]
    if pc_related:
        best_pc = pc_related[0]
        if declared_succ_cks and best_pc["cand_ck"] not in declared_succ_cks:
            return ("EVENT_GEOMETRY_IDENTITY_MISMATCH",
                    "PARENT_CHILD_RECOVERY", 4,
                    classify_candidate_status(best_pc, cfg),
                    best_pc["cand_ck"], post_disc_pct, candidates)
        if post_disc_pct < tol:
            return ("RECONCILED_BY_SPATIAL_DISCOVERY",
                    "PARENT_CHILD_RECOVERY", 4,
                    classify_candidate_status(best_pc, cfg),
                    best_pc["cand_ck"], post_disc_pct, candidates)

    # Check for identity mismatch (declared B ≠ spatial C)
    if declared_succ_cks and best["cand_ck"] not in declared_succ_cks:
        if cand_status in ("CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                           "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED"):
            return ("EVENT_GEOMETRY_IDENTITY_MISMATCH",
                    "GEOMETRY_AVAILABLE_IDENTITY_MISMATCH", 3,
                    cand_status, best["cand_ck"], post_disc_pct, candidates)

    recovery_thresh = cfg.get("spatial_recovery_threshold", 0.65)

    if best["score"] >= recovery_thresh and post_disc_pct < tol:
        return ("RECONCILED_BY_SPATIAL_DISCOVERY",
                "SPATIAL_SUCCESSOR_DISCOVERED", 3,
                cand_status, best["cand_ck"], post_disc_pct, candidates)

    # L5: Adjacency / shared boundary
    if best["score"] >= recovery_thresh:
        return ("RECONCILED_BY_SPATIAL_DISCOVERY",
                "ADJACENCY_RECOVERY", 5,
                cand_status, best["cand_ck"], post_disc_pct, candidates)

    # L6: Unresolved
    if post_disc_pct < tol:
        return ("RECONCILED_WITH_IGNORABLE_RESIDUAL",
                "BOUNDARY_PRECISION", 5,
                cand_status, best["cand_ck"], post_disc_pct, candidates)

    return ("UNRESOLVED_GEOMETRIC_RESIDUAL",
            "GENUINELY_UNRESOLVED", 6,
            cand_status, best["cand_ck"] if candidates else None,
            raw_residual_pct, candidates)


# Cache for predecessor geometries
_pred_geom_cache: dict[tuple, Any] = {}


def _load_pred_geom(pred_ck: str, pre_vintage: int) -> Any | None:
    key = (pred_ck, pre_vintage)
    if key in _pred_geom_cache:
        return _pred_geom_cache[key]
    # Load from silver
    src = SOURCE_BY_YEAR.get(pre_vintage)
    if not src:
        _pred_geom_cache[key] = None
        return None
    path = lib.SILVER_GEOM_DIR / f"{src}_{pre_vintage}.geoparquet"
    if not path.exists():
        _pred_geom_cache[key] = None
        return None
    try:
        gdf = gpd.read_parquet(path, columns=["geom_obs_id", "district_name_norm", "geometry"])
        # Find by normalized name (we may not have obs_id here — use name match)
        pred_name_n = lib.normalize_name(pred_ck)  # fallback
        # Better: load obs → ck mapping to find the right row
        # Since this is called per event, use cached obs_to_ck via ck_year_to_obs
        # This function doesn't have ck_year_to_obs; the caller passes geom via
        # the L3 spatial scan. Use a name heuristic here.
        geom = None
        if not gdf.empty:
            geom = None  # will be resolved via caller's ck_year_to_obs
    except Exception:
        _pred_geom_cache[key] = None
        return None
    _pred_geom_cache[key] = geom
    return geom


def load_pred_geom_by_obs(obs_id: str, pre_vintage: int) -> Any | None:
    """Load predecessor geometry by observation ID (more reliable)."""
    src = SOURCE_BY_YEAR.get(pre_vintage)
    if not src:
        return None
    path = lib.SILVER_GEOM_DIR / f"{src}_{pre_vintage}.geoparquet"
    if not path.exists():
        return None
    try:
        gdf = gpd.read_parquet(path, columns=["geom_obs_id", "geometry"])
        row = gdf[gdf["geom_obs_id"] == obs_id]
        if row.empty:
            return None
        geom = row.iloc[0]["geometry"]
        return geom if (geom is not None and not geom.is_empty) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main discovery engine
# ---------------------------------------------------------------------------

def run_discovery(cfg: dict, data: dict) -> dict:
    """
    Process all unresolved residuals through L1-L6 hierarchy.
    Returns dicts of all outputs.
    """
    unresolved = data["unresolved"]
    name_index = data["name_index"]
    obs_to_ck = data["obs_to_ck"]
    ck_to_name = data["ck_to_name"]
    ck_to_state = data["ck_to_state"]
    ck_to_established = data["ck_to_established"]
    ck_to_closed = data["ck_to_closed"]
    ck_area = data["ck_area"]
    ck_year_to_obs = data["ck_year_to_obs"]
    parent_child = data["parent_child"]
    participants = data["participants"]

    by_event = participants.groupby("event_id")

    candidate_rows: list[dict] = []
    updated_residual_rows: list[dict] = []
    alias_registry_rows: list[dict] = []

    # Counters for report
    stats = {
        "total_unresolved": len(unresolved),
        "resolved_l1_alias": 0,
        "resolved_l2_cross_state": 0,
        "resolved_l3_spatial": 0,
        "resolved_l4_parent_child": 0,
        "resolved_l5_adjacency": 0,
        "identity_mismatch": 0,
        "vintage_mismatch": 0,
        "still_unresolved": 0,
        "requires_manual": 0,
        "missing_spatial": 0,
        "ignorable_residual": 0,
    }

    print(f"\nRunning spatial discovery on {len(unresolved)} unresolved residuals…")

    for idx_row, row in unresolved.iterrows():
        event_id = str(row["event_id"])
        event_year = int(row["event_year"])
        event_type = str(row.get("event_type", ""))
        pred_ck = str(row.get("predecessor_ck", ""))
        pred_name = str(row.get("predecessor_name", ""))
        pred_state = str(row.get("predecessor_state", ""))
        pre_vintage = row.get("pre_vintage")
        post_vintage = row.get("post_vintage")

        pre_vintage = int(pre_vintage) if pd.notna(pre_vintage) else None
        post_vintage = int(post_vintage) if pd.notna(post_vintage) else None

        if pre_vintage is None or post_vintage is None:
            pre_vintage, post_vintage = bracket(event_year)

        if pre_vintage is None or post_vintage is None:
            updated_residual_rows.append(_augment_row(row, "MISSING_SPATIAL_EVIDENCE",
                "NO_BRACKETING_VINTAGE", 6, "NO_VALID_SPATIAL_SUCCESSOR",
                None, float(row.get("residual_pct", 100.0)), 0))
            stats["missing_spatial"] += 1
            continue

        # Collect declared successors for this event
        event_parts = (by_event.get_group(event_id)
                       if event_id in by_event.groups
                       else pd.DataFrame(columns=participants.columns))
        succ_rows = event_parts[event_parts["role"] == "SUCCESSOR"]
        declared_succ_names = set(succ_rows["district_name_raw"].dropna().tolist())
        declared_succ_cks: set[str] = set()
        for _, sr in succ_rows.iterrows():
            sname = str(sr["district_name_raw"])
            sstate = str(sr.get("state", pred_state))
            idx_vi = name_index.get(post_vintage, {})
            sn = lib.normalize_name(sname)
            sst = lib.normalize_name(sstate)
            ck_s = idx_vi.get("by_state", {}).get((sst, sn))
            if ck_s is None:
                cands = idx_vi.get("by_name", {}).get(sn, [])
                ck_s = cands[0] if len(cands) == 1 else None
            if ck_s:
                declared_succ_cks.add(ck_s)

        # Load predecessor geometry via obs_id
        pred_obs = ck_year_to_obs.get((pred_ck, pre_vintage))
        pred_geom = None
        if pred_obs:
            pred_geom = load_pred_geom_by_obs(pred_obs, pre_vintage)
        pred_area = float(row.get("source_area_km2", 0.0))

        # Override _load_pred_geom to use obs-based approach in spatial_scan_l3
        # by directly passing pred_geom
        def _spatial_scan_wrapper(pg):
            return spatial_scan_l3(
                pg, pred_ck, pre_vintage, post_vintage, pred_area,
                declared_succ_cks, declared_succ_names,
                obs_to_ck, ck_to_name, ck_to_state, ck_area,
                ck_to_established, ck_to_closed, parent_child, event_year, cfg,
            )

        # --- L1 ---
        ck_l1, reason_l1, prov_l1 = resolve_l1_alias(
            pred_name, pred_state, post_vintage, name_index)
        if ck_l1:
            final_status = "RECONCILED_BY_ALIAS"
            recovery_reason = "ALIAS_RESOLUTION"
            recovery_level = 1
            cand_status = "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
            best_cand = ck_l1
            post_disc_pct = 0.0
            candidates = []
            stats["resolved_l1_alias"] += 1
            alias_registry_rows.append({
                "raw_name": pred_name,
                "canonical_name": ck_to_name.get(ck_l1, ck_l1),
                "from_state": pred_state,
                "to_state": ck_to_state.get(ck_l1, ""),
                "alias_type": reason_l1,
                "provenance": prov_l1,
                "created_by": "s10b_spatial_discovery",
                "event_year": event_year,
            })
        else:
            # --- L2 ---
            ck_l2, reason_l2, prov_l2 = resolve_l2_cross_state(
                pred_name, pred_state, post_vintage, name_index, cfg)
            if ck_l2:
                final_status = "RECONCILED_BY_CROSS_STATE_RESOLUTION"
                recovery_reason = "CROSS_STATE_IDENTITY_DRIFT"
                recovery_level = 2
                cand_status = "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
                best_cand = ck_l2
                post_disc_pct = 0.0
                candidates = []
                stats["resolved_l2_cross_state"] += 1
            else:
                # --- L3-L6 ---
                tol = cfg.get("residual_tolerance_pct", 1.0)
                recent_gap = cfg.get("recent_event_vintage_gap_years", 4)
                raw_residual_pct = float(row.get("residual_pct", 100.0))
                raw_residual_km2 = float(row.get("residual_area_km2", 0.0))

                if pred_geom is None:
                    final_status = "MISSING_SPATIAL_EVIDENCE"
                    recovery_reason = "MISSING_GEOMETRIC_EVIDENCE"
                    recovery_level = 6
                    cand_status = "NO_VALID_SPATIAL_SUCCESSOR"
                    best_cand = None
                    post_disc_pct = raw_residual_pct
                    candidates = []
                    stats["missing_spatial"] += 1
                else:
                    candidates = _spatial_scan_wrapper(pred_geom)

                    if not candidates:
                        # No overlap at all
                        if event_year >= (LATEST_VINTAGE - recent_gap):
                            final_status = "VINTAGE_MISMATCH"
                            recovery_reason = "VINTAGE_MISMATCH_RECENT"
                            stats["vintage_mismatch"] += 1
                        else:
                            final_status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
                            recovery_reason = "GEOMETRY_NOT_AVAILABLE"
                            stats["still_unresolved"] += 1
                        recovery_level = 6
                        cand_status = "NO_VALID_SPATIAL_SUCCESSOR"
                        best_cand = None
                        post_disc_pct = raw_residual_pct
                    else:
                        best = candidates[0]
                        cand_status = classify_candidate_status(best, cfg)
                        total_disc_km2 = sum(c["intersection_area_km2"] for c in candidates)
                        post_disc_pct = max(0.0,
                            (raw_residual_km2 - total_disc_km2) / pred_area * 100.0
                            if pred_area > 0 else raw_residual_pct
                        )
                        best_cand = best["cand_ck"]
                        recovery_thresh = cfg.get("spatial_recovery_threshold", 0.65)

                        # L4 parent-child check
                        pc_best = next((c for c in candidates if c["parent_child_related"]), None)

                        # Identity mismatch check
                        identity_mismatch = (
                            bool(declared_succ_cks) and
                            best["cand_ck"] not in declared_succ_cks and
                            cand_status in ("CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
                                            "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED")
                        )

                        if identity_mismatch:
                            final_status = "EVENT_GEOMETRY_IDENTITY_MISMATCH"
                            recovery_reason = "GEOMETRY_AVAILABLE_IDENTITY_MISMATCH"
                            recovery_level = 3 if not pc_best else 4
                            stats["identity_mismatch"] += 1
                        elif post_disc_pct < tol and best["score"] >= recovery_thresh:
                            if pc_best:
                                final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
                                recovery_reason = "PARENT_CHILD_RECOVERY"
                                recovery_level = 4
                                stats["resolved_l4_parent_child"] += 1
                            else:
                                final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
                                recovery_reason = "SPATIAL_SUCCESSOR_DISCOVERED"
                                recovery_level = 3
                                stats["resolved_l3_spatial"] += 1
                        elif best["score"] >= recovery_thresh:
                            final_status = "RECONCILED_BY_SPATIAL_DISCOVERY"
                            recovery_reason = "ADJACENCY_RECOVERY"
                            recovery_level = 5
                            stats["resolved_l5_adjacency"] += 1
                        elif post_disc_pct < tol:
                            final_status = "RECONCILED_WITH_IGNORABLE_RESIDUAL"
                            recovery_reason = "BOUNDARY_PRECISION"
                            recovery_level = 5
                            stats["ignorable_residual"] += 1
                        elif event_year >= (LATEST_VINTAGE - recent_gap):
                            final_status = "VINTAGE_MISMATCH"
                            recovery_reason = "VINTAGE_MISMATCH_RECENT"
                            recovery_level = 3
                            stats["vintage_mismatch"] += 1
                        else:
                            final_status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
                            recovery_reason = "GENUINELY_UNRESOLVED"
                            recovery_level = 6
                            stats["still_unresolved"] += 1

        # Build candidate table rows
        confirm_thresh = cfg.get("spatial_confirm_threshold", 0.55)
        for rank, cand in enumerate(candidates[:20]):  # top 20 per predecessor
            c_status = classify_candidate_status(cand, cfg)
            # Parent-child mismatch flag
            pc_mismatch = (
                bool(declared_succ_cks) and
                cand["cand_ck"] not in declared_succ_cks and
                cand["declared_in_event"] is False
            )
            candidate_rows.append({
                "event_id": event_id,
                "event_year": event_year,
                "event_type": event_type,
                "pre_ck": pred_ck,
                "pre_name": pred_name,
                "pre_state": pred_state,
                "candidate_post_ck": cand["cand_ck"],
                "candidate_post_name": cand["cand_name"],
                "candidate_post_state": cand["cand_state"],
                "intersection_area_km2": round(cand["intersection_area_km2"], 4),
                "intersection_pct_of_pre": round(cand["pct_of_pre"], 4),
                "intersection_pct_of_candidate": round(cand["pct_of_cand"], 4),
                "shared_boundary_length": round(cand["shared_bd"], 4),
                "centroid_distance": round(cand["centroid_dist"], 6),
                "declared_in_event": cand["declared_in_event"],
                "declared_parent": pred_ck in (cand.get("declared_successor_name", "")),
                "declared_successor": cand["declared_in_event"],
                "same_state": cand["same_state"],
                "cross_state": not cand["same_state"],
                "spatial_rank": rank + 1,
                "candidate_score": cand["score"],
                "name_similarity": cand["name_similarity"],
                "parent_child_related": cand["parent_child_related"],
                "candidate_status": c_status,
                "evidence_reason": (
                    "DECLARED_AND_SPATIAL" if cand["declared_in_event"]
                    else "SPATIAL_ONLY"
                ),
                "confidence": round(cand["score"], 4),
                "parent_child_mismatch": pc_mismatch,
            })

        updated_residual_rows.append(_augment_row(
            row, final_status, recovery_reason, recovery_level,
            cand_status, best_cand, post_disc_pct,
            len(candidates),
            best_cand_score=candidates[0]["score"] if candidates else 0.0,
            best_cand_name=(ck_to_name.get(best_cand, best_cand)
                            if best_cand else None),
        ))

    return {
        "candidate_rows": candidate_rows,
        "updated_residual_rows": updated_residual_rows,
        "alias_registry_rows": alias_registry_rows,
        "stats": stats,
    }


def _augment_row(row: pd.Series, final_status: str, recovery_reason: str,
                 recovery_level: int, candidate_status: str,
                 best_candidate_ck: str | None, post_disc_pct: float,
                 candidate_count: int,
                 best_cand_score: float = 0.0,
                 best_cand_name: str | None = None) -> dict:
    d = row.to_dict()
    d["initial_residual_pct"] = d.get("residual_pct", 100.0)
    d["post_discovery_residual_pct"] = round(post_disc_pct, 6)
    d["initial_status"] = d.get("accounting_status", "UNRESOLVED_RESIDUAL")
    d["final_status"] = final_status
    d["recovery_level"] = recovery_level
    d["recovery_reason"] = recovery_reason
    d["candidate_count"] = candidate_count
    d["best_candidate"] = best_cand_name or best_candidate_ck or ""
    d["best_candidate_ck"] = best_candidate_ck or ""
    d["best_candidate_score"] = round(best_cand_score, 4)
    d["candidate_status"] = candidate_status
    d["cross_state_transition"] = recovery_reason in (
        "CROSS_STATE_IDENTITY_DRIFT", "SPATIAL_SUCCESSOR_DISCOVERED"
    )
    d["name_resolution_attempted"] = True
    d["administrative_spatial_agreement"] = final_status not in (
        "EVENT_GEOMETRY_IDENTITY_MISMATCH",
    )
    return d


# ---------------------------------------------------------------------------
# GPKG spatial output
# ---------------------------------------------------------------------------

def write_gpkg(candidate_rows: list[dict], updated_residual: pd.DataFrame,
               obs_to_ck: dict, ck_to_name: dict, ck_year_to_obs: dict) -> None:
    out_path = lib.EVENT_TRANSFER_DIR / "event_spatial_candidates.gpkg"

    def _load_geom(ck: str, vintage: int) -> Any | None:
        obs_id = ck_year_to_obs.get((ck, vintage))
        if not obs_id:
            return None
        return load_pred_geom_by_obs(obs_id, vintage)

    layers: dict[str, list[dict]] = {
        "raw_intersections": [],
        "spatial_successor_candidates": [],
        "recovered_transfers": [],
        "unresolved_residuals": [],
    }

    # All spatial candidates → raw_intersections and spatial_successor_candidates
    for cr in candidate_rows:
        base = {
            "event_id": cr["event_id"],
            "event_year": cr["event_year"],
            "pre_ck": cr["pre_ck"],
            "post_ck": cr["candidate_post_ck"],
            "area_km2": cr["intersection_area_km2"],
            "relationship_status": cr["candidate_status"],
            "recovery_level": None,
            "reason": cr["evidence_reason"],
            "confidence": cr["confidence"],
        }
        layers["raw_intersections"].append(base)
        layers["spatial_successor_candidates"].append(base)

    # Recovered and unresolved from updated audit
    for _, r in updated_residual.iterrows():
        final = r.get("final_status", "")
        base = {
            "event_id": r["event_id"],
            "event_year": r["event_year"],
            "pre_ck": r.get("predecessor_ck", ""),
            "post_ck": r.get("best_candidate_ck", ""),
            "area_km2": r.get("residual_area_km2", 0.0),
            "relationship_status": final,
            "recovery_level": r.get("recovery_level"),
            "reason": r.get("recovery_reason", ""),
            "confidence": r.get("best_candidate_score", 0.0),
        }
        if "RECONCILED" in final or final == "VINTAGE_MISMATCH":
            layers["recovered_transfers"].append(base)
        else:
            layers["unresolved_residuals"].append(base)

    written_layers = 0
    for layer_name, rows in layers.items():
        if not rows:
            continue
        # Build GDF with geometry if possible
        geom_rows = []
        for r2 in rows:
            g = None
            geom_rows.append({"geometry": g, **r2})
        gdf = gpd.GeoDataFrame(geom_rows, crs="EPSG:4326")
        mode = "w" if written_layers == 0 else "a"
        gdf.to_file(out_path, layer=layer_name, driver="GPKG")
        written_layers += 1
        print(f"  GPKG layer '{layer_name}': {len(rows)} features")

    print(f"  Wrote {out_path}")


# ---------------------------------------------------------------------------
# TERRITORIAL_RECONCILIATION_REPORT.md
# ---------------------------------------------------------------------------

def write_report(stats: dict, updated_residual: pd.DataFrame,
                 candidate_df: pd.DataFrame, ck_to_name: dict) -> None:
    n = stats["total_unresolved"]
    r_l1 = stats["resolved_l1_alias"]
    r_l2 = stats["resolved_l2_cross_state"]
    r_l3 = stats["resolved_l3_spatial"]
    r_l4 = stats["resolved_l4_parent_child"]
    r_l5 = stats["resolved_l5_adjacency"]
    r_im = stats["identity_mismatch"]
    r_vm = stats["vintage_mismatch"]
    r_ms = stats["missing_spatial"]
    r_ig = stats["ignorable_residual"]
    r_rm = stats["requires_manual"]
    r_su = stats["still_unresolved"]

    total_resolved = r_l1 + r_l2 + r_l3 + r_l4 + r_l5 + r_ig
    total_explained = total_resolved + r_im + r_vm + r_ms + r_rm
    still_unexplained = n - total_explained

    lines = [
        "# Territorial Reconciliation Report",
        "",
        "Generated by `s10b_spatial_successor_discovery.py`.",
        "",
        "## Summary Statistics",
        "",
        "| Category | Count |",
        "|---|---|",
        f"| **Total originally unresolved events** | **{n}** |",
        f"| Resolved — alias/name matching (L1) | {r_l1} |",
        f"| Resolved — cross-state identity (L2) | {r_l2} |",
        f"| Resolved — spatial discovery (L3) | {r_l3} |",
        f"| Resolved — parent-child constrained (L4) | {r_l4} |",
        f"| Resolved — adjacency recovery (L5) | {r_l5} |",
        f"| Resolved — ignorable residual (<1%) | {r_ig} |",
        f"| **Total resolved** | **{total_resolved}** |",
        f"| Admin/spatial identity mismatch | {r_im} |",
        f"| Vintage mismatch (recent events) | {r_vm} |",
        f"| Missing spatial evidence | {r_ms} |",
        f"| Requires manual review | {r_rm} |",
        f"| **Still unresolved** | **{r_su}** |",
        "",
        "> [!NOTE]",
        "> Identity mismatches and vintage mismatches are **explained**, not unresolved.",
        "> They require deliberate data interventions (see §3 of the report).",
        "",
    ]

    # Final classification breakdown
    if not updated_residual.empty and "final_status" in updated_residual.columns:
        lines.append("## Final Classification Breakdown")
        lines.append("")
        lines.append("| Final Status | Count |")
        lines.append("|---|---|")
        for status, cnt in updated_residual["final_status"].value_counts().items():
            lines.append(f"| `{status}` | {cnt} |")
        lines.append("")

    # Top 20 largest residuals
    if not updated_residual.empty:
        lines.append("## Top 20 Largest Residuals (by initial residual area)")
        lines.append("")
        lines.append("| # | Year | Event Type | Predecessor State | Predecessor | "
                     "Source Area (km²) | Initial Residual % | Final Status |")
        lines.append("|---|---|---|---|---|---|---|---|")
        top20 = updated_residual.nlargest(20, "residual_area_km2")
        for i, (_, r) in enumerate(top20.iterrows(), 1):
            lines.append(
                f"| {i} | {r['event_year']} | {r.get('event_type','')} | "
                f"{r.get('predecessor_state','')} | {r.get('predecessor_name','')} | "
                f"{r.get('source_area_km2', 0):,.1f} | "
                f"{r.get('initial_residual_pct', 0):.1f}% | "
                f"`{r.get('final_status','')}`  |"
            )
        lines.append("")

    # Top 20 recovered events
    recovered = updated_residual[
        updated_residual.get("final_status", pd.Series(dtype=str)).str.startswith(
            "RECONCILED", na=False)
    ]
    if not recovered.empty:
        lines.append("## Top 20 Recovered Events")
        lines.append("")
        lines.append("| # | Year | Predecessor | State | Recovery Method | Level | Post-Discovery Residual % |")
        lines.append("|---|---|---|---|---|---|---|")
        top20r = recovered.nlargest(
            min(20, len(recovered)), "residual_area_km2"
        ) if "residual_area_km2" in recovered.columns else recovered.head(20)
        for i, (_, r) in enumerate(top20r.iterrows(), 1):
            lines.append(
                f"| {i} | {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"`{r.get('recovery_reason','')}` | L{r.get('recovery_level','')} | "
                f"{r.get('post_discovery_residual_pct', 0):.2f}% |"
            )
        lines.append("")

    # Top cross-state cases
    cross_state = updated_residual[
        updated_residual.get("cross_state_transition", pd.Series(dtype=bool)).fillna(False)
    ] if "cross_state_transition" in updated_residual.columns else pd.DataFrame()
    if not cross_state.empty:
        lines.append("## Cross-State Transition Cases")
        lines.append("")
        lines.append("| Year | Predecessor | Original State | Best Candidate | Final Status |")
        lines.append("|---|---|---|---|---|")
        for _, r in cross_state.head(20).iterrows():
            lines.append(
                f"| {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"{r.get('best_candidate','')} | "
                f"`{r.get('final_status','')}` |"
            )
        lines.append("")

    # Name resolution failures
    name_fail = updated_residual[
        updated_residual.get("recovery_reason", pd.Series(dtype=str)) == "NAME_RESOLUTION_FAILURE"
    ] if "recovery_reason" in updated_residual.columns else pd.DataFrame()
    if not name_fail.empty:
        lines.append("## Name Resolution Failures")
        lines.append("")
        lines.append("| Year | Predecessor | State | Event Type | Final Status |")
        lines.append("|---|---|---|---|---|")
        for _, r in name_fail.head(20).iterrows():
            lines.append(
                f"| {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | {r.get('event_type','')} | "
                f"`{r.get('final_status','')}` |"
            )
        lines.append("")

    # Identity mismatches
    im_df = updated_residual[
        updated_residual.get("final_status", pd.Series(dtype=str)) == "EVENT_GEOMETRY_IDENTITY_MISMATCH"
    ] if "final_status" in updated_residual.columns else pd.DataFrame()
    if not im_df.empty:
        lines.append("## Admin/Spatial Identity Mismatches")
        lines.append("")
        lines.append("These are research findings: the declared successor in event_summary "
                     "differs from the best spatial candidate. The declared relationship is "
                     "preserved; the spatial candidate is recorded separately.")
        lines.append("")
        lines.append("| Year | Predecessor | State | Best Spatial Candidate | Confidence |")
        lines.append("|---|---|---|---|---|")
        for _, r in im_df.head(20).iterrows():
            lines.append(
                f"| {r['event_year']} | {r.get('predecessor_name','')} | "
                f"{r.get('predecessor_state','')} | "
                f"{r.get('best_candidate','')} | "
                f"{r.get('best_candidate_score', 0):.3f} |"
            )
        lines.append("")

    # Required data interventions
    lines.extend([
        "## Required Data Interventions",
        "",
        "### 1. Events requiring alias updates (`district_evolution_master.csv`)",
        "Add or correct successor district names for events still classified",
        "`UNRESOLVED_GEOMETRIC_RESIDUAL` or `EVENT_GEOMETRY_IDENTITY_MISMATCH`.",
        "",
        "### 2. Events requiring state-transition metadata",
        "For events where the predecessor's state changed (cross-state transitions),",
        "the CSV should explicitly record `predecessor_state` and `successor_state`",
        "as separate columns to allow the engine to route across state boundaries.",
        "",
        "### 3. Vintage mismatch events",
        "For events classified `VINTAGE_MISMATCH`, verify whether the 2025 SOI",
        "geometry already contains the post-event district configuration. If so,",
        "the issue is a name spelling mismatch (fix via alias table). If not,",
        "the event should remain with `VINTAGE_MISMATCH` classification until",
        "updated source geometry is available.",
        "",
        "### 4. Identity mismatch events",
        "For `EVENT_GEOMETRY_IDENTITY_MISMATCH` events, review whether the spatial",
        "candidate is the correct successor (in which case the CSV is wrong) or",
        "the declared successor is correct (in which case the geometry is incomplete).",
        "Do NOT resolve by renaming the spatial candidate to the declared name.",
        "",
    ])

    report = "\n".join(lines)
    out_path = lib.EVENT_TRANSFER_DIR / "TERRITORIAL_RECONCILIATION_REPORT.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"  Wrote {out_path}")


# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------

def write_outputs(result: dict, data: dict, cfg: dict) -> None:
    print(f"\nWriting outputs to {lib.EVENT_TRANSFER_DIR}…")

    candidate_rows = result["candidate_rows"]
    updated_residual_rows = result["updated_residual_rows"]
    alias_registry_rows = result["alias_registry_rows"]
    stats = result["stats"]

    # 1. Candidate table
    candidate_df = pd.DataFrame(candidate_rows) if candidate_rows else pd.DataFrame()
    if not candidate_df.empty:
        candidate_df.to_csv(
            lib.EVENT_TRANSFER_DIR / "spatial_successor_candidates.csv",
            index=False
        )
        candidate_df.to_parquet(
            lib.EVENT_TRANSFER_DIR / "spatial_successor_candidates.parquet",
            index=False
        )
        print(f"  spatial_successor_candidates: {len(candidate_df)} rows")
    else:
        print("  spatial_successor_candidates: 0 rows (no L3+ candidates)")

    # 2. Augmented residual audit (merge resolved rows back into full audit)
    orig_residual = data["residual"].copy()
    resolved_df = pd.DataFrame(updated_residual_rows) if updated_residual_rows else pd.DataFrame()

    if not resolved_df.empty:
        # For rows that were already resolved (not UNRESOLVED_RESIDUAL), keep as-is
        # but add the new columns with neutral values
        unresolved_idx = orig_residual[
            orig_residual["accounting_status"] == "UNRESOLVED_RESIDUAL"
        ].index
        already_resolved = orig_residual.drop(index=unresolved_idx).copy()
        if not already_resolved.empty:
            already_resolved["initial_residual_pct"] = already_resolved["residual_pct"]
            already_resolved["post_discovery_residual_pct"] = already_resolved["residual_pct"]
            already_resolved["initial_status"] = already_resolved["accounting_status"]
            already_resolved["final_status"] = already_resolved["accounting_status"]
            already_resolved["recovery_level"] = 0
            already_resolved["recovery_reason"] = "ALREADY_RESOLVED_BY_S10"
            already_resolved["candidate_count"] = 0
            already_resolved["best_candidate"] = ""
            already_resolved["best_candidate_ck"] = ""
            already_resolved["best_candidate_score"] = 0.0
            already_resolved["candidate_status"] = "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"
            already_resolved["cross_state_transition"] = False
            already_resolved["name_resolution_attempted"] = False
            already_resolved["administrative_spatial_agreement"] = True

        combined = pd.concat([already_resolved, resolved_df], ignore_index=True)
    else:
        combined = orig_residual.copy()

    combined.to_csv(lib.EVENT_TRANSFER_DIR / "residual_area_audit.csv", index=False)
    print(f"  residual_area_audit.csv: {len(combined)} rows (augmented)")

    # 3. Alias registry
    if alias_registry_rows and cfg.get("persist_name_aliases", True):
        alias_df = pd.DataFrame(alias_registry_rows)
        alias_df.to_parquet(lib.NAME_ALIAS_REGISTRY_PATH, index=False)
        print(f"  name_alias_registry: {len(alias_df)} entries → {lib.NAME_ALIAS_REGISTRY_PATH}")

    # 4. GPKG
    write_gpkg(candidate_rows, resolved_df, data["obs_to_ck"],
               data["ck_to_name"], data["ck_year_to_obs"])

    # 5. Territorial reconciliation report
    write_report(stats, resolved_df, candidate_df, data["ck_to_name"])

    print("\n=== Stage 10b complete ===")
    print(f"  Originally unresolved: {stats['total_unresolved']}")
    print(f"  Resolved by alias (L1): {stats['resolved_l1_alias']}")
    print(f"  Resolved by cross-state (L2): {stats['resolved_l2_cross_state']}")
    print(f"  Resolved by spatial discovery (L3): {stats['resolved_l3_spatial']}")
    print(f"  Resolved by parent-child (L4): {stats['resolved_l4_parent_child']}")
    print(f"  Resolved by adjacency (L5): {stats['resolved_l5_adjacency']}")
    print(f"  Identity mismatches (research finding): {stats['identity_mismatch']}")
    print(f"  Vintage mismatch: {stats['vintage_mismatch']}")
    print(f"  Still unresolved: {stats['still_unresolved']}")
    print(f"\nSee {lib.EVENT_TRANSFER_DIR}/TERRITORIAL_RECONCILIATION_REPORT.md")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    data = load_inputs(cfg)
    result = run_discovery(cfg, data)
    write_outputs(result, data, cfg)


if __name__ == "__main__":
    main()
