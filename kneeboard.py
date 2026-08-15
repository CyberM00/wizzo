"""Falcon BMS second-monitor kneeboard.

Run it, then drag the browser window to your second monitor and press F11.

    python kneeboard.py

The board reads the briefing BMS exports and rebuilds itself whenever that file
changes, so committing to a new mission is all it takes to refresh.
"""

from __future__ import annotations

import argparse
import io
import mimetypes
import os
import re
import socket
import sys
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from bmskb import __version__
from bmskb.desktop import browse_available, browse_for_folder, find_port
from bmskb.desktop import run as desktop_run
from bmskb.paths import FROZEN, app_root, state_path
from bmskb.dcs.install import DcsInstall
from bmskb.il2.gtp import GtpError
from bmskb.il2.gtp import open_archive as il2_open_archive
from bmskb.il2.install import Il2Install
from bmskb.install import BmsInstall
from bmskb.selfupdate import REEXEC_GUARD, check_and_update, check_release, describe
from bmskb.state import KneeboardState, validate_laser_code

APP_ROOT = app_root()

app = Flask(__name__)
state: KneeboardState | None = None
update_info: dict = {}


def _serve_from(root: Path | None, relative: str):
    """Send a file from within ``root``, refusing anything that escapes it."""
    if not root or not root.is_dir():
        abort(404)
    try:
        target = (root / relative).resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        abort(404)
    if root_resolved != target and root_resolved not in target.parents:
        abort(403)
    if not target.is_file():
        abort(404)
    return send_file(target)


@app.route("/")
def index():
    return render_template("index.html", version=__version__)


@app.route("/api/state")
def api_state():
    assert state is not None
    payload = dict(state.get(force=request.args.get("force") == "1"))
    payload["app"] = {"version": __version__, "update": update_info}
    return jsonify(payload)


MAP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")


@app.route("/il2map/<map_id>")
def il2_map(map_id: str):
    """Serve an IL-2 theatre map, stitching it on first request.

    Stitching decodes eighty tiles, so it is done here rather than while building
    the board -- the cost is paid once, when the Maps page is actually opened, and
    the result is cached until the game's archive changes.
    """
    assert state is not None
    if not MAP_ID_RE.match(map_id or "") or state.il2 is None:
        abort(404)

    from bmskb.il2.maps import MapError, TheatreMap

    theatre = TheatreMap(state.il2.data_dir, map_id)
    if not theatre.ok:
        abort(404)
    try:
        path = theatre.build()
    except MapError as exc:
        app.logger.warning("IL-2 map %s could not be built: %s", map_id, exc)
        abort(503)
    response = send_file(path, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


DCS_MAP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


@app.route("/dcsmap/<map_id>")
def dcs_map(map_id: str):
    """Serve a DCS theatre chart, stitching it on first request.

    The id is only ever accepted when it is the one the current payload asked
    for, so nothing here is reconstructed from user input -- the crop, the
    terrain and the resolution all come from the board's own state. Stitching
    decodes a few dozen tiles, so it happens here rather than while the board is
    built, and the result is cached until the game's own files change.
    """
    assert state is not None
    if not DCS_MAP_ID_RE.match(map_id or ""):
        abort(404)

    payload = state.get()
    theatre = ((payload.get("charts") or {}).get("theatre")) or {}
    if payload.get("sim") != "dcs" or theatre.get("map_id") != map_id:
        abort(404)

    chart = state.dcs_chart
    if chart is None or chart.map_id != map_id:
        abort(404)

    from bmskb.dcs.maps import MapError, load_verdicts, save_verdict

    try:
        path = chart.build()
    except MapError as exc:
        app.logger.warning("DCS chart %s could not be built: %s", map_id, exc)
        abort(503)
    # Check it against the terrain's own coastline now that there is something to
    # check. The verdict is only read on the next build of the board, so a chart
    # that lands badly is replaced by the honest fallback rather than shown twice.
    # Once recorded for these game files it is not measured again -- every reload
    # of the page would otherwise pay for it.
    stamp = chart.terrain.source_stamp()
    if (load_verdicts().get(map_id) or {}).get("stamp") != stamp:
        try:
            save_verdict(map_id, stamp, chart.alignment())
        except Exception:  # noqa: BLE001 - a failed self-check must not fail the request
            app.logger.warning("DCS chart %s could not be checked", map_id)
    response = send_file(path, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/manual/<sim>/<path:relative>")
def manual_file(sim: str, relative: str):
    """Serve an aircraft manual from a sim's own install.

    Confined to the sim's install root by ``_serve_from``, and only for a sim that
    actually has an indexed manual library.
    """
    assert state is not None
    library = state.manuals.get(sim)
    if library is None or library.base is None:
        abort(404)
    return _serve_from(library.base, relative)


@app.route("/api/sims")
def api_sims():
    """Status of every sim, for the landing page. Stats only, no mission parsing."""
    assert state is not None
    return jsonify({"sims": state.sims_overview()})


@app.route("/api/token")
def api_token():
    """Cheap freshness probe -- the page polls this and reloads on change."""
    assert state is not None
    return jsonify({"token": state.token()})


@app.route("/api/paths")
def api_paths():
    """Where each sim is installed, and where the board looked."""
    assert state is not None
    payload = state.sim_paths()
    payload["can_browse"] = browse_available()
    return jsonify(payload)


@app.route("/api/paths", methods=["POST"])
def api_set_path():
    """Point the board at a sim, or tell it a sim is not installed.

    The board rebuilds around the new folder rather than asking to be restarted,
    because "restart the app" is the kind of instruction that gets a bug report
    instead of a restart.
    """
    assert state is not None
    payload = request.get_json(silent=True) or {}

    if payload.get("done"):
        state.finish_setup()
        return jsonify({"ok": True, **state.sim_paths(), "can_browse": browse_available()})

    sim = str(payload.get("sim", ""))
    if "path" not in payload:
        return jsonify({"ok": False, "error": "No folder given."}), 400
    ok, error = state.set_sim_path(sim, str(payload.get("path", "")))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, **state.sim_paths(), "can_browse": browse_available()})


