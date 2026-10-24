# Direct queries on the raw Thunderbird log

This benchmark searches the original uncompressed log file directly. It uses no
Parquet, BtrBlocks, compression, sidecar index, or decoded-value cache.

## Results

The source contains **16,601,745 rows** and **2,499,575,968 UTF-8 payload bytes**
(2,516,177,713 file bytes). Each query scans the entire file sequentially and
returns ordered row IDs. The table pools 20 measured scans per query (two scans
per query in each of ten repetitions); p95 is the inclusive sample percentile.
Times are seconds.

| Raw-file query | Matches | p50 | p95 | Range | Process CPU p50 |
|---|---:|---:|---:|---:|---:|
| `contains("warning")` | 636,710 | 8.142 | 8.294 | 8.093–8.536 | 7.645 s |
| `contains("failure")` | 2,374 | 8.166 | 8.565 | 8.003–8.872 | 7.692 s |
| absent `contains("ByteForgeAbsentSentinel")` | 0 | 8.078 | 8.602 | 7.911–8.636 | 7.577 s |

The scan rate is approximately **308 MB/s** using logical payload bytes and
median wall time. All 72 recorded scans, including warmups, matched an
independent UTF-8 text scan: **zero result mismatches**. The source SHA-256 is
`a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`.

No cold-disk claim is made. Each request reopens the raw file; OS page-cache state
was uncontrolled. The run initially recorded arbitrary `cold`/`warm` labels on
randomized scans. Those labels were discarded, and the report pools the 20
measured scan observations per query. Timing includes scanning lines and
collecting matching row IDs, but excludes the separate reference scan and result
digest. The timed path searches the original valid UTF-8 bytes using UTF-8 encoded
query bytes; the independent reference decodes each row and searches Python
strings. Only LF record terminators are excluded; CR and all other content are
preserved. A terminal LF adds no extra row.

## Reproduction

From the repository root:

```sh
PYTHONPATH=. .venv/bin/python -m scripts.benchmark_raw_queries \
  --source data/loghub-2.0/Thunderbird_full.log \
  --queries-json docs/measurements/word-index-thunderbird/queries.json \
  --output results/my-raw-query-run --warmups 2 --repetitions 10 \
  --scans-per-query-per-round 2
```

The source is checked by SHA-256 before the run. Each query's oracle is computed
outside the timed section. The runner pins one available CPU and stores raw JSONL
observations, configuration, summary and completion status. New output paths must
not already exist.

Raw configuration and observations are in
[`measurements/raw-thunderbird-only/`](measurements/raw-thunderbird-only/).

An optimized native memory-mapped scanner was subsequently measured against the
same raw-file queries. See [native raw-scan results](raw_scan_optimization_results.md)
and the new per-trial observations in
[`measurements/raw-thunderbird-native/`](measurements/raw-thunderbird-native/).
