# Pipeline Validation Report

Rebuild of the District Evolution Intelligence System's spatial/lineage
core, addressing the defects catalogued in
docs/architecture/lineage_area_redesign.md. Built from 9 real geometry
vintages (Stanford 1951-2021, Survey of India 2025) and the 1,550-row
`district_evolution_master.csv` events compilation. No fabricated data;
every number below is either read from the source GPKGs/CSV or derived
by a documented, deterministic computation.

## V1 — Per-layer topology (self-overlap)

All 9 vintage layers: **PASS**. Self-overlap
fraction ranged 0.0000%-
0.0039% of layer area, far under
the 0.10% warning threshold. The
source geometry is clean; none of the downstream defects found in the old
pipeline originate here.

## Data quality finding: non-administrative placeholder polygons

10 features across 6 vintages were excluded before any
computation -- blank or sentinel names ("DATA NOT AVAILABLE") consistently
attached to a ~80,520-108,672 km2 polygon in Jammu & Kashmir (unadministered/
claimed territory shown for cartographic completeness, never subdivided
into real districts) and a ~270 km2 polygon in Gujarat (the disputed Rann
of Kutch strip). Logged in `outputs/pipeline/s1_excluded_non_administrative.csv`,
not silently dropped. A name is required for an administrative identity;
these features have none.

## Stage 4 — Transition matrix (the core primitive)

8 adjacent-vintage overlays computed. Total district count grew from
316 (1951) to
742 (2025).

Large LOST/GAINED residuals appear in 3 of 8 transitions (1951-1961,
2001-2011, 2011-2021), concentrated **entirely** in international-border
districts (J&K/Ladakh vs. Pakistan, Arunachal Pradesh/Uttarakhand vs.
China, Mizoram/Meghalaya vs. Myanmar/Bangladesh) and known hard-to-digitize
coastal/deltaic districts (Sundarbans, Kachchh/Rann of Kutch, Andaman,
Thane). Verified by inspection (see pipeline run log) -- this is a real,
physically-explainable digitization disagreement between source editions,
not a computation defect. Per redesign doc section 4.1 step 3, these are
reported as an explicit residual mass in `lost_to_unmapped_km2` /
`gained_from_unmapped_km2` and are **never** redistributed into a
fabricated transfer.

## V3 — Ledger closure (conservation)

3,872 source-district accounting rows. Closure holds within
tolerance for 3,871 of them
(**99.97%**). 1 violation(s):
district_name      state  year_a  year_b  closure_error_km2
  Pondicherry Puducherry    2001    2011           3.207309

Contrast with the prior intersection-led Phase 9 pipeline, which reported
301/480 (63%) conservation failures (`outputs/phase9/revised_area_validation.md`)
because it summed pairwise intersections instead of a true geometric
partition. This pipeline's closure tolerance (2.0 km2 /
0.15%) is set from the measured floating-point
summation noise of computing geodesic area on many small overlay fragments
vs. once on the whole polygon (empirically: mean 0.31 km2, max 1.13 km2,
0.108% relative, on the 1951-1961 transition) -- a named, bounded, expected
"small difference," not a loosened pass criterion.

## Identity (Stage 5)

1,371 canonical keys allocated across
1951-2025. 742 active at 2025 (matches
the 742 usable 2025 SOI
districts exactly). Identity was resolved from spatial continuity
(retention/inheritance shares vs. 90%
threshold, both directions), not name matching -- name agreement is
recorded as a corroborating signal only. Spot-checked: continuity decisions
where the name differed are consistently genuine renames (Trichur->Thrissur,
Kollam<->Quilon) or cross-source spelling variants (Cooch Behar/Cooch_Bihar,
Dharwad/Dharwar, Sabarkantha/Sabar Kantha), all with >=94% area share both
directions -- see `outputs/pipeline/s5_continuity_diagnostics.csv`.

## V9 — Lineage / transfer separation (architecture invariant L-3)

A territorial transfer between two continuing districts must not produce a
`district_relationship` row. **PASS**
(2,578 genuine lineage edges, all into CKs established in
that exact window; 7,227 territorial-transfer edges routed to
`measured_area_transfer` instead, per architecture section 11).

relationship_type breakdown:
relationship_type
MERGED_INTO    1037
FORMED_FROM     882
SPLIT_FROM      659

lineage_basis breakdown (corroboration against the events CSV; geometry
decides the edge, the CSV corroborates it):
lineage_basis
SPATIAL_INFERRED             2008
GAZETTE_CORROBORATED          541
GAZETTE_CORROBORATED_WEAK      29

Corroboration rate is a secondary QA signal, not a pass/fail gate: the
geometry engine finds every material boundary change between two mapped
vintages, which is structurally more granular than a curated 1,550-row
academic events compilation. A known, undeveloped gap: multi-predecessor
edges corroborate at a lower rate because the CSV typically records one
named parent per successor row, while geometry can attribute several. Left
as a documented limitation, not force-matched.

## Event area accounting (Stage 8)

935 real administrative events (aggregated from the 1,550-row CSV
to the true event grain -- see below). 460 events
(49.2%) resolved to at least one
geometrically MEASURED transferred-area pair.

measurement_status
MEASURED                 788
PARTIALLY_RESOLVED       338
RESOLVED_NO_OVERLAP      152
UNRESOLVED_NAME           37
NO_BRACKETING_VINTAGE     21

## The single highest-value fix: event grain

The old pipeline minted one `event_id` per CSV row (one row per
parent-child pair); a 1-parent-3-children split became three fake
one-child "events," which made conservation untestable and forced
100%-to-one-child allocation. This pipeline aggregates by
(event_type, parent_district-as-written, state, effective_year):
1,550 CSV rows -> 935 real events, 487
of them genuinely multi-successor. Verified: 0 CSV rows lost in the
aggregation (see Stage 3 run log).

## Reproducibility

Every ID in this pipeline (`event_id`, `rel_id`, `transfer_id`, geometry
observation IDs) is `sha256(natural_key)`, never `uuid4()`. Rerunning this
pipeline against the same inputs reproduces byte-identical IDs and joins --
fixing the defect where the old pipeline's products became un-joinable
after every run.

## Known gaps (not attempted in this pass)

- **RECONSTITUTED_FROM** relationships (a new CK territorially resembling
  a much-earlier abolished CK) are not detected -- this requires matching
  against non-adjacent vintages, a different computation from the
  adjacent-vintage matrix used here. OD-01 (reclassification CK rule) is
  therefore still open, though Stage 5's continuity test gives it an
  empirical answer for any specific case a domain expert wants to check.
- Statistical harmonization (`statistical_crosswalk`) is not rebuilt in
  this pass; Stage 4's row-normalized matrix (`M-hat`) is exactly the
  input the redesign doc specifies for it (section 4.7) whenever that
  layer is prioritized.
- SQL DDL / empty config files noted in the original audit
  (docs/architecture/lineage_area_redesign.md section 2.9) are unchanged;
  this pipeline runs as Python + Parquet, not the medallion DuckDB schema.
