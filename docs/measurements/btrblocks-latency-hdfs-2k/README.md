# BtrBlocks latency follow-up measurements

Dataset: repository's verified `data/hdfs/HDFS_2k.log`; all JSON records contain
the input identity, configuration, runtime metadata and per-trial outcomes.
`zstd-final/` is the new protocol-3 Release run with BtrBlocks payload wrapped in
Zstandard level 1. `raw-control/` uses the same worker and input without outer
Zstandard. Each run has two warmups and ten measured trials for A and diagnostic
B; B explicitly uses `test-byte`, not SGTT. `query-zstd/` contains the 288 query
observations against Parquet and B-zstd, including warmups, repeated cache/no-cache
requests and Python reference checks.

`worker-info.json` records BtrBlocks revision, compiler, protocol and sanitizer
status. The worker executable itself is not included. All trial failures and
warmups are retained in `observations.jsonl`; no failed trials were reported.
These are exploratory measurements of the 2k sample, not the full HDFS corpus or
the paper's SGTT method.
