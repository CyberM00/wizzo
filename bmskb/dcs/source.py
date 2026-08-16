"""Build the kneeboard payload from a DCS mission.

Produces the same top-level shape the BMS side does, so the front end renders
most pages unchanged. Where DCS genuinely has no equivalent -- IFF rotation,
Link 16 files, approach plates, a threat brief -- the section is left empty and
the reason is reported as a warning rather than padded out.
"""

from __future__ import annotations

from math import atan2, degrees, hypot
from pathlib import Path

from .install import DcsInstall
from .mission import DcsMission, MissionError
from .weapons import DcsWeaponLibrary

M_TO_NM = 1.0 / 1852.0


def _theatre_map(install: DcsInstall, mission: DcsMission) -> dict:
    """Metadata for the theatre chart, and the mission's own positions on it.

    The image is not built here -- it is stitched and cached the first time it is
    actually requested, so opening any other page never pays for it. What is done
    here is cheap: reading the tile index, solving the terrain's projection from
    its beacon table, and working out which crop the route needs.
    """
    from . import maps

    theatre = mission.theatre
    try:
        terrain = maps.TerrainCharts(install.base if install else None, theatre)
    except Exception as exc:  # noqa: BLE001 - never let a map problem break the board
        return {"available": False, "error": str(exc), "theatre": theatre}
    if not terrain.ok:
        return {"available": False, "error": terrain.error, "theatre": theatre}

    route = mission.route_positions()
    bullseye = mission.bullseye()
    fields = terrain.airfields()

    anchors = [(p["x"], p["z"]) for p in route]
    if bullseye.get("x") is not None and bullseye.get("y") is not None:
        anchors.append((bullseye["x"], bullseye["y"]))
    if not anchors:
        return {
            "available": False,
            "theatre": theatre,
            "error": "the mission's flight has no waypoint positions to draw",
        }

    margin = maps.DEFAULT_MARGIN_M
    xs = [a[0] for a in anchors]
    zs = [a[1] for a in anchors]
    box = (min(xs) - margin, min(zs) - margin, max(xs) + margin, max(zs) + margin)
    # Airfields close to the route are worth having on the map, so the box grows
    # to take in any that are almost in frame already.
    near = [
        f for f in fields
        if box[0] - margin <= f["x"] <= box[2] + margin
        and box[1] - margin <= f["z"] <= box[3] + margin
    ]
    if near:
        box = (
            min(box[0], min(f["x"] for f in near) - margin / 3),
            min(box[1], min(f["z"] for f in near) - margin / 3),
            max(box[2], max(f["x"] for f in near) + margin / 3),
            max(box[3], max(f["z"] for f in near) + margin / 3),
        )

    chart = maps.TheatreChart(terrain, box)

    # A chart that was built and then failed its own alignment check is not shown
    # again; the terrain outline is drawn instead and the page says why. The check
    # can only run once the image exists, so this takes effect from the second
    # time the board is built -- which is also the first time it could matter.
    stamp = terrain.source_stamp()
    verdict = maps.load_verdicts().get(chart.map_id) or {}
    if verdict.get("stamp") != stamp:
        verdict = {}
    rejected = ""
    if chart.mode == "chart" and verdict.get("verdict") == "contradicted":
        rejected = (
            "the stitched chart's coastline matched the terrain's own better with the "
            f"chart displaced by {round(verdict.get('shift_m', 0) / 1000)} km than "
            f"where the sim's own tile index puts it "
            f"({round((verdict.get('rival_iou') or 0) * 100)}% against "
            f"{round((verdict.get('iou') or 0) * 100)}%), so it is not trusted"
        )
        chart = maps.TheatreChart(terrain, box, force_outline=True)

    if not chart.ok:
        return {
            "available": False,
            "theatre": theatre,
            "error": chart.error or terrain.chart_error or "no chart could be prepared",
        }

    def place(x_north, z_east):
        if x_north is None or z_east is None:
            return None
        px, py = chart.to_pixel(x_north, z_east)
        return {"x": round(px, 1), "y": round(py, 1)}

    def from_bullseye(x_north, z_east):
        if bullseye.get("x") is None or bullseye.get("y") is None:
            return None
        dx, dz = x_north - bullseye["x"], z_east - bullseye["y"]
        return {
            "bearing": round((degrees(atan2(dz, dx)) + 360.0) % 360.0),
            "range_nm": round(hypot(dx, dz) * M_TO_NM),
        }

    drawn_route = []
    for point in route:
        spot = place(point["x"], point["z"])
        if not spot:
            continue
        entry = dict(spot, label=point["label"], order=point["order"])
        entry["bullseye"] = from_bullseye(point["x"], point["z"])
        drawn_route.append(entry)

    airfields = [
        dict(place(f["x"], f["z"]), name=f["name"])
        for f in fields
        if chart.inside(f["x"], f["z"])
    ]

    spawn = None
    unit = (mission.player or {}).get("unit") or {}
    if isinstance(unit.get("x"), (int, float)) and isinstance(unit.get("y"), (int, float)):
        spawn = place(unit["x"], unit["y"])

    projection = terrain.projection
    outline = chart.mode == "outline"
    return {
        "available": True,
        "error": "",
        "theatre": theatre,
        "name": theatre,
        "mode": chart.mode,
        "url": f"/dcsmap/{chart.map_id}",
        "map_id": chart.map_id,
        "width": chart.width,
        "height": chart.height,
        "metres_per_pixel": round(chart.mpp, 2),
        "source_metres_per_pixel": round(chart.source_mpp, 2),
        "visible": chart.visible_box(),
        "route": drawn_route,
        "airfields": airfields,
        "spawn": spawn,
        "bullseye": (
            dict(place(bullseye["x"], bullseye["y"]) or {}, side=bullseye.get("side", ""))
            if bullseye.get("x") is not None
            else None
        ),
        "towns": (
            chart.towns_in_view(skip={f["name"] for f in airfields}) if outline else []
        ),
        "graticule": chart.graticule() if outline else [],
        "scale_bar": chart.scale_bar() if outline else {},
        "corners": chart.corners_lonlat(),
        "cached": bool(chart.cached()),
        "alignment": verdict,
        "fallback_reason": rejected or (terrain.chart_error if outline else ""),
        "projection": projection.describe() if projection else {},
        "tiles": len(chart.tiles),
        # Kept so the image route can stitch exactly this crop without having to
        # rebuild any of it from the request.
        "_chart": chart,
    }


