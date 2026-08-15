"""Build the double-click Windows package and zip it for a release.

    python packaging/build.py

Produces ``dist/MiniKneeboard-<version>-win64.zip``, containing a folder the
user unzips anywhere and runs ``MiniKneeboard.exe`` from. No Python, no pip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bmskb import __version__  # noqa: E402
from bmskb.selfupdate import REPO_URL  # noqa: E402


def run(*args: str) -> None:
    print(f"  $ {' '.join(args)}")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode:
        raise SystemExit(f"failed: {' '.join(args)}")


def main() -> int:
    print(f"Building Mini Kneeboard {__version__}")

    for stale in (ROOT / "build", ROOT / "dist"):
        if stale.exists():
            shutil.rmtree(stale)

    run(sys.executable, str(ROOT / "packaging" / "make_icon.py"))
    run(
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        str(ROOT / "packaging" / "kneeboard.spec"),
    )

    folder = ROOT / "dist" / "MiniKneeboard"
    if not (folder / "MiniKneeboard.exe").is_file():
        raise SystemExit("the executable was not produced")

    # A short read-me beside the exe, because the first thing a stranger does
    # with an unsigned download is look for a reason to trust it.
    (folder / "README.txt").write_text(
        "Mini Kneeboard {v}\n"
        "{rule}\n\n"
        "Run MiniKneeboard.exe. No installation, nothing written outside this\n"
        "folder except your settings and map caches, which live in:\n"
        "  %LOCALAPPDATA%\\Kneeboard\n\n"
        "The board finds Falcon BMS, DCS World and IL-2 automatically. It reads\n"
        "the files those games already write and never modifies them.\n\n"
        "It serves the board on a local web address, so you can also open it on\n"
        "a tablet or phone on the same network -- the address is shown in the\n"
        "log file, in %LOCALAPPDATA%\\Kneeboard\\kneeboard.log\n\n"
        "Windows may warn that the publisher is unrecognised. That is because\n"
        "the executable is not code-signed, not because anything is wrong with\n"
        "it. Source: {repo}\n".format(
            v=__version__, rule="=" * (16 + len(__version__)), repo=REPO_URL
        ),
        encoding="utf-8",
    )

    archive = ROOT / "dist" / f"MiniKneeboard-{__version__}-win64.zip"
    print(f"  zipping {archive.name}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, Path("MiniKneeboard") / path.relative_to(folder))

    size = archive.stat().st_size / 1_000_000
    files = sum(1 for p in folder.rglob("*") if p.is_file())
    print(f"\n  {archive}")
    print(f"  {size:.1f} MB zipped, {files} files unpacked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
