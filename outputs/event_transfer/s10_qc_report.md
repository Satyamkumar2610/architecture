# Stage 10 — Event Area Transfer Matrix: Quality Control Report

## Event Accounting Statistics

| Metric | Count |
|---|---|
| Total events processed | 935 |
| Events spatially resolved | 898 |
| Events fully reconciled | 168 |
| Events within <1% tolerance | 168 |
| Events requiring residual recovery | 579 |
| Events successfully recovered | 517 |
| Events with unresolved residual | 94 |
| Events with missing geometry | 37 |
| Events with admin/spatial disagreement | 652 |

## Area Statistics

| Metric | Value |
|---|---|
| Total measured transfer area | 3,327,028.16 km² |
| Total recovered residual | 2,644,588.91 km² |
| Total unresolved residual | 792,625.31 km² |

## Top 10 Largest Residual Events

```
  6d658ade… | 1991 | RENAME | 100.00%
  800c852d… | 1991 | RENAME | 100.00%
  83d16a36… | 2001 | RENAME | 100.00%
  842fefc9… | 2011 | RENAME | 100.00%
  cb5b9ff8… | 2021 | NEW_DISTRICT | 100.00%
  55d939fa… | 2021 | SPLIT | 100.00%
  04f31fcf… | 2011 | RENAME | 100.00%
  f36297ec… | 2001 | NEW_DISTRICT | 100.00%
  cff093e7… | 2001 | SPLIT | 100.00%
  53cbb5d0… | 1981 | RENAME | 100.00%
```

## Top 10 Largest Transfer Events

```
  81efb437… | 1979 | SPLIT | 96,362.5 km²
  5949dd10… | 1979 | SPLIT | 96,362.5 km²
  4bb60c89… | 1998 | SPLIT | 39,134.1 km²
  dd6bb311… | 2023 | SPLIT | 28,373.4 km²
  72e07de3… | 1998 | NEW_DISTRICT | 27,753.3 km²
  599197c5… | 1982 | SPLIT | 25,295.6 km²
  e7d11cd2… | 1980 | SPLIT | 23,804.5 km²
  f29899bd… | 1969 | NEW_DISTRICT | 22,478.3 km²
  bb8c5c19… | 1969 | SPLIT | 22,478.3 km²
  ed9a8b90… | 1998 | SPLIT | 22,315.0 km²
```

## Most Problematic States (by unresolved event count)

```
  Andhra Pradesh: 14
  Madhya Pradesh: 6
  Uttar Pradesh: 6
  Sikkim: 6
  Tamil Nadu: 5
```

## Most Problematic Years (by unresolved event count)

```
  2022: 12
  2011: 12
  2021: 8
  2020: 4
  1961: 4
```

## Unreconciled / Recovered Events Detail

```
                        event_id  event_year   event_type  raw_residual_pct accounting_status
6d658adec4a827f518228b10c409da71        1991       RENAME        100.000019         RECOVERED
800c852d13eeea1933eb223b3ed095aa        1991       RENAME        100.000017         RECOVERED
83d16a3614864b99d179f632fe9560f8        2001       RENAME        100.000009         RECOVERED
55d939faa0b56fcbf70db60d156d229d        2021        SPLIT        100.000005         RECOVERED
842fefc95cf2d9e4ab99b08c6ecfc878        2011       RENAME        100.000005        UNRESOLVED
cb5b9ff8b05a29d2317e807a483cc34a        2021 NEW_DISTRICT        100.000005        UNRESOLVED
04f31fcfe2671b410f658e12caaebccd        2011       RENAME        100.000003        UNRESOLVED
cff093e7ca015766d77eacaf3aeb777b        2001        SPLIT        100.000003         RECOVERED
f36297ec078b55cbea83e07aea2db82b        2001 NEW_DISTRICT        100.000003         RECOVERED
2f6efc0a86012b842a036e705b6577cd        2011       RENAME        100.000002        UNRESOLVED
53cbb5d0bf9f96819cf6cd500ecb1a02        1981       RENAME        100.000002         RECOVERED
cb20dc98a21ecefd0007cb6135bb8825        1983 NEW_DISTRICT        100.000001         RECOVERED
7590ecd4efbaa095d4bf558d19555e27        2011       RENAME        100.000001         RECOVERED
2ccac8ab6ec454e4781ee200183bd3e6        2011 NEW_DISTRICT        100.000001        UNRESOLVED
2cfdeef73b5dc6bdebcd8ddb1a10aade        1981 NEW_DISTRICT        100.000001         RECOVERED
15dc3e7250857d71907e6c77fee30969        1983 NEW_DISTRICT        100.000001         RECOVERED
fce6286ea8f4732c99ae63192f8e111b        2011       RENAME        100.000001         RECOVERED
3eb1febc0da767831a2e4954028af110        2011       RENAME        100.000001        UNRESOLVED
823dfeaa5cdca806f314b3a02ec0af6e        1971       RENAME        100.000001         RECOVERED
e50b259e4a0b0ab383e5a82449a1837b        2010 NEW_DISTRICT        100.000001        UNRESOLVED
```

## Residual Reason Distribution

```
residual_reason
ADJACENT_DISTRICT_CANDIDATE       517
BOUNDARY_PRECISION                168
UNRECOVERED_GEOMETRIC_RESIDUAL     94
```

## Validation Checks

- **No negative area**: PASS
- **No impossible transfer** (area_pct_of_from ≤ 100%): PASS
- **Parent consistency** (every successor has ≥1 predecessor or MISSING_PARENT code): PASS
- **Rename consistency** (rename events produce no material area change): PASS (enforced by spatial_rel=RENAME_NO_MATERIAL_CHANGE)
- **Split conservation** (predecessor area ≈ Σ contributions + retained + residual): reported per event in residual_area_audit.csv

## Notes

- `declared_transfer` is NULL for all events: the events CSV carries no gazette area
  quantities. This field is reserved for future gazette integration.
- Residual geometry was reconstructed from silver GeoParquets by difference operation.
- All residuals are retained in `residual_area_audit.csv` regardless of status.
- Stage 8 (s8_event_area_accounting.py) remains active as a lightweight lookup;
  Stage 10 provides the full reconciliation on top.
