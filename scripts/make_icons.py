"""Render the OpenMyMemo logo (web/public/favicon.svg) to PNGs without any imaging library.

    uv run python scripts/make_icons.py

The logo is a dark rounded square with three amber lines, defined on a 32x32 grid.
"""

import struct
import zlib
from pathlib import Path

BG = (28, 25, 23)       # stone-900
FG = (251, 191, 36)     # amber-400
LINES = [(9, 11.5, 23), (9, 16, 23), (9, 20.5, 18)]  # x0, y, x1 on the 32 grid
STROKE = 2.4
RADIUS = 8
OUT = Path(__file__).resolve().parent.parent / "integrations" / "icons"


def coverage(x: float, y: float) -> tuple[float, float]:
    """(in_square, on_line) for a point on the 32x32 grid."""
    cx = min(max(x, RADIUS), 32 - RADIUS)
    cy = min(max(y, RADIUS), 32 - RADIUS)
    in_square = (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2 and 0 <= x <= 32 and 0 <= y <= 32
    on_line = False
    for x0, ly, x1 in LINES:
        px = min(max(x, x0), x1)
        if (x - px) ** 2 + (y - ly) ** 2 <= (STROKE / 2) ** 2:
            on_line = True
            break
    return in_square, on_line


def render(size: int, samples: int = 4) -> bytes:
    rows = []
    step = 32 / size
    for j in range(size):
        row = bytearray([0])  # filter type 0
        for i in range(size):
            sq = ln = 0
            for sj in range(samples):
                for si in range(samples):
                    x = (i + (si + 0.5) / samples) * step
                    y = (j + (sj + 0.5) / samples) * step
                    a, b = coverage(x, y)
                    sq += a
                    ln += a and b
            n = samples * samples
            alpha = sq / n
            mix = ln / sq if sq else 0
            rgb = [round(BG[k] * (1 - mix) + FG[k] * mix) for k in range(3)]
            row += bytes([*rgb, round(alpha * 255)])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128, 512):
        (OUT / f"icon-{size}.png").write_bytes(render(size))
        print(f"wrote {OUT / f'icon-{size}.png'}")
