# Lineage and Area Accounting — Audit and Redesign

Status: proposal for review
Basis: Architecture v0.3 (`docs/architecture/ARCHITECTURE.md`), Phase 9 design and outputs,
pipeline code under `scripts/`. The Gold DuckDB and Bronze GPKGs are not present in the
working tree, so this is a static audit of code paths plus the committed run reports; no
pipeline was re-executed.

---

## 1. The problem we are actually solving

For any Indian district, at any date between 1951 and today, answer four questions with a
stated uncertainty and a traceable evidence chain:

1. **Identity** — is this the same administrative entity as the one that carried this name
   or this territory at an earlier date?
2. **Origin** — which earlier districts did its territory come from, and in what proportion?
3. **Fate** — where did its territory go, and in what proportion?
4. **Quantity** — how many km² moved, when, and under what authority?

A fifth question is the reason the other four matter: **harmonization** — restate a
statistic reported on 1971 districts onto 2011 districts without inventing data.

The hard part is not any single one of these. It is that the four answers must be
*mutually consistent*: the lineage graph, the area ledger, and the statistical crosswalk
must all be derivable from one underlying account of where territory went. Today they are
computed by three different code paths from three different premises, and they disagree.

### What the evidence can and cannot identify

| Evidence | What it is | What it can identify |
|---|---|---|
| 9 geometry vintages (1951–2021 Stanford, 2025 SOI) | Observed boundaries at discrete dates | **Net** territorial change between consecutive vintages. Exactly. |
| `district_evolution_master.csv` (1550 rows) | Academic compilation of events | That a change happened, roughly when, and what it was called |
| Names / states | Source attributes | Weak identity prior. Nothing about territory. |
| Gazettes | Not held | Would identify legal authority, exact dates, declared quantities |

This table is the whole design constraint, and the current implementation violates it in
one specific way: **it reports per-event transferred areas as measurements, when the
evidence only identifies per-window net change.** If three events touched a district
between 1961 and 1971, the 1961→1971 area delta cannot be apportioned among them from
geometry alone. It is unidentified. Today it is apportioned anyway, and the result is
labelled `MEASURED`.

---

## 2. Audit findings

Ordered by the size of the error they produce, not by layer.

### 2.1 The event grain is wrong, and it invalidates every conservation test

`build_gold_events.py:314` allocates a fresh `event_id` **per CSV row**. The Bronze CSV has
one row per (parent, child) pair. A real 1-parent-3-children split therefore becomes three
separate "events", each with exactly one predecessor and one successor.
`build_gold_events.py:433` compounds this: `event_participants[event_id][role] = ck`
overwrites by role, so even if a multi-participant event existed, all but one participant
would be silently dropped.

Downstream consequences, all confirmed in `outputs/phase9/phase91_independent_audit.md`:

- "Events with multiple lineage relationships: 0" and "Multi-target event/source groups: 0".
- `rebuild_decadal_area_accounting.py:560-565`: for `CLEAN_SPLIT`, `expected = P0` (the whole
  parent) and `accounted = T1` (one child). The residual is the *other children*. Result:
  **49 of 50 clean splits fail conservation**, and 301 of 480 measurable groups fail overall.
  These are not data problems. They are an arithmetic consequence of the grain.
- `rebuild_decadal_area_accounting.py:603`: `w_j = I_j / ΣI_j` is identically **1.0** for
  single-member groups, so `allocated_transfer = available_area × 1.0`. Every child is
  assigned 100% of the parent's observed loss. This is a fabricated attribution published
  in a column named `allocated_transfer_area_km2` with `measurement_status = MEASURED`.
- `rebuild_decadal_area_accounting.py:550`: `overlap_excess = ΣI_j − area(S ∩ ∪T_j)` is
  identically **0** when the group has one member. `revised_area_validation.md` reports
  "Event/source groups with material or severe overlap: 0" as a result. It is a tautology.
  The Phase 9.1 audit caught this; the report was not corrected.

**This is the single highest-value fix in the system.** Aggregating CSV rows to an
administrative event key — `(state, parent_district, effective_year, event_type)` — turns
1550 pseudo-events into real n:m events and makes conservation a genuine test for the
first time.

