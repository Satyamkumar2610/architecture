# Implementation Status

## Pipeline Stages

| Stage | Script | Status | Description |
|---|---|---|---|
| S1 | s1_bronze_to_silver.py | ✅ Complete | Bronze → Silver geometry standardisation |
| S2 | s2_topology_audit.py | ✅ Complete | Per-vintage topology audit (V1 check) |
| S3 | s3_events_aggregate.py | ✅ Complete | Events CSV → true administrative event grain |
| S4 | s4_transition_matrix.py | ✅ Complete | Planar overlay → conservation-by-construction transition matrix |
| S5 | s5_identity.py | ✅ Complete | Continuity-based CK allocation |
| S6 | s6_lineage.py | ✅ Complete | Lineage classification + measured_area_transfer |
| S7 | s7_area_ledger.py | ✅ Complete | Per-CK area ledger (outbound + inbound) |
| S8 | s8_event_area_accounting.py | ✅ Complete | Lightweight event-level area lookup |
| S10 | s10_event_area_transfer_matrix.py | ✅ Complete | **Event Area Transfer Matrix & Territorial Reconciliation Engine** |
| S9 | s9_validate_and_report.py | ✅ Complete | Final validation + QC report (references S10) |

## Architecture Compliance

| Principle | Status |
|---|---|
| Events as evidence, not spatial truth (Principle 7) | ✅ S10: dual admin/spatial classification; never conflated |
| No silent area loss | ✅ S10: raw_residual always recorded; never normalized to zero |
| Declared ≠ measured | ✅ S10: declared_transfer (NULL) and measured_transfer stored separately |
| Deterministic IDs | ✅ All stages: sha256 natural keys, never uuid4 |
| One area method everywhere | ✅ lib.geodesic_area_km2 (pyproj.Geod WGS84) used exclusively |
| Conservation by construction | ✅ S4: planar partition; S7+S10: verified via closure equation |
| No normalization | ✅ S10: residual receives reason code, never forced to zero |
| Configurable thresholds | ✅ config/reconciliation.yaml (S10), lib.py (global) |
| QGIS-inspectable outputs | ✅ S10: event_residuals.gpkg with 4 layers |

## Known Open Items

| Item | Priority | Notes |
|---|---|---|
| OD-01: reclassification CK rule | Blocking open decision | S5 continuity test gives empirical answer per specific case |
| RECONSTITUTED_FROM relationships | Non-blocking | Requires non-adjacent-vintage matching; not yet implemented |
| Statistical crosswalk rebuild | Non-blocking | S4 M-hat is the ready input; S13 harmonization stage not yet built |
| SQL DDL for invariants | Non-blocking | Pipeline runs as Python + Parquet; DuckDB schema not populated |
| gazette declared_transfer | Non-blocking | declared_transfer field present (NULL); awaits gazette data integration |

## Output Locations

| Product | Path |
|---|---|
| Core pipeline outputs | `outputs/pipeline/` |
| Event area transfer matrix | `outputs/event_transfer/event_area_transfer_matrix.{csv,parquet}` |
| Event accounting summary | `outputs/event_transfer/event_area_accounting_summary.{csv,parquet}` |
| Residual audit | `outputs/event_transfer/residual_area_audit.csv` |
| Event narratives | `outputs/event_transfer/event_narratives.{json,md}` |
| QGIS spatial audit | `outputs/event_transfer/event_residuals.gpkg` |
| S10 QC report | `outputs/event_transfer/s10_qc_report.md` |
| Final validation report | `outputs/pipeline/s9_validation_report.md` |
