# Log-query and decompression latency

## What was slow

The previous B result was the explicitly diagnostic `test-only-byte-symbols-v1`,
not SGTT. It expands every byte into an integer ID rather than exploiting an
LLM-derived substring table. The resulting numeric stream is a poor substitute
for the repeated-byte-pattern compression that Zstd applies directly to logs.
This explains the observed archive-size disadvantage; it does not establish how
actual SGTT would behave. SGTT source/table permissions remain unresolved.

The recorded profile also showed a software bottleneck: about 57 ms in Python
reconstruction/validation versus 3.25 ms in native decompression IPC. Repeated
per-token generators, range checks and temporary serialization dominated much of
the restore path. Native IPC time is not an isolated codec kernel measurement.

## Implemented changes

- Convert native byte IDs to a contiguous byte buffer in C, checking the entire
  ID range, then decode exact row slices. Retain offset/type/size/UTF-8 checks,
  full archive integrity checks and exact reconstruction validation.
- Remove redundant validation/temporary serialization from generic framing.
  The actual SGTT encoder is still blocked, never substituted.
- Add literal `equals` and case-sensitive `contains` filters returning ordered
  row IDs and count, using Arrow compute kernels. Queries do not export an
  intermediate reconstructed log or canonical file. This is not a SQL engine.
- Retain decoder handles across queries. Default cache budget zero means every
  query still reads and decompresses. An explicit all-or-nothing Arrow-buffer
  cache applies the same byte limit to both A and diagnostic B. Results are not
  cached. Cached queries trade RAM for latency; they are not decompression tests.
- Add opt-in Parquet `fast-scan`: Zstd level 1, dictionary disabled. Standard
  settings remain unchanged. This is an empirical candidate, not a guarantee
  that a lower compression level always improves reads.

Archives must remain immutable during a query session: checksums are checked at
open, not recomputed on every cached query. Cache limits cover retained Arrow
buffers, not total Python/native RSS or temporary decoding buffers. Oversized
inputs fall back to scans instead of retaining an unlimited table. The decoder
still uses a trusted-input native format. No indexes or new stored sidecars are
hidden outside the archive size.

## Measured reconstruction result

Dataset: unchanged official `data/hdfs/HDFS_2k.log`, 2,000 rows, 285,848 physical
bytes, 283,848 logical payload bytes. SHA-256:
`6fe25449e79d75e35bb223ead9729fa02c00b7abb23e4e8ec0f3bb2addec6e3a`.
This is a small real sample, not the full HDFS corpus.

The paired experiment restores the **same 233,320-byte archive**, using the same
non-sanitized `fsst-safety-v1` worker, before (`41f6ae6`) and after (`3ff31e4`).
Old Python source is extracted with `git archive` into an owned results directory;
no branch switching or user work modification occurs. Both variants use fresh
Python/native processes, one CPU, two warmups and ten measured trials, alternating
order. All 24 physical outputs match the original exactly; zero failures.

| Metric | Before median [min,max], ms | After median [min,max], ms |
|---|---:|---:|
| Archive to exact raw log | 83.055 [77.719,98.827] | 28.234 [23.645,36.221] |
| Token decoding + validation | 56.276 [54.710,62.253] | 0.949 [0.929,1.574] |

Reconstruction is approximately **2.94× faster** in this paired sample experiment.
Logical-payload throughput increases from 3.42 to 10.05 MB/s (decimal bytes,
complete reconstruction timer). This is not pure codec throughput. Launch/import
wall time is recorded separately; OS caches are uncontrolled. Full restore still
includes archive verification, allocation, native IPC, canonical writing, raw
export and checksums. The improvement specializes the known diagnostic byte table;
it is not evidence of an SGTT speedup.

A separate complete run measured fast-scan A at 57,655 total bytes (53,884 B Parquet,
3,771 B metadata), median encode 9.496 ms and reconstruction 15.631 ms. Diagnostic
B remained 233,320 B, medians 70.456/24.763 ms. All 24 round trips passed. The
standard A archive was 64,278 B; this sample did not require sacrificing storage
for fast-scan. The full-restore A medians are from separate runs, so no paired
A reconstruction improvement is claimed. Query measurements below compare the
profiles directly.

## Usage

Run from the repository root with the existing pinned environment. Choose a new
output directory when repeating benchmark commands; existing results are preserved.

```sh
.venv/bin/python -m byteforge run --path data/hdfs/HDFS_2k.log \
  --output results/my-fast-scan --parquet-profile fast-scan
.venv/bin/python -m byteforge query \
  results/log-latency-fast-scan/round-002-A/archive \
  --operation contains --value WARN
```

For repeated interactive queries, keep a session open:

```python
from pathlib import Path
from byteforge.query import QuerySession

with QuerySession(Path("results/log-latency-fast-scan/round-002-A/archive"),
                  cache_bytes=16 << 20) as logs:
    warnings = logs.filter("contains", "WARN")  # first request reads/decompresses
    matches = logs.filter("contains", "blk_38865049064139660")
    print(warnings["count"], matches["row_ids"])
```

