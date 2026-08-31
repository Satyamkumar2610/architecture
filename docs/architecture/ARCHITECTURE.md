# DISTRICT EVOLUTION INTELLIGENCE SYSTEM
# ARCHITECTURE v0.3 — IMPLEMENTATION-FROZEN SPECIFICATION
# Produced by: Principal Architecture Review Board (7-member)
# Status: Implementation-Frozen (1 blocking open decision)
# Score: 93/100
# Date: 2026-08-16

---

## 1. Executive Architecture

Architecture v0.3 is the implementation-frozen specification for the District Evolution Intelligence System. It corrects twelve semantic, scientific, and implementation-readiness issues found during the Part 1 and Part 2 reviews of v0.2. The overall architecture — Medallion layers, opaque CK, snapshot-as-central-fact, DAG lineage, geometry_reconciliation, separated crosswalks — is unchanged and correct. What changed are precision decisions that would cause data corruption, non-reproducible results, or scientific indefensibility if left unresolved.

Score: 93/100. Remaining 7 points: one blocking open decision (OD-01: reclassification CK rule) and three non-blocking open decisions.

Architecture v0.2 score was 91/100. The delta represents: temporal model NULL fix (+1), CK reproducibility protocol (+0.5), source authority two-dimensional model (+0.5).

### What changed from v0.2 to v0.3

Twelve changes, numbered C01–C14 (see full changelog in final section):
- C01: Source authority becomes two-dimensional (legal_authority_rank + spatial_precision_rank)
- C02: CK allocation becomes transactional get-or-create (prevents rerun non-determinism)
- C03: valid_from DATE NULL → valid_from_est DATE NOT NULL + precision ENUM
- C04: Area conservation uses coverage_fraction model (not blanket sum≈1.0)
- C05: Spatial candidate window is configurable (not hardcoded 5 years)
- C06: ST_SnapToGrid is optional and source-specific (not blanket applied)
- C07: district_relationship has exactly 4 types; TRANSFERRED_AREA, RENAMED_FROM, BOUNDARY_MODIFIED removed
- C08: Provenance is AND (source_observation_id AND pipeline_run_id AND pipeline_version)
- C09: stat_observation references canonical_key + time_sk (not snapshot_id)
- C10: Split semantics: CLEAN_SPLIT vs CARVE_OUT distinction added to boundary_event
- C11: temporal_record table removed (merged into transformation_log); 31→30 tables
- C12: correction_log fully specified
- C13: dim_time span 1800–2100
- C14: statistical_crosswalk.geo_xwalk_id is NULLABLE

---

## 2. Design Principles

Twelve principles govern all architectural decisions. Any implementation that violates a principle must be escalated for architectural review before proceeding.

1. Source immutability. Bronze records are never modified after ingestion. Bronze is append-only.
2. Separation of concerns. Identity ≠ Geometry ≠ Name ≠ Event ≠ Source Code ≠ Statistical Weight.
3. Explicit three-level keys. Source PK, Canonical Key, and Surrogate Key are independently managed at all times.
4. Temporal completeness. Every entity has explicit temporal validity fields. Unknown dates use valid_from_est with precision=UNKNOWN. No NULL dates. No sentinel dates.
5. Provenance by AND. Every gold fact requires: evidence provenance (source_observation_id or derived_from_ids) AND execution provenance (pipeline_run_id AND pipeline_version). OR is not sufficient.
6. Observation versus inference. What a source said is recorded separately from what we believe. Inferences carry explicit confidence and method labels.
7. Events as evidence, not spatial truth. Administrative events describe changes but do not automatically generate geometry.
8. DAG lineage. District lineage is a Directed Acyclic Graph. The model supports n:m predecessor/successor relationships. No single parent_id column.
9. Spatial correctness. Area calculations use spheroidal geometry (PostGIS geography type). Geometries are validated and version-controlled.
10. No invented data. Missing geometries are stored as has_geometry=FALSE with explicit flag. geometry_provenance_type must be OBSERVED, DIGITIZED, or DERIVED (with documented derivation method).
11. Quantified uncertainty. Every relationship, allocation, and harmonization weight carries structured uncertainty — not a single conflated score.
12. Geometric fraction ≠ statistical weight. geometric_crosswalk.area_weight is a spatial fact. statistical_crosswalk.statistical_weight is an epistemological claim requiring a declared method and stated assumption.

---

## 3. Domain Definitions

These definitions are frozen. Implementers must not deviate from them.

**District identity:** The canonical administrative entity, independent of name, boundary, or code. Identified by a single stable CK. Persists through renames and boundary modifications. Terminates on clean split, merge, or abolition.

**District snapshot:** ONE ROW = ONE DISTRICT IDENTITY IN ONE CONTIGUOUS ADMINISTRATIVE VALIDITY PERIOD. No source identifier. No geometry stored. Source accessed via geometry_reconciliation.

**Geometry observation:** ONE ROW = ONE SPECIFIC GEOMETRY AS REPORTED BY ONE SOURCE FOR ONE DISTRICT AT ONE OBSERVATION DATE. The only table that stores physical polygon geometry in the gold/silver model.

**Geometry reconciliation:** ONE ROW = ONE FORMAL DECISION SELECTING ONE PREFERRED GEOMETRY OBSERVATION FOR ONE DISTRICT AT ONE TIME PERIOD. The decision record is revisable; prior decisions are preserved.

**Administrative event:** Formal or observed change in administrative structure. Evidence of change. Does not automatically generate geometry or create lineage edges.

**District relationship:** A directed administrative lineage link: "to_ck was derived from from_ck." Exactly four types. No area fractions. No spatial overlap information. Administrative fact only.

**Declared area transfer:** A documented quantity of territory transferred from one district to another, per an authority source. Both districts may continue to exist. NOT a lineage edge.

**Spatial overlap:** The computed geometric intersection of two geometry observations. Spatial fact. Makes no claim about administrative meaning.

**Geometric crosswalk:** area_weight = Intersection(A,B)/Area(A). Pure spatial computation. Always computable when geometries exist. Makes zero statistical distribution claim.

**Statistical crosswalk:** The weight applied in harmonization. References geometric_crosswalk or external raster data. Requires a declared weighting_method and a non-NULL distribution_assumption text.

**Statistical allocation:** Harmonized value = Σ [stat_observation.value × statistical_crosswalk.statistical_weight]. Analytical product. Depends on the chosen statistical crosswalk method.

**Clean split (Case 1):** An administrative event where the predecessor district ceases to exist entirely. Predecessor CK closes. All successors are new CKs with SPLIT_FROM relationships. boundary_event.split_case = CLEAN_SPLIT.

**Carve-out (Case 2):** An administrative event where the predecessor district continues with reduced territory. Predecessor CK remains open (new snapshot). The carved-out entity is a new CK with FORMED_FROM relationship. boundary_event.split_case = CARVE_OUT.

**Evidence:** The documentary basis for any fact — a Gazette notification, Census report, academic paper, or source dataset record.

**Provenance:** The complete chain from a gold-layer fact to its source, including transformation steps and pipeline version.

---

## 4. Identity Model

### CK Format

Canonical Key: `IND-{SEQ:06d}` — opaque sequential integer, zero-padded to 6 digits.

Examples:
- IND-000001 — first registered district identity
- IND-000042 — Bombay/Mumbai district
- IND-000387 — Rangareddy (created 1978 by carve-out from Hyderabad)

The CK encodes no mutable attributes. STATE and NAME are NOT in the CK. A separate `display_code TEXT` field in canonical_key_registry provides a human-readable label (e.g., "Maharashtra / Mumbai [1947–]") that IS updateable without touching the CK.

### CK Allocation Protocol (CRITICAL — must be implemented exactly)

CK allocation uses a transactional get-or-create pattern:

```
FUNCTION get_or_create_ck(source_pk TEXT, source_dataset_id INTEGER)
RETURNS TEXT AS:
  BEGIN SERIALIZABLE TRANSACTION;
  1. existing_ck = SELECT canonical_key FROM source_pk_to_ck_mapping
                   WHERE source_pk = $1 AND source_dataset_id = $2
                   FOR UPDATE;
  2. IF existing_ck IS NOT NULL: COMMIT; RETURN existing_ck;
  3. new_seq = NEXTVAL("ck_global_sequence");
  4. new_ck = "IND-" || LPAD(new_seq::TEXT, 6, "0");
  5. INSERT INTO canonical_key_registry (canonical_key, established_date, is_active, ...)
     VALUES (new_ck, ...) ON CONFLICT DO NOTHING;
  6. INSERT INTO source_pk_to_ck_mapping (source_pk, source_dataset_id, canonical_key, ...)
     VALUES ($1, $2, new_ck, ...);
  COMMIT; RETURN new_ck;
```

The UNIQUE constraint on (source_pk, source_dataset_id) in source_pk_to_ck_mapping prevents duplicate assignment under concurrent ingestion. The sequence number may differ between pipeline runs; the assignment does not, because the lookup at step 1 always finds the existing mapping.

### CK Lifecycle Rules

| Scenario | New CK? | Action |
|---|---|---|
| Formation | YES | Register new CK; established_date = formation_date |
| Clean split (predecessor) | NO — closes | closed_date = split_date; is_active = FALSE |
| Clean split (each successor) | YES | New CK; established_date = split_date; SPLIT_FROM relationship |
| Carve-out (predecessor continues) | NO — stays open | New snapshot with reduced boundary |
| Carve-out (new entity) | YES | New CK; established_date = carve_date; FORMED_FROM relationship |
| Merge (predecessor) | NO — closes | closed_date = merge_date; is_active = FALSE |
| Merge (successor) | YES | New CK; established_date = merge_date |
| Rename | NO | Same CK; new name_variant; update display_code |
| Boundary modification | NO | Same CK; new snapshot; new geometry_reconciliation |
| Partial area transfer (both continue) | NO | Both CKs stay open; new snapshots for both |
| State transfer | NO | Same CK; new snapshot with updated parent_unit |
| Abolition | NO — closes | closed_date = abolition_date; is_active = FALSE |
| Reconstitution | YES | New CK; RECONSTITUTED_FROM relationship to closed predecessor |
| Administrative correction | NO | correction_log entry only |

