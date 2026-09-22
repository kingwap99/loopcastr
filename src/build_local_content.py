#!/usr/bin/env python3
"""Land the YouTube videos of playlist.json as local files and produce a playlist-local.json that plays offline.

Why landing is needed
  YouTube intermittently answers LOGIN_REQUIRED for anonymous player requests from this public IP
  (Sign in to confirm you are not a bot). Measured: a batch failed completely at 05:11 and the very
  same batch succeeded at 20:02 under identical conditions. So it is a temporary IP flag, not a
  permanent block. The only client that still worked during a block was player_client=android (capped
  at 360p; --no-fallback-client disables it). Because it recurs, a local copy is what makes 24/7 playout
  stable: besides avoiding the bot check it also removes the 6-hour source URL expiry and
  mid-stream googlevideo resets. The content is licensed work from the same group.

Why normalisation is needed
  The concat -c copy path requires every segment to have identical parameters. Measured, this content
  mixed 1280x720 with 1280x718 (and another batch at 480p), and putting them all through concat breaks.
  So landing converts to uniform parameters by default (--target 720) and the playout can then copy only.

Usage
  python3 build_local_content.py --status                # report what is still missing
  python3 build_local_content.py --limit 3               # land 3 as a trial
  python3 build_local_content.py --cookies cookies.txt   # when a login is needed
  python3 build_local_content.py --target 480            # output 480p instead
  python3 build_local_content.py --no-normalize          # keep the source parameters
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import wmtext                      # date watermark (the bitmap text module in this directory)
except ImportError:
    wmtext = None

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(HERE, "media")
RAW_DIR = os.path.join(MEDIA_DIR, ".raw")
MANIFEST = os.path.join(MEDIA_DIR, "manifest.json")

# ── Shared settings ─────────────────────────────────────────────────
# settings.json is the copy meant to be edited by hand (the WebUI edits it too). The values below are
# only defaults; command-line flags always override per run, and with no file present the behaviour is as if the mechanism did not exist.
SETTINGS_FILE = os.path.join(HERE, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


SETTINGS = load_settings()


def cfg(section, key, default):
    """Read section.key from settings.json; a missing or null value returns default."""
    try:
        v = SETTINGS[section][key]
    except (KeyError, TypeError):
        return default
    return default if v is None else v

# Landing at 720p is enough (the normalisation target is 720p); fetching 1080p only doubles the bandwidth.
# m3u8 must be excluded: measured, Ig3vtqtXowY over HLS only gives 137s while DASH gives the full 270s.
DEFAULT_FMT = ("bv*[height<=720][protocol^=https]+ba[protocol^=https]/"
               "b[height<=720][protocol^=https]/"
               "bv*[height<=720]+ba/b[height<=720]/b")
FALLBACK_FMT = "b[height<=360]/b"
FALLBACK_CLIENT = "android"

TARGETS = {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)}

BLACK_TAIL_MIN = float(cfg("content", "black_tail_min", 5.0))   # seconds of black at the tail that count as a black tail
BLACK_TAIL_SLACK = float(cfg("content", "black_tail_slack", 2.5))  # the black tail must end within this many seconds of the file end

# ── Language (console interface and on-screen captions) ─────────────
# zh = Chinese, en = English. Only one language is shown at a time (switchable at the top right of the console).
UI_LANG = str(cfg("ui", "lang", "zh")).strip().lower()


def L(zh, en):
    """Pick a string according to UI_LANG."""
    zh, en = (zh or ""), (en or "")
    return (en or zh) if UI_LANG == "en" else (zh or en)


DATE_LABEL = L(cfg("overlay", "date_label", "首播日期："),
               cfg("overlay", "date_label_en", "First aired: "))
LINK_CAPTION = L(cfg("overlay", "link_caption", "▶ 看原片"),
                 cfg("overlay", "link_caption_en", "▶ Watch original"))
TRANSITION_CAPTION = L(cfg("overlay", "transition_caption", "去追劇"),
                       cfg("overlay", "transition_caption_en", "Watch more"))
# Countdown wording: Chinese puts the prefix first and English puts the suffix last.
_CD_PRE, _CD_SUF = cfg("overlay", "countdown_prefix", "剩餘 "), cfg("overlay", "countdown_suffix", "")
_CD_PRE_EN = cfg("overlay", "countdown_prefix_en", "")
_CD_SUF_EN = cfg("overlay", "countdown_suffix_en", " left")
if UI_LANG == "en":
    CD_PRE, CD_SUF = _CD_PRE_EN, _CD_SUF_EN
else:
    CD_PRE, CD_SUF = _CD_PRE, _CD_SUF
AUDIO_FADE = float(cfg("media", "audio_fade", 2.5))       # fade in at the start / fade out at the end, in seconds
OVERLAY_Y = int(cfg("overlay", "overlay_y", 40))          # watermark distance from the top of the frame
MARQUEE_Y = OVERLAY_Y - 30                                # the marquee sits half a line higher
OVERLAY_MARGIN = int(cfg("overlay", "overlay_margin", 40))  # left and right margin
MARQUEE_SPEED = int(cfg("overlay", "marquee_speed", 120))   # marquee speed in pixels per second
MARQUEE_GAP = int(cfg("overlay", "marquee_gap", 220))       # blank gap between marquee repeats

# Encoding parameters. Bitrate is the knob that most directly affects picture quality and bandwidth, so it is not hard-coded in a command line.
VIDEO_PRESET = cfg("media", "preset", "veryfast")
VIDEO_LEVEL = cfg("media", "level", "3.1")
VIDEO_BITRATE = cfg("media", "video_bitrate", "2500k")
VIDEO_MAXRATE = cfg("media", "video_maxrate", "2500k")
VIDEO_BUFSIZE = cfg("media", "video_bufsize", "5000k")
AUDIO_RATE = int(cfg("media", "sample_rate", 48000))

# Sizes of the on-screen elements
TEXT_SIZE = int(cfg("overlay", "text_size", 44))
TEXT_STROKE = int(cfg("overlay", "text_stroke", 4))
QR_SIZE = int(cfg("overlay", "qr_size", 30))
QR_PX = int(cfg("overlay", "qr_px", 120))

# Sponsor/donation QR: drawn on transition clips only (never on episodes), always bottom right.
# The URL ships as a default so a fresh install shows the author's donation QR, and it is an
# ordinary setting: clear it to remove the QR, or untick overlay.sponsor_show to keep the URL
# but hide the QR. Only build_transitions.py draws it, so this never affects an episode file.
SPONSOR_URL = cfg("overlay", "sponsor_url", "")
SPONSOR_CAPTION = L(cfg("overlay", "sponsor_caption", "贊助"),
                    cfg("overlay", "sponsor_caption_en", "Support"))
if not cfg("overlay", "sponsor_show", True):
    SPONSOR_URL = ""

TRANSITION_FILE = os.path.join(MEDIA_DIR, "_transition.mp4")
TRANSITION_ID = "_tr"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def probe_seconds(path):
    """Measure the real duration with ffprobe. Scheduling depends on this, not on the length the source claims."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        if p.returncode == 0 and p.stdout.strip():
            return float(p.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return None


def check_duration(got, expect, tol=0.05, min_abs=10.0):
    """Compare the duration after downloading. Measured: some videos silently come down at half length (137s vs 270s),
    and without this check they go straight into concat and the broadcast jumps in the middle."""
    if not got or not expect:
        return None
    expect = float(expect)
    diff = abs(got - expect)
    if diff > max(min_abs, tol * expect):
        return "duration mismatch: got %.1fs, the list claims %.1fs" % (got, expect)
    return None


def fetch_raw(seg, cookies, fmt, client=None):
    """Fetch one video to media/.raw/<id>.mp4 and return (path, error message)."""
    sid = seg["id"]
    os.makedirs(RAW_DIR, exist_ok=True)
    url = seg.get("url") or ("https://www.youtube.com/watch?v=" + sid)
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist",
           "--sleep-requests", "1", "--retries", "5", "--fragment-retries", "10"]
    if cookies:
        cmd += ["--cookies", cookies]
    if client:
        cmd += ["--extractor-args", "youtube:player_client=" + client]
    cmd += ["-f", fmt, "--merge-output-format", "mp4", "--remux-video", "mp4",
            "-o", os.path.join(RAW_DIR, sid + ".%(ext)s"), url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                       stdin=subprocess.DEVNULL)
    raw = os.path.join(RAW_DIR, sid + ".mp4")
    if p.returncode != 0 or not os.path.exists(raw):
        tail = (p.stderr.strip().splitlines() or [""])[-1]
        return None, tail[:200]
    return raw, ""


