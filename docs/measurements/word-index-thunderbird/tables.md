| Configuration | Archive + optional index, MB | Additional word index, MB |
|---|---:|---:|
| parquet-lz4 | 299.019 | 0.000 |
| block-b-256 | 818.064 | 0.000 |
| block-b-4096-word | 840.099 | 184.400 |
| parquet-lz4-word | 483.420 | 184.400 |

| Configuration / query | Cold p50 / p95, ms | Warm p50 / p95, ms | Cold parent CPU p50, ms | Matches | Trials |
|---|---:|---:|---:|---:|---:|
| parquet-lz4 / common-warning | 5389.36 / 5975.84 | 5034.18 / 5587.78 | 5313.38 | 636,710 | 10 |
| parquet-lz4 / rare-failure | 5564.79 / 6565.88 | 5433.04 / 6535.11 | 5528.96 | 2,374 | 10 |
| parquet-lz4 / absent-word | 5280.17 / 5815.86 | 5051.61 / 5261.34 | 5225.53 | 0 | 10 |
| block-b-256 / common-warning | 14518.37 / 15911.85 | 12808.12 / 16883.49 | 9161.49 | 636,710 | 10 |
| block-b-256 / rare-failure | 1955.43 / 2344.09 | 680.17 / 904.33 | 1733.67 | 2,374 | 10 |
| block-b-256 / absent-word | 1286.17 / 1565.07 | 42.87 / 52.21 | 1249.86 | 0 | 10 |
| block-b-4096-word / common-warning | 380.87 / 516.97 | 147.84 / 158.18 | 318.32 | 636,710 | 10 |
| block-b-4096-word / rare-failure | 134.88 / 376.93 | 2.63 / 7.93 | 134.12 | 2,374 | 10 |
| block-b-4096-word / absent-word | 135.52 / 374.35 | 3.88 / 8.59 | 135.14 | 0 | 10 |
| parquet-lz4-word / common-warning | 362.13 / 550.27 | 150.59 / 165.79 | 297.39 | 636,710 | 10 |
| parquet-lz4-word / rare-failure | 139.93 / 402.37 | 2.48 / 9.81 | 139.78 | 2,374 | 10 |
| parquet-lz4-word / absent-word | 145.63 / 365.93 | 3.91 / 8.96 | 143.93 | 0 | 10 |
