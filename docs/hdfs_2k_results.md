# HDFS_2k real-data validation

## Input and provenance

The actual input is `data/hdfs/HDFS_2k.log`, found under the repository's data
directory. It was not modified. Its complete bytes match the
[official sample at pinned revision ac4aad2](https://raw.githubusercontent.com/logpai/loghub-2.0/ac4aad2ea86f561e5a0acbc6587fa28b487259f9/2k_dataset/HDFS/HDFS_2k.log).
The current upstream main was also checked at that same revision.

| Property | Observed value |
|---|---:|
| Physical file bytes | 285,848 |
| Logical ordered records | 2,000 |
| Logical UTF-8 payload bytes | 283,848 |
| LF / CRLF / CR counts | 2,000 / 0 / 0 |
| Blank records | 0 |
| Final LF | Present |
| Encoding | Valid UTF-8; all bytes ASCII |

SHA-256: `6fe25449e79d75e35bb223ead9729fa02c00b7abb23e4e8ec0f3bb2addec6e3a`.
The loader removes only each LF delimiter; the final LF does not create a 2,001st
record. No whitespace, identifiers, normalization forms, templates or timestamps
are rewritten. The final-LF flag restores physical framing. The checksum remained
unchanged after all runs. This is the real **2k sample**, not the full Zenodo HDFS
corpus. [Dataset manifest](../datasets/hdfs-2k.json) records source, license,
citations, observed dimensions and canonical checksums.

## Executed experiment

Starting repository HEAD: `aa01a96`; implementation/run revision: `0ebf117` on
`implementation/core`. An initial one-trial A validation at `aa01a96` succeeded
but exposed the loader's full-corpus provenance label for this sample. That run
is retained separately in `results/hdfs-2k-baseline-aa01a96` and is not pooled with
the corrected experiment below.

The corrected loader identifies the sample by SHA-256, size and row count,
independently of its directory. A wrong-checksum file named HDFS_2k.log remains
an unverified candidate. Input provenance and tokenizer provenance are separate:
explicit `--test-encoder` now allows real-data infrastructure validation without
marking the input synthetic. A direct library caller must explicitly set
`allow_test_encoder_on_real=True`. Production B remains a separate blocked record.

```bash
.venv/bin/python -m byteforge run --path data/hdfs/HDFS_2k.log \
  --test-encoder --output results/hdfs-2k-real-validation \
  --warmups 2 --repetitions 10
```

All **20 measured trials and four warmups** passed. Every trial checks ordered
canonical reconstruction and compares the final raw log with the original file
byte for byte. Separate `restore --raw` commands for A and B-test-only followed
by `cmp` also passed. There are zero failed trials or discarded outliers.

## Actual measurements

These medians describe the declared implementation and sample, not a paper result.
The native worker uses ASan/LeakSanitizer and the explicit FSST safety variant;
Parquet uses PyArrow 26.0.0 and Zstd level 3. **B-test-only is a reversible
256-entry byte-symbol encoder, not SGTT or an LLM-derived token table.**

| Pipeline | Complete archive bytes | Encode median (ms) | Raw reconstruction median (ms) |
|---|---:|---:|---:|
| A: Parquet+Zstd | 64,278 | 6.862 | 14.536 |
| B-test-only: byte symbols+BtrBlocks INTEGER | 233,314 | 138.686 | 92.813 |
| B: actual SGTT+BtrBlocks | Not run | Not run | Not run |

A contains a 60,508-byte genuine Parquet file and a 3,770-byte manifest. Raw
Zstd-only bytes are not exposed; column-chunk sizes include page headers.
B-test-only contains 225,545 bytes of native blocks, an 892-byte index, the complete
2,308-byte vocabulary and a 4,569-byte manifest. Its native encoded-column count
is 225,353 bytes, excluding outer block headers/null maps. No asset overhead is
hidden. Ordinary decoder deployment remains outside archive size for both paths.

Reconstruction includes validation, read/decode, detokenization where applicable,
canonical materialization/write, physical raw export and checksum checks. Exact
reference comparisons are outside timers. Raw records also retain phase timings,
fresh-process wall time, CPU counters and process-specific RSS diagnostics.
Whole-tree RSS is unavailable. OS cache is uncontrolled; CPU affinity/thread
settings are recorded. Ten repetitions are not independent datasets, and these
short sample operations are not batched to a 250 ms timing floor. Native sampling
retains upstream random-device entropy. No significance, generalization to the
full corpus, or SGTT benefit/loss follows from this infrastructure comparison.

[Raw observations](measurements/hdfs-2k/observations.jsonl),
[run configuration/hardware/versions](measurements/hdfs-2k/run.json),
[summary](measurements/hdfs-2k/summary.json), and
[completion/checksum record](measurements/hdfs-2k/complete.json) are committed.
Trial archives and worker logs remain under ignored
`results/hdfs-2k-real-validation/`. Existing run directories are never overwritten;
choose a new output name when reproducing. No full corpus or protected SGTT asset
was downloaded for this experiment.

## Actual SGTT integration status

The [official SGTT repository](https://github.com/umbra-db/token-vldb2026) still has
HEAD `e202e36e2b33a4768d1122f96880b64416234175`. Root LICENSE and COPYING requests
returned HTTP 404. A complete tracked-file inventory found `data/dbtext/LICENSE`
and `thirdparty/onpair/LICENSE`; the dataset notice was omitted by earlier
filesystem-only reconnaissance. These nested notices do not establish a source/
patched-table grant for the SGTT implementation. Tokenizer.hpp has its own author
copyright notice; no root grant or README reuse permission was found in the
inspected snapshot. This is a permission-evidence finding, not a determination
that all possible uses are unlawful.

Under the task's explicit licensing restriction, actual SGTT was **not copied into
ByteForge, built, executed, bundled or benchmarked**. No author contact was sent. The existing
production tokenizer protocol and archive decoder remain ready for an authorized
adapter. Source inspection confirms public encode/compress/decode methods but
protected raw ID state and vocabulary accessors; the adapter must expose canonical
per-row IDs and archive the complete decoding table. Code/table permission and
adapter qualification are still required. An unrelated MIT dependency/dataset
notice or public repository visibility does not close that requirement.

[Machine-readable SGTT evidence](sgtt_integration_status.json) records the checks.
No other LLM encoder was silently substituted. Missing independent research
review remains a separate risk and did not block the authorized real-data tests.

## Validation and Git handoff

- Native integer worker and optional Python dependencies were already available;
  no new dependencies were installed and no backend substitution was made.
- `.venv/bin/python -m unittest discover -s tests -v`: **21 pass**. New regressions
  cover checksum-based identity, real-input provenance with the test encoder, and
  preserving the default library opt-in guard. Tests do not require this dataset.
- Ruff lint/format checks and `pip check` pass. No type checker is configured.
- Without optional Arrow/native dependencies: 21 tests discovered, 10 pass and
  11 optional tests skip; no real dataset is required by the unit suite.
- The original untracked AGENTS.md and research files remain untouched.
- No push, default-branch merge, PR, or external author message was performed.