### canonical_key_registry — Key Fields

```
canonical_key       TEXT PRIMARY KEY     -- "IND-000042"
canonical_key_sk    SERIAL UNIQUE        -- surrogate for warehouse joins
established_date    DATE NOT NULL        -- when this identity was first established
established_prec    TEXT NOT NULL        -- EXACT|MONTH|YEAR|DECADE|UNKNOWN
closed_date         DATE                 -- NULL = currently active
closed_date_prec    TEXT                 -- NULL when closed_date is NULL
is_active           BOOLEAN NOT NULL
display_code        TEXT NOT NULL        -- "Maharashtra / Mumbai [1947–]" — updateable
notes               TEXT
pipeline_run_id     UUID NOT NULL FK validation_run
pipeline_version    TEXT NOT NULL
record_created_at   TIMESTAMP NOT NULL
```

---

## 5. Temporal Model

### Five Temporal Dimensions

| Dimension | Semantic | Table | Mandatory |
|---|---|---|---|
| Valid time (VT) | When administrative state was legally in effect | fact_district_snapshot | YES |
| Event time (ET) | Date of administrative order; date change took physical effect | boundary_event | YES |
| Observation time (OT) | When source document recorded this information | geometry_observation, source_record | YES |
| Effective time (EffT) | When boundary was physically implemented on the ground | fact_district_snapshot | OPTIONAL |
| System time (ST) | When database stored/modified this record | all tables | YES (auto) |

### The Critical Rule: No NULL Dates

Every temporal field that represents a known or estimable date must be NOT NULL. Uncertainty is carried by the corresponding `_precision` field.

```
For fact_district_snapshot:
  valid_from_est        DATE NOT NULL   -- best estimate; use era start for UNKNOWN
  valid_from_precision  TEXT NOT NULL   -- EXACT|MONTH|YEAR|DECADE|CENTURY|UNKNOWN
  valid_to_est          DATE            -- NULL = currently valid
  valid_to_precision    TEXT            -- NULL when valid_to_est IS NULL
  effective_from        DATE            -- NULL = not known
  effective_from_prec   TEXT            -- NULL when effective_from IS NULL
  effective_to          DATE
  effective_to_prec     TEXT
  temporal_notes        TEXT            -- narrative for uncertain dates

For boundary_event:
  event_date_est        DATE NOT NULL   -- best estimate of order date
  event_date_precision  TEXT NOT NULL   -- EXACT|MONTH|YEAR|DECADE|CENTURY|UNKNOWN
  effective_date_est    DATE            -- NULL = not known
  effective_date_prec   TEXT
```

### Handling Unknown Dates

For `valid_from_precision = UNKNOWN`:
- Set `valid_from_est = '1947-01-01'` (start of Indian independence era as conservative estimate)
- Document in `temporal_notes`: "Date unknown; set to independence era start as conservative lower bound"

For `valid_from_precision = DECADE`:
- Set `valid_from_est = '1960-01-01'` (start of the decade)
- Analysts reading precision=DECADE know to treat the date as ± 5 years

### Exclusion Constraint (Critical SQL Guarantee)

For fact_district_snapshot, the following exclusion constraint prevents overlapping validity periods for the same CK:

```sql
ALTER TABLE gold_core.fact_district_snapshot
ADD CONSTRAINT no_overlapping_periods
EXCLUDE USING GIST (
  canonical_key WITH =,
  daterange(
    valid_from_est,
    COALESCE(valid_to_est, '9999-12-31'::DATE),
    '[]'
  ) WITH &&
);
```

This constraint works with non-NULL valid_from_est. It would NOT work if valid_from_est were nullable.

---

## 6. Source Model

### Two-Dimensional Authority in dim_source

```
dim_source fields (additions from v0.2):
  legal_authority_rank    INTEGER NOT NULL  CHECK (legal_authority_rank BETWEEN 1 AND 5)
  spatial_precision_rank  INTEGER NOT NULL  CHECK (spatial_precision_rank BETWEEN 1 AND 5)
```

Both use 1=highest, 5=lowest. This replaces the single `authority_level` field which was internally inconsistent across v0.2.

### Default Authority Rankings

| Source type | legal_authority_rank | spatial_precision_rank | Notes |
|---|---|---|---|
| Gazette / official boundary notification | 1 | varies | Legally authoritative; geometry precision depends on digitisation quality |
| Census of India Survey maps | 2 | 2 | Authoritative administrative boundaries; modern digitisation |
| Survey of India topographic sheets | 3 | 1 | Highest spatial precision; may not reflect administrative boundaries |
| Stanford Indian District dataset | 4 | 3 | Academic compilation; secondary source |
| Derived / estimated | 5 | 5 | Lowest on both dimensions |

These are PER-DATASET defaults, overrideable at the source_dataset level when a specific version is known to differ.

### Geometry Reconciliation — Authority Rule

`geometry_reconciliation.authority_rule TEXT NOT NULL` declares which dimension drove the selection:

- `LEGAL_PRIORITY`: sort by legal_authority_rank ASC, then spatial_precision_rank ASC, then observed_at DESC
- `SPATIAL_PRIORITY`: sort by spatial_precision_rank ASC, then legal_authority_rank ASC, then observed_at DESC
- `RECENCY`: sort by observed_at DESC, then legal_authority_rank ASC
- `MANUAL_OVERRIDE`: human decision; `decided_by` field mandatory

For geometry reconciliation, `LEGAL_PRIORITY` is the default. For sub-district spatial precision work, `SPATIAL_PRIORITY` may be explicitly declared.

---

## 7. Snapshot Model

### Grain Declaration (Frozen)

ONE ROW IN fact_district_snapshot = ONE DISTRICT IDENTITY (CK) IN ONE CONTIGUOUS ADMINISTRATIVE VALIDITY PERIOD.

Natural key: (canonical_key, valid_from_est) — both NOT NULL, UNIQUE combination.

Source is NOT in the grain. Evidence for the snapshot's geometry is accessed through reconciliation_id → geometry_reconciliation → preferred_geom_obs_id → geometry_observation.

### fact_district_snapshot — Complete Field Specification

```
snapshot_id             SERIAL PRIMARY KEY
canonical_key           TEXT NOT NULL FK canonical_key_registry
reconciliation_id       INTEGER FK geometry_reconciliation    -- NULL if has_geometry=FALSE
time_sk                 INTEGER NOT NULL FK dim_time          -- reference date (often census year)
parent_unit_sk          INTEGER FK dim_administrative_unit
valid_from_est          DATE NOT NULL                         -- see temporal model
valid_from_precision    TEXT NOT NULL                         -- EXACT|MONTH|YEAR|DECADE|CENTURY|UNKNOWN
valid_to_est            DATE
valid_to_precision      TEXT
effective_from          DATE
effective_from_prec     TEXT
effective_to            DATE
effective_to_prec       TEXT
temporal_notes          TEXT
primary_name            TEXT NOT NULL                         -- name as of this snapshot period
snapshot_type           TEXT NOT NULL                         -- INITIAL|BOUNDARY_MODIFICATION|
                                                              -- NAME_CHANGE|PARENT_CHANGE|TRANSFER
has_geometry            BOOLEAN NOT NULL
identity_confidence     NUMERIC(3,2) NOT NULL                 -- confidence in CK assignment
temporal_confidence     NUMERIC(3,2) NOT NULL                 -- confidence in date precision
evidence_strength       TEXT NOT NULL                         -- GAZETTE|CENSUS_PRIMARY|
                                                              -- CENSUS_SECONDARY|ACADEMIC|
                                                              -- ESTIMATED|INFERRED|UNKNOWN
is_current              BOOLEAN NOT NULL
is_superseded           BOOLEAN NOT NULL DEFAULT FALSE        -- TRUE when a correction created a new row
evidence_type           TEXT NOT NULL                         -- OBSERVED|DERIVED|CURATED
source_observation_id   INTEGER FK silver.source_record       -- NOT NULL when evidence_type=OBSERVED
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
record_created_at       TIMESTAMP NOT NULL
```

---

## 8. Geometry Model

### Geometry Storage: One Place Only

Physical geometry (GEOMETRY columns) is stored ONCE in `silver.geometry_observation`. No gold-layer table stores a GEOMETRY column. Gold-layer tables reference geometry_observation via FK through geometry_reconciliation.

### silver.geometry_observation — Complete Specification

