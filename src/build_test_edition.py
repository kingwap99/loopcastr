#!/usr/bin/env python3
"""產生 T-07 用的縮短測試版：每集剪成固定秒數，右上角燒上流水號。

為什麼要自己畫字：.41 兩台 ffmpeg 都沒有 freetype（沒有 drawtext），也沒有
PIL／ImageMagick，而 Chrome 無頭在「沒有圖形登入」的機器上產不出截圖。
所以這裡用純標準函式庫寫 PNG（zlib + struct）並自帶 5x7 點陣數字字型。

產出
  media-test/<NN>.mp4      每集前 N 秒，右上角有流水號
  media-test/_transition.mp4
  playlist-test.json       對應的播出清單（集數與過場交錯）

用法
  python3 build_test_edition.py                 # 預設每集 180 秒
  python3 build_test_edition.py --seconds 120
  python3 build_test_edition.py --limit 3       # 只做前 3 集（試跑）
"""

import argparse
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
OUTDIR = os.path.join(HERE, "media-test")
WMDIR = "/tmp/wm"
TRANSITION = os.path.join(MEDIA, "_transition.mp4")

FONT = {
    "0": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def write_png(path, w, h, rows):
    """rows: list of bytearray，每列 w*4 bytes（RGBA）。"""
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


def render_number(n, path, scale=16, outline=2):
    """把數字畫成白字黑邊、背景透明的 PNG。"""
    text = str(n)
    tw = len(text) * 6 - 1
    th = 7
    ob = outline
    W, H = tw + 2 * ob, th + 2 * ob

    src = [[0] * W for _ in range(H)]
    for gi, ch in enumerate(text):
        g = FONT.get(ch)
        if not g:
            continue
        x0 = ob + gi * 6
        for y in range(7):
            for x in range(5):
                if g[y][x] == "1":
                    src[ob + y][x0 + x] = 1

    # 黑邊：把每個亮點往外擴張，落在外圍的畫黑
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            if src[y][x]:
                row += b"\xff\xff\xff\xff"      # 白
                continue
            near = False
            for dy in range(-ob, ob + 1):
                yy = y + dy
                if yy < 0 or yy >= H:
                    continue
                for dx in range(-ob, ob + 1):
                    xx = x + dx
                    if 0 <= xx < W and src[yy][xx]:
                        near = True
                        break
                if near:
                    break
            row += b"\x00\x00\x00\xff" if near else b"\x00\x00\x00\x00"
        rows.append(row)

    # 放大
    big = []
    for row in rows:
        px = bytearray()
        for i in range(0, len(row), 4):
            px += bytes(row[i:i + 4]) * scale
        for _ in range(scale):
            big.append(px)
    write_png(path, W * scale, H * scale, big)
    return W * scale, H * scale


def encode(src, wm, out, seconds, w, h, fps):
    vf = ("[0:v]scale=w=%d:h=%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,format=yuv420p[v];"
          "[v][1:v]overlay=W-w-40:40[out]" % (w, h, w, h, fps))
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-i", src, "-i", wm, "-filter_complex", vf,
           "-map", "[out]", "-map", "0:a?", "-t", str(seconds),
           "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
           "-level", "3.1", "-g", str(fps * 2), "-b:v", "2500k",
           "-maxrate", "2500k", "-bufsize", "5000k",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0 or not os.path.exists(out):
        return (p.stderr or "").strip().splitlines()[-1][:160] if p.stderr else "unknown"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--target", default="720")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()

    sizes = {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)}
    w, h = sizes[a.target]

    pl = json.load(open(os.path.join(HERE, "playlist-local.json"),
                        encoding="utf-8"))
    eps = [s for s in pl["segments"] if s["id"] != "_tr"]
    if a.limit:
        eps = eps[:a.limit]

    os.makedirs(OUTDIR, exist_ok=True)
    os.makedirs(WMDIR, exist_ok=True)

    print("產生 %d 個浮水印 PNG…" % len(eps), flush=True)
    jobs = []
    for i in range(1, len(eps) + 1):
        p = os.path.join(WMDIR, "%02d.png" % i)
        render_number(i, p)
        jobs.append(p)
    print("浮水印完成", flush=True)

    todo = []
    for i, seg in enumerate(eps, 1):
        src = os.path.join(HERE, seg["path"])
        out = os.path.join(OUTDIR, "%02d.mp4" % i)
        todo.append((i, seg, src, jobs[i - 1], out))

    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        futs = {ex.submit(encode, src, wm, out, a.seconds, w, h, a.fps):
                (i, seg["id"], out) for (i, seg, src, wm, out) in todo}
        for f, (i, sid, out) in list(futs.items()):
            err = f.result()
            if err:
                fail += 1
                print("FAIL %02d %-14s %s" % (i, sid, err), flush=True)
            else:
                done += 1
                print("OK   %02d %-14s %s" % (i, sid, out), flush=True)
    print("編碼完成 OK=%d FAIL=%d，耗時 %.0f 秒" % (done, fail, time.time() - t0))

    # 組測試清單：集數與過場交錯
    segs = []
    for i, seg in enumerate(eps, 1):
        out = os.path.join(OUTDIR, "%02d.mp4" % i)
        if os.path.exists(out):
            segs.append({"id": "T%02d" % i, "type": "file",
                         "path": os.path.relpath(out, HERE),
                         "mode": "copy", "seconds": a.seconds})
            if os.path.exists(TRANSITION):
                segs.append({"id": "_tr", "type": "file",
                             "path": os.path.relpath(TRANSITION, HERE),
                             "mode": "copy",
                             "seconds": 20.0})
    local = {"target": pl.get("target"), "clients": pl.get("clients"),
             "generated_from": "playlist-local.json (test edition)",
             "segments": segs}
    with open(os.path.join(HERE, "playlist-test.json"), "w",
              encoding="utf-8") as fh:
        json.dump(local, fh, ensure_ascii=False, indent=2)
    total = sum(s["seconds"] for s in segs)
    print("playlist-test.json：%d 段、總長 %.0fs（%.2f 小時）"
          % (len(segs), total, total / 3600.0))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
