"""Weapon reference built from ``Falcon4_WCD.xml`` plus a curated store library.

What comes from where, and why:

* **Game data** (``Falcon4_WCD.xml``) supplies weight in pounds and, for
  missiles, a maximum range in nautical miles. Both were checked against known
  values (Mk-84 = 2039 lb, AIM-120C = 36 nm).
* **Curated data** (``data/f16_stores.json``) supplies employment guidance,
  delivery profiles, fuzing and laser-code applicability. None of that exists
  in the game files -- it comes from the BMS manuals and -34 procedures.

Two deliberate omissions:

* ``Range`` is only surfaced for missiles. For bombs BMS stores 0-2 as a
  campaign-engine artefact, so showing it as a release range would be wrong.
* The numeric ``Guidance``/``DamageType`` enums are not translated. Value 0
  covers both ballistic bombs and GPS JDAMs, so any label derived from it would
  be misleading. Guidance comes from the curated library instead.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# Stores whose WCD "Range" is a real employment range rather than an artefact.
RANGED_CATEGORIES = {"aam", "agm", "arm", "standoff", "gun-pod"}


def _normalise(name: str) -> str:
    """Loose key for matching briefing store names against WCD names."""
    text = name.lower()
    text = re.sub(r"/(he|ap|inert)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


class WeaponLibrary:
    """Look up a store by the name that appears in briefing.txt."""

    def __init__(self, wcd_path: Path | None):
        self.wcd_path = Path(wcd_path) if wcd_path else None
        self.by_name: dict[str, dict] = {}
        self.by_norm: dict[str, dict] = {}
        self.curated: list[dict] = []
        self.errors: list[str] = []
        self._load_curated()
        self._load_wcd()

    # -- loading ---------------------------------------------------------

    def _load_curated(self) -> None:
        path = DATA_DIR / "f16_stores.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.curated = payload.get("stores", [])
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f"curated store library unavailable: {exc}")

    def _load_wcd(self) -> None:
        if not self.wcd_path or not self.wcd_path.is_file():
            self.errors.append("Falcon4_WCD.xml not found; weights and ranges unavailable")
            return
        try:
            root = ET.parse(self.wcd_path).getroot()
        except (OSError, ET.ParseError) as exc:
            self.errors.append(f"could not read Falcon4_WCD.xml: {exc}")
            return

        for node in root:
            name = (node.findtext("Name") or "").strip()
            if not name or name.startswith("-"):
                continue
            entry = {
                "name": name,
                "weight_lb": _as_number(node.findtext("Weight")),
                "range_nm": _as_number(node.findtext("Range")),
                "blast_radius": _as_number(node.findtext("BlastRadius")),
                "in_service": (node.findtext("InServiceStart") or "").strip(),
            }
            self.by_name.setdefault(name, entry)
            self.by_norm.setdefault(_normalise(name), entry)

    # -- lookup ----------------------------------------------------------

    def _curated_for(self, name: str) -> dict | None:
        norm = _normalise(name)
        best: dict | None = None
        best_len = -1
        for store in self.curated:
            for pattern in store.get("match", []):
                pnorm = _normalise(pattern)
                if not pnorm:
                    continue
                # Prefer the longest matching pattern so "GBU-31(v)3/B" wins
                # over a generic "GBU-31" entry.
                if norm == pnorm or norm.startswith(pnorm):
                    if len(pnorm) > best_len:
                        best, best_len = store, len(pnorm)
        return best

    def lookup(self, name: str) -> dict:
        """Resolve a briefing store name into a display record."""
        game = self.by_name.get(name) or self.by_norm.get(_normalise(name))
        curated = self._curated_for(name)

        record: dict = {
            "name": name,
            "matched_game_name": game["name"] if game else "",
            "known": bool(game or curated),
            "category": (curated or {}).get("category", ""),
            "category_label": (curated or {}).get("category_label", ""),
            "guidance": (curated or {}).get("guidance", ""),
            "role": (curated or {}).get("role", ""),
            "employment": (curated or {}).get("employment", []),
            "fuzing": (curated or {}).get("fuzing", ""),
            "laser": bool((curated or {}).get("laser", False)),
            "laser_note": (curated or {}).get("laser_note", ""),
            "requires": (curated or {}).get("requires", []),
            "weight_lb": None,
            "range_nm": None,
            "blast_radius": None,
        }

        if game:
            record["weight_lb"] = game["weight_lb"]
            record["blast_radius"] = game["blast_radius"] or None
            category = record["category"]
            if game["range_nm"] and category in RANGED_CATEGORIES:
                record["range_nm"] = game["range_nm"]

        return record

    def enrich_flight(self, flight: dict) -> dict:
        """Attach weapon records and totals to one parsed ordnance flight."""
        aircraft = []
        for entry in flight.get("aircraft", []):
            stores = []
            total = 0.0
            for store in entry.get("stores", []):
                record = self.lookup(store["name"])
                record = dict(record, count=store["count"])
                if record["weight_lb"]:
                    total += record["weight_lb"] * store["count"]
                stores.append(record)
            aircraft.append(
                {
                    "label": entry.get("label", ""),
                    "stores": stores,
                    "total_weight_lb": round(total) if total else None,
                }
            )

        laser_stores = sorted(
            {s["name"] for entry in aircraft for s in entry["stores"] if s["laser"]}
        )
        return {
            "callsign": flight.get("callsign", ""),
            "uniform": flight.get("uniform", True),
            "aircraft": aircraft,
            "laser_stores": laser_stores,
            "needs_laser_code": bool(laser_stores),
        }


def _as_number(text: str | None):
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value