def has_audio(path):
    """Whether the file has an audio track. Fades are only applied when there is one."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        return bool((p.stdout or "").strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def normalize(raw, out, w, h, venc, abr, fps, overlays=None, max_seconds=0,
              fade_sec=AUDIO_FADE):
    """Convert to uniform parameters so the playout can splice with plain -c copy.

    overlays is [(PNG path, pre-filter or None, overlay position expression), ...], applied in order.
    The pre-filter crops the image to a fixed window, which is what keeps the marquee from overflowing.
    A position expression containing a comma must be wrapped in single quotes, otherwise it is taken as a filter
    argument separator, which is exactly what the marquee trips over.
    With max_seconds > 0 only the first seconds are kept (used for the shortened test edition).
    """
    vf = ("scale=w=%d:h=%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,format=yuv420p"
          % (w, h, w, h, fps))
    if venc == "h264_videotoolbox":
        vargs = ["-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
                 "-profile:v", "high", "-g", str(fps * 2)]
    else:
        vargs = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-profile:v", "high",
                 "-level", VIDEO_LEVEL, "-g", str(fps * 2), "-b:v", VIDEO_BITRATE,
                 "-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE]
    # -nostdin is necessary: this script often runs inside an ssh heredoc, and if ffmpeg read
    # stdin it would treat the script text as interactive commands, quit on the first q and silently
    # truncate the output (measured: the same video came out at 137s and at 163s). stdin=DEVNULL is a second guard.
    tail = ["-c:a", "aac", "-b:a", abr, "-ar", str(AUDIO_RATE), "-ac", "2"]
    # The length cap must be the smaller of the cap and the source length, for two reasons:
    #   1) the -loop 1 / loop on the overlay never ends, so without a cap ffmpeg encodes forever
    #      (measured: the file grew without bound).
    #   2) even with a cap, if the cap is larger than the source length the image is still there while
    #      the main video has hit EOF, so overlay repeatlast pads the last frame to the cap: the video is
    #      stretched while the audio ends at the original length. Measured: a 209-second video became 450
    #      seconds with 13500 frames but only 209.1 seconds of audio. The playout -c copy stalls on that
    #      picture-without-sound tail, my watchdog kills it 20 seconds later and the viewer sees it jump
    #      back to the first video after the second one (measured on 2026-09-19, looping every 728 seconds).
    raw_d = int(probe_seconds(raw) or 0)
    if max_seconds and raw_d:
        max_seconds = min(int(max_seconds), int(raw_d + 0.999))
    elif overlays and not max_seconds and raw_d:
        max_seconds = int(raw_d + 0.999)
    if max_seconds:
        tail += ["-t", str(int(max_seconds))]

    # Audio fade in/out: at a video change both sides of the seam fade, so it does not cut off or pop.
    # Only audio is touched; video is still copied (or goes through the overlay chain above).
    dur = int(max_seconds) if max_seconds else (probe_seconds(raw) or 0)
    af = ""
    if fade_sec and dur > fade_sec * 2 + 1 and has_audio(raw):
        af = ("afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f"
              % (fade_sec, dur - fade_sec, fade_sec))
    # This used to pass -movflags +faststart. Measured (2026-09-19), it made ffmpeg randomly stall while
    # finishing: all the data written, no moov, 0% CPU, the main thread parked in sch_wait, and the same
    # video sometimes ran fine on a retry (a 60-second clip hits it too, so length is irrelevant; 4 times today). faststart exists for
    # progressive playback over the network, but our playout is local files + concat + -c copy and ffmpeg
    # reads moov from the end by itself, so it is not needed. Without it the same 60-second clip finished in 8.6 seconds.
    tail += [out]
    if overlays:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
               "-y", "-i", raw]
        for ov in overlays:
            if len(ov) > 3 and ov[3]:
                # One image per second (for the countdown): a 1 fps input, and overlay swaps frames by time
                cmd += ["-thread_queue_size", "512", "-framerate", "1",
                        "-start_number", "0", "-i", ov[0]]
            else:
                # The image input is a single frame and the repetition is left to the loop filter below, not -loop 1.
                # -loop 1 makes the demuxer resend a packet per frame, so every output frame decodes the whole
                # PNG again; the cost of the countdown strip (450 seconds = 266x25650 px) grows with the square
                # of the length (measured: the same 450-second clip took 197 seconds to encode). The loop filter decodes once
                # and repeats one frame, bringing the same clip down to 47 seconds (4.2x), with the same
                # visual result: it still emits frames with increasing timestamps and the crop time expression still works.
                #
                # -thread_queue_size fixes something else: when an infinite input is fed by the main thread, that
                # thread waiting on the filter graph (sch_wait) deadlocks with it, with the same symptoms: data all
                # written, no moov, 0% CPU, every other thread idle. Measured 3 out of 3 fine.
                cmd += ["-thread_queue_size", "512", "-framerate", str(fps),
                        "-i", ov[0]]
        parts = ["[0:v]%s[v0]" % vf]
        cur = "v0"
        for i, ov in enumerate(overlays, start=1):
            png, pre, pos = (list(ov) + [None, None, None])[:3]
            is_seq = len(ov) > 3 and ov[3]
            src = "%d:v" % i
            # A single-frame PNG is repeated with the loop filter (one decode); a 1 fps sequence input is finite anyway
            # and must not be looped (adding it freezes on the first frame).
            chain = "" if is_seq else "loop=loop=-1:size=1:start=0"
            if pre:
                chain = (chain + "," + pre) if chain else pre
            if chain:
                parts.append("[%s]%s[o%d]" % (src, chain, i))
                src = "o%d" % i
            nxt = "out" if i == len(overlays) else "v%d" % i
            parts.append("[%s][%s]overlay=%s[%s]" % (cur, src, pos, nxt))
            cur = nxt
        if af:
            parts.append("[0:a]%s[aout]" % af)
            maps = ["-map", "[out]", "-map", "[aout]"]
        else:
            maps = ["-map", "[out]", "-map", "0:a?"]
        cmd += ["-filter_complex", ";".join(parts)] + maps + vargs + tail
    else:
        cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-i", raw, "-vf", vf] + (["-af", af] if af else []) + vargs + tail)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0 or not os.path.exists(out):
        return (p.stderr.strip().splitlines() or [""])[-1][:200]
    return ""


def make_countdown_frames(total, out_dir, size=28, prefix=""):
    """Generate one countdown image per second (prefix plus remaining MM:SS), named with a 5-digit sequence number.

    Why one per second: text cannot be drawn onto the video directly (no freetype) and the countdown has to
    change over time. So every second is pre-rendered and handed to ffmpeg as a 1 fps sequence input,
    which makes overlay swap images by time.
    """
    n = int(total)
    os.makedirs(out_dir, exist_ok=True)
    for k in range(n):
        remain = n - k
        wmtext.render_badge("%s%s%02d:%02d%s"
                            % (prefix, CD_PRE, remain // 60, remain % 60, CD_SUF),
                            os.path.join(out_dir, "%05d.png" % k), size=size)
    return n


FP_VERSION = 1      # bump when a change affects the picture (watermark, filters, parameters) so old files are redone


def encode_fp(seg, args, w, h):
    """The encoding-parameter fingerprint of one video.

    Purpose: on a rebuild, if media/<mode>/<id>.mp4 is still there with the same fingerprint and size,
    skip it instead of re-encoding. It used to be not-in-staging-means-redo, so every build re-encoded
    the whole batch (measured: 55 news videos take hours).
    """
    import hashlib
    raw = os.path.join(RAW_DIR, seg["id"] + ".mp4")
    try:
        raw_id = "%d|%d" % (os.path.getsize(raw), int(os.path.getmtime(raw)))
    except OSError:
        raw_id = ""
    blob = json.dumps({
        "v": FP_VERSION, "raw": raw_id, "id": seg["id"],
        "wh": [w, h], "fps": args.fps, "venc": args.venc, "abr": args.abr,
        "no_norm": bool(args.no_normalize), "max": args.max_seconds,
        "fade": args.audio_fade,
        "no_date": bool(args.no_date_overlay), "no_link": bool(args.no_link_button),
        "no_cd": bool(args.no_countdown), "link_caption": args.link_caption,
        "band_left": args.band_left, "label": DATE_LABEL,
        "qr": [QR_SIZE, QR_PX], "txt": [TEXT_SIZE, TEXT_STROKE],
        "marquee": [MARQUEE_SPEED, MARQUEE_GAP], "y": OVERLAY_Y,
        "margin": OVERLAY_MARGIN, "cd": [CD_PRE, CD_SUF],
        # The sponsor QR is not drawn on episodes, so the sponsor settings are deliberately
        # NOT part of this fingerprint: toggling them must not re-encode every video. They are
        # in build_transitions.py's fingerprint instead, where they belong.
        "black": [args.black_tail_min, BLACK_TAIL_SLACK, bool(args.no_auto_trim)],
        "title": seg.get("title") or "", "air": seg.get("air_date") or "",
        "secs": seg.get("seconds"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def black_runs(path, min_len=BLACK_TAIL_MIN):
    """Return [(start, end, duration)]. Only key frames are decoded, so a pass takes about a second and is cheap."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
           "-skip_frame", "nokey", "-i", path,
           "-vf", "blackdetect=d=%.2f:pix_th=0.10" % min_len,
           "-an", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=900, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return []
    return [(float(a), float(b), float(c)) for a, b, c in re.findall(
        r"black_start:([0-9.]+) black_end:([0-9.]+) "
        r"black_duration:([0-9.]+)", p.stderr)]


