"""Index the aircraft manuals BMS and DCS ship as documents.

IL-2 publishes its aircraft performance data as text, so the board can show the
figures directly. BMS and DCS do not -- they ship PDFs. Those cannot be turned
into performance figures without parsing prose and guessing, so they are offered
as documents to open instead: listed, grouped, and served from the install.

BMS keeps them under ``Docs\\02 Aircraft Manuals & Checklists``. DCS keeps them
per module under ``Mods/aircraft/<module>/Doc``.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC_SUFFIXES = {".pdf", ".html", ".htm", ".txt", ".doc", ".docx"}
MAX_DEPTH = 4

# Language-suffixed duplicates of the same manual; English is the one to show.
NON_ENGLISH = re.compile(
    r"[ _-](CN|DE|FR|ES|RU|JP|KO|ZH|PL|CZ|IT|CHS|TW)(\.|$)|(?:_|\b)(chinese|german|french|spanish|russian|japanese|korean|polish)\b",
    re.I,
)


def _is_english(name: str) -> bool:
    return not NON_ENGLISH.search(name)


def _entries(root: Path, base: Path, group: str) -> list[dict]:
    out: list[dict] = []
    if not root.is_dir():
        return out
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
                continue
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if len(relative.parts) > MAX_DEPTH + 1:
                continue
            out.append(
                {
                    "title": path.stem,
                    "format": path.suffix.lower().lstrip("."),
                    "group": group,
                    "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                    "rel": relative.as_posix(),
                    "english": _is_english(path.name),
                }
            )
    except OSError:
        return out
    return out


class ManualLibrary:
    """Documents available for one sim, with a root each request is checked against."""

    def __init__(self, base: Path | None, sim: str):
        self.base = Path(base) if base else None
        self.sim = sim
        self.documents: list[dict] = []
        self._scan()

    def _scan(self) -> None:
        if not self.base or not self.base.is_dir():
            return
        if self.sim == "bms":
            docs = self.base / "Docs"
            if not docs.is_dir():
                return
            for child in sorted(docs.iterdir()):
                if child.is_dir() and "aircraft" in child.name.lower():
                    self.documents = _entries(child, self.base, "Aircraft manuals")
                    break
        elif self.sim == "dcs":
            aircraft = self.base / "Mods" / "aircraft"
            if not aircraft.is_dir():
                return
            for module in sorted(aircraft.iterdir()):
                doc = module / "Doc"
                if doc.is_dir():
                    self.documents.extend(_entries(doc, self.base, module.name))

    # -- lookup ----------------------------------------------------------

    def for_aircraft(self, *hints: str) -> list[dict]:
        """Documents whose group or title matches any of the given hints.

        Hints arrive in several shapes: a DCS type id (``AV8BNA``), a display name
        (``AV-8B N/A``), or a BMS package entry (``2 F-16CM-40``). Each is reduced
        to an alphanumeric key so "F/A-18C" finds "FA-18C", and also to a family
        key -- "F-16CM-40" yields "f16" -- because manuals are named after the
        family rather than the exact block.
        """
        keys: set[str] = set()
        for hint in hints:
            if not hint:
                continue
            # Drop a leading count: BMS writes "2 F-16CM-40".
            trimmed = re.sub(r"^\s*\d+\s+", "", str(hint))
            full = _key(trimmed)
            if len(full) >= 3:
                keys.add(full)
            family = re.match(r"^([a-z]+\d+)", full)
            if family and len(family.group(1)) >= 3:
                keys.add(family.group(1))
        if not keys:
            return []
        matched = []
        for doc in self.documents:
            haystack = _key(doc["group"]) + " " + _key(doc["title"])
            if any(key in haystack for key in keys):
                matched.append(doc)
        return sorted(matched, key=lambda d: (not d["english"], d["title"]))

    def grouped(self) -> list[dict]:
        """Everything, grouped, English first."""
        groups: dict[str, list[dict]] = {}
        for doc in self.documents:
            groups.setdefault(doc["group"], []).append(doc)
        return [
            {
                "group": name,
                "documents": sorted(items, key=lambda d: (not d["english"], d["title"])),
            }
            for name, items in sorted(groups.items())
        ]

    def describe(self) -> dict:
        return {
            "count": len(self.documents),
            "groups": len({d["group"] for d in self.documents}),
        }


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())
