"""Reader for the ``.gtp`` archives IL-2 packs its game data into.

The container looked opaque at first -- a proprietary ``S16R`` format with no
documentation and no bundled extractor -- but it carries a plain file table, so
individual members can be read by name without scanning the archive. That matters:
``Scripts.gtp`` is 524 MB and ``Swf.gtp`` is 1.07 GB, and the tables the kneeboard
needs total about 8.5 MB.

Layout, confirmed by reconciling the parse against the declared entry count on
both archives:

    0x00  b"S16R!"   + flags          32-byte file header, entry count at +0x0c
    0x20  b"STRMFAT" + payload size   32-byte FAT header, size at +0x28
    0x40  FAT payload: packed variable-length records

Each FAT record is a 32-byte fixed part followed by a NUL-terminated lowercase
path, so records are ``32 + path_len`` bytes and must be walked sequentially:

    +0   b"DIR!" or b"FILE"
    +4   u32 kind (2 = directory, 1 = file)
    +8   u32 absolute offset of the data block
    +12  u32 payload size
    +16  u32, +20 u32   hash / id pair
    +24  u32 path length, including the NUL
    +28  u32 zero
    +32  path bytes

Every data block starts with its own 32-byte header (``b"STRMFILE"``, the payload
size, then padding and the block's own offset). The FAT's size field counts only
the *payload*, so a member read must fetch ``32 + size`` bytes -- reading ``size``
alone silently truncates the tail, which is exactly how this was first got wrong.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

FILE_MAGIC = b"S16R!"
FAT_MAGIC = b"STRMFAT"
BLOCK_MAGIC = b"STRMFILE"

HEADER_SIZE = 32
FAT_HEADER_OFFSET = 0x20
FAT_PAYLOAD_OFFSET = 0x40
RECORD_FIXED = 32
BLOCK_HEADER = 32

ENTRY_COUNT_OFFSET = 0x0C
FAT_SIZE_OFFSET = 0x28

REC_DIR = b"DIR!"
REC_FILE = b"FILE"

# A malformed or unexpected archive must fail loudly rather than allocate wildly.
MAX_FAT_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024


class GtpError(ValueError):
    """The file was not a readable .gtp archive."""


class GtpArchive:
    """Random access to the members of one ``.gtp`` archive.

    Use as a context manager. ``path_filter`` is applied while walking the file
    table so only the paths of interest are retained -- ``Swf.gtp`` declares
    28,889 entries and the kneeboard wants one of them.
    """

    def __init__(self, path: Path, path_filter: re.Pattern | None = None):
        self.path = Path(path)
        self._filter = path_filter
        self._handle = None
        self.entries: dict[str, tuple[int, int]] = {}
        self.declared_entries = 0
        self.seen_entries = 0
        self.directories = 0

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "GtpArchive":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> "GtpArchive":
        # Idempotent: ``open_archive()`` already opens, and using its result as a
        # context manager calls this again. Re-parsing would double the counters
        # and leak the first handle.
        if self._handle is not None:
            return self
        try:
            self._handle = self.path.open("rb")
        except OSError as exc:
            raise GtpError(f"could not open {self.path.name}: {exc}") from exc
        try:
            self._read_fat()
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    # -- file table ------------------------------------------------------

    def _read_fat(self) -> None:
        handle = self._handle
        head = handle.read(FAT_PAYLOAD_OFFSET)
        if len(head) < FAT_PAYLOAD_OFFSET:
            raise GtpError(f"{self.path.name} is too small to be a .gtp archive")
        if not head.startswith(FILE_MAGIC):
            raise GtpError(
                f"{self.path.name} does not start with the expected S16R marker; "
                "the archive format may have changed in a game update"
            )
        if not head[FAT_HEADER_OFFSET:].startswith(FAT_MAGIC):
            raise GtpError(f"{self.path.name} has no file table where one was expected")

        self.declared_entries = struct.unpack_from("<I", head, ENTRY_COUNT_OFFSET)[0]
        fat_size = struct.unpack_from("<I", head, FAT_SIZE_OFFSET)[0]
        if not 0 < fat_size <= MAX_FAT_BYTES:
            raise GtpError(f"{self.path.name} declares an implausible file table size ({fat_size})")

        fat = handle.read(fat_size)
        if len(fat) != fat_size:
            raise GtpError(f"{self.path.name} file table is truncated")

        self._walk_fat(fat)

        # The parse is only trustworthy if it lands exactly on the declared count.
        if self.seen_entries != self.declared_entries:
            raise GtpError(
                f"{self.path.name} file table did not reconcile: walked "
                f"{self.seen_entries} records but the header declares "
                f"{self.declared_entries}"
            )

    def _walk_fat(self, fat: bytes) -> None:
        pos = 0
        size = len(fat)
        while pos < size:
            if pos + RECORD_FIXED > size:
                raise GtpError(f"{self.path.name} file table ends mid-record")
            magic = fat[pos : pos + 4]
            if magic not in (REC_DIR, REC_FILE):
                raise GtpError(
                    f"{self.path.name} file table has an unrecognised record marker "
                    f"{magic!r} at offset {pos}"
                )
            offset, payload_size = struct.unpack_from("<II", fat, pos + 8)
            path_len = struct.unpack_from("<I", fat, pos + 24)[0]
            end = pos + RECORD_FIXED + path_len
            if path_len == 0 or end > size:
                raise GtpError(f"{self.path.name} file table has a bad path length at offset {pos}")

            self.seen_entries += 1
            if magic == REC_DIR:
                self.directories += 1
            else:
                raw = fat[pos + RECORD_FIXED : end]
                name = raw.split(b"\x00", 1)[0].decode("cp1251", "replace")
                if self._filter is None or self._filter.search(name):
                    self.entries[name] = (offset, payload_size)
            pos = end

    # -- members ---------------------------------------------------------

    @property
    def file_count(self) -> int:
        return self.seen_entries - self.directories

    def __contains__(self, name: str) -> bool:
        return _normalise(name) in self.entries

    def read(self, name: str) -> bytes:
        """Return one member's payload, or raise ``KeyError`` if absent."""
        key = _normalise(name)
        try:
            offset, size = self.entries[key]
        except KeyError:
            raise KeyError(f"{key} is not in {self.path.name}") from None
        return self._read_at(offset, size, key)

    def _read_at(self, offset: int, size: int, label: str) -> bytes:
        if size > MAX_MEMBER_BYTES:
            raise GtpError(f"{label} declares an implausible size ({size} bytes)")
        handle = self._handle
        if handle is None:
            raise GtpError("archive is not open")
        handle.seek(offset)
        # The size in the table counts the payload only, so the block header has
        # to be read on top of it or the last 32 bytes go missing.
        block = handle.read(BLOCK_HEADER + size)
        if len(block) < BLOCK_HEADER:
            raise GtpError(f"{label} data block is truncated")
        if not block.startswith(BLOCK_MAGIC):
            raise GtpError(f"{label} data block has no STRMFILE marker")
        declared = struct.unpack_from("<I", block, 8)[0]
        if declared != size:
            raise GtpError(
                f"{label} block header declares {declared} bytes but the file table "
                f"says {size}"
            )
        return block[BLOCK_HEADER:]

    def match(self, pattern: str) -> list[str]:
        """Member paths matching a regular expression, sorted."""
        rx = re.compile(pattern)
        return sorted(name for name in self.entries if rx.search(name))

    def describe(self) -> dict:
        return {
            "path": str(self.path),
            "declared_entries": self.declared_entries,
            "files": self.file_count,
            "directories": self.directories,
            "retained": len(self.entries),
        }


def _normalise(name: str) -> str:
    """Archive paths are lowercase, slash-separated and root-anchored."""
    text = str(name).replace("\\", "/").strip().lower()
    if not text.startswith("/"):
        text = "/" + text
    return text


def open_archive(path: Path, path_filter: re.Pattern | None = None) -> GtpArchive:
    return GtpArchive(path, path_filter).open()
