# Exact word-index queries on Thunderbird

## Measured result

On the full 2.516 GB Thunderbird log, the optional index makes covered ID/count
queries much faster than a Parquet+LZ4 scan. It does **not** establish a BtrBlocks
or SGTT advantage: indexed Parquet achieves similar latency with less storage.

Two warmups and ten measured trials per configuration/query, randomized order,
one CPU, cache disabled: **288 observations, zero correctness failures**. Cold
is a fresh session, not cold disk. All values below are milliseconds; p95 is the
inclusive sample percentile of ten trials, not a population guarantee.

| Query / metric | Parquet LZ4 | Block B 256 | Block B 4096 + word index | Parquet LZ4 + same index |
|---|---:|---:|---:|---:|
| Cold `warning` p50 / p95 | 5389 / 5976 | 14518 / 15912 | **381 / 517** | 362 / 550 |
| Warm `warning` p50 / p95 | 5034 / 5588 | 12808 / 16883 | **148 / 158** | 151 / 166 |
| Cold `failure` p50 / p95 | 5565 / 6566 | 1955 / 2344 | **135 / 377** | 140 / 402 |
| Warm `failure` p50 / p95 | 5433 / 6535 | 680 / 904 | **2.63 / 7.93** | 2.48 / 9.81 |
| Cold absent word p50 / p95 | 5280 / 5816 | 1286 / 1565 | **136 / 374** | 146 / 366 |
| Total archive + index, decimal MB | **299.019** | 818.064 | 840.099 | 483.420 |

The common word matches 636,710 rows; `failure` matches 2,374; the absent term is
`ByteForgeAbsentSentinel`. Indexed B's cold medians are 14.2x and 41.3x faster than
unindexed LZ4 for the two present terms. Warm common-word latency is 34.1x lower.
These gains are workload-specific. Indexed-Parquet and indexed-B timing ranges
overlap; neither has an established latency advantage over the other.

All index-only queries decode **zero log payload blocks**. `warning` reads
542,224 compressed posting bytes and reconstructs 1,273,420 posting bytes;
`failure` reads 4,778 compressed posting bytes. Cold opening additionally reads
and SHA-256-verifies the **184,397,824-byte SQLite file**; this I/O and its time
are included. No zero-I/O claim is made. Warm searches reread/decompress the
needed postings; they do not use a cached result list.

The sidecar totals **184,400,115 bytes**, including its manifest. Build took
108.364 seconds wall / 102.060 seconds process CPU in one unpinned single-thread
run, including canonical-file validation; this is not a repeated ingestion result.
It contains 40,122 runs in the dictionary, 278,398 posting pages, and 228,895,575
row-word memberships. Writer memory includes a bounded row window and a global
word dictionary; response memory scales with returned row IDs.

Coarser B payload blocks largely pay for the new index: 840.099 MB is 2.7% more
than the existing 818.064 MB B256 archive, but still **2.81x unindexed LZ4** and
1.74x indexed LZ4. Original payloads were reused; this task does not claim faster
full reconstruction or measure a new B ingestion time. Prior single-run B4096
and LZ4 builds were 703.445 / 13.826 seconds, respectively, before adding the
same 108.364-second sidecar build. B is not a storage/ingestion winner.

Cold `warning` process CPU medians are 5313 ms for plain LZ4 and 318 ms for indexed
B; indexed LZ4 is 297 ms. These three paths require no native child CPU. Using
the indexed-LZ4 CPU difference, its extra 102.060-second index-build CPU cost
amortizes after roughly **21 `warning` queries** on an unchanged dataset. This
excludes storage charges and initial archive creation; it is not a monetary claim
or a B-versus-LZ4 total-cost break-even.

Raw observations/configuration are under
[`measurements/word-index-thunderbird/`](measurements/word-index-thunderbird/).
Regenerate all storage/timing rows with:

```sh
PYTHONPATH=. .venv/bin/python -m scripts.summarize_word_queries \
  docs/measurements/word-index-thunderbird
```

## Noncovered-query control

An additional screen ran two trials, no warmups, with the decoded cache disabled
and exact source-reference checks: **16 observations, zero failures**. These are
two-trial medians, not stable tail-latency estimates:

