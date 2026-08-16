# PyInstaller spec for the double-click build.
#
# One folder, not one file. A one-file build unpacks itself to a temp directory
# on every launch, which is slower to start and markedly more likely to be
# quarantined by antivirus -- and it buys nothing here, because the thing is
# distributed as a zip either way.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
    # Curated reference tables. These resolve through each module's own
    # __file__, which PyInstaller rewrites into the bundle, so they have to land
    # on the same relative paths they occupy in the source tree.
    (str(ROOT / "bmskb" / "data"), "bmskb/data"),
    (str(ROOT / "bmskb" / "dcs" / "data"), "bmskb/dcs/data"),
]

hiddenimports = [
    # Pillow loads its codecs by name at runtime, so the DDS plugin the theatre
    # charts depend on is invisible to static analysis.
    "PIL.DdsImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin",
    *collect_submodules("webview.platforms"),
]

a = Analysis(
    [str(ROOT / "wizzo.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nothing here draws a chart with matplotlib or holds a frame in numpy;
        # left in, they roughly double the download.
        "matplotlib", "numpy", "scipy", "pandas", "tkinter", "test", "unittest",
        "pydoc_data", "setuptools", "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Wizzo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: this is double-clicked. Anything worth reading goes to the log
    # file in the state folder instead.
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "wizzo.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Wizzo",
)
