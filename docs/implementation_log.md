# Implementation qualification log

## Scope and authorization — 2026-10-24

Implementation started from `2d85616` on `implementation/core` in
`/home/nishant/coding/ByteForge`, with verified canonical origin
`https://github.com/JoelJims/ByteForge.git`. Fetch succeeded; initial local/remote
main histories matched. The user's explicit authorization permits implementation
before independent review. Review remains missing and the research is not claimed
to be reconciled. Existing untracked AGENTS.md and research documents remain
untouched and unstaged.

Read AGENTS.md and all four existing research documents. No existing source,
tests, dependency manifest, datasets or benchmark outputs preceded this work.
Implementation findings live here rather than altering research-owned documents.

## Stage 1: representation contract

`byteforge/representation.py` implements checked little-endian uint16/uint32
streams, the nonnegative signed-int32 constraint for B, token offsets, optional
validity and 65,536-element leaf partitioning. `byteforge/tokenizers.py` defines a
replaceable encoder interface and verifies exact reconstruction. Test-only byte
symbols are **not an LLM tokenizer or an alternative B/D experiment**.

Tests cover empty datasets/strings, repeated strings, explicit nulls, NUL,
emoji/CJK/Arabic, distinct NFC/NFD, line breaks, invalid UTF-8 surrogates,
integer overflow/truncation, invalid offsets/IDs/validity, multi-block rows and
65,535/65,536/65,537-element boundaries. This is an in-memory representation,
not yet a standalone archive or production decoder.

## Stage 2: dependency interfaces

Initial environment: Linux x86-64, Python 3.14.8, GCC 16.2.1, GNU Make; no
CMake, Arrow, Python Zstd or Ruff. Installed only into `.venv`: CMake 3.31.10,
PyArrow 26.0.0, zstandard 0.25.0, Ruff 0.15.7. CMake 3.x avoids the removal of
legacy policies needed by older upstream dependencies. No system installation.
Python wheel downloads stalled/resumed before succeeding; a separate diagnostic
curl download timed out. Neither affects the passing installed-library tests.

`byteforge/qualification.py` writes exclusive-create JSON containing actual
versions, revision, dirty status and probe outcomes; it exits nonzero for failed
or unavailable Python dependencies. No timers or benchmark rankings are emitted.
`docs/qualification/python.json` preserves the first successful Python observation;
`python-final.json` records the probe at code commit `383de1c`.

| Interface | Qualification evidence / limitation |
|---|---|
| A: Parquet+Zstd | Explicit Parquet 2.6, page v1, dictionary enabled, 1 MiB pages/dictionary limit, 65,536-row groups, Zstd=3. Empty/null/Unicode/NUL round trips pass. Column chunks report ZSTD. This is a file-level probe, not a complete A archive. |
| D's secondary codec | Python Zstd links 1.5.7; empty and uint16/uint32 frames round-trip at level 3. No tokenizer or complete D archive is claimed. |
| Arrow codec version | Arrow C++ 26.0.0; linked Zstd version is unavailable through the inspected Python codec API. Version equivalence with Python Zstd is not established. |
| SGTT | Pinned `e202e36e2b33a4768d1122f96880b64416234175`; CMake >=3.28, C++23; public encode/compress/decode, protected ID/table state. No root LICENSE/COPYING in inventory; pinned URLs return 404. API license request returns 403, which establishes no rights. No SGTT code/table copied, built, redistributed or executed. |
| BtrBlocks | Pinned `c950c3c01c1fbf91007936b05781d431a29a60d3`; core C++17, upstream CMake >=3.12. Source INTEGER=int32, STRING dispatch and upstream string tests inspected. Native execution described below. |

Native evidence inspected: Units.hpp, Column.hpp/.cpp, MMapVector.hpp,
Relation.hpp/.cpp, Chunk.hpp, StringArrayViewer.hpp, Datablock.hpp/.cpp,
SchemeType.hpp, string Fsst/DynamicDictionary/OneValue sources, V2.cpp tests,
compression example, top-level/core build files and codec dependency CMake files.

## Native build findings

The top-level BtrBlocks project assumes `CMAKE_SOURCE_DIR` owns its tree, so a
direct add_subdirectory integration failed. The probe's small CMake harness
compiles the unchanged core with upstream-pinned FastPFOR, FSST, CRoaring and
Boost, preserving native adaptive schemes. It omits unrelated S3/tooling targets;
it does not replace compression algorithms. The upstream dependency projects can
still build their own test/tool targets. Build concurrency was limited to two.

