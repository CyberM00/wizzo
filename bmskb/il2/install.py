"""Locate the IL-2 Great Battles installation and the files it leaves behind.

IL-2 exports nothing and has no telemetry of any kind, so "the current mission"
means the most recently written mission file, and "what you actually flew" means
the newest sortie log. Both are found here.

Career mode writes ``data\\Missions\\_gen.Mission`` plus a per-language text file
beside it. Scripted campaign missions are packed inside ``Campaigns.gtp`` and are
resolved through the sortie log instead -- see ``mission.py``.
"""

from __future__ import annotations

import os
import re
import string
import time
from pathlib import Path

STEAM_APP_ID = "307960"
STEAM_FOLDER = "IL-2 Sturmovik Battle of Stalingrad"

REG_UNINSTALL = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {STEAM_APP_ID}"

MISSION_SUFFIXES = (".mission",)
# Glob cannot express this: "[0]" would be read as a character class, so the
# literal brackets in "missionReport(...)[0].txt" never match. Filter by regex.
SORTIE_GLOB = "missionReport*.txt"
SORTIE_RE = re.compile(r"^missionReport\((.+?)\)\[0\]\.txt$", re.IGNORECASE)

# Pat Wilson's Campaign Generator writes into data\Missions\PWCG. It is out of
# scope for now, and picking one of its missions up as "current" would show a
# board for a mode the user is not flying.
EXCLUDED_MISSION_DIRS = ("pwcg",)

# The sortie listing is consulted on every freshness poll, so it is memoised
# briefly. Without this a poll every two seconds would stat the whole data folder.
SORTIE_CACHE_SECONDS = 2.0


def _registry_install() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, REG_UNINSTALL) as key:
                value, _ = winreg.QueryValueEx(key, "InstallLocation")
        except OSError:
            continue
        path = Path(str(value))
        if path.is_dir():
            return path
    return None


def _steam_libraries() -> list[Path]:
    """Every Steam library root listed in libraryfolders.vdf."""
    out: list[Path] = []
    for base in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam",
    ):
        vdf = base / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s*"([^"]+)"', text):
            path = Path(match.group(1).replace("\\\\", "\\"))
            if path.is_dir():
                out.append(path)
    return out


def _scan_drives() -> list[Path]:
    found: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        for parent in (drive, drive / "Games", drive / "SteamLibrary" / "steamapps" / "common"):
            if not parent.is_dir():
                continue
            try:
                for child in parent.iterdir():
                    if child.is_dir() and child.name.lower().startswith("il-2 sturmovik"):
                        found.append(child)
            except OSError:
                continue
    return found


