#!/usr/bin/env python3
"""Render a piece of text (usually a URL) as a QR Code PNG.

Why it is written by hand: the target machine has no qrencode, no PIL and an ffmpeg
without freetype, so only the pure-Python qrcode package is installed to get the
matrix, and the PNG is written directly with zlib + struct.

Usage
  python3 make_qr_png.py "https://..." out.png --scale 10
"""

import argparse
import struct
import sys
import zlib


def write_png(path, w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("out")
    ap.add_argument("--scale", type=int, default=10, help="pixels per QR module")
    ap.add_argument("--border", type=int, default=3, help="quiet zone width (in modules)")
    ap.add_argument("--invert", action="store_true", help="white code on black")
    a = ap.parse_args()

    try:
        import qrcode
    except ImportError:
        print("the qrcode package is missing: python3 -m pip install --break-system-packages qrcode",
              file=sys.stderr)
        return 2

    q = qrcode.QRCode(border=a.border,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(a.data)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    s = a.scale

    rows = []
    for y in range(n):
        px = bytearray()
        for x in range(n):
            dark = bool(m[y][x])
            if a.invert:
                dark = not dark
            # Scale horizontally module by module: each module repeats s times, rather than
            # repeating the whole row s times. The latter tiles the image sideways and the
            # QR stops decoding (measured).
            px += (b"\x00\x00\x00\xff" if dark else b"\xff\xff\xff\xff") * s
        for _ in range(s):           # Scale vertically: repeat the same row s times
            rows.append(bytearray(px))

    write_png(a.out, n * s, n * s, rows)
    print("%s -> %s (%dx%d, %d modules, scale %d)"
          % (a.data, a.out, n * s, n * s, n, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
