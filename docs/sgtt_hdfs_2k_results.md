# SGTT qualification and HDFS_2k release controls

## Status

**Actual SGTT integration is blocked by unestablished source and token-table
permissions.** No actual SGTT implementation, reference test, storage result or
performance result is claimed. The only token encoder executed was
`test-only-byte-symbols-v1`, an infrastructure diagnostic. It is not SGTT.

Work started at `e43b598`, on `implementation/core`, in
`/home/nishant/coding/ByteForge`, with canonical origin
`https://github.com/JoelJims/ByteForge.git`. The measured implementation revision
is `560c1cc`. Existing measurements, source dataset, untracked AGENTS.md and
research files were preserved. Independent review remains missing; the user's
explicit authorization permits the independent work reported here.

## Verified paper and artifact

[Accelerating String-Heavy Queries with LLM Token Tables](https://www.vldb.org/pvldb/vol19/p3635-schmidt.pdf),
Tobias Schmidt, Nicolas Schmitt, Thomas Neumann and Andreas Kipf,
PVLDB **19(11), 3635–3648 (2026)**,
[DOI 10.14778/3836663.3836714](https://doi.org/10.14778/3836663.3836714).
The published PDF's first page explicitly links
[umbra-db/token-vldb2026](https://github.com/umbra-db/token-vldb2026).
This report uses that published PDF, not an unverified arXiv identifier.
PDF SHA-256:
`4e3a31620e55daf775ce6b9dd719e2898eefe16304e80f3061a20c79c68329f5`.

Artifact HEAD and inspected revision:
`e202e36e2b33a4768d1122f96880b64416234175`.
Read-only source inspection covered README, CMake, dependency requirements,
`extract_tokens.py`/`.sh`, `src/tokenizer/{Tokenizer,Encoder,Decoder}`,
`OpenAI100kPatchedTokenizer`, `encoder/StandardEncoder`,
`src/compressor/SGTTCompressor.cpp`, `tools/sgtt.cpp`,
`test/TokenizerTest.cpp` and `benchmarks/stacked/sgtt_general.json`.
[Qualification evidence](measurements/sgtt-hdfs-2k/qualification.json) records the
actual inspected paths, hashes, pinned HTTP checks and scope of permissions.

### Method established from paper and source

SGTT means **Static Global Token Table**. It uses a fixed LLM-derived byte-piece
table shared across strings, columns and tables, permitting equality/hash
operations on consistently encoded strings and deferred decoding. It also
studies compression and stacking; it is not model inference. Ordering/range
semantics must not be inferred from numeric token IDs.

The paper's GPT-4-derived configuration uses the custom patched `cl100k` table:
`OpenAI100KPatched`, 65,536 entries, maximum token length seven bytes (`Token7`).
These are positions in the patched, length/byte-sorted table, **not original BPE
rank IDs**. The extraction code retains short original tokens, supplies all 256
single bytes, removes multi-byte NUL/control tokens, adds short letter/digit and
other common combinations, and fills the fixed capacity with further tokens.

The encoder greedily matches byte prefixes against its registered table, with
hash-table optimizations. Lossy hash lookup can lose compression opportunities,
not input bytes. Single-byte entries provide the intended arbitrary-byte fallback;
they are part of the actual table, not an invented replacement. Decoding joins
byte pieces before UTF-8 decoding. Normal model BPE encoding would produce a
different stream. No model checkpoint is needed once the fixed table is available.

Sparse global IDs suit ByteForge's fixed table experiment. Upstream also supports
dense remapping, which needs additional mapping assets. Defaults are configurable
and must not be assumed to select the paper's patched configuration. High-level
`Tokenizer` owns protected state, but public `Encoder::State`, `registerTokens`,
`encode` and `createEncoder` offer a possible authorized adapter boundary. Its
runtime compatibility is **not tested**. The upstream high-level path asserts
nonzero sizes; empty-row handling needs qualification. ByteForge already represents
empty rows with equal offsets, without invoking an encoder.

Upstream whole-file stacked configurations and Umbra field/query experiments are
different workloads. ByteForge must preserve each HDFS row boundary separately.
Source inspection suggests a reversible byte representation, but does not prove
runtime losslessness, arbitrary-input support or HDFS correctness for SGTT.

## Permission evidence and exact blocker

Checked again at the pinned revision; no affected source, tables or extraction
scripts were built, executed, copied into ByteForge, or redistributed.

| Material | Evidence and qualification |
|---|---|
| Paper | CC BY-NC-ND 4.0 notice; publication permission does not establish artifact source/table permission. |
| SGTT source | Root `LICENSE` and `COPYING` return HTTP 404. Tracked license inventory contains only dbtext and OnPair licenses. SGTT headers carry Tobias Schmidt copyright notices; no repository-wide permission grant identified. |
| Base vocabulary | Bundled `tokenizers/cl100k.tiktoken` identified; permission for this bundled asset has not been established. A tokenizer package's license cannot automatically be extended to every derived asset. |
| Patched table | `src/tokens/openai100kpatched/` generated headers and dictionary/length assets identified. Independent derivation, execution and redistribution permissions remain unestablished. No table assets bundled. |
| Model/checkpoint | None required or used for the fixed-table algorithm; this does not clear vocabulary rights. |
| Dependencies | `data/dbtext/LICENSE` is MIT (CWI); `thirdparty/onpair/LICENSE` is MIT (Gargiulo/Venturini). These scoped grants do not license SGTT. Full SGTT transitive audit remains incomplete. CMake requires at least 3.28 and C++23, with native compression dependencies. |

Absence of an identified grant is a qualification blocker under the user's explicit
rules, not a legal conclusion about all possible uses. No author was contacted.
Next steps require an applicable source-code grant and permission for the exact
base/derived tables, followed by the remaining dependency audit. No authorized
substitute or clean-room approximation is presented as SGTT.

## Independent implementation completed

- `byteforge/sgtt.py` exposes the pinned identity, unavailable status and explicit
  exception. `sgtt-status` reports it; `run --encoder sgtt` fails before creating
  output, even if `--test-encoder` is also supplied.
- The archive builder rejects unqualified production encoders. Readers reject
  unsupported encoder identities; the diagnostic table must match its complete
  known vocabulary, even if a modified table has updated file checksums.
- Dataset lengths, final-LF flag, hashes and unsigned bounds are checked before
  reconstruction. The complete native index is preflighted for row/token counts,
  contiguous ranges, truncation, integer/type bounds and supported schemes before
  opening a native decompressor. Existing token/offset checks still apply.
- SGTT+BtrBlocks (`B`) and SGTT+Zstd (`C-sgtt-zstd`) are separately retained as
  `unsupported`, `not_run`, with null size/time/correctness. No fallback is invoked.
- `scripts/summarize_controls.py` regenerates the saved phase summaries from raw
  observations. Original and opt-in `fsst-safety-v1` implementations are unchanged.

No production SGTT archive format is claimed. Diagnostic v1 archives contain
IDs/offsets, the full byte table, file hashes and native build identity. Decoding
requires the matching external BtrBlocks executable; assets are not silently
resolved from writer state. Actual SGTT will require explicit table hashes,
configuration, ID mode/width and authorized asset handling; absent/mismatched
assets must fail. Table storage/amortization costs remain unknown, not zero.
Checksums address accidental corruption; this remains a trusted-input native
format, not a general hostile-input decoder.

## Dataset and parsing

`data/hdfs/HDFS_2k.log` was unchanged and compared byte-for-byte again with the
[official pinned sample](https://raw.githubusercontent.com/logpai/loghub-2.0/ac4aad2ea86f561e5a0acbc6587fa28b487259f9/2k_dataset/HDFS/HDFS_2k.log).
SHA-256:
`6fe25449e79d75e35bb223ead9729fa02c00b7abb23e4e8ec0f3bb2addec6e3a`.
There are 2,000 logical rows, 285,848 physical bytes, 283,848 UTF-8 payload bytes,
2,000 LF bytes, no CR bytes and no blank records. Content is ASCII and valid UTF-8.

The contract is one ordered, non-null `value` string per LF-delimited record.
Only LF terminators are excluded; CR/content/whitespace/Unicode are preserved.
A final LF terminates the last row without creating an extra row. Blank records
become empty strings. Null is distinct and is not accepted as a raw-log value.
The final-LF flag permits exact physical reconstruction. No parsing, trimming,
normalization or deduplication occurred. Dataset notice/citations remain in every
archive manifest; see [dataset validation](measurements/sgtt-hdfs-2k/dataset-validation.json).
This small real sample is not the full HDFS corpus.

## Release control measurements

A separate `build/sgtt-controls-release` directory uses genuine BtrBlocks revision
`c950c3c01c1fbf91007936b05781d431a29a60d3`, `Release`, `-O3 -DNDEBUG`,
`BYTEFORGE_SANITIZE=OFF`, `BYTEFORGE_FSST_SAFETY_FIX=ON`. Worker reports sanitized=0;
`ldd` has no ASan runtime. The original sanitizer build remains intact.
[Build evidence](measurements/sgtt-hdfs-2k/release-build.json) records flags, compiler,
worker hash and dynamic linkage. Python 3.14.8, PyArrow/Arrow 26.0.0 and machine/OS,
CPU affinity, memory and dependency versions are in `run.json`.

Two warmups plus ten measured trials per control, alternating paired order after
a seeded shuffle, one-CPU affinity and one-thread limits. All **24/24** control
trials exactly reproduced both canonical rows and physical source bytes using
separate encode and restore processes; zero failures. Blocked configurations were
not measured and were not counted as successful trials.

| Pipeline | Complete archive | Encode median [min, max], ms | Reconstruction median [min, max], ms |
|---|---:|---:|---:|
| Parquet + Zstd level 3 | 64,278 B | 7.410 [6.211, 30.356] | 15.541 [13.423, 35.477] |
| **Diagnostic byte symbols** + BtrBlocks | 233,320 B | 106.476 [100.413, 146.688] | 83.485 [77.393, 95.273] |
| Actual SGTT + BtrBlocks | Not measured | Blocked | Blocked |
| Actual SGTT + Zstd | Not measured | Blocked | Blocked |

A comprises 60,508 B genuine Parquet and 3,770 B manifest. The diagnostic comprises
225,545 B native block storage (225,353 B codec data), 892 B index, 2,308 B byte
vocabulary and 4,575 B manifest. Manifest differences explain its changed total
relative to the old sanitizer archive; old measurements are unchanged. Raw
Zstd-only Parquet size is unavailable; column-chunk size includes page overhead.
No SGTT asset cost, compression ratio or improvement can be inferred.

Available diagnostic phase medians: tokenization **including local correctness
validation and offset construction** 57.096 ms; checked integer serialization
14.600 ms; compression IPC 15.206 ms; decompression IPC 3.253 ms; token decoding
and validation 57.059 ms. Parquet conversion/write/close is 5.813 ms. Combined
phases are not isolated native kernel timers; they do not sum to total time.
Initialization, archive I/O and integer construction are included in total paths
but not all separately instrumented. Actual SGTT initialization/decoding and
asset costs are null because no authorized implementation was run.

The operation timers exclude Python launch and some library imports. The measured
encode+restore **two-fresh-process** wall median [min,max] is 305.727
[228.735,430.838] ms for A, 430.257 [429.555,529.989] ms for the diagnostic.
This includes launch/import/setup and parent supervision, but excludes one-time
source preparation and independent equality comparisons. It is not a cold-disk
measurement or a single-process service request. OS caches are uncontrolled.
Warmups do not create a persistent initialized service; warm-service latency is
explicitly unmeasured. No SQL/query throughput is measured. RSS values are
process-specific diagnostics, not whole-tree peak memory.

In the paper artifact, tokenizer construction and `prepare`/output allocation occur
outside the timed codec sections in `tools/sgtt.cpp`; Umbra query timings concern
operators on compressed values. ByteForge's validation, archive I/O, reconstruction
and process overhead have different boundaries. Its numbers must not be compared
directly with the paper's codec GB/s or query speedups. No SGTT-specific profiling
or optimization was attempted before reference correctness and rights qualification.

## Reproduction and validation

Existing pinned environment is described in `docs/qualification/README.md`.
No additional packages were installed for this continuation. Build recipe:

```sh
.venv/bin/cmake -S probes/btrblocks -B build/sgtt-controls-release \
  -DBTRBLOCKS_SOURCE=/tmp/byteforge-btr -DCMAKE_BUILD_TYPE=Release \
  -DBYTEFORGE_SANITIZE=OFF -DBYTEFORGE_FSST_SAFETY_FIX=ON \
  -DFETCHCONTENT_SOURCE_DIR_BOOST_SRC=/home/nishant/coding/ByteForge/build/btrblocks/vendor/boost/boost_src-src
.venv/bin/cmake --build build/sgtt-controls-release \
  --target byteforge_integer_worker byteforge_btrblocks_probe --parallel 2
.venv/bin/ctest --test-dir build/sgtt-controls-release --output-on-failure
.venv/bin/python -m byteforge sgtt-status
.venv/bin/python -m byteforge run --path data/hdfs/HDFS_2k.log \
  --output results/sgtt-hdfs-2k-release-controls --encoder test-byte \
  --worker build/sgtt-controls-release/byteforge_integer_worker \
  --warmups 2 --repetitions 10
```

The Boost override reuses the existing pinned source cache; omit that override
on a fresh machine to use the project's configured dependency acquisition. The
BtrBlocks source must be the pinned clean checkout. Output directories must not
already exist; choose a new output name for another experiment.

```sh
.venv/bin/python -m unittest discover -s tests -v
BYTEFORGE_BTR_WORKER=build/sgtt-controls-release/byteforge_integer_worker \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check byteforge tests scripts
.venv/bin/ruff format --check byteforge tests scripts
.venv/bin/python -m pip check
.venv/bin/python scripts/summarize_controls.py \
  docs/measurements/sgtt-hdfs-2k/observations.jsonl \
  --output /tmp/reproduced-sgtt-controls.json
```

Both final Python suites passed **26 tests**; release native CTest passed **5/5**.
Ruff checks/format checks and dependency check passed. The regenerated phase
summary was compared byte-for-byte with the committed one. Tests exercise Unicode,
whitespace, empty/repeated/long strings, nullable representation, integer overflow,
invalid IDs/offsets, corrupt integrity metadata, missing/mismatched table, unknown
versions, fresh-process decoding without input, and explicit SGTT rejection.
The tests named `SGTTQualificationTests` test blocking, not the SGTT algorithm.
Actual reference cases, unknown-token behavior, SGTT reconstruction of HDFS,
SGTT warm/cold performance and SGTT archive portability remain **not run**.

[Raw observations and configuration](measurements/sgtt-hdfs-2k/) retain all trials,
blocked records, hardware/software metadata, phase distributions, representative
manifests and per-trial native leaf directories. Full trial archives and logs stay
locally in `results/sgtt-hdfs-2k-release-controls/`, outside Git. No dataset, native
binary, model or protected table is committed. Nothing was pushed or merged.