class Il2Install:
    """A discovered IL-2 Great Battles installation."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self._sortie_cache: tuple[float, list[Path]] = (0.0, [])

    # -- discovery -------------------------------------------------------

    @classmethod
    def discover(cls, override: str | os.PathLike | None = None) -> "Il2Install | None":
        override = override or os.environ.get("IL2_PATH")
        if override and Path(override).is_dir():
            return cls(Path(override))

        candidate = _registry_install()
        if candidate and (candidate / "data").is_dir():
            return cls(candidate)

        for library in _steam_libraries():
            path = library / "steamapps" / "common" / STEAM_FOLDER
            if (path / "data").is_dir():
                return cls(path)

        for path in _scan_drives():
            if (path / "data").is_dir():
                return cls(path)
        return None

    # -- paths -----------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self.base / "data"

    @property
    def missions_dir(self) -> Path:
        return self.data_dir / "Missions"

    @property
    def startup_cfg(self) -> Path:
        return self.data_dir / "startup.cfg"

    @property
    def gui_dir(self) -> Path:
        return self.data_dir / "GUI"

    @property
    def scripts_archive(self) -> Path:
        return self.data_dir / "Scripts.gtp"

    @property
    def campaigns_archive(self) -> Path:
        return self.data_dir / "Campaigns.gtp"

    # -- missions --------------------------------------------------------

    def missions(self) -> list[Path]:
        """Loose mission files under ``data\\Missions``, newest first."""
        found: list[Path] = []
        if not self.missions_dir.is_dir():
            return found
        try:
            for path in self.missions_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in MISSION_SUFFIXES:
                    continue
                parts = {p.lower() for p in path.relative_to(self.missions_dir).parts[:-1]}
                if parts & set(EXCLUDED_MISSION_DIRS):
                    continue
                found.append(path)
        except OSError:
            return found
        found.sort(key=_mtime, reverse=True)
        return found

    def latest_mission(self) -> Path | None:
        missions = self.missions()
        return missions[0] if missions else None

    # -- sortie logs -----------------------------------------------------

    def sorties(self) -> list[Path]:
        """Sortie header chunks, newest first.

        Only the ``[0]`` chunk of each sortie is listed. The later chunks grow
        every few seconds during flight, so keying freshness on them would rebuild
        the board continuously; ``[0]`` is written once at mission start and is
        where the player's spawn record lives. On this install that is 65 files
        rather than 1,541.
        """
        now = time.monotonic()
        stamped, cached = self._sortie_cache
        if cached and now - stamped < SORTIE_CACHE_SECONDS:
            return cached
        found: list[Path] = []
        if self.data_dir.is_dir():
            try:
                found = [
                    p
                    for p in self.data_dir.glob(SORTIE_GLOB)
                    if p.is_file() and SORTIE_RE.match(p.name)
                ]
            except OSError:
                found = []
            found.sort(key=_mtime, reverse=True)
        self._sortie_cache = (now, found)
        return found

    def latest_sortie(self) -> Path | None:
        sorties = self.sorties()
        return sorties[0] if sorties else None

    # -- scripted campaigns ----------------------------------------------

    def campaign_members(self, mission_file: str) -> dict:
        """Locate a scripted campaign mission's readable parts.

        DLC campaign missions live in ``Campaigns.gtp`` as ``.cmpbin`` -- a
        compiled binary this cannot read -- so there is no route, weather or
        planned loadout to be had. What *is* readable beside each one is its
        briefing text and its briefing map image, and the sortie log names which
        mission is loaded.
        """
        raw = str(mission_file or "").replace("\\", "/").strip().lower()
        if not raw or not self.campaigns_archive.is_file():
            return {}
        stem = raw.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        folder = raw.rsplit("/", 2)[-2] if "/" in raw else ""
        if not stem or not folder:
            return {}
        base = f"/campaigns/{folder}/{stem}"
        return {
            "campaign": folder,
            "mission": stem,
            "text": f"{base}.{self.language()}",
            "text_fallback": f"{base}.eng",
            "image": f"{base}.png",
        }

    # -- configuration ---------------------------------------------------

    def _startup_values(self, section: str) -> dict[str, str]:
        """Read one ``[KEY = <section>]`` block.

        Section awareness matters: ``language`` appears under both ``interface``
        and ``updater``, and only the first is the display language.
        """
        values: dict[str, str] = {}
        try:
            text = self.startup_cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return values
        current = ""
        for line in text.splitlines():
            stripped = line.strip()
            header = re.match(r"\[KEY\s*=\s*(\w+)\]", stripped)
            if header:
                current = header.group(1).lower()
                continue
            if stripped == "[END]":
                current = ""
                continue
            if current != section.lower():
                continue
            pair = re.match(r"(\w+)\s*=\s*(.*)$", stripped)
            if pair:
                values[pair.group(1)] = pair.group(2).strip().strip('"')
        return values

    def language(self) -> str:
        return (self._startup_values("interface").get("language") or "eng").lower()

    @property
    def text_log_enabled(self) -> bool:
        return self._startup_values("system").get("mission_text_log", "0").strip() == "1"

    @property
    def version(self) -> str:
        """IL-2 does not publish a build number in a loose file."""
        return ""

    def describe(self) -> dict:
        latest = self.latest_mission()
        sortie = self.latest_sortie()
        return {
            "base": str(self.base),
            "version": self.version,
            "mission_count": len(self.missions()),
            "latest_mission": str(latest) if latest else "",
            "latest_mission_name": latest.name if latest else "",
            "sortie_count": len(self.sorties()),
            "latest_sortie": str(sortie) if sortie else "",
            "latest_sortie_name": sortie.name if sortie else "",
            "language": self.language(),
            "text_log_enabled": self.text_log_enabled,
            "scripts_archive": self.scripts_archive.is_file(),
            "campaigns_archive": self.campaigns_archive.is_file(),
        }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
