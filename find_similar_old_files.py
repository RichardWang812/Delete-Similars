#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import hashlib
import heapq
import json
import os
import platform
import re
import sqlite3
import struct
import subprocess
import sys
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def iter_tokens(text: str) -> Iterator[str]:
    for match in TOKEN_RE.finditer(text.lower()):
        token = match.group(0)
        if token:
            yield token


class TokenHasher:
    def __init__(self, max_cache: int = 200_000) -> None:
        self._cache: dict[str, int] = {}
        self._max_cache = max_cache

    def hash64(self, token: str) -> int:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big", signed=False)
        if len(self._cache) < self._max_cache:
            self._cache[token] = value
        return value


FNV_OFFSET_BASIS_64 = 1469598103934665603
FNV_PRIME_64 = 1099511628211


def fnv1a_64(values: Sequence[int]) -> int:
    h = FNV_OFFSET_BASIS_64
    for v in values:
        h ^= v & 0xFFFFFFFFFFFFFFFF
        h = (h * FNV_PRIME_64) & 0xFFFFFFFFFFFFFFFF
    return h


class BottomKSketch:
    def __init__(self, k: int) -> None:
        self._k = k
        self._heap: list[int] = []
        self._in_heap: set[int] = set()

    def add(self, value: int) -> None:
        if value in self._in_heap:
            return
        if len(self._heap) < self._k:
            heapq.heappush(self._heap, -value)
            self._in_heap.add(value)
            return

        current_max = -self._heap[0]
        if value >= current_max:
            return

        removed_neg = heapq.heapreplace(self._heap, -value)
        removed = -removed_neg
        self._in_heap.remove(removed)
        self._in_heap.add(value)

    def values(self) -> tuple[int, ...]:
        return tuple(sorted(self._in_heap))


def hamming_distance_64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def sketch_similarity(a: Sequence[int], b: Sequence[int]) -> float:
    if not a or not b:
        return 0.0
    i = 0
    j = 0
    inter = 0
    while i < len(a) and j < len(b):
        av = a[i]
        bv = b[j]
        if av == bv:
            inter += 1
            i += 1
            j += 1
        elif av < bv:
            i += 1
        else:
            j += 1
    return inter / min(len(a), len(b))


def safe_local_datetime(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone()


def fmt_mtime(ts: float) -> str:
    return safe_local_datetime(ts).strftime("%Y-%m-%d %H:%M:%S %z")


def read_text_file(path: Path, max_bytes: int) -> str:
    with path.open("rb") as f:
        raw = f.read(max_bytes)
    return raw.decode("utf-8", errors="ignore")


def extract_docx_text(path: Path, max_chars: int) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                xml_bytes = zf.read("word/document.xml")
            except KeyError:
                return ""
    except zipfile.BadZipFile:
        return ""

    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_bytes)
    except Exception:
        return ""

    chunks: list[str] = []
    for elem in root.iter():
        if not elem.tag.endswith("}t"):
            continue
        if elem.text:
            chunks.append(elem.text)
        if len(chunks) >= 200_000:
            break

    text = " ".join(chunks)
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def extract_xlsx_text(path: Path, max_chars: int) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in names:
                try:
                    xml_bytes = zf.read("xl/sharedStrings.xml")
                    import xml.etree.ElementTree as ET

                    root = ET.fromstring(xml_bytes)
                    for si in root.iter():
                        if not si.tag.endswith("}si"):
                            continue
                        parts: list[str] = []
                        for t in si.iter():
                            if t.tag.endswith("}t") and t.text:
                                parts.append(t.text)
                        if parts:
                            shared_strings.append("".join(parts))
                        if len(shared_strings) >= 500_000:
                            break
                except Exception:
                    shared_strings = []

            sheet_files = sorted(
                n for n in names if n.startswith("xl/worksheets/sheet") and n.lower().endswith(".xml")
            )
            if not sheet_files:
                return ""

            import xml.etree.ElementTree as ET

            chunks: list[str] = []
            total_chars = 0
            for sheet_name in sheet_files:
                try:
                    xml_bytes = zf.read(sheet_name)
                except Exception:
                    continue
                try:
                    root = ET.fromstring(xml_bytes)
                except Exception:
                    continue
                for cell in root.iter():
                    if not cell.tag.endswith("}c"):
                        continue
                    cell_type = cell.get("t") or ""
                    value = ""
                    if cell_type == "inlineStr":
                        for child in cell.iter():
                            if child.tag.endswith("}t") and child.text:
                                value = child.text
                                break
                    else:
                        v_elem = None
                        for child in cell:
                            if child.tag.endswith("}v"):
                                v_elem = child
                                break
                        if v_elem is None or not v_elem.text:
                            continue
                        raw = v_elem.text.strip()
                        if cell_type == "s":
                            try:
                                idx = int(raw)
                            except ValueError:
                                continue
                            if 0 <= idx < len(shared_strings):
                                value = shared_strings[idx]
                        else:
                            value = raw

                    if not value:
                        continue
                    chunks.append(value)
                    total_chars += len(value) + 1
                    if total_chars >= max_chars:
                        return " ".join(chunks)[:max_chars]
            return " ".join(chunks)[:max_chars]
    except zipfile.BadZipFile:
        return ""