| Query | LZ4 cold / warm, ms | B4096 + index cold / warm, ms | Result |
|---|---:|---:|---|
| Numeric `1131523501` | 6310 / 6003 | 12514 / 12172 | One matching row; B is slower |
| Punctuation absent sentinel | 5592 / 5356 | 575 / 69 | No matches; existing Bloom pruning helps B |

The word index is not used for either predicate, but its verification cost is
paid when the indexed session opens. Numeric fallback decodes 2,718 B blocks
and scans 11,132,928 rows. Users with many ID, timestamp or punctuation queries
should not select this coarse-block profile based on its word-search numbers.
Full log export, high-output-cardinality searches and arbitrary substring
queries have not become universally faster. The fallback timing table and raw
observations are in `measurements/word-index-thunderbird/fallback/`.

## Dataset, implementation and profile

Source: unchanged official `data/loghub-2.0/Thunderbird_full.log`, 16,601,745 rows,
2,516,177,713 physical bytes and 2,499,575,968 logical UTF-8 bytes. Source SHA-256:
`a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`;
canonical SHA-256: `19eb21df372ea06090ac92496ed5bb6c727df789a2f9306378b807d619cd2066`.
Parsing, final-LF handling and LogHub attribution/license metadata are preserved
in `index_manifest.json`. The runner recomputed the source checksum and used an
independent Python reference scan, not old query answers.

Query code revision: `8f489a0`. Host is Intel i3-1215U, Linux x86-64,
Python 3.14.8, PyArrow 26.0.0, python-zstandard 0.25.0, SQLite 3.54.0.
Existing B payloads use Release BtrBlocks `c950c3c`, protocol 3,
`fsst-safety-v1`, no sanitizers. Exact environment, executable hash and archive
component sizes are in `run.json`. Index-only answers never start that worker.

One fresh-process resource probe on `warning` returned the same result digest
for Parquet LZ4 and indexed B. End-to-end including interpreter, validation,
query and result digest took 5.53 s / 589 MB peak RSS for LZ4 and 0.51 s / 145 MB
for indexed B. This probe measures process wall time, unlike the in-process
query matrix, and includes a separate digest step. It is one observation with
uncontrolled OS cache. See `fresh-process-resources.json` for exact scope and
commands embedded in the probe.

The before-profile on the B256 sampled-timestamp query showed 3.048 seconds in
session construction, including 1.962 seconds in block-directory validation;
the query itself took 1.212 seconds, with 0.576 seconds in Bloom checks and
0.419 seconds in candidate-block array construction. These are **instrumented
cProfile diagnostics**, not comparable uninstrumented timing measurements.
The profile identified 129,702 filter decodes and 939,077 `_positions` generator
calls. The new postings path bypasses that directory for covered queries; regular
block queries reuse prepared trigram positions. The old directory format remains
compatible and retains its validation cost on fallback queries.

## Scope and implementation

This experiment adds an **optional auxiliary index**, not a new tokenizer or
storage codec. B remains `test-only-byte-symbols-v1` plus real BtrBlocks and
per-block Zstd. Actual SGTT remains permission-blocked. The index also works on
Parquet: any resulting advantage must be attributed to indexing, not SGTT or
BtrBlocks. Existing archives and measurements remain unchanged.

`byteforge.word_index` indexes every maximal `[A-Za-z]+` run, preserving case.
For a nonempty ASCII-letter-only `contains` query, a matching substring must be
inside one such run. Searching the complete word dictionary and unioning its
row postings therefore returns exactly the matching ordered row IDs, including
substrings inside words, with no false negatives or false positives. Duplicate
occurrences in a row contribute one ID. This is not a word-boundary query and
does not normalize the logs. Unicode letters, digits, punctuation, empty terms,
`equals`, and `prefix` use the existing exact payload path. Row ranges apply to
both paths. Nulls remain outside the raw-log contract.

Postings use bounded 65,536-row windows, delta-uint16 little-endian offsets and
independent Zstd level-1 frames in SQLite. The dictionary and all postings are
included in index size. SQLite is a directory/query index; it does not replace
the BtrBlocks log payload. The reader fully hashes the index at session opening,
verifies dataset identity and file sizes, checks mutation between calls, and
bounds frame lengths and decoded row IDs. Payload verification is deferred for
index-only answers; fallback reopens the fully validating archive reader.
Checksums address accidental corruption, not malicious archive producers.