The first core dependency build failed because pinned FSST lacks `<cstdint>` on
GCC 16. The harness adds `-include cstdint` through a separate configure step,
without vendor source edits. Subsequent compilation succeeds. Upstream warnings
remain visible, including CRoaring realloc diagnostics; success does not establish
memory safety.

Native STRING ingestion needs an indexed mmap-backed pool (`Vector<str>` is a
specialization, not an assignable string-view vector). The probe constructs that
layout, lets Relation produce the native int32-offset Chunk, retains its backing
memory and compares owned decoded strings. This adapter's allocation/copy costs
must be measured in future end-to-end runs.

Release probe: integer IDs at 1/65,535/65,536 elements pass; one empty string,
all-empty strings, mixed Unicode/NUL strings pass. A 100,000-byte row plus empty
and short rows aborts with `malloc(): unaligned tcache chunk detected`.
`docs/qualification/native-release.xml` preserves all five outcomes. The pristine
C path is **not qualified** for the complete required contract.

Core-only ASan initially passed (that diagnostic used `detect_leaks=0`), but
instrumenting FSST exposed a heap-buffer write in `makeSample`. The sample-length
allocation assumes 512-byte chunks although short rows contribute fewer bytes;
the number of selected chunks can exceed that allocation. Enlarging the array
to the target byte count is safe because chosen chunks are always nonempty.
That correction exposed an eight-byte read beyond the scalar compressor's
512-byte stack buffer. Adding zero-initialized padding removed that defect.
LeakSanitizer then exposed an unreleased encoder in BtrBlocks' FSST adapter.
The final opt-in `btrblocks-c950c3c-fsst-safety-v1` variant adds all three fixes,
including an RAII encoder guard in a generated private adapter translation unit.
The BtrBlocks checkout is unchanged; only build-directory FSST source is edited.
Sampling/codec selection is preserved, with new memory allocation/cleanup costs
to include in future measurements. All original/intermediate failures remain in
the raw XML files. Full sanitizer checks retain default leak detection.
The final corrected suite passes **5/5** in `native-safety-asan.xml`.
`native-safety-sources.json` records its source differences and artifact hashes.
This establishes only these fixtures; it is not complete backend qualification.

## Licensing and reproducibility boundaries

Inspected license files: BtrBlocks/FSST MIT; FastPFOR/CRoaring Apache-2.0; Boost
BSL-1.0. This is an inventory, not exhaustive clearance of every bundled source.
No third-party source trees, binaries or tokenizer assets are vendored. Future binary redistribution
requires notices and complete transitive license review. FastPFOR fetches Google
Test from moving `main`; record its resolved revision, and freeze that dependency
before reproducible benchmark builds. Native snapshot/source manifests are
separate from Python package versions.

SGTT reuse rights remain unresolved. B/D stay unavailable as complete approaches;
test symbols do not fill that gap. A separately named licensed encoder experiment
requires an explicit scientific-scope decision. No author contact was sent.

## Checkpoint validation

- `python3 -m unittest discover -s tests -v`: initial core tests passed;
  optional dependency tests initially skipped until dependencies were installed.
- `.venv/bin/python -m unittest discover -s tests -v`: **8 passed**, no skips.
- `.venv/bin/ruff check byteforge tests probes/btrblocks/fsst_sample_patch.py`
  and the matching `ruff format --check`: pass after fixing initial style errors.
- `.venv/bin/python -m byteforge.qualification <new-output.json>`: Python
  Parquet/Zstd probes pass; SGTT/native observations are intentionally separate.
- CMake configure/build and `ctest --test-dir build/btrblocks --output-on-failure`
  ran for the pristine and corrected variants, with the failures above retained.
  Final sanitizer suite uses `BYTEFORGE_SANITIZE=ON` and
  `BYTEFORGE_FSST_SAFETY_FIX=ON`; **5 passed**.
- `pip install '.[qualification,dev,native]'` initially failed to resolve
  setuptools through the default index; retry with explicit
  `--index-url https://pypi.org/simple` built and installed ByteForge successfully.
  `.venv/bin/python -m pip check`: no broken requirements.