def extract_pdf_text(path: Path, max_chars: int) -> str:
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            from PyPDF2 import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if not text:
                continue
            parts.append(text)
            total += len(text) + 1
            if total >= max_chars:
                break
        return "\n".join(parts)[:max_chars]
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return (proc.stdout or "")[:max_chars]
    except Exception:
        return ""


def extract_text(path: Path, max_bytes: int, max_chars: int) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext == "docx":
        return extract_docx_text(path, max_chars=max_chars)
    if ext in {"xlsx", "xlsm"}:
        return extract_xlsx_text(path, max_chars=max_chars)
    if ext == "pdf":
        return extract_pdf_text(path, max_chars=max_chars)
    return read_text_file(path, max_bytes=max_bytes)


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    mtime: float
    size: int
    ext: str
    token_count: int
    simhash: int
    sketch: tuple[int, ...]


@dataclass(frozen=True)
class ScanStats:
    scanned: int
    processed: int
    skipped_size: int
    skipped_tokens: int
    errors: int


@dataclass(frozen=True)
class DeletionCandidate:
    path: str
    mtime: float
    size: int
    similarity_to_keep: float


@dataclass(frozen=True)
class SimilarGroup:
    keep_path: str
    keep_mtime: float
    keep_size: int
    candidates: tuple[DeletionCandidate, ...]


def fingerprint_text(
    text: str,
    *,
    token_hasher: TokenHasher,
    shingle_size: int,
    sketch_size: int,
    max_tokens: int,
) -> tuple[int, int, tuple[int, ...]]:
    ones = [0] * 64
    total_tokens = 0
    window: collections.deque[int] = collections.deque(maxlen=shingle_size)
    sketch = BottomKSketch(sketch_size)

    for token in iter_tokens(text):
        total_tokens += 1
        token_hash = token_hasher.hash64(token)

        x = token_hash
        while x:
            lsb = x & -x
            bit = lsb.bit_length() - 1
            ones[bit] += 1
            x ^= lsb

        window.append(token_hash)
        if len(window) == shingle_size:
            shingle_hash = fnv1a_64(tuple(window))
            sketch.add(shingle_hash)

        if total_tokens >= max_tokens:
            break

    simhash = 0
    if total_tokens > 0:
        for bit, count in enumerate(ones):
            if count * 2 >= total_tokens:
                simhash |= 1 << bit

    return total_tokens, simhash & 0xFFFFFFFFFFFFFFFF, sketch.values()


