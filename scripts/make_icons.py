"""Render the app icons and the panel logo from the OpenMyMemo logo.

    uv run --with pillow --with resvg-py python scripts/make_icons.py

The source is integrations/icons/logo.svg: the black "M" shield on a transparent 36x36 canvas. Drawn black on a
white rounded square (the white background sets it apart from MyMemo's black icon), it becomes
integrations/icons/icon-*.png and web/public/logo.png. For the Mac app it also writes
desktop/src-tauri/icons/app-icon.png (the rounded icon inside the margin macOS app icons leave;
`pnpm tauri icon src-tauri/icons/app-icon.png -o src-tauri/icons` in desktop/ turns it into the .icns, the .ico
and the other sizes), the menu-bar template icon tray.png (the shield alone) and the setup page's
desktop/splash/icon.png. Pillow and resvg-py are only needed here, so they aren't project dependencies.
"""

import io
from pathlib import Path

import resvg_py
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "integrations" / "icons" / "logo.svg"
RADIUS = 0.22    # corner radius as a share of the side, matching mymemo.ai's favicon
EDGE = 1 / 48    # a faint outline, so the white square still shows on a white page
SUPERSAMPLE = 4  # draw the square larger, then scale it down, for smooth edges
MAC_SQUARE = 824 / 1024  # the share of a macOS app icon the rounded square fills
DESKTOP = ROOT / "desktop"


def glyph(size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_path=str(LOGO), width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def rounded(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    square = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(square).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=round(big * RADIUS), fill=(255, 255, 255, 255),
        outline=(0, 0, 0, 24), width=max(SUPERSAMPLE, round(big * EDGE)))
    icon = square.resize((size, size), Image.Resampling.LANCZOS)
    icon.alpha_composite(glyph(size))
    return icon


def mac_app_icon(size: int = 1024) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = round(size * MAC_SQUARE)
    canvas.alpha_composite(rounded(inner), ((size - inner) // 2, (size - inner) // 2))
    return canvas


def tray_icon(size: int = 36) -> Image.Image:
    """A template image for the menu bar: only alpha counts, and the logo is drawn on this 36 px grid."""
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.putalpha(glyph(size).getchannel("A"))
    return icon


if __name__ == "__main__":
    targets = {ROOT / "integrations" / "icons" / f"icon-{s}.png": s for s in (16, 32, 48, 128, 512)}
    targets[ROOT / "web" / "public" / "logo.png"] = 256
    targets[DESKTOP / "splash" / "icon.png"] = 256
    for path, size in targets.items():
        rounded(size).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")
    for path, icon in ((DESKTOP / "src-tauri" / "icons" / "app-icon.png", mac_app_icon()),
                       (DESKTOP / "src-tauri" / "icons" / "tray.png", tray_icon())):
        icon.save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")