- Git diff/whitespace and intended-file inspection before commits; no binaries,
  upstream assets, datasets or environment/cache directories staged.

Commits: `c1643ab` introduces representations; `383de1c` adds dependency/native
probes and explicit corrections. No push or default-branch merge was performed.

## Next stages and remaining risks

Keep pristine and corrected native variants distinct. Add versioned standalone archives,
checksums, archived decoding assets, fresh-process recovery and corruption tests
before performance work. Native zero-row/null handling, more adaptive scheme
coverage, hostile-input isolation and uint32-offset limits need further tests.
No query engine, benchmark runner, demo, dataset download or performance claim
exists yet. Dataset rights and benchmark hardware/time/storage budgets remain
unresolved; independent review remains outstanding.

## Raw HDFS implementation milestone — 2026-10-24

User authorization narrows this milestone to raw LogHub-2.0 HDFS A/B pipelines,
with an explicitly test-only encoder where SGTT cannot legally be integrated.
Starting checkout/HEAD verified at `d492b4b`, branch `implementation/core`,
canonical origin unchanged. AGENTS.md, qualification documents, all four research
documents, existing interfaces/tests, and the pinned SGTT public API were inspected.
Research files and AGENTS.md remain untouched/untracked. Independent review is
still missing; user authorization permits implementation despite that risk.

Official evidence: LogHub-2.0 main at `ac4aad2ea86f561e5a0acbc6587fa28b487259f9`,
Zenodo record 8275861/v1 metadata, HDFS.zip central directory and first partial raw
member bytes. Only two 65,536-byte archive ranges and the official 2k sample were
retrieved for reconnaissance. Primary member: `HDFS/HDFS_full.log`; structured
and template CSVs are excluded. Published sizes/counts are source observations,
not local full-dataset validation. No full dataset was downloaded or benchmarked.
Source/license/citation evidence is in `datasets/hdfs-loghub-2.0.json`.

`19b8e63` adds the strict LF/UTF-8 loader, bounded canonical framing, manifest and
synthetic fixture. `3d106e3` implements genuine Parquet+Zstd archives, the persistent
native BtrBlocks INTEGER worker, complete ID/offset/table archives, independent
reconstruction, CLI, paired benchmark execution and failure-aware raw records.
The existing tokenizer protocol/serialization checks are reused. No SGTT source
or table is copied or executed, and the fixture encoder is never labeled SGTT.

Validation actually performed:

- `.venv/bin/cmake -S probes/btrblocks -B build/btrblocks
  -DBTRBLOCKS_SOURCE=/tmp/byteforge-btr -DCMAKE_BUILD_TYPE=Release
  -DBYTEFORGE_SANITIZE=ON -DBYTEFORGE_FSST_SAFETY_FIX=ON` and
  `cmake --build ... --target byteforge_integer_worker --parallel 2`: pass.
- `.venv/bin/python -m unittest discover -s tests -v`: **18 pass**, no skips,
  including native int32 maximum, block/row boundaries, empty input, UTF-8/NUL/
  CRLF preservation, standalone decode after input deletion, corruption rejection
  and explicit unavailable-backend behavior. The new native worker uses ASan/LSan.
- `BYTEFORGE_BTR_WORKER=/tmp/byteforge-native-not-built python3 -m unittest
  discover -s tests -v`: **18 discovered, 9 skipped, remaining 9 pass**. This checks
  operation without optional Arrow/native dependencies; no real dataset is needed.
- Ruff lint and formatting checks pass. Initial authoring style errors were fixed.
  No type checker is configured. `pip check` reports no broken requirements.
- `pip install '.[benchmark,dev,native]' --index-url https://pypi.org/simple`:
  wheel build/install succeeds, including packaged license assets. From `/tmp`,
  the installed package reconstructs a B-test-only archive exactly to raw bytes.
- `check-dataset` without a supplied dataset exits nonzero with official download
  instructions, as intended. A one-round synthetic smoke run and `restore --raw`
  plus `cmp` pass before the final recorded run.
- At code revision `3d106e3`, `run --path tests/fixtures/hdfs-synthetic.log
  --synthetic --test-encoder --output results/hdfs-synthetic-validation
  --warmups 2 --repetitions 10`: **20 measured + 4 warmup trials valid, zero failed**.
  Every trial verifies canonical and physical raw bytes exactly. Fixture: 6 rows,
  1,001 logical UTF-8 bytes, 1,007 physical source bytes. Production B is a separate
  unsupported/not-run record with a licensing reason. These are synthetic
  development diagnostics, not HDFS or SGTT performance findings.

