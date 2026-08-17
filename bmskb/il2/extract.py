"""Build IL-2's loadout name tables out of the game's own packed data.

Two members of two archives carry everything needed:

* ``Scripts.gtp`` → ``/luascripts/worldobjects/planes/<code>.txt`` -- one script per
  aircraft, holding ``[Ammunition=N]`` blocks (N *is* the mission file's
  ``PayloadId``) and ``[WeaponMode=N]`` blocks.
* ``Swf.gtp`` → ``/swf/il2/ammunition/ammunition.locale=<lang>.txt`` -- the weapon
  code to display-name table, one member per language, addressed by path so no
  offset hunting or copy-counting is needed.

This is extracted at runtime rather than hand-curated, which is the opposite of
the choice made for DCS. The reason is simply that here the data is readable: DCS
builds its name mapping by executing Lua, so it had to be curated; IL-2 ships the
table, so curating would mean hand-copying ~3,400 strings and getting a worse
answer than the game's own.

The result is cached and keyed on both archives' size and mtime, so a game patch
invalidates it automatically. The build is cheap enough (well under a second) that
a cache miss is a non-event -- the cache is an optimisation, never a dependency.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .gtp import GtpError, open_archive
from ..paths import state_path

# 2: added the per-aircraft technical notes.
CACHE_SCHEMA = 2
CACHE_PATH = state_path("il2_name_cache.json")

PLANE_MEMBER_RE = re.compile(r"/luascripts/worldobjects/planes/[a-z0-9_]+\.txt$")
LOCALE_MEMBER_RE = re.compile(r"/swf/il2/ammunition/ammunition\.locale=\w+\.txt$")
# The aircraft notes IL-2 shows in its own UI: performance limits, engine modes,
# and the recommended control settings. One file per aircraft, per language.
INFO_MEMBER_RE = re.compile(r"^/swf/il2/worldobjects/planes/[^/]+/info\.locale=\w+\.txt$")
INFO_FIELD_RE = re.compile(r"^&(\w+)=", re.M)

OBJECT_NAME_RE = re.compile(r'object_name\s*=\s*"([^"]*)"')
# Ammunition blocks never nest (verified across 3,387 blocks), so a non-greedy
# body match is safe here. WeaponMode blocks *do* nest, so they are walked with a
# depth counter instead.
AMMO_RE = re.compile(r"\[Ammunition=(\d+)\](.*?)\[end\]", re.S | re.I)
NAME_FIELD_RE = re.compile(r'\bname\s*=\s*"([^"]*)"')
BOMB_LINE_RE = re.compile(r'\bBomb\d+\s*=\s*[^,]*,\s*"([^"]+)"')
WEAPON_MODE_OPEN_RE = re.compile(r"\[WeaponMode=(\d+)\]", re.I)
WMNAME_RE = re.compile(r'WMname\s*=\s*"([^"]*)"')
BLOCK_END_RE = re.compile(r"\[end\]", re.I)

STATION_PREFIX_RE = re.compile(r"^(\d+(?:,\d+)*)-")
LEADING_INT_RE = re.compile(r"^(\d+)")

# Section header to a coarse kind. Guns carry round counts; everything else is
# counted in units.
SECTION_KINDS = {"guns": "gun", "bombs": "bomb", "rockets": "rocket", "other": "other"}

# "Empty" is a sentinel for an unarmed station, not a weapon code that failed to
# resolve. Treating it as unknown would put ~150 spurious entries in the warnings.
EMPTY_TOKENS = {"empty", "none", "-"}

BALLISTIC_KINDS = (("bomb_", "bomb"), ("rkt_", "rocket"), ("torp", "torpedo"))
NATIONS = ("rus", "ger", "gbr", "usa", "ita", "fra", "jap")


class ExtractError(RuntimeError):
    """The game's name tables could not be read."""


# ---------------------------------------------------------------- name table


def parse_locale_table(text: str) -> dict[str, dict]:
    """Parse ``id, name, image|`` rows grouped under ``&section=`` headers."""
    names: dict[str, dict] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("&"):
            section = line.strip("&= \t").lower()
            continue
        if not line.endswith("|"):
            continue
        parts = [p.strip() for p in line.rstrip("|").split(",")]
        if len(parts) < 2:
            continue
        code, display = parts[0], parts[1]
        if not code or code.lower() == "id":
            continue
        names[code] = {
            "name": display or code,
            "section": section,
            "kind": SECTION_KINDS.get(section, "other"),
        }
    return names


