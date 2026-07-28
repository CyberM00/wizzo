"""Parser for the Falcon BMS ``briefing.txt`` record.

The file is tab-delimited, grouped into sections. A section header sits flush
against the left margin ("Steerpoints:", "Ordnance:", "Iff"); every line
belonging to that section is indented with at least one tab.

Rows use runs of tabs as crude column padding, so cells are split on tab and
empty cells dropped. BMS writes "--" rather than a blank for a genuinely empty
field, which is what makes that safe.
"""

from __future__ import annotations

import re
from pathlib import Path

from .install import read_text

ICAO_RE = re.compile(r"\(([A-Z]{4})\)")
COUNT_RE = re.compile(r"^(\d+)\s*x\s+(.*)$", re.IGNORECASE)
AIRCRAFT_LABEL_RE = re.compile(r"^--\s*(.+?)\s*--$")
TACAN_RE = re.compile(r"TCN:\s*([0-9]{1,3}[XYxy]?)")
FLIGHT_ROLE_RE = re.compile(r"^(\S+)\s*\((.+?)\)\s*$")
DIVIDER_RE = re.compile(r"^[-=_]{5,}$")

# Trailing service words on a comm-ladder callsign, e.g. "Osan Ground" -> "Osan".
AGENCY_SUFFIXES = (
    "approach",
    "departure",
    "ground",
    "tower",
    "atis",
    "ops",
    "operations",
    "center",
    "centre",
)


def _cells(line: str) -> list[str]:
    """Split a briefing row into non-empty cells."""
    return [cell.strip() for cell in line.split("\t") if cell.strip()]


def _is_section_header(line: str) -> bool:
    if not line.strip():
        return False
    if line[0] in " \t":
        return False
    return not DIVIDER_RE.match(line.strip())


def _base_name(callsign: str) -> str:
    """'Osan Ground' -> 'Osan'; 'Pyeongtaek Approach' -> 'Pyeongtaek'."""
    words = callsign.split()
    while words and words[-1].lower().strip(":") in AGENCY_SUFFIXES:
        words.pop()
    return " ".join(words).strip()


def split_sections(text: str) -> dict[str, list[str]]:
    """Break the briefing into ``{section_name: [lines]}`` preserving order."""
    sections: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: list[str] | None = None

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line.strip() == "END_OF_BRIEFING":
            break
        # The "BRIEFING RECORD generated at ..." banner is flush-left like a
        # section header, but it is metadata -- keep it in the preamble.
        if "generated at" in line.lower():
            preamble.append(line)
            continue
        if _is_section_header(line):
            # Some headers carry a trailing annotation after a tab, e.g.
            # "Package Elements:\tx = Primary Flight".
            name = line.split("\t")[0].strip().rstrip(":").strip()
            current = sections.setdefault(name, [])
            continue
        (current if current is not None else preamble).append(line)

    if preamble:
        sections["_preamble"] = preamble
    return sections


def _find_section(sections: dict[str, list[str]], *needles: str) -> list[str]:
    """Look a section up case-insensitively by substring, so minor BMS
    wording changes between versions don't silently drop a whole panel."""
    for needle in needles:
        needle = needle.lower()
        for name, lines in sections.items():
            if name.lower() == needle:
                return lines
    for needle in needles:
        needle = needle.lower()
        for name, lines in sections.items():
            if needle in name.lower():
                return lines
    return []


# ---------------------------------------------------------------------------
# section parsers
# ---------------------------------------------------------------------------


