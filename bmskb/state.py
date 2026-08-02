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
from .manuals import ManualLibrary
from .dcs import source as dcs_source
from .dcs.install import DcsInstall
from .dcs.mission import MissionError
from .dcs.weapons import DcsWeaponLibrary
from .dtc import parse_dtc_comm
from .il2 import logs as il2_logs
from .il2 import source as il2_source
from .il2.install import Il2Install
from .il2.mission import MissionError as Il2MissionError
from .il2.reference import Il2Reference
from .il2.weapons import Il2WeaponLibrary
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
    # "auto" follows whichever sim wrote a mission most recently; naming one pins
    # the board to it.
    "sim": "auto",
}

VALID_SIMS = ("auto", "bms", "dcs", "il2")


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

    def __init__(
        self,
        install: BmsInstall | None,
        dcs: DcsInstall | None = None,
        il2: Il2Install | None = None,
    ):
        self.install = install
        self.dcs = dcs
        self.il2 = il2
        self.lock = threading.Lock()
        self.payload: dict | None = None
        self.signature: tuple | None = None
        self.weapons = WeaponLibrary(install.wcd_file if install else None)
        self.dcs_weapons = DcsWeaponLibrary()
        # IL-2's tables come out of the game's packed archives. The library loads
        # them lazily on first use, so a user who never opens the IL-2 board pays
        # nothing and a slow disk cannot delay the server starting.
        self.il2_weapons = Il2WeaponLibrary(
            il2.base if il2 else None, il2.language() if il2 else "eng"
        )
        self.il2_reference = Il2Reference(il2, il2.language() if il2 else "eng")
        # BMS and DCS publish no aircraft performance data, only PDFs. Indexed
        # once per install, like the chart library.
        self.manuals = {
            "bms": ManualLibrary(install.base if install else None, "bms"),
            "dcs": ManualLibrary(dcs.base if dcs else None, "dcs"),
        }
        self.charts = ChartLibrary(
            install.charts_dir if install else None,
            install.maps_dir if install else None,
        )
        self.settings = self._load_settings()
        # Kept so the mission archive can be served without reopening it. IL-2
        # needs no equivalent: its taxi charts are drawn inline from coordinates,
        # so nothing has to be served out of a mission file later.
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

    @staticmethod
    def _latest_mission(install):
        if not install:
            return None, 0.0
        latest = install.latest_mission()
        if not latest:
            return None, 0.0
        try:
            return latest, latest.stat().st_mtime
        except OSError:
            return latest, 0.0

    def _dcs_mission_path(self):
        return self._latest_mission(self.dcs)

    def _il2_mission_path(self):
        return self._latest_mission(self.il2)

    def _candidates(self) -> dict[str, tuple[object, float]]:
        """Each sim that has something to show, with the mission and its age."""
        found: dict[str, tuple[object, float]] = {}
        bms_at = self._bms_mtime()
        if self.install and bms_at:
            found["bms"] = (None, bms_at)
        for name, getter in (("dcs", self._dcs_mission_path), ("il2", self._il2_mission_path)):
            path, at = getter()
            if path:
                found[name] = (path, at)

        # A player who only flies scripted campaigns has no loose mission file at
        # all -- the sortie log is the only handle on what they flew.
        if "il2" not in found and self.il2:
            sortie = self.il2.latest_sortie()
            if sortie:
                found["il2"] = (None, _mtime_of(sortie))
        return found

    def sims_overview(self) -> list[dict]:
        """Per-sim status for the landing page.

        Deliberately cheap: file stats and names only, no mission parsing, so
        opening the front page never pays for building three boards.
        """
        candidates = self._candidates()
        preference = self.settings.get("sim", "auto")
        chosen, _ = self.choose_sim()
        out = []

        # -- BMS
        bms_at = self._bms_mtime()
        out.append(
            {
                "key": "bms",
                "label": "Falcon BMS",
                "title": f"BMS {self.install.version}" if self.install else "Falcon BMS",
                "found": bool(self.install),
                "ready": bool(bms_at),
                "source": "briefing.txt" if bms_at else "",
                "updated": _stamp_text(bms_at),
                "detail": (self.install.theater if self.install else "")
                or ("" if self.install else "not installed"),
                "hint": "" if bms_at else "Commit to a mission in BMS to generate a briefing.",
            }
        )

        # -- DCS
        dcs_path, dcs_at = self._dcs_mission_path()
        out.append(
            {
                "key": "dcs",
                "label": "DCS World",
                "title": f"DCS {self.dcs.version}".strip() if self.dcs else "DCS World",
                "found": bool(self.dcs),
                "ready": bool(dcs_path),
                "source": dcs_path.name if dcs_path else "",
                "updated": _stamp_text(dcs_at),
                "detail": f"{len(self.dcs.missions())} missions" if self.dcs else "not installed",
                "hint": "" if dcs_path else "No .miz found under Saved Games\\DCS\\Missions.",
            }
        )

        # -- IL-2
        il2_path, il2_at = self._il2_mission_path()
        sortie = self.il2.latest_sortie() if self.il2 else None
        details = []
        if self.il2:
            if sortie:
                details.append(f"{len(self.il2.sorties())} sorties logged")
            if not self.il2.text_log_enabled:
                details.append("mission_text_log off")
        out.append(
            {
                "key": "il2",
                "label": "IL-2 Great Battles",
                "title": "IL-2 Great Battles",
                "found": bool(self.il2),
                "ready": bool(il2_path or sortie),
                "source": il2_path.name if il2_path else (sortie.name if sortie else ""),
                "updated": _stamp_text(il2_at or _mtime_of(sortie) if sortie else il2_at),
                "detail": ", ".join(details) if details else ("" if self.il2 else "not installed"),
                "hint": ""
                if (il2_path or sortie)
                else "Fly a career sortie to generate a mission.",
            }
        )

        for entry in out:
            entry["active"] = entry["key"] == chosen
            entry["pinned"] = entry["key"] == preference
            entry["newest"] = bool(
                candidates and entry["key"] == max(candidates, key=lambda k: candidates[k][1])
            )
        return out

    def choose_sim(self) -> tuple[str, object]:
        """Return the sim to display and, for DCS and IL-2, the mission to read.

        In "auto" the most recently written mission wins, which mirrors how the
        board already follows briefing.txt without being told. BMS returns ``None``
        for the mission because its source is a fixed path.
        """
        preference = self.settings.get("sim", "auto")
        candidates = self._candidates()

        if preference in candidates:
            return preference, candidates[preference][0]
        # A pinned sim that has nothing to show still wins: silently showing a
        # different sim would be more confusing than an empty board.
        if preference in ("bms", "dcs", "il2"):
            return preference, None

        if candidates:
            name = max(candidates, key=lambda key: candidates[key][1])
            return name, candidates[name][0]
        return "bms", None

    # -- freshness -------------------------------------------------------

    def _current_signature(self) -> tuple:
        sim, mission = self.choose_sim()

        if sim == "il2":
            if not mission:
                return ("il2", "no-mission")
            # IL-2 has two inputs that change independently: the mission file is
            # written when a sortie is generated, and the sortie log appears
            # seconds later when you click Fly -- which is the moment the as-flown
            # loadout becomes knowable. Both must be in the signature or the board
            # never upgrades "planned" to "as flown".
            sortie = self.il2.latest_sortie() if self.il2 else None
            return (
                "il2",
                str(mission),
                _stat_key(mission),
                str(sortie) if sortie else "",
                _stat_key(sortie) if sortie else 0,
            )

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
        if self.il2 and (self.il2.latest_mission() or self.il2.latest_sortie()):
            out.append("il2")
        return out

    def _manual_block(self, payload: dict) -> dict:
        """Documents for the aircraft page, where the sim ships PDFs not data.

        IL-2 needs none of this: it publishes the performance figures themselves,
        which the board shows directly.
        """
        sim = payload.get("sim", "")
        library = self.manuals.get(sim)
        if library is None or not library.documents:
            return {"available": False, "matched": [], "all": [], "count": 0}

        overview = (payload.get("briefing") or {}).get("overview") or {}
        hints = [
            overview.get("aircraft_id", ""),
            overview.get("aircraft_type", ""),
        ]
        # BMS names the airframe in the package element rather than the overview.
        for element in (payload.get("briefing") or {}).get("package") or []:
            if element.get("callsign") == overview.get("flight"):
                hints.append(element.get("aircraft", ""))
        return {
            "available": True,
            "matched": library.for_aircraft(*hints),
            "all": library.grouped(),
            "count": len(library.documents),
        }

    def _build(self) -> dict:
        sim, mission_path = self.choose_sim()
        builders = {"dcs": self._build_dcs, "il2": self._build_il2}
        builder = builders.get(sim)
        # IL-2 is called even without a loose mission: a scripted campaign has no
        # mission file on disk and is resolved through the sortie log instead.
        if builder is not None and (mission_path is not None or sim == "il2"):
            payload = builder(mission_path)
        elif builder is not None:
            payload = {
                "sim": sim,
                "ok": False,
                "install": None,
                "warnings": [
                    {
                        "level": "error",
                        "text": f"The board is pinned to {sim.upper()} but no mission was "
                        "found for it. Fly a mission, or switch sim with the button "
                        "under the nav.",
                    }
                ],
            }
        else:
            payload = self._build_bms()

        payload["sim"] = payload.get("sim", sim)
        payload["manuals"] = self._manual_block(payload)
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

    def _il2_campaign_log(self, mission_path):
        """A sortie log naming a mission that is not the loose file on disk.

        That means a scripted campaign: its mission is compiled inside
        Campaigns.gtp, so the log is the only handle on it.
        """
        if not self.il2:
            return None
        latest = self.il2.latest_sortie()
        if latest is None:
            return None
        log = il2_logs.SortieLog(latest)
        if not log.ok or not log.mission_file:
            return None
        named = Path(log.mission_file.replace("\\", "/")).stem.casefold()
        if mission_path is not None and named == Path(mission_path).stem.casefold():
            return None
        if "campaign" not in log.mission_file.replace("\\", "/").lower():
            return None
        return log

    def _build_il2(self, mission_path) -> dict:
        campaign_log = self._il2_campaign_log(mission_path)
        if campaign_log is not None:
            payload = il2_source.build_campaign(
                self.il2, campaign_log, self.il2_weapons, self.il2_reference
            )
            payload["laser"] = self._laser_block(payload.get("loadout", {}), sim="il2")
            return payload

        try:
            payload = il2_source.build(
                self.il2, mission_path, self.il2_weapons, self.il2_reference
            )
        except Il2MissionError as exc:
            return {
                "sim": "il2",
                "ok": False,
                "install": None,
                "warnings": [{"level": "error", "text": str(exc)}],
            }
        payload["laser"] = self._laser_block(payload.get("loadout", {}), sim="il2")
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
        if sim == "il2":
            # No laser designators existed in 1943.
            return {
                "code": "",
                "wingman_code": "",
                "notes": "",
                "needed": False,
                "player_laser_stores": [],
                "reference": {},
                "source_note": "",
                "not_applicable": True,
            }
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


def _stamp_text(when: float) -> str:
    if not when:
        return ""
    return datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")


def _mtime_of(path) -> float:
    try:
        return path.stat().st_mtime
    except (OSError, AttributeError):
        return 0.0


def _stat_key(path) -> int:
    """A cheap change token for one file: mtime and size combined."""
    try:
        stat = path.stat()
    except (OSError, AttributeError):
        return 0
    return int(stat.st_mtime) * 1000003 + stat.st_size


def _humanise(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} hr"
    return f"{hours / 24:.0f} days"
