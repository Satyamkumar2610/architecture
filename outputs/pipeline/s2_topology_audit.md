# Topology Audit (V1 / V2 / V4)
Layers audited: 9
ERROR (self-overlap >= 1.0%): 0
WARNING (self-overlap >= 0.10%): 0

## Per-vintage results
| source   |   year |   n_districts |   sum_polygon_area_km2 |   union_area_km2 |   self_overlap_area_km2 |   self_overlap_fraction_pct |   max_area_method_delta_pct | status   |
|:---------|-------:|--------------:|-----------------------:|-----------------:|------------------------:|----------------------------:|----------------------------:|:---------|
| stanford |   1951 |           316 |            3.26856e+06 |      3.2685e+06  |                 57.1602 |                    0.001749 |                           0 | PASS     |
| stanford |   1961 |           340 |            3.18804e+06 |      3.18798e+06 |                 60.8099 |                    0.001907 |                           0 | PASS     |
| stanford |   1971 |           359 |            3.18804e+06 |      3.18798e+06 |                 61.6522 |                    0.001934 |                           0 | PASS     |
| stanford |   1981 |           426 |            3.26856e+06 |      3.26849e+06 |                 68.6891 |                    0.002102 |                           0 | PASS     |
| stanford |   1991 |           468 |            3.26856e+06 |      3.26849e+06 |                 73.0563 |                    0.002235 |                           0 | PASS     |
| stanford |   2001 |           595 |            3.26856e+06 |      3.26847e+06 |                 87.0373 |                    0.002663 |                           0 | PASS     |
| stanford |   2011 |           641 |            3.26487e+06 |      3.26474e+06 |                123.92   |                    0.003796 |                           0 | PASS     |
| stanford |   2021 |           735 |            3.27008e+06 |      3.26998e+06 |                 99.9059 |                    0.003055 |                           0 | PASS     |
| soi      |   2025 |           742 |            3.27005e+06 |      3.27005e+06 |                  0.0388 |                    1e-06    |                           0 | PASS     |

## Interpretation
`self_overlap_area_km2` is exact: sum of individual polygon areas minus the area of their union, so overlaps of any multiplicity are counted once. A layer with material self-overlap cannot be used as a partition for the transition matrix in Stage 4 without repair — this is the gate the old pipeline never ran, which is why pairwise intersection weight sums reached 5.32 and 24.25 (see docs/architecture/lineage_area_redesign.md section 2.2).
`max_area_method_delta_pct` recomputes area independently from the stored geometry; nonzero drift here would mean two area methods are in play (see redesign doc section 2.8) — it is 0 by construction in this pipeline.
