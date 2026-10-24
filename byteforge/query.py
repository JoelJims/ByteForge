"""Literal log filters over verified archives; no SQL or SGTT claims."""

import time
from contextlib import ExitStack
from pathlib import Path

from byteforge.dataset import MAX_ROWS
from byteforge.native import NativeIntegers, worker_path
from byteforge.pipelines import _hybrid_rows, load_archive


class QuerySession:
    """Retain decoder handles. Archives must remain immutable during a session.

    An optional all-or-nothing Arrow buffer cache has the same byte budget for
    both backends. Default zero means every query reads/decompresses again.
    """

    def __init__(self, archive: Path, *, worker: Path | None = None, cache_bytes: int = 0):
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("cache_bytes must be a nonnegative integer")
        import pyarrow as pa
        import pyarrow.parquet as pq

        start = time.perf_counter_ns()
        self.archive = archive
        self.manifest = load_archive(archive)
        self.worker = worker or worker_path()
        self.cache_budget = cache_bytes
        self.cache = None
        self.cache_size = 0
        self.closed = False
        self.resources = ExitStack()
        try:
            if self.manifest["pipeline"] == "A":
                self.parquet = self.resources.enter_context(
                    pq.ParquetFile(archive / "data.parquet")
                )
                if not self.parquet.schema_arrow.equals(
                    pa.schema([pa.field("value", pa.string(), nullable=False)])
                ):
                    raise ValueError("Parquet schema mismatch")
                self.native = None
            else:
                self.native = self.resources.enter_context(
                    NativeIntegers(self.worker, expected=self.manifest["native"])
                )
        except Exception:
            self.resources.close()
            raise
        self.open_ns = time.perf_counter_ns() - start

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        self.cache = None
        self.resources.__exit__(*args)

    def _arrays(self, phases):
        import pyarrow as pa

        if self.cache is not None:
            yield from self.cache
            return
        if self.native is None:
            batches = (
                batch.column(0)
                for batch in self.parquet.iter_batches(
                    batch_size=MAX_ROWS, columns=["value"], use_threads=False
                )
            )
        else:
            batches = (
                pa.array(rows, type=pa.string())
                for rows in _hybrid_rows(
                    self.archive, self.manifest, self.worker, phases, native_session=self.native
                )
            )
        retained, size, fits = [], 0, self.cache_budget > 0
        rows = payload = 0
        import pyarrow.compute as pc

        for array in batches:
            if array.null_count:
                raise ValueError("Unexpected null in raw-log archive")
            rows += len(array)
            payload += pc.sum(pc.binary_length(array)).as_py() or 0
            if fits:
                size += array.get_total_buffer_size()
                if size <= self.cache_budget:
                    retained.append(array)
                else:
                    fits = False
                    retained.clear()
            yield array
        if (
            rows != self.manifest["dataset"]["rows"]
            or payload != self.manifest["dataset"]["payload_bytes"]
        ):
            raise ValueError("Query row/payload metadata mismatch")
        if fits:
            self.cache, self.cache_size = retained, size

    def filter(self, operation: str, value: str) -> dict:
        """Return sorted row IDs for exact equality or case-sensitive literal contains."""
        if self.closed:
            raise ValueError("Query session is closed")
        if operation not in ("equals", "contains") or type(value) is not str:
            raise ValueError("Expected equals/contains and a string value")
        value.encode("utf-8", errors="strict")
        import pyarrow.compute as pc

        start = time.perf_counter_ns()
        hit = self.cache is not None
        ids, base = [], 0
        phases = {"detokenization_validation_ns": 0}
        for array in self._arrays(phases):
            mask = (
                pc.equal(array, value)
                if operation == "equals"
                else pc.match_substring(array, value)
            )
            ids.extend(base + index for index in pc.indices_nonzero(mask).to_pylist())
            base += len(array)
        return {
            "operation": operation,
            "value": value,
            "row_ids": ids,
            "count": len(ids),
            "query_ns": time.perf_counter_ns() - start,
            "cache_hit": hit,
            "cache_buffer_bytes": self.cache_size,
            "cache_budget_bytes": self.cache_budget,
            "pipeline": self.manifest["pipeline"],
            "encoder": self.manifest.get("tokenizer", {}).get("identity", "none"),
            "phases": phases,
        }
