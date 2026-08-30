#!/usr/bin/env bash
# Runs the full District Evolution Intelligence pipeline (v2) end to end.
# Reads data/bronze/{stanford,soi,events}/... only; writes to
# data/silver/geometry/, data/gold/{core,spatial,events}/, data/products/,
# and outputs/pipeline/. See docs/architecture/lineage_area_redesign.md for
# the design this implements.
set -euo pipefail
cd "$(dirname "$0")/../.."
source .venv/bin/activate

for stage in \
  s1_bronze_to_silver \
  s2_topology_audit \
  s3_events_aggregate \
  s4_transition_matrix \
  s5_identity \
  s6_lineage \
  s7_area_ledger \
  s8_event_area_accounting \
  s9_validate_and_report
do
  echo ""
  echo "=================================================================="
  echo "  STAGE: ${stage}"
  echo "=================================================================="
  python3 "scripts/pipeline/${stage}.py"
done

echo ""
echo "Pipeline complete. See outputs/pipeline/s9_validation_report.md"