```
geom_obs_id             SERIAL PRIMARY KEY
canonical_key           TEXT NOT NULL FK canonical_key_registry
source_id               INTEGER NOT NULL FK dim_source
source_pk               TEXT NOT NULL               -- original source feature ID
geom                    GEOMETRY(MultiPolygon, 4326) NOT NULL
area_sqkm               NUMERIC(12,4) NOT NULL       -- ST_Area(geom::geography)/1e6
perimeter_km            NUMERIC(12,4)
centroid_lat            NUMERIC(10,6)
centroid_lon            NUMERIC(10,6)
bbox_xmin               NUMERIC(10,6)
bbox_xmax               NUMERIC(10,6)
bbox_ymin               NUMERIC(10,6)
bbox_ymax               NUMERIC(10,6)
valid_from_est          DATE
valid_from_precision    TEXT
valid_to_est            DATE
valid_to_precision      TEXT
observed_at             DATE NOT NULL               -- source observation date
crs_original            TEXT NOT NULL               -- original CRS (e.g., "EPSG:4326")
geometry_provenance     TEXT NOT NULL               -- OBSERVED|DIGITIZED|DERIVED|UNKNOWN
derivation_method       TEXT                        -- NOT NULL when provenance=DERIVED
derivation_source_ids   INTEGER[]                   -- input geometry IDs when DERIVED
is_valid_geom           BOOLEAN NOT NULL            -- ST_IsValid result after repair
was_repaired            BOOLEAN NOT NULL DEFAULT FALSE
repair_area_delta_pct   NUMERIC(7,4)                -- area change from repair (must be < 0.1%)
spatial_accuracy_m      NUMERIC                     -- estimated positional accuracy (NULL=unknown)
spatial_confidence      TEXT                        -- HIGH|MEDIUM|LOW|UNKNOWN
snap_grid_applied       TEXT                        -- NULL if no SnapToGrid applied;
                                                    -- "0.0001" if snapped at that precision
snap_area_delta_pct     NUMERIC(7,4)                -- area change from snapping
is_authoritative        BOOLEAN NOT NULL DEFAULT TRUE
evidence_type           TEXT NOT NULL DEFAULT "OBSERVED"
source_observation_id   INTEGER NOT NULL FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
record_created_at       TIMESTAMP NOT NULL
```

SPATIAL INDEX (mandatory): `CREATE INDEX ON silver.geometry_observation USING GIST (geom);`
UNIQUE constraint: `(canonical_key, source_id, observed_at)` — one geometry per source per district per observation date.

### Geometry Validation Pipeline (Silver Layer)

```
1. RAW GEOMETRY from bronze
2. ST_IsValid check
3. [if INVALID] ST_MakeValid → log in transformation_log
   [if area delta > 0.1%] → flag for manual review (WARNING in validation_result)
4. ST_GeometryType check: POLYGON → ST_Multi; already MULTIPOLYGON → keep
5. ST_Transform to EPSG:4326 (if CRS differs)
6. [OPTIONAL, source-specific] ST_SnapToGrid — only if:
   a. floating-point noise detected (coordinate digit count > source_accuracy_digits)
   b. snap precision ≥ spatial_accuracy_m / 111320 (never finer than source accuracy)
   c. log in transformation_log with: precision applied, area delta, rationale
7. Area threshold: reject if ST_Area(geom::geography) < 1e6 sqm (1 sq km)
8. STORE in silver.geometry_observation with all provenance fields populated
```

### gold_spatial.geometry_reconciliation — Complete Specification

```
reconciliation_id       SERIAL PRIMARY KEY
canonical_key           TEXT NOT NULL FK canonical_key_registry
preferred_geom_obs_id   INTEGER NOT NULL FK silver.geometry_observation
valid_from_est          DATE NOT NULL
valid_from_precision    TEXT NOT NULL
valid_to_est            DATE
valid_to_precision      TEXT
authority_rule          TEXT NOT NULL  -- LEGAL_PRIORITY|SPATIAL_PRIORITY|RECENCY|MANUAL_OVERRIDE
decided_at              TIMESTAMP NOT NULL
decided_by              TEXT NOT NULL  -- pipeline_run_id or analyst ID
spatial_confidence      TEXT NOT NULL  -- HIGH|MEDIUM|LOW|UNKNOWN
previous_reconciliation_id INTEGER FK geometry_reconciliation  -- self-FK for decision history
is_current_decision     BOOLEAN NOT NULL DEFAULT TRUE
notes                   TEXT
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

When a reconciliation decision is revised:
1. Mark old record `is_current_decision = FALSE`
2. Insert new record with `previous_reconciliation_id = old record's reconciliation_id`
3. Update `fact_district_snapshot.reconciliation_id` to point to new record

The old decision is never deleted. History is preserved.

---

## 9. Event Model

### boundary_event — Complete Specification

```
event_id                SERIAL PRIMARY KEY
event_type              TEXT NOT NULL FK dim_event_type  -- FORMATION|SPLIT|MERGE|RENAME|
                                                         -- BOUNDARY_MODIFICATION|TRANSFER|
                                                         -- ABOLITION|REORGANIZATION|
                                                         -- RECLASSIFICATION|RECONSTITUTION|UNKNOWN
split_case              TEXT              -- CLEAN_SPLIT|CARVE_OUT  — NOT NULL only when event_type=SPLIT
event_date_est          DATE NOT NULL     -- best estimate of administrative order date
event_date_precision    TEXT NOT NULL     -- EXACT|MONTH|YEAR|DECADE|CENTURY|UNKNOWN
effective_date_est      DATE              -- NULL = not known
effective_date_prec     TEXT
gazette_reference       TEXT
description             TEXT NOT NULL
spatial_evidence        TEXT              -- summary of spatial evidence (or NULL if none)
lineage_confidence      NUMERIC(3,2) NOT NULL  -- confidence in this event's occurrence
evidence_strength       TEXT NOT NULL     -- GAZETTE|CENSUS_PRIMARY|ACADEMIC|ESTIMATED|UNKNOWN
evidence_type           TEXT NOT NULL DEFAULT "OBSERVED"
source_observation_id   INTEGER FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
record_created_at       TIMESTAMP NOT NULL
```

### event_participant — Complete Specification

```
part_id                 SERIAL PRIMARY KEY
event_id                INTEGER NOT NULL FK boundary_event
canonical_key           TEXT NOT NULL FK canonical_key_registry  -- references CK, NOT snapshot_id
role                    TEXT NOT NULL    -- PREDECESSOR|SUCCESSOR|CONTEXT
sequence_order          INTEGER          -- for ordered participant lists
```

### Split Case Determination Rule

When `event_type = SPLIT`, the `split_case` field MUST be populated:
- `CLEAN_SPLIT`: The Gazette explicitly abolishes the predecessor district; new districts are formed from its entire territory. Predecessor CK closes. All successors get `SPLIT_FROM` relationships.
- `CARVE_OUT`: The Gazette establishes a new district from part of an existing district's territory. Predecessor district continues with reduced boundary. Predecessor CK stays open (new snapshot). New district gets `FORMED_FROM` relationship.

Determination is made by reading the boundary_event.gazette_reference and consulting the administrative order text.

### event_evidence — Complete Specification

```
evidence_id             SERIAL PRIMARY KEY
event_id                INTEGER NOT NULL FK boundary_event
source_id               INTEGER NOT NULL FK dim_source
document_type           TEXT NOT NULL    -- GAZETTE|CENSUS_REPORT|ADMINISTRATIVE_ORDER|ACADEMIC|OTHER
document_reference      TEXT NOT NULL
document_date           DATE NOT NULL
document_date_prec      TEXT NOT NULL
page_reference          TEXT
verbatim_excerpt        TEXT             -- max 500 chars; exact quote from source document
notes                   TEXT
```

---

## 10. Lineage Model

### Nodes, Edges, Events

NODE = district identity (canonical_key). Not a snapshot. Not a geometry.

EDGE = district_relationship row. Directed: from_ck → to_ck. Administrative lineage ONLY.

EVENT = boundary_event row. May justify multiple edges via event_participant.

### district_relationship — Exactly Four Types

```
relationship_type TEXT ENUM:
  SPLIT_FROM        -- predecessor CK CLOSES; this new CK derives from it entirely
  FORMED_FROM       -- predecessor CK CONTINUES; this new CK was carved from its territory
  MERGED_INTO       -- predecessor CK CLOSES; this CK was absorbed into the merger entity
  RECONSTITUTED_FROM -- predecessor was abolished; this new CK resembles it territorially
```

REMOVED from v0.2 type list:
- TRANSFERRED_AREA: territorial change when both CKs continue → belongs in declared_area_transfer
- RENAMED_FROM: same CK throughout → no lineage edge needed
- BOUNDARY_MODIFIED: same CK with new geometry → no lineage edge needed

### When district_relationship Is Created

- SPLIT_FROM: created for each successor CK when predecessor CK closes
- FORMED_FROM: created for new CK when predecessor continues (carve-out)
- MERGED_INTO: created for each predecessor CK before it closes into merger
- RECONSTITUTED_FROM: created for new CK from abolished predecessor

### When district_relationship Is NOT Created

- Rename: same CK, no derivation event
- Boundary modification: same CK, no identity change
- Partial territorial transfer (both continue): use declared_area_transfer only
- State administrative transfer: same CK, no identity change

### district_relationship — Complete Specification

```
rel_id                  SERIAL PRIMARY KEY
from_ck                 TEXT NOT NULL FK canonical_key_registry
to_ck                   TEXT NOT NULL FK canonical_key_registry
relationship_type       TEXT NOT NULL    -- SPLIT_FROM|FORMED_FROM|MERGED_INTO|RECONSTITUTED_FROM
supporting_event_id     INTEGER FK boundary_event  -- the event that caused this relationship
from_snapshot_id        INTEGER FK fact_district_snapshot  -- optional: specific predecessor snapshot
to_snapshot_id          INTEGER FK fact_district_snapshot  -- optional: specific successor snapshot
lineage_confidence      NUMERIC(3,2) NOT NULL
lineage_basis           TEXT NOT NULL    -- GAZETTE|SPATIAL_INFERRED|ACADEMIC|ESTIMATED|UNKNOWN
is_partial              BOOLEAN NOT NULL DEFAULT FALSE  -- FALSE = full identity derivation
evidence_type           TEXT NOT NULL DEFAULT "OBSERVED"
source_observation_id   INTEGER FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
CONSTRAINT: CHECK (from_ck != to_ck)
CONSTRAINT: DAG acyclicity enforced via Recursive CTE before every INSERT
```

