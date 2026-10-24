"""Optional exact ASCII-letter substring postings; independent of the encoder.

Every maximal [A-Za-z]+ run is indexed, with case preserved. A nonempty ASCII
letter-only needle occurs in a row iff it occurs in at least one of these runs.
This is an auxiliary search index, not SGTT and not a replacement storage codec.
"""

import re
import sqlite3
import sys
import time
from array import array
from contextlib import ExitStack
from pathlib import Path

from byteforge.dataset import checksum, dump_json, row_windows
from byteforge.pipelines import load_archive, prepared_manifest, read_json

FORMAT = "byteforge-ascii-word-postings-v1"
WORDS = re.compile(r"[A-Za-z]+")
PAGE_ROWS = 65536


def build_word_index(prepared: Path, output: Path) -> dict:
    """Build immutable, bounded-window postings from verified canonical input."""
    import zstandard as zstd

    started = time.perf_counter_ns()
    cpu = time.process_time_ns()
    dataset = prepared_manifest(prepared)
    output.mkdir(parents=True, exist_ok=False)
    database = output / "postings.sqlite"
    compressor = zstd.ZstdCompressor(level=1, write_checksum=True)
    db = sqlite3.connect(database)
    base = page_count = postings = 0
    try:
        db.execute("CREATE TABLE terms (id INTEGER PRIMARY KEY, word TEXT UNIQUE NOT NULL)")
        db.execute(
            "CREATE TABLE pages (term INTEGER, base INTEGER, rows INTEGER, count INTEGER, "
            "data BLOB, PRIMARY KEY(term, base)) WITHOUT ROWID"
        )
        term_ids = {}
        for rows in row_windows(prepared / "rows.bin", max_rows=PAGE_ROWS):
            groups = {}
            for local_id, row in enumerate(rows):
                for word in set(WORDS.findall(row)):
                    if word not in groups:
                        groups[word] = array("H")
                    groups[word].append(local_id)
            batch = []
            for word, ids in sorted(groups.items()):
                term = term_ids.get(word)
                if term is None:
                    term = len(term_ids)
                    term_ids[word] = term
                    db.execute("INSERT INTO terms VALUES (?, ?)", (term, word))
                # Delta uint16 keeps repeated/sequential postings compact.
                for i in range(len(ids) - 1, 0, -1):
                    ids[i] -= ids[i - 1]
                if sys.byteorder != "little":
                    ids.byteswap()
                data = compressor.compress(ids.tobytes())
                batch.append((term, base, len(rows), len(ids), data))
                postings += len(ids)
                page_count += 1
            db.executemany("INSERT INTO pages VALUES (?, ?, ?, ?, ?)", batch)
            db.commit()
            base += len(rows)
        if base != dataset["rows"]:
            raise ValueError("Word index row count mismatch")
    finally:
        db.close()
    manifest = {
        "format": FORMAT,
        "dataset": dataset,
        "semantics": "all maximal ASCII letter runs; case-sensitive substring; ordered row IDs",
        "page_rows": PAGE_ROWS,
        "terms": len(term_ids),
        "pages": page_count,
        "postings": postings,
        "codec": "delta-uint16le+zstd-1",
        "sqlite_version": sqlite3.sqlite_version,
        "zstandard_version": zstd.__version__,
        "files": {
            "postings.sqlite": {"bytes": database.stat().st_size, "sha256": checksum(database)}
        },
        "build_ns": time.perf_counter_ns() - started,
        "build_cpu_ns": time.process_time_ns() - cpu,
    }
    dump_json(output / "manifest.json", manifest)
    return manifest


