"""Read an IL-2 mission into the shape the kneeboard renders.

Everything comes from the mission text file plus the localization file beside it.
Units are converted on the way out and shown in both systems: IL-2 aircraft are
metric for German, Soviet, French and Italian types and imperial for British and
American ones, and which one you are reading depends on the airframe, so asserting
one would be wrong half the time.

Two structural facts about the format worth stating, because both are
counter-intuitive and both were established by measurement:

* The player's route is drawn with ``MCU_Icon`` blocks whose ``LineType`` is 14,
  chained through ``Targets``. ``MCU_Waypoint`` blocks exist but reference AI
  flights only -- none of the 55 in the sample points at the player.
* ``Chart`` blocks are taxi routes nested inside ``Airfield``, not flight plans.

Positions are metres on a flat map with **X north and Z east**, verified by
recomputing bearings against the orientations the game itself stores.
"""

from __future__ import annotations

import re
from math import atan2, degrees, hypot
from pathlib import Path

from . import missionfile as mf
from .localization import Localization, to_lines

M_TO_FT = 3.280839895
M_TO_NM = 1.0 / 1852.0
M_TO_KM = 1.0 / 1000.0
MS_TO_KT = 1.943844492
KMH_TO_KT = 0.5399568035
MMHG_TO_INHG = 1.0 / 25.4
MMHG_TO_HPA = 1.33322387415

WANTED_BLOCKS = ("Options", "Plane", "MCU_Icon", "Airfield", "MCU_Waypoint")

ROUTE_LINE_TYPE = 14
TAKEOFF_ICON_ID = 903
MAX_ROUTE_POINTS = 60

PRECIPITATION = {0: "Clear", 1: "Rain", 2: "Snow"}

# Descriptions carry labelled lines the steerpoint table has columns for.
DESC_FIELDS = (
    ("formation", re.compile(r"^Formation:\s*(.+)$", re.I)),
    ("speed", re.compile(r"^Speed to waypoint:\s*(.+)$", re.I)),
    ("altitude", re.compile(r"^Altitude at waypoint:\s*(.+)$", re.I)),
)

MissionError = mf.MissionError


def _both_alt(metres: float | None) -> str:
    if metres is None:
        return "--"
    return f"{metres:,.0f} m / {metres * M_TO_FT:,.0f} ft"


def _both_dist(metres: float | None) -> str:
    if metres is None:
        return "--"
    return f"{metres * M_TO_KM:,.1f} km / {metres * M_TO_NM:,.1f} nm"


