"""Build the kneeboard payload from an IL-2 mission.

Reproduces the same contract as the BMS and DCS sources so the front end renders
most pages unchanged. Where IL-2 has no equivalent -- a threat brief, radio
frequencies, approach plates -- the section is left empty and the reason is
reported, rather than padded out with something invented.

The one structurally new thing here is that the loadout has two sources of
differing authority. The mission file records what the generator planned; the
sortie log records what you actually took off with. They disagree in practice, so
the payload carries both and says which it is showing.
"""

from __future__ import annotations

from pathlib import Path

from . import logs
from .gtp import GtpError, open_archive
from .localization import Localization
from .mission import Il2Mission, MissionError

KG_TO_LB = 2.204622622


def build_campaign(install, log, weapons, reference) -> dict:
    """Build the reduced board a scripted DLC campaign mission allows.

    Campaign missions are compiled into ``.cmpbin`` inside ``Campaigns.gtp``, so
    there is no route, weather or flight plan to read -- only the briefing text and
    briefing map that ship alongside, plus whatever the sortie log records about
    what you took off with.
    """
    warnings: list[dict] = [
        {
            "level": "warn",
            "text": "This is a scripted campaign mission. IL-2 compiles those into a binary "
            "this board cannot read, so the flight plan, weather and planned loadout "
            "are unavailable -- only the briefing, the campaign's own map and what "
            "the sortie log recorded.",
        }
    ]

    members = install.campaign_members(log.mission_file)
    title = ""
    prose: list[dict] = []
    has_image = False

    if members:
        try:
            with open_archive(install.campaigns_archive) as archive:
                for key in ("text", "text_fallback"):
                    member = members.get(key)
                    if member and member in archive:
                        localization = Localization.from_bytes(archive.read(member))
                        title = localization.get(0)
                        prose = localization.prose(1)
                        break
                has_image = bool(members.get("image")) and members["image"] in archive
        except GtpError as exc:
            warnings.append({"level": "warn", "text": str(exc)})

    if not prose:
        warnings.append(
            {
                "level": "warn",
                "text": f"No briefing text was found for {log.mission_file or 'this mission'} "
                "inside Campaigns.gtp.",
            }
        )

    player = log.player or {}
    aircraft_name = player.get("aircraft", "")
    _, record = weapons.find_aircraft(display_name=aircraft_name)
    stores, raw_label = weapons.stores(record, player.get("payload_id"))

    unknown = weapons.unknown_codes(stores)
    if unknown:
        warnings.append(
            {
                "level": "warn",
                "text": "No reference data for these loadout codes: " + ", ".join(unknown),
            }
        )

    fuel = player.get("fuel")
    flight_name = player.get("pilot") or aircraft_name or "Player"

    loadout = {
        "flights": [
            {
                "callsign": flight_name,
                "uniform": True,
                "is_player": True,
                "aircraft": [
                    {"label": aircraft_name, "stores": stores, "total_weight_lb": None}
                ],
                "laser_stores": [],
                "needs_laser_code": False,
            }
        ],
        "player_flight": flight_name,
        "has_player": True,
        "source": {
            "kind": "as-flown",
            "confidence": "campaign-log",
            "reasons": [],
            "log": log.path.name,
            "raw": raw_label,
            "note": f"Read from {log.path.name}. A scripted campaign records nothing else "
            "this board can read, so this is the only loadout source available.",
        },
        "planned": {"payload_id": None, "fuel_pct": None},
        "as_flown": {
            "payload_id": player.get("payload_id"),
            "fuel_pct": round(fuel * 100) if fuel is not None else None,
            "rounds": player.get("rounds", {}),
        },
        "differs": [],
    }

    pages = (
        [
            {
                "name": f"{members['campaign']} {members['mission']}",
                "aircraft": "",
                "entry": members["image"],
                "size_kb": 0,
            }
        ]
        if has_image
        else []
    )

    briefing = {
        "generated": f"{log.game_date} {log.game_time}".strip(),
        "overview": {
            "flight": flight_name,
            "role": title,
            "fields": {},
            "package": "",
            "package_type": "",
            "mission": title or log.mission_file,
            "target_area": "",
            "time_on_target": "",
            "sunrise": "",
            "sunset": "",
            "target_icao": "",
            "aircraft_type": aircraft_name,
            # Campaign folder names are lowercase run-together words, so any
            # attempt to prettify them ("10Daysofautumn") reads worse than the
            # slug itself. Shown verbatim.
            "theatre": members.get("campaign", ""),
            "mission_date": log.game_date,
            "start_time": log.game_time,
            "pilot": player.get("pilot", ""),
            "country": reference.country(player.get("country")),
        },
        "situation": prose,
        "roster": {"headers": [], "rows": []},
        "package": [],
        "threats": [],
        "steerpoints": [],
        "comms": {"rows": [], "airbases": {}, "tanker_tacan": ""},
        "iff": {"blocks": []},
        "link16": [],
        "ordnance": [],
        "weather": {"headers": [], "rows": []},
        "support": [],
        "roe": [],
        "emergency": [],
        "alternate_airfield": {"text": "", "icao": "", "name": ""},
        "airbases": {},
        "sections": ["Situation"],
        "consumables": {
            "fuel_pct": round(fuel * 100) if fuel is not None else None,
            "rounds": player.get("rounds", {}),
        },
        "flight": {},
    }

    return {
        "sim": "il2",
        "ok": True,
        "campaign": True,
        "install": {
            "base": str(install.base),
            "version": install.version,
            "theater": briefing["overview"]["theatre"],
            "pilot_callsign": flight_name,
            "pilot_name": player.get("pilot", ""),
            "mission_file": log.mission_file,
            "mission_name": log.mission_file.replace("\\", "/").rsplit("/", 1)[-1],
        },
        "briefing": briefing,
        "loadout": loadout,
        "dtc": {"generated": "", "uhf": [], "vhf": [], "available": False},
        "charts": {
            "resolved": [],
            "airfields": [],
            "maps": [],
            "pages": pages,
            "taxi": [],
            "summary": {"airfield_count": 0, "chart_count": len(pages), "map_count": 0},
        },
        "warnings": warnings,
    }


