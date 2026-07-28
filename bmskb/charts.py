"""Index the KTO charts and theatre maps shipped in the BMS ``Docs`` tree.

Charts live in ``Docs/03 KTO Charts/<country>/<Base Name (ICAO)>/`` as a mix of
PDF approach plates and PNG airfield diagrams. The folder name carries the ICAO
code, which is what lets a parsed briefing pick its own departure, recovery,
alternate and target plates automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

ICAO_RE = re.compile(r"\(([A-Z]{4})\)")
CHART_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif"}
MAP_SUFFIXES = {".png", ".jpg", ".jpeg"}

# Chart filename fragments -> human label, used to sort plates into a sensible
# reading order rather than raw alphabetical.
CHART_KINDS = [
    ("_agc", "Airfield diagram", 0),
    ("_apc", "Approach chart", 1),
    ("_eor", "End of runway", 2),
    ("ils", "ILS / LOC-DME", 3),
    ("tacan", "TACAN / VOR-DME", 4),
    ("rnav", "RNAV / GPS", 5),
    ("_dep", "Departure", 6),
    ("dep.", "Departure", 6),
    ("sid", "Departure", 6),
    ("star", "Arrival", 7),
    ("visual", "Visual", 8),
]


def _classify(filename: str) -> tuple[str, int]:
    lowered = filename.lower()
    for fragment, label, order in CHART_KINDS:
        if fragment in lowered:
            return label, order
    return "Other", 99


def _clean_title(stem: str, base_name: str) -> str:
    """'Osan AB (RKSO) - ils_or_locdme_rwy_09l' -> 'ils or locdme rwy 09l'."""
    title = stem
    if base_name and title.lower().startswith(base_name.lower()):
        title = title[len(base_name):]
    title = title.lstrip(" -_")
    title = title.replace("_", " ").strip()
    return title or stem


class ChartLibrary:
    """Airfield charts and theatre maps, indexed by ICAO and by name."""

    def __init__(self, charts_dir: Path | None, maps_dir: Path | None):
        self.charts_dir = Path(charts_dir) if charts_dir else None
        self.maps_dir = Path(maps_dir) if maps_dir else None
        self.airfields: list[dict] = []
        self.maps: list[dict] = []
        self._by_icao: dict[str, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._scan_charts()
        self._scan_maps()

    # -- scanning --------------------------------------------------------

    def _scan_charts(self) -> None:
        if not self.charts_dir or not self.charts_dir.is_dir():
            return
        for country_dir in sorted(self.charts_dir.iterdir()):
            if not country_dir.is_dir():
                continue
            country = re.sub(r"^\d+\s*", "", country_dir.name)
            for base_dir in sorted(country_dir.iterdir()):
                if not base_dir.is_dir():
                    continue
                airfield = self._build_airfield(base_dir, country)
                if airfield["charts"]:
                    self.airfields.append(airfield)

        for airfield in self.airfields:
            if airfield["icao"]:
                self._by_icao.setdefault(airfield["icao"].upper(), airfield)
            self._by_name.setdefault(_match_key(airfield["name"]), airfield)

    def _build_airfield(self, base_dir: Path, country: str) -> dict:
        icao_match = ICAO_RE.search(base_dir.name)
        icao = icao_match.group(1) if icao_match else ""
        name = ICAO_RE.sub("", base_dir.name).strip()

        charts: list[dict] = []
        for path in sorted(base_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in CHART_SUFFIXES:
                continue
            kind, order = _classify(path.name)
            charts.append(
                {
                    "title": _clean_title(path.stem, base_dir.name),
                    "kind": kind,
                    "order": order,
                    "format": path.suffix.lower().lstrip("."),
                    "rel": self._relative(path, self.charts_dir),
                }
            )
        charts.sort(key=lambda c: (c["order"], c["title"]))

        return {
            "name": name,
            "icao": icao,
            "country": country,
            "folder": base_dir.name,
            "charts": charts,
        }

    def _scan_maps(self) -> None:
        if not self.maps_dir or not self.maps_dir.is_dir():
            return
        for path in sorted(self.maps_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MAP_SUFFIXES:
                continue
            stem = re.sub(r"^\d+[_\s-]*", "", path.stem).replace("_", " ").strip()
            self.maps.append(
                {
                    "title": stem or path.stem,
                    "group": path.parent.name if path.parent != self.maps_dir else "",
                    "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                    "rel": self._relative(path, self.maps_dir),
                }
            )

    @staticmethod
    def _relative(path: Path, root: Path | None) -> str:
        try:
            return path.relative_to(root).as_posix()
        except (ValueError, TypeError):
            return path.name

    # -- lookup ----------------------------------------------------------

    def find(self, name: str = "", icao: str = "") -> dict | None:
        if icao:
            hit = self._by_icao.get(icao.strip().upper())
            if hit:
                return hit
        if not name:
            return None

        key = _match_key(name)
        if key in self._by_name:
            return self._by_name[key]
        # 'Osan' should match the 'Osan AB' folder.
        for airfield in self.airfields:
            if _match_key(airfield["name"]).startswith(key) and key:
                return airfield
        return None

    def resolve_briefing(self, briefing: dict) -> list[dict]:
        """Pick out the charts relevant to a parsed briefing."""
        wanted = [
            ("departure", "Departure", briefing.get("airbases", {}).get("departure", ""), ""),
            ("recovery", "Recovery", briefing.get("airbases", {}).get("recovery", ""), ""),
            (
                "alternate",
                "Alternate",
                briefing.get("alternate_airfield", {}).get("name", "")
                or briefing.get("airbases", {}).get("alternate", ""),
                briefing.get("alternate_airfield", {}).get("icao", ""),
            ),
            ("target", "Target", "", briefing.get("overview", {}).get("target_icao", "")),
        ]

        resolved: list[dict] = []
        seen: set[str] = set()
        for role, label, name, icao in wanted:
            if not name and not icao:
                continue
            airfield = self.find(name=name, icao=icao)
            key = f"{role}:{airfield['folder'] if airfield else name or icao}"
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                {
                    "role": role,
                    "label": label,
                    "requested": name or icao,
                    "airfield": airfield,
                    "found": airfield is not None,
                }
            )
        return resolved

    def describe(self) -> dict:
        return {
            "airfield_count": len(self.airfields),
            "chart_count": sum(len(a["charts"]) for a in self.airfields),
            "map_count": len(self.maps),
        }


def _match_key(text: str) -> str:
    """Normalise a base name for matching, dropping type suffixes like 'AB'."""
    text = ICAO_RE.sub("", text or "").lower()
    text = re.sub(r"\b(ab|aaf|intl|international|airbase|air base|airport|afb)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)