def parse_overview(lines: list[str]) -> dict:
    data: dict = {
        "flight": "",
        "role": "",
        "fields": {},
        "package": "",
        "package_type": "",
        "mission": "",
        "target_area": "",
        "time_on_target": "",
        "sunrise": "",
        "sunset": "",
        "target_icao": "",
    }

    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        if len(cells) == 1:
            match = FLIGHT_ROLE_RE.match(cells[0])
            if match:
                data["flight"], data["role"] = match.group(1), match.group(2).strip()
            continue

        label = cells[0].rstrip(":").strip()
        value = " ".join(cells[1:]).strip()
        data["fields"][label] = value

        key = label.lower()
        if key.startswith("package"):
            data["package"] = value
            inner = FLIGHT_ROLE_RE.match(value)
            if inner:
                data["package"], data["package_type"] = inner.group(1), inner.group(2).strip()
        elif "mission" in key:
            data["mission"] = value
            icao = ICAO_RE.search(value)
            if icao:
                data["target_icao"] = icao.group(1)
        elif "target area" in key:
            data["target_area"] = value
        elif "time on target" in key:
            data["time_on_target"] = value
        elif key.startswith("sunrise"):
            data["sunrise"] = value
        elif key.startswith("sunset"):
            data["sunset"] = value

    return data


def parse_prose(lines: list[str]) -> list[dict]:
    """Parse a free-text section into headed groups of paragraphs and bullets."""
    groups: list[dict] = []
    current = {"heading": "", "items": []}

    for line in lines:
        # Prose rows are padded with tabs for alignment; collapse them so the
        # text reads as a sentence rather than a fragmented table row.
        text = re.sub(r"\s+", " ", line).strip()
        if not text:
            continue
        if text.startswith("--"):
            current["items"].append({"kind": "bullet", "text": text.lstrip("-").strip()})
            continue
        # A short line ending in ':' with nothing after it is a sub-heading.
        # Keep the limit tight -- BMS also writes full sentences ending in a
        # colon ("Intelligence reports the highest impact targets are:") and
        # those should stay paragraphs.
        if text.endswith(":") and len(text) < 40 and "\t" not in line.strip():
            if current["heading"] or current["items"]:
                groups.append(current)
            current = {"heading": text.rstrip(":"), "items": []}
            continue
        current["items"].append({"kind": "text", "text": text})

    if current["heading"] or current["items"]:
        groups.append(current)
    return groups


STEERPOINT_COLUMNS = [
    "index",
    "description",
    "time",
    "distance",
    "heading",
    "cas",
    "altitude",
    "action",
    "formation",
    "comments",
]


def parse_steerpoints(lines: list[str]) -> list[dict]:
    points: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells or not cells[0].rstrip(".").isdigit():
            continue
        row = dict(zip(STEERPOINT_COLUMNS, cells))
        for column in STEERPOINT_COLUMNS:
            row.setdefault(column, "")
        row["extra"] = cells[len(STEERPOINT_COLUMNS):]
        points.append(row)
    return points


def parse_comm_ladder(lines: list[str]) -> dict:
    rows: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if len(cells) < 2:
            continue
        if cells[0].rstrip(":").lower() == "agency":
            continue
        cells = (cells + ["", "", "", ""])[:5]
        agency = cells[0].rstrip(":").strip()
        rows.append(
            {
                "agency": agency,
                "callsign": cells[1],
                "uhf": cells[2],
                "vhf": cells[3],
                "notes": cells[4],
                "group": _comm_group(agency),
            }
        )

    airbases = _airbases_from_comms(rows)
    tanker = next((r for r in rows if "tanker" in r["agency"].lower() or "aar" in r["agency"].lower()), None)
    return {
        "rows": rows,
        "airbases": airbases,
        "tanker_tacan": (TACAN_RE.search(tanker["callsign"]).group(1) if tanker and TACAN_RE.search(tanker["callsign"]) else ""),
    }


def _comm_group(agency: str) -> str:
    key = agency.lower()
    if key.startswith("dep"):
        return "departure"
    if key.startswith("arr"):
        return "recovery"
    if key.startswith("alt"):
        return "alternate"
    if key.startswith("intra") or "flight" in key:
        return "flight"
    return "general"


def _airbases_from_comms(rows: list[dict]) -> dict:
    """Recover departure/recovery/alternate base names from the comm ladder."""
    bases: dict[str, str] = {}
    for row in rows:
        group = row["group"]
        if group not in ("departure", "recovery", "alternate"):
            continue
        name = _base_name(row["callsign"])
        if name and name.lower() not in ("none", "--") and group not in bases:
            bases[group] = name
    return bases


