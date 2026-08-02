"""Resolve IL-2 payload identifiers into readable stores.

Unlike the DCS side, the names here come from the game's own tables rather than a
curated file -- see ``extract.py`` for why that is possible and preferable. This
module is the lookup layer over those tables plus the honest-unknown convention
the rest of the app already uses: anything that cannot be resolved is shown as its
raw identifier and flagged, never guessed at.

Two things are deliberately withheld from the records this produces:

* **Weapon modifications.** The mission file writes ``WMMask`` as base-2 digits
  and the log writes the same value in decimal, but which end the digit string
  starts from is not confirmed. Bit 0 is set either way, so a wrong reading would
  look plausible while mislabelling every modification.
* **Gun round counts.** A payload label like ``SHKAS-AP-1500`` carries a number,
  but whether it is per-gun or a total for the pair is not established.

Both are extracted and carried in the tables, so surfacing them later is a display
change rather than a re-extraction.
"""

from __future__ import annotations

from pathlib import Path

from . import aircraft as aircraft_notes
from .extract import ExtractError, load_tables


class Il2WeaponLibrary:
    """Loadout lookup for one IL-2 installation.

    Built lazily: a user who never opens the IL-2 board should not pay for reading
    the archives, and a slow disk should not delay the server starting.
    """

    def __init__(self, install_base: Path | None, locale: str = "eng"):
        self.install_base = Path(install_base) if install_base else None
        self.locale = locale
        self.tables: dict = {}
        self.origin = ""
        self.errors: list[str] = []
        self._loaded = False

    # -- loading ---------------------------------------------------------

    def ensure_loaded(self) -> bool:
        if self._loaded:
            return bool(self.tables)
        self._loaded = True
        if not self.install_base:
            return False
        try:
            self.tables, self.origin = load_tables(self.install_base, self.locale)
        except ExtractError as exc:
            self.errors.append(str(exc))
            self.tables = {}
        except Exception as exc:  # never let a data problem stop the board
            self.errors.append(f"could not read IL-2's weapon tables: {exc}")
            self.tables = {}
        return bool(self.tables)

    @property
    def aircraft(self) -> dict:
        return self.tables.get("aircraft", {})

    def describe(self) -> dict:
        stats = self.tables.get("stats", {})
        return {
            "origin": self.origin,
            "aircraft": stats.get("aircraft", 0),
            "names": stats.get("names", 0),
            "unresolved": stats.get("unresolved", []),
            "locale": self.tables.get("source", {}).get("locale", ""),
        }

    # -- lookup ----------------------------------------------------------

    def find_aircraft(self, script: str = "", display_name: str = "") -> tuple[str, dict]:
        """Resolve an aircraft by its script path or its display name.

        Both keys are exact and were verified collision-free across all 120
        aircraft. A display name that does not match exactly is *not* fuzzy-matched:
        "Bf 109 G-2" and "Bf 109 G-4" differ by one character and are different
        aircraft with different loadouts.
        """
        if not self.ensure_loaded():
            return "", {}

        if script:
            key = _normalise_script(script)
            record = self.aircraft.get(key)
            if record:
                return key, record

        if display_name:
            wanted = display_name.strip().casefold()
            for key, record in self.aircraft.items():
                if record.get("object_name", "").strip().casefold() == wanted:
                    return key, record

        return "", {}

    def stores(self, record: dict, payload_id) -> tuple[list[dict], str]:
        """Display records for one payload, plus the raw label behind them."""
        if not record:
            return [], ""
        payloads = record.get("payloads", {})
        payload = payloads.get(str(payload_id))
        if payload is None:
            return [], ""

        stores = []
        for item in payload.get("items", []):
            stores.append(self._store(item))
        return stores, payload.get("raw", "")

    @staticmethod
    def _store(item: dict) -> dict:
        known = bool(item.get("known"))
        count = item.get("count")
        carriers = item.get("carriers")
        stations = item.get("stations") or []

        # Guns carry no quantity in the label -- the station list is the quantity.
        # "2x MG 17 on stations 0, 1" is what the aircraft actually has.
        if item.get("kind") == "gun" and stations:
            count = len(stations)

        detail = []
        if item.get("nation"):
            detail.append(item["nation"])
        if carriers:
            detail.append(f"in {carriers} carriers")
        if stations:
            detail.append("station " + ", ".join(str(s) for s in stations))

        return {
            "name": item.get("name") or item.get("code") or "Unknown store",
            "code": item.get("code", ""),
            "count": count if count else 1,
            "known": known,
            "category": item.get("kind", ""),
            "category_label": _KIND_LABELS.get(item.get("kind", ""), ""),
            "guidance": "",
            "role": ", ".join(detail),
            "employment": [],
            "fuzing": "",
            "laser": False,
            "laser_note": "",
            "requires": [],
            "weight_lb": None,
            "range_nm": None,
            "blast_radius": None,
        }

    def notes_for(self, code: str) -> dict:
        """IL-2's own technical notes for an aircraft, structured for display.

        These are the figures and handling notes the game shows in its aircraft
        screen -- speed and load limits, engine modes with their time limits,
        temperature limits, and the recommended control positions.
        """
        if not self.ensure_loaded() or not code:
            return {"available": False, "name": "", "summary": [], "sections": []}
        entry = (self.tables.get("info") or {}).get(str(code).lower())
        if not entry:
            return {"available": False, "name": "", "summary": [], "sections": []}
        description = entry.get("description", "")
        return {
            "available": bool(description),
            "name": entry.get("name", ""),
            "summary": aircraft_notes.summary(description),
            "sections": aircraft_notes.to_prose(description),
        }

    def unknown_codes(self, stores: list[dict]) -> list[str]:
        return sorted({s["code"] for s in stores if not s["known"] and s["code"]})


_KIND_LABELS = {
    "gun": "Gun",
    "bomb": "Bomb",
    "rocket": "Rocket",
    "torpedo": "Torpedo",
    "other": "Other store",
    "empty": "Empty",
    "": "",
}


def _normalise_script(script: str) -> str:
    text = str(script).replace("\\", "/").strip().lower()
    if not text.startswith("/"):
        text = "/" + text
    return text