class Il2Mission:
    """One parsed IL-2 mission."""

    def __init__(self, path: Path, language: str = "eng", text: str | None = None,
                 localization: Localization | None = None):
        self.path = Path(path)
        self.language = language
        raw = text if text is not None else mf.read_mission(self.path)
        self.blocks = mf.scan(raw, WANTED_BLOCKS)

        if not self.blocks.get("Options"):
            raise MissionError(f"{self.path.name} has no Options block; it is not a mission file")

        self.options = mf.fields(self.blocks["Options"][0])
        self.localization = localization or Localization.for_mission(self.path, language)

        self.planes = [mf.fields(body) for body in self.blocks.get("Plane", [])]
        self.icons = [mf.fields(body) for body in self.blocks.get("MCU_Icon", [])]
        self.airfield_bodies = self.blocks.get("Airfield", [])

        self.player = self._find_player()
        self.route_warnings: list[str] = []
        self._route = None

    # -- player ----------------------------------------------------------

    def _find_player(self) -> dict | None:
        """The player's aircraft carries ``AILevel = 0``; AI planes never do."""
        candidates = [p for p in self.planes if mf.as_int(p.get("AILevel")) == 0]
        if not candidates:
            return None
        if len(candidates) > 1:
            # Co-op missions could in principle have several human slots; take the
            # first and say so rather than guessing which is "yours".
            candidates.sort(key=lambda p: mf.as_int(p.get("NumberInFormation"), 99))
        return candidates[0]

    @property
    def player_script(self) -> str:
        return mf.as_str((self.player or {}).get("Script"))

    @property
    def aircraft_code(self) -> str:
        stem = self.player_script.replace("\\", "/").rsplit("/", 1)[-1]
        return stem[:-4] if stem.lower().endswith(".txt") else stem

    def flight(self) -> dict:
        """The player's own details, and the flight they belong to."""
        player = self.player or {}
        country = mf.as_int(player.get("Country"))
        return {
            "pilot": mf.as_str(player.get("Name")),
            "callsign_id": mf.as_int(player.get("Callsign")),
            "callnum": mf.as_int(player.get("Callnum")),
            "formation_number": mf.as_int(player.get("NumberInFormation")),
            "country": country,
            "aircraft_code": self.aircraft_code,
            "payload_id": mf.as_int(player.get("PayloadId")),
            "weapon_mask": mf.as_str(player.get("WMMask")),
            "fuel": mf.as_float(player.get("Fuel")),
            "start_in_air": mf.as_int(player.get("StartInAir")),
            "index": mf.as_int(player.get("Index")),
            "link_id": mf.as_int(player.get("LinkTrId")),
            "position": self._position(player),
        }

    def squadron(self) -> list[dict]:
        """The player's flight: same callsign and same country.

        Not the same aircraft type -- a flight can mix variants, and this one runs
        Bf 109 G-2s alongside G-4s. Nor callsign alone: callsign numbers repeat
        across coalitions, and a Soviet La-5 flight here shares the player's.
        """
        player = self.player or {}
        callsign = mf.as_int(player.get("Callsign"))
        country = mf.as_int(player.get("Country"))
        if callsign is None:
            return []
        out = []
        for plane in self.planes:
            if mf.as_int(plane.get("Callsign")) != callsign:
                continue
            if mf.as_int(plane.get("Country")) != country:
                continue
            # AI names carry ",configId,skill"; the player's is a bare name.
            name = mf.as_str(plane.get("Name")).split(",")[0].strip()
            stem = mf.as_str(plane.get("Script")).replace("\\", "/").rsplit("/", 1)[-1]
            out.append(
                {
                    "name": name,
                    "callnum": mf.as_int(plane.get("Callnum")),
                    "position": mf.as_int(plane.get("NumberInFormation")),
                    "ai_level": mf.as_int(plane.get("AILevel")),
                    "aircraft_code": stem[:-4] if stem.lower().endswith(".txt") else stem,
                    "is_player": plane is self.player,
                }
            )
        out.sort(key=lambda entry: (entry["position"] if entry["position"] is not None else 99))
        return out

    @staticmethod
    def _position(block: dict) -> tuple[float | None, float | None, float | None]:
        return (
            mf.as_float(block.get("XPos")),
            mf.as_float(block.get("YPos")),
            mf.as_float(block.get("ZPos")),
        )

    # -- overview --------------------------------------------------------

    def title(self) -> str:
        return self.localization.get(self.options.get("LCName"))

    def briefing_prose(self) -> list[dict]:
        return self.localization.prose(self.options.get("LCDesc"))

    def theatre(self) -> str:
        raw = mf.as_str(self.options.get("GuiMap"))
        if not raw:
            return ""
        parts = [p for p in re.split(r"[-_]", raw) if p]
        return " ".join(part.capitalize() for part in parts)

    def mission_date(self) -> str:
        raw = mf.as_str(self.options.get("Date"))
        parts = [p for p in raw.split(".") if p.strip().isdigit()]
        if len(parts) != 3:
            return raw
        day, month, year = (int(p) for p in parts)
        return f"{year:04d}-{month:02d}-{day:02d}"

    def start_time(self) -> str:
        raw = mf.as_str(self.options.get("Time"))
        parts = [p for p in raw.split(":") if p.strip().isdigit()]
        if not parts:
            return raw
        padded = (parts + ["0", "0", "0"])[:3]
        return ":".join(f"{int(p):02d}" for p in padded)

    # -- weather ---------------------------------------------------------

    def wind_layers(self) -> list[dict]:
        """``WindLayers`` rows of ``altitude_m : direction_deg : speed_ms``."""
        body = self.blocks["Options"][0]
        block = re.search(r"WindLayers\s*\{(.*?)\}", body, re.S)
        if not block:
            return []
        layers = []
        for line in block.group(1).splitlines():
            parts = [p.strip() for p in line.strip().rstrip(";").split(":")]
            if len(parts) != 3:
                continue
            altitude = mf.as_float(parts[0])
            direction = mf.as_float(parts[1])
            speed = mf.as_float(parts[2])
            if altitude is None or direction is None or speed is None:
                continue
            layers.append({"altitude_m": altitude, "direction": direction, "speed_ms": speed})
        return layers

    def weather(self) -> dict:
        options = self.options
        rows: list[dict] = []

        precipitation = mf.as_int(options.get("PrecType"), 0) or 0
        level = mf.as_int(options.get("PrecLevel"), 0) or 0
        situation = PRECIPITATION.get(precipitation, "Clear")
        if precipitation and level:
            situation += f" (intensity {level})"
        rows.append({"label": "Situation", "values": [situation]})

        for layer in self.wind_layers():
            knots = layer["speed_ms"] * MS_TO_KT
            rows.append(
                {
                    "label": f"Wind at {layer['altitude_m']:,.0f} m",
                    # IL-2's stored convention -- whether this is the direction the
                    # wind comes from or blows toward -- is not confirmed, so the
                    # figure is given as the file states it, without a claim.
                    "values": [
                        f"dir {layer['direction']:03.0f} deg @ "
                        f"{layer['speed_ms']:.0f} m/s / {knots:.0f} kt"
                    ],
                }
            )

        cloud_base = mf.as_float(options.get("CloudLevel"))
        cloud_thick = mf.as_float(options.get("CloudHeight"))
        if cloud_base is not None:
            rows.append({"label": "Cloud base", "values": [_both_alt(cloud_base)]})
        if cloud_thick is not None:
            rows.append({"label": "Cloud thickness", "values": [_both_alt(cloud_thick)]})

        config = mf.as_str(options.get("CloudConfig"))
        if config:
            preset = config.replace("\\", "/").split("/")
            if len(preset) > 1:
                rows.append({"label": "Cloud preset", "values": [preset[-2].replace("_", " ")]})

        temperature = mf.as_float(options.get("Temperature"))
        if temperature is not None:
            rows.append(
                {
                    "label": "Temperature",
                    "values": [f"{temperature:,.0f} C / {temperature * 9 / 5 + 32:,.0f} F"],
                }
            )

        pressure = mf.as_float(options.get("Pressure"))
        if pressure is not None:
            rows.append(
                {
                    "label": "Pressure",
                    "values": [
                        f"{pressure:,.0f} mmHg / {pressure * MMHG_TO_INHG:.2f} inHg / "
                        f"{pressure * MMHG_TO_HPA:,.0f} hPa"
                    ],
                }
            )

        haze = mf.as_float(options.get("Haze"))
        if haze is not None:
            rows.append({"label": "Haze", "values": [f"{haze:.2f}"]})
        turbulence = mf.as_float(options.get("Turbulence"))
        if turbulence is not None:
            rows.append(
                {"label": "Turbulence", "values": ["none" if not turbulence else f"{turbulence:.2f}"]}
            )
        sea = mf.as_int(options.get("SeaState"))
        if sea is not None:
            rows.append({"label": "Sea state", "values": ["calm" if not sea else str(sea)]})

        return {"headers": ["Mission"], "rows": rows}

    # -- route -----------------------------------------------------------

    def route(self) -> list[dict]:
        if self._route is None:
            self._route = self._build_route()
        return self._route

    def _route_icons(self) -> list[dict]:
        return [i for i in self.icons if mf.as_int(i.get("LineType")) == ROUTE_LINE_TYPE]

    def _build_route(self) -> list[dict]:
        icons = self._route_icons()
        source = f"MCU_Icon LineType {ROUTE_LINE_TYPE}"

        if not icons:
            # Any named, chained icon is a weaker but better-than-nothing signal.
            icons = [i for i in self.icons if self.localization.get(i.get("LCName"))]
            source = "named MCU_Icon blocks"
            if icons:
                self.route_warnings.append(
                    "No route icons of the usual type were found, so the flight plan was "
                    "reconstructed from any named map icons. Check it against your in-game map."
                )

        if not icons:
            self.route_warnings.append(
                "This mission draws no route icons and its waypoints reference AI flights "
                "only, so no flight plan could be read. Use the in-game map."
            )
            return []

        # Resolve each marker's name once so ordering can use it.
        for icon in icons:
            icon["_name"] = self.localization.get(icon.get("LCName"))

        chains = _chains(icons)
        if len(chains) > 1:
            chains.sort(key=lambda chain: _distance_to_player(chain, self.flight()["position"]))
            self.route_warnings.append(
                f"{len(chains)} separate routes are drawn on this map; the one nearest your "
                "start position was chosen. Verify it is your flight's."
            )
        chain = chains[0] if chains else []

        points = []
        previous = None
        for order, icon in enumerate(chain[:MAX_ROUTE_POINTS], start=1):
            x, y, z = self._position(icon)
            leg = heading = None
            if previous and x is not None and z is not None:
                dx, dz = x - previous[0], z - previous[1]
                leg = hypot(dx, dz)
                # X is north, Z is east -- verified against the icons' own YOri.
                heading = (degrees(atan2(dz, dx)) + 360.0) % 360.0
            if x is not None and z is not None:
                previous = (x, z)

            name = self.localization.get(icon.get("LCName"))
            detail = _describe(to_lines(self.localization.get(icon.get("LCDesc"))))
            points.append(
                {
                    "order": order,
                    "name": name or detail.get("kind", "") or "Waypoint",
                    "kind": detail.get("kind", ""),
                    "formation": detail.get("formation", ""),
                    "speed": detail.get("speed", ""),
                    "altitude_text": detail.get("altitude", ""),
                    "comments": detail.get("comments", ""),
                    "altitude_m": y,
                    "leg_m": leg,
                    "heading": heading,
                    "position": (x, y, z),
                }
            )
        return points

    def steerpoints(self) -> list[dict]:
        out = []
        for point in self.route():
            out.append(
                {
                    "index": str(point["order"]),
                    "description": point["name"],
                    # IL-2 records no estimated time at any waypoint; deriving one
                    # from distance and commanded speed would be invention.
                    "time": "--",
                    "distance": _both_dist(point["leg_m"]),
                    "heading": f"{point['heading']:03.0f}" if point["heading"] is not None else "--",
                    "cas": point["speed"] or "--",
                    "altitude": point["altitude_text"] or _both_alt(point["altitude_m"]),
                    "action": point["kind"],
                    "formation": point["formation"],
                    "comments": point["comments"],
                    "extra": [],
                }
            )
        return out

    # -- airfields -------------------------------------------------------

    def airfields(self) -> list[dict]:
        out = []
        for body in self.airfield_bodies:
            data = mf.fields(body)
            x, y, z = self._position(data)
            # The Chart block contains nested Point blocks, so a non-greedy match
            # on its own braces stops at the first Point. Scan for Points from
            # where Chart begins instead -- they are the only thing inside it.
            chart_at = body.find("Chart")
            points = []
            if chart_at != -1:
                for match in re.finditer(
                    r"Point\s*\{(.*?)\}", body[chart_at:], re.S
                ):
                    fields = mf.fields(match.group(1) + ";")
                    px, py = mf.as_float(fields.get("X")), mf.as_float(fields.get("Y"))
                    if px is None or py is None:
                        continue
                    points.append(
                        {"type": mf.as_int(fields.get("Type"), 0), "x": px, "y": py}
                    )
            out.append(
                {
                    "name": mf.as_str(data.get("Name")),
                    "callsign_id": mf.as_int(data.get("Callsign")),
                    "callnum": mf.as_int(data.get("Callnum")),
                    "position": (x, y, z),
                    "taxi_points": points,
                }
            )
        return out

    def nearest_airfield(self, position) -> dict | None:
        x, _, z = position
        if x is None or z is None:
            return None
        best = None
        best_distance = None
        for field in self.airfields():
            fx, _, fz = field["position"]
            if fx is None or fz is None:
                continue
            distance = hypot(fx - x, fz - z)
            if best_distance is None or distance < best_distance:
                best, best_distance = field, distance
        if best is not None:
            best = dict(best, distance_m=best_distance)
        return best


