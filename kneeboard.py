"""Falcon BMS second-monitor kneeboard.

Run it, then drag the browser window to your second monitor and press F11.

    python kneeboard.py

The board reads the briefing BMS exports and rebuilds itself whenever that file
changes, so committing to a new mission is all it takes to refresh.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from bmskb import __version__
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
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--no-update", action="store_true", help="skip the check for a newer version"
    )
    args = parser.parse_args()

    print(f"BMS Kneeboard {__version__}")

    update_info = check_and_update(APP_ROOT, enabled=not args.no_update)
    for line in describe(update_info):
        print(line)

    # The code for this run was already imported, so hand off to a fresh process
    # to actually run what was just pulled. The guard variable makes this a
    # one-shot: the restarted process will not check again.
    if update_info.get("updated"):
        print("  restart: relaunching on the updated version...\n")
        os.environ[REEXEC_GUARD] = "1"
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError as exc:
            print(f"  NOTE:    could not restart automatically ({exc}).")
            print("           The update applies the next time you start the board.\n")

    install = BmsInstall.discover(args.bms_path)
    state = KneeboardState(install)

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
        print("  WARNING: no BMS installation found.")
        print("  Set BMS_PATH or pass --bms-path pointing at your BMS folder.")

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