No decoded values, postings, or query results are retained between requests.
SQLite has a bounded 2 MiB page cache; OS page cache is uncontrolled. Result-ID
materialization still scales with match count and is included in timing. The
query returns IDs/count, not reconstructed matching log strings. Index creation
reads the checksum-verified canonical input; it needs no SGTT assets.

Separately, regular Block B now prepares query trigram hash positions once per
Bloom width instead of hashing the same needle separately for each block.
Bloom decisions and archive formats are unchanged.

## Reproduction

Run from the repository root; use new result directories for every repetition.

```sh
PYTHONPATH=. .venv/bin/python -m byteforge build-word-index \
  --prepared results/thunderbird-full/prepared \
  --output results/word-index-thunderbird/index
PYTHONPATH=. .venv/bin/python -m byteforge query \
  results/thunderbird-full/block-b-4096-widebloom \
  --word-index results/word-index-thunderbird/index \
  --operation contains --value warning
```

The index is a separate required bundle for this accelerated path. Count its
complete bytes alongside each archive; sharing one local index across benchmark
variants does not remove its cost from any variant. Ordinary archive restore is
unchanged and does not require this optional index.

The repeated query comparison uses the existing complete Thunderbird archives:

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ARROW_NUM_THREADS=1 \
PYTHONPATH=. .venv/bin/python -m scripts.benchmark_query_aware \
  --source data/loghub-2.0/Thunderbird_full.log \
  --archive parquet-lz4=results/thunderbird-full/parquet-lz4-dict \
  --archive block-b-256=results/thunderbird-full/block-b \
  --archive block-b-4096-word=results/thunderbird-full/block-b-4096-widebloom \
  --archive parquet-lz4-word=results/thunderbird-full/parquet-lz4-dict \
  --word-index block-b-4096-word=results/word-index-thunderbird/index \
  --word-index parquet-lz4-word=results/word-index-thunderbird/index \
  --queries-json docs/measurements/word-index-thunderbird/queries.json \
  --cache-budgets 0 \
  --worker build/btrblocks-bytefast-release/byteforge_integer_worker \
  --output results/word-index-thunderbird/queries \
  --warmups 2 --repetitions 10 --warm-repetitions 1 --session-warmups 0
```

The runner pins one CPU and Arrow thread. “Cold” includes a new session, metadata
and index verification and the first request, within an already running Python
process. It does **not** mean cold physical disk or include interpreter launch.
Warm means one subsequent request in that session. The decoded-value cache is
disabled; SQLite's bounded page cache and the OS cache still exist. Results are
compared against a separate streaming Python scan of all 16,601,745 source rows.
Timing includes result-ID creation, but excludes the oracle comparison and digest.
The old harness included the oracle comparison; previous results are not edited.

Process CPU time includes the entire indexed query (no native child is needed)
and the Parquet path. It excludes native worker CPU for ordinary B, so that
counter must not be described as complete B CPU cost. Parent peak memory is not
isolated in this harness. No dollar-cost or cold-disk claims are supported.

## Research connection and specialization

The [SGTT paper](https://www.vldb.org/pvldb/vol19/p3635-schmidt.pdf) motivates
delaying string reconstruction and operating on stable representations, especially
for equality, joins and grouping. This auxiliary word index follows the broader
principle of avoiding reconstruction, but does not implement that paper's token
table or establish its benefits. Searching a tokenized needle directly in token
IDs is not a generally correct arbitrary-substring algorithm. The
[BtrBlocks paper](https://www.cs.cit.tum.de/fileadmin/w00cfj/dis/papers/btrblocks.pdf)
targets efficient column compression/decompression; it does not imply that our
byte-as-int diagnostic is an efficient representation of logs.

Useful candidate workloads are repeated incident searches for severity/component
words, failure counts by row/time slice, and searches for rare exception-name
fragments. ASCII-only coverage and the ID-only API apply. Trace IDs, timestamps,
arbitrary Unicode/punctuation searches and full log export still need other paths.
Future paper-aligned work needs authorized SGTT assets, then canonical token-key
equality/grouping and late reconstruction, with the same operators on a conventional
compression ablation. No SGTT performance claim is made here.