### DAG Invariant

The district_relationship graph is acyclic. Proof:
1. Every CK has a unique established_date
2. All edges go from an entity established earlier to one established later (or at the same date in synchronous reorganizations — never to a true predecessor)
3. Reconstituted entities get new CKs (higher sequential number), ensuring they cannot be ancestors of their own predecessors
4. Enforced by: `WITH RECURSIVE cycle_check AS (SELECT ... UNION ALL ...) SELECT 1 WHERE EXISTS (SELECT * FROM cycle_check WHERE ck = from_ck)` — abort INSERT if cycle detected

---

## 11. Territorial Transfer Model

### Separation from Lineage

A territorial transfer occurs when territory moves from district A to district B AND BOTH DISTRICTS CONTINUE TO EXIST. This is NOT identity derivation. No `district_relationship` row is created.

A territorial transfer is represented by:
1. New `fact_district_snapshot` for district A (reduced boundary)
2. New `fact_district_snapshot` for district B (expanded boundary)
3. New `geometry_reconciliation` records for both
4. `declared_area_transfer` recording the declared quantity
5. `geometric_crosswalk` (derived from intersection of old-A and new-B geometries)
6. `boundary_event` with event_type = TRANSFER
7. `event_participant`: A=CONTEXT (continues), B=CONTEXT (continues)
8. NO `district_relationship` row

### When Is declared_area_transfer Created?

- When a Gazette or authority source declares a specific area quantity transferred
- When territory moves from one district to another (whether both continue or one closes)
- For CLEAN_SPLIT: declared_area_transfer may not be needed if the split geometry speaks for itself; create if Gazette declares specific quantities
- For CARVE_OUT: declared_area_transfer records the carved-out area

### declared_area_transfer — Complete Specification

```
transfer_id             SERIAL PRIMARY KEY
from_snapshot_id        INTEGER NOT NULL FK fact_district_snapshot
to_snapshot_id          INTEGER NOT NULL FK fact_district_snapshot
source_id               INTEGER NOT NULL FK dim_source    -- SOURCE IS IN GRAIN
declared_area_sqkm      NUMERIC(12,4) NOT NULL
declared_fraction_of_source NUMERIC(7,6)                  -- if stated in document
authority_document      TEXT                               -- gazette reference
transfer_date_est       DATE NOT NULL
transfer_date_precision TEXT NOT NULL
notes                   TEXT
evidence_type           TEXT NOT NULL DEFAULT "OBSERVED"
source_observation_id   INTEGER NOT NULL FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

Source IS in grain: different authority documents may declare different area quantities for the same transfer. Both are preserved.

---

## 12. Spatial Crosswalk Model

### geometric_crosswalk — Complete Specification

```
geo_xwalk_id            SERIAL PRIMARY KEY
from_snapshot_id        INTEGER NOT NULL FK fact_district_snapshot
to_snapshot_id          INTEGER NOT NULL FK fact_district_snapshot
from_geom_obs_id        INTEGER NOT NULL FK silver.geometry_observation
to_geom_obs_id          INTEGER NOT NULL FK silver.geometry_observation
area_weight             NUMERIC(7,6) NOT NULL    -- Intersection(A,B)/Area(A); SPATIAL FACT ONLY
coverage_fraction       NUMERIC(7,6) NOT NULL    -- fraction of from_snapshot area accounted for
                                                 -- by ALL targets combined
unallocated_fraction    NUMERIC(7,6) NOT NULL    -- 1.0 - coverage_fraction
intersection_sqkm       NUMERIC(12,4) NOT NULL
calculation_crs         TEXT NOT NULL DEFAULT "geography(EPSG:4326)"
calculated_at           TIMESTAMP NOT NULL
evidence_type           TEXT NOT NULL DEFAULT "DERIVED"
derived_from_ids        INTEGER[] NOT NULL       -- PKs of spatial_overlap records used
derivation_method       TEXT NOT NULL DEFAULT "ST_INTERSECTION_GEOGRAPHY"
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

### Coverage Validation Rules

- ERROR: SUM(area_weight) across all targets from same from_snapshot_id > 1.001 (geometry intersection error — territory cannot exceed source)
- WARNING: coverage_fraction < 0.85 (more than 15% of source area unaccounted for)
- INFO: coverage_fraction < 1.0 (normal for historical data — partial coverage is valid)
- NEVER ERROR for coverage_fraction < 1.0 alone

### Area Conservation Check (Split/Merge Validation)

For CLEAN_SPLIT: |area(predecessor) − SUM(area(successors))| / area(predecessor) ≤ max(0.005, 1.0 / area(predecessor_sqkm))
For MERGE: |area(successor) − SUM(area(predecessors))| / area(successor) ≤ same formula

This formula scales with district size. A 100 sq km district gets ±0.5% tolerance minimum. A 50,000 sq km district gets ±0.5% = 250 sq km tolerance. The threshold adapts to the data.

### Spatial Candidate Generation

spatial_overlap is computed ONLY for geometry_observation pairs that satisfy BOTH:
1. Bounding boxes overlap (GIST prefilter — free, uses spatial index)
2. Temporal proximity: validity periods overlap OR are within `spatial_candidate_window_years` (default 10 years; configurable in pipeline_config table)

FALLBACK RULE: If no candidates are found for a district within the temporal window, always include the nearest available geometry observation for that district regardless of temporal distance. This prevents computational savings from causing analytical gaps for sparse pre-1971 data.

The `spatial_candidate_window_years` parameter is stored in `pipeline_config(key TEXT, value TEXT)` and documented in `validation_run` metadata.

---

## 13. Statistical Harmonization Model

### Three Formally Separated Stages

Stage 1 — geometric_crosswalk (spatial fact; §12 above):
`area_weight = ST_Area(Intersection(A,B)::geography) / ST_Area(A::geography)`
Always the same number regardless of which statistic is being harmonized. Makes no statistical claim.

Stage 2 — statistical_crosswalk (epistemological claim):
```
stat_xwalk_id           SERIAL PRIMARY KEY
from_snapshot_id        INTEGER NOT NULL FK fact_district_snapshot
to_snapshot_id          INTEGER NOT NULL FK fact_district_snapshot
weighting_method        TEXT NOT NULL    -- AREA_WEIGHTED|POPULATION_WEIGHTED|
                                         -- CROPLAND_WEIGHTED|IRRIGATION_WEIGHTED|MANUAL
geo_xwalk_id            INTEGER FK geometric_crosswalk   -- NULLABLE for raster-based methods
statistical_weight      NUMERIC(7,6) NOT NULL
distribution_assumption TEXT NOT NULL    -- MANDATORY: explicit statement
                                         -- e.g. "wheat production assumed uniformly distributed
                                         --       across net sown area within district"
uncertainty_estimate    NUMERIC(7,6)
coverage_score          NUMERIC(7,6) NOT NULL
method_uncertainty      TEXT NOT NULL    -- LOW|MEDIUM|HIGH
ancillary_data_ref      TEXT             -- reference to raster dataset if used
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
evidence_type           TEXT NOT NULL DEFAULT "DERIVED"
derived_from_ids        INTEGER[] NOT NULL
derivation_method       TEXT NOT NULL
```

`geo_xwalk_id` is NULLABLE. For AREA_WEIGHTED: geo_xwalk_id references the geometric_crosswalk, statistical_weight = area_weight. For raster-based methods: geo_xwalk_id is NULL; statistical_weight is derived from the ancillary raster.

`distribution_assumption` is NOT NULL with a CHECK constraint: `length(distribution_assumption) >= 10`. This forces every row to carry an explicit assumption statement.

Stage 3 — stat_harmonized_value:
`harmonized_value = Σ [stat_observation.value × statistical_crosswalk.statistical_weight]`

### stat_observation Grain (Changed from v0.2)

```
observation_id          SERIAL PRIMARY KEY
canonical_key           TEXT NOT NULL FK canonical_key_registry  -- references CK, NOT snapshot_id
time_sk                 INTEGER NOT NULL FK dim_time
indicator_code          TEXT NOT NULL
source_id               INTEGER NOT NULL FK dim_source
value                   NUMERIC(20,4) NOT NULL
unit                    TEXT NOT NULL
observation_source_ref  TEXT NOT NULL   -- e.g. "Ministry of Agriculture Annual Report 2001"
confidence_score        NUMERIC(3,2)
evidence_type           TEXT NOT NULL DEFAULT "OBSERVED"
source_observation_id   INTEGER NOT NULL FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

stat_observation references canonical_key + time_sk (NOT snapshot_id). The mapping from CK+period to a specific snapshot is resolved during harmonization_crosswalk computation, not at stat_observation load time. This allows statistical datasets to load before spatial snapshots are fully resolved.

---

## 14. Uncertainty Model

No entity uses a single undifferentiated confidence score. Each dimension of uncertainty is independently reported.

### fact_district_snapshot Uncertainty Fields
```
identity_confidence     NUMERIC(3,2) NOT NULL    -- confidence in CK assignment (0.00-1.00)
temporal_confidence     NUMERIC(3,2) NOT NULL    -- confidence in date precision (0.00-1.00)
evidence_strength       TEXT NOT NULL            -- GAZETTE|CENSUS_PRIMARY|CENSUS_SECONDARY|
                                                 -- ACADEMIC|ESTIMATED|INFERRED|UNKNOWN
