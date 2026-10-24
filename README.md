# ByteForge

ByteForge investigates tokenizer-derived token IDs combined with BtrBlocks for
string compression and analytical workloads. It compares Parquet+Zstd, token
IDs+BtrBlocks, native BtrBlocks strings, and token IDs+Zstd without presuming a
hybrid improvement.

The [raw HDFS benchmark](docs/hdfs_benchmark.md) implements a streaming lossless
loader, standalone Parquet+Zstd archives, native BtrBlocks integer archives,
fresh-process reconstruction, and a reproducible measurement runner. Its synthetic
development path uses an explicitly test-only reversible encoder. The
[actual HDFS_2k sample](docs/hdfs_2k_results.md) now passes both the Parquet path
and test-only BtrBlocks infrastructure validation with exact reconstruction.
Production SGTT
integration remains blocked by code/table reuse rights. The full official dataset
has not been downloaded or benchmarked; no performance improvement is claimed.

See [qualification setup and commands](docs/qualification/README.md),
[implementation findings](docs/implementation_log.md), and the proposed
[research design](research/ByteForge_Research_and_System_Design.md).
