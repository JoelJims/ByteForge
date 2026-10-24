# Query-aware HDFS storage results

## Scope and dataset

This is an exploratory literal-query experiment on the official LogHub-2.0
`HDFS_2k.log` sample, not the full HDFS corpus. The source remains unchanged:
2,000 LF-delimited UTF-8 strings, 285,848 physical bytes, 283,848 logical
payload bytes, SHA-256
`6fe25449e79d75e35bb223ead9729fa02c00b7abb23e4e8ec0f3bb2addec6e3a`.
Rows retain order and exact content. `contains` is case-sensitive UTF-8 byte
substring matching within each row; `equals` is exact equality; `prefix` is a
literal prefix; row ranges are half-open. Empty `contains` and `prefix` match
all selected rows; empty `equals` uses recorded empty-row positions. Result IDs
are original zero-based row positions, in order.

The B tokenizer remains `test-only-byte-symbols-v1`, a reversible byte-symbol
fixture. SGTT is not implemented or measured, and none of the B results below
are SGTT results.

## Selective archive and query path

Added archive v2, `byteforge-hdfs-block-archive-v2`. Each block holds at most
256 consecutive rows, a block-local BtrBlocks integer payload in an independent
Zstandard frame, row offsets, empty-row positions, checksums, and a byte-trigram
Bloom filter. `index.json.zst` contains the block directory and filters;
`vocabulary.bin` is the explicit test encoder table. The reader checks archive
and index identity/lengths at open and verifies the checksum of every candidate
frame before decoding it. V1 remains readable.

The Bloom filter stores every byte trigram occurring in each block's strings.
A query trigram absent from the filter proves no string in that block can contain
the query. False positives only cause extra scans. Terms shorter than three
bytes are never Bloom-pruned. Prefix and equality use the same safe necessary
substring condition; empty equality uses exact empty-row metadata. Uncertain
cases scan. Candidate blocks are decoded independently into zero-copy Arrow
binary arrays; the full column is not reconstructed for a query.

Each `QuerySession` has a byte-bounded LRU of decoded block buffers (default
16 MiB in the Python API, configurable to zero). Cache counters report hits,
misses, and evictions; results themselves are never cached. Archive-file
signatures invalidate the cache if files change during a session. Cold requests
include session opening and, for a B block that survives pruning, native worker
startup. OS filesystem cache state was uncontrolled.

## Measurements

Final run used the unsanitized Release BtrBlocks worker (protocol 3), one CPU,
two warmup rounds, ten measured rounds, seeded shuffled ordering, and eleven
queries: common terms, rare block ID, absent term, punctuation, case mismatch,
Unicode, empty term, equality, prefix, and a row range. Every result was checked
against a Python reference. There were 0 failures in 10,296 JSONL observations.
Cold p50/p95 include opening a fresh session and one request. Warm p50/p95 are
per-request after two warmups in each session. Cache-disabled and 16 MiB cache
are reported separately.

| Archive | Complete size | Components |
|---|---:|---|
| Parquet + Zstd | 64,278 B | Parquet 60,508 B; manifest 3,770 B |
| Full-column BtrBlocks + outer Zstd, test-only bytes | 100,813 B | data 92,868 B; index 892 B; vocabulary 2,308 B; manifest 4,745 B |
| Block archive v2, test-only bytes | 114,957 B | blocks 101,690 B; compressed index 6,068 B; vocabulary 2,308 B; manifest 4,891 B |

All B sizes include required index, vocabulary, and manifest. Block layout adds
14,144 B (14.0%) versus the full-column B control and is 1.79x Parquet size.
Selective decoding improves work avoided for prunable queries; this version
does not improve compression ratio.

| Query/track | Parquet p50 / p95 | Full B p50 / p95 | Block B p50 / p95 |
|---|---:|---:|---:|
| Cold `contains("WARN")`, cache off | 1.337 / 1.883 ms | 17.064 / 20.616 ms | 17.300 / 21.496 ms |
| Cold absent sentinel, cache off | 1.404 / 1.489 ms | 17.994 / 19.079 ms | 0.581 / 0.666 ms |
| Warm `WARN`, cache off | 0.713 / 0.764 ms | 2.423 / 3.429 ms | 1.377 / 1.830 ms |
| Warm `WARN`, 16 MiB cache | 0.370 / 0.401 ms | 0.376 / 0.415 ms | 0.323 / 0.355 ms |
| Warm rare block ID, block B cache off | — | — | 2.450 / 2.861 ms |
| Warm absent sentinel, block B cache off | — | — | 0.037 / 0.041 ms |

For cold `WARN`, block B examined and decoded 5/8 blocks: 1,280 rows, 63,038
compressed frame bytes read, 141,705 native bytes decompressed, and 716,548
integer bytes decoded. The absent sentinel pruned 8/8 blocks and read/decompressed
zero payload bytes. Rare block IDs can defeat trigram pruning and force a full
block scan. Warm cached performance reflects retained decoded data and is not a
cold decode result.

The block path does not beat Parquet for a cold common-term query: worker startup
and native decode dominate. It does beat both tested paths for this absent query
because metadata rules out every block. Reusing a `QuerySession` avoids startup;
decoded-block caching brings repeated common queries close to Parquet. These are
sample-specific literal-query results, not SQL/analytical engine measurements or
evidence that BtrBlocks or SGTT is generally faster.

## Reproduction and validation

Build a v2 archive with explicit test-only encoder selection:

```sh
.venv/bin/python -m byteforge build-query-archive \
  --path data/hdfs/HDFS_2k.log --output results/my-block-archive \
  --worker build/btrblocks-bytefast-release/byteforge_integer_worker \
  --test-encoder --block-rows 256
```

Run the fixed query suite against existing Parquet, full-column B, and block B
archives (output directory must be new):

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_query_aware.py \
  --source data/hdfs/HDFS_2k.log \
  --archive parquet=results/my-run/round-000-A/archive \
  --archive btr-full=results/my-run/round-000-B-test-only/archive \
  --archive btr-block=results/my-block-archive \
  --worker build/btrblocks-bytefast-release/byteforge_integer_worker \
  --output results/my-query-run --warmups 2 --repetitions 10
```

The full suite ran 37 Python tests successfully with the Release worker. Ruff
check and format check, compileall, `pip check`, Release build, and native CTest
are recorded in the implementation log. Exact full-sample block reconstruction
passed. Raw observations, run configuration, and summary are in
[`measurements/query-aware-hdfs-2k/`](measurements/query-aware-hdfs-2k/);
observations are Zstandard-compressed JSONL, with the uncompressed SHA-256 in
`raw_data_manifest.json`.

## Limitations

The dataset has only eight 256-row blocks, and this query set is exploratory.
Bloom filters use a fixed small per-block budget and may have false positives.
Cold common queries still pay for native worker startup. The size and timing
results apply to test-only byte IDs and this worker configuration. SGTT remains
blocked by unresolved implementation/table rights. No push or merge was made.
