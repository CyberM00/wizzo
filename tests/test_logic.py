"""Tests for the parts a game patch, or a careless edit, could quietly break.

Most of this project reads files only present on a machine with the games
installed, and there is no honest way to test that without them -- so those
tests skip rather than fail, and CI runs the rest. What is covered here is the
logic that has an exactly checkable answer:

* the Transverse Mercator solver, against the sim's own published beacons
* the .sup5 tile-index record layout
* the install-folder verifier, including the cross-game confusions that an
  earlier version of it got wrong
* version comparison, which decides whether an update is offered

The projection and index tests need DCS. They are the ones most worth having
when a DCS patch lands, so they run whenever a DCS install is present and skip
politely when it is not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bmskb import simpaths
from bmskb.selfupdate import _version_tuple


def _dcs_root() -> Path | None:
    """A DCS install with terrains, or None."""
    from bmskb.dcs.install import DcsInstall

    install = DcsInstall.discover(os.environ.get("DCS_PATH"))
    if install and install.base and (install.base / "Mods" / "terrains").is_dir():
        return install.base
    return None


needs_dcs = pytest.mark.skipif(_dcs_root() is None, reason="no DCS installation here")


# -- version comparison ---------------------------------------------------


@pytest.mark.parametrize(
    "older, newer",
    [
        ("1.9.0", "1.10.0"),   # the one a string compare gets wrong
        ("1.2.0", "1.11.0"),
        ("1.13.0", "1.13.1"),
        ("0.9", "1.0"),
        ("1.0.0", "1.0.1"),
    ],
)
def test_version_ordering(older, newer):
    assert _version_tuple(older) < _version_tuple(newer)


def test_version_equal_is_not_newer():
    assert not _version_tuple("1.13.1") < _version_tuple("1.13.1")


def test_version_survives_junk():
    # A tag that is not a version must not raise; it just sorts low.
    assert _version_tuple("") == (0,)
    assert _version_tuple("not-a-version") == (0,)


# -- install folder verification -----------------------------------------


def test_verify_rejects_missing_and_empty(tmp_path):
    ok, why = simpaths.verify("bms", tmp_path / "definitely-not-here")
    assert not ok and "does not exist" in why

    ok, why = simpaths.verify("bms", "")
    assert not ok and why

    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    ok, why = simpaths.verify("bms", a_file)
    assert not ok and "not a folder" in why


def test_verify_rejects_unknown_sim(tmp_path):
    ok, _ = simpaths.verify("msfs", tmp_path)
    assert not ok


@pytest.mark.parametrize("sim, marker", [
    ("bms", "Data/TerrData"),
    ("bms", "User/Briefings"),
    ("dcs", "Mods/terrains"),
    ("il2", "data/Swf.gtp"),
])
def test_verify_accepts_each_marker(tmp_path, sim, marker):
    """Any one marker is enough, and a file counts as well as a folder."""
    target = tmp_path / marker
    if marker.endswith(".gtp"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    else:
        target.mkdir(parents=True, exist_ok=True)
    ok, why = simpaths.verify(sim, tmp_path)
    assert ok, why


@pytest.mark.parametrize("offered_as, built_as", [
    ("bms", "dcs"),
    ("dcs", "bms"),
    ("il2", "dcs"),
    ("il2", "bms"),
    ("dcs", "il2"),
])
def test_verify_rejects_the_wrong_game(tmp_path, offered_as, built_as):
    """The regression that mattered: every game has Data, bin and data.

    An earlier version checked those, and so accepted a DCS folder as Falcon BMS
    and IL-2 Sturmovik 1946 as IL-2 Great Battles -- both of which would have
    produced an empty board with no explanation.
    """
    for marker in simpaths.MARKERS[built_as]:
        (tmp_path / marker).mkdir(parents=True, exist_ok=True)
    # The generic folders all three share, to make the trap explicit.
    for shared in ("Data", "data", "bin", "Bin"):
        (tmp_path / shared).mkdir(parents=True, exist_ok=True)

    ok, why = simpaths.verify(offered_as, tmp_path)
    assert not ok, f"{offered_as} accepted a {built_as} folder"
    assert "does not look like" in why


def test_il2_1946_is_not_il2_great_battles(tmp_path):
    """The real folder that fooled the first version: bin, no data/Swf.gtp."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "Missions").mkdir()
    ok, why = simpaths.verify("il2", tmp_path)
    assert not ok
    assert "1946" in why  # the message names the game it is not


# -- which folder the board actually uses --------------------------------


