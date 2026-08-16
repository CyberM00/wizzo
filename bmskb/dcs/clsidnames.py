"""Names for pylon codes, taken from DCS's own comments about them.

DCS builds its CLSID-to-name mapping by executing Lua, so there is no table to
read -- which is why store names here are hand-curated. That covers the common
airframes and leaves everything else showing a raw code.

For a readable code that is a fair outcome: ``{AIM_54A_Mk47}`` tells a pilot what
it is. For an opaque one it is useless. A Hornet flying SEAD showed four stations
reading ``{B06DD79A-F21E-4EB9-BD9D-AB3844618C93}`` -- which is an AGM-88C HARM.

That id is not actually anonymous. DCS annotates it, in a comment on the same
line, in four separate files it ships::

    { CLSID = "{B06DD79A-...}", Cx_gain_item = 0.621 },  -- AGM-88 on LAU-118
    { CLSID = "{B06DD79A-...}", Cx_gain_item = 0.4 },    -- LAU-118 + AGM-88
    { id     = "{B06DD79A-...}", pylons = {1,1,0,1,1}},  -- HARM LAU118
    [3] = "{B06DD79A-...}",                              -- AGM-88C

This is deliberately a different signal from the three approaches that were tried
and rejected before. Those inferred a name from *nearby* text -- the enclosing
block, or the nearest string -- and were wrong often enough to mislabel an
ALQ-184 as a Soviet recon pod. A comment on the same line as the id annotates
that id and nothing else. Across the payload presets DCS ships, this names 86%
of the opaque codes the curated library does not cover.

What it is not is authoritative. These are developer comments: informal, and
occasionally abbreviated to the point of ambiguity. So the curated library always
wins, a name from here is labelled as coming from the game's own files rather
than from the curated data, and a code that appears with contradictory comments
is left unresolved instead of picking a winner.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..paths import state_path

CACHE_PATH = state_path("dcs_clsid_names.json")
CACHE_SCHEMA = 1

# Where DCS keeps the files that mention pylon codes. Scanning the whole install
# would mean tens of thousands of files for no extra names.
SCAN_DIRS = ("CoreMods", "MissionEditor", "Scripts", "Mods/aircraft")

# CLSID = "..." with a -- comment later on the same line. The comment has to be
# on that line: that is the whole point.
CLSID_LINE_RE = re.compile(r"""CLSID\s*=\s*["']([^"']+)["'][^\n]*?--\s*(.+?)\s*$""", re.M)
# Some pylon tables use "id" rather than "CLSID", but only ever for a braced code.
ID_LINE_RE = re.compile(r"""\bid\s*=\s*["'](\{[^"']+\})["'][^\n]*?--\s*(.+?)\s*$""", re.M)
# A bare index assignment with a trailing comment, as the mission generator uses.
INDEX_LINE_RE = re.compile(r"""=\s*["'](\{[0-9A-Fa-f-]{30,}\})["']\s*,?\s*--\s*(.+?)\s*$""", re.M)

# Comments that are plainly not a store name.
REJECT_PREFIXES = ("todo", "fixme", "http", "note", "not ", "remove", "was ", "see ")
MAX_NAME = 60


def _clean(comment: str) -> str:
    text = re.sub(r"\s+", " ", comment.strip().strip("-").strip())
    # Trailing counts like "x2" belong to the rack maths, not the name.
    return text.strip()


def _usable(comment: str) -> bool:
    if not comment or len(comment) > MAX_NAME:
        return False
    low = comment.lower()
    if low.startswith(REJECT_PREFIXES):
        return False
    # A comment with no letter is a number, not a name.
    return any(ch.isalpha() for ch in comment)


def _agreement_key(comment: str) -> str:
    """Loose key so "AGM-88 on LAU-118" and "LAU-118 + AGM-88" count as agreeing."""
    text = re.sub(r"[^a-z0-9]+", " ", comment.lower())
    return " ".join(sorted(set(text.split()) - {"on", "with", "and", "the", "x", "in"}))


def _stamp(base: Path) -> str:
    """A cheap change token: the newest mtime and file count across the scan."""
    newest, count = 0.0, 0
    for name in SCAN_DIRS:
        folder = base / name
        if not folder.is_dir():
            continue
        try:
            stat = folder.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        count += 1
    return f"{CACHE_SCHEMA}:{newest:.0f}:{count}"


def _scan(base: Path) -> dict[str, str]:
    readings: dict[str, Counter] = defaultdict(Counter)
    sources: dict[str, set] = defaultdict(set)

    for name in SCAN_DIRS:
        folder = base / name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.lua"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for regex in (CLSID_LINE_RE, ID_LINE_RE, INDEX_LINE_RE):
                for clsid, comment in regex.findall(text):
                    comment = _clean(comment)
                    if _usable(comment):
                        readings[clsid][comment] += 1
                        sources[clsid].add(path.name)

    resolved: dict[str, str] = {}
    for clsid, counter in readings.items():
        groups: dict[str, list] = defaultdict(list)
        for comment, hits in counter.items():
            groups[_agreement_key(comment)].append((hits, comment))
        ranked = sorted(
            groups.items(), key=lambda kv: sum(h for h, _ in kv[1]), reverse=True
        )
        best_hits = sum(h for h, _ in ranked[0][1])
        # Contradiction: two readings that are not variants of each other, with
        # comparable support. Better to show the raw code than to pick one.
        if len(ranked) > 1:
            rival_hits = sum(h for h, _ in ranked[1][1])
            if rival_hits >= best_hits:
                continue
        # Prefer the most-repeated wording, then the most descriptive one.
        resolved[clsid] = max(ranked[0][1], key=lambda pair: (pair[0], len(pair[1])))[1]
    return resolved


def load(base: Path | None) -> dict[str, str]:
    """The mapping for this install, built once and cached until DCS changes."""
    if base is None:
        return {}
    base = Path(base)
    if not base.is_dir():
        return {}

    stamp = _stamp(base)
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp and isinstance(cached.get("names"), dict):
            return cached["names"]
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    started = time.monotonic()
    names = _scan(base)
    try:
        CACHE_PATH.write_text(
            json.dumps(
                {
                    "stamp": stamp,
                    "built_seconds": round(time.monotonic() - started, 2),
                    "names": names,
                },
                indent=0,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return names
