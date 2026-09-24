#!/usr/bin/env python3
"""Build a dedicated transition clip for every episode: the bottom-right QR code points at the original URL of the episode that just finished.

Why one per episode: the transition exists so viewers can scan through to the video that just
finished, so the QR must hold that episode URL rather than the channel URL. 53 episodes means 53 transitions.

The source is media/_transition-clean.mp4 (the clean copy without a QR). Always start from it when
changing the QR; overlaying onto an already-overlaid copy gets muddier every time.

Usage
  python3 build_transitions.py                 # redo everything
  python3 build_transitions.py --limit 3       # trial run on the first 3 episodes
  python3 build_transitions.py --verify 5      # decode-verify 5 of them afterwards
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import build_local_content as blc      # reuse its normalisation so the parameters match
except ImportError:
    blc = None

import wmtext                              # draws the title bar and the QR button

HERE = os.path.dirname(os.path.abspath(__file__))
# Follow the configured library location (settings.json media.dir), like every other builder does.
MEDIA = blc.media_root() if blc else os.path.join(HERE, "media")
CLEAN = os.path.join(MEDIA, "_transition-clean.mp4")
TMP = "/tmp/wm"



FP_VERSION = 1          # bump when a change affects the transition picture, so old files are redone


def transition_fp(base_id, seconds, caption, title, air, sid, p, stride):
    """The parameter fingerprint of one transition: base, length, text, QR, sponsor... unchanged means no redo."""
    if blc is None:
        return ""
    import hashlib
    blob = json.dumps({
        "v": FP_VERSION, "base": os.path.basename(base_id or ""),
        "seconds": seconds, "caption": caption, "title": title, "air": air,
        "sid": sid, "pass": p, "stride": stride,
        "qr_size": blc.QR_SIZE, "qr_px": blc.QR_PX,
        "text_size": blc.TEXT_SIZE, "text_stroke": blc.TEXT_STROKE,
        "date_label": blc.DATE_LABEL,
        "tools": [bool(blc.wmtext), blc.have_qrcode()],
        "sponsor_url": blc.SPONSOR_URL, "sponsor_qr_image": blc.SPONSOR_QR_IMAGE,
        "sponsor_caption": blc.SPONSOR_CAPTION,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def load_manifest(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def deployed_ok(skip_dir, name, fp):
    """The deployed copy is still there and the fingerprint matches, so it can be skipped."""
    if not skip_dir or not fp:
        return False
    p = os.path.join(skip_dir, name)
    if not os.path.exists(p):
        return False
    rec = load_manifest(os.path.join(skip_dir, "transitions.json")).get(name) or {}
    return rec.get("fp") == fp and rec.get("bytes") == os.path.getsize(p)


def make_qr(url, path, scale=8, border=4):
    p = subprocess.run([sys.executable, os.path.join(HERE, "make_qr_png.py"),
                        url, path, "--scale", str(scale), "--border", str(border)],
                       capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
    return "" if p.returncode == 0 and os.path.exists(path) else (p.stderr or "")[:160]


def title_overlays(title, air, w=1280, tag="", band_left=0):
    """The title-bar overlay: static and right-aligned when it fits, otherwise a marquee (fixed crop window).

    A transition has plenty of room (a vertical short is pillarboxed), so band_left is 0 and no space
    is reserved for a logo; the episode bar does leave that space.
    """
    if blc is None or not air:
        return []
    span = w - band_left
    if span <= 200:
        return []
    text = (("%s　%s%s" % (title, blc.DATE_LABEL, air)) if title
            else ("%s%s" % (blc.DATE_LABEL, air)))
    label = os.path.join(TMP, "label-%s.png" % tag)
    ow, _ = wmtext.render(text, label, size=blc.TEXT_SIZE, stroke=blc.TEXT_STROKE)
    if ow > span:
        strip = os.path.join(TMP, "strip-%s.png" % tag)
        res = wmtext.render_marquee_strip(text, strip,
                                              tile_gap=blc.MARQUEE_GAP,
                                              size=blc.TEXT_SIZE, stroke=blc.TEXT_STROKE)
        if not res:
            return []
        sw, sh, tile = res
        pre = "crop=w=%d:h=%d:x='mod(t*%d,%d)':y=0" % (
            min(span, sw), sh, blc.MARQUEE_SPEED, tile)
        return [(strip, pre, "x=%d:y=%d" % (band_left, blc.MARQUEE_Y))]
    return [(label, None, "x=%d-w:y=%d" % (w, blc.OVERLAY_Y))]


def overlay(base, out, seconds, overlays):
    """Overlay several layers onto base. base can be the fixed transition or a rotating short.

    The length cap must be the smaller of the requested cap and the length of base itself: after
    overlaying with -loop 1 the stream never ends, so giving only a -t cap stretches a shorter base and
    freezes its last frame at the tail (measured: a 53-second short became 90 seconds).
    """
    base_d = blc.probe_seconds(base) or 0
    lim = int(min(seconds, base_d)) if base_d else int(seconds)
    return blc.normalize(base, out, 1280, 720, "libx264", "128k", 30,
                         overlays, lim)


SHORTS_FMT = ("bv*[height<=1080][protocol^=https]+ba[protocol^=https]/"
              "b[height<=1080][protocol^=https]/b[height<=1080]/b")

# Where yt-dlp lives: prefer whatever is on PATH (not everyone installs it with brew)
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"


def fetch_shorts(url, count):
    """The newest shorts, keeping the longest answer of a few attempts.

    The flat listing intermittently comes back short, and a short answer here shrinks the pool the
    transitions rotate through. build_playlist.py guards the video listing the same way.
    """
    best = []
    for i in range(3):
        p = subprocess.run([YTDLP, "--no-warnings",
                            "--socket-timeout", "25", "--flat-playlist",
                            "-I", "1:%d" % count, "--print", "%(id)s", url],
                           capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if len(ids) > len(best):
            best = ids
        elif best:
            print("listing attempt %d returned %d shorts; keeping %d" % (i + 1, len(ids), len(best)),
                  flush=True)
        if not best:
            time.sleep(2 + i * 2)
    return best


def land_short(sid, media_dir, raw_dir):
    """Download one short and normalise it to the same parameters as the other segments.

    Shorts are vertical (1080x1920 for example), so this scales them down proportionally and pads them
    into 1280x720 with black bars: shrink to fit, without cropping or distorting.
    """
    dst = os.path.join(media_dir, "short-%s.mp4" % sid)
    if os.path.exists(dst):
        return dst, ""
    os.makedirs(raw_dir, exist_ok=True)
    raw = os.path.join(raw_dir, "short-%s.mp4" % sid)
    p = subprocess.run([YTDLP, "--no-warnings",
                        "--no-playlist", "--retries", "5",
                        "--fragment-retries", "10", "-f", SHORTS_FMT,
                        "--merge-output-format", "mp4", "--remux-video", "mp4",
                        "-o", raw,
                        "https://www.youtube.com/watch?v=%s" % sid],
                       capture_output=True, text=True, timeout=1800,
                       stdin=subprocess.DEVNULL)
    if not os.path.exists(raw):
        return None, (p.stderr or "").strip()[-160:]
    err = blc.normalize(raw, dst, 1280, 720, "libx264", "128k", 30, None, 0)
    if err:
        return None, err
    try:
        os.remove(raw)
    except OSError:
        pass
    return dst, ""


def verify(path):
    """Decode a frame from the encoded file: merely having pixels on screen does not count."""
    try:
        import cv2
    except ImportError:
        return None
    subprocess.run(["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-nostdin",
                    "-loglevel", "error", "-y", "-ss", "10", "-i", path,
                    "-frames:v", "1", os.path.join(TMP, "_v.png")], check=True)
    img = cv2.imread(os.path.join(TMP, "_v.png"))
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--parallel", type=int,
                    default=int(blc.cfg("content", "transitions_parallel", 3)) if blc else 3)
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--verify", type=int, default=0,
                    help="how many transitions to decode-verify afterwards (0 = skip)")
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist-local.json"),
                    help="which list defines the episode order (default playlist-local.json)."
                         "point it at the master list when building transitions for a new list")
    ap.add_argument("--shorts-url", default="",
                    help="use a shorts carousel from this URL as the transitions (e.g. a channel's /shorts)."
                         "when empty, use the fixed _transition-clean.mp4")
    ap.add_argument("--shorts-count", type=int, default=30,
                    help="how many shorts to use as the carousel pool (default 30, max 50)")
    ap.add_argument("--button-caption",
                    default=(blc.TRANSITION_CAPTION if blc else "去追劇"),
                    help="caption on the transition QR button (episodes use link_caption); "
                         "default comes from settings.json overlay.transition_caption")
    ap.add_argument("--no-button", action="store_true",
                    help="do not overlay the QR button on transitions")
    # When changing transitions while on air, write to a staging directory first and mv them in after
    # verification: mv is atomic, so the playout (-c copy reopening files every loop) only ever sees a complete old or new file.
    ap.add_argument("--out-dir", default="",
                    help="output directory (default media/); a staging dir keeps the playout from reading half-written files")
    # The shorts rotate independently of the video passes: pass p, video i uses short
    # (p * episodes + i - 1) of the pool. So with 30 videos and 50 shorts, passes=2 makes the
    # second pass continue from short 31.
    ap.add_argument("--passes", type=int, default=1,
                    help="how many passes of videos per round (default 1) so the whole shorts pool gets used")
    ap.add_argument("--stride", type=int, default=0,
                    help="stride of the shorts rotation (default: the episode count of this round). A batched build must pass a fixed value,"
                         "otherwise a change in the episode count changes every transition fingerprint and redoes them all")
    ap.add_argument("--skip-dir", default="",
                    help="deployed directory; a transition whose fingerprint is unchanged is skipped (used by the batched build)")
    ap.add_argument("--force", action="store_true",
                    help="ignore fingerprints and redo everything")
    a = ap.parse_args()

    out_dir = a.out_dir or MEDIA
    os.makedirs(out_dir, exist_ok=True)

    # Same reasoning as build_local_content: a transition without its QR is not what was asked for.
    if not a.no_button and blc is not None and not blc.have_qrcode():
        print("ERROR: the transition QR button is enabled but this interpreter cannot import the "
              "qrcode package:", file=sys.stderr)
        print("  %s" % sys.executable, file=sys.stderr)
        print("  install it with: %s -m pip install --user qrcode" % sys.executable, file=sys.stderr)
        print("  (or pass --no-button to build transitions without QR codes)", file=sys.stderr)
        return 2

    if not os.path.exists(CLEAN) and not a.shorts_url:
        print("%s not found; keep a clean transition without a QR first" % CLEAN, file=sys.stderr)
        return 2

    d = json.load(open(a.playlist, encoding="utf-8"))
    eps = [s for s in d["segments"] if s["id"] != "_tr"]
    if a.limit:
        eps = eps[:a.limit]
    os.makedirs(TMP, exist_ok=True)

    # Transition base: one fixed clip, or a pool of shorts used in turn
    if a.shorts_url:
        n = min(a.shorts_count or 30, 50)
        ids = fetch_shorts(a.shorts_url, n)
        print("got %d shorts, downloading and normalizing..." % len(ids), flush=True)
        bases = []
        raw_dir = os.path.join(MEDIA, ".raw")
        for k, sid in enumerate(ids, 1):
            dst, err = land_short(sid, MEDIA, raw_dir)
            if dst:
                bases.append(dst)
                print("  short %2d/%d %-13s -> %s"
                      % (k, len(ids), sid, os.path.basename(dst)), flush=True)
            else:
                print("  short %-13s failed: %s" % (sid, err), flush=True)
        if not bases:
            print("no usable shorts, stopping", file=sys.stderr)
            return 3
    else:
        bases = [CLEAN]

    jobs = []
    skipped = 0
    for p in range(max(1, a.passes)):
        for i, seg in enumerate(eps, 1):
            sid = seg["id"]
            url = "https://youtu.be/%s" % sid
            # Name by video id, not by index: indices overwrite each other when the list changes,
            # and switching back to an old list would pick up the wrong QR. From pass 2 on add _pN, because the same video
            # pairs with a different short in each pass.
            name = "_tr_%s.mp4" % sid if p == 0 else "_tr_%s_p%d.mp4" % (sid, p + 1)
            out = os.path.join(out_dir, name)

            # QR: position and format are identical to an episode (top right, same panel and caption),
            # with no distinction. The button is built first because the marquee right edge depends on its width.
            btn = os.path.join(TMP, "qrb-%s.png" % sid)
            btn_w = 0
            if not a.no_button:
                kw = {"caption": a.button_caption} if a.button_caption else {}
                btn_w, _ = wmtext.render_link_button(url, btn, size=blc.QR_SIZE,
                                                 qr_px=blc.QR_PX,
                                                 **kw)

            # Title bar: right edge at the button left edge, left edge 0 (a vertical short is pillarboxed, so no logo space is needed)
            ov = title_overlays(seg.get("title") or "", seg.get("air_date") or "",
                                w=1280 - btn_w, tag=sid, band_left=0)
            if not a.no_button:
                ov.append((btn, None, "x=W-w:y=0"))
            if not a.no_button and (blc.SPONSOR_QR_IMAGE or blc.SPONSOR_URL):
                sb = os.path.join(TMP, "sponsor-%s.png" % sid)
                picture = blc.sponsor_image()
                if picture:
                    # The operator's own QR picture (or the one that ships with the project).
                    wmtext.render_image_button(picture, sb, size=blc.QR_SIZE,
                                               qr_px=blc.QR_PX,
                                               caption=blc.SPONSOR_CAPTION)
                else:
                    wmtext.render_link_button(blc.SPONSOR_URL, sb, size=blc.QR_SIZE,
                                              qr_px=blc.QR_PX,
                                              caption=blc.SPONSOR_CAPTION,
                                              show_url=False)
                ov.append((sb, None, "x=W-w:y=H-h"))

            # Continuous shorts rotation: pass p, video i uses pool entry (p * len(eps) + i - 1).
            # WARNING: these two lines used to be indented inside the has-a-sponsor-link if, so with no
            #   sponsor_url set no transition was built at all (measured on .22: the six concat segments were all videos and there were zero _tr_ files).
            # Stride: by default the episode count of this round; a batched build passes a fixed value (for example
            # the mode video_limit) so adding episodes midway does not redo every finished transition.
            stride = a.stride or len(eps)
            idx = p * stride + (i - 1)
            base = bases[idx % len(bases)]
            fp = transition_fp(base, a.seconds, a.button_caption,
                               seg.get("title") or "", seg.get("air_date") or "",
                               sid, p, stride)
            if not a.force and deployed_ok(a.skip_dir, name, fp):
                skipped += 1
                continue
            jobs.append({"i": i, "pass": p, "sid": sid, "url": url, "out": out,
                         "base": base, "ov": ov, "name": name, "fp": fp})

    print("building %d transitions (title bar + QR), %d skipped by fingerprint..."
          % (len(jobs), skipped), flush=True)
    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        futs = {ex.submit(overlay, j["base"], j["out"], a.seconds, j["ov"]): j
                for j in jobs}
        for f, j in list(futs.items()):
            err = f.result()
            if err:
                fail += 1
                print("FAIL %02d %-14s %s" % (j["i"], j["sid"], err), flush=True)
            else:
                done += 1
    print("encoding done OK=%d FAIL=%d in %.0f s" % (done, fail, time.time() - t0))

    # Record the fingerprint (with bytes): the next round skips when the file is still there with the same fingerprint and size.
    if a.skip_dir:
        mp = os.path.join(a.skip_dir, "transitions.json")
        rec = load_manifest(mp)
        for j in jobs:
            if os.path.exists(j["out"]):
                rec[j["name"]] = {"fp": j["fp"], "bytes": os.path.getsize(j["out"])}
        os.makedirs(a.skip_dir, exist_ok=True)
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)

    if a.verify and fail == 0:
        print("sample verification (decoded from the encoded files)...", flush=True)
        step = max(1, len(jobs) // a.verify)
        bad = 0
        for j in jobs[::step][:a.verify]:
            got = verify(j["out"])
            want = j["url"]
            ok = (got == want)
            if not ok:
                bad += 1
            print("  %02d %-14s %s  %r"
                  % (j["i"], j["sid"], "OK " if ok else "BAD", got))
        print("verification: %d/%d correct" % (a.verify - bad, a.verify))
        return 0 if bad == 0 else 1
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