The raw run/configuration, summaries, native identity and archives remain under
ignored `results/hdfs-synthetic-validation/`; large/generated outputs are not
committed. `docs/hdfs_validation.json` summarizes verification and raw-file hashes.
Source parsing is timed separately; full reconstruction includes physical export.
Whole-tree peak RSS remains unavailable; individual process high-water marks are
diagnostic. Native sampling has upstream random-device entropy despite seeded
trial order. Exact native decoder-build matching and trusted-input restrictions
are documented. No default-branch merge, push or PR was performed.

Remaining research work: obtain the exact full raw artifact, resolve SGTT code/
table rights and implement its authorized adapter, complete independent review,
lock all native transitive dependencies, and declare hardware/run budgets before
real full-corpus measurements. Synthetic byte-token results cannot answer the
primary research question. Reproduction commands: `docs/hdfs_benchmark.md`.

## Real HDFS_2k validation and SGTT recheck — 2026-10-24

Starting HEAD verified at `aa01a96` on `implementation/core`; origin remains the
canonical ByteForge URL. The source is `data/hdfs/HDFS_2k.log`, not another dataset
named after a directory. Its full bytes match the official raw sample at current
LogHub-2.0 main `ac4aad2ea86f561e5a0acbc6587fa28b487259f9`: 285,848 bytes,
2,000 logical records, 283,848 UTF-8 payload bytes, 2,000 LF terminators, zero
CR/CRLF or blank rows, final LF present. It is ASCII-compatible strict UTF-8.
SHA-256 is `6fe25449e79d75e35bb223ead9729fa02c00b7abb23e4e8ec0f3bb2addec6e3a`;
the original remains unchanged. Only a small official sample was downloaded for
comparison, not the full corpus. The source/license/contract record is now
`datasets/hdfs-2k.json`.

An initial A-only run at `aa01a96` passed and is retained separately. Inspection
showed two in-scope issues: metadata mislabeled the sample as a full-corpus
candidate, and the previous synthetic-only encoder guard prevented the newly
authorized real-data infrastructure test. Code commit `0ebf117` identifies the
sample by pinned checksum/dimensions, separates data provenance from encoder
provenance, and permits the explicit CLI `--test-encoder` choice on real input.
The library's default real-input guard remains, with an explicit opt-in override.
Production B still cannot fall back to the byte-symbol test encoder.

At `0ebf117`, the paired run on the actual sample with two warmups and ten measured
repetitions passed **20 measured trials + four warmups, zero failures**. Both
canonical ordered values and physical raw files match byte for byte. Separate
`restore --raw` + `cmp` checks also passed for A and B-test-only. Actual archive
sizes: A 64,278 bytes (Parquet file 60,508), B-test-only 233,314 bytes (native data
225,545, vocabulary 2,308, index 892, manifest 4,569). The sample result is not a
claim about SGTT or the full corpus. Native workers remain ASan/LSan instrumented.
Raw measurements and full configurations are preserved under
`docs/measurements/hdfs-2k/` and `results/hdfs-2k-real-validation/`; report and exact
commands are in `docs/hdfs_2k_results.md`. No outliers were discarded or pooled
with the earlier A-only run.

SGTT remote HEAD still equals pinned `e202e36e2b33a4768d1122f96880b64416234175`.
Root LICENSE/COPYING endpoints return 404; the tracked tree contains a nested
dataset MIT notice and an OnPair dependency notice, not an established SGTT
source/patched-table grant. The dataset notice was missed by earlier filesystem
inventory; that correction does not establish permission for the implementation.
The protected ID/table API boundary was re-inspected. No SGTT source/table was
copied into ByteForge, built or executed, and no author was contacted. Evidence
and the remaining permission requirement are in `docs/sgtt_integration_status.json`.
The actual SGTT run remains unsupported under the user's stated restriction.

