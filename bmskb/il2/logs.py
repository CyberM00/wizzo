"""Read the sortie log IL-2 writes when a mission starts.

This is the only source for what the player *actually* flew. The mission file
records the generator's default loadout and is never updated when you change it in
the arming screen -- one career mission here was reused across five sorties with
different loadouts each time.

Only the ``[0]`` chunk is read, and only its first few kilobytes: the header is
line 3 and the player's spawn record is around line 60. The later chunks are the
event stream and grow throughout the flight.

Logging depends on ``mission_text_log = 1`` in ``data\\startup.cfg``. It is on by
default; when it is off there is no as-flown source at all and the board says so
rather than presenting the planned loadout as fact.
"""

from __future__ import annotations

import re
from pathlib import Path

HEAD_BYTES = 16 * 1024

# Values can contain spaces -- "TYPE:Bf 109 G-2" and mission paths with spaces --
# so every field is bounded by the key that follows it rather than by whitespace.
MISSION_FILE_RE = re.compile(r"\bMFile:(.*?)\s+MID:")
TYPE_RE = re.compile(r"\bTYPE:(.*?)\s+COUNTRY:")
SKIN_RE = re.compile(r"\bSKIN:(.*?)\s+WM:")
GDATE_RE = re.compile(r"\bGDate:(\S+)")
GTIME_RE = re.compile(r"\bGTime:(\S+)")
GTYPE_RE = re.compile(r"\bGType:(\S+)")

SPAWN_LINE_RE = re.compile(r"\bAType:10\b")
HEADER_LINE_RE = re.compile(r"\bAType:0\b")


def _field(line: str, key: str):
    match = re.search(rf"\b{key}:(-?[\d.]+)\b", line)
    return match.group(1) if match else None


def _num(text, cast=int, default=None):
    try:
        return cast(text)
    except (TypeError, ValueError):
        return default


class SortieLog:
    """The header and player spawn record of one sortie."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.mission_file = ""
        self.game_date = ""
        self.game_time = ""
        self.game_type = ""
        self.player: dict | None = None
        self.ok = False
        self.error = ""
        self._read()

    def _read(self) -> None:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(HEAD_BYTES)
        except OSError as exc:
            self.error = f"could not read {self.path.name}: {exc}"
            return
        text = raw.decode("utf-8", "replace")

        for line in text.splitlines():
            if not self.mission_file and HEADER_LINE_RE.search(line):
                found = MISSION_FILE_RE.search(line)
                self.mission_file = found.group(1).strip() if found else ""
                for attr, pattern in (
                    ("game_date", GDATE_RE),
                    ("game_time", GTIME_RE),
                    ("game_type", GTYPE_RE),
                ):
                    hit = pattern.search(line)
                    if hit:
                        setattr(self, attr, hit.group(1).strip())
                continue

            if self.player is None and SPAWN_LINE_RE.search(line) and " ISPL:1" in f" {line}":
                self.player = self._parse_spawn(line)

        self.ok = bool(self.mission_file or self.player)

    @staticmethod
    def _parse_spawn(line: str) -> dict:
        aircraft = TYPE_RE.search(line)
        skin = SKIN_RE.search(line)
        name = re.search(r"\bNAME:(.*?)\s+TYPE:", line)
        return {
            "aircraft": aircraft.group(1).strip() if aircraft else "",
            "pilot": name.group(1).strip() if name else "",
            "skin": skin.group(1).strip() if skin else "",
            "payload_id": _num(_field(line, "PAYLOAD")),
            "weapon_mask": _num(_field(line, "WM")),
            "fuel": _num(_field(line, "FUEL"), float),
            "formation": _num(_field(line, "FORM")),
            "country": _num(_field(line, "COUNTRY")),
            "in_air": _num(_field(line, "INAIR")),
            "rounds": {
                "bullets": _num(_field(line, "BUL")),
                "shells": _num(_field(line, "SH")),
                "bombs": _num(_field(line, "BOMB")),
                "rockets": _num(_field(line, "RCT")),
            },
        }

    # -- correlation -----------------------------------------------------

    def matches(self, mission_path: Path, options: dict) -> dict:
        """Decide whether this log describes the mission currently on disk.

        Career reuses one generated mission across sorties with identical date and
        time, so "which sortie" is unanswerable. The useful question is whether the
        log belongs to the mission we just parsed, which these four tests answer.
        """
        reasons: list[str] = []

        logged_stem = Path(self.mission_file.replace("\\", "/")).stem.casefold()
        same_file = bool(logged_stem) and logged_stem == Path(mission_path).stem.casefold()
        if not same_file:
            reasons.append(
                f"the log names {self.mission_file or 'no mission'}, not {Path(mission_path).name}"
            )

        same_date = _same_date(self.game_date, options.get("Date", ""))
        if not same_date:
            reasons.append(f"log date {self.game_date or '?'} does not match the mission's")

        same_time = _same_time(self.game_time, options.get("Time", ""))
        if not same_time:
            reasons.append(f"log time {self.game_time or '?'} does not match the mission's")

        newer = _mtime(self.path) >= _mtime(Path(mission_path)) - 5
        if not newer:
            reasons.append("the log predates the mission file")

        if same_file and same_date and same_time and newer:
            confidence = "matched"
        elif same_file and newer:
            confidence = "same-mission-file"
        else:
            confidence = "unmatched"

        return {
            "confidence": confidence,
            "reasons": reasons,
            "log": self.path.name,
            "mission_file": self.mission_file,
        }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _same_date(logged: str, mission: str) -> bool:
    """Log writes ``Y.M.D``; the mission's Options block writes ``D.M.Y``."""
    left = [p for p in str(logged).split(".") if p.strip().isdigit()]
    right = [p for p in str(mission).strip().strip('"').split(".") if p.strip().isdigit()]
    if len(left) != 3 or len(right) != 3:
        return False
    return [int(x) for x in left] == [int(right[2]), int(right[1]), int(right[0])]


def _same_time(logged: str, mission: str) -> bool:
    left = [p for p in str(logged).split(":") if p.strip().isdigit()]
    right = [p for p in str(mission).strip().strip('"').split(":") if p.strip().isdigit()]
    if not left or not right:
        return False
    pad = lambda parts: [int(x) for x in (parts + ["0", "0", "0"])[:3]]
    return pad(left) == pad(right)


def newest_for(install, mission_path: Path, options: dict) -> tuple["SortieLog | None", dict]:
    """The newest sortie log, plus how well it corresponds to this mission."""
    latest = install.latest_sortie()
    if latest is None:
        return None, {
            "confidence": "none",
            "reasons": ["no sortie log found"],
            "log": "",
            "mission_file": "",
        }
    log = SortieLog(latest)
    if not log.ok:
        return None, {
            "confidence": "none",
            "reasons": [log.error or f"{latest.name} could not be parsed"],
            "log": latest.name,
            "mission_file": "",
        }
    return log, log.matches(mission_path, options)
