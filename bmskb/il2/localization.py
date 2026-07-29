"""Read the text that goes with an IL-2 mission and turn it into prose.

Every string in a mission is an integer reference into a per-language file beside
it: ``Options { LCDesc = 1; }`` means "the briefing is key 1 of ``_gen.eng``".
Those files are UTF-16 and one key per line.

The briefing itself is pseudo-HTML with unit-conversion tags the game substitutes
according to a metric/imperial preference we cannot read. Both units are rendered
instead, which is unambiguous regardless of which airframe's instruments you are
looking at.

Output is the same structure the front end already renders for BMS and DCS:
``[{heading, items: [{kind, text}]}]``. It must be plain text -- the renderer
escapes everything, so markup would appear literally.
"""

from __future__ import annotations

import re
from pathlib import Path

M_TO_FT = 3.280839895
KMH_TO_MPH = 0.6213711922
KMH_TO_KT = 0.5399568035
MMHG_TO_INHG = 1.0 / 25.4
MMHG_TO_HPA = 1.33322387415

KEY_LINE_RE = re.compile(r"^(\d+)\s*:(.*)$")
LOCALIZE_RE = re.compile(r"</?localize>", re.IGNORECASE)
UNDERLINE_RE = re.compile(r"<u>(.*?)</u>\s*:?", re.IGNORECASE | re.DOTALL)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]*>")
BULLET_RE = re.compile(r"^\s*[-*•]\s+")

ENCODINGS = ("utf-16", "utf-16-le", "utf-16-be", "utf-8-sig", "utf-8", "cp1251", "cp1252")