`.venv/bin/python -m unittest discover -s tests -v`: **21 pass**, no skips, with
new sample-identity and real-input/test-encoder separation regressions. Ruff lint,
format checks and `pip check` pass. Existing Python dependencies and native worker
were reused; no new dependency installation or backend substitution. AGENTS.md,
research files, source dataset and old results were preserved. Independent review
is still missing; it did not block this explicitly authorized validation.

## SGTT qualification and archive guards — 2026-10-24

Continued from `e43b598` on `implementation/core`. Rechecked the authoritative
PVLDB paper and its artifact link to `umbra-db/token-vldb2026`, pinned at
`e202e36e2b33a4768d1122f96880b64416234175`. Source and patched table permission
remain unestablished; no SGTT source/assets were copied into ByteForge or executed.
Independent review remains missing; the user's authorization permits independent
implementation to continue. Detailed evidence and release control measurements
are recorded in `docs/sgtt_hdfs_2k_results.md`.

Added `byteforge/sgtt.py` qualification status and explicit CLI encoder selection.
Production encoding/decoding cannot silently accept a test tokenizer as SGTT.
The runner records both SGTT+BtrBlocks and SGTT+Zstd as unmeasured/unsupported.
Archive metadata arithmetic and the entire integer-block index are checked before
native decompression; test vocabulary identity is verified independently of hashes.
Existing original and `fsst-safety-v1` native build behavior remains unchanged.

Validation: `.venv/bin/python -m unittest discover -s tests -v` passed 25 tests
before the final vocabulary regression test was added. With
`BYTEFORGE_BTR_WORKER=build/sgtt-controls-release/byteforge_integer_worker`, the
final suite passed **26 tests**. Ruff check/format checks passed; `pip check`
reported no broken requirements. CTest in the separate non-sanitized release
build passed **5/5** probes. An initial Ruff check found import ordering and one
long line; both were fixed before committing. These are infrastructure/control
tests, not actual SGTT reference or reconstruction tests.

### Release controls and saved evidence

At implementation revision `560c1cc`, ran the unchanged, official-byte-matched
`data/hdfs/HDFS_2k.log` through A and explicitly selected diagnostic byte symbols.
The separate Release build reports sanitized=0 and has no ASan dynamic runtime;
original worker hash remains unchanged. No new dependencies installed. Build
configure and compilation succeeded; recipe, flags and hashes are in the report.

Command: `.venv/bin/python -m byteforge run --path data/hdfs/HDFS_2k.log
--output results/sgtt-hdfs-2k-release-controls --encoder test-byte
--worker build/sgtt-controls-release/byteforge_integer_worker --warmups 2
--repetitions 10`. Outcome: **24/24 exact canonical and physical reconstructions**,
zero failed trials. Actual SGTT+BtrBlocks and SGTT+Zstd were not run. Complete A
archive: 64,278 B, median encode/reconstruction 7.410/15.541 ms; diagnostic archive:
233,320 B, medians 106.476/83.485 ms. Variability and timing boundaries are recorded;
these measurements make no SGTT performance claim.

Saved raw JSONL, environment/configuration, summaries, table/source permission
HTTP evidence, dataset validation, representative manifests and native directories
under `docs/measurements/sgtt-hdfs-2k/`. Source dataset was re-compared with the
pinned official sample and is unchanged. Added `scripts/summarize_controls.py`;
its output exactly matches the saved phase summary. Final default ASan-worker
Python suite passed **26 tests**, as did the Release-worker suite. Ruff and
`pip check` passed. Actual SGTT tests/profiling, asset accounting and warm-service
measurements remain blocked/unavailable, not simulated. No query claim is made.

Next requirement: obtain an applicable source and exact table/vocabulary grant,
complete transitive dependency audit, then qualify public Encoder/Decoder APIs,
reference IDs, empty rows, byte fallback, 2,000-row exact reconstruction and a
versioned asset-aware production archive. Author contact requires authorization.
Existing research documents and AGENTS.md were neither modified nor staged.

## Log-query and decompression latency work — 2026-10-24

User prioritizes query/decompression speed and permits a compression-ratio tradeoff.
Continued at `41f6ae6` on `implementation/core`; origin fetched safely and existing
untracked AGENTS.md/research preserved. Read current archive/representation/
tokenizer/benchmark/CLI sources, saved phase observations, AGENTS.md and relevant
query/cache requirements in research benchmark/decision documents. Actual SGTT
permission blocker remains unchanged; no restricted material was used.

