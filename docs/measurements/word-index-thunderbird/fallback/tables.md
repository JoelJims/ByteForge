| Configuration | Archive + optional index, MB | Additional word index, MB |
|---|---:|---:|
| parquet-lz4 | 299.019 | 0.000 |
| block-b-4096-word | 840.099 | 184.400 |

| Configuration / query | Cold p50 / p95, ms | Warm p50 / p95, ms | Cold parent CPU p50, ms | Matches | Trials |
|---|---:|---:|---:|---:|---:|
| parquet-lz4 / sampled-timestamp | 6309.90 / 6387.82 | 6003.08 / 6039.84 | 6180.19 | 1 | 2 |
| parquet-lz4 / punctuation-sentinel | 5591.92 / 5655.25 | 5356.34 / 5359.55 | 5475.51 | 0 | 2 |
| block-b-4096-word / sampled-timestamp | 12514.13 / 12531.63 | 12171.64 / 12254.95 | 6935.20 | 1 | 2 |
| block-b-4096-word / punctuation-sentinel | 574.58 / 628.65 | 69.26 / 70.97 | 412.73 | 0 | 2 |
