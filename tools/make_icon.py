"""Draw the app icon (a focus reticle) and write src/omni_autofocus/assets/icon.ico. Stdlib only."""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "omni_autofocus" / "assets" / "icon.ico"
BG = (24, 89, 160)
FG = (255, 255, 255)
ACCENT = (255, 196, 61)
SS = 4  # supersampling per axis


def coverage(x: float, y: float) -> tuple[float, tuple[int, int, int]] | None:
    """Colour at a point of the unit square (0..1), or None for transparent."""
    r = 0.2  # corner radius of the rounded square
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return None
    d = math.hypot(x - 0.5, y - 0.5)
    if d < 0.07:
        return 1.0, ACCENT  # centre dot
    if 0.27 < d < 0.33:
        return 1.0, FG  # ring
    for ax, ay in ((x, y), (y, x)):  # four ticks crossing the ring
        if abs(ax - 0.5) < 0.035 and (0.12 < ay < 0.36 or 0.64 < ay < 0.88):
            return 1.0, FG
    return 1.0, BG


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            alpha = 0
            for sy in range(SS):
                for sx in range(SS):
                    c = coverage((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c:
                        alpha += 1
                        for i in range(3):
                            acc[i] += c[1][i]
            if alpha:
                row += bytes(int(v / alpha) for v in acc) + bytes([alpha * 255 // (SS * SS)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [render(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, img in zip(sizes, images, strict=True):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    OUT.write_bytes(header + entries + b"".join(images))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