date_precision          TEXT NOT NULL            -- from valid_from_precision field
temporal_notes          TEXT                     -- narrative for uncertain dates
```

### geometry_observation Uncertainty Fields
```
spatial_accuracy_m      NUMERIC                  -- estimated positional accuracy in metres (NULL=unknown)
spatial_confidence      TEXT NOT NULL            -- HIGH|MEDIUM|LOW|UNKNOWN
```
`spatial_accuracy_m = NULL` means "accuracy unknown", NOT "perfect accuracy." This distinction must be preserved in all analytical uses.

### district_relationship Uncertainty Fields
```
lineage_confidence      NUMERIC(3,2) NOT NULL    -- confidence in this lineage relationship (0.00-1.00)
lineage_basis           TEXT NOT NULL            -- GAZETTE|SPATIAL_INFERRED|ACADEMIC|ESTIMATED|UNKNOWN
```

### statistical_crosswalk Uncertainty Fields
```
method_uncertainty      TEXT NOT NULL            -- LOW|MEDIUM|HIGH: uncertainty from method choice
distribution_assumption TEXT NOT NULL            -- explicit assumption statement (mandatory, non-empty)
uncertainty_estimate    NUMERIC(7,6)             -- estimated fractional uncertainty in weight
```

### stat_harmonized_value Uncertainty Fields
```
coverage_score          NUMERIC(7,6) NOT NULL    -- fraction of target area with source data
uncertainty_pct         NUMERIC(7,4) NOT NULL    -- estimated % uncertainty in harmonized value
uncertainty_sources     TEXT[] NOT NULL          -- e.g. ["GEOMETRIC_IMPRECISION", "DISTRIBUTION_ASSUMPTION",
                                                 --       "MISSING_SOURCE_GEOMETRY", "PARTIAL_COVERAGE"]
```

---

## 15. Provenance Model

### Three Provenance Categories

Every gold-layer record has `evidence_type TEXT NOT NULL` with value from:
- OBSERVED: fact is directly supported by a source observation
- DERIVED: fact is computed from other gold-layer records
- CURATED: fact was manually created, verified, or corrected

### Required Provenance Fields by Category

OBSERVED facts:
```
source_observation_id   INTEGER NOT NULL FK silver.source_record
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

DERIVED facts:
```
derived_from_ids        INTEGER[] NOT NULL    -- PKs of the input gold-layer records
derivation_method       TEXT NOT NULL         -- algorithm name and version string
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

CURATED facts:
```
source_observation_id   INTEGER FK silver.source_record    -- may reference original basis
curated_by              TEXT NOT NULL
curation_date           DATE NOT NULL
curation_notes          TEXT NOT NULL
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

`pipeline_run_id` and `pipeline_version` are MANDATORY on ALL three categories.

The OR construction from v0.2 ("source_observation_id OR pipeline_run_id") is retired. Both are required, with the specific fields per category as above.

### Transformation Provenance (silver layer)

`silver.transformation_log` records every transformation applied between bronze and silver:
- transformation_type: GEOMETRY_REPAIR|CRS_TRANSFORM|NAME_NORMALIZE|DATE_PARSE|DEDUP_RESOLUTION|SNAP_TO_GRID|DEDUPLICATION
- input_value: TEXT representation of value before transformation
- output_value: TEXT representation of value after transformation
- transformation_rule: which rule or algorithm was applied
- applied_at: TIMESTAMP

Rule: the transformation is logged BEFORE it is applied. If the system crashes after logging but before applying, the log shows what was intended — allowing reprocessing to skip or re-apply correctly.

### correction_log — Complete Specification

```
correction_id           SERIAL PRIMARY KEY
entity_type             TEXT NOT NULL    -- SNAPSHOT|RELATIONSHIP|EVENT|CK_MAPPING|
                                         -- GEOMETRY|AREA_TRANSFER|STAT_OBSERVATION
entity_id               INTEGER NOT NULL -- PK of the corrected record
correction_type         TEXT NOT NULL    -- DATE_CORRECTION|CK_REASSIGNMENT|
                                         -- LINEAGE_CORRECTION|GEOMETRY_CORRECTION|DATA_ERROR
field_corrected         TEXT NOT NULL    -- name of the field that was wrong
old_value               TEXT            -- serialized old value (NULL if addition)
new_value               TEXT NOT NULL    -- serialized new value
correction_reason       TEXT NOT NULL
corrected_by            TEXT NOT NULL    -- analyst ID or pipeline job name
corrected_at            TIMESTAMP NOT NULL
supporting_evidence_id  INTEGER FK event_evidence  -- optional: document supporting correction
pipeline_run_id         UUID NOT NULL FK validation_run
pipeline_version        TEXT NOT NULL
```

---

## 16. Validation Model

### Validation Categories and Severity

| Category | Examples | Severity | Blocks promotion? |
|---|---|---|---|
| Schema | type mismatch, NULL where mandatory, PK collision | ERROR | YES — hard block |
| Domain | invalid CK format, invalid enum value | ERROR | YES — hard block |
| Spatial | ST_IsValid=FALSE after repair attempt | ERROR | YES — hard block |
| Temporal | valid_from_est NULL, valid_from_est > valid_to_est, overlapping periods | ERROR | YES — hard block |
| Referential | FK to non-existent CK, snapshot, or source | ERROR | YES — hard block |
| Area overclaim | SUM(area_weight) > 1.001 from same source snapshot | ERROR | YES — data quality |
| Repair delta | repair_area_delta_pct > 0.1% from ST_MakeValid | WARNING | NO — promoted with flag |
| Coverage | coverage_fraction < 0.85 | WARNING | NO — promoted with flag |
| Source conflict | Two source geometries disagree on area > 5% | WARNING | NO — flagged in reconciliation_candidate |
| Conservation | |area(predecessor) − sum(area(successors))| / area > threshold | WARNING | NO — promoted with flag |
| Completeness | snapshot has no source_observation_id when evidence_type=OBSERVED | WARNING | NO |
| Scientific | confidence < 0.30 without documentation | INFO | NO |

### Area Conservation Threshold

`max(0.005, 1.0 / area_of_larger_district_sqkm)` — scales with district size. Rationale stored in `validation_rule.threshold_rationale`.

### Promotion Gates

Records failing ERROR-severity validation are NEVER promoted to gold without:
1. A `correction_log` entry with correction_reason, corrected_by, corrected_at
2. OR a manual resolution recorded in `validation_result.resolution_notes` with `is_resolved = TRUE`

Records failing WARNING-severity validation are promoted but marked with `has_validation_warnings = TRUE` on the gold record (flag added to each gold table for filtering purposes).

---

## 17. Final Table Inventory (30 Tables)

### Bronze Layer (4 tables)

| Table | Grain | Purpose |
|---|---|---|
| source_dataset | One source dataset version | Source metadata, authority ranks |
| ingest_manifest | One ingested file | File integrity, SHA-256, path |
| stanford_district_raw | One Stanford GPKG record | Immutable source data + JSONB raw fields |
| soi_district_raw | One SOI GPKG record | Immutable source data + JSONB raw fields |

### Silver Layer (6 tables)

| Table | Grain | Purpose |
|---|---|---|
| source_record | One normalized source record (metadata) | Normalized source metadata, linked to raw bronze |
| geometry_observation | One geometry per source per district per obs date | Primary physical geometry store |
| name_variant | One name form per district per source per period | Temporal name variants |
| transformation_log | One transformation applied to one record | Audit trail for every cleaning step |
| reconciliation_candidate | One cross-source match candidate | Conflict flagging, pre-reconciliation |
| source_pk_to_ck_mapping | One Source PK → CK assignment | Authoritative CK assignment registry |

### Gold Core (7 tables)

| Table | Grain | Purpose |
|---|---|---|
| canonical_key_registry | One district identity (CK) | CK lifecycle, display_code |
| dim_district | One identity (Type 1) | Current-state identity dimension |
| dim_time | One date (1800–2100) | Date dimension with is_census_year flag |
| dim_event_type | One event type | Controlled vocabulary for event classification |
| dim_source | One source dataset | Source authority (two-dimensional ranks) |
| dim_administrative_unit | One admin unit per period | State/division hierarchy, temporal |
| fact_district_snapshot | CK × contiguous validity period | Central administrative fact |

### Gold Events (5 tables)

| Table | Grain | Purpose |
|---|---|---|
| boundary_event | One administrative event | Event record with split_case field |
| event_participant | Event × CK × role | N:M bridge for event participation |
| event_evidence | One source document per event | Documentary evidence per event |
| district_relationship | Directed lineage CK → CK | Administrative lineage DAG edges |
| correction_log | One correction per entity per field | Data correction audit trail |

### Gold Spatial (4 tables)

| Table | Grain | Purpose |
|---|---|---|
| geometry_reconciliation | CK × period × decision date | Formal geometry selection decision record |
| spatial_overlap | Geometry observation pair | Pairwise intersection matrix |
| geometric_crosswalk | Source snapshot → target snapshot | Area weight; spatial fact only |
| declared_area_transfer | From snapshot × to snapshot × source | Gazette-declared territory quantities |

### Gold Harmonization (3 tables)

| Table | Grain | Purpose |
|---|---|---|
| stat_observation | CK × period × indicator × source | Historical statistical observations |
| statistical_crosswalk | From snapshot × to snapshot × method | Statistical weights with declared assumptions |
| stat_harmonized_value | Target snapshot × indicator × method | Harmonized output statistics |

### Gold Validation (3 tables)

| Table | Grain | Purpose |
|---|---|---|
| validation_rule | One rule | Rule definitions with threshold rationale |
| validation_result | One finding per entity per rule run | Findings as first-class data |
| validation_run | One execution | Run metadata, layer scope, counts |

**Total: 30 tables.**

---

## 18. Final Grain Matrix

