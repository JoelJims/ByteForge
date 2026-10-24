# Exact word-index queries on Thunderbird

## Scope and implementation

This experiment adds an **optional auxiliary index**, not a new tokenizer or
storage codec. B remains `test-only-byte-symbols-v1` plus real BtrBlocks and
per-block Zstd. Actual SGTT remains permission-blocked. The index also works on
Parquet: any resulting advantage must be attributed to indexing, not SGTT or
BtrBlocks. Existing archives and measurements remain unchanged.

`byteforge.word_index` indexes every maximal `[A-Za-z]+` run, preserving case.
For a nonempty ASCII-letter-only `contains` query, a matching substring must be
inside one such run. Searching the complete word dictionary and unioning its
row postings therefore returns exactly the matching ordered row IDs, including
substrings inside words, with no false negatives or false positives. Duplicate
occurrences in a row contribute one ID. This is not a word-boundary query and
does not normalize the logs. Unicode letters, digits, punctuation, empty terms,
`equals`, and `prefix` use the existing exact payload path. Row ranges apply to
both paths. Nulls remain outside the raw-log contract.

Postings use bounded 65,536-row windows, delta-uint16 little-endian offsets and
independent Zstd level-1 frames in SQLite. The dictionary and all postings are
included in index size. SQLite is a directory/query index; it does not replace
the BtrBlocks log payload. The reader fully hashes the index at session opening,
verifies dataset identity and file sizes, checks mutation between calls, and
bounds frame lengths and decoded row IDs. Payload verification is deferred for
index-only answers; fallback reopens the fully validating archive reader.
Checksums address accidental corruption, not malicious archive producers.

No decoded values, postings, or query results are retained between requests.
SQLite has a bounded 2 MiB page cache; OS page cache is uncontrolled. Result-ID
materialization still scales with match count and is included in timing. The
query returns IDs/count, not reconstructed matching log strings. Index creation
reads the checksum-verified canonical input; it needs no SGTT assets.

Separately, regular Block B now prepares query trigram hash positions once per
Bloom width instead of hashing the same needle separately for each block.
Bloom decisions and archive formats are unchanged.

## Reproduction

Run from the repository root; use new result directories for every repetition.

```sh
PYTHONPATH=. .venv/bin/python -m byteforge build-word-index \
  --prepared results/thunderbird-full/prepared \
  --output results/word-index-thunderbird/index
PYTHONPATH=. .venv/bin/python -m byteforge query \
  results/thunderbird-full/block-b-4096-widebloom \
  --word-index results/word-index-thunderbird/index \
  --operation contains --value warning
```

The index is a separate required bundle for this accelerated path. Count its
complete bytes alongside each archive; sharing one local index across benchmark
variants does not remove its cost from any variant. Ordinary archive restore is
unchanged and does not require this optional index.

## Research connection and specialization

The [SGTT paper](https://www.vldb.org/pvldb/vol19/p3635-schmidt.pdf) motivates
delaying string reconstruction and operating on stable representations, especially
for equality, joins and grouping. This auxiliary word index follows the broader
principle of avoiding reconstruction, but does not implement that paper's token
table or establish its benefits. Searching a tokenized needle directly in token
IDs is not a generally correct arbitrary-substring algorithm. The
[BtrBlocks paper](https://www.cs.cit.tum.de/fileadmin/w00cfj/dis/papers/btrblocks.pdf)
targets efficient column compression/decompression; it does not imply that our
byte-as-int diagnostic is an efficient representation of logs.

Useful candidate workloads are repeated incident searches for severity/component
words, failure counts by row/time slice, and searches for rare exception-name
fragments. ASCII-only coverage and the ID-only API apply. Trace IDs, timestamps,
arbitrary Unicode/punctuation searches and full log export still need other paths.
Future paper-aligned work needs authorized SGTT assets, then canonical token-key
equality/grouping and late reconstruction, with the same operators on a conventional
compression ablation. No SGTT performance claim is made here.
