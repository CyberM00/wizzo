"""Read a DCS ``.miz`` into the same shape the kneeboard renders for BMS.

A ``.miz`` is a zip. The pieces that matter:

* ``mission``                  -- the whole mission as a Lua table
* ``l10n/DEFAULT/dictionary``  -- the strings the mission table refers to by key
* ``theatre``                  -- terrain name
* ``KNEEBOARD/<TYPE>/IMAGES/`` -- pre-rendered pages, when the generator adds them

Units are converted on the way out, because DCS stores metric internally and a
kneeboard is read in feet, knots and nautical miles.

One thing deliberately not attempted: waypoint latitude and longitude. DCS
positions are metres in a projection that differs per terrain, and converting
without the terrain's parameters would produce confident nonsense. Leg distance
and bearing *are* computed, because those are valid from the raw offsets.
"""

from __future__ import annotations

import zipfile
from datetime import timedelta
from math import atan2, degrees, hypot
from pathlib import Path

from . import luaparse

M_TO_FT = 3.280839895
MS_TO_KT = 1.943844492
M_TO_NM = 1.0 / 1852.0
MMHG_TO_INHG = 1.0 / 25.4
MMHG_TO_HPA = 1.33322387415
KG_TO_LB = 2.204622622

AIRCRAFT_KINDS = ("plane", "helicopter")
PLAYER_SKILLS = ("Player", "Client")

# Group tasks that indicate a support asset worth listing. Deliberately narrow:
# DCS group tasks do not reliably describe what a generated group actually does,
# so only the two that are unambiguous are surfaced. Labelling a BARCAP as
# "Transport" because that is what the task field said would be worse than
# leaving it out.
SUPPORT_TASKS = {
    "Refueling": "Tanker",
    "AWACS": "AWACS",
}

# Friendlier labels for waypoints the mission left unnamed.
POINT_TYPE_LABELS = {
    "TakeOffGround": "Takeoff",
    "TakeOffGroundHot": "Takeoff (hot)",
    "TakeOffParking": "Takeoff (parking)",
    "TakeOffParkingHot": "Takeoff (parking, hot)",
    "TakeOff": "Takeoff",
    "Land": "Land",
    "Turning Point": "Waypoint",
    "Turning point": "Waypoint",
}


class MissionError(ValueError):
    """The file was not a readable DCS mission."""


def _read_lua(archive: zipfile.ZipFile, name: str, root: str):
    try:
        raw = archive.read(name)
    except KeyError:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return luaparse.load_assignments(text).get(root)
    except luaparse.LuaParseError as exc:
        raise MissionError(f"could not parse {name}: {exc}") from exc


def _fmt_clock(seconds: float | int | None) -> str:
    if not seconds and seconds != 0:
        return ""
    try:
        total = int(seconds) % 86400
    except (TypeError, ValueError):
        return ""
    return str(timedelta(seconds=total))[:8].rjust(8, "0") + "z"


def _num(value, default=None):
    return value if isinstance(value, (int, float)) else default


