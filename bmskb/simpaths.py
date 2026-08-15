"""Where each sim is installed: what was found, what the user said instead.

Discovery is good enough that most people will never open this. It is here for
the ones it fails: a registry key that was never written, a game moved to another
drive, a portable install. Before this existed the only answer was "set an
environment variable", which is no answer at all to someone who downloaded a zip
and double-clicked it.

A saved path is not trusted blindly. It is checked for the folders the game
itself puts at its root, so a mistyped or moved directory is reported as wrong
rather than accepted and turned into an empty board with no explanation.
"""

from __future__ import annotations

import os
from pathlib import Path

SIMS = ("bms", "dcs", "il2")

LABELS = {"bms": "Falcon BMS", "dcs": "DCS World", "il2": "IL-2 Great Battles"}
SETTING_KEYS = {"bms": "bms_path", "dcs": "dcs_path", "il2": "il2_path"}
ENV_KEYS = {"bms": "BMS_PATH", "dcs": "DCS_PATH", "il2": "IL2_PATH"}

#: Saved value meaning "find it yourself".
AUTO = ""
#: Saved value meaning "I do not have this game; stop looking".
NOT_INSTALLED = "off"

# Things the board itself reads out of each install. Any one is enough.
#
# Deliberately specific. The obvious markers -- Data, bin, data -- are shared by
# all three: checking those accepted a DCS folder as Falcon BMS, and accepted
# IL-2 Sturmovik 1946, a different game from 2006 with none of the same files,
# as IL-2 Great Battles. Both would have produced an empty board and no clue
# why, which is the exact failure this is here to prevent.
MARKERS = {
    "bms": ("Data/TerrData", "User/Briefings"),
    "dcs": ("Mods/terrains", "Mods/aircraft"),
    "il2": ("data/Swf.gtp", "data/Scripts.gtp"),
}

# What to say when the folder exists but is not the game.
WRONG_FOLDER = {
    "bms": "That does not look like Falcon BMS -- there is no Data\\TerrData or "
           "User\\Briefings inside. Pick the folder containing Launcher.exe.",
    "dcs": "That does not look like DCS World -- there is no Mods\\terrains "
           "inside. Pick the folder containing bin and Mods.",
    "il2": "That does not look like IL-2 Great Battles -- there is no "
           "data\\Swf.gtp inside. Pick the folder containing bin and data. "
           "(IL-2 Sturmovik 1946 is a different game and is not supported.)",
}


def verify(sim: str, path: str | os.PathLike) -> tuple[bool, str]:
    """Whether ``path`` looks like an install of ``sim``, and why not if it does not."""
    if sim not in SIMS:
        return False, f"{sim!r} is not a sim this board reads."
    text = str(path or "").strip().strip('"')
    if not text:
        return False, "No folder given."
    target = Path(text)
    if not target.exists():
        return False, "That folder does not exist."
    if not target.is_dir():
        return False, "That is a file, not a folder."
    if not any((target / marker).exists() for marker in MARKERS[sim]):
        return False, WRONG_FOLDER[sim]
    return True, ""


def override_for(sim: str, cli: dict, settings: dict) -> str | None:
    """The path to hand discovery, or ``NOT_INSTALLED`` to skip it entirely.

    Order: the command line, then the environment, then what was saved in the
    board, then automatic discovery. The first two are per-launch and explicit,
    so they win over a stored preference; a stored preference wins over guessing.
    """
    from_cli = (cli or {}).get(sim) or ""
    if from_cli:
        return str(from_cli)
    from_env = os.environ.get(ENV_KEYS[sim], "")
    if from_env:
        return from_env
    saved = str((settings or {}).get(SETTING_KEYS[sim], AUTO) or AUTO)
    if saved == NOT_INSTALLED:
        return NOT_INSTALLED
    return saved or None


def describe(sim: str, cli: dict, settings: dict, install) -> dict:
    """One row of the setup panel: what is configured, and what came of it."""
    saved = str((settings or {}).get(SETTING_KEYS[sim], AUTO) or AUTO)
    from_cli = bool((cli or {}).get(sim))
    from_env = bool(os.environ.get(ENV_KEYS[sim], ""))
    base = ""
    if install is not None and getattr(install, "base", None):
        base = str(install.base)

    if saved == NOT_INSTALLED:
        source, note = "off", "You said this one is not installed."
    elif from_cli:
        source, note = "command-line", "Set for this run by a command-line option."
    elif from_env:
        source, note = "environment", f"Set for this run by {ENV_KEYS[sim]}."
    elif saved:
        source, note = "saved", "Set here."
    else:
        source, note = "found", "Found automatically."

    found = bool(base)
    if not found and saved != NOT_INSTALLED:
        note = (
            "Not found automatically. If you have it, point the board at it; "
            "if you do not, mark it as not installed so it stops looking."
        )

    return {
        "sim": sim,
        "label": LABELS[sim],
        "path": base or (saved if saved != NOT_INSTALLED else ""),
        "saved": saved,
        "found": found,
        "source": source,
        "note": note,
        # A path fixed for this run cannot be changed from the page without the
        # change appearing to do nothing, so the row says so instead.
        "locked": from_cli or from_env,
    }
