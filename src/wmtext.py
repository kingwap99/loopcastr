#!/usr/bin/env python3
"""Render text as white-on-black-outline PNGs with a transparent background, for overlay.

PIL plus a system CJK font (STHeiti) is preferred, so CJK text can be drawn; the shipped
default captions are CJK. Only when PIL or the fonts are unavailable does it fall back to
the built-in 5x7 bitmap font, which covers digits and - / . and space only and cannot draw
CJK text.

Usage
  from wmtext import render
  render("First aired: 2026-09-03", "/tmp/date.png", size=44, stroke=4)
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

# ── Bitmap fallback font (cannot draw CJK; used only when PIL is missing) ──
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
        raise ValueError("the bitmap font cannot draw these characters: %r" % text)
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
    """Draw the per-second countdown as one tall strip; return (tile width, height).

    Why not one file per second: a 1 fps sequence input does not line up with the 30 fps
    main picture inside overlay, the whole layer disappears, and ffmpeg reports no error at
    all (measured). One long strip plus a time-based crop instead reuses the mechanism
    already proven by the marquee.
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("no usable CJK font was found")
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
    """Draw a small rounded badge (dark translucent panel, white text), such as the
    remaining-time countdown. Return (width, height)."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("no usable CJK font was found")
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
    """Draw a link button: the QR, its caption and the short URL stacked in a rounded panel.

    The live picture itself cannot be clicked (video pixels are not UI), so this is a visual
    hint: viewers scan the QR or type the short URL. A genuinely clickable link has to come
    from the YouTube description or chat.

    With caption_above=True the caption sits above the QR (that is how the transition caption
    is used); otherwise it sits below.
    """
    from PIL import Image
    import qrcode

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
    short = url.replace("https://", "").replace("http://", "") if show_url else ""
    return _button_panel(qr, path, caption, size, qr_px, pad, radius, gap,
                         caption_above=caption_above, url_text=short)


def render_image_button(image_path, path, caption="贊助", size=30, qr_px=120,
                        pad=16, radius=18, gap=14, caption_above=False):
    """Draw the operator's own QR picture in the same panel a link button uses.

    This is how the sponsor QR works: an operator can supply a branded QR image (from their
    payment provider, or a picture of their own) instead of letting this program generate a
    plain QR from a payment link. The picture is scaled to fit qr_px, centred and never cropped.
    """
    from PIL import Image

    src = Image.open(image_path).convert("RGBA")
    scale = min(float(qr_px) / src.width, float(qr_px) / src.height)
    w = max(1, int(round(src.width * scale)))
    h = max(1, int(round(src.height * scale)))
    box = Image.new("RGBA", (qr_px, qr_px), (0, 0, 0, 0))
    box.alpha_composite(src.resize((w, h), Image.LANCZOS),
                        ((qr_px - w) // 2, (qr_px - h) // 2))
    return _button_panel(box, path, caption, size, qr_px, pad, radius, gap,
                         caption_above=caption_above)


def _button_panel(inner, path, caption, size, inner_px, pad, radius, gap,
                  caption_above=False, url_text=""):
    """Draw the rounded panel shared by render_link_button and render_image_button.

    ``inner`` is already the picture that belongs in the panel (a generated QR or the
    operator's own QR image) at ``inner_px`` square, and ``url_text`` is optional text under
    the caption.
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        raise RuntimeError("no usable CJK font was found")
    f1 = ImageFont.truetype(font_path, size)
    f2 = ImageFont.truetype(font_path, int(size * 0.75))

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    cap_w = probe.textbbox((0, 0), caption, font=f1)[2]
    url_w = probe.textbbox((0, 0), url_text, font=f2)[2] if url_text else 0
    cap_h = int(size * 1.30)
    url_h = int(size * 0.75 * 1.30)

    inner_w = max(inner_px, cap_w, url_w)
    W = inner_w + pad * 2
    top_text_h = cap_h if caption_above else 0
    bot_text_h = (url_h if url_text else 0) + (0 if caption_above else cap_h)
    H = (pad + top_text_h + (gap if top_text_h else 0) + inner_px
         + (gap if bot_text_h else 0) + bot_text_h + pad)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius,
                        fill=(0, 0, 0, 175), outline=(255, 255, 255, 235),
                        width=3)

    ty = pad
    if caption_above:
        d.text(((W - cap_w) // 2, ty), caption, font=f1,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))
        ty += cap_h + gap
    img.alpha_composite(inner, ((W - inner_px) // 2, ty))
    ty += inner_px + (gap if bot_text_h else 0)
    if not caption_above:
        d.text(((W - cap_w) // 2, ty), caption, font=f1,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))
        ty += cap_h
    if url_text:
        d.text(((W - url_w) // 2, ty), url_text, font=f2,
               fill=(255, 255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0, 255))

    img.save(path)
    return img.size


def render_marquee_strip(text, path, tile_gap=220, size=44, stroke=4,
                         copies=2, pad=6):
    """Build a seamlessly scrollable strip of text. Return (strip width, height, one period).

    Why not simply move the text: clamping the start position is not enough - as the text
    scrolls out to the left it still paints over whatever is there (such as the original
    video's top-left logo). With a wide strip plus a fixed crop window the text can never be
    drawn outside the window, and avoiding a region is just a matter of moving the window.

    The text repeats `copies` times along the strip with `tile_gap` between repeats, so taking
    the crop window's x modulo the period (text width + gap) scrolls forever with no blank
    frames.
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
    """Draw text and return (width, height). Uses PIL plus a CJK font when available,
    otherwise the bitmap font."""
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