class DcsMission:
    """A parsed mission, exposed in the kneeboard's payload shape."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.archive = zipfile.ZipFile(self.path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise MissionError(f"{self.path.name} is not a readable .miz: {exc}") from exc

        self.mission = _read_lua(self.archive, "mission", "mission")
        if not isinstance(self.mission, dict):
            raise MissionError("the mission file contained no mission table")
        self.dictionary = _read_lua(self.archive, "l10n/DEFAULT/dictionary", "dictionary") or {}
        try:
            self.theatre = self.archive.read("theatre").decode("utf-8", "replace").strip()
        except KeyError:
            self.theatre = str(self.mission.get("theatre", ""))

        self.player = self._find_player()

    # -- helpers ---------------------------------------------------------

    def text(self, key) -> str:
        """Resolve a DictKey_ reference, or pass a literal through."""
        if not isinstance(key, str):
            return ""
        if key.startswith("DictKey_"):
            return str(self.dictionary.get(key, ""))
        return key

    def _groups(self, side: str):
        coalition = (self.mission.get("coalition") or {}).get(side) or {}
        for country in coalition.get("country") or []:
            if not isinstance(country, dict):
                continue
            for kind in AIRCRAFT_KINDS:
                block = country.get(kind) or {}
                for group in block.get("group") or []:
                    if isinstance(group, dict):
                        yield country.get("name", ""), kind, group

    def _find_player(self) -> dict | None:
        best = None
        for side in ("blue", "red"):
            for country, kind, group in self._groups(side):
                for unit in group.get("units") or []:
                    if not isinstance(unit, dict):
                        continue
                    skill = unit.get("skill")
                    if skill not in PLAYER_SKILLS:
                        continue
                    entry = {"side": side, "country": country, "kind": kind, "group": group, "unit": unit}
                    if skill == "Player":
                        return entry
                    best = best or entry
        return best

    # -- payload sections ------------------------------------------------

    def overview(self) -> dict:
        unit = (self.player or {}).get("unit") or {}
        group = (self.player or {}).get("group") or {}

        callsign = unit.get("callsign")
        if isinstance(callsign, dict):
            callsign_name = str(callsign.get("name", "") or "")
        else:
            callsign_name = str(callsign or "")

        start = _num(self.mission.get("start_time"), 0)
        date = self.mission.get("date") or {}
        date_text = ""
        if isinstance(date, dict) and date.get("Year"):
            date_text = f"{int(date.get('Year',0)):04d}-{int(date.get('Month',0)):02d}-{int(date.get('Day',0)):02d}"

        # The target time is the ETA of the last waypoint that has one before
        # the route turns for home.
        tot = ""
        points = self._route_points()
        target_eta = next((p["eta_raw"] for p in reversed(points) if p.get("is_target") and p["eta_raw"]), 0)
        if target_eta:
            tot = _fmt_clock(start + target_eta)

        return {
            "flight": callsign_name or str(group.get("name", "")).split("|")[0],
            "role": str(group.get("task", "")),
            "fields": {},
            "package": "",
            "package_type": "",
            "mission": self.text(self.mission.get("sortie")) or self.path.stem,
            "target_area": "",
            "time_on_target": tot,
            "sunrise": "",
            "sunset": "",
            "target_icao": "",
            "aircraft_type": str(unit.get("type", "")),
            "theatre": self.theatre,
            "mission_date": date_text,
            "start_time": _fmt_clock(start),
            "group_name": str(group.get("name", "")),
        }

    def briefing_prose(self) -> list[dict]:
        """The mission description, split into headed groups."""
        body = self.text(self.mission.get("descriptionText"))
        if not body.strip():
            return []

        groups: list[dict] = []
        current = {"heading": "", "items": []}
        lines = body.replace("\r\n", "\n").split("\n")
        for i, raw in enumerate(lines):
            line = raw.rstrip()
            if not line.strip():
                continue
            # Retribution underlines its headings with a row of '='.
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if set(line.strip()) <= {"=", "-"} and len(line.strip()) > 3:
                continue
            if nxt and set(nxt) <= {"="} and len(nxt) > 3:
                if current["heading"] or current["items"]:
                    groups.append(current)
                current = {"heading": line.strip().rstrip(":"), "items": []}
                continue
            stripped = line.strip()
            if stripped.startswith(("-", "*", "•")):
                current["items"].append({"kind": "bullet", "text": stripped.lstrip("-*• ").strip()})
            else:
                current["items"].append({"kind": "text", "text": stripped})
        if current["heading"] or current["items"]:
            groups.append(current)
        return groups

    def _route_points(self) -> list[dict]:
        group = (self.player or {}).get("group") or {}
        raw = ((group.get("route") or {}).get("points")) or []
        points: list[dict] = []
        prev = None
        for i, p in enumerate(raw, 1):
            if not isinstance(p, dict):
                continue
            x, y = _num(p.get("x")), _num(p.get("y"))
            leg_nm = heading = None
            if prev and x is not None and y is not None:
                dx, dy = x - prev[0], y - prev[1]
                leg_nm = hypot(dx, dy) * M_TO_NM
                # DCS x is north, y is east.
                heading = (degrees(atan2(dy, dx)) + 360.0) % 360.0
            if x is not None and y is not None:
                prev = (x, y)

            name = str(p.get("name") or "")
            ptype = str(p.get("type") or "")
            action = str(p.get("action") or "")
            eta = _num(p.get("ETA"), 0) or 0
            alt_m = _num(p.get("alt"), 0) or 0
            speed_ms = _num(p.get("speed"), 0) or 0

            points.append(
                {
                    "index": i,
                    "name": self.text(name) or name,
                    "type": ptype,
                    "action": action,
                    "eta_raw": eta,
                    "alt_ft": round(alt_m * M_TO_FT) if alt_m else 0,
                    "speed_kt": round(speed_ms * MS_TO_KT) if speed_ms else 0,
                    "leg_nm": round(leg_nm, 1) if leg_nm else None,
                    "heading": round(heading) if heading is not None else None,
                    # Target legs are the ones the generator names after objects
                    # rather than phases of flight.
                    "is_target": bool(name) and name.upper() not in {
                        "HOLD", "JOIN", "NAV", "INGRESS", "SPLIT", "EGRESS",
                        "REFUEL", "LANDING", "BULLSEYE", "TAKEOFF",
                    },
                }
            )
        return points

    def steerpoints(self) -> list[dict]:
        start = _num(self.mission.get("start_time"), 0) or 0
        out = []
        for p in self._route_points():
            out.append(
                {
                    "index": str(p["index"]),
                    "description": p["name"] or POINT_TYPE_LABELS.get(p["type"], p["type"]),
                    "time": _fmt_clock(start + p["eta_raw"]) if p["eta_raw"] else "--",
                    "distance": f"{p['leg_nm']}" if p["leg_nm"] is not None else "--",
                    "heading": f"{p['heading']:03d}" if p["heading"] is not None else "--",
                    "cas": str(p["speed_kt"]) if p["speed_kt"] else "--",
                    "altitude": f"{p['alt_ft']:,}" if p["alt_ft"] else "--",
                    "action": p["action"],
                    "formation": "",
                    # DCS repeats the action in the type for ordinary waypoints;
                    # showing it twice adds a column of noise.
                    "comments": "" if p["type"] == p["action"] else p["type"],
                    "extra": [],
                }
            )
        return out

    def comms(self) -> dict:
        unit = (self.player or {}).get("unit") or {}
        group = (self.player or {}).get("group") or {}
        rows: list[dict] = []

        freq = _num(group.get("frequency"))
        if freq:
            rows.append(
                {
                    "agency": "Flight",
                    "callsign": str(group.get("name", "")).split("|")[0],
                    "uhf": f"{freq:.3f} MHz",
                    "vhf": "--",
                    "notes": "Group frequency",
                    "group": "flight",
                }
            )

        for side in ("blue",):
            for country, kind, g in self._groups(side):
                task = str(g.get("task", ""))
                if task not in SUPPORT_TASKS:
                    continue
                f = _num(g.get("frequency"))
                rows.append(
                    {
                        "agency": SUPPORT_TASKS[task],
                        "callsign": str(g.get("name", "")).split("|")[0],
                        "uhf": f"{f:.3f} MHz" if f else "--",
                        "vhf": "--",
                        "notes": task,
                        "group": "general",
                    }
                )

        return {"rows": rows, "airbases": {}, "tanker_tacan": ""}

    def support(self) -> list[dict]:
        """Tanker and AWACS groups on the player's side."""
        side = (self.player or {}).get("side") or "blue"
        out = []
        for country, kind, group in self._groups(side):
            task = str(group.get("task", ""))
            label = SUPPORT_TASKS.get(task)
            if not label:
                continue
            freq = _num(group.get("frequency"))
            units = group.get("units") or []
            first = units[0] if units and isinstance(units[0], dict) else {}
            callsign = first.get("callsign")
            if isinstance(callsign, dict):
                callsign_name = str(callsign.get("name", "") or "")
            else:
                callsign_name = ""
            # A tanker's TACAN channel is stored as a unit property when set.
            props = first.get("AddPropAircraft") or {}
            tacan = ""
            if isinstance(props, dict):
                chan = props.get("TACAN_Channel") or props.get("tacan_channel")
                band = props.get("TACAN_Band") or props.get("tacan_band") or ""
                if chan:
                    tacan = f"{chan}{band}"
            out.append(
                {
                    "callsign": callsign_name or str(group.get("name", "")).split("|")[0],
                    "kind": label,
                    "asset": str(first.get("type", "")),
                    "detail": f"{len(units)} aircraft"
                    + (f", {freq:.3f} MHz" if freq else ""),
                    "tacan": tacan,
                }
            )
        return out

    def radio_presets(self) -> list[dict]:
        """The aircraft's programmed preset channels, one block per radio."""
        unit = (self.player or {}).get("unit") or {}
        radios = unit.get("Radio")
        if not isinstance(radios, list):
            return []
        out = []
        for i, radio in enumerate(radios, 1):
            if not isinstance(radio, dict):
                continue
            channels = radio.get("channels")
            if not isinstance(channels, list):
                continue
            presets = [
                {"preset": n, "frequency": f"{c:.3f}" if isinstance(c, (int, float)) else str(c), "comment": "", "open": False}
                for n, c in enumerate(channels, 1)
                if c
            ]
            if presets:
                out.append({"radio": f"Radio {i}", "presets": presets})
        return out

    def weather(self) -> dict:
        w = self.mission.get("weather") or {}
        clouds = w.get("clouds") or {}
        wind = w.get("wind") or {}
        vis = w.get("visibility") or {}
        temp = (w.get("season") or {}).get("temperature")
        qnh_mmhg = _num(w.get("qnh"))

        def wind_row(key, label):
            block = wind.get(key) or {}
            speed = _num(block.get("speed"))
            direction = _num(block.get("dir"))
            if speed is None or direction is None:
                return None
            # DCS stores the direction the wind blows toward, rotated 180 from
            # the meteorological convention the Mission Editor displays. The
            # "from" wording is kept in the value so the convention is explicit
            # rather than something the reader has to assume.
            from_dir = (direction + 180) % 360
            return {
                "label": label,
                "values": [f"from {from_dir:03.0f}deg @ {speed * MS_TO_KT:.0f} kts"],
            }

        rows = []
        base_m = _num(clouds.get("base"))
        thick_m = _num(clouds.get("thickness"))
        precip = _num(clouds.get("iprecptns"), 0)
        rows.append({"label": "Situation", "values": [
            {0: "Clear", 1: "Rain", 2: "Thunderstorm", 3: "Snow", 4: "Snowstorm"}.get(int(precip or 0), "Clear")
        ]})
        for key, label in (("atGround", "Wind surface"), ("at2000", "Wind 2000m"), ("at8000", "Wind 8000m")):
            row = wind_row(key, label)
            if row:
                rows.append(row)
        if _num(vis.get("distance")) is not None:
            rows.append({"label": "Visibility", "values": [f"{vis['distance'] / 1000:.0f} km"]})
        if temp is not None:
            rows.append({"label": "Temp", "values": [f"{temp:.0f}deg C"]})
        if base_m:
            rows.append({"label": "Cloud base", "values": [f"{base_m * M_TO_FT:,.0f} ft MSL"]})
        if thick_m:
            rows.append({"label": "Cloud thickness", "values": [f"{thick_m * M_TO_FT:,.0f} ft"]})
        if qnh_mmhg:
            rows.append({"label": "QNH", "values": [
                f"{qnh_mmhg * MMHG_TO_INHG:.2f} inHg / {qnh_mmhg * MMHG_TO_HPA:.0f} hPa"
            ]})

        return {"headers": ["Mission"], "rows": rows}

    def pylons(self) -> list[dict]:
        """The player's stations, in order."""
        unit = (self.player or {}).get("unit") or {}
        payload = unit.get("payload") or {}
        pylons = payload.get("pylons") or {}
        if isinstance(pylons, list):
            pairs = list(enumerate(pylons, 1))
        else:
            pairs = sorted(pylons.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
        out = []
        for station, entry in pairs:
            clsid = ""
            if isinstance(entry, dict):
                clsid = str(entry.get("CLSID", ""))
            if clsid:
                out.append({"station": str(station), "clsid": clsid})
        return out

    def consumables(self) -> dict:
        unit = (self.player or {}).get("unit") or {}
        payload = unit.get("payload") or {}
        fuel_kg = _num(payload.get("fuel"))
        return {
            "fuel_lb": round(fuel_kg * KG_TO_LB) if fuel_kg else None,
            "fuel_kg": round(fuel_kg) if fuel_kg else None,
            "flare": _num(payload.get("flare")),
            "chaff": _num(payload.get("chaff")),
            "gun_percent": _num(payload.get("gun")),
        }

    def kneeboard_pages(self) -> list[dict]:
        """Pre-rendered kneeboard images the mission generator embedded."""
        pages = []
        for name in self.archive.namelist():
            upper = name.upper()
            if not upper.startswith("KNEEBOARD/"):
                continue
            if not upper.endswith((".PNG", ".JPG", ".JPEG")):
                continue
            parts = name.split("/")
            aircraft = parts[1] if len(parts) > 2 else ""
            pages.append(
                {
                    "name": parts[-1],
                    "aircraft": aircraft,
                    "entry": name,
                    "size_kb": round(self.archive.getinfo(name).file_size / 1024),
                }
            )
        pages.sort(key=lambda p: (p["aircraft"], p["name"]))
        return pages

    def read_entry(self, entry: str) -> bytes:
        return self.archive.read(entry)

    def bullseye(self) -> dict:
        side = (self.player or {}).get("side") or "blue"
        coalition = (self.mission.get("coalition") or {}).get(side) or {}
        be = coalition.get("bullseye") or {}
        return {"x": _num(be.get("x")), "y": _num(be.get("y")), "side": side}