# ------------------------------------------------------------ plane scripts


def _weapon_modes(text: str) -> dict[str, str]:
    """Map each ``[WeaponMode=N]`` index to its ``WMname``.

    These blocks nest -- ``[Gun=N]``, ``[BombHolder]`` and ``[attach]`` sit inside
    them -- so a non-greedy body match would truncate at the first nested
    ``[end]``. No depth tracking is needed though, because ``WMname`` always
    appears before any nested block: taking the first one after each header, and
    stopping at the next header, is both simpler and correct.
    """
    modes: dict[str, str] = {}
    headers = list(WEAPON_MODE_OPEN_RE.finditer(text))
    for position, match in enumerate(headers):
        stop = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        found = WMNAME_RE.search(text, match.end(), stop)
        if found:
            modes[match.group(1)] = found.group(1)
    return modes


def _path_tokens(basename: str) -> set[str]:
    """Lowercase underscore/dot-separated tokens of a ballistics filename."""
    return {t for t in re.split(r"[._\-]+", basename.lower()) if t}


def _ballistics(body: str) -> list[tuple[str, str, str]]:
    """``BombN=`` lines as (basename, kind, nation). Rockets use ``Bomb`` too."""
    out = []
    for path in BOMB_LINE_RE.findall(body):
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        low = base.lower()
        kind = next((k for prefix, k in BALLISTIC_KINDS if low.startswith(prefix)), "")
        nation = next((n.upper() for n in NATIONS if f"_{n}_" in low), "")
        out.append((base, kind, nation))
    return out


def tokenise_payload(raw: str, names: dict[str, dict], ballistics: list[tuple[str, str, str]]) -> list[dict]:
    """Split an ``[Ammunition] name=`` string into structured stores.

    Weapon codes contain hyphens (``MG15120pod-APHE``), so the string cannot be
    split on ``-``. Instead each item is matched longest-first against the game's
    own code vocabulary, which makes the name table the parser.
    """
    vocabulary = sorted(names, key=len, reverse=True)
    items: list[dict] = []

    for chunk in raw.split(" + "):
        item = chunk.strip()
        if not item:
            continue

        stations: list[int] = []
        prefix = STATION_PREFIX_RE.match(item)
        if prefix:
            stations = [int(n) for n in prefix.group(1).split(",")]
            item = item[prefix.end():]

        if item.lower() in EMPTY_TOKENS:
            items.append({"code": "", "name": "Empty station", "kind": "empty",
                          "known": True, "stations": stations, "count": None,
                          "rounds": None, "modifiers": []})
            continue

        code = next(
            (v for v in vocabulary if item == v or item.startswith(v + "-")),
            "",
        )
        if not code:
            # Still split off a trailing quantity so the unresolved code is
            # reported cleanly ("FTankGer300", not "FTankGer300-1") and the count
            # survives even when the name does not.
            head, _, trail = item.rpartition("-")
            if head and trail.isdigit():
                items.append({"code": head, "name": head, "kind": "", "known": False,
                              "stations": stations, "count": int(trail),
                              "rounds": None, "modifiers": []})
            else:
                items.append({"code": item, "name": item, "kind": "", "known": False,
                              "stations": stations, "count": None, "rounds": None,
                              "modifiers": []})
            continue

        tail = item[len(code):].lstrip("-")
        trailing = LEADING_INT_RE.match(tail)
        number = int(trailing.group(1)) if trailing else None
        modifiers = [t for t in tail[trailing.end():].split("-") if t] if trailing else \
                    [t for t in tail.split("-") if t]

        entry = names[code]
        kind = entry["kind"]

        # Quantity is taken from the count of BombN lines naming this store,
        # which is independent of the label -- the trailing number in the name is
        # only a cross-check.
        #
        # Match on whole underscore-separated tokens, not substrings: "SC50" is a
        # substring of "BOMB_GER_SC500_Late.txt", which silently inflated counts
        # on every aircraft carrying both sizes.
        matched = [b for b in ballistics if code.lower() in _path_tokens(b[0])]
        count = len(matched) or None
        record = {
            "code": code,
            "name": entry["name"],
            "kind": matched[0][1] if matched and matched[0][1] else kind,
            "nation": matched[0][2] if matched else "",
            "known": True,
            "stations": stations,
            "modifiers": modifiers,
            "count": None,
            "rounds": None,
        }
        if kind == "gun":
            # Whether this is per-gun or a total for the group is not confirmed,
            # so it is carried but deliberately not displayed.
            record["rounds"] = number
        else:
            # Prefer the number in the game's own label over the count of BombN
            # lines. They agree for ordinary stores, but for cassette munitions
            # the label counts submunitions while the lines count the containers
            # holding them -- il2m43 loads 192 PTAB-2.5-1.5 in 32 cassettes. The
            # label is what the in-game arming screen shows, so it is the figure
            # a pilot recognises.
            record["count"] = number or count
            if count and number and count != number:
                record["carriers"] = count
        items.append(record)

    return items