def build(install: DcsInstall, mission_path: Path, weapons: DcsWeaponLibrary) -> dict:
    warnings: list[dict] = []
    mission = DcsMission(mission_path)

    if mission.player is None:
        warnings.append(
            {
                "level": "error",
                "text": f"{mission_path.name} has no player or client aircraft, so there is "
                "no flight to build a kneeboard for.",
            }
        )

    overview = mission.overview()
    pylons = mission.pylons()

    stores = []
    for pylon in pylons:
        record = weapons.lookup(pylon["clsid"])
        record["station"] = pylon["station"]
        stores.append(record)

    # Three outcomes, and they are worth telling apart. Curated stores carry
    # employment detail. Stores named from DCS's own comments carry a name and
    # nothing else. Anything else is shown as its raw code.
    from_game = sorted({s["name"] for s in stores if s.get("named_by_game")})
    unnamed = sorted(
        {s["clsid"] for s in stores if not s["known"] and not s.get("named_by_game")}
    )
    if from_game:
        warnings.append(
            {
                "level": "info",
                "text": "Named from DCS's own files rather than the curated library: "
                + ", ".join(from_game)
                + ". DCS annotates these codes in a comment beside them, which gives a "
                "name but no employment detail, so those stores show a name only.",
            }
        )
    if unnamed:
        warnings.append(
            {
                "level": "warn",
                "text": "No reference data for these pylon codes: "
                + ", ".join(unnamed)
                + ". DCS does not publish a readable code-to-name table and says nothing "
                "about these, so they are shown as raw codes rather than guessed at.",
            }
        )

    laser_stores = sorted({s["name"] for s in stores if s["laser"]})

    flight = {
        "callsign": overview["flight"] or "Player",
        "uniform": True,
        "is_player": True,
        "aircraft": [
            {
                "label": overview.get("aircraft_type", "") or "Player",
                "stores": stores,
                "total_weight_lb": None,
            }
        ],
        "laser_stores": laser_stores,
        "needs_laser_code": bool(laser_stores),
    }

    theatre_map = _theatre_map(install, mission)
    chart_object = theatre_map.pop("_chart", None)
    if not theatre_map.get("available") and theatre_map.get("error"):
        warnings.append(
            {
                "level": "warn",
                "text": f"The theatre chart could not be prepared: {theatre_map['error']}",
            }
        )
    elif theatre_map.get("mode") == "outline" and theatre_map.get("fallback_reason"):
        warnings.append(
            {
                "level": "warn",
                "text": "The Maps page is showing the terrain outline rather than the sim's "
                f"own aeronautical chart, because {theatre_map['fallback_reason']}.",
            }
        )

    pages = mission.kneeboard_pages()
    if not pages:
        warnings.append(
            {
                "level": "warn",
                "text": "This mission embeds no kneeboard pages. DCS ships no approach "
                "plates, so the Charts page has nothing to show for it.",
            }
        )

    briefing = {
        "generated": overview.get("mission_date", ""),
        "overview": overview,
        "situation": mission.briefing_prose(),
        "roster": {"headers": [], "rows": []},
        "package": [],
        "threats": [],
        "steerpoints": mission.steerpoints(),
        "comms": mission.comms(),
        "iff": {"blocks": []},
        "link16": [],
        "ordnance": [],
        "weather": mission.weather(),
        "support": mission.support(),
        "roe": [],
        "emergency": [],
        "alternate_airfield": {"text": "", "icao": "", "name": ""},
        "airbases": {},
        "sections": ["Mission Overview", "Situation", "Steerpoints", "Weather"],
        "radios": mission.radio_presets(),
        "consumables": mission.consumables(),
        "bullseye": mission.bullseye(),
    }

    return {
        "sim": "dcs",
        "ok": True,
        "install": {
            "base": str(install.base) if install.base else "",
            "version": install.version,
            "theater": mission.theatre,
            "pilot_callsign": "",
            "pilot_name": "",
            "mission_file": str(mission_path),
            "mission_name": mission_path.name,
        },
        "briefing": briefing,
        "loadout": {
            "flights": [flight],
            "player_flight": flight["callsign"],
            "has_player": mission.player is not None,
        },
        "dtc": {"generated": "", "uhf": [], "vhf": [], "available": False},
        "charts": {
            "resolved": [],
            "airfields": [],
            "maps": [],
            "pages": pages,
            "theatre": theatre_map,
            "summary": {
                "airfield_count": len(theatre_map.get("airfields") or []),
                "chart_count": len(pages),
                "map_count": 1 if theatre_map.get("available") else 0,
            },
        },
        "warnings": warnings,
        "_mission": mission,
        "_chart": chart_object,
    }
