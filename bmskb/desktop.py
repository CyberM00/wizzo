"""Present the board as a desktop window rather than a browser tab.

Packaged for other people to use, "start a server, then open a browser" is two
steps too many. The server is still there -- it has to be, because the whole
point of the board is that a tablet on the same network can open it too -- but a
packaged copy puts a window in front of it so none of that is visible.

The window is the operating system's own web view (WebView2 on Windows, which
ships with Windows 11 and with any updated Windows 10). If it is missing, the
board falls back to opening the default browser, because a working board in
Firefox beats an error dialog about a runtime nobody has heard of.
"""

from __future__ import annotations

import socket
import threading
import time

WINDOW_MIN = (900, 600)
WINDOW_DEFAULT = (1400, 900)
# How long to let the server come up before showing the window, so the first
# paint is the board rather than a connection error.
READY_TIMEOUT = 15.0


def find_port(host: str, wanted: int, walk: bool = True, attempts: int = 25) -> int | None:
    """A port that actually binds.

    Port 5000 is a popular default and is often already taken -- by another copy
    of this board, or by anything else. Left alone that surfaces as a stack trace
    at startup, or as nothing at all once there is no console to print it to, so
    the board looks for the next free port instead.

    A port named explicitly on the command line is never walked away from: if
    someone asked for 5057, silently serving 5058 would be worse than failing.
    """
    bind_host = "" if host in ("0.0.0.0", "::") else host
    for offset in range(attempts if walk else 1):
        candidate = wanted + offset
        if candidate > 65535:
            break
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Deliberately no SO_REUSEADDR: on Windows it lets two sockets share
            # a port, which would make this check answer "free" for a port that
            # is anything but.
            probe.bind((bind_host, candidate))
        except OSError:
            continue
        else:
            return candidate
        finally:
            probe.close()
    return None


def wait_until_serving(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """Block until the server accepts a connection, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.15)
        finally:
            probe.close()
    return False


def browse_available() -> bool:
    """Whether a folder picker can be shown, i.e. whether a window is up."""
    try:
        import webview
    except ImportError:
        return False
    return bool(getattr(webview, "windows", None))


def browse_for_folder(title: str) -> str:
    """Ask the user for a folder. Empty string if they cancel or cannot be asked."""
    if not browse_available():
        return ""
    import webview

    try:
        chosen = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
    except Exception:  # noqa: BLE001 - a cancelled or unavailable dialog is not an error
        return ""
    if not chosen:
        return ""
    # Older versions hand back a tuple, newer ones a list; both hold one entry.
    return str(chosen[0]) if isinstance(chosen, (list, tuple)) else str(chosen)


def available() -> tuple[bool, str]:
    """Whether a native window can be shown here, and why not if it cannot."""
    try:
        import webview  # noqa: F401
    except ImportError as exc:
        return False, f"pywebview is not installed ({exc})"
    return True, ""


def run(url: str, title: str, serve, port: int) -> str:
    """Start the server on a thread and show the board in a window.

    Returns "" on a clean exit, or a reason the window could not be shown -- in
    which case the caller falls back to a browser.
    """
    ok, why = available()
    if not ok:
        return why

    import webview

    thread = threading.Thread(target=serve, daemon=True, name="kneeboard-server")
    thread.start()
    if not wait_until_serving(port):
        return "the board did not start serving in time"

    class Bridge:
        """The few things the page cannot do for itself inside a web view.

        Deliberately holds no attributes. pywebview walks this object to expose
        it to JavaScript, so a reference to the window stored here sends it into
        the native control's own object graph -- ``Bounds.Empty.Empty.Empty...``
        until it hits the recursion limit, taking the server thread down with it.
        The window is looked up when it is needed instead.
        """

        def toggle_fullscreen(self) -> bool:
            # F11 belongs to the browser, not the page, so inside a window
            # nothing is listening for it unless the page forwards it here.
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
            return True

    bridge = Bridge()
    try:
        # No background_color: older WebView2 builds have no
        # ICoreWebView2Controller2, and pywebview reaching for it throws a COM
        # exception into the log on every launch. The page sets its own theme
        # before the stylesheet paints anyway, so this bought nothing.
        webview.create_window(
            title,
            url,
            width=WINDOW_DEFAULT[0],
            height=WINDOW_DEFAULT[1],
            min_size=WINDOW_MIN,
            js_api=bridge,
        )
        webview.start()
    except Exception as exc:  # noqa: BLE001 - any failure here means "use a browser"
        return f"the desktop window could not be opened ({exc})"
    return ""
