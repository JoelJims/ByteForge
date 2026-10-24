# Raw HDFS benchmark

The implemented experiment has two paths: **A**, genuine PyArrow Parquet+Zstd;
and **B**, row token IDs/offsets compressed by the actual pinned BtrBlocks integer
backend. Production B is blocked by SGTT code/table reuse rights. An explicit
**B-test-only** path exercises the complete archive infrastructure using reversible
byte symbols. Its results do not validate an LLM encoder. The
[real HDFS_2k validation](hdfs_2k_results.md) uses the actual 2k sample and keeps
that real-input identity separate from its test-only tokenizer identity.

## Exact dataset identity and access

Use [LogHub-2.0](https://github.com/logpai/loghub-2.0) and its linked
[Zenodo release 8275861, v1](https://zenodo.org/records/8275861), published
2023-08-23. The artifact is **HDFS.zip** (321,944,794 bytes, published MD5
`f23880dd4938379a535ab71a8d27a798`). Its raw member is **HDFS/HDFS_full.log**,
1,565,682,217 uncompressed bytes according to the ZIP directory. The other members
are structured/template CSVs and are excluded. The README reports 11,167,740
annotated HDFS logs; the complete raw line count has not been locally verified.
Do not substitute another repository's HDFS-v1/v2/v3 artifact or the 2k sample.

The [2k HDFS directory](https://github.com/logpai/loghub-2.0/tree/main/2k_dataset/HDFS)
is documentation/sample material, not this benchmark's primary input. During
implementation only official metadata, the 2k sample, and two 64 KiB byte ranges
of HDFS.zip were inspected. The latter established the ZIP member and a partial
raw-text sample; the full artifact was **not** downloaded or inspected.
[datasets/hdfs-loghub-2.0.json](../datasets/hdfs-loghub-2.0.json) records that snapshot.

Download HDFS.zip manually from the release, verify its published checksum,
and extract the raw member to the default location:
`data/loghub-2.0/HDFS/HDFS_full.log`. Keep the source license notice with downloaded
or redistributed data. Zenodo metadata declares CC-BY-4.0; the repository's
[license](https://github.com/logpai/loghub-2.0/blob/main/LICENSE) specifies research/
academic use, attribution, retention of its notice, and citations to Jiang et al.
(ISSTA 2024) and Zhu et al. (ISSRE 2023). Both statements and the full notice are
retained in real-input manifests; no broader permission is inferred.

```bash
.venv/bin/python -m byteforge check-dataset
.venv/bin/python -m byteforge check-dataset --path /absolute/path/HDFS_full.log
```

`BYTEFORGE_HDFS_PATH` overrides the default; explicit `--path` takes precedence.
Missing input produces an actionable error and nonzero exit. Nothing downloads
data during installation, tests or ordinary execution. CSVs and compressed
archives are rejected as loader inputs. A supplied file remains a provenance
candidate: source SHA-256 and comparison with published dimensions are recorded,
but matching dimensions alone cannot authenticate its origin.

## Lossless data contract

Each LF byte terminates one ordered, non-null UTF-8 `value`. Remove **only** that
delimiter. Retain CR, BOM, NUL, spaces, case, Unicode normalization forms and all
other bytes. A terminal LF does not manufacture an extra row; consecutive LFs
produce empty strings. Empty file = zero rows; `LF` = one empty row; `a LF LF` =
`["a", ""]`. CRLF input retains a CR at the end of each logical value. Unicode
line separators are ordinary content. Null/missing values have no representation
in this primary contract; the literal text `null` remains a string.

The representative official bytes use LF and ASCII-compatible text. UTF-8 for
the full file is an ingestion requirement, not a claim of full-file inspection.
Invalid UTF-8 rejects the dataset with physical row/byte location; there is no
replacement, skipped record or inferred timestamp/severity/component field.
An otherwise unusual HDFS message is preserved because no message parser runs.

Preparation streams a canonical `rows.bin`: ASCII `BFROWS01`, uint64 little-endian
row count, then repeated uint64 byte length + UTF-8 value. Its `dataset.json`
records source and canonical sizes/checksums, a logical length-framed checksum,
payload bytes, row count, final-LF state, contract and provenance. Preparation
failures retain `failure.json` and partial output, with no success manifest.
Physical reconstruction joins records with LF and restores the final-LF state;
its source byte count and checksum must match. Windows have at most 65,536 rows
and 64 MiB payload. Oversized rows fail explicitly; no truncation is permitted.

## Install and build

Run from the checkout on Linux x86-64, Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[benchmark,dev,native]' --index-url https://pypi.org/simple
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check byteforge tests probes/btrblocks/fsst_sample_patch.py
.venv/bin/ruff format --check byteforge tests probes/btrblocks/fsst_sample_patch.py
```

PyArrow 26.0.0 implements A. Unit tests need no real dataset or network; optional
Arrow/native tests skip when those dependencies are unavailable. Core loader and
representation tests use the standard library. To build the integer worker, use
the clean pinned BtrBlocks checkout described in the [qualification guide](qualification/README.md):

```bash
.venv/bin/cmake -S probes/btrblocks -B build/btrblocks \
  -DBTRBLOCKS_SOURCE=/absolute/path/to/pinned/btrblocks -DCMAKE_BUILD_TYPE=Release \
  -DBYTEFORGE_FSST_SAFETY_FIX=ON -DBYTEFORGE_SANITIZE=ON
.venv/bin/cmake --build build/btrblocks --target byteforge_integer_worker --parallel 2
```

Set `BYTEFORGE_BTR_WORKER` or pass `--worker` if its executable is elsewhere.
The existing FSST safety corrections and instrumentation are identified in build
metadata. The integer backend itself is not substituted. ASan/LeakSanitizer builds
are for qualification; use a separate build with `BYTEFORGE_SANITIZE=OFF` for
future performance work and report its different executable hash. Upstream
FastPFOR uses a moving Google Test dependency during builds; the environment is
not a fully locked publication build yet.

## Run and reconstruct

The six-row fixture is authored synthetic data, including a blank row, repeated
message shapes, changing identifiers/numbers, Unicode and a long diagnostic.
Additional generated tests cover much longer values and block boundaries.

```bash
.venv/bin/python -m byteforge run --path tests/fixtures/hdfs-synthetic.log \
  --synthetic --test-encoder --output results/my-synthetic-run \
  --warmups 2 --repetitions 10
```

Output directories must be new. Each trial gets an independent archive and worker
logs. The runner compares canonical files **byte for byte**, then compares the
physically reconstructed log to the source **byte for byte**. On success only its
own temporary decoded files are removed; archives and measurements remain.
Warmup observations and failed trials are retained, with no numeric zero standing
in for missing data. Missing BtrBlocks produces unavailable trials, never another
codec. `--test-encoder` explicitly opts into infrastructure validation on either
synthetic or real input; it does not change the input's provenance or activate
SGTT. Direct library callers retain a default guard for real input and must opt
in with `allow_test_encoder_on_real=True`.

When the real member is available, the same loader and A implementation run with:

```bash
.venv/bin/python -m byteforge run --path data/loghub-2.0/HDFS/HDFS_full.log \
  --output results/my-hdfs-run --warmups 2 --repetitions 10
```

This currently runs **A only**, plus an explicit production-B blocked record.
It cannot establish the primary A/B research comparison. `build_hybrid` accepts
the existing tokenizer protocol for future authorized integration; no SGTT code
or table is loaded, copied, executed or silently replaced by the CLI.

```bash
.venv/bin/python -m byteforge restore results/my-synthetic-run/round-000-A/archive \
  --raw --output build/reconstructed.log
```

For B-test-only supply its archive path and the matching native worker. Decoding
loads only archived table/offset/block assets and ordinary decoder software; no
original input or network is needed. Without `--raw`, restore writes canonical
framing. Archives refuse overwrite, version/build mismatches, missing assets,
checksum failures, malformed directories and invalid decoded token offsets/IDs.

## Archive and measurement scope

A contains genuine `data.parquet` and `manifest.json`; its only field is non-null
STRING `value`. Explicit settings: Parquet 2.6, page v1, Zstd level 3, dictionary
enabled, 1 MiB target data/dictionary pages, statistics enabled, one row group per
logical window. Schema and Arrow versions are stored. Linked Arrow Zstd version
is not exposed by the inspected Python API and remains unavailable.

B contains `data.bin`, `index.json`, `vocabulary.bin` and `manifest.json`. Per-row
tokens are flattened without cross-row tokenization. uint32 ID and N+1 offset
streams are range-checked for nonnegative int32 and independently divided into
65,536-element native INTEGER blocks, including partial leaves. An empty row has
equal adjacent offsets. The UTF-8 JSON directory stores uint64-compatible byte
positions, lengths, counts and schemes. The table stores uint32 entry count and
uint64 length-prefixed byte entries. The decoder concatenates token bytes before
UTF-8 decoding. All table, index, native headers/dictionaries and wrapper bytes
are counted once in complete archive storage. Generic runtime deployment is
outside archive totals for both methods; native ABI/build identity is checked.

`run.json`, `observations.jsonl`, `summary.json`, trial logs and `complete.json`
preserve actual measurements/configurations. Ratios use logical UTF-8 payload
bytes divided by complete archive bytes; zero-payload ratios are null. Parquet
file bytes and column-chunk bytes are distinct; raw Zstd-only bytes are unavailable
because chunk sizes include page headers. B's codec count is native encoded-column
bytes including nested cascade metadata, excluding outer block headers/null maps.

Source parsing/canonical preparation is timed once, outside paired archive trials.
Both methods consume the same canonical file. Encode totals include canonical
checksum/read, initialization, conversion/tokenization, compression, serialization
and archive close. Phase timers identify inseparable work explicitly. Restore
totals include archive validation, read/decode, detokenization where applicable,
owned-row materialization, canonical write, physical export and checksum checks.
Independent exact-file comparison occurs outside performance timers. Nested
phase times do not necessarily sum to the independent total.

Workers are fresh processes with one-CPU affinity and Arrow/OMP/BLAS thread limits.
Fresh-process wall time includes launch/setup separately from operation timers.
Process CPU and child CPU are recorded. RSS high-water values are process-specific
diagnostics (child values may include inherited state), not a summed whole-tree
peak. Whole-tree peak is null with a reason. OS cache is uncontrolled; this is
not a cold-disk benchmark. Trial order alternates after a seeded initial shuffle;
upstream native sampling still uses random-device entropy, with selected schemes
retained in each index. Very short fixture timings are diagnostics, not scientific
performance evidence. No SQL/query-engine or codec-only microbenchmark is claimed.

The native boundary caps requests, validates headers, uses a 60-second response
deadline, disables core dumps and limits CPU to 3,600 seconds. Release workers
also cap address space at 2 GiB; ASan workers need unrestricted shadow address
space. Runner timeout terminates the worker process group. Checksums detect
accidental corruption, not maliciously re-signed native bodies; native archives
remain a trusted-input format. Large real runs retain all trial archives and
need a declared disk/time budget before execution.

## Full Thunderbird scale run

The largest raw LogHub-2.0 artifact inspected in this run was the 2.516 GB
`Thunderbird_full.log`. Full A, full B, and block B archive sizes, end-to-end
reconstruction times, query observations, provenance, and limitations are in
[the full Thunderbird report](query_aware_thunderbird_full_results.md). Both B
variants use test-only byte symbols, not SGTT. The query screening run has one
trial per case and is not a stable p50/p95 evaluation.

## Actual SGTT qualification and release controls

See [SGTT/HDFS_2k report](sgtt_hdfs_2k_results.md) and the new independent
`docs/measurements/sgtt-hdfs-2k/` records. The published paper explicitly identifies
`umbra-db/token-vldb2026`; revision
`e202e36e2b33a4768d1122f96880b64416234175` still lacks an established grant covering
its source and patched tables. No SGTT code/table was executed or bundled.

```sh
.venv/bin/python -m byteforge sgtt-status
# Fails explicitly; never substitutes test byte symbols:
.venv/bin/python -m byteforge run --path data/hdfs/HDFS_2k.log \
  --output results/new-actual-sgtt-run --encoder sgtt
```

`--encoder test-byte` explicitly selects the existing diagnostic, equivalently to
`--test-encoder`. It never makes a production SGTT result. The saved release
control run used `build/sgtt-controls-release/byteforge_integer_worker` with
sanitizers OFF and `fsst-safety-v1` ON, two warmups and ten measured repetitions.
The original sanitizer executable and old measurements remain unchanged. Both
actual SGTT+BtrBlocks and SGTT+Zstd are recorded as unsupported with null metrics.
The report includes the separate build recipe and storage/timing breakdown.

Archive readers now preflight complete index bounds and dataset metadata and
verify the diagnostic vocabulary identity before native decompression. Checksums
are not authenticity guarantees; trusted native archives are still required.
Fresh-process timings include separate startup/import/setup measurements. Warmups
are not a persistent warm-service benchmark; that metric remains unavailable.

## Log-query latency and optional fast-scan profile

[Log-query performance](log_query_performance.md) documents the optimized diagnostic
decoder, literal query CLI/API and fresh measurements. `--parquet-profile fast-scan`
selects Zstd level 1 without dictionary encoding; the standard profile is unchanged.

```sh
.venv/bin/python -m byteforge query \
  results/log-latency-fast-scan/round-002-A/archive \
  --operation contains --value WARN
```

Use `byteforge.query.QuerySession` for retained handles and repeated queries;
its Python API defaults to a bounded 16 MiB decoded-value/block cache (set
`cache_bytes=0` to disable it). CLI single-shot queries do not reuse that cache.
Cache-backed queries are not decompression benchmarks. Immutable session
archives, explicit profile/cache configuration and ordered-result reference
checks are required. No actual SGTT, SQL engine or large-corpus performance
claim follows from these measurements.

## Query-aware HDFS_2k archive

The `build-query-archive` command creates versioned v2 archives with independent
256-row BtrBlocks blocks and a compressed trigram Bloom index. Queries skip
blocks only when the index proves a substring cannot occur; short terms and
potential false positives are scanned. The existing v1 full-column format
remains supported. This implementation uses the explicitly identified test-only
byte encoder and is not SGTT. See the [query-aware results](query_aware_storage_results.md)
for complete archive size, checksum, exactness, query timings, raw records and
reproduction commands. On HDFS_2k, absent long-term searches benefit sharply;
cold common-term searches remain slower than Parquet, while the block index adds
storage overhead versus full-column B.

A separate 200 MiB ordered prefix of the official Spark raw log was used as a
larger scale check; it is clearly distinguished from the complete Spark
dataset. See [Spark sample results](query_aware_spark_200m_results.md) and
machine-readable observations under
`docs/measurements/query-aware-spark-200m/`. The test-only block path pruned
most blocks for selected Spark queries and beat the Parquet query scan on this
sample, with substantial extra archive storage. The Spark archive and derived
prefix remain ignored local data and are not committed.