class IndexedQuerySession:
    """Exact covered queries without opening the payload decoder; lazy fallback.

    The index is fully checksummed at every session open. It must match the
    canonical dataset identity. No results/decoded postings are cached. Both
    index and archive must remain immutable throughout a session.
    """

    def __init__(self, archive: Path, word_index: Path, *, worker=None, cache_bytes=16 << 20):
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("cache_bytes must be a nonnegative integer")
        started = time.perf_counter_ns()
        self.archive, self.index_path = archive, word_index
        self.worker, self.cache_budget = worker, cache_bytes
        self.manifest = load_archive(archive, metadata_only=True)
        index = read_json(word_index / "manifest.json")
        if index.get("format") != FORMAT or index.get("page_rows") != PAGE_ROWS:
            raise ValueError("Unsupported word index format")
        if index.get("dataset") != self.manifest["dataset"]:
            raise ValueError("Word index/dataset mismatch")
        if index.get("codec") != "delta-uint16le+zstd-1":
            raise ValueError("Unsupported postings codec")
        path = word_index / "postings.sqlite"
        if (
            set(index.get("files", {})) != {"postings.sqlite"}
            or {p.name for p in word_index.iterdir()} != {"postings.sqlite", "manifest.json"}
            or path.is_symlink()
            or path.stat().st_size != index["files"]["postings.sqlite"]["bytes"]
            or checksum(path) != index["files"]["postings.sqlite"]["sha256"]
        ):
            raise ValueError("Word index checksum/size mismatch")
        self.index_bytes = path.stat().st_size + (word_index / "manifest.json").stat().st_size
        self.resources = ExitStack()
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        self.resources.callback(self.db.close)
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("PRAGMA cache_size=-2048")
        self.signatures = {
            p: self._signature(p) for folder in (archive, word_index) for p in folder.iterdir()
        }
        self.fallback = None
        self.native = None
        self.closed = False
        self.open_ns = time.perf_counter_ns() - started

    @staticmethod
    def _signature(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        self.resources.__exit__(*args)

    def filter(self, operation, value, *, row_range=None):
        started = time.perf_counter_ns()
        if self.closed:
            raise ValueError("Query session is closed")
        if operation not in ("contains", "equals", "prefix") or type(value) is not str:
            raise ValueError("Expected equals/contains/prefix and a string value")
        value.encode("utf-8", errors="strict")
        rows = self.manifest["dataset"]["rows"]
        if row_range is not None and (
            type(row_range) is not tuple
            or len(row_range) != 2
            or any(type(x) is not int for x in row_range)
            or not 0 <= row_range[0] <= row_range[1] <= rows
        ):
            raise ValueError("row_range must be a valid half-open row-ID interval")
        start, stop = row_range or (0, rows)
        if any(self._signature(p) != signature for p, signature in self.signatures.items()):
            self.__exit__(None, None, None)
            raise RuntimeError("Archive or word index changed during query session")
        if operation != "contains" or WORDS.fullmatch(value) is None:
            from byteforge.query import QuerySession

            if self.fallback is None:
                self.fallback = self.resources.enter_context(
                    QuerySession(self.archive, worker=self.worker, cache_bytes=self.cache_budget)
                )
            result = self.fallback.filter(operation, value, row_range=row_range)
            self.native = self.fallback.native
            result.update(
                query_path="payload-fallback",
                word_index_bytes=self.index_bytes,
                query_ns=time.perf_counter_ns() - started,
            )
            return result
        import zstandard as zstd

        decompressor = zstd.ZstdDecompressor()
        ids = set()
        pages = compressed_bytes = posting_bytes = terms = 0
        # Search the dictionary, not the log rows. instr is literal and case-sensitive.
        for (term,) in self.db.execute("SELECT id FROM terms WHERE instr(word, ?) > 0", (value,)):
            terms += 1
            for base, count_rows, count, data in self.db.execute(
                "SELECT base, rows, count, data FROM pages WHERE term=? "
                "AND base < ? AND base + rows > ? ORDER BY base",
                (term, stop, start),
            ):
                if (
                    type(base) is not int
                    or type(count_rows) is not int
                    or type(count) is not int
                    or not 0 <= base < rows
                    or not 1 <= count <= count_rows <= PAGE_ROWS
                    or base + count_rows > rows
                    or len(data) > (2 * PAGE_ROWS + 1024)
                ):
                    raise ValueError("Invalid posting page bounds")
                if zstd.frame_content_size(data) != count * 2:
                    raise ValueError("Invalid posting frame length")
                raw = decompressor.decompress(data, max_output_size=count * 2)
                if len(raw) != count * 2:
                    raise ValueError("Invalid posting length")
                offsets = array("H")
                offsets.frombytes(raw)
                if sys.byteorder != "little":
                    offsets.byteswap()
                local = 0
                for pos, delta in enumerate(offsets):
                    local += delta
                    if (pos and not delta) or local >= count_rows:
                        raise ValueError("Invalid posting row ID")
                    if start <= base + local < stop:
                        ids.add(base + local)
                pages += 1
                compressed_bytes += len(data)
                posting_bytes += len(raw)
        ordered = sorted(ids)
        return {
            "operation": operation,
            "value": value,
            "row_ids": ordered,
            "count": len(ordered),
            "query_ns": time.perf_counter_ns() - started,
            "query_path": "exact-word-postings",
            "pipeline": self.manifest["pipeline"],
            "encoder": self.manifest.get("tokenizer", {}).get("identity", "none"),
            "row_range": [start, stop],
            "word_index_bytes": self.index_bytes,
            "posting_pages_read": pages,
            "dictionary_terms_matched": terms,
            "posting_compressed_bytes_read": compressed_bytes,
            "posting_decoded_bytes": posting_bytes,
            "blocks_decoded": 0,
            "rows_scanned": 0,
            "decoded_payload_bytes": 0,
            "compressed_bytes_read": 0,
            "cache_budget_bytes": self.cache_budget,
            "cache_buffer_bytes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