@app.route("/api/browse", methods=["POST"])
def api_browse():
    """Open a folder picker, when running in the packaged window.

    A browser cannot be asked to pick a folder and hand back its path, so this
    only works in the desktop build; the page shows a plain text box otherwise.
    """
    payload = request.get_json(silent=True) or {}
    chosen = browse_for_folder(str(payload.get("title", "Select the game folder")))
    return jsonify({"ok": bool(chosen), "path": chosen})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    assert state is not None
    payload = request.get_json(silent=True) or {}

    for field in ("laser_code", "wingman_laser_code"):
        if field in payload:
            ok, message = validate_laser_code(str(payload[field]))
            if not ok:
                return jsonify({"ok": False, "error": message}), 400

    return jsonify({"ok": True, "settings": state.update_settings(payload)})


@app.route("/chart/<path:relative>")
def chart_file(relative: str):
    assert state is not None
    return _serve_from(state.install.charts_dir if state.install else None, relative)


@app.route("/map/<path:relative>")
def map_file(relative: str):
    assert state is not None
    return _serve_from(state.install.maps_dir if state.install else None, relative)


@app.route("/page/<path:entry>")
def mission_page(entry: str):
    """Serve a kneeboard image straight out of the current .miz archive."""
    assert state is not None
    state.get()
    mission = state.dcs_mission
    if mission is None:
        abort(404)
    # Only entries the mission itself listed as kneeboard pages are servable,
    # so a crafted path cannot pull arbitrary members out of the archive.
    allowed = {page["entry"] for page in mission.kneeboard_pages()}
    if entry not in allowed:
        abort(403)
    try:
        data = mission.read_entry(entry)
    except (KeyError, OSError):
        abort(404)
    guessed = mimetypes.guess_type(entry)[0] or "application/octet-stream"
    return send_file(io.BytesIO(data), mimetype=guessed, download_name=Path(entry).name)


@app.route("/il2page/<path:entry>")
def il2_campaign_page(entry: str):
    """Serve a scripted campaign's briefing map out of Campaigns.gtp.

    Restricted to the entries the current payload actually listed, so a crafted
    path cannot pull other members out of the archive.
    """
    assert state is not None
    payload = state.get()
    allowed = {page["entry"] for page in (payload.get("charts") or {}).get("pages", [])}
    if entry not in allowed or not state.il2:
        abort(403)
    try:
        with il2_open_archive(state.il2.campaigns_archive) as archive:
            data = archive.read(entry)
    except (GtpError, KeyError, OSError):
        abort(404)
    guessed = mimetypes.guess_type(entry)[0] or "application/octet-stream"
    return send_file(io.BytesIO(data), mimetype=guessed, download_name=Path(entry).name)


