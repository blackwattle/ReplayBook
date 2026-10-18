"""Read one collection straight out of a `mongodump --archive` file, no database needed.

The Public Jira Dataset ships as a single gzipped archive that expands to about 60 GB
inside MongoDB. Replaybook only wants one collection (one Jira) and usually one project,
so this streams the archive once and hands back the documents it is asked for, skipping
everything else without decoding it.

Archive layout (mongo-tools `common/archive`): a 4-byte magic number, a header document,
one metadata document per collection followed by a terminator, then blocks of
`{db, collection, EOF, CRC}` namespace headers each followed by BSON documents and a
terminator. Blocks of different collections interleave. A terminator is the four bytes
0xFFFFFFFF where a document length would be.
"""

from __future__ import annotations

import gzip
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

MAGIC = b"\x6d\xe2\x99\x81"
TERMINATOR = b"\xff\xff\xff\xff"
GZIP_MAGIC = b"\x1f\x8b"
KEY_MARKER = b"\x02key\x00"


class ArchiveFormatError(ValueError):
    """The file is not a mongodump archive (or not one this reader understands)."""


def _read_raw(handle: IO[bytes]) -> bytes | None:
    """One BSON document as bytes, None at a terminator, EOFError at the end of the file."""
    head = handle.read(4)
    if not head:
        raise EOFError
    if head == TERMINATOR:
        return None
    if len(head) < 4:
        raise ArchiveFormatError("truncated archive")
    length = struct.unpack("<i", head)[0]
    if length < 5:
        raise ArchiveFormatError(f"bad document length {length}")
    rest = handle.read(length - 4)
    if len(rest) != length - 4:
        raise ArchiveFormatError("truncated document")
    return head + rest


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        import bson
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError(
            "reading a mongodump archive needs pymongo: `uv sync --extra jira`"
        ) from exc
    return bson.decode(raw)


def _read_doc(handle: IO[bytes]) -> dict[str, Any] | None:
    raw = _read_raw(handle)
    return None if raw is None else _decode(raw)


# BSON element types with a fixed size, for walking the top level of a document.
_FIXED = {
    0x01: 8,
    0x06: 0,
    0x07: 12,
    0x08: 1,
    0x09: 8,
    0x0A: 0,
    0x10: 4,
    0x11: 8,
    0x12: 8,
    0x13: 16,
}
_LENGTH_PREFIXED = {0x02, 0x0D, 0x0E}  # string, code, symbol: int32 length then bytes
_EMBEDDED = {0x03, 0x04, 0x0F}  # document, array, code with scope: int32 total length


def issue_key(raw: bytes) -> str | None:
    """The top-level `key` string of a document without decoding the whole thing.

    Walks the top-level elements only; nested documents are skipped by their length.
    Returns None when there is no such string or an element type is not understood.
    """
    pos = 4
    end = len(raw) - 1
    while pos < end:
        kind = raw[pos]
        name_end = raw.find(b"\x00", pos + 1)
        if name_end < 0:
            return None
        name = raw[pos + 1 : name_end]
        pos = name_end + 1
        if kind == 0x02 and name == b"key":
            length = struct.unpack("<i", raw[pos : pos + 4])[0]
            try:
                return raw[pos + 4 : pos + 4 + length - 1].decode("utf-8")
            except UnicodeDecodeError:
                return None
        if kind in _FIXED:
            pos += _FIXED[kind]
        elif kind in _LENGTH_PREFIXED:
            pos += 4 + struct.unpack("<i", raw[pos : pos + 4])[0]
        elif kind in _EMBEDDED:
            pos += struct.unpack("<i", raw[pos : pos + 4])[0]
        elif kind == 0x05:  # binary: int32 length, subtype byte, bytes
            pos += 5 + struct.unpack("<i", raw[pos : pos + 4])[0]
        elif kind == 0x0B:  # regex: two cstrings
            pos = raw.find(b"\x00", raw.find(b"\x00", pos) + 1) + 1
        elif kind == 0x0C:  # dbpointer: string then 12 bytes
            pos += 4 + struct.unpack("<i", raw[pos : pos + 4])[0] + 12
        elif kind in (0x7F, 0xFF):
            pass
        else:
            return None
    return None


def open_archive(path: Path) -> IO[bytes]:
    with Path(path).open("rb") as probe:
        gzipped = probe.read(2) == GZIP_MAGIC
    return gzip.open(path, "rb") if gzipped else Path(path).open("rb")  # type: ignore[return-value]


def list_collections(path: Path) -> list[dict[str, Any]]:
    """The collections an archive holds, from its prelude (fast: nothing else is read)."""
    with open_archive(path) as handle:
        _check_magic(handle)
        _read_doc(handle)  # header: tool and server versions
        found: list[dict[str, Any]] = []
        while True:
            meta = _read_doc(handle)
            if meta is None:
                return found
            found.append({k: meta.get(k) for k in ("db", "collection", "size", "type")})


def _check_magic(handle: IO[bytes]) -> None:
    if handle.read(4) != MAGIC:
        raise ArchiveFormatError("not a mongodump archive (bad magic number)")


def iter_collection(
    path: Path, collection: str, db: str | None = None, key_prefix: str | None = None
) -> Iterator[dict[str, Any]]:
    """Documents of one collection. With `key_prefix`, only issues whose key starts with it
    (checked on the raw bytes, so the rest of the archive is never decoded)."""
    with open_archive(path) as handle:
        _check_magic(handle)
        _read_doc(handle)
        while _read_doc(handle) is not None:  # collection metadata until the terminator
            pass
        try:
            while True:
                header = _read_doc(handle)
                if header is None:
                    continue
                wanted = header.get("collection") == collection and (
                    db is None or header.get("db") == db
                )
                while True:
                    raw = _read_raw(handle)
                    if raw is None:
                        break
                    if not wanted:
                        continue
                    if key_prefix is not None:
                        key = issue_key(raw)
                        if key is not None and not key.startswith(key_prefix):
                            continue
                        if key is None:
                            doc = _decode(raw)
                            if not str(doc.get("key", "")).startswith(key_prefix):
                                continue
                            yield doc
                            continue
                    yield _decode(raw)
        except EOFError:
            return


def write_archive(
    path: Path, collections: dict[str, list[dict[str, Any]]], db: str = "test"
) -> None:
    """Write a small archive in the same layout (for tests and samples)."""
    import bson

    with gzip.open(path, "wb") as handle:
        handle.write(MAGIC)
        handle.write(
            bson.encode(
                {
                    "concurrent_collections": 1,
                    "version": "0.1",
                    "server_version": "7.0.0",
                    "tool_version": "replaybook",
                }
            )
        )
        for name, docs in collections.items():
            handle.write(
                bson.encode(
                    {"db": db, "collection": name, "metadata": "{}", "size": len(docs), "type": ""}
                )
            )
        handle.write(TERMINATOR)
        for name, docs in collections.items():
            handle.write(bson.encode({"db": db, "collection": name, "EOF": False, "CRC": 0}))
            for doc in docs:
                handle.write(bson.encode(doc))
            handle.write(TERMINATOR)
            handle.write(bson.encode({"db": db, "collection": name, "EOF": True, "CRC": 0}))
            handle.write(TERMINATOR)
