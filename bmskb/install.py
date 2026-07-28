"""Locate the Falcon BMS installation and the files we read out of it.

Discovery order:
  1. BMS_PATH environment variable
  2. Windows registry (HKLM\\SOFTWARE\\[WOW6432Node\\]Benchmark Sims\\*)
  3. A scan of common install locations across all fixed drives
"""

from __future__ import annotations

import os
import re
import string
from pathlib import Path

REG_ROOTS = [
    r"SOFTWARE\WOW6432Node\Benchmark Sims",
    r"SOFTWARE\Benchmark Sims",
]

# Text files BMS writes are cp1252 (smart quotes in the emergency-procedures block),
# but campaign-supplied strings occasionally arrive as utf-8. Try in this order.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def read_text(path: Path) -> str:
    """Read a BMS text file, tolerating its mixed encodings."""
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _decode_reg_string(value) -> str:
    """PilotCallsign/PilotName are stored as NUL-padded byte arrays."""
    if isinstance(value, str):
        return value.strip("\x00").strip()
    try:
        return bytes(value).split(b"\x00")[0].decode("cp1252", errors="replace").strip()
    except Exception:
        return ""


def _registry_installs() -> list[tuple[str, Path, dict]]:
    """Return (version, baseDir, extra_values) for every BMS install in the registry."""
    try:
        import winreg
    except ImportError:
        return []

    found: list[tuple[str, Path, dict]] = []
    for root in REG_ROOTS:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub_name) as sub:
                        values: dict = {}
                        for j in range(winreg.QueryInfoKey(sub)[1]):
                            name, data, _ = winreg.EnumValue(sub, j)
                            values[name] = data
                        base = values.get("baseDir")
                        if not base:
                            continue
                        base_path = Path(str(base))
                        if not base_path.is_dir():
                            continue
                        version = _version_from_name(sub_name)
                        found.append((version, base_path, values))
                except OSError:
                    continue
    return found


def _version_from_name(name: str) -> str:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", name)
    return match.group(1) if match else name


def _version_key(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _scan_drives() -> list[Path]:
    """Last-resort scan for a 'Falcon BMS <version>' directory."""
    candidates: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        for parent in (drive, drive / "Program Files", drive / "Program Files (x86)", drive / "Games"):
            if not parent.is_dir():
                continue
            try:
                for child in parent.iterdir():
                    if child.is_dir() and child.name.lower().startswith("falcon bms"):
                        candidates.append(child)
            except OSError:
                continue
    return candidates


class BmsInstall:
    """A discovered BMS installation, with resolved paths to everything we read."""

    def __init__(self, base: Path, version: str = "", registry: dict | None = None):
        self.base = Path(base)
        self.version = version or _version_from_name(self.base.name)
        self.registry = registry or {}

    # -- discovery -------------------------------------------------------

    @classmethod
    def discover(cls, override: str | os.PathLike | None = None) -> "BmsInstall | None":
        override = override or os.environ.get("BMS_PATH")
        if override:
            path = Path(override)
            if path.is_dir():
                return cls(path)

        installs = _registry_installs()
        if installs:
            installs.sort(key=lambda item: _version_key(item[0]), reverse=True)
            version, base, values = installs[0]
            return cls(base, version, values)

        scanned = _scan_drives()
        if scanned:
            scanned.sort(key=lambda p: _version_key(p.name), reverse=True)
            return cls(scanned[0])

        return None

    # -- paths -----------------------------------------------------------

    @property
    def briefings_dir(self) -> Path:
        return self.base / "User" / "Briefings"

    @property
    def briefing_file(self) -> Path:
        return self.briefings_dir / "briefing.txt"

    @property
    def dtc_comm_file(self) -> Path:
        return self.briefings_dir / "dtc_comm.txt"

    @property
    def objects_dir(self) -> Path:
        return self.base / "Data" / "TerrData" / "Objects"

    @property
    def wcd_file(self) -> Path:
        return self.objects_dir / "Falcon4_WCD.xml"

    @property
    def docs_dir(self) -> Path:
        return self.base / "Docs"

    @property
    def charts_dir(self) -> Path:
        return self._docs_subdir("KTO Charts")

    @property
    def maps_dir(self) -> Path:
        return self._docs_subdir("Maps")

    def _docs_subdir(self, needle: str) -> Path:
        """Docs folders are numbered ('03 KTO Charts'); match on the name, not the number."""
        needle = needle.lower()
        if self.docs_dir.is_dir():
            for child in sorted(self.docs_dir.iterdir()):
                if child.is_dir() and needle in child.name.lower():
                    return child
        return self.docs_dir / needle

    # -- metadata --------------------------------------------------------

    @property
    def pilot_callsign(self) -> str:
        return _decode_reg_string(self.registry.get("PilotCallsign", ""))

    @property
    def pilot_name(self) -> str:
        return _decode_reg_string(self.registry.get("PilotName", ""))

    @property
    def theater(self) -> str:
        value = self.registry.get("curTheater", "")
        return value if isinstance(value, str) else ""

    def describe(self) -> dict:
        return {
            "base": str(self.base),
            "version": self.version,
            "theater": self.theater,
            "pilot_callsign": self.pilot_callsign,
            "pilot_name": self.pilot_name,
            "briefing_file": str(self.briefing_file),
            "briefing_exists": self.briefing_file.is_file(),
            "dtc_comm_exists": self.dtc_comm_file.is_file(),
            "wcd_exists": self.wcd_file.is_file(),
            "charts_dir": str(self.charts_dir),
            "charts_exists": self.charts_dir.is_dir(),
            "maps_dir": str(self.maps_dir),
            "maps_exists": self.maps_dir.is_dir(),
        }
