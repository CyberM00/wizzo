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
}


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

    def __init__(self, install: BmsInstall | None):
        self.install = install
        self.lock = threading.Lock()
        self.payload: dict | None = None
        self.signature: tuple | None = None
        self.weapons = WeaponLibrary(install.wcd_file if install else None)
        self.charts = ChartLibrary(
            install.charts_dir if install else None,
            install.maps_dir if install else None,
        )
        self.settings = self._load_settings()

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
            for key in DEFAULT_SETTINGS:
                if key in changes:
                    self.settings[key] = str(changes[key])
            try:
                SETTINGS_PATH.write_text(
                    json.dumps(self.settings, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            if self.payload is not None:
                self.payload["laser"] = self._laser_block(self.payload.get("loadout", {}))
            return dict(self.settings)

    # -- freshness -------------------------------------------------------

    def _current_signature(self) -> tuple:
        if not self.install:
            return ("no-install",)
        parts: list = []
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

    def _build(self) -> dict:
        warnings: list[dict] = []

        if not self.install:
            return {
                "ok": False,
                "install": None,
                "warnings": [
                    {
                        "level": "error",
                        "text": "No Falcon BMS installation found. Set the BMS_PATH "
                        "environment variable to your BMS folder and restart.",
                    }
                ],
                "token": self.token(),
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
            "ok": True,
            "install": install_info,
            "briefing": briefing,
            "loadout": loadout,
            "dtc": dtc,
            "charts": {
                "resolved": resolved_charts,
                "airfields": self.charts.airfields,
                "maps": self.charts.maps,
                "summary": self.charts.describe(),
            },
            "laser": self._laser_block(loadout),
            "warnings": warnings,
            "token": self.token(),
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

    def _laser_block(self, loadout: dict) -> dict:
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
            "source_note": "Laser codes are set in the in-game DTC and are not written to "
            "any file BMS exports, so this panel is entered by hand. It is a "
            "reminder of what you set -- it does not read the jet.",
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
