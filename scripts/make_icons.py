"""Render the app icons and the panel logo from the MyMemo logo.

    uv run --with pillow python scripts/make_icons.py <square logo, at least 512 px>

The source is MyMemo's square app icon (the gradient "M" on black, 1024x1024, full bleed). This rounds the
corners the way mymemo.ai's favicon does and writes integrations/icons/icon-*.png and web/public/logo.png.
Pillow is only needed here, so it isn't a project dependency.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RADIUS = 0.22    # corner radius as a share of the side, matching mymemo.ai's favicon
SUPERSAMPLE = 4  # draw the corner mask larger, then scale it down, for smooth edges


def rounded(src: Image.Image, size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * RADIUS), fill=255)
    icon = src.convert("RGB").resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    icon.putalpha(mask.resize((size, size), Image.Resampling.LANCZOS))
    return icon


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Image.open(sys.argv[1])
    if src.width != src.height or src.width < 512:
        sys.exit("The logo must be square and at least 512 px.")
    targets = {ROOT / "integrations" / "icons" / f"icon-{s}.png": s for s in (16, 32, 48, 128, 512)}
    targets[ROOT / "web" / "public" / "logo.png"] = 256
    for path, size in targets.items():
        rounded(src, size).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")
