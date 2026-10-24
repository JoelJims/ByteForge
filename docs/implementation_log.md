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
