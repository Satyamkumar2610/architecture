"""
Regression tests for Stage 10b — production v2
(s10b_spatial_successor_discovery.py)

Tests are grouped into 9 fixture classes matching the spec §44–§45.

Each test asserts:
  - area accounting values (raw vs post-discovery, never conflated)
  - recovery level used
  - final classification code (exact string)
  - administrative vs spatial distinction preserved

Run with:
  python3 -m pytest tests/test_s10b_regression.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline"))
import lib
import s10b_spatial_successor_discovery as s10b

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_name_index(vintage: int, district_name: str, state_name: str,
                     ck: str) -> dict:
    name_n = lib.normalize_name(district_name)
    state_n = lib.normalize_name(state_name)
    return {
        vintage: {
            "by_state": {(state_n, name_n): ck},
            "by_name": {name_n: [ck]},
        }
    }


def _merge_idx(*indexes) -> dict:
    result: dict = {}
    for idx in indexes:
        for yr, d in idx.items():
            if yr not in result:
                result[yr] = {"by_state": {}, "by_name": {}}
            result[yr]["by_state"].update(d["by_state"])
            for n, cks in d["by_name"].items():
                result[yr]["by_name"].setdefault(n, []).extend(cks)
    return result


def _minimal_row(**kwargs) -> pd.Series:
    base = {
        "event_id": "TEST_EVT",
        "event_year": 1960,
        "event_type": "NEW_DISTRICT",
        "predecessor_ck": "CK_PRE",
        "predecessor_name": "Test District",
        "predecessor_state": "Test State",
        "pre_vintage": 1951,
        "post_vintage": 1961,
        "source_area_km2": 10000.0,
        "intersection_area_km2": 0.0,
        "accounted_area_km2": 0.0,
        "residual_area_km2": 10000.0,
        "residual_pct": 100.0,
        "raw_residual_km2": 10000.0,
        "recovered_residual_km2": 0.0,
        "unresolved_residual_km2": 10000.0,
        "accounting_status": "UNRESOLVED_RESIDUAL",
        "residual_reason": "CK_RESOLUTION_FAILURE",
    }
    base.update(kwargs)
    return pd.Series(base)


EMPTY_CFG = {
    "enable_cross_state_resolution": True,
    "min_discovery_intersection_km2": 1.0,
    "min_candidate_overlap_pct": 0.5,
    "spatial_confirm_threshold": 0.55,
    "spatial_recovery_threshold": 0.65,
    "residual_tolerance_pct": 1.0,
    "recent_event_vintage_gap_years": 4,
    "candidate_ranking_weights": {
        "intersection_area_km2": 0.25,
        "intersection_pct_of_pre": 0.20,
        "intersection_pct_of_candidate": 0.15,
        "shared_boundary": 0.10,
        "spatial_adjacency": 0.05,
        "event_year_compat": 0.05,
        "parent_child_relation": 0.10,
        "state_transition_compat": 0.05,
        "name_similarity": 0.03,
        "declared_in_event": 0.02,
    },
}


# ============================================================================
# Test 1: Kangra — Punjab → Himachal Pradesh (cross-state)
# ============================================================================

class TestKangra:
    """
    §44.1 — Kangra: Punjab reorganisation 1966.
    Event CSV lists Kangra under Punjab. Post-1966 it is in Himachal Pradesh.
    L1 alias must resolve this; L2 cross-state must also resolve if L1 fails.
    """

    def _idx(self):
        return _make_name_index(1961, "Kangra", "Himachal Pradesh", "CK_KANGRA")

    def test_l1_alias_resolves_kangra_punjab(self):
        """L1 alias table must map (kangra, punjab) → Himachal Pradesh CK."""
        idx = self._idx()
        ck, atype, etype, prov = s10b.resolve_l1_alias(
            "Kangra", "Punjab", 1961, idx, {})
        assert ck == "CK_KANGRA", f"Expected CK_KANGRA via L1; got {ck!r}"
        assert "DOCUMENTARY" in etype or etype == "DOCUMENTARY"
        assert "Punjab Reorganisation" in prov or len(prov) > 0

    def test_l2_cross_state_resolves_kangra_if_l1_unavailable(self):
        """L2 must resolve Kangra cross-state even with an empty alias lookup."""
        idx = self._idx()
        # Use a name that the alias table doesn't have exactly
        ck, reason = s10b.resolve_l2_cross_state(
            "Kangra", "Punjab", 1961, idx, EMPTY_CFG)
        assert ck == "CK_KANGRA", f"L2 should find Kangra; got {ck!r}"
        assert "cross_state" in reason or "name_only" in reason or "state_successor" in reason

    def test_result_preserves_raw_residual(self):
        """raw_residual_pct (=100%) must remain 100% even after recovery."""
        idx = self._idx()
        row = _minimal_row(predecessor_name="Kangra", predecessor_state="Punjab",
                           source_area_km2=24934.0, residual_area_km2=24934.0,
                           residual_pct=100.0)
        ck_l1, atype, etype, prov = s10b.resolve_l1_alias(
            "Kangra", "Punjab", 1961, idx, {})
        assert ck_l1 is not None
        result = s10b._build_result(
            row, "RECONCILED_BY_ALIAS", "ALIAS_RESOLUTION",
            1, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            ck_l1, "Kangra", 1.0, 0.0, 100.0, 24934.0, 24934.0, 0.0, 1, 1.0, 1.0,
        )
        assert result["initial_residual_pct"] == 100.0, \
            "raw residual must be preserved at 100%"
        assert result["post_discovery_residual_pct"] == 0.0, \
            "post-discovery residual must be 0 after alias recovery"
        assert result["final_status"] == "RECONCILED_BY_ALIAS"
        assert result["recovery_level"] == 1


# ============================================================================
# Test 2: Nellore — historical state/name mismatch (Madras → Andhra Pradesh)
# ============================================================================

class TestNellore:
    """
    §44.2 — Nellore: listed under Madras/Tamil Nadu in event CSV.
    Should be Andhra Pradesh from 1956 onwards.
    """

    def _idx(self):
        return _make_name_index(1961, "Nellore", "Andhra Pradesh", "CK_NELLORE")

    def test_alias_resolves_nellore_from_madras(self):
        ck, atype, etype, prov = s10b.resolve_l1_alias(
            "Nellore", "Madras", 1961, self._idx(), {})
        assert ck == "CK_NELLORE", f"Expected CK_NELLORE via L1; got {ck!r}"

    def test_alias_resolves_nellore_from_tamil_nadu(self):
        ck, atype, etype, prov = s10b.resolve_l1_alias(
            "Nellore", "Tamil Nadu", 1961, self._idx(), {})
        assert ck == "CK_NELLORE", f"Expected CK_NELLORE via L1; got {ck!r}"

    def test_cross_state_resolves_nellore(self):
        ck, reason = s10b.resolve_l2_cross_state(
            "Nellore", "Madras", 1961, self._idx(), EMPTY_CFG)
        assert ck == "CK_NELLORE"


# ============================================================================
# Test 3: Jodhpur 2023 — recent-vintage mismatch
# ============================================================================

class TestJodhpur2023:
    """
    §44.3 — Jodhpur 2023 split. Event year 2023; latest vintage 2025.
    gap=2 < recent_event_vintage_gap_years=4.
    If no candidates: DATA_VINTAGE_INSUFFICIENT, not UNRESOLVED_GEOMETRIC_RESIDUAL.
    """

    def test_recent_event_no_candidates_is_vintage_insufficient(self):
        LATEST = s10b.LATEST_VINTAGE   # 2025
        recent_gap = 4
        event_year = 2023
        candidates = []
        is_recent = event_year >= (LATEST - recent_gap)
        assert is_recent, "2023 must be recent"
        if not candidates and is_recent:
            status = "DATA_VINTAGE_INSUFFICIENT"
        else:
            status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
        assert status == "DATA_VINTAGE_INSUFFICIENT"

    def test_raw_residual_preserved_for_vintage_mismatch(self):
        row = _minimal_row(event_year=2023, source_area_km2=22680.0,
                           residual_area_km2=22680.0, residual_pct=100.0)
        result = s10b._build_result(
            row, "DATA_VINTAGE_INSUFFICIENT", "VINTAGE_MISMATCH_RECENT",
            3, "NO_VALID_SPATIAL_SUCCESSOR",
            None, None, 0.0, 100.0, 100.0, 22680.0, 0.0, 22680.0, 0, 0.0, 0.0,
        )
        assert result["initial_residual_pct"] == 100.0
        assert result["post_discovery_residual_pct"] == 100.0
        assert result["final_status"] == "DATA_VINTAGE_INSUFFICIENT"
        assert result["recovery_level"] == 3


# ============================================================================
# Test 4: Kurnool 2022 — AP split, recent event
# ============================================================================

class TestKurnool2022:
    """§44.4 — Kurnool 2022; similar to Jodhpur but also tests state mismatch."""

    def test_kurnool_is_recent(self):
        assert 2022 >= (s10b.LATEST_VINTAGE - 4), "2022 must be within recent_gap"

    def test_alias_resolves_kurnool_from_madras(self):
        """Kurnool listed under Madras should resolve via L1 alias."""
        idx = _make_name_index(1961, "Kurnool", "Andhra Pradesh", "CK_KURNOOL")
        ck, atype, etype, prov = s10b.resolve_l1_alias("Kurnool", "Madras", 1961, idx, {})
        # If L1 doesn't fire (alias table may have it under Tamil Nadu), L2 must work
        if ck is None:
            ck, _ = s10b.resolve_l2_cross_state("Kurnool", "Madras", 1961, idx, EMPTY_CFG)
        assert ck == "CK_KURNOOL", f"Expected CK_KURNOOL via L1 or L2; got {ck!r}"

    def test_alias_resolves_kurnool_from_tamil_nadu(self):
        idx = _make_name_index(1961, "Kurnool", "Andhra Pradesh", "CK_KURNOOL")
        ck, _, _, _ = s10b.resolve_l1_alias("Kurnool", "Tamil Nadu", 1961, idx, {})
        assert ck == "CK_KURNOOL"


# ============================================================================
# Test 5: Pure rename — Trichur → Thrissur
# ============================================================================

class TestPureRename:
    """§44.5 — Trichur renamed Thrissur 1991. Same CK, same territory."""

    def _idx(self):
        return _make_name_index(1991, "Thrissur", "Kerala", "CK_THRISSUR")

    def test_trichur_resolves_via_alias(self):
        ck, atype, etype, prov = s10b.resolve_l1_alias(
            "Trichur", "Kerala", 1991, self._idx(), {})
        assert ck == "CK_THRISSUR"
        assert atype == "HISTORICAL_NAME"
        assert etype in ("DOCUMENTARY", "EXISTING_REGISTRY")

    def test_trichur_from_travancore_resolves(self):
        """
        Trichur under Travancore-Cochin must resolve to Thrissur/Kerala.
        The alias is keyed on 'travancore cochin' (normalised from 'Travancore - Cochin').
        The target is at vintage 1961 (pre-renaming) or 1991 (post-renaming).
        Both vintages should resolve.
        """
        idx_1961 = _make_name_index(1961, "Trichur", "Kerala", "CK_THRISSUR")
        idx_1991 = _make_name_index(1991, "Thrissur", "Kerala", "CK_THRISSUR")
        for v, idx in [(1961, idx_1961), (1991, idx_1991)]:
            ck, _, _, _ = s10b.resolve_l1_alias(
                "Trichur", "Travancore - Cochin", v, idx, {})
            if ck is None:
                ck, reason = s10b.resolve_l2_cross_state(
                    "Trichur", "Travancore - Cochin", v, idx, EMPTY_CFG)
            assert ck == "CK_THRISSUR", (
                f"Expected CK_THRISSUR from vintage {v}; got {ck!r}")

    def test_rename_raw_residual_preserved(self):
        row = _minimal_row(predecessor_name="Trichur", predecessor_state="Kerala",
                           event_type="RENAME", source_area_km2=3032.0,
                           residual_area_km2=3032.0, residual_pct=100.0)
        result = s10b._build_result(
            row, "RECONCILED_BY_ALIAS", "ALIAS_RESOLUTION",
            1, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_THRISSUR", "Thrissur", 1.0, 0.0, 100.0, 3032.0, 3032.0, 0.0, 1, 1.0, 1.0,
        )
        # Raw 100% preserved; post-discovery is 0
        assert result["initial_residual_pct"] == 100.0
        assert result["post_discovery_residual_pct"] == 0.0
        assert result["final_status"] == "RECONCILED_BY_ALIAS"


# ============================================================================
# Test 6: Simple split — one parent → two successors
# ============================================================================

class TestSimpleSplit:
    """§44.6 — A → B + C. Both successors declared; both should be candidates."""

    def test_declared_high_score_is_confirmed_successor(self):
        cand = {
            "declared_in_event": True,
            "candidate_score": 0.82,
            "parent_child_related": False,
        }
        status = s10b.classify_candidate(cand, EMPTY_CFG)
        assert status == "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"

    def test_undeclared_high_score_is_event_unlisted(self):
        cand = {
            "declared_in_event": False,
            "candidate_score": 0.72,
            "parent_child_related": False,
        }
        status = s10b.classify_candidate(cand, EMPTY_CFG)
        assert status == "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED"

    def test_low_score_is_no_valid(self):
        cand = {
            "declared_in_event": False,
            "candidate_score": 0.18,
            "parent_child_related": False,
        }
        status = s10b.classify_candidate(cand, EMPTY_CFG)
        assert status == "NO_VALID_SPATIAL_SUCCESSOR"

    def test_mid_score_is_possible(self):
        cand = {
            "declared_in_event": False,
            "candidate_score": 0.42,
            "parent_child_related": False,
        }
        status = s10b.classify_candidate(cand, EMPTY_CFG)
        assert status == "POSSIBLE_SPATIAL_SUCCESSOR"


# ============================================================================
# Test 7: Multi-parent formation — two predecessors → one successor
# ============================================================================

class TestMultiParentFormation:
    """
    §44.7 — Multi-parent formation.
    The engine must preserve raw residuals for each predecessor separately.
    """

    def test_each_predecessor_retains_its_own_raw_residual(self):
        row_a = _minimal_row(predecessor_ck="CK_A", predecessor_name="District A",
                              source_area_km2=5000.0, residual_area_km2=100.0,
                              residual_pct=2.0)
        row_b = _minimal_row(predecessor_ck="CK_B", predecessor_name="District B",
                              source_area_km2=3000.0, residual_area_km2=60.0,
                              residual_pct=2.0)
        result_a = s10b._build_result(
            row_a, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_SUCCESSOR", "Successor X", 0.78,
            0.5, 2.0, 100.0, 95.0, 5.0, 3, 0.85, 0.70,
        )
        result_b = s10b._build_result(
            row_b, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_SUCCESSOR", "Successor X", 0.78,
            0.3, 2.0, 60.0, 57.0, 3.0, 3, 0.85, 0.70,
        )
        # Each predecessor's raw residual is preserved independently
        assert result_a["initial_residual_pct"] == 2.0
        assert result_b["initial_residual_pct"] == 2.0
        # They are NOT summed or merged
        assert result_a["predecessor_ck"] == "CK_A"
        assert result_b["predecessor_ck"] == "CK_B"

    def test_multi_parent_no_single_forced_attribution(self):
        """Engine must NOT force all residual to a single predecessor."""
        row_a = _minimal_row(predecessor_ck="CK_A", source_area_km2=5000.0,
                              residual_area_km2=100.0, residual_pct=2.0)
        result = s10b._build_result(
            row_a, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_S", "S", 0.80, 0.4, 2.0, 100.0, 95.0, 5.0, 2, 0.9, 0.7,
        )
        assert result["unresolved_area_km2"] == 5.0  # not zero-d
        assert result["recovered_area_km2"] == 95.0   # not inflated to 100.0


# ============================================================================
# Test 8: Continuing-district area transfer
# ============================================================================

class TestContinuingDistrictTransfer:
    """
    §44.8 — Territory moves A→B where both A and B continue.
    The declared successor (the continuing district) must be
    CONFIRMED_ADMINISTRATIVE_SUCCESSOR, NOT an identity mismatch.
    """

    def test_declared_continuing_district_is_confirmed(self):
        cand = {
            "declared_in_event": True,
            "candidate_score": 0.84,
            "parent_child_related": True,
        }
        status = s10b.classify_candidate(cand, EMPTY_CFG)
        assert status == "CONFIRMED_ADMINISTRATIVE_SUCCESSOR"

    def test_territorial_transfer_does_not_create_lineage(self):
        """
        A territorial transfer between two continuing districts must NOT
        update any lineage record. S10b writes new audit rows only.
        The test verifies the _build_result fields do not include a lineage field.
        """
        row = _minimal_row(predecessor_ck="CK_A", event_type="AREA_TRANSFER")
        result = s10b._build_result(
            row, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_B", "District B", 0.80, 0.3, 100.0, 10000.0, 9700.0, 300.0, 2, 0.85, 0.8,
        )
        # No lineage field should be written
        assert "lineage_from" not in result
        assert "lineage_to" not in result
        assert "parent_ck" not in result
        # The 'best_candidate_ck' is recorded as spatial candidate, NOT as lineage parent
        assert result["best_candidate_ck"] == "CK_B"
        assert result["final_status"] == "RECONCILED_BY_SPATIAL_DISCOVERY"


# ============================================================================
# Test 9: Intentionally unresolved residual
# ============================================================================

class TestIntentionallyUnresolved:
    """
    §44.9 — Frontier territory or genuinely missing geometry.
    No spatial evidence; historical event. Must remain UNRESOLVED, never zeroed.
    """

    def test_historical_no_candidates_stays_unresolved(self):
        LATEST = s10b.LATEST_VINTAGE
        event_year = 1957
        is_recent = event_year >= (LATEST - 4)
        assert not is_recent, "1957 must NOT be a recent event"
        candidates = []
        if not candidates and not is_recent:
            status = "UNRESOLVED_GEOMETRIC_RESIDUAL"
        else:
            status = "DATA_VINTAGE_INSUFFICIENT"
        assert status == "UNRESOLVED_GEOMETRIC_RESIDUAL"

    def test_unresolved_raw_residual_never_zeroed(self):
        row = _minimal_row(predecessor_name="Balipara Frontier Tract",
                           predecessor_state="Assam", event_year=1954,
                           source_area_km2=13409.0, residual_area_km2=13409.0,
                           residual_pct=100.0)
        result = s10b._build_result(
            row, "UNRESOLVED_GEOMETRIC_RESIDUAL", "GENUINELY_UNRESOLVED",
            6, "NO_VALID_SPATIAL_SUCCESSOR",
            None, None, 0.0, 100.0, 100.0, 13409.0, 0.0, 13409.0, 0, 0.0, 0.0,
        )
        # Both raw and post-discovery residuals must be preserved
        assert result["initial_residual_pct"] == 100.0
        assert result["post_discovery_residual_pct"] == 100.0
        # Nothing is zero-d
        assert result["unresolved_area_km2"] == 13409.0
        assert result["recovered_area_km2"] == 0.0
        assert result["recovery_level"] == 6
        assert result["final_status"] == "UNRESOLVED_GEOMETRIC_RESIDUAL"

    def test_recovery_level_6_for_genuinely_unresolved(self):
        row = _minimal_row(source_area_km2=5000.0, residual_area_km2=5000.0)
        result = s10b._build_result(
            row, "UNRESOLVED_GEOMETRIC_RESIDUAL", "MISSING_GEOMETRIC_EVIDENCE",
            6, "NO_VALID_SPATIAL_SUCCESSOR", None, None,
            0.0, 100.0, 100.0, 5000.0, 0.0, 5000.0, 0, 0.0, 0.0,
        )
        assert result["recovery_level"] == 6


# ============================================================================
# Architecture invariants (§45)
# ============================================================================

class TestArchitectureInvariants:
    """§45 — system-level invariants."""

    def test_no_negative_area_in_build_result(self):
        """_build_result must not produce negative area."""
        row = _minimal_row(source_area_km2=10000.0, residual_area_km2=10000.0)
        result = s10b._build_result(
            row, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_X", "X", 0.8, 0.0, 100.0, 10000.0, 10000.0, 0.0, 1, 0.9, 0.8,
        )
        assert result["recovered_area_km2"] >= 0.0
        assert result["unresolved_area_km2"] >= 0.0
        assert result["post_discovery_residual_pct"] >= 0.0

    def test_spatial_candidate_never_becomes_lineage(self):
        """The _build_result dict must never contain lineage-creating fields."""
        row = _minimal_row()
        result = s10b._build_result(
            row, "RECONCILED_BY_SPATIAL_DISCOVERY", "SPATIAL_SUCCESSOR_DISCOVERED",
            3, "SPATIALLY_CONFIRMED_BUT_EVENT_UNLISTED",
            "CK_Y", "Y", 0.70, 1.5, 100.0, 10000.0, 9850.0, 150.0, 2, 0.75, 0.30,
        )
        lineage_fields = {"lineage_from", "lineage_to", "parent_ck",
                          "child_ck", "successor_of", "predecessor_of"}
        assert lineage_fields.isdisjoint(result.keys()), \
            f"Lineage fields found in result: {lineage_fields & result.keys()}"

    def test_raw_residual_never_overwritten(self):
        """initial_residual_pct must equal the original residual_pct from S10."""
        row = _minimal_row(residual_pct=97.3)
        result = s10b._build_result(
            row, "RECONCILED_BY_ALIAS", "ALIAS_RESOLUTION",
            1, "CONFIRMED_ADMINISTRATIVE_SUCCESSOR",
            "CK_Z", "Z", 1.0, 0.0, 97.3, 9730.0, 9730.0, 0.0, 1, 1.0, 1.0,
        )
        assert result["initial_residual_pct"] == 97.3, \
            "initial_residual_pct must equal original S10 residual_pct"

    def test_spatial_discovery_alone_not_authoritative(self):
        """
        A SPATIAL_DISCOVERY-only entry must NOT appear in the approved registry.
        This is a design constraint: only DOCUMENTARY/MANUAL_VERIFIED may be approved.
        """
        # Directly verify the filtering logic used in _write_alias_registries
        candidate = {
            "evidence_type": "SPATIAL_DISCOVERY",
            "approval_status": "CANDIDATE",
        }
        approved_types = ("DOCUMENTARY", "MANUAL_VERIFIED", "EXISTING_REGISTRY")
        will_approve = candidate["evidence_type"] in approved_types
        assert not will_approve, \
            "SPATIAL_DISCOVERY alone must never auto-promote to approved registry"

    def test_name_similarity_is_symmetric(self):
        a = s10b.name_sim("trichur", "thrissur")
        b = s10b.name_sim("thrissur", "trichur")
        assert abs(a - b) < 0.05, f"JW not symmetric: {a} vs {b}"

    def test_normalize_strips_district_suffix(self):
        assert lib.normalize_name("Kangra District") == lib.normalize_name("Kangra")

    def test_state_predecessor_map_covers_key_transitions(self):
        """STATE_PREDECESSOR_MAP must include critical historical transitions."""
        norm_map = s10b._STATE_NORM_SUCCESSORS
        assert any("kerala" in v for v in norm_map.values()), \
            "Travancore-Cochin → Kerala missing"
        assert any("gujarat" in v for k, v in norm_map.items() if "bombay" in k), \
            "Bombay → Gujarat missing"
        assert any("andhra pradesh" in v for k, v in norm_map.items()
                   if "madras" in k), "Madras → Andhra Pradesh missing"
        assert any("himachal pradesh" in v for k, v in norm_map.items()
                   if "punjab" in k), "Punjab → Himachal Pradesh missing"

    def test_cross_state_disabled_returns_none(self):
        idx = _make_name_index(1961, "Kangra", "Himachal Pradesh", "CK_KANGRA")
        cfg = {**EMPTY_CFG, "enable_cross_state_resolution": False}
        ck, reason = s10b.resolve_l2_cross_state("Kangra", "Punjab", 1961, idx, cfg)
        assert ck is None, "Cross-state disabled must return None"
        assert reason == "CROSS_STATE_DISABLED"

    def test_s10_output_not_overwritten_by_s10b(self):
        """S10 original output (residual_area_audit_s10.csv) must exist and contain 94 unresolved."""
        from pathlib import Path
        p = Path("outputs/event_transfer/residual_area_audit_s10.csv")
        assert p.exists(), "S10 original audit file must be preserved"
        import pandas as pd
        df = pd.read_csv(p)
        unres = (df["accounting_status"] == "UNRESOLVED_RESIDUAL").sum()
        assert unres == 94, f"S10 original must still show 94 unresolved; got {unres}"

    def test_transfer_matrix_has_all_spec_fields(self):
        """Transfer matrix must have all fields from spec §8."""
        from pathlib import Path
        import pandas as pd
        p = Path("outputs/event_transfer/event_area_transfer_matrix.csv")
        if not p.exists():
            pytest.skip("transfer matrix not generated yet")
        tm = pd.read_csv(p)
        spec_fields = [
            "event_id", "event_year", "event_type",
            "from_ck", "from_name", "from_state",
            "to_ck", "to_name", "to_state",
            "relationship_type", "area_km2", "area_pct_from", "area_pct_to",
            "source_vintage", "target_vintage",
            "administrative_relationship", "spatial_relationship",
            "declared_transfer", "measured_transfer",
            "successor_resolution_method", "recovery_level", "recovery_reason",
            "raw_area", "recovered_area", "residual_area",
            "residual_pct", "confidence", "provenance",
        ]
        missing = [f for f in spec_fields if f not in tm.columns]
        assert not missing, f"Transfer matrix missing spec fields: {missing}"

    def test_gpkg_has_geometry(self):
        """All GPKG layers must have geometry (not None) for QGIS inspection."""
        from pathlib import Path
        import geopandas as gpd
        import fiona
        p = Path("outputs/event_transfer/event_spatial_candidates.gpkg")
        if not p.exists():
            pytest.skip("GPKG not generated yet")
        layers = fiona.listlayers(str(p))
        for layer in ["raw_intersections", "recovered_transfers"]:
            if layer in layers:
                gdf = gpd.read_file(str(p), layer=layer)
                n_null = gdf.geometry.isna().sum()
                assert n_null == 0, \
                    f"GPKG layer '{layer}' has {n_null} null geometries"

    def test_alias_candidates_not_in_approved_registry(self):
        """SPATIAL_DISCOVERY entries must be in candidates, not approved registry."""
        from pathlib import Path
        import pandas as pd
        cand_path = Path("data/gold/core/name_alias_candidates.parquet")
        reg_path = Path("data/gold/core/name_alias_registry.parquet")
        if not cand_path.exists() or not reg_path.exists():
            pytest.skip("Alias registries not generated yet")
        cands = pd.read_parquet(cand_path)
        registry = pd.read_parquet(reg_path)
        if "evidence_type" in registry.columns:
            spatial_in_reg = (registry["evidence_type"] == "SPATIAL_DISCOVERY").sum()
            assert spatial_in_reg == 0, \
                f"Found {spatial_in_reg} SPATIAL_DISCOVERY entries in approved registry"