def parse_iff(lines: list[str]) -> dict:
    """IFF is a mix of labelled settings and a rotation table.

    The rotation rows in BMS do not all carry the same number of values, so
    rows are kept as-is rather than zipped into columns -- pairing them up
    would risk showing a code against the wrong time block.
    """
    blocks: list[dict] = []
    current = {"heading": "", "rows": []}

    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        if len(cells) == 1 and cells[0].endswith(":"):
            if current["heading"] or current["rows"]:
                blocks.append(current)
            current = {"heading": cells[0].rstrip(":"), "rows": []}
            continue
        if len(cells) == 1:
            current["rows"].append({"label": "", "values": [cells[0]]})
            continue
        current["rows"].append({"label": cells[0].rstrip(":"), "values": cells[1:]})

    if current["heading"] or current["rows"]:
        blocks.append(current)
    return {"blocks": blocks}


def parse_link16(lines: list[str]) -> list[dict]:
    files: list[dict] = []
    current: dict | None = None

    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        head = cells[0].rstrip(":").strip()
        if head.upper().startswith("FILE "):
            current = {"name": head.upper(), "meta": [], "stns": [], "headers": []}
            files.append(current)
            continue
        if current is None:
            continue
        if head.upper() == "STNS":
            current["headers"] = cells[1:]
            continue
        if head.lower() in ("donor", "team", "flight"):
            stns = cells[1:9]
            extras = cells[9:]
            pairs = [
                {"label": extras[i].rstrip(":"), "value": extras[i + 1]}
                for i in range(0, len(extras) - 1, 2)
            ]
            current["stns"].append({"label": head, "values": stns, "channels": pairs})
            continue
        # "Flt Lead: No" style metadata, several per line.
        for cell in cells:
            if ":" in cell:
                label, _, value = cell.partition(":")
                current["meta"].append({"label": label.strip(), "value": value.strip()})

    return files


def parse_ordnance(lines: list[str]) -> list[dict]:
    """Parse the per-flight ordnance blocks.

    A block opens with a row of ``-- Callsign11 --`` aircraft labels and is
    followed by one row per store type, with one column per aircraft.
    """
    flights: list[dict] = []
    current: dict | None = None

    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        if cells[0].rstrip(":").lower() == "callsign":
            continue

        labels = [AIRCRAFT_LABEL_RE.match(cell) for cell in cells]
        if any(labels):
            callsign = next(
                (cell for cell, match in zip(cells, labels) if not match),
                "",
            )
            aircraft = [m.group(1) for m in labels if m]
            current = {
                "callsign": callsign,
                "aircraft": [{"label": name, "stores": []} for name in aircraft],
            }
            flights.append(current)
            continue

        if current is None or not current["aircraft"]:
            continue

        for i, cell in enumerate(cells):
            if i >= len(current["aircraft"]):
                break
            match = COUNT_RE.match(cell)
            if match:
                count, name = int(match.group(1)), match.group(2).strip()
            else:
                count, name = 1, cell
            current["aircraft"][i]["stores"].append({"count": count, "name": name})

    for flight in flights:
        flight["uniform"] = _stores_uniform(flight["aircraft"])
    return flights


def _stores_uniform(aircraft: list[dict]) -> bool:
    if len(aircraft) < 2:
        return True
    def key(entry):
        return sorted((s["count"], s["name"]) for s in entry["stores"])
    first = key(aircraft[0])
    return all(key(other) == first for other in aircraft[1:])


def parse_package_elements(lines: list[str]) -> list[dict]:
    elements: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        head = cells[0]
        if head.rstrip(":").lower() == "callsign" or head.startswith("x "):
            continue

        if head.upper().startswith("T/O"):
            if elements:
                elements[-1]["timing"] = [
                    {"label": c.partition(":")[0].strip(), "value": c.partition(":")[2].strip()}
                    for c in cells
                    if ":" in c
                ]
            continue

        cells = (cells + ["", "", "", ""])[:5]
        flight_no = cells[1]
        primary = "(x" in flight_no
        elements.append(
            {
                "callsign": cells[0],
                "flight_number": re.sub(r"\(.*?\)", "", flight_no).strip(),
                "primary": primary,
                "role": cells[2],
                "aircraft": cells[3],
                "task": cells[4],
                "timing": [],
            }
        )
    return elements