# ------------------------------------------------------------- helpers


def _describe(lines: list[str]) -> dict:
    """Pull labelled values out of a waypoint description."""
    out: dict[str, str] = {}
    leftovers: list[str] = []
    for index, line in enumerate(lines):
        matched = False
        for key, pattern in DESC_FIELDS:
            found = pattern.match(line)
            if found:
                out[key] = found.group(1).strip()
                matched = True
                break
        if matched:
            continue
        if index == 0:
            out["kind"] = line
        else:
            leftovers.append(line)
    if leftovers:
        out["comments"] = "; ".join(leftovers)
    return out


def _chains(icons: list[dict]) -> list[list[dict]]:
    """Group icons into connected chains and order each one.

    The chain is a closed cycle in practice, so it has no natural head and a naive
    walk would loop. Head selection is ordered deliberately: a numeric naming
    scheme is authorial and trustworthy, whereas "nearest the player" is tried last
    because it was measured picking a *landing* point and reversing the route.
    """
    by_index = {mf.as_int(i.get("Index")): i for i in icons if mf.as_int(i.get("Index")) is not None}
    targets = {
        index: [t for t in mf.as_int_list(icon.get("Targets")) if t in by_index]
        for index, icon in by_index.items()
    }

    unvisited = set(by_index)
    chains: list[list[dict]] = []

    while unvisited:
        # Collect one connected component.
        start = next(iter(unvisited))
        component: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            for neighbour in targets.get(node, []):
                if neighbour not in component:
                    stack.append(neighbour)
            for other, links in targets.items():
                if node in links and other not in component:
                    stack.append(other)
        unvisited -= component

        members = [by_index[i] for i in component]
        chains.append(_order_chain(members, targets, by_index))

    chains.sort(key=len, reverse=True)
    return chains


