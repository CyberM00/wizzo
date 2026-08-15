"""Build a chart image for a DCS theatre and place the mission's route on it.

DCS ships genuine raster aeronautical charts for each terrain, as DXT-compressed
tiles under ``Mods\\terrains\\<T>\\RasterCharts``. Two things are needed to use
them: where each tile sits in the world, and how mission coordinates map to the
world. Both turn out to be published.

**Where the tiles sit.** ``rasterCharts.sup5`` is the scene index for the chart
tiles. It is a binary, but a regular one: fixed 344-byte records, each carrying
six floats -- ``(minX, minY, minZ, maxX, maxY, maxZ)`` in world metres -- followed
by a length-prefixed tile name. Every record in all three installed terrains
parses, and the boxes agree with a regular grid to the metre, so tiles are placed
from what the file states rather than from anything inferred out of their names.
This matters: Syria's sheets happen to sit on a tidy 262 144 m grid from the
origin, and Caucasus's do not.

**How mission coordinates map to the world.** ``beacons.lua`` lists every beacon
twice over -- ``position`` in world metres and ``positionGeo`` in latitude and
longitude -- which is enough to solve the projection outright. DCS uses
Transverse Mercator on WGS 84 at the UTM scale factor 0.9996, with the central
meridian of the terrain's UTM zone and a per-terrain false origin. Fitted against
the 151/164/101 beacons the three terrains publish, the residual is **0.04 m
median, 0.8 m worst** -- an exact match, not a fit. The false origins come out as
round integers, which is the tell.

Mission positions are metres with **X north and Z east** (the mission file names
the east axis ``y``). North is up on the charts, so the transform to pixels is a
plain scale with the north axis flipped.

Stitching is deliberately not done while the board is being assembled: it decodes
several dozen tiles, so it happens the first time the image is actually requested
and is cached until the game's own files change.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import struct
import threading
import zipfile
from pathlib import Path
from ..paths import state_path

CACHE_DIR = state_path("dcs_map_cache")

JPEG_QUALITY = 86

# WGS 84.
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
# The UTM scale factor. Confirmed rather than assumed: fitting it freely against
# the beacon table lands on 0.9996 and drops the residual from ~100 m to ~0.04 m.
K0 = 0.9996

# Room around the route so the map is not cropped to the flight plan exactly.
DEFAULT_MARGIN_M = 30_000.0
# A stitched chart above this is slow to build and slow to pan; the next coarser
# sheet resolution is used instead.
MAX_PIXELS = 44_000_000
MIN_PIXELS = 700
# Fraction of the wanted box that must fall inside real tiles before a
# resolution is considered usable.
COVERAGE_FLOOR = 0.98

# The terrain's own land/water image, exactly georeferenced, used both as the
# fallback base map and as an independent check that the charts land where the
# index says they do.
OUTLINE_ZOOM = 4
# Less coastline than this in view and the check cannot say anything either way.
ALIGN_MIN_WATER = 0.02
# How much better one position must score than another to have won at all.
ALIGN_MARGIN = 0.02

TILE_NAME_RE = re.compile(rb"(\d+)m_?([A-Za-z]+)(-?\d+)_x(\d+)_z(\d+)\.tif")
# (minX, minY, minZ, maxX, maxY, maxZ) sit this far ahead of the tile name.
SUP5_BBOX_OFFSET = -214

THEATRE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,40}$")

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
BEACON_RE = re.compile(
    r"type\s*=\s*(BEACON_TYPE_\w+)\s*;.*?"
    r"position\s*=\s*\{\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\}\s*;.*?"
    r"positionGeo\s*=\s*\{\s*latitude\s*=\s*(-?[\d.]+)\s*,\s*longitude\s*=\s*(-?[\d.]+)\s*\}",
    re.S,
)
BEACON_NAME_RE = re.compile(r"display_name\s*=\s*_\('([^']*)'\)")
AIRFIELD_TYPES = ("BEACON_TYPE_AIRPORT_HOMER", "BEACON_TYPE_AIRPORT_HOMER_WITH_MARKER")
TOWN_RE = re.compile(
    r"latitude\s*=\s*(-?[\d.]+)\s*,\s*longitude\s*=\s*(-?[\d.]+)\s*,\s*"
    r"display_name\s*=\s*_\(\"([^\"]*)\"\)"
)


class MapError(RuntimeError):
    """The theatre chart could not be built."""


# -- projection -----------------------------------------------------------


def _tm_forward(lat_deg: float, lon_deg: float, lon0_deg: float) -> tuple[float, float]:
    """Latitude and longitude to Transverse Mercator northing and easting."""
    lat = math.radians(lat_deg)
    dlon = math.radians(lon_deg - lon0_deg)
    e2 = WGS84_E2
    n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    t = math.tan(lat)
    c = e2 / (1.0 - e2) * math.cos(lat) ** 2
    a = math.cos(lat) * dlon
    m = WGS84_A * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * lat
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * lat)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * lat)
        - (35 * e2**3 / 3072) * math.sin(6 * lat)
    )
    easting = K0 * n * (
        a
        + (1 - t**2 + c) * a**3 / 6
        + (5 - 18 * t**2 + t**4 + 72 * c - 58 * e2 / (1 - e2)) * a**5 / 120
    )
    northing = K0 * (
        m
        + n * t * (
            a**2 / 2
            + (5 - t**2 + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t**2 + t**4 + 600 * c - 330 * e2 / (1 - e2)) * a**6 / 720
        )
    )
    return northing, easting


def _tm_inverse(northing: float, easting: float, lon0_deg: float) -> tuple[float, float]:
    e2 = WGS84_E2
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    m = northing / K0
    mu = m / (WGS84_A * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    c1 = e2 / (1 - e2) * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = WGS84_A / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    r1 = WGS84_A * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    d = easting / (n1 * K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * e2 / (1 - e2)) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * e2 / (1 - e2) - 3 * c1**2) * d**6 / 720
    )
    lon = (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * e2 / (1 - e2) + 24 * t1**2) * d**5 / 120
    ) / math.cos(phi1)
    return math.degrees(lat), lon0_deg + math.degrees(lon)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return 0.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


class Projection:
    """The terrain's Transverse Mercator frame, solved from its own beacons."""

    def __init__(self, beacons_path: Path):
        self.error = ""
        self.lon0 = 0.0
        self.false_northing = 0.0
        self.false_easting = 0.0
        self.samples = 0
        self.residual_median = 0.0
        self.residual_max = 0.0
        self.airfields: list[dict] = []
        self._solve(Path(beacons_path))

    def _solve(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.error = f"{path.name} could not be read: {exc}"
            return

        control: list[tuple[float, float, float, float]] = []
        for match in BEACON_RE.finditer(text):
            kind, x, _alt, z, lat, lon = match.groups()
            control.append((float(x), float(z), float(lat), float(lon)))
            if kind in AIRFIELD_TYPES:
                name = ""
                # The display name sits just above the type inside the same record.
                head = text[max(0, match.start() - 400) : match.start()]
                names = BEACON_NAME_RE.findall(head)
                if names:
                    name = names[-1].strip()
                if name:
                    self.airfields.append({"name": name.title(), "x": float(x), "z": float(z)})

        if len(control) < 4:
            self.error = f"{path.name} lists too few beacons to solve the projection"
            return

        # DCS uses the UTM central meridian for the zone the terrain sits in, so
        # only the six-degree meridians are candidates. The right one is obvious:
        # it is the only one that makes the offsets constant.
        mean_lon = sum(p[3] for p in control) / len(control)
        best = None
        for step in range(-1, 2):
            zone = int((mean_lon + 180.0) // 6.0) + 1 + step
            lon0 = (zone - 1) * 6.0 - 180.0 + 3.0
            north_offsets = []
            east_offsets = []
            for x, z, lat, lon in control:
                northing, easting = _tm_forward(lat, lon, lon0)
                north_offsets.append(x - northing)
                east_offsets.append(z - easting)
            fn = _median(north_offsets)
            fe = _median(east_offsets)
            errors = [math.hypot(a - fn, b - fe) for a, b in zip(north_offsets, east_offsets)]
            candidate = (_median(errors), max(errors), lon0, fn, fe)
            if best is None or candidate[0] < best[0]:
                best = candidate

        self.residual_median, self.residual_max, self.lon0, fn, fe = best
        self.false_northing = fn
        self.false_easting = fe
        self.samples = len(control)

    @property
    def ok(self) -> bool:
        # A metre of slop over a 500 km theatre is the published figure; anything
        # far above that means the model does not hold for this terrain and no
        # latitude or longitude should be shown for it.
        return not self.error and self.residual_max < 50.0

    def to_lonlat(self, x_north: float, z_east: float) -> tuple[float, float]:
        return _tm_inverse(x_north - self.false_northing, z_east - self.false_easting, self.lon0)

    def to_world(self, lat: float, lon: float) -> tuple[float, float]:
        northing, easting = _tm_forward(lat, lon, self.lon0)
        return northing + self.false_northing, easting + self.false_easting

    def describe(self) -> dict:
        return {
            "central_meridian": round(self.lon0, 4),
            "false_northing": round(self.false_northing, 1),
            "false_easting": round(self.false_easting, 1),
            "scale_factor": K0,
            "beacons": self.samples,
            "residual_median_m": round(self.residual_median, 3),
            "residual_max_m": round(self.residual_max, 3),
            "error": self.error,
        }


# -- the chart tile index -------------------------------------------------


class Tile:
    __slots__ = ("mpp", "minx", "minz", "maxx", "maxz", "archive", "member")

    def __init__(self, mpp, minx, minz, maxx, maxz, archive, member):
        self.mpp = mpp
        self.minx = minx
        self.minz = minz
        self.maxx = maxx
        self.maxz = maxz
        self.archive = archive
        self.member = member


def _read_sup5(path: Path) -> tuple[dict[str, dict], str]:
    """Every chart tile the index names, keyed on its file name."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {}, f"{path.name} could not be read: {exc}"

    out: dict[str, dict] = {}
    skipped = 0
    for match in TILE_NAME_RE.finditer(data):
        start = match.start()
        if start + SUP5_BBOX_OFFSET < 0 or start < 4:
            skipped += 1
            continue
        # The record states the name's length immediately before the text; if it
        # disagrees the fields are not where they are expected to be, so the
        # record is dropped rather than read at a guessed offset.
        (declared,) = struct.unpack_from("<i", data, start - 4)
        if declared != match.end() - start:
            skipped += 1
            continue
        box = struct.unpack_from("<6f", data, start + SUP5_BBOX_OFFSET)
        mpp = int(match.group(1))
        if box[3] <= box[0] or box[5] <= box[2]:
            skipped += 1
            continue
        out[match.group(0).decode("ascii").lower()] = {
            "mpp": mpp,
            "minx": box[0],
            "minz": box[2],
            "maxx": box[3],
            "maxz": box[5],
        }
    if not out:
        return {}, f"{path.name} named no chart tiles"
    return out, ""


def _index_archives(root: Path) -> dict[str, tuple[Path, str]]:
    """Locate every chart tile on disk.

    Syria and the Persian Gulf pack the whole terrain into one ``rasterCharts.zip``;
    Caucasus splits it into one zip per sheet under a folder per resolution. Both
    are plain zips with the same member names, so both are read the same way.
    """
    found: dict[str, tuple[Path, str]] = {}
    archives = sorted(root.rglob("*.zip"))
    for archive in archives:
        try:
            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    base = member.rsplit("/", 1)[-1].lower()
                    if base.endswith(".tif.dds"):
                        found[base[: -len(".dds")]] = (archive, member)
        except (OSError, zipfile.BadZipFile):
            continue
    return found


class TerrainCharts:
    """The chart tiles, projection and bounds one DCS terrain publishes."""

    def __init__(self, install_base: Path | None, theatre: str):
        self.theatre = str(theatre or "").strip()
        self.slug = re.sub(r"[^a-z0-9]", "", self.theatre.lower())
        self.root: Path | None = None
        self.tiles: list[Tile] = []
        self.by_mpp: dict[int, list[Tile]] = {}
        self.bounds: tuple[float, float, float, float] | None = None
        self.nodes_map: Path | None = None
        self.nodes_mpp = 0.0
        self.nodes_size = (0, 0)
        self.projection: Projection | None = None
        self.error = ""
        self.chart_error = ""
        self.sources: list[Path] = []
        self._resolve(install_base)

    # -- discovery -------------------------------------------------------

    def _resolve(self, install_base: Path | None) -> None:
        if not self.theatre or not THEATRE_RE.match(self.theatre):
            self.error = "the mission names no terrain"
            return
        if not install_base:
            self.error = "no DCS installation was found, so its terrains cannot be read"
            return

        terrains = Path(install_base) / "Mods" / "terrains"
        self.root = self._terrain_dir(terrains)
        if self.root is None:
            self.error = f"{self.theatre} is not installed under Mods\\terrains"
            return

        self._read_bounds()
        beacons = self.root / "beacons.lua"
        if beacons.is_file():
            self.projection = Projection(beacons)
        self._read_charts()

    def _terrain_dir(self, terrains: Path) -> Path | None:
        """The folder for this theatre, matched on the id its entry.lua declares."""
        if not terrains.is_dir():
            return None
        wanted = self.slug
        fallback = None
        try:
            children = sorted(p for p in terrains.iterdir() if p.is_dir())
        except OSError:
            return None
        for child in children:
            if re.sub(r"[^a-z0-9]", "", child.name.lower()) == wanted:
                fallback = child
            entry = child / "entry.lua"
            if not entry.is_file():
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"\['id'\]\s*=\s*\"([^\"]+)\"", text)
            if match and re.sub(r"[^a-z0-9]", "", match.group(1).lower()) == wanted:
                return child
        return fallback

    def _read_bounds(self) -> None:
        assert self.root is not None
        path = self.root / "MissionGenerator" / "nodesMap.lua"
        image = self.root / "MissionGenerator" / "nodesMap.png"
        try:
            numbers = [float(v) for v in NUMBER_RE.findall(path.read_text(errors="replace"))]
        except OSError:
            return
        if len(numbers) >= 4:
            self.bounds = (numbers[0], numbers[1], numbers[2], numbers[3])
        if not image.is_file() or not self.bounds:
            return
        # The outline is only usable if its own scale can be established; the
        # bounds and the pixel size together state it exactly.
        try:
            from PIL import Image

            with Image.open(image) as im:
                self.nodes_size = im.size
        except Exception:  # noqa: BLE001 - a bad PNG must not break the board
            return
        if min(self.nodes_size) < 2:
            return
        self.nodes_map = image
        self.nodes_mpp = (self.bounds[3] - self.bounds[1]) / self.nodes_size[0]

    def _read_charts(self) -> None:
        assert self.root is not None
        root = self.root / "RasterCharts"
        if not root.is_dir():
            self.chart_error = f"{self.theatre} ships no RasterCharts folder"
            return
        index = root / "rasterCharts.sup5"
        if not index.is_file():
            self.chart_error = "rasterCharts.sup5 is missing, so tile positions are unknown"
            return

        placed, error = _read_sup5(index)
        if error:
            self.chart_error = error
            return
        members = _index_archives(root)
        if not members:
            self.chart_error = "no chart archives were found beside rasterCharts.sup5"
            return

        self.sources = sorted({index, *(a for a, _ in members.values())})
        for name, box in placed.items():
            located = members.get(name + ".dds") or members.get(name)
            if located is None:
                continue
            self.tiles.append(
                Tile(box["mpp"], box["minx"], box["minz"], box["maxx"], box["maxz"], *located)
            )
        if not self.tiles:
            self.chart_error = "the chart index and the chart archives name different tiles"
            return

        for tile in self.tiles:
            self.by_mpp.setdefault(tile.mpp, []).append(tile)

        # A sanity check on the frame rather than on the tiles: if the index put
        # the charts somewhere other than where the terrain says it is, nothing
        # placed on them would mean anything.
        if self.bounds:
            minx, minz, maxx, maxz = self.bounds
            if not any(
                t.maxx > minx and t.minx < maxx and t.maxz > minz and t.minz < maxz
                for t in self.tiles
            ):
                self.tiles = []
                self.by_mpp = {}
                self.chart_error = (
                    "the chart index places every tile outside the terrain's own bounds"
                )

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def has_charts(self) -> bool:
        return bool(self.tiles) and not self.chart_error

    def source_stamp(self) -> float:
        """Newest modification time across everything the image is built from."""
        newest = 0.0
        for path in [*self.sources, self.nodes_map]:
            if not path:
                continue
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return newest

    # -- reference data --------------------------------------------------

    def airfields(self) -> list[dict]:
        return list(self.projection.airfields) if self.projection else []

    def towns(self) -> list[dict]:
        """Named places, for the fallback map. Needs the projection to place them."""
        if not self.root or not self.projection or not self.projection.ok:
            return []
        path = self.root / "map" / "towns.lua"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        out = []
        for lat, lon, name in TOWN_RE.findall(text):
            if not name:
                continue
            x, z = self.projection.to_world(float(lat), float(lon))
            out.append({"name": name, "x": x, "z": z})
        return out


# -- the image ------------------------------------------------------------


def _snap(value: float, step: float) -> float:
    return math.floor(value / step) * step


class TheatreChart:
    """One cropped, stitched chart for one mission, with its own projection."""

    def __init__(
        self,
        terrain: TerrainCharts,
        box: tuple[float, float, float, float],
        force_outline: bool = False,
    ):
        self.terrain = terrain
        self.error = terrain.error
        self.force_outline = force_outline
        self.mode = "none"
        self.mpp = 0.0
        self.source_mpp = 0.0
        self.origin_x = 0.0   # north edge, world metres
        self.origin_z = 0.0   # west edge, world metres
        self.width = 0
        self.height = 0
        self.tiles: list[Tile] = []
        self.coverage = 0.0
        if not self.error:
            self._plan(box)

    # -- planning --------------------------------------------------------

    def _plan(self, box: tuple[float, float, float, float]) -> None:
        minx, minz, maxx, maxz = box
        if maxx <= minx or maxz <= minz:
            self.error = "the mission's route has no extent to draw"
            return

        if self.terrain.has_charts and not self.force_outline:
            for mpp in sorted(self.terrain.by_mpp):
                tiles = self.terrain.by_mpp[mpp]
                coverage = _coverage(tiles, box)
                if coverage < COVERAGE_FLOOR:
                    continue
                width = int(round((maxz - minz) / mpp))
                height = int(round((maxx - minx) / mpp))
                if width * height > MAX_PIXELS or width < MIN_PIXELS:
                    continue
                self.mode = "chart"
                self.mpp = float(mpp)
                self.source_mpp = float(mpp)
                self.coverage = coverage
                # Snap the crop to the tile grid so every source pixel lands on a
                # whole destination pixel and nothing is resampled.
                self.origin_z = _snap(minz, mpp)
                self.origin_x = -_snap(-maxx, mpp)
                self.width = int(math.ceil((maxz - self.origin_z) / mpp))
                self.height = int(math.ceil((self.origin_x - minx) / mpp))
                self.tiles = [
                    t for t in tiles
                    if t.maxx > minx and t.minx < maxx and t.maxz > minz and t.minz < maxz
                ]
                return

        self._plan_outline(box)

    def _plan_outline(self, box: tuple[float, float, float, float]) -> None:
        """Fall back to the terrain's own land and water image."""
        if not self.terrain.nodes_map or not self.terrain.nodes_mpp:
            self.error = self.terrain.chart_error or (
                f"{self.terrain.theatre} publishes neither charts nor a terrain outline"
            )
            return
        minx, minz, maxx, maxz = box
        source_mpp = self.terrain.nodes_mpp
        self.mode = "outline"
        self.source_mpp = source_mpp
        self.mpp = source_mpp / OUTLINE_ZOOM
        self.origin_z = _snap(minz, source_mpp)
        self.origin_x = -_snap(-maxx, source_mpp)
        self.width = int(math.ceil((maxz - self.origin_z) / self.mpp))
        self.height = int(math.ceil((self.origin_x - minx) / self.mpp))
        self.coverage = _box_overlap(box, self.terrain.bounds)

    @property
    def ok(self) -> bool:
        return not self.error and self.mode != "none" and self.width > 0 and self.height > 0

    # -- geometry --------------------------------------------------------

    def to_pixel(self, x_north: float, z_east: float) -> tuple[float, float]:
        """Mission metres to pixels on the cropped image. X is north, Z is east."""
        return (z_east - self.origin_z) / self.mpp, (self.origin_x - x_north) / self.mpp

    def inside(self, x_north: float, z_east: float) -> bool:
        px, py = self.to_pixel(x_north, z_east)
        return 0 <= px <= self.width and 0 <= py <= self.height

    def visible_box(self) -> dict:
        """The part of the image inside the terrain's own bounds, for framing."""
        if not self.terrain.bounds:
            return {"left": 0, "top": 0, "right": self.width, "bottom": self.height}
        minx, minz, maxx, maxz = self.terrain.bounds
        left, top = self.to_pixel(maxx, minz)
        right, bottom = self.to_pixel(minx, maxz)
        return {
            "left": max(0, round(min(left, right))),
            "top": max(0, round(min(top, bottom))),
            "right": min(self.width, round(max(left, right))),
            "bottom": min(self.height, round(max(top, bottom))),
        }

    def corners_lonlat(self) -> dict:
        """Latitude and longitude of the image's four corners."""
        projection = self.terrain.projection
        if not projection or not projection.ok:
            return {}
        out = {}
        for key, (px, py) in (
            ("nw", (0, 0)), ("ne", (self.width, 0)),
            ("sw", (0, self.height)), ("se", (self.width, self.height)),
        ):
            x = self.origin_x - py * self.mpp
            z = self.origin_z + px * self.mpp
            lat, lon = projection.to_lonlat(x, z)
            out[key] = [round(lat, 5), round(lon, 5)]
        return out

    def graticule(self) -> list[dict]:
        """Latitude and longitude lines across the image, as pixel polylines.

        Sampled rather than drawn corner to corner: a meridian is a straight line
        in Transverse Mercator only on the central meridian, and a parallel never
        is, so a two-point line would be visibly wrong at the edges.
        """
        projection = self.terrain.projection
        if not projection or not projection.ok or not self.ok:
            return []
        corners = self.corners_lonlat()
        if not corners:
            return []
        lats = [corners[k][0] for k in corners]
        lons = [corners[k][1] for k in corners]
        span = max(max(lats) - min(lats), max(lons) - min(lons))
        step = next((s for s in (0.1, 0.25, 0.5, 1.0, 2.0, 5.0) if span / s <= 9), 10.0)

        lines: list[dict] = []
        for is_parallel in (True, False):
            low, high = (min(lats), max(lats)) if is_parallel else (min(lons), max(lons))
            other_low, other_high = (min(lons), max(lons)) if is_parallel else (min(lats), max(lats))
            value = math.ceil(low / step) * step
            while value <= high + 1e-9:
                points = []
                for i in range(21):
                    other = other_low + (other_high - other_low) * i / 20.0
                    lat, lon = (value, other) if is_parallel else (other, value)
                    x, z = projection.to_world(lat, lon)
                    px, py = self.to_pixel(x, z)
                    points.append([round(px, 1), round(py, 1)])
                # Anchor the label to the first point actually on the image: a
                # line can start just outside the crop, and a label there is
                # clipped away.
                anchor = next(
                    (
                        p for p in points
                        if 0 <= p[0] <= self.width and 0 <= p[1] <= self.height
                    ),
                    points[0] if points else [0, 0],
                )
                lines.append(
                    {
                        "kind": "parallel" if is_parallel else "meridian",
                        "label": _dms_label(value, is_parallel),
                        "points": points,
                        "anchor": anchor,
                    }
                )
                value += step
        return lines

    def towns_in_view(self, limit: int = 70, skip: set[str] | None = None) -> list[dict]:
        """Named places inside the image, thinned so the labels stay readable."""
        if not self.ok:
            return []
        cells: dict[tuple[int, int], dict] = {}
        taken = {name.casefold() for name in (skip or ())}
        grid = 9
        for town in self.terrain.towns():
            if town["name"].casefold() in taken:
                continue
            px, py = self.to_pixel(town["x"], town["z"])
            if not (0 <= px <= self.width and 0 <= py <= self.height):
                continue
            cell = (
                int(px * grid / max(1, self.width)),
                int(py * grid / max(1, self.height)),
            )
            if cell in cells:
                continue
            cells[cell] = {"x": round(px, 1), "y": round(py, 1), "name": town["name"]}
            if len(cells) >= limit:
                break
        return list(cells.values())

    def scale_bar(self) -> dict:
        """A round distance and how many pixels it spans, for a drawn scale bar."""
        if not self.ok:
            return {}
        wanted_px = max(80.0, self.width / 6.0)
        metres = wanted_px * self.mpp
        nice = next(
            (v for v in (1e3, 2e3, 5e3, 1e4, 2e4, 2.5e4, 5e4, 1e5, 2e5, 5e5) if v >= metres),
            1e6,
        )
        return {
            "metres": int(nice),
            "pixels": round(nice / self.mpp, 1),
            "label": f"{nice / 1000:g} km",
            "nm_label": f"{nice / 1852.0:.0f} nm",
        }

    # -- identity and cache ----------------------------------------------

    @property
    def map_id(self) -> str:
        return (
            f"{self.terrain.slug}-{self.mode}-{round(self.mpp * 100)}-"
            f"{round(self.origin_x)}-{round(self.origin_z)}-{self.width}x{self.height}"
        )

    @property
    def cache_path(self) -> Path:
        return CACHE_DIR / f"{self.map_id}.jpg"

    def cached(self) -> Path | None:
        path = self.cache_path
        if not path.is_file():
            return None
        try:
            if path.stat().st_mtime >= self.terrain.source_stamp():
                return path
        except OSError:
            return None
        return None

    # -- raster ----------------------------------------------------------

    def build(self) -> Path:
        existing = self.cached()
        if existing:
            return existing
        if not self.ok:
            raise MapError(self.error or "the theatre chart could not be planned")

        try:
            from PIL import Image
        except ImportError as exc:
            raise MapError(
                "Pillow is required to build the theatre chart. Install it with "
                "'pip install Pillow'."
            ) from exc

        canvas = (
            self._stitch_charts(Image) if self.mode == "chart" else self._stitch_outline(Image)
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Two browsers can ask for the same uncached image at once, so each write
        # goes to its own scratch file; the rename that publishes it is atomic.
        temp = self.cache_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            canvas.save(temp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            os.replace(temp, self.cache_path)
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise MapError(f"could not write the map cache: {exc}") from exc
        finally:
            canvas.close()
        return self.cache_path

    def _stitch_charts(self, Image):
        canvas = Image.new("RGB", (self.width, self.height), (28, 28, 28))
        placed = 0
        opened: dict[Path, zipfile.ZipFile] = {}
        try:
            for tile in sorted(self.tiles, key=lambda t: (-t.maxx, t.minz)):
                try:
                    archive = opened.get(tile.archive)
                    if archive is None:
                        archive = opened[tile.archive] = zipfile.ZipFile(tile.archive)
                    with Image.open(io.BytesIO(archive.read(tile.member))) as im:
                        im.load()
                        patch = im.convert("RGB") if im.mode != "RGB" else im.copy()
                except (OSError, KeyError, zipfile.BadZipFile, ValueError):
                    continue
                left = int(round((tile.minz - self.origin_z) / self.mpp))
                top = int(round((self.origin_x - tile.maxx) / self.mpp))
                canvas.paste(patch, (left, top))
                patch.close()
                placed += 1
        finally:
            for archive in opened.values():
                archive.close()
        if not placed:
            canvas.close()
            raise MapError("none of the chart tiles for this area could be decoded")
        return canvas

    def _outline_window(self) -> tuple[int, int, int, int]:
        """The rectangle of the terrain outline this map covers, in its pixels."""
        assert self.terrain.bounds
        bminx, bminz, bmaxx, bmaxz = self.terrain.bounds
        scale = self.terrain.nodes_mpp
        left = int(round((self.origin_z - bminz) / scale))
        top = int(round((bmaxx - self.origin_x) / scale))
        wide = max(1, int(round(self.width * self.mpp / scale)))
        tall = max(1, int(round(self.height * self.mpp / scale)))
        return left, top, wide, tall

    def _stitch_outline(self, Image):
        assert self.terrain.nodes_map
        with Image.open(self.terrain.nodes_map) as im:
            im.load()
            source = im.convert("RGB")
        left, top, wide, tall = self._outline_window()
        crop = source.crop((left, top, left + wide, top + tall))
        source.close()
        out = crop.resize((self.width, self.height), Image.BICUBIC)
        crop.close()
        return out

    # -- an independent check on where the charts landed -----------------

    def alignment(self) -> dict:
        """Check the built chart against the terrain's own land and water image.

        Tile positions come from the game's own index rather than from anything
        inferred, but the index is a binary read at a fixed offset, so it is worth
        proving. Coastlines are what the two images can be made to agree on.

        What is *not* done is score the overlap and compare it to a fixed
        threshold. The three installed terrains draw their seas quite differently
        -- Syria's is a solid blue, Caucasus's is nearly white -- so the same
        colour test finds 52% of one and 12% of another, and a fixed threshold
        rejected a Caucasus chart that visibly lands on its printed airfield
        symbols. Instead the same overlap is measured again with the chart
        deliberately displaced, and what is reported is whether the stated
        position beats every displaced one. That question survives a mediocre
        colour test, because both sides of the comparison share it.
        """
        blank = {"checked": False, "verdict": "unchecked", "iou": None, "reason": ""}
        if self.mode != "chart" or not self.terrain.nodes_map:
            return dict(blank, reason="only a stitched chart can be checked")
        cached = self.cached()
        if cached is None:
            return dict(blank, reason="the chart has not been built yet")
        try:
            from PIL import Image, ImageChops
        except ImportError:
            return dict(blank, reason="Pillow is not installed")

        left, top, wide, tall = self._outline_window()
        # The charts extend well beyond the terrain's mission area, so only the
        # part of the image the outline actually covers can be scored.
        source_w, source_h = self.terrain.nodes_size
        right = min(left + wide, source_w)
        bottom = min(top + tall, source_h)
        left = max(0, left)
        top = max(0, top)
        wide, tall = right - left, bottom - top
        if wide < 8 or tall < 8:
            return dict(blank, reason="too little of this area is covered by the terrain outline")

        try:
            with Image.open(self.terrain.nodes_map) as im:
                im.load()
                nodes = im.convert("RGB")
            with Image.open(cached) as im:
                im.load()
                chart = im.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            return dict(blank, reason=f"the images could not be read: {exc}")

        try:
            bminx, bminz, bmaxx, bmaxz = self.terrain.bounds
            scale = self.terrain.nodes_mpp
            cx0, cy0 = self.to_pixel(bmaxx - top * scale, bminz + left * scale)
            cx1, cy1 = self.to_pixel(
                bmaxx - (top + tall) * scale, bminz + (left + wide) * scale
            )
            window = chart.crop(
                (int(round(cx0)), int(round(cy0)), int(round(cx1)), int(round(cy1)))
            )
            size = (wide, tall)
            reference = _water_mask(
                nodes.crop((left, top, left + wide, top + tall)), outline=True
            )
            measured = _water_mask(window.resize(size, Image.BOX), outline=False)
            window.close()
            total = wide * tall
            water = measured.histogram()[255]
            if min(water, reference.histogram()[255]) < total * ALIGN_MIN_WATER:
                return dict(
                    blank, reason="there is no coastline in this area to check against"
                )

            def score(dx: int, dy: int) -> float | None:
                x, y = left + dx, top + dy
                if x < 0 or y < 0 or x + wide > source_w or y + tall > source_h:
                    return None
                shifted = _water_mask(
                    nodes.crop((x, y, x + wide, y + tall)), outline=True
                )
                inter = ImageChops.multiply(shifted, measured).histogram()[255]
                union = water + shifted.histogram()[255] - inter
                return (inter / union) if union else 0.0

            here = score(0, 0) or 0.0
            # Big enough a displacement that a large body of water cannot overlap
            # itself well; a few pixels either way barely changes an open sea and
            # would make the comparison meaningless.
            step = max(6, min(wide, tall) // 4)
            elsewhere = [
                value
                for dx, dy in (
                    (step, 0), (-step, 0), (0, step), (0, -step),
                    (step, step), (-step, -step), (step, -step), (-step, step),
                )
                for value in (score(dx, dy),)
                if value is not None
            ]
            if len(elsewhere) < 3:
                return dict(
                    blank,
                    iou=round(here, 3),
                    reason="this area sits at the edge of the terrain outline, so the "
                    "chart could not be compared against displaced positions",
                )
            rival = max(elsewhere)
            # The test is which position wins, not what the winning value is: a
            # chart placed wrongly would score highest somewhere other than where
            # it claims to be, whatever the colour test makes of its sea. The
            # index is the evidence and this is the corroboration, so a near tie
            # is reported as inconclusive rather than resolved either way.
            if here > rival + ALIGN_MARGIN and here > 0.02:
                verdict = "confirmed"
            elif rival > here + ALIGN_MARGIN:
                verdict = "contradicted"
            else:
                verdict = "inconclusive"
            return {
                "checked": True,
                "verdict": verdict,
                "iou": round(here, 3),
                "rival_iou": round(rival, 3),
                "shift_m": round(step * self.terrain.nodes_mpp),
                "candidates": len(elsewhere),
                "reason": "",
            }
        finally:
            nodes.close()
            chart.close()

    def describe(self) -> dict:
        return {
            "theatre": self.terrain.theatre,
            "mode": self.mode,
            "metres_per_pixel": round(self.mpp, 2),
            "source_metres_per_pixel": round(self.source_mpp, 2),
            "origin_x": round(self.origin_x),
            "origin_z": round(self.origin_z),
            "pixel_size": [self.width, self.height],
            "tiles": len(self.tiles),
            "coverage": round(self.coverage, 3),
            "error": self.error,
        }


def _dms_label(value: float, is_parallel: bool) -> str:
    hemisphere = ("N" if value >= 0 else "S") if is_parallel else ("E" if value >= 0 else "W")
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = round((magnitude - degrees) * 60)
    if minutes == 60:
        degrees, minutes = degrees + 1, 0
    return f"{degrees}{chr(176)}{minutes:02d}'{hemisphere}" if minutes else f"{degrees}{chr(176)}{hemisphere}"


def _water_mask(image, outline: bool):
    """A binary water mask.

    The two sources render water very differently -- the terrain outline uses one
    flat blue or teal fill, the charts a pale blue -- so each gets its own test.
    Both amount to "blue dominates", which is what distinguishes water from land
    on either image.
    """
    from PIL import ImageChops

    r, g, b = image.split()
    if outline:
        blue_over_red = ImageChops.subtract(b, r).point(lambda v: 255 if v > 4 else 0, "L")
        blue_over_green = ImageChops.subtract(b, g).point(lambda v: 255 if v > 0 else 0, "L")
        return ImageChops.multiply(blue_over_red, blue_over_green)
    bright = b.point(lambda v: 255 if v > 150 else 0, "L")
    blue_over_red = ImageChops.subtract(b, r).point(lambda v: 255 if v > 12 else 0, "L")
    blue_over_green = ImageChops.subtract(b, g).point(lambda v: 255 if v > 5 else 0, "L")
    return ImageChops.multiply(ImageChops.multiply(bright, blue_over_red), blue_over_green)


def _box_overlap(box, other) -> float:
    """Fraction of ``box`` that falls inside ``other``."""
    minx, minz, maxx, maxz = box
    ominx, ominz, omaxx, omaxz = other
    wide = max(0.0, min(maxz, omaxz) - max(minz, ominz))
    tall = max(0.0, min(maxx, omaxx) - max(minx, ominx))
    area = (maxz - minz) * (maxx - minx)
    return (wide * tall / area) if area else 0.0


def _coverage(tiles: list[Tile], box, samples: int = 24) -> float:
    """Fraction of ``box`` that real tiles actually cover, by sampling a grid."""
    minx, minz, maxx, maxz = box
    inside = 0
    boxes = [
        t for t in tiles
        if t.maxx > minx and t.minx < maxx and t.maxz > minz and t.minz < maxz
    ]
    if not boxes:
        return 0.0
    for i in range(samples):
        x = minx + (maxx - minx) * (i + 0.5) / samples
        for j in range(samples):
            z = minz + (maxz - minz) * (j + 0.5) / samples
            if any(t.minx <= x <= t.maxx and t.minz <= z <= t.maxz for t in boxes):
                inside += 1
    return inside / (samples * samples)


# -- caching the alignment verdict ---------------------------------------


_VERDICT_LOCK = threading.Lock()


def _verdict_path() -> Path:
    return CACHE_DIR / "alignment.json"


def load_verdicts() -> dict:
    try:
        return json.loads(_verdict_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_verdict(map_id: str, stamp: float, verdict: dict) -> None:
    with _VERDICT_LOCK:
        verdicts = load_verdicts()
        verdicts[map_id] = dict(verdict, stamp=stamp)
        # Only the current mission's areas matter; the file is a hint, not a record.
        if len(verdicts) > 40:
            verdicts = dict(
                sorted(verdicts.items(), key=lambda kv: kv[1].get("stamp", 0))[-20:]
            )
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _verdict_path().write_text(json.dumps(verdicts, indent=1), encoding="utf-8")
        except OSError:
            pass