def parse_roster(lines: list[str]) -> dict:
    headers: list[str] = []
    rows: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        if cells[0].rstrip(":").lower() == "callsign":
            headers = [c.rstrip(":") for c in cells[1:]]
            continue
        rows.append({"callsign": cells[0], "pilots": cells[1:]})
    return {"headers": headers, "rows": rows}


def parse_weather(lines: list[str]) -> dict:
    headers: list[str] = []
    rows: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        label = cells[0].rstrip(":").strip()
        if label.lower().startswith("conditions"):
            headers = [c.rstrip(":") for c in cells[1:]]
            continue
        rows.append({"label": label, "values": cells[1:]})
    return {"headers": headers or ["Take Off", "Target Area", "Landing"], "rows": rows}


def parse_support(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells:
            continue
        label = cells[0].rstrip(":").strip()
        if label.lower() == "callsign" or label.lower().startswith("station area"):
            continue
        match = FLIGHT_ROLE_RE.match(label)
        callsign, kind = (match.group(1), match.group(2)) if match else (label, "")
        detail = " ".join(cells[2:]).strip() if len(cells) > 2 else ""
        tacan = TACAN_RE.search(line)
        entries.append(
            {
                "callsign": callsign,
                "kind": kind,
                "asset": cells[1] if len(cells) > 1 else "",
                "detail": detail,
                "tacan": tacan.group(1) if tacan else "",
            }
        )
    return entries


def _alternate_airfield(groups: list[dict]) -> dict:
    for group in groups:
        if "alternate" not in group["heading"].lower():
            continue
        for item in group["items"]:
            icao = ICAO_RE.search(item["text"])
            return {
                "text": item["text"],
                "icao": icao.group(1) if icao else "",
                "name": item["text"].split("(")[0].strip(),
            }
    return {"text": "", "icao": "", "name": ""}


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def parse_briefing_text(text: str) -> dict:
    sections = split_sections(text)

    overview = parse_overview(_find_section(sections, "Mission Overview"))
    comms = parse_comm_ladder(_find_section(sections, "Comm Ladder"))
    emergency = parse_prose(_find_section(sections, "Emergency Procedures"))
    alternate = _alternate_airfield(emergency)

    generated = ""
    for line in sections.get("_preamble", []):
        if "generated at" in line.lower():
            generated = line.strip().rstrip(".").split("generated at", 1)[1].strip()
            break

    airbases = dict(comms["airbases"])
    if alternate["name"]:
        airbases["alternate"] = alternate["name"]

    return {
        "generated": generated,
        "overview": overview,
        "situation": parse_prose(_find_section(sections, "Situation")),
        "roster": parse_roster(_find_section(sections, "Pilot Roster")),
        "package": parse_package_elements(_find_section(sections, "Package Elements")),
        "threats": parse_prose(_find_section(sections, "Threat Analysis")),
        "steerpoints": parse_steerpoints(_find_section(sections, "Steerpoints")),
        "comms": comms,
        "iff": parse_iff(_find_section(sections, "Iff")),
        "link16": parse_link16(_find_section(sections, "Link 16")),
        "ordnance": parse_ordnance(_find_section(sections, "Ordnance")),
        "weather": parse_weather(_find_section(sections, "Weather")),
        "support": parse_support(_find_section(sections, "Support")),
        "roe": parse_prose(_find_section(sections, "Rules of Engagement")),
        "emergency": emergency,
        "alternate_airfield": alternate,
        "airbases": airbases,
        "sections": sorted(name for name in sections if not name.startswith("_")),
    }


def parse_briefing(path: Path) -> dict:
    text = read_text(Path(path))
    data = parse_briefing_text(text)
    data["raw"] = text
    return data
