"""Parser for ``dtc_comm.txt`` -- the radio preset table BMS exports.

This file is written independently of ``briefing.txt`` and is not always
regenerated at the same time, so the generation timestamp is captured and
compared against the briefing's. A preset list from a previous mission is
worse than none at all, so a mismatch is reported rather than hidden.
"""

from __future__ import annotations

import re
from pathlib import Path

from .install import read_text

PRESET_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{2,3}\.\d{1,3})\s*(.*)$")
BAND_RE = re.compile(r"^\s*(UHF|VHF)\s*$", re.IGNORECASE)
GENERATED_RE = re.compile(r"generated at\s+(.*?)\.?\s*$", re.IGNORECASE)


def parse_dtc_comm_text(text: str) -> dict:
    bands: dict[str, list[dict]] = {"UHF": [], "VHF": []}
    generated = ""
    current: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        if not generated:
            match = GENERATED_RE.search(line)
            if match:
                generated = match.group(1).strip()
                continue

        band = BAND_RE.match(line)
        if band:
            current = band.group(1).upper()
            continue

        if current is None:
            continue
        if line.strip().startswith("=") or "Preset" in line:
            continue

        preset = PRESET_RE.match(line)
        if preset:
            comment = preset.group(3).strip()
            bands[current].append(
                {
                    "preset": int(preset.group(1)),
                    "frequency": preset.group(2),
                    "comment": "" if comment == "(open)" else comment,
                    "open": comment == "(open)",
                }
            )

    return {
        "generated": generated,
        "uhf": bands["UHF"],
        "vhf": bands["VHF"],
        "available": bool(bands["UHF"] or bands["VHF"]),
    }


def parse_dtc_comm(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"generated": "", "uhf": [], "vhf": [], "available": False}
    return parse_dtc_comm_text(read_text(path))