def _fmt(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def _convert_units(text: str) -> str:
    """Expand the value/unit tag pairs, showing both systems.

    IL-2 writes ``<m-ft-v>1100</m-ft-v> <m-ft-u/>`` and substitutes either metres
    or feet at display time. Which one it picks depends on a user setting, so both
    are rendered.
    """

    def metres(match):
        raw = float(match.group(1))
        return f"{_fmt(raw)} m / {_fmt(raw * M_TO_FT)} ft"

    def celsius(match):
        raw = float(match.group(1))
        return f"{_fmt(raw)} C / {_fmt(raw * 9 / 5 + 32)} F"

    def speed(match):
        raw = float(match.group(1))
        return f"{_fmt(raw)} km/h / {_fmt(raw * KMH_TO_MPH)} mph / {_fmt(raw * KMH_TO_KT)} kt"

    def pressure(match):
        raw = float(match.group(1))
        return (
            f"{_fmt(raw)} mmHg / {_fmt(raw * MMHG_TO_INHG, 2)} inHg / "
            f"{_fmt(raw * MMHG_TO_HPA)} hPa"
        )

    def plain(match):
        return match.group(1)

    pairs = (
        (r"<m-ft-v>\s*(-?[\d.]+)\s*</m-ft-v>\s*(?:<m-ft-u\s*/?>)?", metres),
        (r"<c-f-v>\s*(-?[\d.]+)\s*</c-f-v>\s*(?:<c-f-u\s*/?>)?", celsius),
        (r"<kmh-mph-v>\s*(-?[\d.]+)\s*</kmh-mph-v>\s*(?:<kmh-mph-u\s*/?>)?", speed),
        (r"<mmhg-psi-v>\s*(-?[\d.]+)\s*</mmhg-psi-v>\s*(?:<mmhg-psi-u\s*/?>)?", pressure),
        # Seen in some generators; units are not documented, so it is passed
        # through as written rather than converted on a guess.
        (r"<routespeed>\s*(-?[\d.]+)\s*</routespeed>", plain),
    )
    for pattern, handler in pairs:
        text = re.sub(pattern, handler, text, flags=re.IGNORECASE)
    return text


CONTROL_TABLE = str.maketrans("", "", "".join(chr(c) for c in range(0x20) if c not in (0x09, 0x0A)) + chr(0x7F))


def _clean(text: str) -> str:
    text = TAG_RE.sub("", text)
    # The briefing text embeds C0 control bytes ahead of proper nouns -- the pilot
    # roster reads "Leader: \x01Oskar Schirmer" in the raw file. They are markers
    # of some kind, not content, and render as boxes if left in.
    text = text.translate(CONTROL_TABLE)
    return re.sub(r"[ \t]+", " ", text).strip()


def to_lines(html: str) -> list[str]:
    """Flatten one localized string into plain text lines."""
    if not html:
        return []
    text = _convert_units(html)
    text = LOCALIZE_RE.sub("", text)
    text = BR_RE.sub("\n", text)
    return [line for line in (_clean(part) for part in text.split("\n")) if line]


def to_prose(html: str) -> list[dict]:
    """Convert a briefing into the renderer's headed-group structure."""
    if not html:
        return []

    text = _convert_units(html)
    text = LOCALIZE_RE.sub("", text)
    text = BR_RE.sub("\n", text)

    groups: list[dict] = []
    current = {"heading": "", "items": []}

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        heading = UNDERLINE_RE.match(line)
        if heading:
            remainder = _clean(line[heading.end():])
            title = _clean(heading.group(1))
            if current["heading"] or current["items"]:
                groups.append(current)
            current = {"heading": title, "items": []}
            if remainder:
                current["items"].append({"kind": "text", "text": remainder})
            continue

        bullet = BULLET_RE.match(line)
        cleaned = _clean(BULLET_RE.sub("", line) if bullet else line)
        if not cleaned:
            continue
        current["items"].append({"kind": "bullet" if bullet else "text", "text": cleaned})

    if current["heading"] or current["items"]:
        groups.append(current)
    return groups


class Localization:
    """The integer-keyed strings belonging to one mission."""

    def __init__(self, entries: dict[int, str] | None = None, language: str = "", path: Path | None = None):
        self.entries = entries or {}
        self.language = language
        self.path = path

    @classmethod
    def for_mission(cls, mission_path: Path, language: str = "eng") -> "Localization":
        """Load ``<stem>.<language>`` beside the mission, falling back sensibly."""
        mission_path = Path(mission_path)
        candidates = [mission_path.with_suffix(f".{language.lower()}")]
        if language.lower() != "eng":
            candidates.append(mission_path.with_suffix(".eng"))

        for candidate in candidates:
            if candidate.is_file():
                return cls._load(candidate)

        # Any sibling language beats no text at all.
        for sibling in sorted(mission_path.parent.glob(mission_path.stem + ".*")):
            if sibling.suffix.lower() in (".mission", ".msnbin", ".list"):
                continue
            if sibling.is_file():
                return cls._load(sibling)
        return cls()

    @classmethod
    def from_bytes(cls, raw: bytes, language: str = "", path: Path | None = None) -> "Localization":
        return cls(_parse(_decode(raw)), language, path)

    @classmethod
    def _load(cls, path: Path) -> "Localization":
        try:
            raw = path.read_bytes()
        except OSError:
            return cls()
        return cls(_parse(_decode(raw)), path.suffix.lstrip(".").lower(), path)

    # -- lookup ----------------------------------------------------------

    def get(self, key) -> str:
        """Resolve an integer key. Key 0 is a legitimate reference, not 'absent'."""
        if key is None or key == "":
            return ""
        try:
            index = int(str(key).strip().strip('"'))
        except (TypeError, ValueError):
            return ""
        return self.entries.get(index, "")

    def prose(self, key) -> list[dict]:
        return to_prose(self.get(key))

    def lines(self, key) -> list[str]:
        return to_lines(self.get(key))

    @property
    def available(self) -> bool:
        return bool(self.entries)

    def describe(self) -> dict:
        return {
            "language": self.language,
            "path": str(self.path) if self.path else "",
            "keys": len(self.entries),
        }


def _decode(raw: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # A wrong UTF-16 endianness decodes without error but yields CJK soup, so
        # require the result to look like the expected "<int>:" line format.
        if KEY_LINE_RE.match(text.lstrip("﻿").splitlines()[0] if text.splitlines() else ""):
            return text
    return raw.decode("latin-1", "replace")


def _parse(text: str) -> dict[int, str]:
    """Parse ``<int>:<text>`` lines, keeping only the ones carrying content."""
    entries: dict[int, str] = {}
    for line in text.lstrip("﻿").splitlines():
        match = KEY_LINE_RE.match(line)
        if not match:
            continue
        value = match.group(2).strip()
        if value:
            entries[int(match.group(1))] = value
    return entries