def build(install, mission_path: Path, weapons, reference) -> dict:
    warnings: list[dict] = []
    mission = Il2Mission(mission_path, install.language())

    if mission.player is None:
        warnings.append(
            {
                "level": "error",
                "text": f"{Path(mission_path).name} has no player aircraft, so there is no "
                "flight to build a kneeboard for.",
            }
        )

    flight = mission.flight()
    if not mission.localization.available:
        warnings.append(
            {
                "level": "warn",
                "text": "No text file was found beside this mission, so the briefing, "
                "waypoint names and objectives are unavailable.",
            }
        )

    # -- loadout: planned from the mission, as flown from the log -----------

    log, correlation = logs.newest_for(install, mission_path, mission.options)
    if not install.text_log_enabled:
        warnings.append(
            {
                "level": "warn",
                "text": "mission_text_log is not enabled in data\\startup.cfg, so IL-2 writes "
                "no sortie log and the loadout below is only what the mission planned, "
                "not what you flew.",
            }
        )

    aircraft_key, record = weapons.find_aircraft(
        script=mission.player_script,
        display_name=(log.player or {}).get("aircraft", "") if log else "",
    )

    as_flown_ok = bool(log and log.player and correlation["confidence"] in ("matched", "same-mission-file"))
    planned_payload = flight["payload_id"]
    flown_payload = (log.player or {}).get("payload_id") if log else None

    use_payload = flown_payload if as_flown_ok and flown_payload is not None else planned_payload
    stores, raw_label = weapons.stores(record, use_payload)

    if record and not stores and use_payload is not None:
        warnings.append(
            {
                "level": "warn",
                "text": f"{record.get('object_name') or mission.aircraft_code} has no payload "
                f"{use_payload} in IL-2's own table, so the stores list is empty rather "
                "than showing a different loadout.",
            }
        )
    if not record:
        warnings.append(
            {
                "level": "warn",
                "text": f"No loadout table found for {mission.aircraft_code or 'this aircraft'}. "
                "Weapon names come from the game's packed data; without a match the "
                "payload cannot be named.",
            }
        )

    unknown = weapons.unknown_codes(stores)
    if unknown:
        warnings.append(
            {
                "level": "warn",
                "text": "No reference data for these loadout codes: "
                + ", ".join(unknown)
                + ". IL-2's own name table does not list them, so they are shown as raw "
                "codes rather than guessed at.",
            }
        )

    differs = []
    if as_flown_ok and log and log.player:
        if flown_payload is not None and planned_payload is not None and flown_payload != planned_payload:
            differs.append("payload")
        planned_fuel = flight["fuel"]
        flown_fuel = log.player.get("fuel")
        if planned_fuel is not None and flown_fuel is not None and abs(planned_fuel - flown_fuel) > 0.01:
            differs.append("fuel")

    aircraft_name = (
        (log.player or {}).get("aircraft") if log and log.player else ""
    ) or record.get("object_name", "") or mission.aircraft_code

    callsign = reference.flight_callsign(flight["callsign_id"], flight["callnum"])
    fuel_fraction = (
        log.player.get("fuel") if as_flown_ok and log and log.player else flight["fuel"]
    )

    loadout_flight = {
        "callsign": callsign or flight["pilot"] or "Player",
        "uniform": True,
        "is_player": True,
        "aircraft": [
            {
                "label": aircraft_name,
                "stores": stores,
                "total_weight_lb": None,
            }
        ],
        "laser_stores": [],
        "needs_laser_code": False,
    }

    loadout = {
        "flights": [loadout_flight],
        "player_flight": loadout_flight["callsign"],
        "has_player": mission.player is not None,
        "source": {
            "kind": "as-flown" if as_flown_ok else "planned",
            "confidence": correlation["confidence"],
            "reasons": correlation["reasons"],
            "log": correlation["log"],
            "raw": raw_label,
            "note": (
                f"Read from {correlation['log']}, the log IL-2 wrote when this mission started."
                if as_flown_ok
                else "IL-2 never writes your chosen loadout back to the mission file, so this "
                "is what the mission planned, not necessarily what is on the aircraft."
            ),
        },
        "planned": {
            "payload_id": planned_payload,
            "fuel_pct": round(flight["fuel"] * 100) if flight["fuel"] is not None else None,
        },
        "as_flown": (
            {
                "payload_id": flown_payload,
                "fuel_pct": round(log.player["fuel"] * 100) if log.player.get("fuel") is not None else None,
                "rounds": log.player.get("rounds", {}),
            }
            if as_flown_ok and log and log.player
            else None
        ),
        "differs": differs,
    }

    # -- comms: callsigns only; IL-2 models no tunable radio ---------------

    comm_rows = []
    if callsign:
        comm_rows.append(
            {
                "agency": "Your flight",
                "callsign": callsign,
                "uhf": "--",
                "vhf": "--",
                "notes": f"{len(mission.squadron())} aircraft",
                "group": "flight",
            }
        )

    departure = mission.nearest_airfield(flight["position"])
    route = mission.route()
    # The last route marker is the recovery end of the plan; the nearest field to
    # it is where you are expected to land.
    recovery = mission.nearest_airfield(route[-1]["position"]) if route else None
    if recovery and departure and recovery["name"] == departure["name"]:
        recovery = None

    for label, field in (("Departure", departure), ("Recovery", recovery)):
        if not field:
            continue
        comm_rows.append(
            {
                "agency": f"{label} field",
                "callsign": reference.airfield_callsign(field["callsign_id"], field["callnum"])
                or field["name"],
                "uhf": "--",
                "vhf": "--",
                "notes": field["name"],
                "group": "departure",
            }
        )

    # -- charts: taxi diagrams for the fields that matter ------------------

    taxi = []
    for label, field in (("Departure", departure), ("Recovery", recovery)):
        if field and field["taxi_points"]:
            taxi.append(
                {
                    "label": label,
                    "airfield": field["name"],
                    "callsign": reference.airfield_callsign(field["callsign_id"], field["callnum"]),
                    "points": field["taxi_points"],
                }
            )
    if not taxi:
        warnings.append(
            {
                "level": "warn",
                "text": "No taxi diagram is available for your departure field, and IL-2 ships "
                "no approach plates, so the Charts page has nothing to show.",
            }
        )

    for text in mission.route_warnings:
        warnings.append({"level": "warn", "text": text})
    for text in weapons.errors:
        warnings.append({"level": "warn", "text": text})
    for text in reference.errors:
        warnings.append({"level": "warn", "text": text})

    squadron = mission.squadron()
    roster_rows = [
        {
            "callsign": f"{entry['callnum'] or ''}".strip() or "--",
            "pilots": [
                entry["name"] + (" (you)" if entry["is_player"] else ""),
                entry["aircraft_code"],
            ],
        }
        for entry in squadron
    ]

    briefing = {
        "generated": f"{mission.mission_date()} {mission.start_time()}".strip(),
        "overview": {
            "flight": callsign or flight["pilot"] or "Player",
            "role": mission.title(),
            "fields": {},
            "package": "",
            "package_type": "",
            "mission": mission.title(),
            "target_area": "",
            "time_on_target": "",
            "sunrise": "",
            "sunset": "",
            "target_icao": "",
            "aircraft_type": aircraft_name,
            "theatre": mission.theatre(),
            "mission_date": mission.mission_date(),
            "start_time": mission.start_time(),
            "pilot": flight["pilot"],
            "country": reference.country(flight["country"]),
        },
        "situation": mission.briefing_prose(),
        "roster": {"headers": ["Pilot", "Aircraft"], "rows": roster_rows},
        "package": [],
        "threats": [],
        "steerpoints": mission.steerpoints(),
        "comms": {"rows": comm_rows, "airbases": {}, "tanker_tacan": ""},
        "iff": {"blocks": []},
        "link16": [],
        "ordnance": [],
        "weather": mission.weather(),
        "support": [],
        "roe": [],
        "emergency": [],
        "alternate_airfield": {"text": "", "icao": "", "name": ""},
        "airbases": {
            "departure": departure["name"] if departure else "",
            "recovery": recovery["name"] if recovery else (departure["name"] if departure else ""),
        },
        "sections": ["Mission Overview", "Situation", "Steerpoints", "Weather"],
        "consumables": {
            "fuel_pct": round(fuel_fraction * 100) if fuel_fraction is not None else None,
            "rounds": (log.player or {}).get("rounds", {}) if as_flown_ok and log else {},
        },
        "flight": flight,
    }

    return {
        "sim": "il2",
        "ok": True,
        "install": {
            "base": str(install.base),
            "version": install.version,
            "theater": mission.theatre(),
            "pilot_callsign": callsign,
            "pilot_name": flight["pilot"],
            "mission_file": str(mission_path),
            "mission_name": Path(mission_path).name,
        },
        "briefing": briefing,
        "loadout": loadout,
        "dtc": {"generated": "", "uhf": [], "vhf": [], "available": False},
        "charts": {
            "resolved": [],
            "airfields": [],
            "maps": [],
            "pages": [],
            "taxi": taxi,
            "summary": {"airfield_count": len(mission.airfields()), "chart_count": len(taxi), "map_count": 0},
        },
        "warnings": warnings,
    }
