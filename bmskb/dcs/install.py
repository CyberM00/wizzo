"""Locate the DCS World installation and the missions it can show.

DCS exports nothing for external tools -- there is no equivalent of BMS's
``briefing.txt``. Everything a kneeboard needs lives inside the ``.miz`` the
mission was saved as, so "the current mission" means "the most recently written
mission file", found under the Saved Games folders.
"""

from __future__ import annotations

import os
import string
from pathlib import Path

REG_PATHS = [
    (r"SOFTWARE\Eagle Dynamics\DCS World", "Path"),
    (r"SOFTWARE\Eagle Dynamics\DCS World OpenBeta", "Path"),
    (r"SOFTWARE\WOW6432Node\Eagle Dynamics\DCS World", "Path"),
]

# Saved Games variant names, most preferred first.
SAVED_VARIANTS = ("DCS", "DCS.openbeta", "DCS.earlyaccess")


def _registry_paths() -> list[Path]:
    try:
        import winreg
    except ImportError:
        return []
    out: list[Path] = []
    for key_path, value in REG_PATHS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, key_path) as key:
                    data, _ = winreg.QueryValueEx(key, value)
            except OSError:
                continue
            path = Path(str(data))
            if path.is_dir() and path not in out:
                out.append(path)
    return out


def _scan_drives() -> list[Path]:
    found: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        for parent in (drive, drive / "Program Files", drive / "Program Files (x86)", drive / "Games"):
            if not parent.is_dir():
                continue
            try:
                for child in parent.iterdir():
                    if child.is_dir() and child.name.lower().startswith("dcs world"):
                        found.append(child)
            except OSError:
                continue
    return found


class DcsInstall:
    """A discovered DCS installation plus its Saved Games mission folders."""

    def __init__(self, base: Path | None, saved_dirs: list[Path] | None = None):
        self.base = Path(base) if base else None
        self.saved_dirs = saved_dirs or []

    @classmethod
    def discover(cls, override: str | os.PathLike | None = None) -> "DcsInstall | None":
        base: Path | None = None
        override = override or os.environ.get("DCS_PATH")
        if override and Path(override).is_dir():
            base = Path(override)
        if base is None:
            candidates = _registry_paths() or _scan_drives()
            base = candidates[0] if candidates else None

        saved = cls._saved_dirs()
        if base is None and not saved:
            return None
        return cls(base, saved)

    @staticmethod
    def _saved_dirs() -> list[Path]:
        root = Path.home() / "Saved Games"
        out: list[Path] = []
        if not root.is_dir():
            return out
        for name in SAVED_VARIANTS:
            path = root / name
            if path.is_dir():
                out.append(path)
        return out

    # -- missions --------------------------------------------------------

    def missions(self) -> list[Path]:
        """Every ``.miz`` under the Saved Games mission folders, newest first."""
        found: list[Path] = []
        for saved in self.saved_dirs:
            missions = saved / "Missions"
            if not missions.is_dir():
                continue
            try:
                found.extend(p for p in missions.rglob("*.miz") if p.is_file())
            except OSError:
                continue
        found.sort(key=lambda p: _mtime(p), reverse=True)
        return found

    def latest_mission(self) -> Path | None:
        missions = self.missions()
        return missions[0] if missions else None

    @property
    def version(self) -> str:
        """Read the build version DCS records next to the executable."""
        if not self.base:
            return ""
        for name in ("autoupdate.cfg",):
            path = self.base / name
            if not path.is_file():
                continue
            try:
                import json

                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                return str(data.get("version", ""))
            except (OSError, ValueError):
                continue
        return ""

    def describe(self) -> dict:
        latest = self.latest_mission()
        return {
            "base": str(self.base) if self.base else "",
            "version": self.version,
            "saved_dirs": [str(p) for p in self.saved_dirs],
            "mission_count": len(self.missions()),
            "latest_mission": str(latest) if latest else "",
            "latest_mission_name": latest.name if latest else "",
        }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