### fact_district_snapshot
ONE ROW = ONE DISTRICT IDENTITY IN ONE CONTIGUOUS ADMINISTRATIVE VALIDITY PERIOD.
PK: snapshot_id SERIAL
Natural key: (canonical_key, valid_from_est) — UNIQUE, NOT NULL
Grain constraint: Exclusion on daterange(valid_from_est, COALESCE(valid_to_est, '9999-12-31')) per CK
Source: NOT in grain; accessed via reconciliation_id
Measures: identity_confidence, temporal_confidence, has_geometry
Temporal: valid_from_est/valid_to_est (administrative validity); effective_from/effective_to (ground implementation)
Spatial: via reconciliation_id → geometry_reconciliation → geometry_observation

### geometry_observation (silver)
ONE ROW = ONE SPECIFIC GEOMETRY AS REPORTED BY ONE SOURCE FOR ONE DISTRICT AT ONE OBSERVATION DATE.
PK: geom_obs_id SERIAL
Natural key: (canonical_key, source_id, observed_at) — UNIQUE
Grain constraint: UNIQUE (canonical_key, source_id, observed_at)
Measures: area_sqkm, spatial_accuracy_m, is_valid_geom, was_repaired
Temporal: observed_at (source observation date)
Spatial: geom GEOMETRY(MultiPolygon, 4326) — physical geometry here only

### geometry_reconciliation (gold_spatial)
ONE ROW = ONE FORMAL DECISION SELECTING ONE PREFERRED GEOMETRY FOR ONE DISTRICT AT ONE TIME PERIOD.
PK: reconciliation_id SERIAL
Natural key: (canonical_key, valid_from_est, decided_at) — decision history preserved
Current decision: most recent decided_at for a given (canonical_key, valid_from_est)
Measures: spatial_confidence, authority_rule
Temporal: valid_from_est/valid_to_est (period covered by this decision)
Spatial: preferred_geom_obs_id FK (no geometry stored here)

### spatial_overlap (gold_spatial)
ONE ROW = ONE PAIRWISE GEOMETRIC INTERSECTION BETWEEN TWO GEOMETRY OBSERVATIONS WITH NON-ZERO AREA.
PK: overlap_id SERIAL
Natural key: (from_geom_obs_id, to_geom_obs_id) — unique ordered pair
Grain constraint: UNIQUE (from_geom_obs_id, to_geom_obs_id)
Measures: intersection_sqkm, fraction_of_from, fraction_of_to
Temporal: derived from the temporal proximity filter at computation time

### geometric_crosswalk (gold_spatial)
ONE ROW = ONE AREA WEIGHT FROM ONE SOURCE SNAPSHOT TO ONE TARGET SNAPSHOT.
PK: geo_xwalk_id SERIAL
Natural key: (from_snapshot_id, to_snapshot_id) — unique pair
Measures: area_weight, coverage_fraction, unallocated_fraction, intersection_sqkm
Temporal: derived from validity periods of from_snapshot and to_snapshot
Spatial: derived from geometry_observation intersection; no geometry stored

### statistical_crosswalk (gold_harmonization)
ONE ROW = ONE STATISTICAL WEIGHT FROM ONE SOURCE SNAPSHOT TO ONE TARGET SNAPSHOT USING ONE METHOD.
PK: stat_xwalk_id SERIAL
Natural key: (from_snapshot_id, to_snapshot_id, weighting_method) — one per method per pair
Measures: statistical_weight, uncertainty_estimate, coverage_score
Temporal: inherited from snapshot temporal periods
Note: geo_xwalk_id is NULLABLE (NULL for raster-based weighting methods)

### boundary_event (gold_events)
ONE ROW = ONE ADMINISTRATIVE EVENT AFFECTING ONE OR MORE DISTRICT IDENTITIES.
PK: event_id SERIAL
Natural key: none guaranteed unique (use PK)
Participants in event_participant bridge
Measures: event_date_est, event_date_precision, lineage_confidence, split_case

### event_participant (gold_events, bridge)
ONE ROW = ONE DISTRICT IDENTITY'S PARTICIPATION IN ONE EVENT WITH ONE ROLE.
PK: part_id SERIAL
Natural key: (event_id, canonical_key, role) — UNIQUE
References CK (NOT snapshot_id)

### district_relationship (gold_events)
ONE ROW = ONE DIRECTED ADMINISTRATIVE LINEAGE RELATIONSHIP BETWEEN TWO DISTRICT IDENTITIES.
PK: rel_id SERIAL
Natural key: (from_ck, to_ck, relationship_type) — unique per directed pair per type
No area fractions. No territory quantities.
Measures: lineage_confidence, lineage_basis
Constraint: from_ck ≠ to_ck; DAG acyclicity

### declared_area_transfer (gold_core)
ONE ROW = ONE DECLARED TRANSFER OF TERRITORY PER ONE AUTHORITY SOURCE.
PK: transfer_id SERIAL
Natural key: (from_snapshot_id, to_snapshot_id, source_id) — source in grain
Measures: declared_area_sqkm, declared_fraction_of_source

### stat_observation (gold_harmonization)
ONE ROW = ONE STATISTICAL OBSERVATION FOR ONE DISTRICT IDENTITY IN ONE PERIOD FOR ONE INDICATOR FROM ONE SOURCE.
PK: observation_id SERIAL
Natural key: (canonical_key, time_sk, indicator_code, source_id)
References canonical_key (NOT snapshot_id)
Measures: value, unit

### stat_harmonized_value (gold_harmonization)
ONE ROW = ONE HARMONIZED STATISTICAL VALUE FOR ONE TARGET DISTRICT IN ONE PERIOD FOR ONE INDICATOR USING ONE METHOD.
PK: harmonized_id SERIAL
Natural key: (to_snapshot_id, indicator_code, weighting_method)
Measures: harmonized_value, coverage_score, uncertainty_pct, uncertainty_sources[]

---

## 20. Final Invariants (27 Invariants)

### Group 1: Identity (I-1 to I-5)

I-1: A source PK maps to at most one CK at any given time. Enforced by UNIQUE(source_pk, source_dataset_id) on source_pk_to_ck_mapping.

I-2: A CK is never deleted. No DELETE allowed on canonical_key_registry. Enforced by trigger or application constraint.

I-3: A CK is never reused. The ck_global_sequence is never reset. Enforced by SERIAL with NO CYCLE.

I-4: A CK contains no mutable attributes. Format IND-{SEQ:06d} enforced by CHECK constraint: `canonical_key ~ '^IND-[0-9]{6}$'`.

I-5: CK allocation follows the transactional get-or-create protocol: check source_pk_to_ck_mapping BEFORE allocating new CK. Enforced by application logic and UNIQUE constraint.

### Group 2: Provenance (P-1 to P-5)

P-1: Bronze records are immutable after ingestion. No UPDATE or DELETE on bronze.* tables after initial INSERT. Enforced by trigger: `RAISE EXCEPTION 'bronze table is immutable'`.

P-2: Every gold-layer record has evidence_type ENUM(OBSERVED, DERIVED, CURATED) set to NOT NULL. Enforced by schema constraint.

P-3: pipeline_run_id and pipeline_version are NOT NULL on ALL gold-layer records. Enforced by schema constraint.

P-4: OBSERVED facts have source_observation_id NOT NULL. DERIVED facts have derived_from_ids NOT NULL and derivation_method NOT NULL. CURATED facts have curated_by, curation_date, curation_notes all NOT NULL. Enforced by CHECK constraints per evidence_type.

P-5: Every transformation applied between bronze and silver is logged in transformation_log BEFORE the transformation is applied. Enforced by application code ordering.

### Group 3: Geometry (G-1 to G-5)

G-1: geometry_provenance must be OBSERVED, DIGITIZED, or DERIVED. UNKNOWN means has_geometry=FALSE in the associated snapshot. Enforced by CHECK constraint on geometry_observation.geometry_provenance.

G-2: Every fact_district_snapshot with has_geometry=TRUE has reconciliation_id NOT NULL referencing a current geometry_reconciliation record. Enforced by CHECK constraint.

G-3: When source geometries disagree, both are preserved in geometry_observation. No DELETE on geometry_observation after INSERT. Enforced by trigger.

G-4: Geometry repair (ST_MakeValid) is logged in transformation_log with invalidity type, fix strategy, and area_delta_pct before being applied. Enforced by application code.

G-5: ST_SnapToGrid is applied ONLY when documented in transformation_log with the precision level, rationale, and area_delta_pct. Never applied to geometries without logging. Enforced by application code.

### Group 4: Spatial-Statistical (S-1 to S-5)

S-1: geometric_crosswalk.area_weight makes no statistical claim about spatial distribution. This is a semantic constraint; documented in code comments and API documentation.

S-2: statistical_crosswalk.distribution_assumption is NOT NULL and non-empty (minimum 10 characters). Enforced by CHECK constraint: `length(distribution_assumption) >= 10`.

S-3: statistical_crosswalk.weighting_method is NOT NULL and non-empty. Enforced by CHECK constraint.

S-4: SUM(area_weight) from any source snapshot across all targets MUST NOT exceed 1.001. Enforced by validation_rule with ERROR severity.

S-5: coverage_fraction < 1.0 is valid. Never error for partial coverage alone. Enforced by validation_rule with WARNING severity (not ERROR).

### Group 5: Lineage (L-1 to L-4)

L-1: district_relationship DAG is acyclic. Recursive CTE cycle check runs before every INSERT into district_relationship. Enforced by application code (cannot be a simple CHECK constraint).

L-2: district_relationship.relationship_type has exactly four valid values: SPLIT_FROM, FORMED_FROM, MERGED_INTO, RECONSTITUTED_FROM. Enforced by CHECK constraint or PostgreSQL ENUM type.

L-3: A territorial transfer between two continuing districts does NOT produce a district_relationship row. Enforced by application code and semantic constraint.

L-4: A rename does NOT produce a district_relationship row. Enforced by application code.

