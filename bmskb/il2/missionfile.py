"""Targeted reader for the IL-2 mission text format.

A mission file is a flat text tree of ``Identifier { Key = Value; }`` blocks. The
career file measured here is 7.9 MB across 444,000 lines, and roughly 95% of that
is scenery -- 8,710 ``Block`` entries, 6,601 ``Damaged``, 2,123 ``MCU_Timer``. The
kneeboard wants five block types totalling about 0.38 MB.

So this extracts only the blocks asked for rather than parsing the whole file. A
full parse would materialise tens of thousands of dictionaries to answer questions
about 52 aircraft and 5 route markers, and the grammar is regular enough that a
depth counter cannot get lost: braces always sit alone on their line, values are
always ``;``-terminated, and no value contains a brace. (Contrast the DCS side,
where ``luaparse`` needed a real tokenizer because Lua strings can contain braces
and escaped newlines -- the briefing text there does exactly that.)

Blocks nest, and the nesting matters: a career file has its ``Plane`` entries at
the top level, but other generators wrap them in ``Group``. Matching the wanted
identifier at any depth handles both.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

from ..install import read_text

SCALAR_RE = re.compile(r"(?m)^\s*(\w+)\s*=\s*([^;\n]*);")
INT_LIST_RE = re.compile(r"^\[(.*)\]$")
CONTROL_CHARS = "".join(chr(c) for c in range(0x20)) + chr(0x7F)
CONTROL_TABLE = str.maketrans("", "", CONTROL_CHARS)

# Refuse anything that is clearly not a mission before reading it, and cap what a
# malformed file can make us retain.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_CAPTURED_BYTES = 8 * 1024 * 1024
MAX_BLOCKS_PER_TYPE = 4000


class MissionError(ValueError):
    """The file was not a readable IL-2 mission."""


def scan(text: str, wanted: set[str] | tuple[str, ...]) -> dict[str, list[str]]:
    """Return ``{block_type: [raw body, ...]}`` for the wanted block types.

    Bodies exclude the enclosing braces and include any nested blocks, so a
    captured ``Airfield`` still carries its ``Chart``.
    """
    targets = set(wanted)
    out: dict[str, list[str]] = {name: [] for name in targets}

    pending: str | None = None
    capturing: str | None = None
    depth = 0
    buffer: list[str] = []
    captured = 0

    for line in StringIO(text):
        stripped = line.strip()

        if capturing is None:
            if stripped in targets:
                pending = stripped
                continue
            if pending is not None:
                if stripped == "{":
                    capturing = pending
                    pending = None
                    depth = 1
                    buffer = []
                    continue
                if stripped:
                    pending = None
            continue

        # Inside a block: track depth so nested blocks are kept whole.
        opens = stripped.count("{")
        closes = stripped.count("}")
        if depth + opens - closes <= 0:
            body = "".join(buffer)
            bucket = out[capturing]
            if len(bucket) < MAX_BLOCKS_PER_TYPE:
                bucket.append(body)
                captured += len(body)
                if captured > MAX_CAPTURED_BYTES:
                    raise MissionError(
                        "mission file asked for more memory than a mission should need; "
                        "it may be malformed"
                    )
            capturing = None
            depth = 0
            buffer = []
            continue

        depth += opens - closes
        buffer.append(line)

    if capturing is not None:
        raise MissionError(f"mission file ended inside an unterminated {capturing} block")

    return out


def fields(body: str) -> dict[str, str]:
    """Scalar ``Key = Value;`` pairs in one block body, nested blocks ignored."""
    return {key: value.strip() for key, value in SCALAR_RE.findall(body)}


def read_mission(path: Path) -> str:
    """Read a mission file, refusing the compiled binary sibling."""
    path = Path(path)
    if path.suffix.lower() == ".msnbin":
        raise MissionError(
            f"{path.name} is the compiled binary form of a mission, which cannot be read"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MissionError(f"could not read {path.name}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise MissionError(f"{path.name} is {size:,} bytes, too large to be a mission file")
    try:
        return read_text(path)
    except OSError as exc:
        raise MissionError(f"could not read {path.name}: {exc}") from exc


# ------------------------------------------------------------- coercion


def as_str(value: str | None) -> str:
    """Strip quotes and control characters.

    The player's ``Name`` genuinely begins with a C0 byte -- ``"\\x04Oskar Harder"``
    -- so stripping controls is required, not defensive.
    """
    if value is None:
        return ""
    return value.strip().strip('"').translate(CONTROL_TABLE).strip()


def as_int(value: str | None, default=None):
    try:
        return int(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return default


def as_float(value: str | None, default=None):
    try:
        return float(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return default


def as_int_list(value: str | None) -> list[int]:
    """Parse ``[3, 4, 5]``; an empty list is ``[]``."""
    if not value:
        return []
    match = INT_LIST_RE.match(value.strip())
    inner = match.group(1) if match else value
    out = []
    for part in inner.split(","):
        number = as_int(part)
        if number is not None:
            out.append(number)
    return out


def bits_from_base2(value: str | None) -> list[int]:
    """Indices of set bits in a base-2 digit string such as ``10011``.

    Whether IL-2 writes these most- or least-significant digit first is NOT
    confirmed, so callers must not present the result as fact. Bit 0 is read as
    the rightmost digit here, which is the conventional reading.
    """
    text = str(value or "").strip().strip('"')
    if not text or any(ch not in "01" for ch in text):
        return []
    return [i for i, ch in enumerate(reversed(text)) if ch == "1"]
