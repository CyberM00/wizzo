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
import socket
import sys
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from bmskb import __version__
from bmskb.dcs.install import DcsInstall
from bmskb.il2.gtp import GtpError
from bmskb.il2.gtp import open_archive as il2_open_archive
from bmskb.il2.install import Il2Install
from bmskb.install import BmsInstall
from bmskb.selfupdate import REEXEC_GUARD, check_and_update, describe
from bmskb.state import KneeboardState, validate_laser_code

APP_ROOT = Path(__file__).resolve().parent

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


def main() -> int:
    global state, update_info

    parser = argparse.ArgumentParser(description="Falcon BMS second-monitor kneeboard")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=5000, help="port (default: 5000)")
    parser.add_argument("--bms-path", default=None, help="override the BMS install path")
    parser.add_argument("--dcs-path", default=None, help="override the DCS install path")
    parser.add_argument("--il2-path", default=None, help="override the IL-2 install path")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--no-update", action="store_true", help="skip the check for a newer version"
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="report whether an update is available, then exit without changing anything",
    )
    args = parser.parse_args()

    print(f"BMS Kneeboard {__version__}")

    if args.check_update:
        result = check_and_update(APP_ROOT, enabled=True, dry_run=True)
        for line in describe(result) or ["  update:  Nothing to report."]:
            print(line)
        return 0

    update_info = check_and_update(APP_ROOT, enabled=not args.no_update)
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

    install = BmsInstall.discover(args.bms_path)
    dcs = DcsInstall.discover(args.dcs_path)
    il2 = Il2Install.discover(args.il2_path)
    state = KneeboardState(install, dcs, il2)

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

    url = f"http://localhost:{args.port}"
    print(f"\n  Open {url}")
    lan = _lan_address()
    if lan and args.host == "0.0.0.0":
        print(f"  On a tablet or phone: http://{lan}:{args.port}")
    print("  Drag the window to your second monitor and press F11 for fullscreen.\n")

    if not args.no_browser:
        webbrowser.open(url)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