### Group 6: Temporal (T-1 to T-3)

T-1: fact_district_snapshot.valid_from_est is NOT NULL. For unknown start dates: use the start of the earliest plausible era with valid_from_precision=UNKNOWN. Enforced by NOT NULL schema constraint.

T-2: For any CK, no two fact_district_snapshot records may have overlapping daterange(valid_from_est, COALESCE(valid_to_est, '9999-12-31')). Enforced by PostgreSQL exclusion constraint using GIST index.

T-3: boundary_event.event_date_est is NOT NULL. For unknown event dates: use start of most precise known era with event_date_precision=DECADE or UNKNOWN. Enforced by NOT NULL schema constraint.

---

## 21. Layer Architecture

```
L0  BUSINESS / SEMANTIC MODEL
    Domain definitions; frozen in this document.

L1  BRONZE / LANDING (immutable)
    source_dataset · ingest_manifest
    stanford_district_raw · soi_district_raw
    Rule: no UPDATE or DELETE ever. SHA-256 checksum on ingest.

L2  SILVER / STANDARDIZED
    source_record · geometry_observation [ONLY physical geometry store]
    name_variant · transformation_log
    reconciliation_candidate · source_pk_to_ck_mapping
    Rule: every transformation logged before applied.
          geometry in EPSG:4326 MultiPolygon only.
          SnapToGrid optional and source-specific.

L3  CANONICAL IDENTITY RESOLUTION
    canonical_key_registry · source_pk_to_ck_mapping (populated here)
    CK = IND-{SEQ:06d} via transactional get-or-create.

L4  GOLD CORE: ADMINISTRATIVE MODEL
    dim_district (Type 1) · dim_time (1800–2100) · dim_source
    dim_event_type · dim_administrative_unit
    fact_district_snapshot [grain: (CK, valid_from_est)]
    declared_area_transfer

L5  GOLD EVENTS + LINEAGE
    boundary_event [with split_case field] · event_participant
    event_evidence · district_relationship [4 types, admin lineage only]
    correction_log

L6  GOLD SPATIAL
    geometry_reconciliation [formal decision record; no geometry stored here]
    spatial_overlap [bounded: GIST + configurable temporal window + nearest fallback]
    geometric_crosswalk [area_weight + coverage_fraction + unallocated_fraction]

L7  GOLD HARMONIZATION: STATISTICAL
    stat_observation [references CK+period, not snapshot_id]
    statistical_crosswalk [mandatory method + distribution_assumption; geo_xwalk_id nullable]
    stat_harmonized_value [with coverage_score, uncertainty_pct, uncertainty_sources[]]

L8  GOLD VALIDATION (cross-cutting)
    validation_rule [with threshold_rationale] · validation_result · validation_run

L9  SERVING / ANALYTICAL
    Views · REST API · GeoParquet exports

L10 APPLICATION / GIS UI
    Historical map · time slider · lineage explorer · area change visualization
```

---

## 22. Data Flow

```
SOURCE FILES (Stanford GPKG, SOI GPKG)
      │ [SHA-256 checksum; JSONB verbatim storage; is_active=TRUE]
      ▼
BRONZE [L1] (immutable; 4 tables; never modified)
      │ [geometry: ST_IsValid → ST_MakeValid if needed → ST_Multi → ST_Transform(4326)
      │  → optional SnapToGrid (source-specific, logged) → area threshold check
      │  names: Unicode → ASCII slug; dates: parsed to DATE + precision ENUM
      │  all steps logged in transformation_log BEFORE application]
      ▼
SILVER [L2] (6 tables; geometry_observation is sole geometry store)
      │ [entity matching: fuzzy name + spatial centroid + temporal overlap
      │  → score ≥0.85: auto-match; 0.60–0.84: quarantine for manual review
      │  → <0.60: new CK via transactional get-or-create
      │  all assignments in source_pk_to_ck_mapping]
      ▼
CANONICAL IDENTITY REGISTRY [L3]
      │ [dim_time (1800-2100), dim_source (two-dimensional authority),
      │  dim_event_type, dim_administrative_unit, dim_district loaded]
      ▼
GOLD CORE [L4] (fact_district_snapshot; valid_from_est NOT NULL; exclusion constraint)
      │                             │
      ▼                             ▼
GOLD EVENTS [L5]              GOLD SPATIAL [L6]
(boundary_event with            (geometry_reconciliation per CK per period;
 split_case field;               spatial_overlap via GIST + temporal window;
 district_relationship           geometric_crosswalk: area_weight +
 4 types only)                   coverage_fraction + unallocated_fraction)
      │                             │
      └──────────────┬──────────────┘
                     ▼
GOLD HARMONIZATION [L7]
(stat_observation via CK+period;
 statistical_crosswalk with mandatory method + assumption;
 geo_xwalk_id nullable;
 stat_harmonized_value with uncertainty_sources[])
                     │
GOLD VALIDATION [L8] (cross-cutting; runs after each layer; results as data)
                     │
               SERVING [L9] → GIS UI [L10]
```

---

## 25. Implementation Readiness

Architecture v0.3 is implementation-ready with ONE blocking open decision.

### What Must Be Frozen Before Phase 0 Begins (All Frozen)
1. CK format IND-{06d} — frozen
2. CK allocation via transactional get-or-create — frozen
3. Snapshot grain (CK, valid_from_est) with exclusion constraint — frozen
4. valid_from_est NOT NULL with precision ENUM — frozen
5. Two-dimensional source authority (legal + spatial ranks) — frozen
6. geometry_observation as sole physical geometry store — frozen
7. geometry_reconciliation formal decision record — frozen
8. Four district_relationship types only — frozen
9. CLEAN_SPLIT vs CARVE_OUT split_case field — frozen
10. Coverage_fraction model (not sum≈1.0 invariant) — frozen
11. Geometric/statistical crosswalk separation — frozen
12. Structured uncertainty per entity — frozen
13. AND provenance requirement — frozen
14. 30-table inventory — frozen
15. stat_observation references CK+period (not snapshot_id) — frozen

### What Blocks Implementation: ONE Decision

OD-01: Reclassification CK rule — when a district is reclassified to a lower administrative tier (district → tehsil) and later restored, does the restored district keep the same CK or get a new one?

This must be resolved before the entity matching algorithm is built, because the algorithm must know whether to retrieve or create when it encounters a reclassified entity.

Resolution evidence needed: Review of Census of India district-series tables for one documented case (e.g., Raipur as a subdivision under a commissionerate period). If statistical reporting series was continuous → same CK. If series was broken → new CK with RECONSTITUTED_FROM.

Get this from Prof. Patel or Census district documentation.

---

## 26. Open Decisions

### OD-01: Reclassification CK rule — BLOCKING

Question: When a district is reclassified (district → tehsil or subdivision) and later restored as a district, does it keep the same CK or receive a new one?

Options:
A. Same CK if: (a) territory is substantially the same, (b) administrative function in statistical series is continuous, (c) Gazette does not abolish the entity but merely reclassifies it
B. New CK always on reclassification; RECONSTITUTED_FROM on restoration

Recommendation: A (same CK) if the Census of India treated the entity as continuous in its district-series tables during the reclassification period. B (new CK) if the series was broken.

Evidence needed: At least one documented case from Census of India district handbooks. Contact Prof. Patel for domain ruling.

Status: BLOCKING — must resolve before entity matching module is coded.

### OD-02: Graph traversal mechanism — NON-BLOCKING

Question: Recursive CTE vs. Apache AGE extension vs. external graph DB.

Recommendation: Start with Recursive CTE. Upgrade to Apache AGE if traversal queries consistently exceed 500ms on the full national dataset.

Status: NON-BLOCKING — Recursive CTE implementation is straightforward. Migration to AGE is possible without schema changes.

### OD-03: Pre-independence display_code convention — NON-BLOCKING

Question: For districts established before 1947, the display_code state reference should use what? Princely state name, Province name, or modern state code.

Recommendation: Princely state name or Province name with a mapping table. Example: "Hyderabad State / Gulbarga [pre-1947]"

Evidence needed: Consult Census of India's own naming convention for pre-independence district descriptions.

Status: NON-BLOCKING — affects only display_code (a human-readable label), not the CK or any analytical computation.

### OD-04: Spatial candidate window default value — NON-BLOCKING

Question: The default `spatial_candidate_window_years = 10` is a heuristic. Is 10 years the right default for Indian district boundary history?

Recommendation: Keep 10 as default. Validate empirically once the full Stanford dataset is processed by checking whether any cross-decade spatial intersections are missed.

Status: NON-BLOCKING — parameter is configurable without schema changes.

---

## CHANGELOG v0.2 → v0.3

