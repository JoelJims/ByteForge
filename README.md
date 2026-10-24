# ByteForge

ByteForge investigates tokenizer-derived token IDs combined with BtrBlocks for
string compression and analytical workloads. It compares Parquet+Zstd, token
IDs+BtrBlocks, native BtrBlocks strings, and token IDs+Zstd without presuming a
hybrid improvement.

Implementation is at the dependency-qualification stage. Python representation,
Parquet/Zstd and native BtrBlocks compatibility probes are available. The native
probe preserves failures and offers an explicitly named corrected FSST variant.
SGTT reuse rights remain unresolved; no LLM tokenizer or full benchmark pipeline
is implemented. No performance improvement has been measured.

See [qualification setup and commands](docs/qualification/README.md),
[implementation findings](docs/implementation_log.md), and the proposed
[research design](research/ByteForge_Research_and_System_Design.md).
