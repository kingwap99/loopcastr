#!/usr/bin/env python3
"""把一段文字（通常是網址）畫成 QR Code PNG。

為什麼要自己寫：.41 沒有 qrencode、沒有 PIL、ffmpeg 也沒有 freetype。所以
只裝純 Python 的 qrcode 套件拿矩陣，PNG 自己用 zlib + struct 寫出來。

用法
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
    ap.add_argument("--scale", type=int, default=10, help="每個模組幾像素")
    ap.add_argument("--border", type=int, default=3, help="靜區寬度（模組數）")
    ap.add_argument("--invert", action="store_true", help="黑底白碼")
    a = ap.parse_args()

    try:
        import qrcode
    except ImportError:
        print("缺少 qrcode 套件：python3 -m pip install --break-system-packages qrcode",
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
            # 逐模組水平放大：是「一個模組重複 s 次」，不是「整列重複 s 次」。
            # 後者會把圖橫向平鋪，QR 就解不出來了（實測踩過）。
            px += (b"\x00\x00\x00\xff" if dark else b"\xff\xff\xff\xff") * s
        for _ in range(s):           # 垂直放大：同一列重複 s 次
            rows.append(bytearray(px))

    write_png(a.out, n * s, n * s, rows)
    print("%s -> %s（%dx%d，模組 %d，scale %d）"
          % (a.data, a.out, n * s, n * s, n, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