Optimized the exact diagnostic byte-table decoder using C-level integer-to-byte
conversion and row slicing, preserving ID bounds, offset validation and strict
per-row UTF-8. Removed redundant serialization allocations and repeated validation
while retaining all required checks. Existing archives/native variants remain
compatible. Added streaming literal equality/contains queries with consumed ordered
row IDs, reusable native handles and optional equally bounded Arrow-buffer caches
for A/B. The cache does not store query results; default zero re-decompresses each
scan. Archives must remain immutable while a query session is open.

Added opt-in `fast-scan` Parquet profile (Zstd level 1, no dictionary); standard
profile remains unchanged. Added query correctness/edge/budget/invalid-ID and
offset regression tests and a reproducible exploratory query timing runner.
Release-worker Python suite passed 29 tests; Ruff checks passed after fixing
formatting and a test closure binding. No additional dependencies installed.
Measurements and user-facing commands follow in `docs/log_query_performance.md`.

### Latency measurements and final validation

At `3ff31e4`, a paired before/after test (`scripts/compare_restore.py`) used the
identical original 233,320-byte diagnostic archive and unchanged Release worker.
Python `41f6ae6` was read via a private `git archive` snapshot; no worktree/branch
was modified. Two warmups plus ten trials per variant: all 24 physical outputs
matched exactly. Median full restoration 83.055 → 28.234 ms (2.94×); median token
decode/validation 56.276 → 0.949 ms. This is not a native codec or SGTT speedup.

A new fast-scan complete pipeline run had zero failures and 24 exact round trips.
Fast-scan A: 57,655 B; diagnostic B: unchanged 233,320 B. Existing results untouched.
The query runner compared standard A, fast-scan A and diagnostic B on the same
canonical source, no cache and equal 16 MiB cache caps, with two warmups/ten
measured rounds. All 216 observation records passed the Python result reference.
Repeated WARN scan medians without value cache: 0.730/0.599/6.814 ms; with cache:
0.367/0.368/0.369 ms. Cached equality: approximately 0.042 ms for all. Retained
Arrow buffers 291,852 B. First-request costs, trial ranges, batching and limitations
are documented; caching is not called decompression. Final-query-evaluation
batching protocol is not claimed for this exploratory API benchmark.

Saved raw observations/configurations under `docs/measurements/log-latency/` and
commands/explanations in `docs/log_query_performance.md`. An initial direct-script
query launch failed before creating output because the installed package lacked
the new module; `python -m scripts.benchmark_log_queries` from the repository root
uses current sources and resolved it. Failure retained in attempts.json. Default
ASan-worker and Release-worker Python suites both passed 29 tests; CTest 5/5,
Ruff check/format, pip check and diff checks passed. No extra dependencies installed,
no restricted SGTT assets used, no user research/data modified, nothing pushed.

## Follow-up: reduce B archive size and query/reconstruction latency — 2026-10-24

Working tree continued from `d178668` on `implementation/core`; no push or merge.
Inspected pipeline, native worker, query, CLI and benchmark code plus prior result
artifacts. The new worker was built as a separate unsanitized Release target with
protocol 3; the previous worker/build and archive variants were left intact.

Added direct native int32-to-byte output for checked 0..255 token IDs, so restore
and query no longer create a Python integer object per encoded byte. Added optional
outer Zstd framing around BtrBlocks' native stream, defaulted benchmark runs to
level 1, and retained `none` as an explicit comparison. Existing protocol-2
archives remain readable with their matching worker. Restore now encodes each row
once for both canonical output and logical hashing. A Python `QuerySession` caches
decoded Arrow values up to 16 MiB by default; cache remains configurable and the
single-shot CLI stays uncached. Literal B queries pass UTF-8 bytes directly to
Arrow binary kernels.

On `data/hdfs/HDFS_2k.log` (2,000 rows, 285,848 source bytes, previously verified
SHA-256 unchanged), two warmups and ten measured Release trials per pipeline all
passed exact canonical and physical reconstruction. A: 64,278 B, 6.801 ms encode,
14.259 ms restore. Diagnostic B: 100,811–100,813 B with outer Zstd (233,320 B
without it), 67.579 ms encode and 20.228 ms restore. The latest B archive is still
1.57x A size and 1.42x its restore median; this is not an SGTT comparison.
Outer Zstd level 1 saves 56.8% over raw B. BtrBlocks DICT-encodes byte IDs and
BP-encodes offsets; encoded native stream is 225,545 B before outer Zstd. One
32-bit ID per raw input byte is the principal diagnostic-representation problem.