def black_tail(path, seconds, min_len=BLACK_TAIL_MIN):
    """Seconds at which the black tail starts; None when there is no black tail.

    Why: measured, 8jtdcMDuV_A has 66 seconds of black at the tail. The playout cannot tell at all
    (the transfer is continuous and the timeline has no hole) but the viewer just sees black. Black frames are a content
    problem that only pixel inspection catches, so it is caught at landing time.
    """
    if not seconds:
        return None
    tails = [r for r in black_runs(path, min_len)
             if r[1] >= float(seconds) - BLACK_TAIL_SLACK]
    return min(r[0] for r in tails) if tails else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist.json"))
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--status", action="store_true", help="only report what is missing, do not download")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--limit", type=int, default=0, help="max videos to fetch this run (0 = no limit)")
    ap.add_argument("--format", default=DEFAULT_FMT, help="yt-dlp format selector")
    ap.add_argument("--no-fallback-client", action="store_true",
                    help="disable the android client fallback")
    ap.add_argument("--no-normalize", action="store_true",
                    help="skip normalization, keep the source parameters (concat will break)")
    ap.add_argument("--target", default=cfg("media", "target", "720"),
                    choices=sorted(TARGETS), help="normalization target resolution")
    ap.add_argument("--venc", default=cfg("media", "venc", "libx264"),
                    choices=["libx264", "h264_videotoolbox"])
    ap.add_argument("--abr", default=cfg("media", "abr", "128k"),
                    help="audio bitrate")
    ap.add_argument("--fps", type=int, default=int(cfg("media", "fps", 30)))
    ap.add_argument("--audio-fade", type=float, default=AUDIO_FADE,
                    help="audio fade in/out seconds per segment (default: settings.json)")
    ap.add_argument("--respect-playlist-format", action="store_true",
                    help="use the per-segment format from the list (default: ignore, use --format)")
    ap.add_argument("--rescan", action="store_true",
                    help="no fetching; rescan black tails of local files and rebuild the playout list")
    ap.add_argument("--black-tail-min", type=float, default=BLACK_TAIL_MIN,
                    help="seconds of black at the tail to count as a black tail (default 5.0)")
    ap.add_argument("--no-auto-trim", action="store_true",
                    help="only mark black tails, do not trim them")
    ap.add_argument("--transition", default="",
                    help="transition video (YouTube URL / video id / local file path)."
                         "kept once landed; add --force to fetch it again")
    ap.add_argument("--no-transition", action="store_true",
                    help="disable transitions even if the shared _transition.mp4 exists")
    ap.add_argument("--no-date-overlay", action="store_true",
                    help="do not burn in the first-air watermark (drawn top-right when the list has air_date)")
    ap.add_argument("--out-playlist", default=os.path.join(HERE, "playlist-local.json"),
                    help="output playout list path (default playlist-local.json)."
                         "point it elsewhere for a trial run so the live one is untouched")
    # When changing files while on air, write to a staging directory first and mv into media/ after verification:
    # mv is atomic, so the playout (reopening files every loop) only sees a complete old or new file. Pair it with --force.
    ap.add_argument("--out-dir", default="",
                    help="directory for normalized files (default media/)."
                         "for live replacement point it at a staging dir, then mv the verified files in")
    ap.add_argument("--max-seconds", type=int,
                    default=int(cfg("media", "max_seconds", 0)),
                    help="keep at most this many seconds per video (0 = full length); used for short test editions")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the downloaded originals (media/.raw/) so you can re-encode"
                         "without downloading again and without another quality loss")
    ap.add_argument("--no-link-button", action="store_true",
                    default=not bool(cfg("overlay", "link_button", True)),
                    help="do not overlay the link button (QR + short URL)")
    ap.add_argument("--no-countdown", action="store_true",
                    default=not bool(cfg("overlay", "countdown", True)),
                    help="do not show the remaining-time countdown under the QR")
    ap.add_argument("--link-caption", default=LINK_CAPTION,
                    help="caption on the episode link button (picked by ui.lang)")
    ap.add_argument("--band-left", type=int, default=int(cfg("overlay", "band_left", 0)),
                    help="marquee left bound in pixels (0 = use 1/7 of the width)."
                         "that space is reserved for the original video's top-left logo; use a larger value for more")
    # One copy of the content per mode: media/<mode>/. When the same video has a different length
    # cap per mode they cannot overwrite each other, and each keeps its own manifest (raw files in media/.raw are shared).
    ap.add_argument("--media-dir", default="",
                    help="content directory (default media/); use media/<mode> when several modes coexist")
    # A transition pairs video i with short i, so one round uses only the first N of the pool. --passes lets
    # a round contain several video passes so the shorts carry on (pass 2 starts at short 31).
    ap.add_argument("--passes", type=int, default=int(cfg("media", "passes", 1)),
                    help="how many passes of videos per round (default 1) so the whole shorts pool gets used")
    args = ap.parse_args()

    if args.media_dir:
        global MEDIA_DIR, MANIFEST, TRANSITION_FILE
        MEDIA_DIR = os.path.abspath(args.media_dir)
        MANIFEST = os.path.join(MEDIA_DIR, "manifest.json")
        TRANSITION_FILE = os.path.join(MEDIA_DIR, "_transition.mp4")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    pl = load_json(args.playlist, None)
    if not pl:
        raise SystemExit("playlist not found: %s" % args.playlist)

    cookies = args.cookies
    if cookies is None:
        auto = os.path.join(HERE, "cookies.txt")
        cookies = auto if os.path.exists(auto) else ""

    manifest = load_json(MANIFEST, {})
    segs = [s for s in pl["segments"] if s.get("type") in (None, "vod", "live")]

    missing = []
    skipped = 0
    for seg in segs:
        sid = seg["id"]
        staged = os.path.join(args.out_dir or MEDIA_DIR, sid + ".mp4")
        deployed = os.path.join(MEDIA_DIR, sid + ".mp4")
        rec = manifest.get(sid) or {}
        fp = encode_fp(seg, args, *(TARGETS.get(args.target) or (0, 0)))
        if args.force:
            missing.append(seg)
        elif args.out_dir and os.path.exists(staged):
            continue                      # already in staging, so resume instead of redoing it
        elif (os.path.exists(deployed) and rec.get("fp") == fp
              and rec.get("bytes") == os.path.getsize(deployed)):
            skipped += 1                  # deployed, parameters unchanged and size equal, so skip
            continue
        else:
            missing.append(seg)

    mode = "source parameters (not recommended)" if args.no_normalize else "%dx%d" % TARGETS[args.target]
    log("list has %d segments, %d present, %d missing (%d skipped by fingerprint); "
        "output format %s"
        % (len(segs), len(segs) - len(missing), len(missing), skipped, mode))

    if args.status:
        for seg in missing:
            log("  missing %s" % seg["id"])
        have = [m for m in manifest.values() if m.get("seconds")]
        total = sum(m.get("seconds") or 0 for m in have)
        log("manifest: %d segments measured, %.0fs total (%.2f h)"
            % (len(have), total, total / 3600.0))
        return 0 if not missing else 2

    if args.rescan:
        hits = 0
        for seg in segs:
            path = os.path.join(MEDIA_DIR, seg["id"] + ".mp4")
            if not os.path.exists(path):
                continue
            sec = probe_seconds(path)
            cut = black_tail(path, sec, args.black_tail_min)
            rec = manifest.get(seg["id"]) or {}
            rec["file"] = os.path.relpath(path, HERE)
            rec["seconds"] = round(sec, 3) if sec else None
            rec["bytes"] = os.path.getsize(path)
            if cut:
                rec["black_tail_start"] = round(cut, 3)
                rec["black_tail_len"] = round((sec or 0) - cut, 3)
                if not args.no_auto_trim:
                    rec["outpoint"] = round(cut, 3)
                hits += 1
                log("WARN %-13s black tail %.1fs (starts %.2fs)%s"
                    % (seg["id"], rec["black_tail_len"], cut,
                       "" if args.no_auto_trim else " -> trimmed"))
            else:
                for k in ("outpoint", "black_tail_start", "black_tail_len"):
                    rec.pop(k, None)
            manifest[seg["id"]] = rec
        save_json(MANIFEST, manifest)
        log("rescan done: %d segments scanned, %d with a black tail" % (len(segs), hits))
        missing = []

    if not missing:
        log("nothing to fetch; rebuilding the playout list")

    w, h = TARGETS[args.target]
    done = fail = 0
    # The video index in the master list (the countdown badge shows n of total)
    seg_ord = {s["id"]: i for i, s in enumerate(segs, 1)}
    for seg in missing[:args.limit or None]:
        sid = seg["id"]
        t0 = time.time()
        fmt = seg.get("format") if args.respect_playlist_format else args.format
        attempts = [(fmt or DEFAULT_FMT, None), (fmt or DEFAULT_FMT, None)]
        if not args.no_fallback_client:
            attempts.insert(1, (FALLBACK_FMT, FALLBACK_CLIENT))
        raw = err = bad = None
        kept = os.path.join(RAW_DIR, sid + ".mp4")
        if args.keep_raw and os.path.exists(kept):
            raw = kept
            log("     %s reusing the kept original (--keep-raw)" % sid)
        else:
            for (afmt, aclient) in attempts:
                tag = aclient or "default"
                raw, err = fetch_raw(seg, cookies, afmt, aclient)
                if not raw:
                    log("     %s [%s] download failed" % (sid, tag))
                    continue
                bad = check_duration(probe_seconds(raw), seg.get("seconds"))
                if not bad:
                    break
                log("     %s [%s] %s" % (sid, tag, bad))
                try:
                    os.remove(raw)
                except OSError:
                    pass
                raw = None
        if not raw:
            fail += 1
            log("FAIL %-13s %s" % (sid, bad or err))
            save_json(MANIFEST, manifest)
            continue

        out = os.path.join(args.out_dir or MEDIA_DIR, sid + ".mp4")
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
        if args.no_normalize:
            shutil.move(raw, out)
            nerr = ""
        else:
            overlays = []
            os.makedirs(RAW_DIR, exist_ok=True)

            # Build the link button first: it hugs the top right and takes a width, and the marquee range depends on it.
            btn_w = 0
            btn_h = 0
            if not args.no_link_button and wmtext:
                try:
                    btn = os.path.join(RAW_DIR, "btn-%s.png" % sid)
                    btn_w, btn_h = wmtext.render_link_button(
                        "https://youtu.be/%s" % sid, btn, size=QR_SIZE, qr_px=QR_PX,
                        caption=args.link_caption)
                    overlays.append((btn, None, "x=W-w:y=0"))
                except Exception as exc:
                    log("      %s link button failed, skipping the overlay: %s" % (sid, exc))
                    btn_w = btn_h = 0

            # Countdown: show the remaining playing time of this video under the QR button.
            # Overlay it as a one-image-per-second sequence and overlay swaps images by time.
            if btn_w and wmtext and not args.no_countdown:
                # The countdown strip length must also take the smaller value against the source length, or the picture
                # starts counting from 07:30 while the video ends after three and a half minutes.
                raw_d = probe_seconds(raw) or 0
                total = args.max_seconds or raw_d
                if args.max_seconds and raw_d:
                    total = min(args.max_seconds, raw_d)
                try:
                    if total >= 10:
                        prefix = "%02d/%02d　" % (seg_ord.get(sid, 0),
                                                  len(segs))
                        strip = os.path.join(RAW_DIR, "cd-%s.png" % sid)
                        bw, bh = wmtext.render_countdown_strip(
                            prefix, total, strip, pre=CD_PRE, suf=CD_SUF)
                        # Pick the current tile with a time-based crop (the same mechanism as the marquee).
                        # A 1 fps sequence input cannot be used: it does not line up with the 30 fps main picture
                        # inside overlay, the whole layer disappears and no error is reported (measured).
                        pre = ("crop=w=%d:h=%d:x=0:y='floor(t)*%d'"
                               % (bw, bh, bh))
                        overlays.append((strip, pre,
                                         "x=W-w:y=%d" % btn_h))
                        log("      %s countdown strip %dx%d (%d s, starts at '%s%s%02d:%02d%s')"
                            % (sid, bw, bh * int(total), int(total), prefix, CD_PRE,
                               int(total) // 60, int(total) % 60, CD_SUF))
                except Exception as exc:
                    log("      %s countdown failed, skipping the overlay: %s" % (sid, exc))

            # Visible range of the title bar: [left bound freed for the source logo, left edge of the button]
            # The left bound always frees 1/7 of the frame width for the source top-left logo (automatic detection
            # misjudges easily, so a fixed ratio is used; override it with --band-left in pixels)
            band_left = args.band_left if args.band_left > 0 else w // 7
            band_right = w - btn_w
            span = band_right - band_left
            air = seg.get("air_date")
            if air and not args.no_date_overlay and wmtext and span > 200:
                try:
                    text = "%s%s" % (DATE_LABEL, air)
                    title = (seg.get("title") or "").strip()
                    if title:
                        text = "%s　%s" % (title, text)
                    label = os.path.join(RAW_DIR, "label-%s.png" % sid)
                    ow, _ = wmtext.render(text, label, size=TEXT_SIZE, stroke=TEXT_STROKE)
                    if ow > span:
                        strip = os.path.join(RAW_DIR, "strip-%s.png" % sid)
                        res = wmtext.render_marquee_strip(
                            text, strip, tile_gap=MARQUEE_GAP, size=TEXT_SIZE,
                            stroke=TEXT_STROKE)
                        if not res:
                            raise RuntimeError("marquee strip generation failed")
                        sw, sh, tile = res
                        cw = min(span, sw)
                        pre = ("crop=w=%d:h=%d:x='mod(t*%d,%d)':y=0"
                               % (cw, sh, MARQUEE_SPEED, tile))
                        overlays.append((strip, pre,
                                         "x=%d:y=%d" % (band_left, MARQUEE_Y)))
                        log("      %s title %dpx > %dpx available, marquee (window "
                            "%d~%d, crop width %d)"
                            % (sid, ow, span, band_left, band_right, cw))
                    else:
                        overlays.append((label, None,
                                         "x=%d-w:y=%d"
                                         % (band_right, OVERLAY_Y)))
                except Exception as exc:
                    log("      %s watermark failed, skipping the overlay: %s" % (sid, exc))
            nerr = normalize(raw, out, w, h, args.venc, args.abr, args.fps,
                             overlays, args.max_seconds,
                             fade_sec=args.audio_fade)
            if not nerr and not args.keep_raw:
                try:
                    os.remove(raw)
                except OSError:
                    pass
        if nerr:
            fail += 1
            log("FAIL %-13s normalization failed: %s" % (sid, nerr))
            save_json(MANIFEST, manifest)
            continue

        done += 1
        seconds = probe_seconds(out)
        mrec = {
            "file": os.path.relpath(out, HERE),
            "seconds": round(seconds, 3) if seconds else None,
            "bytes": os.path.getsize(out),
            "fetched_at": int(time.time()),
            "target": "raw" if args.no_normalize else "%dx%d" % (w, h),
            # Encoding fingerprint: the next round compares this (plus the file size) to decide on a re-encode
            "fp": encode_fp(seg, args, w, h),
        }
        cut = black_tail(out, seconds, args.black_tail_min)
        if cut:
            mrec["black_tail_start"] = round(cut, 3)
            mrec["black_tail_len"] = round((seconds or 0) - cut, 3)
            if not args.no_auto_trim:
                mrec["outpoint"] = round(cut, 3)
            log("WARN %-13s black tail %.1fs (starts %.2fs)%s"
                % (sid, mrec["black_tail_len"], cut,
                   "" if args.no_auto_trim else " -> trimmed"))
        manifest[sid] = mrec
        save_json(MANIFEST, manifest)
        log("OK   %-13s %.1fs / %s  (%.1fs)"
            % (sid, seconds or -1, "%.1f MB" % (os.path.getsize(out) / 1e6),
               time.time() - t0))

    # Transition clip: --transition gives the source (URL / video id / local file).
    # Once landed it is not fetched again; --no-transition disables it.
    if args.transition and (args.force or not os.path.exists(TRANSITION_FILE)):
        src = os.path.expanduser(args.transition)
        raw = None
        if os.path.exists(src):
            raw = src
        else:
            if "://" not in src and not src.startswith("www."):
                src = "https://www.youtube.com/watch?v=" + src
            log("fetching the transition video...")
            raw, err = fetch_raw({"id": TRANSITION_ID, "url": src},
                                 cookies, args.format)
            if not raw:
                log("transition download failed: %s" % err)
        if raw and os.path.exists(raw):
            nerr = normalize(raw, TRANSITION_FILE, w, h, args.venc,
                             args.abr, args.fps, fade_sec=args.audio_fade)
            if nerr:
                log("transition normalization failed: %s" % nerr)
            else:
                if os.path.abspath(raw) != os.path.abspath(TRANSITION_FILE):
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
                log("transition ready: %.1fs" % (probe_seconds(TRANSITION_FILE) or -1))

    # Regenerate the offline playout list: only files really on disk with a readable duration are included.
    def transition_for(episode_id, p=0):
        """The transition that follows one episode.

        Prefer the per-episode media/_tr_<video id>.mp4, whose QR code points at the episode
        that just finished so viewers can scan through to it; fall back to the shared
        _transition.mp4 when there is no dedicated file. Video ids are used instead of indices so a list change cannot overwrite them.

        For p > 0 (multi-pass rotation) use _tr_<id>_p<N>.mp4: the same video pairs with a different
        short in each pass, so every pass needs its own transition file.
        """
        if args.no_transition:
            return None
        names = ["_tr_%s.mp4" % episode_id]
        if p:
            names.insert(0, "_tr_%s_p%d.mp4" % (episode_id, p + 1))
        path = ""
        for n in names:
            cand = os.path.join(MEDIA_DIR, n)
            if os.path.exists(cand):
                path = cand
                break
        if not path:
            path = TRANSITION_FILE
        if not os.path.exists(path):
            return None
        ts = probe_seconds(path)
        if not ts:
            return None
        return {"id": TRANSITION_ID, "type": "file",
                "path": os.path.relpath(path, HERE),
                "mode": "copy", "seconds": round(ts, 3)}

    out_segs = []
    default_rel = os.path.relpath(MEDIA_DIR, HERE)
    for p in range(max(1, args.passes)):
        for seg in segs:
            m = manifest.get(seg["id"]) or {}
            path = m.get("file") or os.path.join(default_rel, seg["id"] + ".mp4")
            full = os.path.join(HERE, path)
            if not os.path.exists(full):
                continue
            seconds = m.get("seconds") or probe_seconds(full)
            if not seconds:
                log("skipping %s: no duration" % seg["id"], file=sys.stderr)
                continue
            rec = {"id": seg["id"], "type": "file", "path": path,
                   "mode": "copy", "seconds": round(seconds, 3)}
            if m.get("outpoint"):
                rec["outpoint"] = round(min(float(m["outpoint"]), seconds), 3)
            out_segs.append(rec)
            tr = transition_for(seg["id"], p)
            if tr:
                out_segs.append(tr)

    local = {"target": pl.get("target"), "clients": pl.get("clients"),
             "generated_from": os.path.basename(args.playlist),
             "normalized_to": "raw" if args.no_normalize else "%dx%d" % (w, h),
             "segments": out_segs}
    save_json(args.out_playlist, local)

    if os.path.isdir(RAW_DIR) and not os.listdir(RAW_DIR):
        os.rmdir(RAW_DIR)

    total = sum(s.get("outpoint") or s["seconds"] for s in out_segs)
    log("this run: OK=%d FAIL=%d; %s has %d segments, %.0fs total (%.2f h)"
        % (done, fail, os.path.basename(args.out_playlist),
           len(out_segs), total, total / 3600.0))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