### 2.2 `spatial_overlap` mixes vintages, which manufactured the entire overlap problem

`build_gold_spatial.py:575-593` is an O(n²) loop over **all 4,381 geometry observations
across all 9 vintages**. The temporal window is defeated by its own fallback: `if yr_diff >
window_yrs and nearest != yr_diff: continue` — when the nearest observation for a CK *is*
the far one, the pair is kept regardless of distance.

So a 1951 district is intersected against its own 1961, 1971, 1981 and 1991 geometries as
if they were competing targets. Summing `fraction_of_from` over such a set gives ~5.
That is precisely `outputs/crosswalk_audit/case_studies.md` Case C (raw weight sum 5.32)
and the Phase 7.1 maximum of 24.25.

**These are not overlapping administrative jurisdictions. They are the same ground counted
once per vintage.** The diagnosis in `build_gold_harmonization.py:179` —
`log.info("Diagnosis: Cause = overlapping targets -> safe to normalize.")` — is a hardcoded
print statement, not a computation. Phase 7.1 then "conditionally accepted" normalization
on the strength of that non-diagnosis.

Two further defects in the same loop:
- `:583` dedupes pairs **symmetrically** (`(a,b) or (b,a)`) while storing **directional**
  fractions (`fraction_of_from`, `fraction_of_to`). The reverse direction is never emitted,
  so a target's inbound composition cannot be assembled from this table for half its pairs.
- `:613` drops pairs where `frac_from > 1.001` with `continue` instead of recording them.
  A true intersection cannot exceed its source; exceeding it means the stored `area_sqkm`
  and the recomputed intersection area were produced by **different area methods**. The
  evidence of the worst cases is discarded from the table and survives only in a CSV that
  then drives normalization elsewhere.

### 2.3 Normalization to Σw = 1.0 contradicts the frozen architecture

`build_gold_harmonization.py:241` computes `norm_w = orig_w / tot`, forcing weights to sum
to 1.0. Architecture change **C04** explicitly retired this: *"sum(area_weights) ≈ 1.0
invariant → coverage_fraction model; partial coverage is historically valid; blanket rule
wrong"*, and invariant **S-5** states *"coverage_fraction < 1.0 is valid. Never error for
partial coverage alone."*

Worse, `:253` hardcodes `coverage_score = 1.0` on exactly these rows — the ones with the
worst geometric evidence are stamped with perfect coverage. A downstream filter on
`coverage_score` cannot see them. Three compounding errors — vintage-mixed overlaps, a
fictional diagnosis, and a fabricated quality score — produce a crosswalk that is
mathematically closed and scientifically meaningless.

### 2.4 Identity is string equality on (name, state)

`run_identity.py:491-577` maintains `name_state_to_ck: Dict[(name, state), ck]` and matches
by exact lowercase equality. The architecture's own data flow (§22) specifies *"fuzzy name +
spatial centroid + temporal overlap → score ≥0.85 auto-match; 0.60–0.84 quarantine"*.
**No spatial or temporal evidence is used at all.** The `match_score` values at `:536`,
`:566`, `:596`, `:624` are hardcoded literals (1.0, 1.0, 0.9, 0.85) — labels, not computed
quantities. Every `identity_confidence` downstream inherits a constant.

Consequences:
- The map is not time-scoped. A (name, state) key binds to one CK forever, including after
  that district is abolished. A name reused decades later silently re-attaches to the dead
  CK and fabricates continuity.
- 215 rows / 105 groups of `AMBIGUOUS_DUPLICATE_CK_VINTAGE` — two source features in one
  vintage resolving to one CK. `rebuild_decadal_area_accounting.py:288` drops **both** rows,
  so 105 district-vintages vanish from the area matrix entirely. Conservative, but the
  ambiguity is never resolved, and 19 registered CKs end up with no area row at all.
- `run_identity.py:441`: `for yr_offset in range(-5, 15): check_year = year - yr_offset`
  searches years `year+5` down to `year-14`. A formation event up to **five years in the
  future** justifies creating a district observed in an earlier vintage. The window is
  asymmetric in the wrong direction.
