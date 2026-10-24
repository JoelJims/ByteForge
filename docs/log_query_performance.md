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

### Selective block-query follow-up

The newer query-aware archive adds independent 256-row decoding and a safe
byte-trigram Bloom filter. Full v2 layout, dataset hash, complete archive costs,
query semantics and the cold/warm comparison against Parquet and v1 full-column
B are documented in [query-aware storage results](query_aware_storage_results.md).
The v2 measurement is still the test-only byte encoder, not SGTT. For `WARN`,
the block filter skips 3/8 blocks but the cold request is 17.300 ms versus
Parquet's 1.337 ms. An absent long sentinel is pruned from all 8 blocks and
returns in 0.581 ms cold; repeated cached `WARN` is 0.323 ms. V2 costs 114,957 B,
14,144 B more than full-column B and 1.79x Parquet. This tradeoff improves
selective query work for prunable terms, not compression ratio or every query.

The earlier query runner defaulted to zero retained-value cache. The current
Python `QuerySession` retains a bounded 16 MiB block cache by default; CLI
single-shot queries still do not gain cross-request reuse. Set `cache_bytes=0`
to measure uncached behavior.

On a separately downloaded 200 MiB complete-line prefix of Spark's raw LogHub-2.0
log, Block B beat Parquet on the tested cold common, rare, and absent terms by
pruning 96.3%, 99.99%, and 100% of 7,953 blocks respectively. Cold `WARN` was
202.7 ms for Block B versus 463.0 ms for Parquet; its 7.1 MB index made session
opening about 114 ms. See [Spark 200 MiB results](query_aware_spark_200m_results.md).
This differs from HDFS_2k, where only 8 blocks existed and `WARN` matched 5,
leaving too much work to make a cold B query faster. Here “cold” is first query
in a new process/session, not cold physical storage; OS cache state was not
controlled. Both B runs use test-only bytes, not SGTT.

### Earlier full-column result

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

## Follow-up: native decode and outer Zstandard — 2026-10-24

The next profile showed that B's ID decode was crossing the native/Python boundary
as one Python integer per byte. A release-only protocol-3 operation now validates
the BtrBlocks integers and returns compact bytes directly. Protocol-2 readers and
existing archive variants remain supported. B's archive can also wrap the native
payload in explicit Zstandard level 1; this is now the benchmark runner default,
and `--b-outer-codec none` retains an unwrapped comparison. This adds a small
streaming decompression cost but substantially reduces the diagnostic archive.

The restore writer previously encoded each reconstructed row twice: once for the
logical checksum and again for canonical serialization. It now encodes once and
feeds the same bytes to both. `QuerySession` now retains a decoded Arrow column up
to 16 MiB by default; the CLI's one-shot `query` still defaults to no cache because
that process cannot reuse the cache. Set `cache_bytes=0` to measure each BtrBlocks
decompression, or choose a different explicit cap. Oversized datasets stream and
skip retaining the full column. This cache stores decoded values, not query
results, and gives both A and B the same limit.

### Latest HDFS_2k diagnostic run

The protocol-3 unsanitized Release worker was built in
`build/btrblocks-bytefast-release`. On the unchanged 2,000-row sample, 10 measured
trials plus two warmups per backend all reconstructed the canonical and physical
input exactly; zero trials failed. The production SGTT path remains blocked on
source/table usage rights. B below is explicitly test-only byte IDs.

| Metric | Parquet + Zstd | Test byte IDs + BtrBlocks + Zstd-1 |
|---|---:|---:|
| Complete archive | 64,278 B | 100,811–100,813 B |
| Encode median | 6.801 ms | 67.579 ms |
| Restore/reconstruct median | 14.259 ms | 20.228 ms |
| Native integer decode IPC phase | n/a | 2.092 ms |
| Exact trials | 10/10 | 10/10 |

Without outer Zstd, B is 233,320 B. The outer frame reduces that by 56.8% to
about 100,812 B, but B remains 1.57x the complete Parquet archive. BtrBlocks
selects DICT for the IDs and BP for row offsets. The diagnostic encoder creates
one 32-bit integer per original input byte, then BtrBlocks' encoded integer
stream is 225,545 B before the outer frame. Parquet compresses the original
strings directly. The remaining size gap therefore comes mainly from this
inefficient test representation plus ID/offset/table/archive metadata; it is not
evidence about SGTT. The outer Zstd pass took 0.818 ms in a representative final
trial.

Query runner: two warmups and ten measured trials for each query mode; query row
IDs matched the Python reference on every observation. On this sample, the first
WARN filter was 1.361 ms for Parquet and 18.087 ms for B; this includes the first
column read/decode. Repeated uncached scans were 0.713 vs 2.534 ms for `contains`
and 0.425 vs 2.203 ms for equality. With the same 16 MiB value cache, repeated
`contains` was 0.366 ms for Parquet and 0.372 ms for B, and equality was 0.048 ms
for Parquet and 0.049 ms for B. Thus repeated query latency is fixed by keeping
the decoded column resident; the first B query still pays archive decode. The cache is a
memory/latency tradeoff, not a faster decompression result.

Raw round-trip and query observations are in
[measurements/btrblocks-latency-hdfs-2k](measurements/btrblocks-latency-hdfs-2k/).
Earlier measurements remain unchanged. These small-sample diagnostics are not a
final query-engine benchmark and do not demonstrate SGTT or a general performance
win over Parquet.

### Full Thunderbird scale check

The full 16.6-million-row LogHub-2.0 Thunderbird log confirms that block pruning
is workload-selective. Block B was faster for a one-row timestamp and an absent
term, but slower than Parquet for a common term matching 636,710 rows. Opening
and checking its 62.7 MB index costs about 1.3 s even when every data block is
pruned. This is one screening trial per case; see the [full results and query
tables](query_aware_thunderbird_full_results.md) and raw observations under
`measurements/query-aware-thunderbird-full/`. The B encoder remains test-only.
