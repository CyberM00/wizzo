"""Build the kneeboard payload from a DCS mission.

Produces the same top-level shape the BMS side does, so the front end renders
most pages unchanged. Where DCS genuinely has no equivalent -- IFF rotation,
Link 16 files, approach plates, a threat brief -- the section is left empty and
the reason is reported as a warning rather than padded out.
"""

from __future__ import annotations

from pathlib import Path

from .install import DcsInstall
from .mission import DcsMission, MissionError
from .weapons import DcsWeaponLibrary


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

    unknown = sorted({s["clsid"] for s in stores if not s["known"]})
    if unknown:
        warnings.append(
            {
                "level": "warn",
                "text": "No reference data for these pylon codes: "
                + ", ".join(unknown)
                + ". DCS does not publish a readable code-to-name table, so these are "
                "shown as raw codes rather than guessed at.",
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
            "summary": {"airfield_count": 0, "chart_count": len(pages), "map_count": 0},
        },
        "warnings": warnings,
        "_mission": mission,
    }