Ten-round query observations (all IDs checked against a Python reference): first
WARN query A/B 1.361/18.087 ms; uncached repeated contains 0.713/2.534 ms;
uncached repeated equality 0.425/2.203 ms. With equal 16 MiB cache caps, contains
is 0.366/0.372 ms and equality 0.048/0.049 ms. The cache makes repeated filters
competitive by retaining decoded values; it does not accelerate the cold first
decode. The test-only encoder remains visibly named and production SGTT remains
license-blocked. See `docs/log_query_performance.md` and
`docs/measurements/btrblocks-latency-hdfs-2k/` for raw evidence and limitations.

Validation for this follow-up: isolated CMake Release build succeeded, worker
reports protocol 3 and `sanitized=0`; the complete HDFS benchmark reported 0
failures across 24 trial executions and exact reconstruction; query run retained
288 observations with 0 failures. Ruff and whitespace checks are pending final
review; final Ruff and whitespace checks passed. No unit test suite was run in
this follow-up. No source dataset or
pre-existing measurement was modified.

## Query-aware storage and selective decoding — 2026-10-24

Continued from `5daf0de` on `implementation/core`. Added a versioned v2 HDFS
archive with independent 256-row compressed blocks, a Zstd-compressed block
directory, safe byte-trigram Bloom filters, per-frame checksums, and bounded
decoded-block LRU caching. `QuerySession` supports ordered `contains`, `equals`,
`prefix`, and half-open row-range filtering without full-column reconstruction.
The original v1 reader/query path remains covered. All B results still use the
test-only byte-symbol encoder; no SGTT implementation or claim was introduced.

Reproduced the release A/full B run on the verified unchanged 2k source and
measured 11 deterministic query cases across Parquet, full B, and v2 B: two
warmups and ten measured rounds. All 10,296 observations matched the Python
reference; exact v2 restore produced the same 2,000 canonical rows. Complete
sizes: A 64,278 B, full B 100,813 B, and block B 114,957 B. Cold `WARN` p50:
1.337/17.064/17.300 ms; cold absent-sentinel p50: 1.404/17.994/0.581 ms for
A/full B/block B. The absent query pruned 8/8 blocks and read zero data frames;
`WARN` examined 5/8 blocks. A 16 MiB cache gave block B warm `WARN` p50 of
0.323 ms. Block layout improves selective work for prunable terms but increases
size and does not beat Parquet on cold common terms. Raw trial records and
configuration are in `docs/measurements/query-aware-hdfs-2k/`; full discussion
is `docs/query_aware_storage_results.md`.

The Release-worker Python unittest suite passed 37 tests after updating the
protocol-3 fast-decoder corruption test to exercise the byte-ID operation and
offset validation. Ruff check and format check passed, compileall passed,
`pip check` found no broken requirements, and Release native CTest passed 5/5.
Initial
`cmake`/`ctest` commands failed because the executables were not on the shell
PATH; reran successfully through `.venv/bin/cmake` and `.venv/bin/ctest`. No source data,
research-owned files, prior measurement outputs, or Git history were changed;
no push or merge was performed.

## Larger Spark query scale check — 2026-10-24

Downloaded LogHub-2.0's Spark archive from its Zenodo v1 record and verified the
published 230,699,188-byte archive MD5 `ca0ef9c6e8ece0df602793bd302e03a6`. Its
raw member is 1,629,352,119 bytes. Created a local ignored prefix ending at a
complete LF record: 209,715,181 bytes (200 MiB minus 19 bytes), 2,035,741 rows,
SHA-256 `61330feaedcd2ad3021816605401a1e272659c2bb2ad1a2851b98310b3635ea3`.
The full Spark corpus was not extracted or benchmarked. Dataset notice and
citations are recorded in the sample manifest.

