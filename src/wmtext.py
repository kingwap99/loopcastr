#!/usr/bin/env python3
"""把文字畫成「白字黑邊、背景透明」的 PNG，給 ffmpeg overlay 用。

優先走 PIL ＋ 系統中文字型（STHeiti），所以可以畫中文（例如「首播日期：」）。
只有在 PIL 或字型都拿不到時，才退回內建的 5x7 點陣字型 —— 那組只涵蓋數字
與 - / . 空白，畫不出中文。

用法
  from wmtext import render
  render("首播日期：2026-09-03", "/tmp/date.png", size=44, stroke=4)
"""

import os
import struct
import zlib

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]

# ── 點陣後備字型（畫不出中文，只在沒有 PIL 時用）────────────────────
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
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "/": ["00001", "00001", "00010", "00100", "01000", "10000", "10000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}


def _write_png(path, w, h, rows):
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


def _render_bitmap(text, path, scale=6, outline=2):
    glyphs = [FONT[c] for c in text if c in FONT]
    if not glyphs:
        raise ValueError("點陣字型畫不出這些字元：%r" % text)
    tw, th = len(glyphs) * 6 - 1, 7
    W, H = tw + 2 * outline, th + 2 * outline
    src = [[0] * W for _ in range(H)]
    for gi, g in enumerate(glyphs):
        x0 = outline + gi * 6
        for y in range(7):
            for x in range(5):
                if g[y][x] == "1":
                    src[outline + y][x0 + x] = 1
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            if src[y][x]:
                row += b"\xff\xff\xff\xff" * scale
                continue
            near = False
            for dy in range(-outline, outline + 1):
                yy = y + dy
                if 0 <= yy < H:
                    for dx in range(-outline, outline + 1):
                        xx = x + dx
                        if 0 <= xx < W and src[yy][xx]:
                            near = True
                            break
                if near:
                    break
            row += (b"\x00\x00\x00\xff" if near else b"\x00\x00\x00\x00") * scale
        for _ in range(scale):
            rows.append(bytearray(row))
    _write_png(path, W * scale, H * scale, rows)
    return W * scale, H * scale


def render_countdown_strip(prefix, total, path, size=28, pad=12, radius=12,
                            stroke=3, pre="剩餘 ", suf=""):
    """把每一秒的倒數畫成「一張直條圖」，回傳 (單張寬, 單張高)。

    為什麼不是一秒一個檔案的序列：1 fps 的序列輸入跟 30 fps 的主畫面在
    overlay 裡對不起來，整層會完全不見，而且 ffmpeg 不會報任何錯（實測）。
    改成一張長條 ＋ 時間裁切，就跟跑馬燈用的是同一套已驗證可行的機制。
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("找不到可用的中文字型")
    f = ImageFont.truetype(font_path, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    n = int(total)
    texts = ["%s%s%02d:%02d%s" % (prefix, pre, r // 60, r % 60, suf)
             for r in range(n, 0, -1)]
    boxes = [probe.textbbox((0, 0), t, font=f, stroke_width=stroke)
             for t in texts]
    bw = max(b[2] - b[0] for b in boxes) + pad * 2
    bh = max(b[3] - b[1] for b in boxes) + pad * 2
    img = Image.new("RGBA", (bw, bh * n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k, (txt, box) in enumerate(zip(texts, boxes)):
        y0 = k * bh
        d.rounded_rectangle([0, y0, bw - 1, y0 + bh - 1], radius=radius,
                            fill=(0, 0, 0, 175),
                            outline=(255, 255, 255, 235), width=2)
        d.text((((bw - (box[2] - box[0])) // 2) - box[0], y0 + pad - box[1]),
               txt, font=f, fill=(255, 255, 255, 255), stroke_width=stroke,
               stroke_fill=(0, 0, 0, 255))
    img.save(path)
    return bw, bh


def render_badge(text, path, size=28, pad=12, radius=12, stroke=3):
    """畫一個圓角小標籤（深色半透明底 ＋ 白字），例如剩餘時間的倒數。回傳 (寬, 高)。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("找不到可用的中文字型")
    f = ImageFont.truetype(font_path, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    box = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    tw, th = box[2] - box[0], box[3] - box[1]
    W, H = tw + pad * 2, th + pad * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius,
                        fill=(0, 0, 0, 175), outline=(255, 255, 255, 235),
                        width=2)
    d.text((pad - box[0], pad - box[1]), text, font=f,
           fill=(255, 255, 255, 255), stroke_width=stroke,
           stroke_fill=(0, 0, 0, 255))
    img.save(path)
    return W, H


def render_link_button(url, path, caption="▶ 看原片", size=30,
                       qr_px=120, pad=16, radius=18, gap=14,
                       caption_above=False, show_url=True):
    """畫一個「連結按鈕」：QR 與說明、短網址垂直堆疊，外框是圓角半透明面板。

    直播畫面本身不能被點擊（影片像素不是 UI），所以這是視覺提示：觀眾可以
    掃 QR 或照著打短網址。真正可點的連結要靠 YouTube 的說明欄或聊天室。

    caption_above=True 時說明放在 QR 上方（過場的「追劇去」就是這樣用），
    否則說明在 QR 下方。
    """
    from PIL import Image, ImageDraw, ImageFont
    import qrcode

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("找不到可用的中文字型")
    f1 = ImageFont.truetype(font_path, size)
    f2 = ImageFont.truetype(font_path, int(size * 0.75))

    short = url.replace("https://", "").replace("http://", "")
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    cap_w = probe.textbbox((0, 0), caption, font=f1)[2]
    url_w = probe.textbbox((0, 0), short, font=f2)[2] if show_url else 0
    cap_h = int(size * 1.30)
    url_h = int(size * 0.75 * 1.30)

    inner = max(qr_px, cap_w, url_w)
    W = inner + pad * 2
    top_text_h = cap_h if caption_above else 0
    bot_text_h = (url_h if show_url else 0) + (0 if caption_above else cap_h)
    H = pad + top_text_h + (gap if top_text_h else 0) + qr_px +        + (gap if bot_text_h else 0) + bot_text_h + pad

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius,
                        fill=(0, 0, 0, 175), outline=(255, 255, 255, 235),
                        width=3)

    q = qrcode.QRCode(border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(url)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    qr = Image.new("RGB", (n, n), (255, 255, 255))
    px = qr.load()
    for y in range(n):
        for x in range(n):
            if m[y][x]:
                px[x, y] = (0, 0, 0)
    qr = qr.resize((qr_px, qr_px), Image.NEAREST).convert("RGBA")
    ty = pad
    if caption_above:
        d.text(((W - cap_w) // 2, ty), caption, font=f1,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))
        ty += cap_h + gap
    img.alpha_composite(qr, ((W - qr_px) // 2, ty))
    ty += qr_px + (gap if bot_text_h else 0)
    if not caption_above:
        d.text(((W - cap_w) // 2, ty), caption, font=f1,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))
        ty += cap_h
    if show_url:
        d.text(((W - url_w) // 2, ty), short, font=f2,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))

    img.save(path)
    return img.size


def render_marquee_strip(text, path, tile_gap=220, size=44, stroke=4,
                         copies=2, pad=6):
    """把文字做成可無縫捲動的長條圖。回傳 (長條寬, 高, 單一週期寬)。

    為什麼不直接移動文字：光限制起始位置不夠 —— 文字往左捲出去時照樣會壓過
    左邊的東西（例如原片左上角的 logo）。改成「寬長條 ＋ 固定視窗裁切」之後，
    文字永遠不會畫到視窗外，要避開哪一塊就只要調整視窗位置。

    文字在長條上重複 copies 次、每次間隔 tile_gap，所以只要把裁切視窗的 x
    對週期（文字寬 ＋ 間隔）取模，就能無限捲動而不會出現空白。
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        return None
    f = ImageFont.truetype(font_path, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    box = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    tw = box[2] - box[0]
    th = box[3] - box[1]
    tile = tw + tile_gap
    W = tile * copies
    H = th + pad * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k in range(copies):
        d.text((k * tile - box[0], pad - box[1]), text, font=f,
               fill=(255, 255, 255, 255), stroke_width=stroke,
               stroke_fill=(0, 0, 0, 255))
    img.save(path)
    return W, H, tile


def render(text, path, size=44, stroke=4, scale=6, outline=2, pad=6):
    """畫出 text，回傳 (寬, 高)。有 PIL＋中文字型就用它，否則退回點陣。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _render_bitmap(text, path, scale, outline)

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        return _render_bitmap(text, path, scale, outline)

    try:
        f = ImageFont.truetype(font_path, size)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        box = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
        w = (box[2] - box[0]) + pad * 2
        h = (box[3] - box[1]) + pad * 2
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(img).text(
            (pad - box[0], pad - box[1]), text, font=f,
            fill=(255, 255, 255, 255), stroke_width=stroke,
            stroke_fill=(0, 0, 0, 255))
        img.save(path)
        return img.size
    except Exception:
        return _render_bitmap(text, path, scale, outline)