TAKEOFF_NAME_RE = re.compile(r"^\s*(take\s*off|start|departure)\b", re.I)


def _order_chain(members: list[dict], targets: dict, by_index: dict) -> list[dict]:
    """Put one connected chain into flying order.

    The chain is a closed cycle in practice, so no node lacks an incoming edge and
    there is no natural head. Rules are tried in this order for a reason:
    a wholly numeric naming scheme is the mission author's own ordering and is
    unambiguous, while "start nearest the player" is last because it was measured
    selecting a *landing* point and silently reversing the route.
    """
    names = {mf.as_int(i.get("Index")): (i.get("_name") or "").strip() for i in members}

    # 1. Every marker named with a number: that is the author's sequence.
    if names and all(name.isdigit() for name in names.values()):
        return sorted(members, key=lambda i: int(names[mf.as_int(i.get("Index"))]))

    # 2. An explicit takeoff marker, by icon id or by name.
    head = None
    for icon in members:
        index = mf.as_int(icon.get("Index"))
        if mf.as_int(icon.get("IconId")) == TAKEOFF_ICON_ID or TAKEOFF_NAME_RE.match(names.get(index, "")):
            head = index
            break

    # 3. A node with no incoming edge, if the chain happens to be open.
    if head is None:
        indices = [mf.as_int(i.get("Index")) for i in members]
        incoming = {i: 0 for i in indices}
        for index in indices:
            for target in targets.get(index, []):
                if target in incoming:
                    incoming[target] += 1
        head = next((i for i in indices if incoming[i] == 0), None)

    # 4. File order.
    if head is None:
        head = mf.as_int(members[0].get("Index"))

    return _walk_from(head, members, targets, by_index)


def _walk_from(head: int, members: list[dict], targets: dict, by_index: dict) -> list[dict]:
    ordered: list[dict] = []
    seen: set[int] = set()
    node = head
    # The cycle makes a `seen` guard mandatory, not merely defensive.
    while node is not None and node not in seen and len(ordered) < MAX_ROUTE_POINTS:
        seen.add(node)
        ordered.append(by_index[node])
        following = [t for t in targets.get(node, []) if t not in seen]
        node = following[0] if following else None

    for icon in members:
        index = mf.as_int(icon.get("Index"))
        if index not in seen:
            ordered.append(icon)
    return ordered


def _distance_to_player(chain: list[dict], position) -> float:
    x, _, z = position
    if x is None or z is None or not chain:
        return float("inf")
    best = float("inf")
    for icon in chain:
        ix, iz = mf.as_float(icon.get("XPos")), mf.as_float(icon.get("ZPos"))
        if ix is None or iz is None:
            continue
        best = min(best, hypot(ix - x, iz - z))
    return best
