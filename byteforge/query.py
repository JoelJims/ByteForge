"""Literal filters over full-column and independently decodable log archives."""

import struct
import time
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path

from byteforge.dataset import MAX_ROWS
from byteforge.native import NativeIntegers, worker_path
from byteforge.pipelines import (
    BLOCK_FORMAT,
    _decode_block_buffers,
    _hybrid_rows,
    _read_vocabulary,
    load_archive,
)
from byteforge.query_index import PreparedTrigramQuery, decode_filter


class QuerySession:
    """Retain archive handles and a bounded decoded-value/block LRU cache.

    Archive files must remain immutable during a session. V2 blocks are checked
    against their indexed checksums when read; simple file mutation is detected
    between calls and invalidates the cache.
    """

    def __init__(self, archive: Path, *, worker: Path | None = None, cache_bytes: int = 16 << 20):
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
        self.block_cache = OrderedDict()
        self.block_cache_size = 0
        self.cache_counters = {"hits": 0, "misses": 0, "evictions": 0}
        self.block_index = self.manifest.get("_validated_block_index")
        self.is_block_archive = self.manifest["format"] == BLOCK_FORMAT
        self.block_filters = (
            [decode_filter(block["trigram_bloom"]) for block in self.block_index["blocks"]]
            if self.is_block_archive
            else []
        )
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
                self.parquet_rowgroups = self.parquet.metadata.num_row_groups
            else:
                if self.is_block_archive:
                    # Queries eliminated by metadata need no native codec process.
                    self.native = None
                    self.vocabulary = _read_vocabulary(archive / "vocabulary.bin")
                    self.block_data = self.resources.enter_context(
                        (archive / "data.bin").open("rb")
                    )
                    self.archive_signature = {
                        name: self._file_signature(archive / name)
                        for name in (
                            "manifest.json",
                            "index.json.zst",
                            "vocabulary.bin",
                            "data.bin",
                        )
                    }
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
        self.block_cache.clear()
        self.block_cache_size = 0
        self.resources.__exit__(*args)

    def _check_immutable(self):
        if any(
            self._file_signature(self.archive / name) != signature
            for name, signature in self.archive_signature.items()
        ):
            self.block_cache.clear()
            self.block_cache_size = 0
            raise RuntimeError("Block archive changed during query session")

    @staticmethod
    def _file_signature(path: Path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

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
                pa.array(rows, type=pa.binary())
                for rows in _hybrid_rows(
                    self.archive,
                    self.manifest,
                    self.worker,
                    phases,
                    native_session=self.native,
                    as_bytes=True,
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

    def filter(
        self,
        operation: str,
        value: str,
        *,
        row_range: tuple[int, int] | None = None,
    ) -> dict:
        """Return ordered original row IDs for equals/contains/prefix.

        Substring and prefix matching are literal, case-sensitive UTF-8 matches.
        ``row_range`` optionally restricts output and block reads to [start, stop).
        """
        if self.closed:
            raise ValueError("Query session is closed")
        if operation not in ("equals", "contains", "prefix") or type(value) is not str:
            raise ValueError("Expected equals/contains/prefix and a string value")
        needle = value.encode("utf-8", errors="strict")
        row_count = self.manifest["dataset"]["rows"]
        if row_range is None:
            row_start, row_stop = 0, row_count
        elif (
            type(row_range) is not tuple
            or len(row_range) != 2
            or any(type(bound) is not int for bound in row_range)
            or not 0 <= row_range[0] <= row_range[1] <= row_count
        ):
            raise ValueError("row_range must be a valid half-open row-ID interval")
        else:
            row_start, row_stop = row_range
        if self.is_block_archive:
            self._check_immutable()
            return self._filter_blocks(operation, value, needle, row_start, row_stop)
        return self._filter_full_column(operation, value, needle, row_start, row_stop)

    def _result(self, operation, value, row_ids, elapsed, row_start, row_stop):
        return {
            "operation": operation,
            "value": value,
            "row_ids": row_ids,
            "count": len(row_ids),
            "query_ns": elapsed,
            "cache_buffer_bytes": self.block_cache_size
            if self.is_block_archive
            else self.cache_size,
            "cache_budget_bytes": self.cache_budget,
            "pipeline": self.manifest["pipeline"],
            "encoder": self.manifest.get("tokenizer", {}).get("identity", "none"),
            "query_representation": "utf8-bytes" if self.manifest["pipeline"] == "B" else "string",
            "row_range": [row_start, row_stop],
            "query_index_bytes": self.manifest["files"].get("index.json.zst", {}).get("bytes", 0),
        }

    def _filter_full_column(self, operation, value, needle, row_start, row_stop):
        import pyarrow.compute as pc

        start = time.perf_counter_ns()
        cache_hit = self.cache is not None
        ids, base = [], 0
        phases = {"detokenization_validation_ns": 0}
        rows_scanned = bytes_searched = compressed_bytes_read = native_bytes_decompressed = 0
        arrays = 0
        for array in self._arrays(phases):
            arrays += 1
            query_value = needle if self.native is not None else value
            if operation == "equals":
                mask = pc.equal(array, query_value)
            elif operation == "prefix":
                mask = pc.starts_with(array, query_value)
            else:
                mask = pc.match_substring(array, query_value)
            ids.extend(
                base + index
                for index in pc.indices_nonzero(mask).to_pylist()
                if row_start <= base + index < row_stop
            )
            rows_scanned += len(array)
            bytes_searched += pc.sum(pc.binary_length(array)).as_py() or 0
            base += len(array)
        if not cache_hit:
            if self.native is None:
                metadata = self.parquet.metadata
                compressed_bytes_read = sum(
                    metadata.row_group(i).column(0).total_compressed_size
                    for i in range(metadata.num_row_groups)
                )
                arrays = metadata.num_row_groups
            else:
                data_name = "data.zst" if "data.zst" in self.manifest["files"] else "data.bin"
                compressed_bytes_read = self.manifest["files"][data_name]["bytes"]
                native_bytes_decompressed = self.manifest.get(
                    "native_data_bytes", compressed_bytes_read
                )
        result = self._result(
            operation, value, ids, time.perf_counter_ns() - start, row_start, row_stop
        )
        result.update(
            cache_hit=cache_hit,
            blocks_total=arrays,
            blocks_examined=arrays,
            blocks_pruned=0,
            blocks_range_skipped=0,
            blocks_decoded=0 if cache_hit else arrays,
            blocks_cached=arrays if cache_hit else 0,
            rows_scanned=rows_scanned,
            bytes_searched=bytes_searched,
            compressed_bytes_read=compressed_bytes_read,
            native_bytes_decompressed=native_bytes_decompressed,
            integer_bytes_decoded=0,
            decoded_payload_bytes=0,
            cache_hits=arrays if cache_hit else 0,
            cache_misses=0 if cache_hit else arrays,
            cache_evictions=0,
            phases=phases,
        )
        return result

    def _block_array(self, block_id: int):
        import pyarrow as pa

        if block_id in self.block_cache:
            array = self.block_cache.pop(block_id)
            self.block_cache[block_id] = array
            self.cache_counters["hits"] += 1
            return array, {}, True
        self.cache_counters["misses"] += 1
        if self.native is None:
            self.native = self.resources.enter_context(
                NativeIntegers(self.worker, expected=self.manifest["native"])
            )
        block = self.block_index["blocks"][block_id]
        payload, offsets, stats = _decode_block_buffers(
            self.block_data, block, self.native, self.vocabulary
        )
        offsets_raw = struct.pack("<" + "i" * len(offsets), *offsets)
        array = pa.Array.from_buffers(
            pa.binary(),
            block["rows"],
            [None, pa.py_buffer(offsets_raw), pa.py_buffer(payload)],
        )
        size = array.get_total_buffer_size()
        if 0 < size <= self.cache_budget:
            while self.block_cache and self.block_cache_size + size > self.cache_budget:
                _, removed = self.block_cache.popitem(last=False)
                self.block_cache_size -= removed.get_total_buffer_size()
                self.cache_counters["evictions"] += 1
            self.block_cache[block_id] = array
            self.block_cache_size += size
        return array, stats, False

    def _filter_blocks(self, operation, value, needle, row_start, row_stop):
        import pyarrow.compute as pc

        start = time.perf_counter_ns()
        before = self.cache_counters.copy()
        blocks = self.block_index["blocks"]
        row_ids = []
        candidates = []
        blocks_range_skipped = blocks_pruned = 0
        universal_match = operation in ("contains", "prefix") and not needle
        empty_equality = operation == "equals" and not needle
        pruning = PreparedTrigramQuery(needle)
        for block_id, block in enumerate(blocks):
            begin, end = block["row_start"], block["row_start"] + block["rows"]
            if begin >= row_stop or end <= row_start:
                blocks_range_skipped += 1
                continue
            if universal_match:
                row_ids.extend(range(max(begin, row_start), min(end, row_stop)))
                continue
            if empty_equality:
                row_ids.extend(
                    begin + offset
                    for offset in block["empty_row_ids"]
                    if row_start <= begin + offset < row_stop
                )
                if not block["empty_row_ids"]:
                    blocks_pruned += 1
                continue
            if len(needle) >= 3 and not pruning.may_contain(*self.block_filters[block_id]):
                blocks_pruned += 1
                continue
            candidates.append((block_id, block))
        counters = {
            "blocks_total": len(blocks),
            "blocks_examined": len(candidates),
            "blocks_pruned": blocks_pruned,
            "blocks_range_skipped": blocks_range_skipped,
            "blocks_decoded": 0,
            "blocks_cached": 0,
            "compressed_bytes_read": 0,
            "native_bytes_decompressed": 0,
            "integer_bytes_decoded": 0,
            "decoded_payload_bytes": 0,
            "rows_scanned": 0,
            "bytes_searched": 0,
        }
        for block_id, block in candidates:
            array, decoded, hit = self._block_array(block_id)
            counters["blocks_cached" if hit else "blocks_decoded"] += 1
            for key in (
                "compressed_bytes_read",
                "native_bytes_decompressed",
                "integer_bytes_decoded",
                "decoded_payload_bytes",
            ):
                counters[key] += decoded.get(key, 0)
            counters["rows_scanned"] += block["rows"]
            counters["bytes_searched"] += block["payload_bytes"]
            if operation == "equals":
                mask = pc.equal(array, needle)
            elif operation == "prefix":
                mask = pc.starts_with(array, needle)
            else:
                mask = pc.match_substring(array, needle)
            row_ids.extend(
                block["row_start"] + index
                for index in pc.indices_nonzero(mask).to_pylist()
                if row_start <= block["row_start"] + index < row_stop
            )
        row_ids.sort()
        result = self._result(
            operation,
            value,
            row_ids,
            time.perf_counter_ns() - start,
            row_start,
            row_stop,
        )
        result.update(counters)
        result.update(
            cache_hit=bool(counters["blocks_cached"]),
            cache_buffer_bytes=self.block_cache_size,
            cache_hits=self.cache_counters["hits"] - before["hits"],
            cache_misses=self.cache_counters["misses"] - before["misses"],
            cache_evictions=self.cache_counters["evictions"] - before["evictions"],
        )
        return result
