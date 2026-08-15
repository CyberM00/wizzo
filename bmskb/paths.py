"""Where the board reads what ships with it, and where it is allowed to write.

Running from a git clone these are the same folder, which is why everything used
to derive both from ``__file__``. A packaged copy breaks that assumption twice
over: the code is inside the bundle, and the folder it was unzipped into may not
be writable at all -- Program Files is the obvious case, but so is any folder the
user extracted as read-only.

So the two are separated. Bundled data resolves against the bundle; caches and
settings go to the per-user application data folder. The distinction only bites
when frozen, so a clone keeps writing beside its own source exactly as before and
nothing about the developer workflow changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: True when running from a PyInstaller build rather than a source checkout.
FROZEN = bool(getattr(sys, "frozen", False))

# Deliberately not the project's display name: this folder outlives any rename,
# and moving it would silently orphan a user's settings.
STATE_DIRNAME = "Kneeboard"


def resource_root() -> Path:
    """The folder bundled read-only files were unpacked to."""
    if FROZEN:
        # PyInstaller sets _MEIPASS for both one-file and one-folder builds; the
        # executable's own folder is the honest fallback if that ever changes.
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return Path(__file__).resolve().parent.parent


def state_root() -> Path:
    """A folder the board may write to, whatever it was installed into."""
    if not FROZEN:
        return Path(__file__).resolve().parent.parent
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base and Path(base).is_dir():
        return Path(base) / STATE_DIRNAME
    return Path.home() / f".{STATE_DIRNAME.lower()}"


def state_path(*parts: str) -> Path:
    """A path inside the writable area. The parent is created on demand."""
    target = state_root().joinpath(*parts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def app_root() -> Path:
    """The folder the board was started from, for the self-update check."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def describe() -> dict:
    return {
        "frozen": FROZEN,
        "resources": str(resource_root()),
        "state": str(state_root()),
        "app": str(app_root()),
    }