- `run_identity.py:367-368` indexes renames bidirectionally
  (`renames[parent]=child` *and* `renames[child]=parent`), so a rename can resolve backwards
  onto a district that still exists under its old name.

### 2.5 Lineage edges are built on state-blind name resolution

- `build_gold_events.py:252-269` — `resolve_name_to_ck()` accepts a `state` parameter and
  **never uses it**. It picks the CK whose snapshot year is nearest the event year; ties break
  on `sorted()` over a `set` of tuples, so equal-distance candidates resolve unstably. Two
  districts sharing a name in different states produce a cross-state lineage edge stamped
  `lineage_confidence=0.8`, `lineage_basis='ACADEMIC'`, `evidence_type='OBSERVED'`.
- `build_gold_events.py:88-99` — `split_case` is decided by whether a snapshot carrying the
  parent's normalized name exists in any later year. `name_years` is keyed **on name only**,
  so the `(parent, state)` grouping key at `:94` is discarded at lookup time. And the whole
  `(parent, state)` group takes one verdict from `group["effective_year"].min()`, so a parent
  with a 1961 carve-out and a 2001 clean split gets a single classification for both. The
  architecture (§9) requires this be read from the gazette; it is inferred from name
  persistence.
- `build_gold_events.py:374-378` — for `NEW_DISTRICT`, the code tries to resolve
  `parent_name` as a CK and adds a `PREDECESSOR` if *any* name matches. The comment at `:366`
  concedes the parent is "often a region, not a CK". Region names that collide with district
  names become `FORMED_FROM` edges.

### 2.6 Nothing is reproducible across runs