The same API accepts diagnostic B with
`worker=Path("build/sgtt-controls-release/byteforge_integer_worker")`.
For a single CLI invocation, cache population cannot benefit future invocations;
use the persistent Python session for that benefit. UTF-8 content, case, whitespace,
empty strings, punctuation and row order are preserved. No normalization or regex
interpretation is performed. Literal `contains` matches must stay within a row.

## Reproduction and validation

```sh
.venv/bin/python -m scripts.compare_restore \
  --archive results/sgtt-hdfs-2k-release-controls/round-002-B-test-only/archive \
  --worker build/sgtt-controls-release/byteforge_integer_worker \
  --source data/hdfs/HDFS_2k.log --output results/my-restore-comparison
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ARROW_NUM_THREADS=1 \
  .venv/bin/python -m scripts.benchmark_log_queries \
  --source data/hdfs/HDFS_2k.log \
  --archive parquet-standard=results/sgtt-hdfs-2k-release-controls/round-002-A/archive \
  --archive parquet-fast-scan=results/log-latency-fast-scan/round-002-A/archive \
  --archive byte-btrblocks=results/log-latency-fast-scan/round-002-B-test-only/archive \
  --worker build/sgtt-controls-release/byteforge_integer_worker \
  --output results/my-query-comparison --warmups 2 --repetitions 10
```

Tests: default ASan-worker and release-worker Python suites both passed **29 tests**;
release native CTest passed **5/5**. Ruff check/format and `pip check` passed.
New tests compare every query's ordered row IDs with a Python reference, covering
Unicode, repeats, blank values, CR, long rows, absent values, cross-row false
matches, empty datasets, disabled/insufficient/sufficient caches and closed sessions.
Fault-injection tests ensure native invalid IDs and row boundaries remain rejected.
Actual SGTT and query-engine integration remain untested/blocked.

One direct-script benchmark launch failed before creating output because the
existing installed package predates `byteforge.query`. Invoking the scripts as
modules (`python -m scripts...`) from the repository root uses the current source
and resolves that issue. Early Ruff formatting/test-closure findings were fixed.
No extra dependencies were installed, and no measurements were dropped as outliers.

## Measured query results

Two frozen literal queries: `contains("WARN")` returns 80 ordered row IDs;
`equals(first_original_line)` returns one. Each backend/session configuration has
two warmups and ten measured rounds. All **216 observation records** (including
warmups, first-request and repeated-query records) passed the independent Python
reference comparison; no measured trials failed.

First request includes handle opening, checksum checks, the initial decode and
filter. Repeated scans retain handles but decompress on each call. Cached requests
retain Arrow values and perform the filter again; they never reuse result sets.
Both backends use a 16 MiB maximum cache budget, actually **291,852 B** of retained
Arrow buffers for this sample. The budget excludes native/Python process overhead.

| Backend | First warning query, no value cache, ms | Repeated warning scan, no value cache, ms | Repeated warning query, cached, ms | Repeated equality query, cached, ms |
|---|---:|---:|---:|---:|
| Standard Parquet + Zstd | 1.618 | 0.730 | 0.367 | 0.042 |
| Fast-scan Parquet + Zstd | 1.267 | 0.599 | 0.368 | 0.042 |
| Diagnostic byte IDs + BtrBlocks | 24.081 | 6.814 | 0.369 | 0.042 |

Values are medians. Warning repeated-scan [min,max]: standard A
[0.658,0.840] ms; fast A [0.576,0.630] ms; diagnostic B [6.180,7.499] ms.
Cached diagnostic B warning query [0.364,0.377] ms; cached equality
[0.041,0.045] ms. Complete ranges for every track are in the raw summary.
The first request that also populates the cache is reported separately there.

For this workload, fast-scan A reduces repeated warning-scan median latency by
about 18% versus standard A. Diagnostic B's cache reduces warning latency about
18× relative to repeatedly decompressing with the same new decoder. The cache
benefit is a memory/initialization tradeoff, **not improved codec throughput**.
Cached A and B have essentially the same filter path and similar latencies;
B does not beat A on first touch or uncached scans. Prefer Parquet for current
interactive use unless an experiment specifically needs the diagnostic backend.

Measurements use one CPU and one-thread limits, seeded randomized session order,
32 requests per uncached batch and 16,384 per cached batch, with result consumption
and reference comparison inside each batch timer. Different modes have explicit
batch counts; counts are identical across backends within a mode. These are
exploratory API microbenchmarks on a small sample, not the research plan's final
held-out query-engine evaluation. Some fast uncached batches are shorter than
250 ms; they do not meet that final evaluation's batching protocol. All raw
observations are retained, including the first-request outliers. Imports are
preloaded, OS caches uncontrolled; CLI/Python launch latency is excluded here.
No disk-cold, SQL, full-corpus or end-to-end service latency claim is made.

Raw observations, build identity and environment/configuration are preserved in
[measurements/log-latency](measurements/log-latency/). Full generated archives
remain under `results/log-latency-fast-scan/`; the old archive used for the paired
restore test remains under `results/sgtt-hdfs-2k-release-controls/`. Neither prior
measurements nor the source data were overwritten. The next scale-oriented step
would be indexed block selection and a bounded partial-block cache; this first
implementation intentionally falls back to scans when the whole Arrow value
column exceeds the cache budget.