def _u64_to_i64(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    if v >= (1 << 63):
        return v - (1 << 64)
    return v


def _i64_to_u64(v: int) -> int:
    if v < 0:
        return (v + (1 << 64)) & 0xFFFFFFFFFFFFFFFF
    return v & 0xFFFFFFFFFFFFFFFF


class FingerprintCache:
    def __init__(self, db_path: Path, *, k: int, shingle_size: int, max_tokens: int) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_fingerprints(
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                ext TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                simhash INTEGER NOT NULL,
                sketch BLOB NOT NULL
            )
            """
        )
        self._conn.commit()

        expected = {
            "version": "1",
            "k": str(k),
            "shingle_size": str(shingle_size),
            "max_tokens": str(max_tokens),
        }
        rows = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
        if rows and any(rows.get(k2) != v2 for k2, v2 in expected.items()):
            self._conn.execute("DELETE FROM file_fingerprints")
            self._conn.execute("DELETE FROM meta")
            self._conn.commit()
            rows = {}

        if not rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                list(expected.items()),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, path: str, *, mtime: float, size: int) -> FileFingerprint | None:
        row = self._conn.execute(
            """
            SELECT path, mtime, size, ext, token_count, simhash, sketch
            FROM file_fingerprints
            WHERE path = ? AND mtime = ? AND size = ?
            """,
            (path, mtime, size),
        ).fetchone()
        if not row:
            return None
        sketch_blob = row[6]
        sketch = tuple(struct.unpack(f">{len(sketch_blob) // 8}Q", sketch_blob))
        return FileFingerprint(
            path=row[0],
            mtime=row[1],
            size=row[2],
            ext=row[3],
            token_count=row[4],
            simhash=_i64_to_u64(row[5]),
            sketch=sketch,
        )

    def put(self, fp: FileFingerprint) -> None:
        sketch_blob = struct.pack(f">{len(fp.sketch)}Q", *fp.sketch)
        simhash_i64 = _u64_to_i64(fp.simhash)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO file_fingerprints(path, mtime, size, ext, token_count, simhash, sketch)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (fp.path, fp.mtime, fp.size, fp.ext, fp.token_count, simhash_i64, sketch_blob),
        )

    def commit(self) -> None:
        self._conn.commit()


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def iter_candidate_files(
    roots: Sequence[Path],
    *,
    exts: set[str],
    exclude_dirs: Sequence[str],
    exclude_files: Sequence[str],
    follow_symlinks: bool,
) -> Iterator[Path]:
    def is_excluded_dir(name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in exclude_dirs)

    def is_excluded_file(name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in exclude_files)

    def onerror(err: OSError) -> None:
        print(f"[skip] {err}", file=sys.stderr)

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks, onerror=onerror):
            dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
            for filename in filenames:
                if is_excluded_file(filename):
                    continue
                path = Path(dirpath) / filename
                if path.suffix.lower().lstrip(".") not in exts:
                    continue
                yield path


def trash_file_macos(path: str) -> None:
    escaped = path.replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{escaped}"'
    subprocess.run(["osascript", "-e", script], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def delete_path(path: Path, *, action: str) -> None:
    if action == "print":
        print(f"[DRY-RUN] would delete: {path}")
        return
    if action == "delete":
        path.unlink()
        return
    if action == "trash":
        try:
            from send2trash import send2trash  # type: ignore

            send2trash(str(path))
            return
        except Exception:
            pass
        if platform.system() == "Darwin":
            trash_file_macos(str(path))
            return
        raise RuntimeError("trash mode requires 'send2trash' on this platform")
    raise ValueError(f"unknown action: {action}")


def build_buckets(simhashes: Sequence[int], *, bands: int) -> dict[tuple[int, int], list[int]]:
    if 64 % bands != 0:
        raise ValueError("bands must divide 64")
    bits_per_band = 64 // bands
    mask = (1 << bits_per_band) - 1

    buckets: dict[tuple[int, int], list[int]] = {}
    for idx, sh in enumerate(simhashes):
        for band in range(bands):
            key = (band, (sh >> (band * bits_per_band)) & mask)
            buckets.setdefault(key, []).append(idx)
    return buckets


def suggest_similar_groups(
    files: Sequence[FileFingerprint],
    *,
    max_hamming: int,
    min_similarity: float,
    bands: int,
    min_sketch_len: int,
    cross_ext: bool,
    max_token_diff_ratio: float,
) -> list[list[int]]:
    simhashes = [f.simhash for f in files]
    buckets = build_buckets(simhashes, bands=bands)

    uf = UnionFind(len(files))
    verified_pairs: set[tuple[int, int]] = set()

    for idxs in buckets.values():
        if len(idxs) < 2:
            continue

        idxs_sorted = sorted(idxs)
        for i_pos, i in enumerate(idxs_sorted):
            fi = files[i]
            for j in idxs_sorted[i_pos + 1 :]:
                fj = files[j]
                if not cross_ext and fi.ext != fj.ext:
                    continue
                if fi.token_count == 0 or fj.token_count == 0:
                    continue
                if len(fi.sketch) < min_sketch_len or len(fj.sketch) < min_sketch_len:
                    continue

                token_hi = max(fi.token_count, fj.token_count)
                token_lo = min(fi.token_count, fj.token_count)
                if token_lo / token_hi < (1.0 - max_token_diff_ratio):
                    continue

                if hamming_distance_64(fi.simhash, fj.simhash) > max_hamming:
                    continue
                key = (min(i, j), max(i, j))
                if key in verified_pairs:
                    continue
                sim = sketch_similarity(fi.sketch, fj.sketch)
                if sim < min_similarity:
                    continue
                uf.union(i, j)
                verified_pairs.add(key)

    groups: dict[int, list[int]] = {}
    for idx in range(len(files)):
        root = uf.find(idx)
        groups.setdefault(root, []).append(idx)

    clusters = [sorted(idxs) for idxs in groups.values() if len(idxs) >= 2]
    clusters.sort(key=len, reverse=True)
    return clusters


def scan_fingerprints(
    roots: Sequence[Path],
    *,
    exts: set[str],
    exclude_dirs: Sequence[str],
    exclude_files: Sequence[str],
    follow_symlinks: bool,
    max_bytes: int,
    read_bytes: int,
    docx_max_chars: int,
    min_tokens: int,
    max_tokens: int,
    shingle_size: int,
    sketch_size: int,
    cache_path: str,
    progress_every: int = 200,
    progress_cb=None,
) -> tuple[list[FileFingerprint], ScanStats]:
    cache: FingerprintCache | None = None
    if cache_path.strip():
        try:
            cache = FingerprintCache(
                Path(cache_path),
                k=sketch_size,
                shingle_size=shingle_size,
                max_tokens=max_tokens,
            )
        except Exception as e:
            print(f"[warn] cache disabled: {e}", file=sys.stderr)
            cache = None

    token_hasher = TokenHasher()
    fingerprints: list[FileFingerprint] = []
    scanned = 0
    processed = 0
    skipped_size = 0
    skipped_tokens = 0
    errors = 0

    try:
        for path in iter_candidate_files(
            roots,
            exts=exts,
            exclude_dirs=exclude_dirs,
            exclude_files=exclude_files,
            follow_symlinks=follow_symlinks,
        ):
            scanned += 1
            try:
                st = path.stat()
            except OSError:
                errors += 1
                continue

            if st.st_size > max_bytes:
                skipped_size += 1
                continue

            fp = None
            if cache is not None:
                fp = cache.get(str(path), mtime=st.st_mtime, size=st.st_size)

            if fp is None:
                try:
                    text = extract_text(path, max_bytes=read_bytes, max_chars=docx_max_chars)
                    token_count, simhash, sketch = fingerprint_text(
                        text,
                        token_hasher=token_hasher,
                        shingle_size=shingle_size,
                        sketch_size=sketch_size,
                        max_tokens=max_tokens,
                    )
                except Exception:
                    errors += 1
                    continue

                if token_count < min_tokens:
                    skipped_tokens += 1
                    continue

                fp = FileFingerprint(
                    path=str(path),
                    mtime=st.st_mtime,
                    size=st.st_size,
                    ext=path.suffix.lower().lstrip("."),
                    token_count=token_count,
                    simhash=simhash,
                    sketch=sketch,
                )
                if cache is not None:
                    cache.put(fp)

            fingerprints.append(fp)
            processed += 1
            if cache is not None and processed % progress_every == 0:
                cache.commit()

            if processed % progress_every == 0:
                stats = ScanStats(
                    scanned=scanned,
                    processed=processed,
                    skipped_size=skipped_size,
                    skipped_tokens=skipped_tokens,
                    errors=errors,
                )
                if progress_cb is not None:
                    try:
                        progress_cb(stats)
                    except Exception:
                        pass
                else:
                    print(
                        f"[progress] scanned={scanned} processed={processed} skipped_size={skipped_size} skipped_tokens={skipped_tokens} errors={errors}"
                    )
    finally:
        if cache is not None:
            try:
                cache.commit()
                cache.close()
            except Exception:
                pass

    return (
        fingerprints,
        ScanStats(
            scanned=scanned,
            processed=processed,
            skipped_size=skipped_size,
            skipped_tokens=skipped_tokens,
            errors=errors,
        ),
    )


def build_deletion_groups(
    fingerprints: Sequence[FileFingerprint],
    clusters: Sequence[Sequence[int]],
    *,
    cutoff_ts: float,
) -> list[SimilarGroup]:
    groups: list[SimilarGroup] = []
    for indices in clusters:
        cluster_files = [fingerprints[i] for i in indices]
        cluster_files.sort(key=lambda f: f.mtime, reverse=True)
        keep = cluster_files[0]
        candidates = []
        for f in cluster_files[1:]:
            if f.mtime >= cutoff_ts:
                continue
            candidates.append(
                DeletionCandidate(
                    path=f.path,
                    mtime=f.mtime,
                    size=f.size,
                    similarity_to_keep=sketch_similarity(keep.sketch, f.sketch),
                )
            )
        if not candidates:
            continue
        groups.append(
            SimilarGroup(
                keep_path=keep.path,
                keep_mtime=keep.mtime,
                keep_size=keep.size,
                candidates=tuple(candidates),
            )
        )
    return groups


def load_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Search for similar documents and optionally delete old versions (interactive).",
        epilog=textwrap.dedent(
            """
            Notes
            - Similarity is estimated via token shingles + bottom-k sketch, with SimHash used as a fast pre-filter.
            - By default this is a dry-run (won't delete). Use --action trash or --action delete.
            """
        ).strip(),
    )
    p.add_argument(
        "paths",
        nargs="*",
        default=[str(Path.home())],
        help="One or more root paths to scan (default: your home directory).",
    )
    p.add_argument("--age-years", type=float, default=2.0, help="Old-file threshold in years (default: 2).")
    p.add_argument(
        "--ext",
        action="append",
        default=[],
        help="File extension to include (repeatable). Default: docx,xlsx,pdf,txt,md.",
    )
    p.add_argument("--exclude-dir", action="append", default=[], help="Exclude directory name glob (repeatable).")
    p.add_argument("--exclude-file", action="append", default=[], help="Exclude file name glob (repeatable).")
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks while walking directories.")
    p.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024, help="Skip files larger than this.")
    p.add_argument("--read-bytes", type=int, default=2 * 1024 * 1024, help="Max bytes to read for text files.")
    p.add_argument("--docx-max-chars", type=int, default=2_000_000, help="Max characters to extract from DOCX.")
    p.add_argument("--min-tokens", type=int, default=80, help="Skip documents with fewer tokens.")
    p.add_argument("--max-tokens", type=int, default=200_000, help="Stop tokenizing after this many tokens.")
    p.add_argument("--shingle-size", type=int, default=3, help="Token shingle size (default: 3).")
    p.add_argument("--sketch-size", type=int, default=128, help="Bottom-k sketch size (default: 128).")
    p.add_argument("--bands", type=int, default=8, help="SimHash bands for candidate generation (default: 8).")
    p.add_argument("--max-hamming", type=int, default=10, help="Max SimHash Hamming distance (default: 10).")
    p.add_argument("--min-similarity", type=float, default=0.82, help="Min sketch similarity (default: 0.82).")
    p.add_argument(
        "--max-token-diff-ratio",
        type=float,
        default=0.5,
        help="Skip comparing documents whose token counts differ too much (default: 0.5).",
    )
    p.add_argument(
        "--cross-ext",
        action="store_true",
        help="Allow comparing documents with different extensions (default: only compare same extension).",
    )
    p.add_argument(
        "--action",
        choices=["print", "trash", "delete"],
        default="print",
        help="What to do after confirmation: print (dry-run), trash, or delete (permanent).",
    )
    p.add_argument("--yes", action="store_true", help="Do not prompt; apply action automatically.")
    p.add_argument("--cache", default=".autodelete_cache.sqlite3", help="SQLite cache path (set empty to disable).")
    p.add_argument("--report", default="", help="Write a JSON report to this path.")
    return p.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    args = load_args(argv)
    roots = [Path(p).expanduser().resolve() for p in args.paths]
    exts = {e.lower().lstrip(".") for e in (args.ext or [])}
    if not exts:
        exts = {"docx", "xlsx", "xlsm", "pdf", "txt", "md"}

    exclude_dirs = args.exclude_dir or []
    if not exclude_dirs:
        exclude_dirs = [".git", "node_modules", ".Trash", "__pycache__", ".venv"]

    exclude_files = args.exclude_file or []

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    cutoff_seconds = args.age_years * 365.25 * 24 * 3600
    cutoff_ts = now - cutoff_seconds

    fingerprints, stats = scan_fingerprints(
        roots,
        exts=exts,
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
        follow_symlinks=args.follow_symlinks,
        max_bytes=args.max_bytes,
        read_bytes=args.read_bytes,
        docx_max_chars=args.docx_max_chars,
        min_tokens=args.min_tokens,
        max_tokens=args.max_tokens,
        shingle_size=args.shingle_size,
        sketch_size=args.sketch_size,
        cache_path=str(args.cache),
    )

    if not fingerprints:
        print("No candidate files found.")
        return 0

    min_sketch_len = min(20, args.sketch_size)
    clusters = suggest_similar_groups(
        fingerprints,
        max_hamming=args.max_hamming,
        min_similarity=args.min_similarity,
        bands=args.bands,
        min_sketch_len=min_sketch_len,
        cross_ext=args.cross_ext,
        max_token_diff_ratio=args.max_token_diff_ratio,
    )

    report_data: dict[str, object] = {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "roots": [str(r) for r in roots],
        "cutoff_ts": cutoff_ts,
        "cutoff_local": fmt_mtime(cutoff_ts),
        "params": {
            "age_years": args.age_years,
            "exts": sorted(exts),
            "min_tokens": args.min_tokens,
            "max_tokens": args.max_tokens,
            "shingle_size": args.shingle_size,
            "sketch_size": args.sketch_size,
            "bands": args.bands,
            "max_hamming": args.max_hamming,
            "min_similarity": args.min_similarity,
        },
        "clusters": [],
        "stats": {
            "scanned": stats.scanned,
            "processed": stats.processed,
            "skipped_size": stats.skipped_size,
            "skipped_tokens": stats.skipped_tokens,
            "errors": stats.errors,
        },
    }

    if not clusters:
        print("No similar groups found.")
        if args.report:
            Path(args.report).write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    groups = build_deletion_groups(fingerprints, clusters, cutoff_ts=cutoff_ts)
    print(f"Cutoff (older than): {fmt_mtime(cutoff_ts)}")

    suggested_deletes_total = 0
    deleted_total = 0

    for group_idx, group in enumerate(groups, start=1):
        suggested_deletes_total += len(group.candidates)

        print("")
        print(f"=== Group {group_idx} ({1 + len(group.candidates)} files) ===")
        print(f"Keep (newest): {group.keep_path}")
        print(f"  mtime: {fmt_mtime(group.keep_mtime)}")
        print(f"  size:  {group.keep_size} bytes")
        print("Candidates to delete (older than cutoff):")
        for c in group.candidates:
            print(f"- {c.path}")
            print(f"  mtime: {fmt_mtime(c.mtime)}")
            print(f"  size:  {c.size} bytes")
            print(f"  similarity_to_keep: {c.similarity_to_keep:.3f}")

        report_data["clusters"].append(
            {
                "keep": {
                    "path": group.keep_path,
                    "mtime": group.keep_mtime,
                    "mtime_local": fmt_mtime(group.keep_mtime),
                    "size": group.keep_size,
                },
                "delete_candidates": [
                    {
                        "path": c.path,
                        "mtime": c.mtime,
                        "mtime_local": fmt_mtime(c.mtime),
                        "size": c.size,
                        "similarity_to_keep": c.similarity_to_keep,
                    }
                    for c in group.candidates
                ],
            }
        )

        do_it = args.yes
        if not args.yes:
            if not sys.stdin.isatty():
                print("[warn] stdin is not interactive; skipping (use --yes to apply automatically).")
                do_it = False
            else:
                while True:
                    ans = input(f"Apply action '{args.action}' to these {len(group.candidates)} files? [y/N/q] ")
                    ans = ans.strip().lower()
                    if ans in {"y", "yes"}:
                        do_it = True
                        break
                    if ans in {"", "n", "no"}:
                        do_it = False
                        break
                    if ans in {"q", "quit"}:
                        do_it = False
                        print("Quit.")
                        if args.report:
                            Path(args.report).write_text(
                                json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8"
                            )
                        return 0
                    print("Please enter y, n, or q.")

        if not do_it:
            continue

        for c in group.candidates:
            try:
                delete_path(Path(c.path), action=args.action)
                if args.action != "print":
                    deleted_total += 1
            except Exception as e:
                print(f"[error] failed to process {c.path}: {e}", file=sys.stderr)

    print("")
    print(
        f"Done. scanned={stats.scanned} processed={stats.processed} groups={len(groups)} suggested_deletes={suggested_deletes_total} deleted={deleted_total}"
    )

    if args.report:
        Path(args.report).write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report written: {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
