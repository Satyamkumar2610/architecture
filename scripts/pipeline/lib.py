"""
Shared library for the District Evolution Intelligence pipeline (v2).

This rebuild fixes the defects documented in
docs/architecture/lineage_area_redesign.md:
  - deterministic IDs (sha256 of natural key), never uuid4
  - one geodesic area method everywhere (pyproj.Geod, WGS84), never mixed
  - explicit, configured tolerances for "small difference" edge cases,
    never a silent normalize-to-1.0
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Geod

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SILVER_GEOM_DIR = PROJECT_ROOT / "data" / "silver" / "geometry"
GOLD_SPATIAL_DIR = PROJECT_ROOT / "data" / "gold" / "spatial"
GOLD_EVENTS_DIR = PROJECT_ROOT / "data" / "gold" / "events"
GOLD_CORE_DIR = PROJECT_ROOT / "data" / "gold" / "core"
PRODUCTS_DIR = PROJECT_ROOT / "data" / "products"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "pipeline"
EVENTS_CSV = PROJECT_ROOT / "data" / "bronze" / "events" / "district_evolution_master.csv"

for d in (SILVER_GEOM_DIR, GOLD_SPATIAL_DIR, GOLD_EVENTS_DIR, GOLD_CORE_DIR, PRODUCTS_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

PIPELINE_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Tolerances — every "ignore small differences" edge case is named and
# configured here, not scattered as magic numbers through the code.
# ---------------------------------------------------------------------------

# A parcel from the overlay smaller than this (in km^2 OR as a fraction of
# both source and target area) is topological noise (snapping/digitization
# jitter along a shared boundary), not a real territorial transfer.
SLIVER_ABS_KM2 = 0.5
SLIVER_REL_FRACTION = 0.0001  # 0.01% of the smaller polygon's area

# Self-overlap / gap budget for a single vintage layer (V1, V2).
SELF_OVERLAP_WARN_FRACTION = 0.001   # 0.1% of national footprint
SELF_OVERLAP_ERROR_FRACTION = 0.01   # 1%
GAP_WARN_FRACTION = 0.01             # 1%

# Stored vs recomputed area agreement (V4). Two computations of the same
# polygon by the same method should agree far tighter than this; this is a
# smoke test for method drift (see redesign doc section 2.8), not a
# measurement-uncertainty tolerance.
AREA_METHOD_TOLERANCE_PCT = 0.05  # 0.05%

# Row/column closure of the transition matrix (V3). A polygon's geodesic
# area computed once on the whole ring, versus summed over ~5-15 overlay
# fragments of it, takes a different floating-point summation path and
# does not agree to machine precision even though both are the identical
# ST_Area_Spheroid-equivalent method. Measured empirically on the 1951-1961
# transition (316 districts): mean 0.31 km2, max 1.13 km2, max relative
# 0.108%, zero cases above that. This tolerance absorbs exactly that noise
# source and nothing larger — a genuine topology defect (e.g. an
# unrepaired self-overlap) produces errors an order of magnitude bigger.
CLOSURE_TOLERANCE_KM2 = 2.0
CLOSURE_TOLERANCE_PCT = 0.15  # 0.15%

# Continuity threshold for identity (retention / inheritance share).
CONTINUITY_THRESHOLD = 0.90

# Conservation tolerance for event area accounting: the same "small
# difference" principle as CLOSURE, applied at event grain, where vintage
# geometries (not exact event-date boundaries) introduce real observational
# slack. Scales with district size per architecture invariant precedent.
CONSERVATION_ABS_TOLERANCE_KM2 = 5.0
CONSERVATION_REL_TOLERANCE_PCT = 0.5

GEOD = Geod(ellps="WGS84")


def geodesic_area_km2(geom) -> float:
    """The one area method used everywhere in this pipeline."""
    if geom is None or geom.is_empty:
        return 0.0
    area_m2, _ = GEOD.geometry_area_perimeter(geom)
    return abs(float(area_m2)) / 1_000_000.0


def stable_id(*parts: object) -> str:
    """Deterministic identifier: sha256 of a natural key. Reruns are idempotent."""
    text = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[.’'`]")
_DASH_RE = re.compile(r"[-–—/]")


def normalize_name(value: object) -> str:
    """Normalize a district/state name for matching. Not a display value."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub("", text)
    text = _DASH_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    # drop a small set of purely administrative suffixes that vary by source
    text = re.sub(r"\bdistrict\b", "", text).strip()
    text = _WS_RE.sub(" ", text).strip()
    return text


def display_name(value: object) -> str:
    """Human-readable canonical form (title case, single spaces)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    text = _WS_RE.sub(" ", text)
    return text.title()


# Census PC11 state codes -> canonical modern state name. Public, standard
# Census of India 2011 codes. Used only for the 2011 Stanford layer, which
# carries state as a numeric code with no name field.
PC11_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
}

# Canonicalization for state name spelling/rename drift observed across the
# 9 vintages and the events CSV (see audit). This is a DISPLAY / weak-prior
# aid only — it is never load-bearing for identity, which runs on spatial
# continuity (lib.CONTINUITY_THRESHOLD), so imperfect coverage here cannot
# fabricate lineage.
STATE_ALIASES = {
    "orissa": "Odisha", "pondicherry": "Puducherry",
    "uttranchal": "Uttarakhand", "mysore": "Karnataka",
    "karnatak": "Karnataka", "andamans and nicobars": "Andaman and Nicobar Islands",
    "andaman and nicobar": "Andaman and Nicobar Islands",
    "tamilnadu": "Tamil Nadu", "madras": "Tamil Nadu",
    "laccadive minicoy and amindiv": "Lakshadweep",
    "goa daman and diu": "Goa",
    "andhra pradesh telangana": "Andhra Pradesh",  # pre-2014 combined state
    "dadra and nagar have": "Dadra and Nagar Haveli",
}


def canonical_state(value: object) -> str:
    norm = normalize_name(value)
    norm = norm.replace(",", "")
    canon = STATE_ALIASES.get(norm)
    if canon:
        return canon
    return display_name(value)


def parse_multi_name(value: object) -> list[str]:
    """Split a comma/'and'-joined list of district names (events CSV parent_district)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text:
        return []
    # normalize connector words before splitting
    text = re.sub(r"\band\b", ",", text, flags=re.IGNORECASE)
    parts = [p.strip().rstrip(".").strip() for p in text.split(",")]
    return [p for p in parts if p]


def sliver_threshold_km2(area_a: float, area_b: float) -> float:
    smaller = min(a for a in (area_a, area_b) if a is not None and a > 0) if (area_a or area_b) else 0.0
    return max(SLIVER_ABS_KM2, smaller * SLIVER_REL_FRACTION)


def within_tolerance(value: float, expected: float, abs_tol: float, rel_tol_pct: float) -> bool:
    if value is None or expected is None or pd.isna(value) or pd.isna(expected):
        return False
    tol = max(abs_tol, abs(expected) * rel_tol_pct / 100.0)
    return abs(value - expected) <= tol
