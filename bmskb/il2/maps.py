"""Build a theatre map image for IL-2 and place mission coordinates on it.

IL-2 ships the map its own planner draws, as DXT-compressed tiles inside the
``Maps*.gtp`` archives, together with the metadata needed to georeference them:

* ``/swf/il2/maps/<guimap>/info.txt``      -- names the tile set and the corner latitudes
* ``/swf/il2/mapstiles/<tiles>/info.txt``  -- grid shape, tile size, and the extent in metres
* ``/swf/il2/mapstiles/<tiles>/tiles/<lod>/<row>_<col>.dds``

The mission's ``GuiMap`` value keys straight into the first of those, so no
guesswork is needed to find the right map for the mission being flown.

The extent is stated outright (``imageSizeInMeters``), which makes the transform
from mission metres to pixels a plain linear scale. Mission positions are metres
on a flat grid with **X north and Z east** -- established by recomputing bearings
against orientations the game itself stores.

Stitching is done once per theatre and cached as a JPEG. The tiles decode to a
few hundred megabytes at full size, so this is deliberately not something the
board does while serving a page.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .gtp import GtpError, open_archive

CACHE_DIR = Path(__file__).parent.parent.parent / "il2_map_cache"

# Level "02" is the middle of the three the game ships: twice the base grid, so
# 45 m per pixel on the theatres measured, which resolves rivers, coastlines and
# airfields without producing a file too large to pan smoothly.
DEFAULT_LOD = "02"
JPEG_QUALITY = 86

MAP_INFO_RE = "^/swf/il2/maps/{}/info\\.txt$"
TILES_INFO_RE = "^/swf/il2/mapstiles/{}/info\\.txt$"
TILES_RE = "^/swf/il2/mapstiles/{}/tiles/"

FIELD_RE = re.compile(r"^&(\w+)\s*=\s*([^\r\n/]*)", re.M)


class MapError(RuntimeError):
    """The theatre map could not be built."""


def _fields(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip().strip('"') for m in FIELD_RE.finditer(text)}


def _numbers(value: str) -> list[float]:
    return [float(p) for p in re.findall(r"-?\d+(?:\.\d+)?", value or "")]


class TheatreMap:
    """Metadata and a cached raster for one IL-2 theatre."""

    def __init__(self, data_dir: Path, gui_map: str, lod: str = DEFAULT_LOD):
        self.data_dir = Path(data_dir)
        self.gui_map = str(gui_map or "").strip()
        self.lod = lod
        self.tiles_id = ""
        self.grid = (0, 0)
        self.tile_px = 0
        self.image_m = (0.0, 0.0)
        self.world_m = (0.0, 0.0)
        self.corner_lat = {}
        self.archive: Path | None = None
        self.error = ""
        self._resolve()

    # -- metadata --------------------------------------------------------

    def _resolve(self) -> None:
        if not self.gui_map:
            self.error = "the mission does not name a map"
            return
        swf = self.data_dir / "Swf.gtp"
        if not swf.is_file():
            self.error = f"{swf.name} not found"
            return

        try:
            with open_archive(swf, re.compile(MAP_INFO_RE.format(re.escape(self.gui_map)))) as ar:
                if not ar.entries:
                    self.error = f"no map metadata for {self.gui_map!r}"
                    return
                fields = _fields(ar.read(next(iter(ar.entries))).decode("utf-8-sig", "replace"))
        except GtpError as exc:
            self.error = str(exc)
            return

        self.tiles_id = fields.get("tilesId", "")
        self.corner_lat = {
            key: float(fields[key])
            for key in ("mapNELat", "mapNWLat", "mapSWLat", "mapSELat")
            if key in fields
        }
        if not self.tiles_id:
            self.error = f"{self.gui_map!r} names no tile set"
            return

        # The tile set and its metadata live in whichever Maps archive shipped it.
        pattern = re.compile(TILES_INFO_RE.format(re.escape(self.tiles_id)))
        for candidate in sorted(self.data_dir.glob("Maps*.gtp")):
            try:
                with open_archive(candidate, pattern) as ar:
                    if not ar.entries:
                        continue
                    info = _fields(
                        ar.read(next(iter(ar.entries))).decode("utf-8-sig", "replace")
                    )
            except GtpError:
                continue
            self.archive = candidate
            width = int(_numbers(info.get("widthTilesAmount", "0"))[0] or 0)
            height = int(_numbers(info.get("heightTilesAmount", "0"))[0] or 0)
            self.grid = (width, height)
            self.tile_px = int(_numbers(info.get("tileSizeInPixels", "0"))[0] or 0)
            image = _numbers(info.get("imageSizeInMeters", ""))
            world = _numbers(info.get("worldSizeInMeters", ""))
            self.image_m = (image[0], image[1]) if len(image) >= 2 else (0.0, 0.0)
            self.world_m = (world[0], world[1]) if len(world) >= 2 else self.image_m
            break

        if self.archive is None:
            self.error = f"no tiles found for {self.tiles_id!r}"
        elif not all(self.grid) or not self.tile_px or not all(self.image_m):
            self.error = f"{self.tiles_id!r} tile metadata is incomplete"

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def scale(self) -> int:
        """Tile multiplier for the chosen level: "02" is twice the base grid."""
        try:
            return max(1, int(self.lod))
        except ValueError:
            return 1

    @property
    def pixel_size(self) -> tuple[int, int]:
        return (
            self.grid[0] * self.scale * self.tile_px,
            self.grid[1] * self.scale * self.tile_px,
        )

    @property
    def metres_per_pixel(self) -> float:
        width_px = self.pixel_size[0]
        return self.image_m[0] / width_px if width_px else 0.0

    # -- projection ------------------------------------------------------

    def to_pixel(self, x_north: float, z_east: float) -> tuple[float, float]:
        """Mission metres to pixels on the stitched image.

        X is north and Z is east. East maps straight onto the image width, but
        north needs care: the image is taller than the world it depicts, and the
        surplus is all at the bottom. So the world's north edge sits on the image's
        top edge, while the scale is the image's own.

        This was settled against the map's own printed grid rather than by
        assuming. IL-2 labels each 10 km square with a row and column that work
        out to ``col = (Z-35000)/10000`` and ``row = (323400-X)/10000``, matching
        ``visibleAreaInMeters`` read as a rectangle in mission coordinates. Both
        the obvious readings -- scaling north by the image height or by the world
        height -- put airfields 3-6 km off their printed symbols; this does not.
        """
        width_px, height_px = self.pixel_size
        px = (z_east / self.image_m[0]) * width_px
        py = ((self.world_m[1] - x_north) / self.image_m[1]) * height_px
        return px, py

    def visible_box(self) -> dict:
        """The pixel rectangle the playable area occupies, for framing the view."""
        # visibleAreaInMeters is (min east, min north, width east, height north).
        left, top = self.to_pixel(35000.0, 35000.0)
        right, bottom = self.to_pixel(35000.0 + 288400.0, 35000.0 + 416200.0)
        return {
            "left": round(min(left, right)),
            "top": round(min(top, bottom)),
            "right": round(max(left, right)),
            "bottom": round(max(top, bottom)),
        }

    def describe(self) -> dict:
        return {
            "gui_map": self.gui_map,
            "tiles_id": self.tiles_id,
            "grid": list(self.grid),
            "lod": self.lod,
            "tile_px": self.tile_px,
            "pixel_size": list(self.pixel_size),
            "image_m": list(self.image_m),
            "world_m": list(self.world_m),
            "metres_per_pixel": round(self.metres_per_pixel, 2),
            "corner_lat": self.corner_lat,
            "archive": self.archive.name if self.archive else "",
            "error": self.error,
        }

    # -- raster ----------------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return CACHE_DIR / f"{self.gui_map}.lod{self.lod}.jpg"

    def cached(self) -> Path | None:
        """The cached raster, if it is present and newer than its archive."""
        path = self.cache_path
        if not path.is_file() or self.archive is None:
            return None
        try:
            if path.stat().st_mtime >= self.archive.stat().st_mtime:
                return path
        except OSError:
            return None
        return None

    def build(self) -> Path:
        """Stitch the tiles into one image and cache it. Returns the file path."""
        existing = self.cached()
        if existing:
            return existing
        if not self.ok:
            raise MapError(self.error)

        try:
            from PIL import Image
        except ImportError as exc:
            raise MapError(
                "Pillow is required to build the theatre map. Install it with "
                "'pip install Pillow'."
            ) from exc

        width_px, height_px = self.pixel_size
        cols = self.grid[0] * self.scale
        rows = self.grid[1] * self.scale

        pattern = re.compile(TILES_RE.format(re.escape(self.tiles_id)))
        canvas = Image.new("RGB", (width_px, height_px))
        placed = 0

        try:
            with open_archive(self.archive, pattern) as archive:
                wanted = {}
                for name in archive.entries:
                    parts = name.split("/")
                    # .../tiles/<lod>/<row>_<col>.dds
                    if len(parts) < 8 or parts[6] != self.lod:
                        continue
                    cell = parts[7].rsplit(".", 1)[0]
                    bits = cell.split("_")
                    if len(bits) != 2 or not all(b.isdigit() for b in bits):
                        continue
                    wanted[(int(bits[0]), int(bits[1]))] = name

                if not wanted:
                    raise MapError(
                        f"{self.tiles_id!r} has no tiles at level {self.lod}"
                    )

                for (row, col), name in sorted(wanted.items()):
                    if row > rows or col > cols:
                        continue
                    import io

                    try:
                        tile = Image.open(io.BytesIO(archive.read(name)))
                        tile.load()
                    except Exception:
                        continue
                    if tile.mode != "RGB":
                        tile = tile.convert("RGB")
                    canvas.paste(tile, ((col - 1) * self.tile_px, (row - 1) * self.tile_px))
                    placed += 1
                    tile.close()
        except GtpError as exc:
            raise MapError(str(exc)) from exc

        if not placed:
            raise MapError(f"no tiles could be decoded for {self.tiles_id!r}")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(".tmp")
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

        self.tiles_placed = placed
        return self.cache_path
