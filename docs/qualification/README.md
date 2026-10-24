# Reproduce dependency qualification

These are correctness probes, not compression or analytical benchmarks. Run from
the repository root. Python 3.11+ is required; observed environment was 3.14.8.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[qualification,dev,native]' --index-url https://pypi.org/simple
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check byteforge tests probes/btrblocks/fsst_sample_patch.py
.venv/bin/ruff format --check byteforge tests probes/btrblocks/fsst_sample_patch.py
.venv/bin/python -m byteforge.qualification build/new-python-probe.json
```

The JSON output path must not already exist. Missing optional dependencies skip
their unit tests but cause the qualification command to exit nonzero. Core tests
need only Python's standard library. No type checker is configured yet.

## Native probe

Use a new clone directory; never clone over an existing checkout. The following
sample path must be absent. No upstream setup script is required.

```bash
git clone https://github.com/maxi-k/btrblocks.git /tmp/byteforge-btr-qualification
git -C /tmp/byteforge-btr-qualification checkout --detach c950c3c01c1fbf91007936b05781d431a29a60d3
.venv/bin/cmake -S probes/btrblocks -B build/native-pristine \
  -DBTRBLOCKS_SOURCE=/tmp/byteforge-btr-qualification -DCMAKE_BUILD_TYPE=Release
.venv/bin/cmake --build build/native-pristine --target byteforge_btrblocks_probe --parallel 2
.venv/bin/ctest --test-dir build/native-pristine --output-on-failure --output-junit native.xml
```

The pristine five-case suite is expected to expose the recorded long-row failure
on the observed toolchain. Preserve failures; do not treat a partial pass as full
native-string qualification. Zero-row native input is not invoked. The integer
probe exercises actual BtrBlocks INTEGER columns; ID and offset streams must be
separate columns/blocks because they have different lengths.

For diagnostic instrumentation, configure with `-DBYTEFORGE_SANITIZE=ON`; this
instruments the core, probe and FSST, but not FastPFOR/CRoaring. A core-only ASan
trial with leak detection disabled passed despite the release failure; instrumenting FSST exposed the
sample-length heap overflow. It is therefore insufficient to rely on core-only
instrumentation. Sanitizer checks are separate from performance builds.

## Explicit corrected variant

Use another build directory and include both flags:

```bash
.venv/bin/cmake -S probes/btrblocks -B build/native-fixed \
  -DBTRBLOCKS_SOURCE=/tmp/byteforge-btr-qualification -DCMAKE_BUILD_TYPE=Release \
  -DBYTEFORGE_SANITIZE=ON -DBYTEFORGE_FSST_SAFETY_FIX=ON
.venv/bin/cmake --build build/native-fixed --target byteforge_btrblocks_probe --parallel 2
.venv/bin/ctest --test-dir build/native-fixed --output-on-failure --output-junit native-fixed.xml
```

`btrblocks-c950c3c-fsst-safety-v1` increases the allocated sample-length
array bound (nonempty chunks imply the target byte count bounds their number),
pads the scalar compressor's stack buffer for eight-byte loads, and releases
the encoder through an RAII guard in a private copy of BtrBlocks' FSST adapter.
Sample choice, codes and schemes are unchanged. The corrections are opt-in and
versioned, with default sanitizer leak detection retained. They edit the downloaded
FSST dependency and generate a corrected adapter inside the build directory;
the BtrBlocks checkout remains unchanged. Turning the flag off
in a previously patched directory fails clearly; use a new build directory for
pristine runs. See [FSST notice](FSST-NOTICE.txt). No performance qualification is
implied: allocation/cleanup costs must be included when this variant is measured.

All native builds also supply `<cstdint>` to pinned FSST for GCC 16 compatibility.
This is a compiler/header correction, not a codec substitution. Upstream build
warnings and temporary dependencies remain in `build/`; network access, Make,
GCC/compatible x86-64 toolchain and free disk space are required. The initial
native dependency tree occupied about 631 MiB before sanitizer builds. Boost
submodules and native codec dependencies come from upstream pins. FastPFOR's
build-only Google Test reference is moving `main`, so this is not yet a fully
locked benchmark environment; [native-sources.json](native-sources.json) captures
the resolved revisions and license hashes from this execution.

Raw files here preserve Python observations, native test output and failure
traces. Their revisions/dirty states identify the staged development execution;
timestamps and test duration fields are diagnostic, never benchmark results.
Trailing whitespace in captured XML was normalized; measurements were not changed.
SGTT code/table rights remain unresolved and no LLM-tokenizer round trip is
claimed. Independent review, standalone archive qualification and real-data
benchmark decisions remain outstanding. See [implementation log](../implementation_log.md).
