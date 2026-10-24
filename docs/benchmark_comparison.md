# ByteForge benchmark comparison

This summary compares the existing raw-query, compressed-storage, and indexed
query measurements. Results from different datasets or timing protocols are
kept separate. All B results use `test-only-byte-symbols-v1`; none measure SGTT.

## Full Thunderbird raw-query scans

Source: official 2.516 GB LogHub-2.0 Thunderbird log, 16,601,745 rows,
SHA-256 `a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`.
The direct scanner, Parquet LZ4, and Block B query `warning`, `failure`, and an
absent sentinel. Native raw scan uses 10 trials after two warmups; other
compressed numbers are from the ten-trial word-index experiment. “Cold” means a
new query session/process, not uncached physical storage; OS cache was not
controlled. Request boundaries differ slightly, so treat cross-path ratios as
directional. Native timing includes subprocess startup and returned ID bytes,
but not Python parsing of those bytes; query-session timing runs inside the
already-running benchmark process.

| Query | Python raw scan p50 | Native mmap raw p50 / p95 | Parquet LZ4 scan p50 / p95 | Block B 256 p50 / p95 | Block B 4096 + word index p50 / p95 | Parquet LZ4 + same index p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| `contains("warning")` | 8,560 ms | **1,442 / 2,024 ms** | 5,389 / 5,976 ms | 14,518 / 15,912 ms | 381 / 517 ms | **362 / 550 ms** |
| `contains("failure")` | 8,682 ms | **1,437 / 1,894 ms** | 5,565 / 6,566 ms | 1,955 / 2,344 ms | **135 / 377 ms** | 140 / 402 ms |
| Absent sentinel | 8,278 ms | **1,451 / 1,591 ms** | 5,280 / 5,816 ms | 1,286 / 1,565 ms | 136 / 374 ms | **146 / 366 ms** |

The AVX2 raw scanner is about 5.7–6.0× faster than the Python line scanner and
roughly 3.7× faster than an unindexed Parquet LZ4 scan for these terms. It reads
the entire 2.5 GB file each time. Adding the exact word-postings index cuts
covered queries further: indexed Parquet and indexed B have overlapping latency
ranges, and neither shows a reliable speed advantage over the other. Queries
outside the index's ASCII-letter coverage fall back to scanning.

| Thunderbird storage profile | Complete bytes | Build / index cost |
|---|---:|---:|
| Raw source, no index | 2,516,177,713 | no archive build |
| Parquet + Zstd | 164,417,323 | 15.354 s |
| Parquet + LZ4 | 299,019,419 | 13.826 s |
| Block B, 256 rows | 818,064,093 | 866.466 s |
| Block B, 4,096 rows, wide Bloom filter | 655,699,338 | 703.445 s |
| Parquet LZ4 + word index | 483,419,534 | index adds 184,400,115 B; one index build took 108.364 s |
| Block B 4,096 + same word index | 840,099,453 | same index build; B base archive is much larger |

The word-index totals include the sidecar and manifest. Indexed Parquet is about
1.62× its unindexed LZ4 archive; indexed B is about 1.28× its wide-Bloom archive
and 1.74× indexed Parquet. For a common `warning` query, the index costs
additional build time and storage but reduces median latency from seconds to
about 0.36–0.38 s. See
[`word_index_query_results.md`](word_index_query_results.md) for methodology,
index coverage and controls.

## Other datasets (not pooled with Thunderbird)

| Dataset / query | Parquet | Block B | Result |
|---|---:|---:|---|
| HDFS 2k, cold `WARN` | 1.337 ms | 17.300 ms | Parquet faster; B skips only 3 of 8 blocks |
| HDFS 2k, cold absent term | 1.404 ms | **0.581 ms** | B safely skips all 8 blocks |
| HDFS 2k, warm cached `WARN` | 0.370 ms | **0.323 ms** | Similar; cache retains decoded values |
| Spark 200 MiB, cold `WARN` | 463.0 ms | **202.7 ms** | B scans 292 of 7,953 blocks |
| Spark 200 MiB, cold rare ID | 502.0 ms | **140.5 ms** | B scans one block |
| Spark 200 MiB, cold absent term | 500.3 ms | **129.2 ms** | B skips all blocks |

The query-selective block design helps when metadata can rule out most blocks;
it loses on small/common-term workloads and can have substantial index-open
cost. The Spark result is a 200 MiB prefix sample with three measured rounds;
the HDFS result has only eight blocks. Neither dataset result should be read as
a Thunderbird measurement. For full dataset sizes, query variants, and raw
observations, see [HDFS results](query_aware_storage_results.md) and
[Spark results](query_aware_spark_200m_results.md).

## Takeaway

For one-off searches over raw Thunderbird, the native sequential scan is a
practical improvement and avoids archive creation. For repeated supported word
queries, the word index is faster, with indexed Parquet offering similar query
latency to indexed B at substantially lower total storage. Block B's advantage
is selective pruning on some workloads, not overall compression or universal
query speed. The hybrid's real SGTT path remains unavailable; byte-ID results
are diagnostic only.

Per-trial raw records and full parameter sets are preserved in
[`measurements/raw-thunderbird-native/`](measurements/raw-thunderbird-native/),
[`measurements/word-index-thunderbird/`](measurements/word-index-thunderbird/),
[`measurements/query-aware-hdfs-2k/`](measurements/query-aware-hdfs-2k/), and
[`measurements/query-aware-spark-200m/`](measurements/query-aware-spark-200m/).