def test_override_precedence(monkeypatch):
    monkeypatch.delenv("BMS_PATH", raising=False)
    settings = {"bms_path": r"D:\saved"}

    assert simpaths.override_for("bms", {"bms": r"D:\cli"}, settings) == r"D:\cli"

    monkeypatch.setenv("BMS_PATH", r"D:\env")
    assert simpaths.override_for("bms", {}, settings) == r"D:\env"
    assert simpaths.override_for("bms", {"bms": r"D:\cli"}, settings) == r"D:\cli"

    monkeypatch.delenv("BMS_PATH")
    assert simpaths.override_for("bms", {}, settings) == r"D:\saved"
    assert simpaths.override_for("bms", {}, {}) is None


def test_not_installed_short_circuits(monkeypatch):
    monkeypatch.delenv("IL2_PATH", raising=False)
    settings = {"il2_path": simpaths.NOT_INSTALLED}
    assert simpaths.override_for("il2", {}, settings) == simpaths.NOT_INSTALLED


# -- the terrain projection ----------------------------------------------


@needs_dcs
@pytest.mark.parametrize("terrain, meridian", [
    ("Syria", 39.0),
    ("Caucasus", 33.0),
    ("PersianGulf", 57.0),
])
def test_projection_matches_the_published_beacons(terrain, meridian):
    """Solved from each terrain's own beacon table, and exact rather than fitted.

    The residual is the whole argument for trusting waypoints drawn on a chart,
    so it is asserted rather than assumed. A metre of slack is generous: the
    measured worst case across the three terrains is 0.8 m.
    """
    from bmskb.dcs.maps import TerrainCharts

    root = _dcs_root().parent if (_dcs_root() / "Mods").is_dir() is False else _dcs_root()
    charts = TerrainCharts(root, terrain)
    if not charts.projection or not charts.projection.ok:
        pytest.skip(f"{terrain} is not installed")

    assert charts.projection.samples >= 50
    assert charts.projection.lon0 == pytest.approx(meridian, abs=0.001)
    assert charts.projection.residual_max < 1.0


@needs_dcs
def test_projection_round_trips():
    from bmskb.dcs.maps import TerrainCharts

    charts = TerrainCharts(_dcs_root(), "Syria")
    if not charts.projection or not charts.projection.ok:
        pytest.skip("Syria is not installed")

    for lat, lon in ((33.82, 35.49), (35.15, 33.90), (36.5, 37.2)):
        x, z = charts.projection.to_world(lat, lon)
        back_lat, back_lon = charts.projection.to_lonlat(x, z)
        assert back_lat == pytest.approx(lat, abs=1e-6)
        assert back_lon == pytest.approx(lon, abs=1e-6)


@needs_dcs
def test_tile_index_parses_and_is_gridded():
    """Every .sup5 record parses, and the boxes agree with a regular grid.

    Tile placement is read from this file rather than worked out from tile
    names precisely because the names do not describe a grid on every terrain.
    """
    from bmskb.dcs.maps import TerrainCharts

    charts = TerrainCharts(_dcs_root(), "Syria")
    if not charts.tiles:
        pytest.skip("Syria charts are not installed")

    assert len(charts.tiles) > 100
    for tile in charts.tiles:
        assert tile.maxx > tile.minx
        assert tile.maxz > tile.minz
        # A tile is square, and one tile is 1024 pixels of its own resolution.
        # Tiles overlap their neighbours by a pixel, so the span is 1023 across.
        span_x = tile.maxx - tile.minx
        span_z = tile.maxz - tile.minz
        assert span_x == pytest.approx(span_z, rel=0.01)
        assert span_x == pytest.approx(tile.mpp * 1023, rel=0.01)
        assert tile.member.endswith(".dds")

    # Both sheet resolutions the terrain ships are indexed, not just one.
    assert set(charts.by_mpp) >= {32, 64}


# -- names taken from DCS's own comments ---------------------------------


def test_comment_scan_reads_a_same_line_comment(tmp_path):
    from bmskb.dcs import clsidnames

    (tmp_path / "CoreMods").mkdir()
    (tmp_path / "CoreMods" / "a.lua").write_text(
        '{ CLSID = "{B06DD79A-1111}", Cx_gain_item = 0.4 }, -- LAU-118 + AGM-88\n'
        '{ CLSID = "{PLAIN-2222}" },\n',
        encoding="utf-8",
    )
    names = clsidnames._scan(tmp_path)
    assert names["{B06DD79A-1111}"] == "LAU-118 + AGM-88"
    # No comment means no name, rather than a name borrowed from a neighbour.
    assert "{PLAIN-2222}" not in names