| ID | Old (v0.2) | New (v0.3) | Why | Impact | Blocking? |
|---|---|---|---|---|---|
| C01 | authority_level INTEGER (direction inconsistent) | legal_authority_rank + spatial_precision_rank (both 1=highest) | Legal and spatial precision diverge; internal inconsistency | dim_source schema; geometry_reconciliation authority_rule | NO |
| C02 | CK = IND-{SERIAL} via database sequence | Transactional get-or-create via source_pk_to_ck_mapping lookup | Pipeline reruns assign different CKs without lookup-first | Entity matching module design | YES |
| C03 | valid_from DATE NULL | valid_from_est DATE NOT NULL + valid_from_precision ENUM | NULL breaks natural key and exclusion constraint | All temporal queries; schema change | YES |
| C04 | sum(area_weights) ≈ 1.0 invariant | coverage_fraction model; ERROR only if weights > 1.001 | Partial coverage is historically valid; blanket rule wrong | geometric_crosswalk gains 2 fields; validation rules | NO |
| C05 | "within 5 years" hardcoded | configurable spatial_candidate_window_years (default 10) + nearest fallback | 5yr was arbitrary; may exclude valid historical comparisons | Pipeline config; documentation | NO |
| C06 | ST_SnapToGrid(0.000001°) mandatory | Optional, source-specific, precision ≥ spatial_accuracy_m/111320 | False precision for hand-digitised historical data | Standardization pipeline | NO |
| C07 | TRANSFERRED_AREA, RENAMED_FROM, BOUNDARY_MODIFIED in district_relationship | Exactly 4 types; SPLIT_FROM, FORMED_FROM, MERGED_INTO, RECONSTITUTED_FROM | Territorial transfer ≠ identity derivation; rename = same CK | Entity matching; event parsing | YES |
| C08 | source_observation_id OR pipeline_run_id | AND: all gold records require pipeline_run_id + pipeline_version; per-category required fields | OR allows incomplete provenance | All gold table schemas | YES |
| C09 | stat_observation.snapshot_id FK fact_district_snapshot | stat_observation.canonical_key + time_sk | Statistical load independent of spatial pipeline | stat_observation grain change | YES (ordering) |
| C10 | Split semantics implicit | CLEAN_SPLIT vs CARVE_OUT; boundary_event.split_case field | Hyderabad-type carve-outs need distinct handling | boundary_event schema; entity matching | YES |
| C11 | temporal_record as separate table | Merged into transformation_log | Redundant with transformation_log date parsing entries | Table count 31→30 | NO |
| C12 | correction_log minimal | Fully specified with entity_type, correction_type, old/new value | Implementation requires complete spec | Schema definition | NO |
| C13 | dim_time 1940–2030 | dim_time 1800–2100 | Pre-independence records exist | Time dimension rebuild | NO |
| C14 | statistical_crosswalk.geo_xwalk_id NOT NULL implied | NULLABLE for raster-based weighting methods | Population/cropland-weighted methods have no geo_xwalk | Schema change | NO |
---

## Stage 10 — Event Area Transfer Matrix & Territorial Reconciliation Engine

### Purpose

Stage 10 is the full per-event spatial accounting engine. It extends the
"one primitive, three products" model (§4.7 of the redesign doc) to five
products, all derived from the same transition matrix M:

| Product | Source | Stage |
|---|---|---|
| Lineage graph (district_relationship) | M, genuine new-CK edges | S6 |
| Area ledger (district_area_ledger) | M, per-CK window accounting | S7 |
| Statistical crosswalk (M-hat) | row-normalized M | future |
| **Event area transfer matrix** | M joined to events, per (event, pred, succ) | **S10** |
| **Event territorial reconciliation** | conservation + residual recovery | **S10** |

### Architecture Invariants Enforced by S10

- **No silent area loss**: raw residual always recorded, never forced to zero.
- **Declared ≠ measured**: `declared_transfer` and `measured_transfer` are separate fields.
- **No normalization**: area weights never forced to sum to 1.0. Residual gets a reason code.
- **Events as evidence (Principle 7)**: `administrative_relationship` and `spatial_relationship` stored and compared separately. Disagreements flagged, not hidden.
- **Chronological processing**: events processed in strict year order.
- **Deterministic IDs**: sha256 natural keys. Reruns produce byte-identical joins.

### Outputs

```
outputs/event_transfer/
├── event_area_transfer_matrix.{csv,parquet}
├── event_area_accounting_summary.{csv,parquet}
├── event_district_relationships.csv
├── residual_area_audit.csv
├── district_area_ledger_event.csv
├── event_narratives.{json,md}
├── event_residuals.gpkg   (4 QGIS layers: raw/recovered/unresolved/transfers)
└── s10_qc_report.md
```

### Conservation Equation (hard requirement — no exceptions)

```
source_area_km2       = area(predecessor at pre_vintage)
accounted_area_km2    = Σ_j M[pred_obs, succ_obs_j]
residual_area_km2     = max(0, source_area_km2 − accounted_area_km2)
residual_pct          = residual_area_km2 / source_area_km2 × 100

NEVER: residual = 0 by normalization
ALWAYS: raw_residual stored in residual_area_audit.csv regardless of magnitude
```

### Residual Recovery Hierarchy

| Level | Check | Outcome |
|---|---|---|
| Tolerance gate | \|residual_pct\| < 1% | IGNORABLE_RESIDUAL (kept in audit) |
| L1 — geometry | invalid geom, CRS mismatch, slivers | document; reclassify reason |
| L2 — adjacency scoring | score_j = Σ w_k × feature_k (config/reconciliation.yaml) | RECOVERED_BY_ADJACENCY if score ≥ threshold |
| L3 — parent-child constrained | event_summary relationship constrains candidates | prefer administratively related |
| Exhausted | all levels fail | UNRESOLVED_RESIDUAL with taxonomy reason code |

### Residual Reason Taxonomy

`GEOMETRY_INVALID | TOPOLOGY_GAP | TOPOLOGY_OVERLAP | SLIVER | CRS_MISMATCH | BOUNDARY_PRECISION | VINTAGE_MISMATCH | MISSING_DISTRICT_GEOMETRY | MISSING_PARENT | UNDOCUMENTED_TRANSFER | ADJACENT_DISTRICT_CANDIDATE | PARENT_RELATIONSHIP_MISMATCH | EVENT_METADATA_INCOMPLETE | UNRECOVERED_GEOMETRIC_RESIDUAL | UNKNOWN`

---

## Stage 10b — Spatial Successor Discovery & Territorial Reconciliation Engine

### Purpose and Position

Stage 10b is a **companion** to S10 running after S10 in the pipeline:
`S10 → S10b → final reporting`. It processes `UNRESOLVED_RESIDUAL` events
left by S10 (~94 of 935 events). Its objective is to **explain** each residual
with spatial and administrative evidence — not merely to close the ledger.

### Governing Principle: Two-Evidence Model

```
ADMINISTRATIVE EVIDENCE    +    SPATIAL EVIDENCE
  event_summary                  district geometries
  Gazette relationships          measured intersections
  event metadata                 shared boundaries
        ↓                                ↓
   (declared)                      (discovered)
        └──────────── S10b JOIN ───────────┘
                            ↓
               Reconciliation decision (never silent merge)
```

A spatially discovered relationship is **never** automatically an administrative
one. A declared relationship is **never** assumed spatially correct without
geometric verification. Disagreement → `EVENT_GEOMETRY_IDENTITY_MISMATCH` (research
finding, not a resolution).

### Recovery Hierarchy L0–L6

| Level | Method |
|---|---|
| L0 | Direct resolution (already done by S10) |
| L1 | Evidence-based alias table (60+ DOCUMENTARY entries) |
| L2 | Cross-state identity (STATE_PREDECESSOR_MAP; removes state constraint) |
| L3 | Full spatial discovery — ALL target-vintage districts via STRtree |
| L4 | Parent-child constrained recovery |
| L5 | Adjacency/shared-boundary heuristic |
| L6 | UNRESOLVED — documented cause required |

### Dual Confidence (not a single blind score)

`spatial_confidence` and `administrative_confidence` are computed and reported
**separately**. Administrative evidence gates candidate classification before
scores are combined. Weights are configurable in `config/reconciliation.yaml`.

### Two-Registry Alias Architecture

```
data/gold/core/name_alias_candidates.parquet  ← discovered; require review
data/gold/core/name_alias_registry.parquet    ← approved (DOCUMENTARY/MANUAL only)
```

`SPATIAL_DISCOVERY` alone never promotes an alias to authoritative registry.

### S10 Output Immutability

```
residual_area_audit_s10.csv   ← S10 original (READ-ONLY, never overwritten)
residual_area_audit_s10b.csv  ← S10b augmented
residual_area_audit.csv       ← convenience latest alias
```

`initial_residual_pct` (S10 raw) and `post_discovery_residual_pct` (S10b) are
**separate fields** — both always present in the audit table.

### Invariants

1. No lineage modification — S10b does NOT write to district_relationship (S6) or district_area_ledger (S7).
2. No area fabrication — area is never manufactured to close the ledger.
3. No silent discard — every residual has a documented reason code.
4. Raw residual preserved — `initial_residual_pct` = S10's original value.
5. Identity ≠ territory — territorial transfer between continuing districts ≠ lineage.
6. 1% rule applied after recovery — materiality threshold, not a data-cleaning rule.
7. Provenance on every relationship — event_id, geometry IDs, vintage, level, reason.

### New Outputs

```
outputs/event_transfer/
  residual_area_audit_s10.csv                    ← S10 original (READ-ONLY)
  residual_area_audit_s10b.csv                   ← S10b augmented (779 rows)
  spatial_successor_candidates.{csv,parquet}     ← all L3 candidates
  event_spatial_candidates.gpkg                  ← 4 QGIS layers (with geometry)
  event_area_transfer_matrix.{csv,parquet}       ← augmented (+11 spec fields)
  event_area_accounting_summary_s10b.{csv,parquet}
  event_narratives_s10b.{json,md}               ← S10b-specific narratives
  TERRITORIAL_RECONCILIATION_REPORT.md
data/gold/core/
  name_alias_candidates.parquet
  name_alias_registry.parquet
```

### Final Classification Codes

`FULLY_RECONCILED | RECONCILED_WITH_IGNORABLE_RESIDUAL | RECONCILED_BY_SPATIAL_DISCOVERY | RECONCILED_BY_ALIAS | RECONCILED_BY_CROSS_STATE_RESOLUTION | REQUIRES_MANUAL_REVIEW | UNRESOLVED_GEOMETRIC_RESIDUAL | EVENT_GEOMETRY_IDENTITY_MISMATCH | DATA_VINTAGE_INSUFFICIENT | MISSING_SPATIAL_EVIDENCE`

