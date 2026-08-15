"""Generate the Windows icon from the same plane the browser tab uses.

Kept as a build step rather than a committed binary so the icon cannot drift
from the favicon: both come from this one path string.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

# The favicon path from templates/index.html, in a 32x32 box.
PATH = (
    "M30 17.2c0 .9-.7 1.6-1.6 1.6h-8.1l-4.4 10.4c-.1.3-.4.5-.7.5h-2.4c-.5 0-.9-.5-.7-1"
    "l2.6-9.9H9l-2 3c-.2.2-.4.4-.7.4H4.6c-.5 0-.9-.5-.7-1l1.4-4.1-1.4-4.1c-.2-.5.2-1 .7-1"
    "h1.7c.3 0 .5.1.7.4l2 3h5.7l-2.6-9.9c-.2-.5.2-1 .7-1h2.4c.3 0 .6.2.7.5l4.4 10.4h8.1"
    "c.9 0 1.6.7 1.6 1.6z"
)

PLANE = "#ffb454"     # --amber
PANEL = "#241715"     # a shade off --panel, so the icon reads on a dark taskbar
SIZES = (16, 24, 32, 48, 64, 128, 256)

NUM = r"[-+]?(?:\d*\.\d+|\d+\.?)"
TOKEN = re.compile(r"([MmLlHhVvCcZz])|" + NUM)


def flatten(path: str, steps: int = 16) -> list[tuple[float, float]]:
    """The path as a polygon. Only the commands this one uses."""
    parts = [m.group(0) for m in TOKEN.finditer(path)]
    pts: list[tuple[float, float]] = []
    i, x, y, cmd = 0, 0.0, 0.0, None
    start = (0.0, 0.0)
    while i < len(parts):
        token = parts[i]
        if token.isalpha():
            cmd = token
            i += 1
            if cmd in "Zz":
                pts.append(start)
                continue
        def n(k: int) -> float:
            return float(parts[i + k])
        if cmd in "Mm":
            x, y = (n(0), n(1)) if cmd == "M" else (x + n(0), y + n(1))
            start = (x, y)
            pts.append((x, y))
            i += 2
            cmd = "L" if cmd == "M" else "l"
        elif cmd in "Ll":
            x, y = (n(0), n(1)) if cmd == "L" else (x + n(0), y + n(1))
            pts.append((x, y))
            i += 2
        elif cmd in "Hh":
            x = n(0) if cmd == "H" else x + n(0)
            pts.append((x, y))
            i += 1
        elif cmd in "Vv":
            y = n(0) if cmd == "V" else y + n(0)
            pts.append((x, y))
            i += 1
        elif cmd in "Cc":
            if cmd == "C":
                x1, y1, x2, y2, x3, y3 = (n(k) for k in range(6))
            else:
                x1, y1 = x + n(0), y + n(1)
                x2, y2 = x + n(2), y + n(3)
                x3, y3 = x + n(4), y + n(5)
            x0, y0 = x, y
            for s in range(1, steps + 1):
                t = s / steps
                u = 1 - t
                pts.append((
                    u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                    u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
                ))
            x, y = x3, y3
            i += 6
        else:
            i += 1
    return pts


def render(size: int, poly: list[tuple[float, float]], ss: int = 8) -> Image.Image:
    big = size * ss
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.18), fill=PANEL)
    inset = big * 0.10
    k = (big - inset * 2) / 32.0
    d.polygon([(inset + px * k, inset + py * k) for px, py in poly], fill=PLANE)
    return im.resize((size, size), Image.LANCZOS)


def main() -> None:
    poly = flatten(PATH)
    frames = [render(s, poly) for s in SIZES]
    out = Path(__file__).parent / "kneeboard.ico"
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {out} with sizes {SIZES}")


if __name__ == "__main__":
    main()
