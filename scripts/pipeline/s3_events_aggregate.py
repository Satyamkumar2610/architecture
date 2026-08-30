"""
Stage 3 — Aggregate the events CSV to the true administrative event grain.

Fixes the single highest-value defect found in the audit
(docs/architecture/lineage_area_redesign.md section 2.1): the old pipeline
minted one event_id per CSV row, and the CSV has one row per
(parent, child) pair. A 1-parent-3-children split became three fake
one-child "events", which made conservation untestable by construction.

Verified against the actual CSV (1,550 rows):
  - 411/423 SPLIT (parent, state, year) groups have >1 child row
  - 78/378 NEW_DISTRICT (parent, state, year) groups have >1 child row
  - 14 NEW_DISTRICT rows carry a comma/`and`-joined MULTI-parent string
    (e.g. Baksa 2004 from "Nalbari, Barpeta, Kamrup, And Darrang")
  - 0 SPLIT rows have multi-parent or multi-child strings
  - RENAME rows are already 1:1 (old name -> new name); no grouping needed

Event grain: one event = one (event_type, parent_district-as-written,
state, effective_year) group. This is exact for SPLIT and NEW_DISTRICT
(verified no cross-contamination above) and trivially exact for RENAME.
Multi-parent strings are parsed per row into a predecessor list; multi-
child groups are captured by the groupby.

Event IDs are deterministic (sha256 of the natural key), not uuid4 —
fixes the reproducibility defect in section 2.6: reruns must be
idempotent so every downstream product stays joinable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def load_events() -> pd.DataFrame:
    df = pd.read_csv(lib.EVENTS_CSV)
    df["state_std"] = df["state"].map(lib.canonical_state)
    return df


def build_events(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = []
    participants = []

    # --- SPLIT and NEW_DISTRICT: group by (parent_district, state, year) ---
    for event_type in ("SPLIT", "NEW_DISTRICT"):
        sub = df[df["event_type"] == event_type]
        group_cols = ["parent_district", "state_std", "effective_year"]
        for (parent_raw, state, year), group in sub.groupby(group_cols, dropna=False):
            predecessor_names = lib.parse_multi_name(parent_raw)
            successor_names = sorted(set(group["child_district"].dropna().astype(str)))
            sources_cited = sorted(set(group["source"].astype(str)))

            event_id = lib.stable_id("EVENT", event_type, parent_raw, state, year)
            events.append({
                "event_id": event_id,
                "event_type": event_type,
                "state": state,
                "parent_district_raw": parent_raw,
                "effective_year": int(year),
                "n_predecessors": len(predecessor_names),
                "n_successors": len(successor_names),
                "sources_cited": ";".join(sources_cited),
                "n_csv_rows": len(group),
            })
            for name in predecessor_names:
                participants.append({
                    "event_id": event_id, "role": "PREDECESSOR",
                    "district_name_raw": name, "state": state,
                })
            for name in successor_names:
                participants.append({
                    "event_id": event_id, "role": "SUCCESSOR",
                    "district_name_raw": name, "state": state,
                })

    # --- RENAME: already 1:1, one row = one event ---
    ren = df[df["event_type"] == "RENAME"]
    for _, row in ren.iterrows():
        state = row["state_std"]
        year = int(row["effective_year"])
        old_name = str(row["parent_district"])
        new_name = str(row["child_district"])
        event_id = lib.stable_id("EVENT", "RENAME", old_name, state, year)
        events.append({
            "event_id": event_id,
            "event_type": "RENAME",
            "state": state,
            "parent_district_raw": old_name,
            "effective_year": year,
            "n_predecessors": 1,
            "n_successors": 1,
            "sources_cited": str(row["source"]),
            "n_csv_rows": 1,
        })
        participants.append({
            "event_id": event_id, "role": "PREDECESSOR",
            "district_name_raw": old_name, "state": state,
        })
        participants.append({
            "event_id": event_id, "role": "SUCCESSOR",
            "district_name_raw": new_name, "state": state,
        })

    events_df = pd.DataFrame(events).sort_values(
        ["effective_year", "event_type", "state", "parent_district_raw"]
    ).reset_index(drop=True)
    participants_df = pd.DataFrame(participants)
    return events_df, participants_df


def main() -> None:
    df = load_events()
    print(f"Loaded {len(df)} raw event rows")

    events_df, participants_df = build_events(df)

    n_multi_pred = (events_df["n_predecessors"] > 1).sum()
    n_multi_succ = (events_df["n_successors"] > 1).sum()
    dup_ids = events_df["event_id"].duplicated().sum()

    print(f"Aggregated to {len(events_df)} true events (from {len(df)} CSV rows)")
    print(f"  multi-predecessor events: {n_multi_pred}")
    print(f"  multi-successor events:   {n_multi_succ}")
    print(f"  duplicate event_ids:      {dup_ids}")
    print(events_df["event_type"].value_counts())

    if dup_ids:
        raise RuntimeError("Duplicate deterministic event_ids — natural key collision")

    events_df.to_parquet(lib.GOLD_EVENTS_DIR / "boundary_event.parquet")
    participants_df.to_parquet(lib.GOLD_EVENTS_DIR / "event_participant.parquet")
    events_df.to_csv(lib.OUTPUT_DIR / "s3_boundary_event.csv", index=False)

    print(f"\nWrote {lib.GOLD_EVENTS_DIR / 'boundary_event.parquet'} ({len(events_df)} rows)")
    print(f"Wrote {lib.GOLD_EVENTS_DIR / 'event_participant.parquet'} ({len(participants_df)} rows)")

    # Sanity: every CSV row must be represented in exactly one event's
    # participant set (no event, no data loss).
    csv_children = set(df["child_district"].dropna().astype(str))
    ev_successors = set(participants_df.loc[participants_df.role == "SUCCESSOR", "district_name_raw"])
    missing = csv_children - ev_successors
    if missing:
        print(f"  WARNING: {len(missing)} child names from CSV not represented as successors: "
              f"{sorted(missing)[:10]}")


if __name__ == "__main__":
    main()
