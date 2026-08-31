# Implementation Status

## Pipeline Stages

| Stage | Script | Status | Description |
|---|---|---|---|
| S1 | s1_bronze_to_silver.py | ✅ Complete | Bronze → Silver geometry standardisation |
| S2 | s2_topology_audit.py | ✅ Complete | Per-vintage topology audit |
| S3 | s3_events_aggregate.py | ✅ Complete | Events CSV → administrative event grain |
| S4 | s4_transition_matrix.py | ✅ Complete | Planar overlay → transition matrix |
| S5 | s5_identity.py | ✅ Complete | Continuity-based CK allocation |
| S6 | s6_lineage.py | ✅ Complete | Lineage classification |
| S7 | s7_area_ledger.py | ✅ Complete | Per-CK area ledger |
| S8 | s8_event_area_accounting.py | ✅ Complete | Event-level area lookup |
| S10 | s10_event_area_transfer_matrix.py | ✅ Complete | **Event Area Transfer Matrix & Territorial Reconciliation Engine** |
| S10b | s10b_spatial_successor_discovery.py | ✅ Complete | **Spatial Successor Discovery & Residual Recovery** |
| S9 | s9_validate_and_report.py | ✅ Complete | Final validation + QC report |

## Architecture Compliance

| Principle | Status |
|---|---|
| Events as evidence, not spatial truth | ✅ S10/S10b: dual admin/spatial classification; never conflated |
| No silent area loss | ✅ raw_residual always recorded; initial_residual_pct never overwritten |
| Declared ≠ measured | ✅ declared_transfer/measured_transfer separate; admin_confidence ≠ spatial_confidence |
| Deterministic IDs | ✅ sha256 natural keys throughout |
| One area method | ✅ lib.geodesic_area_km2 (pyproj.Geod WGS84) exclusively |
| Conservation by construction | ✅ S4 planar partition; S10b: raw = recovered + unresolved |
| No normalization | ✅ residual receives reason code; never forced to zero |
| Configurable thresholds | ✅ config/reconciliation.yaml — all S10b weights externalized |
| QGIS-inspectable outputs | ✅ event_residuals.gpkg (S10); event_spatial_candidates.gpkg (S10b, with geometry) |
| No lineage modification by S10b | ✅ S10b does NOT write to district_relationship or district_area_ledger |
| Two-registry alias safety | ✅ SPATIAL_DISCOVERY alone never promotes to authoritative registry |
| S10 output immutability | ✅ residual_area_audit_s10.csv preserved read-only |

## Known Open Items

| Item | Priority | Notes |
|---|---|---|
| OD-01: reclassification CK rule | Blocking open decision | S5 continuity test gives empirical answer per case |
| RECONSTITUTED_FROM relationships | Non-blocking | Requires non-adjacent-vintage matching |
| Statistical crosswalk rebuild | Non-blocking | S4 M-hat ready; S13 harmonization not yet built |
| SQL DDL for invariants | Non-blocking | Pipeline runs as Python + Parquet; DuckDB not populated |
| gazette declared_transfer | Non-blocking | Field present (NULL); awaits gazette data integration |
| S10b identity mismatch review | Non-blocking | Research findings requiring domain expert verification |
| Alias candidate promotion | Non-blocking | 39 SPATIAL_DISCOVERY entries need documentary verification |

## Output Locations

| Product | Path |
|---|---|
| Event area transfer matrix | `outputs/event_transfer/event_area_transfer_matrix.{csv,parquet}` |
| Event accounting summary (S10) | `outputs/event_transfer/event_area_accounting_summary.{csv,parquet}` |
| Event accounting summary (S10b) | `outputs/event_transfer/event_area_accounting_summary_s10b.{csv,parquet}` |
| S10 audit — original READ-ONLY | `outputs/event_transfer/residual_area_audit_s10.csv` |
| S10b audit — augmented | `outputs/event_transfer/residual_area_audit_s10b.csv` |
| Latest audit (convenience) | `outputs/event_transfer/residual_area_audit.csv` |
| Spatial successor candidates | `outputs/event_transfer/spatial_successor_candidates.{csv,parquet}` |
| Event narratives (S10) | `outputs/event_transfer/event_narratives.{json,md}` |
| Event narratives (S10b) | `outputs/event_transfer/event_narratives_s10b.{json,md}` |
| QGIS spatial audit (S10) | `outputs/event_transfer/event_residuals.gpkg` |
| QGIS spatial audit (S10b) | `outputs/event_transfer/event_spatial_candidates.gpkg` |
| Reconciliation report | `outputs/event_transfer/TERRITORIAL_RECONCILIATION_REPORT.md` |
| Alias candidates | `data/gold/core/name_alias_candidates.parquet` |
| Alias registry (approved) | `data/gold/core/name_alias_registry.parquet` |
| S10 QC report | `outputs/event_transfer/s10_qc_report.md` |
| Final validation report | `outputs/pipeline/s9_validation_report.md` |