def test_comment_scan_prefers_the_better_supported_reading(tmp_path):
    from bmskb.dcs import clsidnames

    (tmp_path / "CoreMods").mkdir()
    (tmp_path / "CoreMods" / "a.lua").write_text(
        '{ CLSID = "{X}" }, -- AGM-88 on LAU-118\n'
        '{ CLSID = "{X}" }, -- LAU-118 + AGM-88\n', encoding="utf-8")
    (tmp_path / "CoreMods" / "b.lua").write_text(
        '{ CLSID = "{X}" }, -- AGM-88 on LAU-118\n', encoding="utf-8")
    # Those are the same words in a different order, so they agree rather
    # than contradict, and the most repeated wording wins.
    assert clsidnames._scan(tmp_path)["{X}"] == "AGM-88 on LAU-118"


def test_comment_scan_refuses_to_pick_between_contradictions(tmp_path):
    from bmskb.dcs import clsidnames

    (tmp_path / "CoreMods").mkdir()
    (tmp_path / "CoreMods" / "a.lua").write_text(
        '{ CLSID = "{Y}" }, -- AIM-9X Sidewinder\n', encoding="utf-8")
    (tmp_path / "CoreMods" / "b.lua").write_text(
        '{ CLSID = "{Y}" }, -- Mk-84 bomb\n', encoding="utf-8")
    # Two unrelated readings with equal support: showing either would be a
    # coin toss, and a wrong weapon name is worse than an unresolved code.
    assert "{Y}" not in clsidnames._scan(tmp_path)


def test_comment_scan_rejects_comments_that_are_not_names(tmp_path):
    from bmskb.dcs import clsidnames

    (tmp_path / "CoreMods").mkdir()
    (tmp_path / "CoreMods" / "a.lua").write_text(
        '{ CLSID = "{A}" }, -- TODO check this\n'
        '{ CLSID = "{B}" }, -- 0.786\n'
        '{ CLSID = "{C}" }, -- ' + "x" * 80 + "\n",
        encoding="utf-8",
    )
    names = clsidnames._scan(tmp_path)
    assert not ({"{A}", "{B}", "{C}"} & set(names))


def test_library_falls_back_and_flags_where_the_name_came_from():
    from bmskb.dcs.weapons import DcsWeaponLibrary

    lib = DcsWeaponLibrary(None)          # no install: nothing to read
    lib.from_game = {"{Z}": "AGM-88C"}    # as if the scan had found one

    named = lib.lookup("{Z}")
    assert named["name"] == "AGM-88C"
    assert named["known"] is False        # no employment detail behind it
    assert named["named_by_game"] is True
    assert named["employment"] == []

    raw = lib.lookup("{UNSEEN}")
    assert raw["name"] == "{UNSEEN}"
    assert raw["named_by_game"] is False


@needs_dcs
def test_the_harm_on_the_hornet_resolves():
    """The code that prompted all this, read from a real install."""
    from bmskb.dcs.weapons import DcsWeaponLibrary

    lib = DcsWeaponLibrary(_dcs_root())
    if not lib.from_game:
        pytest.skip("no names could be read from this install")
    found = lib.lookup("{B06DD79A-F21E-4EB9-BD9D-AB3844618C93}")
    assert found["named_by_game"]
    assert "88" in found["name"]


# -- punctuation the project does not use --------------------------------


def test_no_em_dashes_anywhere():
    """No em-dashes or en-dashes in anything this repository ships.

    They depend on every step of the chain reading the encoding correctly, and
    turn into mojibake when one does not. A spaced double hyphen never does.

    The characters are written as escapes so this file does not trip its own
    check.
    """
    banned = {
        "\u2014": "em-dash",
        "\u2013": "en-dash",
        # Assembled, so the literal entity does not appear in this file.
        "&" + "mdash;": "an em-dash HTML entity",
        "&" + "ndash;": "an en-dash HTML entity",
    }
    exts = {".py", ".md", ".js", ".css", ".html", ".txt", ".yml", ".yaml",
            ".toml", ".bat", ".ps1", ".json", ".cfg"}
    skip_dirs = {".git", "build", "dist", "__pycache__", ".venv", "node_modules"}

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        # Caches are built from the games' own data; their punctuation is theirs.
        if path.name in {"il2_name_cache.json", "dcs_clsid_names.json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for char, label in banned.items():
            if char in text:
                line = text[: text.index(char)].count("\n") + 1
                offenders.append(
                    f"{path.relative_to(root)}:{line} contains {label}"
                )

    assert not offenders, "use ' -- ' instead:\n  " + "\n  ".join(offenders)
