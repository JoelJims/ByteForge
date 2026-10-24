# Full Thunderbird Query Benchmark

## Dataset and provenance

This run uses the complete raw `Thunderbird/Thunderbird_full.log` member from
LogHub-2.0 release v1 (Zenodo record 8275861), extracted from the official
`Thunderbird.zip`. The ZIP's published MD5 was verified. The raw log is 2,516,177,713
bytes with SHA-256 `a276905e7517a7d2c1d658490a0f27919c666045c3454ba695a5e77f79e9cbfe`.
ByteForge parsed LF-delimited UTF-8 without trimming or normalization: terminal LF
does not create an extra row; CR is retained; blank LF-delimited records remain
empty strings. It yielded 16,601,745 ordered rows and 2,499,575,968 UTF-8 payload
bytes. The canonical logical checksum is
`19eb21df372ea06090ac92496ed5bb6c727df789a2f9306378b807d619cd2066`.

The dataset is distinct from the HDFS and Spark corpora. The source repository
requests research/academic use, attribution, and citations to the LogHub and
LogHub-2.0 papers. See the [official repository](https://github.com/logpai/Loghub-2.0),
[Zenodo release](https://zenodo.org/records/8275861), and
[license notice](https://github.com/logpai/Loghub-2.0/blob/main/LICENSE). The full
manifest and dataset verification are in
[`measurements/query-aware-thunderbird-full/dataset_manifest.json`](measurements/query-aware-thunderbird-full/dataset_manifest.json).

## Compared archives

All pipelines consumed the same prepared canonical rows. A is genuine Parquet
2.6 with Zstandard level 3, dictionary encoding, 1 MiB pages, and 65,536-row
windows. Full B uses ByteForge's explicitly test-only reversible byte-symbol
encoder, int32 little-endian token IDs/offsets, BtrBlocks release worker, then
outer Zstandard level 1; tokenization/serialization windows were capped at 1 MiB.
Block B uses the same diagnostic encoder and worker, 256 rows per independently
decodable block, Zstandard level 1 frames and index, and a safe byte-trigram Bloom
filter. **Neither B result uses SGTT or measures an LLM tokenizer.**

| Pipeline | Complete archive | Payload / archive | Build time | Exact full reconstruction |
|---|---:|---:|---:|---|
| A: Parquet + Zstd | 164,417,323 B | 15.20× | 15.354 s | Yes, 219.527 s |
| Full B: byte IDs + BtrBlocks + Zstd | 631,875,507 B | 3.96× | 496.019 s | Yes, 33.548 s |
| Block B: byte IDs + block BtrBlocks | 818,064,093 B | 3.06× | 866.466 s | Yes, 47.110 s |

Archive size includes manifests, indexes, vocabulary and data. Block B has
64,851 blocks; its compressed index alone is 62,651,861 B. Full B's `data.zst`
is 626,368,695 B and index 5,501,075 B. Parquet's data file is 164,414,876 B.
Thus the current diagnostic B archives are 3.84× and 4.98× the Parquet size.
Full B restored faster in this one end-to-end run, but its build was much slower
and its archive larger; this is not evidence of SGTT performance.

## Query observations

The runner used 0 B and 16 MiB decoded-block cache budgets, disabled result
caching, one cold request and one repeated warm request for each case. Queries
are case-sensitive substring checks: lowercase `warning` (636,710 matching
rows), `1131523501` (one row, sampled from the first record), and an absent
sentinel (zero rows). Every result matched the streaming Python reference.

The table shows the **single observed run** in milliseconds, not statistically
meaningful p50/p95 estimates. Each cell is `cache off cold / warm; 16 MiB cold /
warm`.

| Query | Parquet + Zstd | Full B | Block B |
|---|---:|---:|---:|
| Common `warning` | 5997.95 / 5754.54; 6181.67 / 5834.73 | 23726.67 / 23075.63; 23678.14 / 22954.84 | 14731.55 / 13575.45; 15534.09 / 14579.00 |
| One-row timestamp | 6838.25 / 6733.32; 7068.38 / 6708.54 | 24266.01 / 24030.97; 24378.58 / 23876.22 | 1641.40 / 413.69; 1887.56 / 353.52 |
| Absent sentinel | 6206.36 / 6049.18; 6128.31 / 6016.43 | 23763.59 / 23497.23; 23855.04 / 23235.07 | 1312.54 / 117.91; 1397.73 / 116.53 |

Block B scanned 44,161 of 64,851 blocks for `warning` (11,305,105 rows),
so a common term still costs 13.6–15.5 s. It examined 525 blocks for the sampled
timestamp and pruned all 64,851 blocks for the absent term. Even when no data
blocks were read, cold query setup took about 1.3 s because the large index must
be opened and checked. The 16 MiB cache helped selective repeated Block B queries;
it could not retain the full Parquet or Full B column. These results show that
query selectivity and index-open cost dominate different cases. Parquet was much
faster for the common term, while Block B was faster for selective and absent
terms in this single run.

## Reproduction and limitations

The host was an Intel 12th Gen i3-1215U, Linux x86-64, Python 3.14.8,
PyArrow 26.0.0, and python-zstandard 0.25.0. The native BtrBlocks worker was an
unsanitized Release build (protocol 3, `fsst-safety-v1` enabled), pinned source
revision `c950c3c01c1fbf91007936b05781d431a29a60d3`; processes were limited to one
CPU and Arrow/OMP/BLAS threads to one. OS page-cache state was uncontrolled.

The test used one trial per query/configuration, no benchmark warmup, and a
single warm repeat. Treat values as screening data only; run at least ten trials
with controlled warmup before drawing performance conclusions. Build and restore
times above are individual end-to-end runs. No result here measures SGTT, and the
diagnostic byte encoder is inefficient for these strings.

Reproduce after downloading/extracting the official raw member and building the
Release worker:

```bash
PYTHONPATH=. .venv/bin/python - <<'PYCODE'
from pathlib import Path
from byteforge.dataset import prepare
from byteforge.pipelines import (
    TestByteEncoder, build_parquet, build_hybrid, build_hybrid_blocked
)

source = Path("data/loghub-2.0/Thunderbird_full.log")
prepared = Path("results/thunderbird-full/prepared")
worker = Path("build/btrblocks-bytefast-release/byteforge_integer_worker")
prepare(source, prepared, source_metadata={
    "kind": "loghub_2_0_full_raw",
    "provenance": {"dataset": "LogHub-2.0 Thunderbird", "release": "Zenodo 8275861 v1"},
    "artifact_identity": "full raw Thunderbird log member from verified official archive",
})
build_parquet(prepared, Path("results/thunderbird-full/parquet"))
build_hybrid(prepared, Path("results/thunderbird-full/full-b"), TestByteEncoder(), worker,
             allow_test_encoder_on_real=True, outer_codec="zstd",
             max_payload_per_window=1 << 20)
build_hybrid_blocked(prepared, Path("results/thunderbird-full/block-b"),
                     TestByteEncoder(), worker, allow_test_encoder_on_real=True,
                     block_rows=256)
PYCODE
PYTHONPATH=. .venv/bin/python scripts/benchmark_query_aware.py \
  --source data/loghub-2.0/Thunderbird_full.log \
  --archive parquet=results/thunderbird-full/parquet \
  --archive btr-full=results/thunderbird-full/full-b \
  --archive btr-block=results/thunderbird-full/block-b \
  --worker build/btrblocks-bytefast-release/byteforge_integer_worker \
  --output results/thunderbird-full/query-run --warmups 0 --repetitions 1 \
  --warm-repetitions 1 --session-warmups 0 \
  --query-ids common-warning sampled-timestamp absent-sentinel
```

The exact run record, summary, 36 raw observations (Zstandard-compressed with a
verified uncompressed checksum), build/restore component metrics and complete
status are preserved under
[`measurements/query-aware-thunderbird-full/`](measurements/query-aware-thunderbird-full/).
Downloaded data and generated archives remain local and are not committed.

Follow-up LZ4 and Block B granularity/Bloom-filter experiments on the same
Thunderbird source are documented in
[the codec and tuning report](query_aware_thunderbird_lz4_results.md).
