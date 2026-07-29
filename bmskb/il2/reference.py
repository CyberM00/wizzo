"""The loose lookup tables IL-2 leaves readable in ``data\\GUI``.

Two small files, and they are what turn raw numbers into something a pilot
recognises: a ``Plane`` block's ``Callsign = 7; Callnum = 4`` becomes "Finch 4",
and an ``Airfield``'s ``Callsign = 39`` becomes its tower callsign.

Install-scoped and read once, in the same spirit as ``bmskb/charts.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .localization import Localization

SECTION_RE = re.compile(r"^\[ObjectType\s*=\s*(\d+)\]")
ROW_RE = re.compile(r'^\s*callsign\s*=\s*(\d+)\s*,\s*"([^"]*)"')

OBJECT_PLANES = 1
OBJECT_AIRFIELDS = 2
OBJECT_SPOTTERS = 3

ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1251", "cp1252")


def _read(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("latin-1", "replace")


class Il2Reference:
    """Callsign words and country names for one installation."""

    def __init__(self, install=None, language: str = "eng"):
        self.language = language
        self.callsigns: dict[int, dict[int, str]] = {}
        self.countries: dict[int, str] = {}
        self.errors: list[str] = []
        if install is not None:
            self._load(install)

    def _load(self, install) -> None:
        gui = install.gui_dir
        suffix = f".{self.language.lower()}"

        callsign_file = gui / f"DefaultCallsigns{suffix}"
        if not callsign_file.is_file():
            callsign_file = gui / "DefaultCallsigns.eng"
        if callsign_file.is_file():
            self.callsigns = _parse_callsigns(_read(callsign_file))
        else:
            self.errors.append("DefaultCallsigns not found; callsigns will show as numbers")

        country_file = gui / f"DefaultCountryNames{suffix}"
        if not country_file.is_file():
            country_file = gui / "DefaultCountryNames.eng"
        if country_file.is_file():
            # Same "<int>:<text>" shape the mission text files use.
            self.countries = Localization.from_bytes(country_file.read_bytes()).entries

    # -- lookup ----------------------------------------------------------

    def callsign(self, object_type: int, callsign_id, callnum=None) -> str:
        """"Finch 4" for a flight, "Finch" for a field with no number."""
        try:
            index = int(callsign_id)
        except (TypeError, ValueError):
            return ""
        word = self.callsigns.get(object_type, {}).get(index, "")
        if not word:
            return ""
        if callnum in (None, "", 0):
            return word
        return f"{word} {callnum}"

    def flight_callsign(self, callsign_id, callnum=None) -> str:
        return self.callsign(OBJECT_PLANES, callsign_id, callnum)

    def airfield_callsign(self, callsign_id, callnum=None) -> str:
        return self.callsign(OBJECT_AIRFIELDS, callsign_id, callnum)

    def country(self, country_id) -> str:
        try:
            return self.countries.get(int(country_id), "")
        except (TypeError, ValueError):
            return ""

    def describe(self) -> dict:
        return {
            "callsign_groups": {k: len(v) for k, v in self.callsigns.items()},
            "countries": len(self.countries),
        }


def _parse_callsigns(text: str) -> dict[int, dict[int, str]]:
    out: dict[int, dict[int, str]] = {}
    section = 0
    for line in text.splitlines():
        stripped = line.strip()
        header = SECTION_RE.match(stripped)
        if header:
            section = int(header.group(1))
            continue
        row = ROW_RE.match(stripped)
        if row and section:
            out.setdefault(section, {})[int(row.group(1))] = row.group(2).strip()
    return out
