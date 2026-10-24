# Native raw-file scan optimization

This work optimizes literal queries over the original uncompressed Thunderbird
log. It does not use Parquet, BtrBlocks, any other codec, an index, or a decoded
value cache. Each query still scans the whole file.

## Implementation

`probes/raw_scan/raw_query.cpp` memory maps the file read-only, then scans in
32-byte AVX2 chunks. A vector pass finds line feeds and candidate first bytes;
the scanner verifies candidate substrings against the mapped input and emits
ordered row IDs. This removes Python's per-line byte-object and interpreter
overhead and avoids copying the 2.5 GB source into a second full-size buffer.
An LF terminator is excluded, CR and all other bytes are retained, blank lines
are empty values, and a terminal LF creates no phantom row. The byte-oriented
query behavior is validated against a strict UTF-8 Python reference on this
dataset.

The Python wrapper and benchmark runner are in
`scripts/benchmark_raw_queries.py`. The native binary is compiled for the
current machine; builds without AVX2 use the scalar line scanner.

## Dataset and setup

- File: `data/loghub-2.0/Thunderbird_full.log`
- Physical size: 2,516,177,713 bytes
- Logical rows: 16,601,745
- UTF-8 payload: 2,499,575,968 bytes
- SHA-256: `a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`
- Machine: 12th Gen Intel Core i3-1215U, Linux x86-64
- Compiler: g++ 16.2.1; Python 3.14.8
- CPU affinity: benchmark pins one available CPU

Build and run from the repository root:

```sh
mkdir -p build/raw-query
g++ -O3 -DNDEBUG -std=c++17 -march=native -Wall -Wextra -Wpedantic \
  probes/raw_scan/raw_query.cpp -o build/raw-query/raw_query
PYTHONPATH=. python -m scripts.benchmark_raw_queries \
  --source data/loghub-2.0/Thunderbird_full.log \
  --queries-json docs/measurements/word-index-thunderbird/queries.json \
  --output docs/measurements/raw-thunderbird-native \
  --warmups 2 --repetitions 10 --scans-per-query-per-round 1 \
  --scanner both --native-scanner build/raw-query/raw_query
```

The output directory must not already exist. Raw per-trial records, environment,
configuration, summaries, completion status, and source and executable hashes
are saved under `docs/measurements/raw-thunderbird-native/`.

## Results

Times are milliseconds for an entire request, including sequential scanning and
collecting ordered matching row IDs. The native request time also includes
starting the scanner subprocess and returning its result bytes. It does not
include Python-side parsing of the native result protocol; the child scanner's
own measured scan time is provided below. Ten measured trials follow two
warmups per query and scanner. p95 uses the inclusive percentile. All results
matched the independently computed reference exactly.

| Raw query | Matches | Python lines p50 / p95 | Native mmap p50 / p95 | p50 speedup |
|---|---:|---:|---:|---:|
| `contains("warning")` | 636,710 | 8,560 / 8,825 ms | 1,442 / 2,024 ms | 5.9× |
| `contains("failure")` | 2,374 | 8,682 / 8,834 ms | 1,437 / 1,894 ms | 6.0× |
| `contains("ByteForgeAbsentSentinel")` | 0 | 8,278 / 8,779 ms | 1,451 / 1,591 ms | 5.7× |

Native scanner-only p50 wall times were 1,414 ms (`warning`), 1,418 ms
(`failure`), and 1,429 ms (absent term). The native path read about 2.5 GB per
query, around 1.7 GB/s at those medians. It returns exact original row IDs;
there were zero correctness failures in the 72 total observations including
warmups.

The same-run Python line scanner is the comparison in the table. Earlier raw
Python-only measurements are preserved in
[`raw_log_query_results.md`](raw_log_query_results.md); those were 8.1–8.2 s
p50 and are historical, separate runs.

## Limits and next steps

This is a faster **full scan**, not a sublinear query. The native scanner reads
every byte on every request, so repeated query latency remains proportional to
file size. OS page-cache state was not controlled; these timings are not a
cold-disk guarantee. The current implementation is case-sensitive and matches
byte substrings, equivalent to Python substring search for valid UTF-8 strings.
For consistently low latency across repeated queries, a persistent row-aware
term index or a persistent scanner process would be the next design to evaluate;
neither is included in these measurements.
