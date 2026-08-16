"""Resolve DCS pylon CLSID codes against the curated store library.

DCS assembles its own CLSID-to-name mapping by executing Lua at load time, so
there is no table in the game files to read. Three extraction approaches were
tried and all were rejected: proximity matching reached 44% and mislabelled an
ALQ-184 as a Soviet recon pod; enclosing-block matching reached 35% with
conflicts; literal declaration arguments were accurate but covered only a
handful.

So names come from a hand-curated library keyed on CLSID, which carries the
employment detail as well as the name.

Anything the library does not cover falls back to ``clsidnames``, which reads the
comment DCS itself writes on the same line as the code in the files it ships.
That is a name and nothing else -- no employment guidance, no laser
applicability -- and it is labelled as coming from the game's files rather than
from curated data, because a developer comment is not a specification.

A code neither can name is shown raw, the same treatment BMS stores without
reference data already get.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import clsidnames

DATA_DIR = Path(__file__).parent / "data"

# Racks encode their contents in the CLSID: "BRU-42_2*GBU-38_LEFT" is two bombs.
RACK_COUNT_RE = re.compile(r"[_x](\d+)\s*\*", re.IGNORECASE)


def _normalise(clsid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clsid.lower())


class DcsWeaponLibrary:
    def __init__(self, install_base=None) -> None:
        self.stores: list[dict] = []
        self.errors: list[str] = []
        self._by_norm: dict[str, dict] = {}
        self._load()
        # Names DCS writes about its own codes. Read from the install, so it is
        # empty when there is no install to read -- in which case unknown codes
        # are shown raw exactly as before.
        try:
            self.from_game = clsidnames.load(install_base)
        except Exception as exc:  # noqa: BLE001 - a name lookup must never break the board
            self.from_game = {}
            self.errors.append(f"Could not read DCS's own store names: {exc}")

    def _load(self) -> None:
        path = DATA_DIR / "dcs_stores.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f"DCS store library unavailable: {exc}")
            return
        self.stores = payload.get("stores", [])
        for store in self.stores:
            for code in store.get("match", []):
                self._by_norm.setdefault(_normalise(code), store)

    def lookup(self, clsid: str) -> dict:
        store = self._by_norm.get(_normalise(clsid))

        # A rack CLSID names its own quantity; prefer that over the curated
        # default, since the same weapon appears on several rack types.
        quantity = (store or {}).get("quantity", 1)
        rack = RACK_COUNT_RE.search(clsid)
        if rack:
            try:
                quantity = int(rack.group(1))
            except ValueError:
                pass

        if not store:
            # DCS's own comment about this code, when it has one.
            from_game = self.from_game.get(clsid) or self.from_game.get(clsid.strip("{}"))
            return {
                "name": from_game or clsid,
                "clsid": clsid,
                "known": False,
                # Named, but only as far as a developer comment goes: no
                # employment detail, and the page says where the name came from.
                "named_by_game": bool(from_game),
                "category": "",
                "category_label": "",
                "guidance": "",
                "role": "",
                "employment": [],
                "fuzing": "",
                "laser": False,
                "laser_note": "",
                "requires": [],
                "count": quantity,
                "weight_lb": None,
                "range_nm": None,
                "blast_radius": None,
            }

        return {
            "name": store.get("name", clsid),
            "clsid": clsid,
            "known": True,
            "named_by_game": False,
            "category": store.get("category", ""),
            "category_label": store.get("category_label", ""),
            "guidance": store.get("guidance", ""),
            "role": store.get("role", ""),
            "employment": store.get("employment", []),
            "fuzing": store.get("fuzing", ""),
            "laser": bool(store.get("laser", False)),
            "laser_note": store.get("laser_note", ""),
            "requires": store.get("requires", []),
            "count": quantity,
            # DCS does not publish a per-store weight or range we can trust the
            # way Falcon4_WCD.xml can be trusted, so these stay empty rather
            # than being invented.
            "weight_lb": None,
            "range_nm": None,
            "blast_radius": None,
        }