def _lan_address() -> str:
    """Best-effort LAN IP, so the board can be opened on a tablet."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _start_logging() -> Path | None:
    """Send console output to a file, since a windowed build has no console.

    Without this, a packaged copy that cannot find a sim -- or cannot bind a
    port -- fails with nothing to look at. The log is truncated each run so it
    describes this launch rather than growing forever.
    """
    if not FROZEN:
        return None
    path = state_path("kneeboard.log")
    try:
        stream = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = stream
    sys.stderr = stream
    return path


def main() -> int:
    global state, update_info

    log_path = _start_logging()

    parser = argparse.ArgumentParser(description="Falcon BMS second-monitor kneeboard")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=5000, help="port (default: 5000)")
    parser.add_argument("--bms-path", default=None, help="override the BMS install path")
    parser.add_argument("--dcs-path", default=None, help="override the DCS install path")
    parser.add_argument("--il2-path", default=None, help="override the IL-2 install path")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--window",
        dest="window",
        action="store_true",
        default=None,
        help="show the board in its own window (the default for the packaged build)",
    )
    parser.add_argument(
        "--no-window",
        dest="window",
        action="store_false",
        help="serve only, and use a browser (the default when run from source)",
    )
    parser.add_argument(
        "--no-update", action="store_true", help="skip the check for a newer version"
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="report whether an update is available, then exit without changing anything",
    )
    args = parser.parse_args()

    print(f"Mini Kneeboard {__version__}")
    if log_path:
        print(f"  log:     {log_path}")

    # A clone fast-forwards itself; a packaged copy has no repo to pull, so it
    # reports what is available and leaves the swap to the user.
    def look_for_update(enabled: bool, dry_run: bool = False) -> dict:
        if FROZEN:
            return check_release(__version__, enabled=enabled)
        return check_and_update(APP_ROOT, enabled=enabled, dry_run=dry_run)

    if args.check_update:
        result = look_for_update(True, dry_run=True)
        for line in describe(result) or ["  update:  Nothing to report."]:
            print(line)
        return 0

    update_info = look_for_update(not args.no_update)
    for line in describe(update_info):
        print(line)

    # The code for this run was already imported, so hand off to a fresh process
    # to actually run what was just pulled. The guard variable makes this a
    # one-shot: the restarted process will not check again.
    if update_info.get("updated"):
        print("  restart: relaunching on the updated version...\n")
        os.environ[REEXEC_GUARD] = "1"
        # execv replaces this process, discarding anything still sitting in the
        # stdout buffer. Without this flush the summary of what was updated is
        # lost whenever output is redirected to a file rather than a console.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError as exc:
            print(f"  NOTE:    could not restart automatically ({exc}).")
            print("           The update applies the next time you start the board.\n")

    # Discovery has to see the saved folders, so the settings are read before the
    # sims are looked for rather than after.
    cli_paths = {"bms": args.bms_path, "dcs": args.dcs_path, "il2": args.il2_path}
    saved = KneeboardState.load_settings_file()
    install, dcs, il2 = KneeboardState.discover_all(cli_paths, saved)
    state = KneeboardState(install, dcs, il2, cli_paths=cli_paths)

    if install:
        print(f"  BMS {install.version} at {install.base}")
        print(f"  theater: {install.theater or 'unknown'}")
        if install.pilot_callsign:
            print(f"  pilot:   {install.pilot_callsign} ({install.pilot_name})")
        summary = state.charts.describe()
        print(
            f"  indexed: {summary['chart_count']} charts across "
            f"{summary['airfield_count']} airfields, {summary['map_count']} maps"
        )
        if not install.briefing_file.is_file():
            print("  note:    briefing.txt not present yet -- commit to a mission in BMS")
    else:
        print("  no BMS installation found (set BMS_PATH or pass --bms-path).")

    if dcs:
        info = dcs.describe()
        print(f"  DCS at {info['base'] or 'unknown path'}")
        if info["latest_mission_name"]:
            print(
                f"  missions: {info['mission_count']} found, newest "
                f"{info['latest_mission_name']}"
            )
        else:
            print("  note:    no .miz found under Saved Games\\DCS\\Missions")
    else:
        print("  no DCS installation found (set DCS_PATH or pass --dcs-path).")

    if il2:
        info = il2.describe()
        print(f"  IL-2 at {info['base']}")
        if info["latest_mission_name"]:
            print(
                f"  missions: {info['mission_count']} found, newest "
                f"{info['latest_mission_name']}  (language {info['language']})"
            )
        else:
            print("  note:    no mission found under data\\Missions -- fly a career sortie")
        if info["sortie_count"]:
            print(f"  sorties: {info['sortie_count']} logged, newest {info['latest_sortie_name']}")
        if not info["text_log_enabled"]:
            print(
                "  note:    mission_text_log is 0 in data\\startup.cfg, so the as-flown "
                "loadout cannot be read"
            )
    else:
        print("  no IL-2 installation found (set IL2_PATH or pass --il2-path).")

    active, mission = state.choose_sim()
    print(f"  showing: {active.upper()}" + (f" -- {mission.name}" if mission else ""))

    if not install and not dcs and not il2:
        print("  WARNING: no sim was found; the board will have nothing to show.")

    # A packaged copy is double-clicked, so it cannot fail on a port someone else
    # already holds. An explicitly requested port is honoured or refused.
    asked = "--port" in sys.argv
    port = find_port(args.host, args.port, walk=not asked)
    if port is None:
        print(f"  ERROR:   port {args.port} is already in use.")
        return 1
    if port != args.port:
        print(f"  note:    port {args.port} was busy, using {port} instead")

    url = f"http://localhost:{port}"
    print(f"\n  Open {url}")
    lan = _lan_address()
    if lan and args.host == "0.0.0.0":
        print(f"  On a tablet or phone: http://{lan}:{port}")

    def serve():
        app.run(host=args.host, port=port, debug=False, threaded=True)

    windowed = args.window if args.window is not None else FROZEN
    if windowed:
        print("  Opening the board in its own window.\n")
        # The server keeps running either way -- the window is a front end to it,
        # not a replacement, so a tablet can still open the same board.
        failed = desktop_run(url, f"Mini Kneeboard {__version__}", serve, port)
        if not failed:
            return 0
        print(f"  note:    {failed}; opening a browser instead")
        if not args.no_browser:
            webbrowser.open(url)
        serve()
        return 0

    print("  Drag the window to your second monitor and press F11 for fullscreen.\n")
    if not args.no_browser:
        webbrowser.open(url)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
