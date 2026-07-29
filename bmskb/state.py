"""Assemble the full kneeboard payload and track when it needs rebuilding.

The briefing is re-read whenever its modification time changes, which is what
makes the board update the moment BMS regenerates it. The heavy, static data --
the weapon library and the chart index -- is loaded once and reused.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .briefing import parse_briefing
from .charts import ChartLibrary
from .dcs import source as dcs_source
from .dcs.install import DcsInstall
from .dcs.mission import MissionError
from .dcs.weapons import DcsWeaponLibrary
from .dtc import parse_dtc_comm
from .install import BmsInstall
from .weapons import DATA_DIR, WeaponLibrary

SETTINGS_PATH = Path(__file__).parent.parent / "user_settings.json"

TIMESTAMP_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

DEFAULT_SETTINGS = {
    "laser_code": "1688",
    "wingman_laser_code": "",
    "notes": "",
    # "auto" follows whichever sim wrote a mission most recently; "bms" or
    # "dcs" pin the board to one.
    "sim": "auto",
}

VALID_SIMS = ("auto", "bms", "dcs")


def _parse_timestamp(text: str) -> datetime | None:
    text = (text or "").strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _laser_reference() -> dict:
    try:
        payload = json.loads((DATA_DIR / "f16_stores.json").read_text(encoding="utf-8"))
        return payload.get("laser_codes", {})
    except (OSError, json.JSONDecodeError):
        return {}


def validate_laser_code(code: str) -> tuple[bool, str]:
    """BMS accepts 1xyz where x, y and z are each 1-8."""
    code = (code or "").strip()
    if not code:
        return True, ""
    if len(code) != 4 or not code.isdigit():
        return False, "Laser code must be exactly four digits."
    if code[0] != "1":
        return False, "First digit must be 1."
    if any(ch not in "12345678" for ch in code[1:]):
        return False, "Digits two to four must each be between 1 and 8."
    return True, ""


class KneeboardState:
    """Caches the parsed kneeboard and rebuilds it when briefing.txt changes."""

    def __init__(self, install: BmsInstall | None, dcs: DcsInstall | None = None):
        self.install = install
        self.dcs = dcs
        self.lock = threading.Lock()
        self.payload: dict | None = None
        self.signature: tuple | None = None
        self.weapons = WeaponLibrary(install.wcd_file if install else None)
        self.dcs_weapons = DcsWeaponLibrary()
        self.charts = ChartLibrary(
            install.charts_dir if install else None,
            install.maps_dir if install else None,
        )
        self.settings = self._load_settings()
        # Kept so the mission archive can be served without reopening it.
        self.dcs_mission = None

    # -- settings --------------------------------------------------------

    def _load_settings(self) -> dict:
        settings = dict(DEFAULT_SETTINGS)
        try:
            settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
        return settings

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            sim_changed = False
            for key in DEFAULT_SETTINGS:
                if key in changes:
                    value = str(changes[key])
                    if key == "sim":
                        if value not in VALID_SIMS:
                            continue
                        sim_changed = value != self.settings.get("sim")
                    self.settings[key] = value
            try:
                SETTINGS_PATH.write_text(
                    json.dumps(self.settings, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            if sim_changed:
                # Switching sim changes the whole board, not just a panel.
                self.payload = None
                self.signature = None
            elif self.payload is not None:
                self.payload["laser"] = self._laser_block(self.payload.get("loadout", {}))
            return dict(self.settings)

    # -- source selection ------------------------------------------------

    def _bms_mtime(self) -> float:
        if not self.install or not self.install.briefing_file.is_file():
            return 0.0
        try:
            return self.install.briefing_file.stat().st_mtime
        except OSError:
            return 0.0

    def _dcs_mission_path(self):
        if not self.dcs:
            return None, 0.0
        latest = self.dcs.latest_mission()
        if not latest:
            return None, 0.0
        try:
            return latest, latest.stat().st_mtime
        except OSError:
            return latest, 0.0

    def choose_sim(self) -> tuple[str, object]:
        """Return the sim to display and, for DCS, the mission to read.

        In "auto" the more recently written mission wins, which mirrors how the
        board already follows briefing.txt without being told.
        """
        preference = self.settings.get("sim", "auto")
        bms_at = self._bms_mtime()
        dcs_path, dcs_at = self._dcs_mission_path()

        if preference == "bms":
            return "bms", None
        if preference == "dcs":
            return "dcs", dcs_path
        if dcs_path and dcs_at > bms_at:
            return "dcs", dcs_path
        if bms_at:
            return "bms", None
        return ("dcs", dcs_path) if dcs_path else ("bms", None)

    # -- freshness -------------------------------------------------------

    def _current_signature(self) -> tuple:
        sim, mission = self.choose_sim()
        if sim == "dcs":
            if not mission:
                return ("dcs", "no-mission")
            try:
                stat = mission.stat()
                return ("dcs", str(mission), int(stat.st_mtime), stat.st_size)
            except OSError:
                return ("dcs", str(mission), 0, 0)

        if not self.install:
            return ("no-install",)
        parts: list = ["bms"]
        for path in (self.install.briefing_file, self.install.dtc_comm_file):
            try:
                stat = path.stat()
                parts.append((path.name, int(stat.st_mtime), stat.st_size))
            except OSError:
                parts.append((path.name, 0, 0))
        return tuple(parts)

    def token(self) -> str:
        return str(abs(hash(self._current_signature())))

    def get(self, force: bool = False) -> dict:
        signature = self._current_signature()
        with self.lock:
            if force or self.payload is None or signature != self.signature:
                self.payload = self._build()
                self.signature = signature
            return self.payload

    # -- assembly --------------------------------------------------------

    def _available_sims(self) -> list[str]:
        out = []
        if self.install:
            out.append("bms")
        if self.dcs and self.dcs.latest_mission():
            out.append("dcs")
        return out

    def _build(self) -> dict:
        sim, mission_path = self.choose_sim()
        if sim == "dcs" and mission_path is not None:
            payload = self._build_dcs(mission_path)
        else:
            payload = self._build_bms()

        payload["sim"] = payload.get("sim", sim)
        payload["sims"] = {
            "available": self._available_sims(),
            "preference": self.settings.get("sim", "auto"),
            "active": payload["sim"],
        }
        payload["token"] = self.token()
        return payload

    def _build_dcs(self, mission_path) -> dict:
        try:
            payload = dcs_source.build(self.dcs, mission_path, self.dcs_weapons)
        except MissionError as exc:
            self.dcs_mission = None
            return {
                "sim": "dcs",
                "ok": False,
                "install": None,
                "warnings": [{"level": "error", "text": str(exc)}],
            }

        self.dcs_mission = payload.pop("_mission", None)
        payload["laser"] = self._laser_block(payload.get("loadout", {}), sim="dcs")
        for text in self.dcs_weapons.errors:
            payload["warnings"].append({"level": "warn", "text": text})
        return payload

    def _build_bms(self) -> dict:
        warnings: list[dict] = []

        if not self.install:
            return {
                "sim": "bms",
                "ok": False,
                "install": None,
                "warnings": [
                    {
                        "level": "error",
                        "text": "No Falcon BMS installation found. Set the BMS_PATH "
                        "environment variable to your BMS folder and restart.",
                    }
                ],
            }

        install_info = self.install.describe()

        if not self.install.briefing_file.is_file():
            warnings.append(
                {
                    "level": "error",
                    "text": f"briefing.txt not found at {self.install.briefing_file}. "
                    "Commit to a mission in BMS to generate it.",
                }
            )
            briefing: dict = {}
        else:
            try:
                briefing = parse_briefing(self.install.briefing_file)
            except Exception as exc:  # a malformed brief must not take the board down
                warnings.append({"level": "error", "text": f"Could not parse briefing.txt: {exc}"})
                briefing = {}
            else:
                # A file that reads cleanly but yields nothing recognisable is
                # more likely truncated or from an unsupported version than empty.
                if not any(
                    (
                        briefing.get("overview", {}).get("flight"),
                        briefing.get("steerpoints"),
                        briefing.get("ordnance"),
                        briefing.get("comms", {}).get("rows"),
                    )
                ):
                    warnings.append(
                        {
                            "level": "error",
                            "text": "briefing.txt contains no recognisable sections. It may be "
                            "truncated, still being written, or from an unsupported BMS "
                            "version. Regenerate it by committing to a mission in BMS.",
                        }
                    )

        dtc = parse_dtc_comm(self.install.dtc_comm_file)
        loadout = self._build_loadout(briefing)
        resolved_charts = self.charts.resolve_briefing(briefing) if briefing else []

        warnings.extend(self._freshness_warnings(briefing, dtc))
        warnings.extend(
            {"level": "warn", "text": text} for text in self.weapons.errors
        )
        unknown = sorted(
            {
                store["name"]
                for flight in loadout.get("flights", [])
                for aircraft in flight["aircraft"]
                for store in aircraft["stores"]
                if not store["known"]
            }
        )
        if unknown:
            warnings.append(
                {
                    "level": "warn",
                    "text": "No reference data for: " + ", ".join(unknown),
                }
            )
        for entry in resolved_charts:
            if not entry["found"]:
                warnings.append(
                    {
                        "level": "warn",
                        "text": f"No charts found for {entry['label'].lower()} field "
                        f"'{entry['requested']}'.",
                    }
                )

        return {
            "sim": "bms",
            "ok": True,
            "install": install_info,
            "briefing": briefing,
            "loadout": loadout,
            "dtc": dtc,
            "charts": {
                "resolved": resolved_charts,
                "airfields": self.charts.airfields,
                "maps": self.charts.maps,
                "pages": [],
                "summary": self.charts.describe(),
            },
            "laser": self._laser_block(loadout),
            "warnings": warnings,
        }

    def _build_loadout(self, briefing: dict) -> dict:
        flights = [self.weapons.enrich_flight(f) for f in briefing.get("ordnance", [])]
        player = briefing.get("overview", {}).get("flight", "")
        for flight in flights:
            flight["is_player"] = bool(player) and flight["callsign"] == player
        flights.sort(key=lambda f: (not f["is_player"], f["callsign"]))
        return {
            "flights": flights,
            "player_flight": player,
            "has_player": any(f["is_player"] for f in flights),
        }

    def _laser_block(self, loadout: dict, sim: str = "bms") -> dict:
        needed = any(f.get("needs_laser_code") for f in loadout.get("flights", []))
        stores = sorted(
            {
                store
                for flight in loadout.get("flights", [])
                if flight.get("is_player")
                for store in flight.get("laser_stores", [])
            }
        )
        return {
            "code": self.settings.get("laser_code", "1688"),
            "wingman_code": self.settings.get("wingman_laser_code", ""),
            "notes": self.settings.get("notes", ""),
            "needed": needed,
            "player_laser_stores": stores,
            "reference": _laser_reference(),
            "source_note": (
                "Laser codes are set in the cockpit and in the mission editor, and are not "
                "written anywhere this board can read, so this panel is entered by hand. "
                "It is a reminder of what you set -- it does not read the jet."
                if sim == "dcs"
                else "Laser codes are set in the in-game DTC and are not written to any "
                "file BMS exports, so this panel is entered by hand. It is a reminder "
                "of what you set -- it does not read the jet."
            ),
        }

    def _freshness_warnings(self, briefing: dict, dtc: dict) -> list[dict]:
        warnings: list[dict] = []
        if not briefing or not dtc.get("available"):
            return warnings

        brief_at = _parse_timestamp(briefing.get("generated", ""))
        dtc_at = _parse_timestamp(dtc.get("generated", ""))

        if brief_at and dtc_at:
            delta = abs((brief_at - dtc_at).total_seconds())
            if delta > 300:
                older = "older" if dtc_at < brief_at else "newer"
                warnings.append(
                    {
                        "level": "warn",
                        "text": f"Radio presets are {_humanise(delta)} {older} than the "
                        f"briefing (dtc_comm.txt {dtc['generated']} vs briefing "
                        f"{briefing['generated']}). They may be from a different "
                        "mission -- verify against the comm ladder.",
                    }
                )
        elif briefing.get("generated") and dtc.get("generated"):
            if briefing["generated"] != dtc["generated"]:
                warnings.append(
                    {
                        "level": "warn",
                        "text": "Radio presets and briefing carry different timestamps; "
                        "the presets may be from a different mission.",
                    }
                )
        return warnings


def _humanise(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} hr"
    return f"{hours / 24:.0f} days"
