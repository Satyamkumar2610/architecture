"""
Stage 6 — Lineage classification, corroborated by events (not driven by them).

Per redesign doc section 4.4 / Principle 7 ("events as evidence, not spatial
truth"): the transition matrix (Stage 4) and continuity resolution (Stage 5)
already determine which CKs continue and which close. This stage classifies
the remaining genuine lineage edges (from_ck != to_ck, i.e. territory that
left a closing or continuing source and became a NEW identity) into the
architecture's four relationship types, then checks each edge against the
events CSV (Stage 3) as corroborating evidence -- agreement is recorded,
disagreement is a research-queue finding, but geometry decides the edge.

A from_ck != to_ck row in the transition matrix means territory crossed a
CK boundary, but that is NOT automatically identity derivation: it also
covers an ordinary boundary transfer between two districts that both
already continue (to_ck existed before this window). Architecture
invariant L-3 is explicit: such a transfer must NOT produce a
district_relationship row. The correct test is whether to_ck was actually
ESTABLISHED in this transition window (registry.established_year ==
year_b) -- only then is genuine identity being formed. Edges into an
already-existing to_ck are routed to Stage 7's measured_area_transfer
product instead (the table the old system's audit found was never built
-- see redesign doc section 2.9 / section 4.3).

Per-edge classification, for edges into a genuinely NEW to_ck:
  - source continues (did not close at year_b)        -> FORMED_FROM
  - source closes, and is the ONLY closing predecessor
    feeding this specific target in this window        -> SPLIT_FROM
  - source closes, and >=2 distinct closing predecessors
    feed this specific target in this window            -> MERGED_INTO

RECONSTITUTED_FROM (new CK resembling a much-earlier abolished CK) is not
attempted here -- it requires matching a new CK's territory against a
non-adjacent-vintage closed CK, a materially different computation from the
adjacent-vintage matrix this stage uses. Left as a documented gap, not
guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def load_events_index() -> dict:
    events = pd.read_parquet(lib.GOLD_EVENTS_DIR / "boundary_event.parquet")
    participants = pd.read_parquet(lib.GOLD_EVENTS_DIR / "event_participant.parquet")
    participants["name_norm"] = participants["district_name_raw"].map(lib.normalize_name)
    participants["state_norm"] = participants["state"].map(lib.normalize_name)
    events = events.set_index("event_id")

    # Two indices: state-scoped (precise tier) and name-only (fallback tier,
    # for state-name drift across sources -- e.g. "Andhra Pradesh-Telangana"
    # in the events CSV vs "Andhra Pradesh" as normalized from geometry).
    pred_by_state: dict[tuple[str, str], list[tuple[str, int]]] = {}
    succ_by_state: dict[tuple[str, str], list[tuple[str, int]]] = {}
    pred_by_name: dict[str, list[tuple[str, int]]] = {}
    succ_by_name: dict[str, list[tuple[str, int]]] = {}
    for _, row in participants.iterrows():
        year = int(events.loc[row["event_id"], "effective_year"])
        entry = (row["event_id"], year)
        state_key = (row["state_norm"], row["name_norm"])
        if row["role"] == "PREDECESSOR":
            pred_by_state.setdefault(state_key, []).append(entry)
            pred_by_name.setdefault(row["name_norm"], []).append(entry)
        else:
            succ_by_state.setdefault(state_key, []).append(entry)
            succ_by_name.setdefault(row["name_norm"], []).append(entry)
    return {
        "pred_by_state": pred_by_state, "succ_by_state": succ_by_state,
        "pred_by_name": pred_by_name, "succ_by_name": succ_by_name,
        "events": events,
    }


# Administrative-order year vs. mapping-vintage year can legitimately
# differ by a few years (order signed, boundary surveyed/mapped later, or
# the CSV's "effective_year" reflects gazette notification while the next
# available map vintage lags). This buffer absorbs that, not real slack.
_EVENT_YEAR_BUFFER = 3


def corroborate(from_name_norm, to_name_norm, state_norm, year_a, year_b, event_idx) -> tuple[str, str | None]:
    lo, hi = year_a - 1, year_b + _EVENT_YEAR_BUFFER

    def match(hits: list[tuple[str, int]]) -> set[str]:
        return {eid for eid, yr in hits if lo <= yr <= hi}

    # Tier 1: state-scoped match (precise)
    pred_hits = match(event_idx["pred_by_state"].get((state_norm, from_name_norm), []))
    succ_hits = match(event_idx["succ_by_state"].get((state_norm, to_name_norm), []))
    common = pred_hits & succ_hits
    if common:
        return "GAZETTE_CORROBORATED", sorted(common)[0]

    # Tier 2: name-only match (state-name drift fallback)
    pred_hits = match(event_idx["pred_by_name"].get(from_name_norm, []))
    succ_hits = match(event_idx["succ_by_name"].get(to_name_norm, []))
    common = pred_hits & succ_hits
    if common:
        return "GAZETTE_CORROBORATED_WEAK", sorted(common)[0]

    return "SPATIAL_INFERRED", None


def main() -> None:
    registry = pd.read_parquet(lib.GOLD_CORE_DIR / "canonical_key_registry.parquet")
    closed_year_by_ck = dict(zip(registry["canonical_key"], registry["closed_year"]))
    established_year_by_ck = dict(zip(registry["canonical_key"], registry["established_year"]))
    name_by_ck = dict(zip(registry["canonical_key"], registry["display_name"]))
    state_by_ck = dict(zip(registry["canonical_key"], registry["state_at_creation"]))

    transitions = pd.read_parquet(lib.GOLD_SPATIAL_DIR / "ck_transitions.parquet")
    cross_ck = transitions[transitions["from_ck"] != transitions["to_ck"]].copy()

    is_new_target_this_window = cross_ck.apply(
        lambda r: established_year_by_ck.get(r["to_ck"]) == r["year_b"], axis=1
    )
    genuine = cross_ck[is_new_target_this_window].copy()
    transfers = cross_ck[~is_new_target_this_window].copy()
    print(f"Cross-CK material transitions: {len(cross_ck)} / {len(transitions)} total")
    print(f"  -> genuine lineage edges (to_ck newly established this window): {len(genuine)}")
    print(f"  -> territorial transfers between continuing/pre-existing CKs (L-3, no relationship row): {len(transfers)}")

    transfers_out = transfers.rename(columns={"transition_area_km2": "transferred_area_km2"})
    transfers_out["transfer_id"] = transfers_out.apply(
        lambda r: lib.stable_id("XFER", r["from_ck"], r["to_ck"], r["year_a"], r["year_b"]), axis=1
    )
    transfers_out[[
        "transfer_id", "from_ck", "to_ck", "year_a", "year_b",
        "transferred_area_km2", "outflow_share", "inflow_share",
    ]].to_parquet(lib.GOLD_SPATIAL_DIR / "measured_area_transfer.parquet")
    transfers_out.to_csv(lib.OUTPUT_DIR / "s6_measured_area_transfer.csv", index=False)

    genuine["source_closes"] = genuine.apply(
        lambda r: closed_year_by_ck.get(r["from_ck"]) == r["year_b"], axis=1
    )

    # count distinct CLOSING predecessors feeding each (to_ck, year window)
    closing_edges = genuine[genuine["source_closes"]]
    closing_pred_counts = (
        closing_edges.groupby(["to_ck", "year_a", "year_b"])["from_ck"].nunique()
    )

    def classify(row) -> str:
        if not row["source_closes"]:
            return "FORMED_FROM"
        n = closing_pred_counts.get((row["to_ck"], row["year_a"], row["year_b"]), 1)
        return "MERGED_INTO" if n >= 2 else "SPLIT_FROM"

    genuine["relationship_type"] = genuine.apply(classify, axis=1)
    genuine["rel_id"] = genuine.apply(
        lambda r: lib.stable_id("REL", r["from_ck"], r["to_ck"], r["year_a"], r["year_b"]), axis=1
    )

    event_idx = load_events_index()
    corrob = genuine.apply(
        lambda r: corroborate(
            lib.normalize_name(name_by_ck.get(r["from_ck"])),
            lib.normalize_name(name_by_ck.get(r["to_ck"])),
            lib.normalize_name(state_by_ck.get(r["to_ck"])),
            r["year_a"], r["year_b"], event_idx,
        ), axis=1, result_type="expand",
    )
    genuine["lineage_basis"] = corrob[0]
    genuine["supporting_event_id"] = corrob[1]

    # lineage_confidence: continuity math is exact; corroboration raises
    # confidence, absence of a matching event lowers it (still real evidence
    # -- the transition matrix -- just uncorroborated by the academic
    # compilation, e.g. pre-1951 events or events the CSV doesn't cover).
    _confidence_by_basis = {
        "GAZETTE_CORROBORATED": 0.95, "GAZETTE_CORROBORATED_WEAK": 0.85, "SPATIAL_INFERRED": 0.70,
    }
    genuine["lineage_confidence"] = genuine["lineage_basis"].map(_confidence_by_basis)

    out_cols = [
        "rel_id", "from_ck", "to_ck", "relationship_type", "year_a", "year_b",
        "transition_area_km2", "outflow_share", "inflow_share",
        "lineage_basis", "supporting_event_id", "lineage_confidence",
    ]
    district_relationship = genuine[out_cols].sort_values(["year_b", "from_ck", "to_ck"])
    district_relationship.to_parquet(lib.GOLD_EVENTS_DIR / "district_relationship.parquet")
    district_relationship.to_csv(lib.OUTPUT_DIR / "s6_district_relationship.csv", index=False)

    print(f"\nrelationship_type counts:")
    print(district_relationship["relationship_type"].value_counts())
    print(f"\nlineage_basis counts:")
    print(district_relationship["lineage_basis"].value_counts())
    corroborated = district_relationship["lineage_basis"].isin(
        ["GAZETTE_CORROBORATED", "GAZETTE_CORROBORATED_WEAK"]
    )
    print(f"corroboration rate (either tier): {100*corroborated.mean():.1f}%")

    # DAG acyclicity check (invariant L-1): with CKs allocated
    # sequentially and edges only ever pointing from an earlier-established
    # CK to a later one (continuity edges never appear here since
    # from_ck != to_ck was already filtered, and Stage 5 only mints a new,
    # higher-numbered CK), the edge set is acyclic by construction. Verify.
    est_year = dict(zip(registry["canonical_key"], registry["established_year"]))
    bad = district_relationship[
        district_relationship.apply(
            lambda r: est_year.get(r["to_ck"], 0) < est_year.get(r["from_ck"], 0), axis=1
        )
    ]
    if len(bad):
        print(f"WARNING: {len(bad)} edges violate established-year ordering (possible cycle risk)")
    else:
        print("DAG check: all edges point from earlier-established to later-established CK. OK.")

    print(f"\nWrote {lib.GOLD_EVENTS_DIR / 'district_relationship.parquet'} ({len(district_relationship)} rows)")


if __name__ == "__main__":
    main()
