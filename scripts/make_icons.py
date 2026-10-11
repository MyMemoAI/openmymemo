"""Render the app icons and the panel logo from the MyMemo logo.

    uv run --with pillow python scripts/make_icons.py <square logo, at least 512 px>

The source is MyMemo's square app icon (the gradient "M" on black, 1024x1024, full bleed). This rounds the
corners the way mymemo.ai's favicon does and writes integrations/icons/icon-*.png and web/public/logo.png.
For the Mac app it also writes desktop/src-tauri/icons/app-icon.png (the rounded icon inside the margin
macOS app icons leave; `pnpm tauri icon src-tauri/icons/app-icon.png -o src-tauri/icons` in desktop/ turns
it into the .icns), the menu-bar template icon tray.png (the rounded square in black with the "M" cut out)
and the setup page's desktop/splash/icon.png. Pillow is only needed here, so it isn't a project dependency.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RADIUS = 0.22    # corner radius as a share of the side, matching mymemo.ai's favicon
SUPERSAMPLE = 4  # draw the corner mask larger, then scale it down, for smooth edges
MAC_SQUARE = 824 / 1024  # the share of a macOS app icon the rounded square fills
DESKTOP = ROOT / "desktop"


def rounded(src: Image.Image, size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * RADIUS), fill=255)
    icon = src.convert("RGB").resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    icon.putalpha(mask.resize((size, size), Image.Resampling.LANCZOS))
    return icon


def mac_app_icon(src: Image.Image, size: int = 1024) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = round(size * MAC_SQUARE)
    canvas.alpha_composite(rounded(src, inner), ((size - inner) // 2, (size - inner) // 2))
    return canvas


def tray_icon(src: Image.Image, size: int = 36, scale: float = 0.9) -> Image.Image:
    """A template image for the menu bar: only alpha counts, so the bright "M" becomes a hole."""
    inner = round(size * scale)
    square = rounded(src, inner).getchannel("A")
    letter = src.convert("L").resize((inner, inner), Image.Resampling.LANCZOS).point(lambda v: min(255, v * 3))
    alpha = Image.new("L", (size, size), 0)
    alpha.paste(Image.composite(Image.new("L", (inner, inner), 0), square, letter), ((size - inner) // 2,) * 2)
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.putalpha(alpha)
    return icon


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Image.open(sys.argv[1])
    if src.width != src.height or src.width < 512:
        sys.exit("The logo must be square and at least 512 px.")
    targets = {ROOT / "integrations" / "icons" / f"icon-{s}.png": s for s in (16, 32, 48, 128, 512)}
    targets[ROOT / "web" / "public" / "logo.png"] = 256
    targets[DESKTOP / "splash" / "icon.png"] = 256
    for path, size in targets.items():
        rounded(src, size).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")
    for path, icon in ((DESKTOP / "src-tauri" / "icons" / "app-icon.png", mac_app_icon(src)),
                       (DESKTOP / "src-tauri" / "icons" / "tray.png", tray_icon(src))):
        icon.save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")