`build_gold_events.py:31` sets `PIPELINE_RUN_ID = str(uuid.uuid4())` at import, and
`:393-401` deletes and re-inserts all events with fresh `uuid4()` event_ids on every run.
**Event identifiers are not stable.** Every product keyed by `event_id` becomes
un-joinable after a rerun. The Phase 9.1 audit observed the symptom ("binary versus UUID
event identifiers break cross-product provenance") without reaching the cause.

Architecture change **C02** solved exactly this disease for canonical keys — lookup-first,
get-or-create, stable across reruns. The same treatment was never applied to events,
relationships, or overlaps. Every one of them uses `uuid4()`.

### 2.7 Area arithmetic defects in the published products

In `rebuild_decadal_area_accounting.py`:

- `:103-104, 524-525` — `unique_non_null()` applies `pd.unique` before summing.
  `target_total = sum(target_after_values)` at `:557` therefore **counts two equal-area
  targets once**. Same defect for `MERGE` predecessors at `:574`.
- `:705-717` — `build_summary` sums `expected_area_km2` across groups into a field named
  `source_area_before_km2`. But `expected` is a *parent area* for `CLEAN_SPLIT` and a
  *parent loss* for `CARVE_OUT`. Heterogeneous quantities are added together, and
  `unaccounted_area_km2` is derived from the sum. This is a category error in a
  researcher-facing product.
- `:604-612` — when only some targets in a group have geometry, `available` (the full parent
  loss) is allocated across **only the measurable subset**, silently over-allocating to
  whichever children happen to have polygons. There is no `allocation_base_incomplete` flag.
- `:91-100` — geodesic area takes `abs()` of the **signed sum** from
  `Geod.geometry_area_perimeter`. Without normalizing ring orientation first
  (`shapely.ops.orient`), a mis-oriented interior ring inflates the area and the `abs()`
  hides the sign that would have revealed it.
- `:292` — a load-bearing uniqueness guarantee is enforced by a bare `assert`, which
  disappears under `python -O`.
- `:401-406, 796` — vintage bracketing is strict (`vintage < y`, `vintage > y`). An event
  dated 1971 gets `pre=1961, post=1981` — a 20-year window for a change the 1971 map may
  already show. 1,265 events sit in `MODERATE` alignment and 21 have no pre-observation.
- `:790-795` — `change_class` uses `tolerance = max(5 km², 0.5%)`. On a 45,000 km² district
  that is 225 km² — larger than many real transfers — all labelled `STABLE`.

### 2.8 The method drift nobody chased

Five Phase 9 documents state the area method is `ST_Area_Spheroid` on geometries stored in
`geometry_observation.area_sqkm`. The Phase 9 rebuild uses `pyproj.Geod` in pandas
(`:31, :91`) and never reconciles the two. `revised_area_validation.md` then reports a
**median 3.70% and maximum 4.01% difference** between the two methods **on the identical
2025 SOI polygons**, and describes it as a benign "method comparison".

Two calculations of the same polygon should agree to ~1e-6. A 4% gap means one of them is
wrong. This is almost certainly the same defect that produces `frac_from > 1.001` in
§2.2 — the stored `area_sqkm` and the recomputed area do not come from the same method.
It should be a blocking error, not a footnote.

### 2.9 The specification exists only as prose

- `sql/schema/*.sql` — all 7 files **empty**. Not one of the 27 invariants, the exclusion
  constraint (§5), or the CHECK constraints exists as DDL. Every constraint in the frozen
  spec is re-implemented ad hoc in Python, differently, per script.
- `config/matching.yaml`, `pipeline.yaml`, `spatial.yaml`, `validation.yaml` — **empty**.
  Thresholds are literals scattered through code (`window_yrs`, `0.85`, `5.0`, `0.5`).
- `docs/domain/*.md` (5 files), `docs/spatial/*.md` (3), `docs/validation/*.md` (2),
  root `ARCHITECTURE.md`, `README.md` — **all empty**. The rules they are named for live
  nowhere.
- Every module under `src/` except bronze/silver/identity is **0 bytes** — `src/lineage/`,
  `src/spatial/`, `src/validation/` are empty shells. All logic lives in one-shot scripts.
- `declared_area_transfer` and `correction_log` are **never created or written by any
  script**. `declared_area_transfer` is the architecture's designated home (§11) for exactly
  the case in the brief: *territory moved, both districts continue*. The system currently
  has no place to record it.

---

## 3. Root cause

Three, and everything above descends from them.

**RC-1 — Events were made the carrier of area.** The architecture is explicit (Principle 7):
*"Events as evidence, not spatial truth."* The implementation inverted it. Area is computed
per event, so the event grain's defects (§2.1) become arithmetic defects, and the
unidentifiability of per-event attribution (§1) is hidden behind a `MEASURED` label.

**RC-2 — Pairwise intersection was used where a partition was required.** Summing pairwise
intersections has no conservation property, so weights exceed 1, so normalization was
introduced to force closure, so a fictional diagnosis was needed to justify normalization.
A planar overlay of two vintages conserves area *by construction* and needs none of it.

**RC-3 — Identity was resolved before territory was measured.** CKs are assigned by name
equality (§2.4) and everything else is built on them. But identity *is* a territorial
question: two districts are the same entity if they occupy substantially the same ground.
The pipeline runs the dependency backwards.

---

## 4. The redesign

One primitive, computed once, from which lineage, area accounting, and statistical
harmonization are all derived — so they cannot disagree.

### 4.1 The primitive: the vintage transition matrix

For each consecutive vintage pair `(V_t, V_{t+1})` — 1951→1961, …, 2021→2025:

**Step 1 — Per-layer topology audit (do this first; it gates everything).**
Before any cross-vintage work, test each vintage layer as a planar partition of the
national footprint:
- self-overlap area: `Σ area(d_i ∩ d_j)` for all `i<j` within the layer — should be ~0
- gap area: `area(national_footprint) − area(∪ d_i)`
- total layer area vs. the published national land area for that year

Publish a per-layer topology budget. If the 1991 layer self-overlaps materially, **nothing
computed from it is trustworthy**, and that must be known before, not after, publishing
transfer estimates. This check does not exist today and is the cheapest high-value thing in
this document.

**Step 2 — Overlay.** Compute the planar overlay (identity/union) of layer `t` and layer
`t+1`. This yields atomic parcels, each labelled with exactly one source district, one
target district, and an area. Parcels are mutually exclusive and exhaustive.

**Step 3 — Residuals, named not distributed.**
- parcel in `t` with no counterpart in `t+1` → `LOST_TO_UNMAPPED`
- parcel in `t+1` with no counterpart in `t` → `GAINED_FROM_UNMAPPED`
These are real (coastline digitization, disputed territory, differing national coverage).
They are reported as an explicit residual mass and **never redistributed**.

**Step 4 — Sliver filter.** A parcel is a sliver when
`area < max(0.5 km², 0.01% × min(area_src, area_tgt))` **and** its thinness ratio
(`4π·area/perimeter²`) is below threshold. Slivers are aggregated into a per-pair sliver
budget, excluded from transfers, and reported. Thresholds live in `config/spatial.yaml`
with a written rationale, per the architecture's `threshold_rationale` requirement.

**Step 5 — The matrix.**
```
M_t[i][j] = Σ area(parcels from district i at V_t to district j at V_{t+1})   [km²]
```
with the exact identity, per source district `i`:
```
Σ_j M_t[i][j] + lost_i + sliver_i = area_i(V_t)
```
Conservation holds **by construction**. There is nothing to normalize. Invariant S-4
(`Σ area_weight ≤ 1.001`) becomes structurally true rather than enforced after the fact.

Row-normalized `M̂` (outflow shares) and column-normalized `M̌` (inflow shares) are stored
alongside. `LOST`/`GAINED` are carried as absorbing states so rows and columns each sum to
exactly 1.

### 4.2 Identity derived from continuity, not from names

For source `i` and target `j` in a transition:
```
retention  r_i  = max_j M̂[i][j]        (largest share of i's territory going to one target)
inheritance h_j = max_i M̌[i][j]        (largest share of j's territory coming from one source)
```

| Condition | Meaning | CK action |
|---|---|---|
| `r_i ≥ θ_c` and `h_j ≥ θ_c` for the same `(i,j)` | Same entity, boundary possibly modified | **Same CK** |
| `r_i ≥ θ_c`, `h_j < θ_c` | `i` continues; `j` absorbed territory | Same CK for `i` |
| `r_i < θ_c`, `h_j ≥ θ_c` | `i` dissolved into several; `j` dominated by one source | New CK for `j` |
| neither | Genuine reorganization | Review queue |

`θ_c` defaults to 0.90, configurable, with rationale. Name similarity enters as a **prior
that raises or lowers confidence**, never as the decision — inverting today's logic (§2.4).
Cases below threshold go to quarantine for human adjudication, which is what the
architecture already specifies and the current matcher never does on spatial grounds.

This also resolves `AMBIGUOUS_DUPLICATE_CK_VINTAGE` structurally: two polygons in one
vintage cannot both hold continuity with one predecessor, because the shares are computed
from a partition and must sum to 1.

**Note:** re-deriving CKs breaks the frozen "CK never reused" invariant (I-3) if done in
place. Do it as a new CK generation with a `ck_migration` mapping table from the old
registry, preserving both. Invariant I-2 (never delete) is honoured; the old CKs become
superseded, not removed.

### 4.3 Lineage classification from the matrix

Applied per transition window, with all thresholds in config:

| Pattern | Classification | Architecture type |
|---|---|---|
| `r_i ≥ θ_c`, `h_j ≥ θ_c`, `M[i][j]` ≈ both areas | Continuity | *(no edge)* |
| `r_i ≥ θ_c`, and ≥1 other target `k` with material `M[i][k]` | Carve-out | `FORMED_FROM` (i→k) |
| `r_i < θ_c`, territory spread over ≥2 targets, no continuity target | Clean split | `SPLIT_FROM` (i→each) |
| ≥2 sources with `r ≥ θ_c` into one target, none continuing | Merge | `MERGED_INTO` |
| Both `i` and `j` continue, `M[i][j]` material and bidirectional or small | **Territorial transfer** | **no edge** — area ledger only |
| Continuity, name changed | Rename | *(no edge)* |

The last two rows are the brief's specific question, and they are the two the current
system cannot express: `district_relationship` correctly excludes them (C07 removed
`TRANSFERRED_AREA`, `RENAMED_FROM`, `BOUNDARY_MODIFIED`), but the table that was supposed
to receive them — `declared_area_transfer` — was never built (§2.9). Result: transfers
between two continuing districts currently vanish, or get misfiled as `FORMED_FROM`.

**Architecture gap to close:** §11 defines `declared_area_transfer` for *gazette-declared*
quantities, with `source_id` in the grain. There is no home for a *measured* transfer
derived from the overlay. Add a sibling `measured_area_transfer` (grain: from_ck × to_ck ×
transition_window, `evidence_type = DERIVED`, `derived_from_ids` = parcel IDs), or widen
`declared_area_transfer` to admit `evidence_type = DERIVED` with the transition window in
the grain. The first is cleaner: it keeps "what the gazette said" and "what the maps show"
separately measurable and comparable — which is itself a validation.

### 4.4 Events become evidence, restoring Principle 7

The event CSV stops driving area and starts doing three jobs it can actually do:

1. **Name the change** — supply the administrative label (SPLIT / NEW_DISTRICT / RENAME)
   and the gazette reference when one exists.
2. **Date it within the window** — geometry gives a 10-year bracket; the event gives a year.
3. **Corroborate or contradict the geometric classification** — for each window, compare
   the classification derived in §4.3 against the one implied by the events. Agreement
   raises confidence; disagreement is the research queue. This is a real scientific output
   and costs almost nothing once the matrix exists.

Required fixes to the event layer regardless of the redesign:
- **Aggregate to the true event grain**: `(state, parent_district, effective_year, event_type)`
  → one event, N successors. This alone makes conservation testable (§2.1).
- **Deterministic IDs**: `event_id = sha256(natural_key)`, never `uuid4()`. Same for
  relationships and parcels. Reruns must be idempotent (§2.6).
- **State-scoped name resolution**: pass and use `state` in `resolve_name_to_ck`; stable
  tie-breaking; unresolved names go to the failure log rather than to the nearest match.

### 4.5 Per-event attribution, honestly bounded

For a district in window `(V_t, V_{t+1})`, let `E` be the set of events touching it:

- `|E| = 0` and material area change → `UNEXPLAINED_CHANGE` (a finding worth investigating:
  either a missing event or a boundary revision in the source data)
- `|E| = 1` → the window change **is** that event's change. `status = MEASURED`.
- `|E| ≥ 2` → **`UNIDENTIFIED_MULTI_EVENT_WINDOW`**. Report the window total as a bound on
  each event; do not apportion. If gazette areas exist for some events, subtract them and
  attribute the remainder.

This one rule reclassifies a large share of today's fake `MEASURED` rows as honestly
bounded, and legitimately measures the rest. It is the direct answer to §1's constraint.

### 4.6 Origin and fate as matrix composition

The question "where did this district come from" has a closed-form answer once the matrices
exist. For vintages `a < b`:
```
A(a→b) = M̂_a · M̂_{a+1} · … · M̂_{b-1}
```
- `A[i][j]` = fraction of ancestor `i`'s territory at `a` now inside `j` at `b` — **fate**.
- The column-normalized product gives the fraction of `j`'s present territory that came
  from `i` at `a` — **origin decomposition**.

Two products serve the brief directly:
- `district_ancestry` — for each district at any vintage, its origin decomposition back to
  1951, ordered by share.
- `district_descendancy` — the transpose.

Carrying `LOST`/`GAINED` as absorbing states keeps the chain stochastic, so composition
degrades gracefully across windows with poor coverage instead of silently renormalizing.

The graph (§4.3) tells the administrative story; the matrix product gives the quantitative
ancestry. They are consistent because both derive from the same overlay — which is the
consistency property the current three-code-path design lacks.

### 4.7 One primitive, three products

The same `M` is:
- the **area ledger** (§4.5),
- the **lineage graph** (§4.3),
- and the **geometric crosswalk** for statistical harmonization — `M̂[i][j]` *is*
  `geometric_crosswalk.area_weight`, and `1 − lost_i/area_i` *is* `coverage_fraction`.

Statistical harmonization then follows the architecture as written (§13): `statistical_weight`
starts from `M̂`, with `weighting_method` and a non-empty `distribution_assumption`, and
**normalization is deleted** — closure is structural. That removes §2.3 entirely and makes
C04's coverage_fraction model real rather than aspirational.

---

## 5. Validation that would actually bite

Current validation checks that numbers are well-formed. These check that they are right.

| # | Check | Fails when | Severity |
|---|---|---|---|
| V1 | Per-layer self-overlap area | > 0.1% of layer area | ERROR — blocks the vintage |
| V2 | Per-layer gap vs national footprint | > 1% unexplained | WARNING + published budget |
| V3 | Row closure: `Σ_j M[i][j] + lost + sliver = area_i` | > 1e-6 relative | ERROR — overlay is broken |
| V4 | Stored `area_sqkm` vs recomputed geodesic area | > 0.01% | ERROR — resolves §2.8 |
| V5 | Continuity determinism: rerun produces identical CK assignments | any diff | ERROR |
| V6 | Event/geometry classification agreement | report rate; no threshold | INFO — research queue |
| V7 | Ancestry rows sum to 1 after composition | > 1e-9 | ERROR |
| V8 | Every published `MEASURED` row has `|E| = 1` in its window | any violation | ERROR |
| V9 | No `district_relationship` edge where both endpoints hold continuity | any | ERROR — enforces L-3 |

V1 and V4 should run before anything else is rebuilt. They are cheap, and if either fails,
the current products are unsalvageable regardless of what else changes.

---

## 6. Sequencing

Each phase is independently useful and independently verifiable.

**P0 — Stop the bleeding.** Deterministic IDs everywhere (§2.6). Withdraw or clearly mark
the normalized crosswalks and the tautological overlap claim in `revised_area_validation.md`.
Write the empty spec files: `sql/schema/*.sql` as real DDL carrying the 27 invariants, and
the four `config/*.yaml` with every threshold and its rationale.

**P1 — Ground truth on the inputs.** Run V1 and V4 on all 9 vintages. Publish the topology
budget. This determines whether anything downstream is worth rebuilding, and it is the
cheapest step in the plan.

**P2 — The primitive.** Build the overlay and `M` for the 8 consecutive vintage pairs.
Validate with V3. Retire `spatial_overlap` as currently computed (§2.2).

**P3 — Identity.** Re-derive continuity-based CKs with a `ck_migration` table. Quarantine
sub-threshold cases for review rather than auto-assigning.

**P4 — Lineage and events.** Aggregate the event grain, rebuild edges from §4.3, produce
the event/geometry agreement report.

**P5 — Ledger, transfers, ancestry.** Area ledger with §4.5 identifiability status;
`measured_area_transfer`; `district_ancestry` / `district_descendancy`.

**P6 — Harmonization.** Rebuild `statistical_crosswalk` from `M̂`. Delete normalization.

**Open decision, unchanged and still blocking:** OD-01 (reclassification CK rule). Note that
§4.2's continuity test gives it an empirical answer that the name-based matcher never could:
if a reclassified entity's territory holds continuity across the gap, it is the same entity.
That reduces OD-01 from a domain ruling to a threshold choice — worth raising with Prof.
Patel as a reframing rather than a question.

---

## 7. What this fixes

| Current symptom | Root cause | Fixed by |
|---|---|---|
| 49/50 clean splits fail conservation | RC-1, event grain (§2.1) | §4.4 event aggregation |
| 301/480 conservation failures | RC-1 | §4.1 partition + §4.4 |
| Raw weight sums to 5.32, 24.25 | RC-2, vintage mixing (§2.2) | §4.1 adjacent-vintage overlay |
| Normalization contradicting C04/S-5 | RC-2 | §4.7 — closure is structural |
| "Zero overlap" is a tautology | Single-member groups (§2.1) | §4.4 — real n:m groups |
| 105 ambiguous CK/vintage, 19 CKs with no area | RC-3, name matching (§2.4) | §4.2 continuity identity |
| 1058/1550 rows UNMEASURED | Missing lineage + geometry | §4.1 measures territory regardless of lineage |
| Per-event areas asserted, not identified | RC-1 (§1) | §4.5 identifiability rule |
| Transfers between continuing districts unrecordable | §2.9 | §4.3 + `measured_area_transfer` |
| Products un-joinable after rerun | `uuid4()` (§2.6) | §4.4 deterministic IDs |
| 3.7% area discrepancy on identical polygons | Method drift (§2.8) | V4 |
| 27 invariants exist only as prose | §2.9 | P0 DDL |
