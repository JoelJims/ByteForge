# Thunderbird LZ4 and Block B Tuning Results

## Summary

Added a Parquet `lz4` profile and compared it with the existing Parquet Zstandard
baseline on the complete 16,601,745-row Thunderbird raw log. Both profiles use
the same Parquet 2.6 schema, dictionary encoding, 1 MiB pages and row windows;
the only material writer change is the codec (Zstd level 3 versus LZ4, which has
no configured level). PyArrow documents that its Parquet LZ4 codec uses the
Parquet `LZ4_RAW` representation ([PyArrow Parquet docs](https://arrow.apache.org/docs/python/parquet/parquet.html)).

The same run compared existing 256-row Block B with 4,096-row blocks. The larger
block reduced archive/index costs and full reconstruction time, but harmed
present-term pruning. Raising the Bloom filter cap to 65,536 bits fixed absent
term pruning but did not make a sampled timestamp selective: the constituent
trigrams occur across many rows independently. Therefore 256 rows remains the
default query-oriented setting; 4,096 is an explicit storage/index tradeoff.

All B results still use `test-only-byte-symbols-v1`, not SGTT.

## Dataset and environment

The unchanged input was the verified official `Thunderbird/Thunderbird_full.log`
member from LogHub-2.0, Zenodo release 8275861 v1: 2,516,177,713 source bytes,
SHA-256 `a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`,
16,601,745 rows, 2,499,575,968 logical UTF-8 bytes. The canonical checksum is
`19eb21df372ea06090ac92496ed5bb6c727df789a2f9306378b807d619cd2066`.
Provenance and source-license notes are in
[`dataset_manifest.json`](measurements/query-aware-thunderbird-lz4/dataset_manifest.json).

Host: Intel 12th Gen i3-1215U, Linux x86-64, Python 3.14.8, PyArrow 26.0.0,
python-zstandard 0.25.0. The BtrBlocks worker was Release, protocol 3, sanitizers
off, `fsst-safety-v1` on, revision `c950c3c01c1fbf91007936b05781d431a29a60d3`.
Runs used one CPU and one Arrow/OMP/BLAS thread. OS page cache was uncontrolled.

## Storage, ingestion, and full reconstruction

Archive sizes include data, metadata, index, vocabulary and manifest. These
build/restore values are individual end-to-end observations, not repeated timing
statistics. The 256-row values are from the prior full Thunderbird run; the
4,096-row variants and LZ4 Parquet were built in this follow-up.

| Configuration | Full archive | Index | Build | Full restore | Exact canonical checksum |
|---|---:|---:|---:|---:|---|
| Parquet + Zstd level 3 | 164,417,323 B | — | 15.354 s | 13.857 s | Yes |
| Parquet + LZ4 | 299,019,419 B | — | 13.826 s | 13.547 s | Yes |
| Block B, 256 rows, 8,192-bit cap | 818,064,093 B | 62,651,861 B | 866.466 s | 47.110 s | Yes |
| Block B, 4,096 rows, 8,192-bit cap | 640,649,882 B | 2,091,251 B | 718.325 s | 32.211 s | Yes |
| Block B, 4,096 rows, 65,536-bit cap | 655,699,338 B | 17,143,110 B | 703.445 s | 33.525 s | Yes |

The tuned wide-filter Block B is 19.8% smaller than the original 256-row Block B,
and its index is 72.6% smaller. It remains 3.99× the size of Parquet Zstd. Its
build took 45.8× as long as Parquet Zstd and full reconstruction 2.42× as long.
The byte-symbol representation and BtrBlocks integer archive are still the main
cost; changing the row block size does not make this diagnostic encoder
competitive for full-column storage.

The 4,096-row / 8,192-bit trial was smaller than the wide-filter variant, but its
sampled timestamp query examined 3,466 blocks. Expanding the filter cap to
65,536 bits increased its index by 15.1 MB and reduced that query to 2,718
blocks, which was still not sufficiently selective. Bloom filters are safe
(false positives only); larger blocks contain more independently occurring
trigrams and can pass a substring query even when no one row contains the full
term.

## Query latency

The final matched query run completed with 48 observations and zero correctness
failures. It used three case-sensitive `contains` terms: lowercase `warning`
(636,710 matching rows), a sampled timestamp `1131523501` (one matching row),
and an absent sentinel (zero matches). Each configuration had one cold request
and one repeated same-session request at 0 B and 16 MiB decoded-value cache
budgets. No benchmark warmups were used. Values are single observations in ms,
not stable p50/p95 estimates. Cold includes session open; OS cache was not
cleared. The warm request is not a disk-cold measurement.

### Cold request, cache disabled

| Query | Parquet Zstd | Parquet LZ4 | Block B 256 | Block B 4,096, wide Bloom |
|---|---:|---:|---:|---:|
| Common `warning` | 5,319 | 4,998 | 14,077 | 16,274 |
| Sampled timestamp | 6,180 | 5,968 | 1,617 | 12,264 |
| Absent sentinel | 5,519 | 5,110 | 1,246 | 232 |

### Repeated request, cache disabled

| Query | Parquet Zstd | Parquet LZ4 | Block B 256 | Block B 4,096, wide Bloom |
|---|---:|---:|---:|---:|
| Common `warning` | 5,208 | 4,731 | 12,574 | 16,237 |
| Sampled timestamp | 6,044 | 5,591 | 410 | 11,928 |
| Absent sentinel | 5,406 | 4,902 | 112 | 7 |

With 16 MiB caching, results were close to the cache-disabled cases for Parquet
and common terms because a full column or all matching blocks do not fit in the
budget. Selective Block B repeats benefited: 256-row timestamp/absent queries
were 319/118 ms; wide-filter 4,096-row absent queries were 15 ms. Result caching
was disabled.

Parquet scans all 16.6 million rows for these arbitrary substring queries. The
256-row block filter scanned 11.31 million rows for common `warning`, 134,400
for the sampled timestamp, and none for the absent term. The 4,096-row wide
filter scanned 15.41 million, 11.13 million, and zero rows respectively. This
shows why the coarse block layout helps absent terms but is a poor default for
present terms.

LZ4 Parquet was about 6–10% faster than Zstd in these one-run query observations,
and about 2% faster in one full restore, while using 82% more archive space.
These small differences need repeated controlled trials before treating them
as general codec behavior.

## Implementation and reproducibility

`build_parquet(..., profile="lz4")` and CLI `--parquet-profile lz4` select
Parquet LZ4 while retaining dictionary encoding and all other standard writer
settings. Block B supports 1–4,096 rows; the default remains 256. The Bloom
filter grows at eight bits per distinct trigram and may now use up to 65,536
bits. Older v2 archives with smaller filters remain readable.

```bash
.venv/bin/python -m unittest tests.test_pipelines.ParquetArchiveTests \
  tests.test_query.QueryTests tests.test_query_blocks.TrigramFilterTests -v
.venv/bin/ruff check byteforge scripts tests
```

The exact machine-readable run, query summary, 48 raw observations (Zstandard
compressed with a verified uncompressed checksum), full-dataset manifest and
pipeline metrics are under
[`measurements/query-aware-thunderbird-lz4/`](measurements/query-aware-thunderbird-lz4/).
The raw downloaded dataset and generated archives remain in ignored local
`data/` and `results/` directories, not Git.

A planned three-trial matrix that reopened the large block index for every
session was interrupted because its repeated warmup path took impractical time;
those partial observations are excluded. The complete reported run is one trial
per query/configuration and should be repeated for publication-quality estimates.