def parse_info_file(text: str) -> dict:
    """Parse a ``&key=value`` aircraft note file.

    Values run over multiple lines until the next ``&key=``. The description is
    wrapped in single quotes and percent-encodes its literal percent signs, so a
    fuel setting reads ``10%25`` in the raw file.
    """
    fields: dict[str, str] = {}
    matches = list(INFO_FIELD_RE.finditer(text))
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():stop].strip()
        if value.startswith("'"):
            value = value[1:]
        if value.endswith("'"):
            value = value[:-1]
        fields[match.group(1)] = value.replace("%25", "%").strip()
    return fields


def _aircraft_info(archive, locale: str) -> dict[str, dict]:
    """Per-aircraft notes, keyed by the aircraft's directory code."""
    wanted = f"info.locale={locale.lower()}.txt"
    out: dict[str, dict] = {}
    for member in sorted(archive.entries):
        if not member.endswith(wanted):
            continue
        code = member.split("/")[5]
        try:
            fields = parse_info_file(archive.read(member).decode("utf-8-sig", "replace"))
        except Exception:
            continue
        if fields.get("description") or fields.get("name"):
            out[code] = {
                "name": fields.get("name", ""),
                "description": fields.get("description", ""),
            }
    return out


def parse_plane_script(text: str, names: dict[str, dict]) -> dict:
    payloads: dict[str, dict] = {}
    for match in AMMO_RE.finditer(text):
        index, body = match.group(1), match.group(2)
        label = NAME_FIELD_RE.search(body)
        raw = label.group(1) if label else ""
        ballistics = _ballistics(body)
        payloads[index] = {
            "raw": raw,
            "items": tokenise_payload(raw, names, ballistics) if raw else [],
        }

    object_name = ""
    found = OBJECT_NAME_RE.search(text)
    if found:
        object_name = found.group(1).strip()

    return {
        "object_name": object_name,
        "payloads": payloads,
        "modes": _weapon_modes(text),
    }


# ------------------------------------------------------------------- build


