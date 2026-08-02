"""Turn IL-2's per-aircraft notes into readable kneeboard sections.

IL-2 ships, for every airframe, the text it shows in its own aircraft screen:
speed and load limits, maximum speeds by engine mode and altitude, climb rates,
sustained turn times, endurance, takeoff and landing speeds, engine power by mode,
and the recommended positions of the mixture, radiator, prop pitch and throttle
controls. That is exactly what a kneeboard should carry, and it needs no
interpretation -- only structuring.

Figures are metric. Imperial equivalents are added for the three units that are
unambiguous in context (km/h, m/s, and altitudes in metres) since the rest of the
board shows both. Load factors, angles, litres per minute and horsepower are left
alone: converting them would either be meaningless or need a guess about what the
number represents.
"""

from __future__ import annotations

import re

KMH_TO_MPH = 0.6213711922
KMH_TO_KT = 0.5399568035
M_TO_FT = 3.280839895
MS_TO_FPM = 196.850393701

# A short line ending in a colon opens a section. Numbered steps ("1. Starting
# the engine:") are headings too -- that is how the control-setting notes are laid
# out.
HEADING_RE = re.compile(r"^\s*(?:\d+\.\s*)?(.{2,70}?):\s*$")
BULLET_RE = re.compile(r"^\s*[-*•]\s*")
NOTE_RE = re.compile(r"^\s*Note\s*\d*\s*:", re.I)

# Ranges ("165..175 km/h") and single values both occur.
SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)(\.\.(\d+(?:\.\d+)?))?\s*km/h")
CLIMB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m/s")
ALT_RE = re.compile(r"(?<![\d.])(\d{3,5})\s*m(?![a-z/])")


def _fmt(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def _add_units(text: str) -> str:
    """Append imperial equivalents to the metric figures in a line."""

    def speed(match):
        low = float(match.group(1))
        if match.group(3):
            high = float(match.group(3))
            return (
                f"{match.group(1)}..{match.group(3)} km/h "
                f"({_fmt(low * KMH_TO_KT)}..{_fmt(high * KMH_TO_KT)} kt)"
            )
        return f"{match.group(1)} km/h ({_fmt(low * KMH_TO_MPH)} mph / {_fmt(low * KMH_TO_KT)} kt)"

    def climb(match):
        raw = float(match.group(1))
        return f"{match.group(1)} m/s ({_fmt(raw * MS_TO_FPM)} ft/min)"

    def altitude(match):
        raw = float(match.group(1))
        return f"{match.group(1)} m ({_fmt(raw * M_TO_FT)} ft)"

    text = SPEED_RE.sub(speed, text)
    text = CLIMB_RE.sub(climb, text)
    return ALT_RE.sub(altitude, text)


# IL-2 writes these notes from a fixed template, but most blocks carry no heading
# of their own. Labels are attached by matching the block's own opening phrase, so
# a block whose wording changes simply stays unheaded rather than being mislabelled.
BLOCK_LABELS = (
    ("Indicated stall speed", "Limits"),
    ("Maximum true air speed", "Maximum speed"),
    ("Service ceiling", "Ceiling and climb"),
    ("Maximum performance turn", "Turn performance"),
    ("Flight endurance", "Endurance"),
    ("Takeoff speed", "Takeoff and landing"),
    ("Water rated temperature", "Temperature limits"),
    ("Oil rated temperature", "Temperature limits"),
    ("Supercharger", "Supercharger"),
    ("Empty weight", "Weights"),
    ("Length:", "Dimensions"),
    ("Combat debut", "Service history"),
    ("Note 1", "Notes"),
)


def _label_for(items: list[dict]) -> str:
    if not items:
        return ""
    opening = items[0]["text"]
    for prefix, label in BLOCK_LABELS:
        if opening.startswith(prefix):
            return label
    return ""


def to_prose(description: str, convert: bool = True) -> list[dict]:
    """Structure the notes into the renderer's headed-group format."""
    if not description:
        return []

    groups: list[dict] = []
    current = {"heading": "", "items": []}

    for raw_line in description.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            # A blank line ends a section only if something is in it already.
            if current["heading"] or current["items"]:
                groups.append(current)
                current = {"heading": "", "items": []}
            continue

        bullet = bool(BULLET_RE.match(line.strip())) or line.startswith(("\t-", "    -"))
        body = BULLET_RE.sub("", line.strip())

        heading = HEADING_RE.match(line) if not bullet else None
        if heading and not NOTE_RE.match(line):
            if current["heading"] or current["items"]:
                groups.append(current)
            current = {"heading": heading.group(1).strip(), "items": []}
            continue

        if convert:
            body = _add_units(body)
        current["items"].append({"kind": "bullet" if bullet else "text", "text": body})

    if current["heading"] or current["items"]:
        groups.append(current)

    # A heading with nothing under it is an artefact of the source nesting a
    # heading directly inside another one, and renders as a stray title.
    groups = [g for g in groups if g["items"]]
    for group in groups:
        if not group["heading"]:
            group["heading"] = _label_for(group["items"])
    return groups


def summary(description: str) -> list[dict]:
    """Pull out the handful of figures worth a stat card.

    Only exact label matches are used. A missing figure is omitted rather than
    approximated from a related one.
    """
    if not description:
        return []

    wanted = (
        ("Dive limit", r"Dive speed limit:\s*([^\n]+)"),
        ("Max load", r"Maximum load factor:\s*([^\n]+)"),
        ("Stall (clean)", r"Indicated stall speed in flight configuration:\s*([^\n]+)"),
        ("Stall (landing)", r"Indicated stall speed in takeoff/landing configuration:\s*([^\n]+)"),
        ("Ceiling", r"Service ceiling:\s*([^\n]+)"),
        ("Takeoff", r"Takeoff speed:\s*([^\n]+)"),
        ("Landing", r"Landing speed:\s*([^\n]+)"),
        ("Endurance", r"Flight endurance[^:]*:\s*([^\n]+)"),
    )

    out = []
    for label, pattern in wanted:
        found = re.search(pattern, description, re.I)
        if found:
            out.append({"label": label, "value": _add_units(found.group(1).strip())})
    return out
