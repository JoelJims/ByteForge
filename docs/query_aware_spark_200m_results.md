# Query-aware storage on a 200 MiB Spark sample

## Dataset

The source archive is LogHub-2.0 `Spark.zip`, Zenodo record 8275861, v1. The
download is 230,699,188 bytes and matches Zenodo's published MD5
`ca0ef9c6e8ece0df602793bd302e03a6`. Its raw member `Spark/Spark_full.log` is
1,629,352,119 uncompressed bytes. The sample used here is the first complete
LF-terminated records not exceeding 200 MiB: 2,035,741 rows and 209,715,181
bytes. It is a prefix sample, not a published split or the complete Spark
dataset. Strict UTF-8 ingestion passed. Sample SHA-256:
`61330feaedcd2ad3021816605401a1e272659c2bb2ad1a2851b98310b3635ea3`.

LogHub-2.0's notice limits dataset use to research/academic use and asks users to
refer to the repository and cite the LogHub and LogHub-2.0 papers. The dataset
and raw sample remain ignored under `data/`; neither is committed. Sources:
[official LogHub-2.0 repository](https://github.com/logpai/Loghub-2.0),
[Zenodo record and file checksums](https://zenodo.org/records/8275861),
[dataset license notice](https://github.com/logpai/Loghub-2.0/blob/main/LICENSE).

## Configurations and correctness

Compared Parquet + Zstandard (A), the existing full-column BtrBlocks archive,
and query-aware block BtrBlocks (up to 256 rows per block). Both B variants use
the test-only byte-symbol encoder, not SGTT. The Release BtrBlocks worker used
protocol 3, sanitizers off, and one CPU. The three literal queries were common
`WARN`, a rare exact Spark `blk_...` identifier, and an absent sentinel. Two
warmup rounds and three measured rounds were used because these scans operate on
200 MiB of text; each session also performed two untimed warm filters followed
by one timed warm filter. All 270 observations passed the Python reference.

All three formats were restored in a fresh process and their canonical output
SHA-256 matched `5a27af138a1b2d5a2cb248acc4657cd7bdc385293696cd5e11803701fa834fdd`
(2,035,741 rows, 207,679,440 payload bytes). Single observed restore times were
1.602 s for A, 2.837 s for full B and 3.149 s for block B; these are correctness
checks, not repeated performance estimates.

| Format | Complete archive size | Encoding/build time |
|---|---:|---:|
| Parquet + Zstd | 14,016,582 B | 1.413 s |
| Full B + outer Zstd | 33,430,273 B | 46.299 s |
| Block B | 52,780,861 B | 71.127 s |

Block B includes 45,637,011 B block frames, 7,136,395 B compressed index,
2,308 B vocabulary and 5,147 B manifest. Full B includes a 33,002,540 B outer
compressed stream, 420,420 B index, 2,308 B vocabulary and 5,005 B manifest.
The diagnostic B archive sizes are 2.38x and 3.76x Parquet respectively.

## Query latency

“Cold” means the first query in a newly opened `QuerySession`, with the decoded
value cache disabled. It includes archive opening and first filtering; it does
**not** mean a cold disk cache. Filesystem cache state was uncontrolled. p50 is
the median of three independent session trials; p95 is omitted because three
trials do not support a useful tail-latency estimate. Times below are milliseconds.

| Query, cold first request | Parquet | Full B | Block B |
|---|---:|---:|---:|
| `contains("WARN")` | 463.0 | 1,773.4 | **202.7** |
| Rare block ID | 502.0 | 1,860.3 | **140.5** |
| Absent sentinel | 500.3 | 1,754.9 | **129.2** |

| Query, repeated in an open session, cache disabled | Parquet | Full B | Block B |
|---|---:|---:|---:|
| `contains("WARN")` | 455.4 | 1,877.6 | **69.7** |
| Rare block ID | 489.6 | 1,767.8 | **13.2** |
| Absent sentinel | 478.3 | 1,758.2 | **12.8** |

The block index reduced the `WARN` scan to 292 of 7,953 blocks (74,752 rows and
8.69 MB of decoded log payload, about 4.2% of input payload). The rare identifier
needed one block (256 rows, 26 KB); the absent sentinel pruned all blocks and
read no compressed block data. Parquet still read and evaluated its column for
each literal filter. The 200 MiB case therefore shows a query-selectivity
benefit that the 2k sample could not: at 2k, `WARN` survived in 5/8 blocks and
Block B's cold first request was slower than Parquet.

Block B's cold open took about 114 ms, mostly validating and decoding its
7.1 MB index. A long-lived session amortizes this; it is a real first-query
cost. Common terms still require decoding candidate blocks. The test-only B
representation also stores one integer ID per input byte, which explains much
of its larger archive and slower full-column reconstruction. These results do
not predict actual SGTT performance.

## Reproduction and raw observations

The complete verified archive and prefix manifest are in the ignored `data/`
directory on the machine used for the run. `run.json`, `summary.json`,
`complete.json`, and compressed JSONL observations are preserved under
[`measurements/query-aware-spark-200m/`](measurements/query-aware-spark-200m/).
The observation manifest gives the uncompressed JSONL SHA-256.

The measured query command was:

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_query_aware.py \
  --source data/loghub-2.0/Spark_200MiB.log \
  --archive parquet=results/spark-200m/parquet \
  --archive btr-full=results/spark-200m/full-b \
  --archive btr-block=results/spark-200m/block-b \
  --worker build/btrblocks-bytefast-release/byteforge_integer_worker \
  --output results/spark-200m/query-run --warmups 2 --repetitions 3 \
  --warm-repetitions 1 \
  --query-ids common-warning rare-block-id absent-sentinel
```

This sample run is a scale check, not a final benchmark: it uses a derived
prefix, three measured trials, uncontrolled filesystem cache, and a diagnostic
encoder. The original data file was not changed; no dataset or archive was
committed.