def build_tables(install_base: Path, locale: str = "eng") -> dict:
    """Read both archives and return the full name table."""
    data_dir = Path(install_base) / "data"
    scripts = data_dir / "Scripts.gtp"
    swf = data_dir / "Swf.gtp"

    for path in (scripts, swf):
        if not path.is_file():
            raise ExtractError(
                f"{path.name} not found at {path}. Weapon names come from the game's "
                "own tables, so loadouts will show raw identifiers without it."
            )

    started = time.time()

    # Names first -- the vocabulary is what makes payload parsing possible. The
    # aircraft notes come from the same archive, so both are taken in one pass.
    try:
        with open_archive(swf, _either(LOCALE_MEMBER_RE, INFO_MEMBER_RE)) as archive:
            member = _pick_locale(archive.entries, locale)
            if not member:
                raise ExtractError(
                    f"no ammunition name table found inside {swf.name}"
                )
            chosen_locale = _locale_of(member)
            names = parse_locale_table(_decode_locale(archive.read(member)))
            info = _aircraft_info(archive, chosen_locale or locale)
    except GtpError as exc:
        raise ExtractError(str(exc)) from exc

    if not names:
        raise ExtractError(f"the name table inside {swf.name} contained no entries")

    aircraft: dict[str, dict] = {}
    try:
        with open_archive(scripts, PLANE_MEMBER_RE) as archive:
            for member in sorted(archive.entries):
                stem = member.rsplit("/", 1)[-1]
                if stem.startswith("_"):
                    continue  # helper documents, not aircraft
                text = archive.read(member).decode("cp1251", "replace")
                record = parse_plane_script(text, names)
                if not record["payloads"]:
                    continue
                record["code"] = stem[:-4]
                aircraft[member] = record
    except GtpError as exc:
        raise ExtractError(str(exc)) from exc

    if not aircraft:
        # An empty result is a failure, not an answer -- caching it would make a
        # transient problem permanent.
        raise ExtractError(
            f"read {scripts.name} but found no aircraft loadout tables; the archive "
            "layout may have changed in a game update"
        )

    unresolved = sorted(
        {
            item["code"]
            for record in aircraft.values()
            for payload in record["payloads"].values()
            for item in payload["items"]
            if not item["known"] and item["code"]
        }
    )

    return {
        "schema": CACHE_SCHEMA,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_seconds": round(time.time() - started, 3),
        "source": {
            "install": str(install_base),
            "locale": chosen_locale,
            "scripts": _stamp(scripts),
            "swf": _stamp(swf),
        },
        "stats": {
            "aircraft": len(aircraft),
            "names": len(names),
            "notes": len(info),
            "unresolved": unresolved,
        },
        "names": names,
        "aircraft": aircraft,
        "info": info,
    }


def _either(*patterns: re.Pattern) -> re.Pattern:
    return re.compile("|".join(f"(?:{p.pattern})" for p in patterns), patterns[0].flags)


def _pick_locale(entries: dict, locale: str) -> str:
    """Pick the requested language's table by path, falling back to English."""
    wanted = f"ammunition.locale={locale.lower()}.txt"
    for name in entries:
        if name.endswith(wanted):
            return name
    for name in entries:
        if name.endswith("ammunition.locale=eng.txt"):
            return name
    # Last resort must still be an ammunition table -- the archive is now opened
    # with a filter that also admits the aircraft note files.
    tables = sorted(n for n in entries if "/ammunition/" in n)
    return tables[0] if tables else ""


def _locale_of(member: str) -> str:
    found = re.search(r"locale=(\w+)\.txt$", member)
    return found.group(1) if found else ""


def _decode_locale(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _stamp(path: Path) -> dict:
    try:
        stat = path.stat()
        return {"size": stat.st_size, "mtime": int(stat.st_mtime)}
    except OSError:
        return {"size": 0, "mtime": 0}


# ------------------------------------------------------------------- cache


def _cache_key(payload: dict) -> tuple:
    source = payload.get("source", {})
    scripts = source.get("scripts", {})
    swf = source.get("swf", {})
    return (
        payload.get("schema"),
        source.get("locale"),
        source.get("install"),
        scripts.get("size"),
        scripts.get("mtime"),
        swf.get("size"),
        swf.get("mtime"),
    )


def load_tables(install_base: Path, locale: str = "eng") -> tuple[dict, str]:
    """Return ``(tables, origin)`` where origin is "cache" or "built"."""
    expected = {
        "schema": CACHE_SCHEMA,
        "source": {
            "install": str(install_base),
            "locale": locale,
            "scripts": _stamp(Path(install_base) / "data" / "Scripts.gtp"),
            "swf": _stamp(Path(install_base) / "data" / "Swf.gtp"),
        },
    }

    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        # The locale actually used may differ from the one requested; compare on
        # what was requested to avoid rebuilding forever when a language is absent.
        probe = dict(cached)
        probe_source = dict(cached.get("source", {}))
        probe_source["locale"] = locale
        probe["source"] = probe_source
        if _cache_key(probe) == _cache_key(expected) and cached.get("aircraft"):
            return cached, "cache"
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass

    tables = build_tables(install_base, locale)
    _write_cache(tables)
    return tables, "built"


def _write_cache(tables: dict) -> None:
    temp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
    try:
        temp.write_text(json.dumps(tables, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, CACHE_PATH)
    except OSError:
        # A read-only project directory must not break the feature; it only means
        # rebuilding each run.
        try:
            temp.unlink()
        except OSError:
            pass