Built Parquet, full BtrBlocks+Zstd and block B archives. Complete sizes were
14,016,582 B / 33,430,273 B / 52,780,861 B; build times were 1.413 / 46.299 /
71.127 s. The two B configurations use test-only byte IDs. On three queries
(WARN, a rare block identifier, absent sentinel), two warmup and three measured
rounds, 270 observations had zero correctness failures. All three archives
restored exactly to the same canonical checksum. Cold first-request p50s in ms:
WARN 463.0 / 1,773.4 / 202.7; rare ID 502.0 / 1,860.3 / 140.5; absent 500.3 /
1,754.9 / 129.2 (A/full B/block B). The block path checked 292/7,953 blocks for
WARN, one block for the rare ID, and no payload blocks for the absent term.
Its 7.1 MB index caused roughly 114 ms of first-session open time. Here “cold”
means first query in a new session, not cold disk; OS cache was uncontrolled.

Improved the large-run query harness by counting rare terms in one pass,
precomputing reference results once, and allowing selected query IDs and a
separate timed warm-repeat count. Raw data, archive measurements and full
observation records are saved under `docs/measurements/query-aware-spark-200m/`;
analysis is in `docs/query_aware_spark_200m_results.md`. No dataset/archive
binary was staged or committed.

## Full LogHub-2.0 Thunderbird benchmark — 2026-10-24

Located the largest raw log member among the inspected large LogHub-2.0
artifacts: `Thunderbird/Thunderbird_full.log` (2,516,177,713 bytes) in the
official 293,018,641-byte ZIP. The ZIP MD5 matched Zenodo's published checksum;
the extracted log SHA-256 is recorded in the measurement manifest. Prepared and
ran the complete 16,601,745 rows; source remained in ignored `data/`.

Built and exactly restored genuine Parquet+Zstd, full B, and block B. B continues
to use test-only byte symbols, not SGTT. Complete archive sizes were 164,417,323,
631,875,507, and 818,064,093 bytes. Full restore times were 219.527, 33.548, and
47.110 seconds respectively. One-trial query screening found Block B helpful for
selective/absent terms and slower than Parquet for common `warning` (636,710
matches); all 36 observations matched the reference. The large Block B index
(62,651,861 bytes) adds about 1.3 seconds of session-open cost. Results and raw
observations: `docs/query_aware_thunderbird_full_results.md` and
`docs/measurements/query-aware-thunderbird-full/`. Full query results are only
screening observations, not stable p50/p95 claims.

The large-file runner now streams reference rows instead of holding the complete
source in Python memory. Dataset preparation accepts explicit non-synthetic
provenance, and the full-B builder accepts bounded payload windows to keep
intermediate integer buffers practical. Validation outcomes and local commit
are recorded in the handoff; no data binaries, research files, or AGENTS.md were
staged.

## Parquet LZ4 and Block B tuning — 2026-10-24

Added `--parquet-profile lz4` / `build_parquet(profile="lz4")`, keeping the
standard Parquet 2.6, dictionary, page and window settings while selecting
PyArrow's Parquet LZ4 codec. Unit tests cover exact restore and query results.
The 16.6-million-row Thunderbird LZ4 archive was 299,019,419 B; build was
13.826 s and full canonical restore 13.547 s. Zstd was 164,417,323 B, 15.354 s
build and 13.857 s restore in the measured runs. One-run query timing favored
LZ4 by roughly 6–10%, with substantially larger storage.

Investigated Block B's 64,851-block/62.7 MB index. A 4,096-row variant reduced
its index to 2.09 MB and complete archive to 640,649,882 B, and reduced build /
restore from 866.466 / 47.110 s to 718.325 / 32.211 s. Query selectivity
regressed for a sampled timestamp and common term, so the default remains 256
rows. Raising the Bloom cap to 65,536 bits yielded a 17.14 MB index and
655,699,338 B archive; it pruned absent queries quickly but still scanned 11.1M
rows for the sampled timestamp. The block-size tradeoff is documented rather
than presented as a universal improvement.

The completed query screen recorded 48 observations with zero reference
mismatches, one trial per query/configuration, no warmups, and uncontrolled OS
cache. Exact restores of the Parquet LZ4 and 4,096-row variants matched the full
canonical SHA-256. The full Python suite passed 40 tests; Ruff, formatting,
compileall, `pip check`, and native CTest (5/5) passed. See
`docs/query_aware_thunderbird_lz4_results.md` and
`docs/measurements/query-aware-thunderbird-lz4/`. The larger repeated-query
matrix was interrupted for runtime cost; its partial output is excluded from
reported measurements. No data files were added to Git.
