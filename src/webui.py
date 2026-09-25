#!/usr/bin/env python3
"""The loopcastr local console: status, settings and build actions.

Design principles
  - Standard library only. Like the main programs, cloning the repo is enough to run it, with no venv.
  - Binds to 127.0.0.1 by default. Exposing it requires a token of your own.
  - Never runs as root and stores no password: privileged actions only try sudo -n (non-interactive) and
    on failure tell you exactly which sudoers line to add, rather than feeding a password into the program.
  - Write actions do only two things: edit the settings file and call existing scripts. The underlying logic is not reimplemented.

Usage
  python3 webui.py                      # http://127.0.0.1:8787
  python3 webui.py --port 9000
  python3 webui.py --host 0.0.0.0 --token-file webui-token   # a token is required when exposing it
"""

import argparse
import hmac
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import buildlock                 # the build slot, so the button cannot race the refresh service

HERE = os.path.dirname(os.path.abspath(__file__))

# The console title links to the project itself. This is the only project address, so it needs no parameter.
REPO_URL = "https://github.com/kingwap99/loopcastr"
PROJECT = "loopcastr"

# Where the code lives (HERE) is separate from where the data lives (PREFIX).
#   Installed: src/ is flattened into the install directory, so both are the same and everything sits under PREFIX.
#   Run from the repo: the code and settings are in src/ while the data (media, logs) is in the install directory.
# So every file is looked up in both places with pick() and only lands in PREFIX when neither has it.
PREFIX = HERE
MEDIA = os.path.join(PREFIX, "media")
LOGS = os.path.join(PREFIX, "logs")
MODES = os.path.join(PREFIX, "modes.json")
SETTINGS = os.path.join(PREFIX, "settings.json")
PLAYLIST = os.path.join(PREFIX, "playlist.json")
PLAYLIST_LOCAL = os.path.join(PREFIX, "playlist-local.json")
CONCAT = os.path.join(PREFIX, "concat.txt")
PLAYOUT_LOG = os.path.join(LOGS, "playout.log")
KEYFILE = os.path.join(PREFIX, "stream.key")
TASK_LOG = os.path.join(LOGS, "webui-task.log")
MEDIAMTX = os.path.join(PREFIX, "mediamtx.yml")


def set_prefix(path):
    global PREFIX, MEDIA, LOGS, MODES, SETTINGS, PLAYLIST, PLAYLIST_LOCAL
    global CONCAT, PLAYOUT_LOG, KEYFILE, TASK_LOG, MEDIAMTX
    PREFIX = os.path.abspath(path)

    def pick(name):
        for base in (PREFIX, HERE):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
        return os.path.join(PREFIX, name)

    MEDIA = pick("media")
    # media.dir can point the library at another disk; an empty value keeps the default folder.
    _md = str(((read_json(SETTINGS, {}) or {}).get("media") or {}).get("dir") or "").strip()
    if _md:
        MEDIA = _md if os.path.isabs(_md) else os.path.join(PREFIX, _md)
    LOGS = pick("logs")
    MODES = pick("modes.json")
    SETTINGS = pick("settings.json")
    PLAYLIST = pick("playlist.json")
    PLAYLIST_LOCAL = pick("playlist-local.json")
    CONCAT = pick("concat.txt")
    KEYFILE = pick("stream.key")
    MEDIAMTX = pick("mediamtx.yml")
    PLAYOUT_LOG = os.path.join(LOGS, "playout.log")
    TASK_LOG = os.path.join(LOGS, "webui-task.log")

PLAYOUT_START = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})"
    r" (?:start #([0-9]+)|第 ([0-9]+) 次啟動)")


# ── Helpers ─────────────────────────────────────────────────────────
def sh(cmd, timeout=6):
    """Run a command and return (rc, output). A timeout or a missing command counts as failure, without raising."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Write atomically and keep a .bak. A broken settings file takes the whole chain down, so nothing half-done is allowed."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as src:
                old = src.read()
            with open(path + ".bak", "w", encoding="utf-8") as dst:
                dst.write(old)
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def tail(path, n=40):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except OSError:
        return []


def unquote(s):
    """Strip the outer quotes from each concat list line (the quote character is not hard-coded, to avoid quoting fights in the source)."""
    s = s.strip()
    for q in (chr(39), chr(34)):
        if len(s) >= 2 and s.startswith(q) and s.endswith(q):
            s = s[1:-1]
    return s


def human(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return ("%d B" % num) if unit == "B" else ("%.1f %s" % (num, unit))
        num /= 1024.0


def dirsize(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ── Source URL validation ───────────────────────────────────────────
def _is_youtube(url):
    """Only YouTube-family URLs are accepted: this API passes the URL it is given to yt-dlp."""
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = url.split("//", 1)[1].split("/", 1)[0].lower()
    host = host.split("@")[-1].split(":")[0]
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def probe_sources(video_source, shorts_url):
    """Resolve once for real to confirm the URL is right (rather than finding the typo after a whole build)."""
    parts = []
    for label, url in (("頻道／清單", video_source), ("shorts", shorts_url)):
        if not url:
            parts.append("%s：未填" % label)
            continue
        if not _is_youtube(url):
            parts.append("%s：只接受 youtube.com／youtu.be 網址" % label)
            continue
        rc, txt = sh(["yt-dlp", "--no-warnings", "--flat-playlist",
                      "--playlist-end", "1", "--print", "%(id)s|%(title)s", url],
                     timeout=90)
        lines = [x for x in (txt or "").strip().splitlines() if x.strip()]
        if rc == 0 and lines and "|" in lines[0]:
            vid, title = lines[0].split("|", 1)
            parts.append("%s：OK　第一支 %s（%s）" % (label, vid, title[:30]))
        else:
            parts.append("%s：失敗　%s" % (label, (lines or ["沒有輸出"])[-1][:70]))
    return {"ok": True, "summary": "　｜　".join(parts)}


# ── Settings form schema ────────────────────────────────────────────
# (section, title, [(key, field label, type, options, range, help)])
# The form is generated from this schema, so a new knob is one line; types are text / int / float / bool / choice.
SETTINGS_SCHEMA = [
    # (section, title, [(key, field label, type, options, range, help, default)])
    # The form is generated from this schema: a new knob is one line. Types are text / int / float / bool / choice.
    # The default is what the form shows when settings.json has no such key, and it is also the built-in
    # default of the program (the two must agree, or the form shows a number that differs from the real behaviour).
    ("ui", "語言", [
        ("lang", "介面與畫面語言", "choice", ["zh", "en"], None,
         "後台右上角的切換鈕就是改這個；畫面字樣（首播日期、QR 說明）也跟著換", "zh"),
    ]),
    ("media", "媒體與畫質", [
        ("dir", "媒體資料夾", "text", None, None,
         "留空＝程式目錄下的 media；要放到別顆硬碟就填絕對路徑（例如 /Volumes/media/loopcastr）。改完下次建置生效", ""),
        ("target", "解析度", "choice", ["1080", "720", "480"], None,
         "所有片段都正規化到這個尺寸。播出端是純複製，所以全部必須一致", "720"),
        ("fps", "影格率", "int", None, (1, 60), "一般用 30", 30),
        ("venc", "編碼器", "choice", ["libx264", "h264_videotoolbox"], None,
         "libx264 品質穩定但吃 CPU；videotoolbox 走硬體、較省電", "libx264"),
        ("preset", "x264 preset", "choice",
         ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"], None,
         "越快＝同流量下畫質越差；veryfast 是多數情況的平衡點", "veryfast"),
        ("video_bitrate", "影片位元率", "text", None, None,
         "例如 2500k／4000k。最直接影響畫質與上傳頻寬的旋鈕", "2500k"),
        ("video_maxrate", "位元率上限", "text", None, None, "通常與位元率相同", "2500k"),
        ("video_bufsize", "位元率緩衝", "text", None, None, "通常是位元率的 2 倍", "5000k"),
        ("abr", "音訊位元率", "text", None, None, "128k 對談話內容足夠", "128k"),
        ("sample_rate", "音訊取樣率", "int", None, (8000, 48000), "48000 是通用值", 48000),
        ("audio_fade", "換片淡入淡出（秒）", "float", None, (0, 10),
         "每段開頭淡入、結尾淡出。播出端接縫插不了濾鏡，所以要建置時烤進檔案", 2.5),
        ("max_seconds", "每支長度上限（秒）", "int", None, (0, 14400),
         "0＝播完整支。模式層級（modes.json）可以再覆寫", 0),
        ("passes", "一輪播幾趟", "int", None, (1, 10),
         "大於 1 時一輪會重播影片，shorts 池接著往下輪", 1),
    ]),
    ("overlay", "畫面元素", [
        ("date_label", "日期前綴", "text", None, None,
         "浮水印上「首播日期：」那段文字，換語系改這裡", "首播日期："),
        ("date_label_en", "日期前綴（英文）", "text", None, None,
         "ui.lang=en 時用這一個", "First aired: "),
        ("overlay_y", "浮水印距頂端（px）", "int", None, (0, 400), "", 40),
        ("overlay_margin", "左右邊界（px）", "int", None, (0, 400), "", 40),
        ("band_left", "跑馬燈左界（px）", "int", None, (0, 640),
         "0＝自動用畫面寬度的 1/7，讓開原片左上角的 logo", 0),
        ("marquee_speed", "跑馬燈速度（px/秒）", "int", None, (10, 600), "", 120),
        ("marquee_gap", "跑馬燈間距（px）", "int", None, (0, 1000), "兩輪文字之間的空白", 220),
        ("text_size", "文字大小（px）", "int", None, (16, 96), "", 44),
        ("text_stroke", "文字描邊（px）", "int", None, (0, 12),
         "描邊讓字在任何畫面上都看得清", 4),
        ("qr_size", "QR 按鈕字級", "int", None, (12, 60), "", 30),
        ("qr_px", "QR 邊長（px）", "int", None, (60, 300), "越大越好掃，但佔畫面", 120),
        ("link_button", "顯示 QR 按鈕", "bool", None, None, "關掉就只剩跑馬燈", True),
        ("link_caption", "按鈕文字（集數）", "text", None, None, "集數的按鈕說明", "▶ 看原片"),
        ("link_caption_en", "按鈕文字（集數，英文）", "text", None, None, "", "▶ Watch original"),
        ("countdown", "顯示剩餘時間倒數", "bool", None, None,
         "QR 下方那一行「剩餘 02:57」", True),
        ("countdown_prefix", "倒數前綴（中文）", "text", None, None,
         "中文放在秒數前面（剩餘 02:57）", "剩餘 "),
        ("countdown_suffix_en", "倒數後綴（英文）", "text", None, None,
         "英文放在秒數後面（02:57 left）", " left"),
        ("transition_caption", "按鈕文字（過場）", "text", None, None,
         "過場的按鈕說明", "去追劇"),
        ("transition_caption_en", "按鈕文字（過場，英文）", "text", None, None, "", "Watch more"),
        ("sponsor_qr_image", "贊助 QR 圖片（網址或路徑）", "text", None, None,
         "有填就用這張圖（可以放自己的 QR 圖，例如付款平台給的）；留空＝用下面的連結自動產生 QR",
         "https://raw.githubusercontent.com/kingwap99/loopcastr/main/assets/sponsor-qr.png"),
        ("sponsor_url", "贊助連結（QR）", "text", None, None,
         "沒有上面的圖片時，用這個連結自動產生 QR。只畫在過場影片的右下角（集數不畫）；圖片與連結都留空＝不顯示",
         "https://www.paypal.com/ncp/payment/H6UW76SZSN7WS"),
        ("sponsor_show", "顯示贊助 QR", "bool", None, None,
         "取消勾選＝保留上面的連結但影片不畫 QR（過場重做後生效）", True),
        ("sponsor_caption", "贊助按鈕文字", "text", None, None,
         "QR 下方的說明文字", "贊助"),
        ("sponsor_caption_en", "贊助按鈕文字（英文）", "text", None, None, "", "Support"),
    ]),
    ("content", "內容處理", [
        ("black_tail_min", "黑尾門檻（秒）", "float", None, (0, 120),
         "片尾連續黑畫面超過這個秒數就截掉。播出端看不出來，觀眾端是一片黑", 5.0),
        ("black_tail_slack", "黑尾容許範圍（秒）", "float", None, (0, 30),
         "黑尾結束點要落在片尾幾秒內才算數", 2.5),
        ("transitions_parallel", "過場同時編幾個", "int", None, (1, 8),
         "越高越快但越吃 CPU", 3),
    ]),
]


# ── Status ──────────────────────────────────────────────────────────
# pgrep is used rather than launchctl: querying system-domain services needs root, and this program
# deliberately does not run as root. Whether a process exists and whether its log is moving can still be seen.
PROCS = [
    ("mediamtx", "mediamtx"),
    ("playout", "playout.sh"),
    # The playout ffmpeg always carries -stream_loop (the concat loop), so matching on it works for every list
    ("playout ffmpeg", "stream_loop"),
    ("publish", "yt_publish.sh"),
    ("publish ffmpeg", "live2/"),
    ("health", "healthcheck.py"),
    ("refresh", "refreshwatch.py"),
]


def procs():
    out = []
    for name, pattern in PROCS:
        _rc, txt = sh(["pgrep", "-f", pattern])
        n = len([x for x in txt.split() if x.strip().isdigit()])
        out.append({"name": name, "count": n, "up": n > 0})
    return out


def mtx(api, path_name):
    url = "%s/v3/paths/get/%s" % (api.rstrip("/"), path_name)
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.load(r)
        return {"ok": True, "ready": bool(d.get("ready")),
                "readers": len(d.get("readers") or []),
                "bytesReceived": d.get("bytesReceived"),
                "tracks": d.get("tracks") or []}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


# The service (launchd job) list. The front end no longer keeps its own copy and always uses what /api/status returns.
# The label prefix is not hard-coded: installs from before the rename (loopcastr was ytpl) are com.ytpl.*,
# so the prefix comes from the plist actually present in this directory.
SERVICE_NAMES = ["mediamtx", "playout", "publish", "health", "refresh"]
DEFAULT_PREFIX = "com.loopcastr."


def service_prefix():
    for base in (PREFIX, HERE):
        try:
            for name in sorted(os.listdir(base)):
                if name.startswith("com.") and name.endswith(".playout.plist"):
                    return name[: -len("playout.plist")]
        except OSError:
            continue
    return DEFAULT_PREFIX


LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")


def launchctl_jobs():
    """`launchctl list` 一次拿到這個使用者 domain 的所有 job：label -> pid（None＝沒跑）。"""
    rc, out = sh(["launchctl", "list"], timeout=10)
    jobs = {}
    if rc != 0:
        return jobs
    for line in (out or "").splitlines()[1:]:
        f = line.split("\t") if "\t" in line else line.split(None, 2)
        if len(f) < 3:
            continue
        pid, label = f[0].strip(), f[2].strip()
        jobs[label] = int(pid) if pid.isdigit() else None
    return jobs


def service_state(label, jobs):
    """一個服務的狀態。重點是分清楚「沒載入」和「載入了但沒在跑」——
    先前控制台只會顯示「沒有在跑」，看不出 publish 根本沒被載入過。"""
    installed = os.path.join(LAUNCH_AGENTS, label + ".plist")
    src = os.path.join(PREFIX, label + ".plist")
    st = {"label": label, "loaded": False, "pid": None,
          "installed": os.path.exists(installed),
          "plist": src if os.path.exists(src) else ""}
    if label in jobs:
        st["loaded"] = True
        st["pid"] = jobs[label]
    else:
        # System-domain jobs do not show up in `launchctl list`.
        rc, _out = sh(["launchctl", "print", "system/" + label], timeout=6)
        if rc == 0:
            st["loaded"] = True
    return st


def services():
    jobs = launchctl_jobs()
    prefix = service_prefix()
    return [service_state(prefix + s, jobs) for s in SERVICE_NAMES]


def hls_preview(path_name):
    """HLS 預覽頁的位址。

    位址由 mediamtx.yml 決定：`hls: no`（repo 的預設值）時 MediaMTX 根本沒開
    HLS，所以按鈕不該出現 —— 按了只會連到一個空頁面。
    只回傳埠號與路徑，主機名稱交給瀏覽器自己填（從別的機器開控制台也通）。
    """
    cfg = {}
    try:
        with open(MEDIAMTX, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        return {"enabled": False, "why": "找不到 mediamtx.yml"}
    if cfg.get("hls", "").lower() not in ("yes", "true", "1"):
        return {"enabled": False, "why": "mediamtx.yml 的 hls 是 no（預設值，要用請改成 yes）"}
    addr = cfg.get("hlsAddress") or ":8888"
    port = addr.rpartition(":")[2] or "8888"
    if not port.isdigit():
        return {"enabled": False, "why": "hlsAddress 讀不出埠號：%s" % addr}
    return {"enabled": True, "port": int(port), "path": path_name, "why": ""}


def round_info():
    """單輪長度與下一次循環的時間點。

    清單要讀「播出端實際載入的那一份」：永遠讀 playlist-local.json 的話，
    播 news 模式時會顯示預設版（測試版）的長度，看起來就像另一件事。
    """
    d = read_json(loaded_edition().get("playlist") or PLAYLIST_LOCAL)
    if not d or not d.get("segments"):
        return {}
    total = 0.0
    for s in d["segments"]:
        total += float(s.get("outpoint") or s.get("seconds") or 0)
    info = {"segments": len(d["segments"]), "round_seconds": round(total, 1)}
    last = None
    for line in tail(PLAYOUT_LOG, 3000):
        m = PLAYOUT_START.search(line)
        if m:
            last = m
    if last and total > 0:
        st = time.mktime(time.strptime(last.group(1), "%Y-%m-%d %H:%M:%S"))
        now = time.time()
        k = int((now - st) // total) + 1
        nxt = st + total * k
        info["playout_start"] = last.group(1)
        info["playout_run"] = int(last.group(2) or last.group(3))
        info["next_loop"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nxt))
        info["loop_in_seconds"] = int(nxt - now)
    return info


def content_info():
    # Read the concat file the playout actually loaded: hard-coding concat.txt would show 0 segments while test or news is playing.
    list_path = loaded_edition().get("list") or CONCAT
    entries = []
    if os.path.exists(list_path):
        for line in tail(list_path, 600):
            if line.startswith("file "):
                p = unquote(line[5:])
                entries.append((os.path.basename(p), os.path.exists(p)))
    key = os.path.exists(KEYFILE)
    return {
        "concat_entries": len(entries),
        "concat_missing": [n for n, ok in entries if not ok],
        "media_size": human(dirsize(MEDIA)) if os.path.isdir(MEDIA) else "0 B",
        "stream_key": {"exists": key,
                       "bytes": os.path.getsize(KEYFILE) if key else 0},
    }


def have_qrcode():
    """這個直譯器畫不畫得出 QR code。控制台啟動子行程時用的是 sys.executable，所以控制台本身
    沒有 qrcode 時，它觸發的建置就畫不出 QR，而且會安靜地產出沒有 QR 的影片（實測：.41 的控制台
    被手動用 /usr/bin/python3 起起來，之後每一次重建都把整批影片的 QR 洗掉）。"""
    try:
        import qrcode          # noqa: F401
        return True
    except Exception:
        return False


def python_info():
    ok = have_qrcode()
    return {"executable": sys.executable,
            "qrcode": ok,
            "install": "%s -m pip install --user qrcode" % sys.executable}


def status(api, path_name):
    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prefix": PREFIX,
        "lang": ui_lang(),
        "python": python_info(),
        "proc": procs(),
        "mtx": mtx(api, path_name),
        "preview": hls_preview(path_name),
        "services": services(),
        "round": round_info(),
        "content": content_info(),
        "ready": ready_map(api, path_name),
        "playing_mode": mode_of_edition(loaded_edition().get("list")),
        "logs": {
            "health": tail(os.path.join(LOGS, "health.log"), 8),
            "alerts": tail(os.path.join(LOGS, "alerts.jsonl"), 5),
            "publish": tail(os.path.join(LOGS, "publish.log"), 5),
        },
    }


# ── Go-live check ───────────────────────────────────────────────────
# Are the files built, is the playout switched and is the stream up: the three things an operator asks most,
# computed into one verdict on the page instead of making them read the log.
EDITION = {
    "live": ("playlist-local.json", "concat.txt"),
    "news": ("playlist-news-local.json", "concat-news.txt"),
    "promotion": ("playlist-promotion-local.json", "concat-promotion.txt"),
    "test": ("playlist-test-local.json", "concat-test.txt"),
}


def loaded_edition():
    """播出端「實際載入」的那一版。讀 launchctl 而不是讀檔案：
    編輯過 plist 但沒重啟時，檔案的內容會騙人。"""
    label = service_prefix() + "playout"
    rc, txt = sh(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], timeout=5)
    if rc != 0:
        rc, txt = sh(["sudo", "-n", "launchctl", "print", "system/" + label], timeout=5)
    out = {"list": "", "playlist": "", "ok": False}
    for line in (txt or "").splitlines():
        s = line.strip()
        if s.startswith("LIST =>"):
            out["list"] = s.split("=>", 1)[1].strip()
        elif s.startswith("PLAYLIST =>"):
            out["playlist"] = s.split("=>", 1)[1].strip()
    out["ok"] = bool(out["list"] or out["playlist"])
    return out


def _verdict(state, short, detail):
    return {"state": state, "short": short, "detail": detail}


def check_ready(mode, modes_cfg, api, path_name):
    """這個模式現在可以正式開播了嗎？"""
    cfg = (modes_cfg or {}).get(mode) or {}
    src = str(cfg.get("video_source") or "").strip()

    if mode not in EDITION:
        return _verdict("unknown", "不確定的模式", "沒有 %s 對應的清單檔名" % mode)
    pl_name, list_name = EDITION[mode]

    if not src:
        return _verdict("no_source", "還沒填播放清單網址",
                        "到上面的「① 來源設定」填播放清單與 shorts 網址，存檔後再建置")
    if "YourChannel" in src:
        return _verdict("no_source", "來源還是範例值 @YourChannel",
                        "請改成你真實的頻道或播放清單網址")

    with TASK_LOCK:
        running, started = TASK["running"], TASK["started"]
        stopping = TASK.get("stopping")
    if running:
        if stopping:
            return _verdict("building", "正在停止建置…",
                            "已經送出停止訊號，收工後會自動清掉被中斷的半成品")
        return _verdict("building", "正在建置（下載／轉檔中）",
                        "從 %s 開始，完成前不要開播；下面那個框有即時進度" % (started or "剛剛"))

    pl_path = os.path.join(PREFIX, pl_name)
    list_path = os.path.join(PREFIX, list_name)
    if not os.path.exists(pl_path) or not os.path.exists(list_path):
        return _verdict("no_content", "還沒建置內容",
                        "找不到 %s／%s；按下面的「建置並切換（開始直播）」" % (pl_name, list_name))

    segs = (read_json(pl_path) or {}).get("segments") or []
    total = sum(float(s.get("outpoint") or s.get("seconds") or 0) for s in segs)
    files, missing = 0, []
    for line in tail(list_path, 2000):
        if line.startswith("file "):
            files += 1
            if not os.path.exists(unquote(line[5:])):
                missing.append(os.path.basename(unquote(line[5:])))
    info = "%d 段、單輪約 %d 分" % (len(segs), round(total / 60))
    if missing:
        return _verdict("partial", "內容不完整（有檔案不見了）",
                        "少了 %d 個：%s" % (len(missing), "、".join(missing[:4])))

    if os.path.basename(loaded_edition().get("list") or "") != list_name:
        now = os.path.basename(loaded_edition().get("list") or "") or "（沒有載入播出端）"
        return _verdict("not_switched", "已轉好，但播出端還在播另一版",
                        "%s 已就緒（%s）。目前播的是 %s；按「建置並切換（開始直播）」就會切過去"
                        % (list_name, info, now))

    m = mtx(api, path_name)
    if not m.get("ok") or not m.get("ready"):
        return _verdict("stream_down", "串流沒有起來",
                        "MediaMTX 沒有 ready；看下面的「服務行程」與日誌")

    return _verdict("ok", "可以開始直播",
                    "已經轉好、也切換完成（%s），串流正常" % info)


def mode_of_edition(list_path):
    """concat-<mode>.txt → <mode>；concat.txt（正式版）→ live。"""
    b = os.path.basename(list_path or "")
    for m, pair in EDITION.items():
        if b == pair[1]:
            return m
    return ""


def ready_map(api, path_name):
    modes = read_json(MODES) or {}
    return {m: check_ready(m, modes, api, path_name) for m in modes if m != "_comment"}


# ── Actions (background, one at a time) ─────────────────────────────
TASK = {"running": False, "action": "", "started": "", "pid": None, "rc": None,
        "mode": "", "stopping": False, "adopted": False}
TASK_LOCK = threading.Lock()


# ── Process tree (used by stop build) ───────────────────────────────
# Why the whole tree matters: a mode_build started by the console starts build_local_content, which
# starts ffmpeg. Killing only the top level leaves ffmpeg orphaned, still burning CPU and still writing files.
def ps_snapshot():
    """一次 ps 取得整張表：ppid -> [pid] 與 pid -> stat。macOS 的 ps 沒有 --ppid。"""
    rc, out = sh(["ps", "-Ao", "pid=,ppid=,stat="], timeout=8)
    kids, info = {}, {}
    if rc != 0:
        return kids, info
    for line in (out or "").splitlines():
        f = line.split()
        if len(f) < 3 or not f[0].isdigit() or not f[1].isdigit():
            continue
        pid, ppid = int(f[0]), int(f[1])
        kids.setdefault(ppid, []).append(pid)
        info[pid] = f[2].strip()
    return kids, info


def pid_alive(pid, info):
    """殭屍（Z）算已經結束：它只等父行程回收，殺不動也不需要殺。"""
    st = info.get(pid)
    return bool(st) and not st.startswith("Z")


def tree_pids(root, kids):
    seen, stack, out = set(), [root], []
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        stack.extend(kids.get(p, ()))
    return out


def find_build_pids(only_here=False):
    """現在的 mode_build.py 行程 -> {pid: 模式名}。

    only_here：只認這一份安裝（命令列裡有 <HERE>/mode_build.py）的建置。
    同一台機器上可能同時跑著別的目錄的安裝，不能互相搶。
    """
    rc, out = sh(["pgrep", "-f", "mode_build.py"], timeout=8)
    if rc != 0:
        return {}
    marker = os.path.join(HERE, "mode_build.py")
    found = {}
    for tok in (out or "").split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        _rc, cmd = sh(["ps", "-o", "command=", "-p", str(pid)], timeout=8)
        cmd = cmd or ""
        if only_here and marker not in cmd:
            continue
        m = re.search(r"--mode\s+(\S+)", cmd)
        found[pid] = m.group(1) if m else ""
    return found


def stage_dirs(mode):
    """暫存目錄的命名跟 mode_build.py 的 files_for() 一致，改那裡要一起改這裡。"""
    return ["/tmp/stage-ep-%s" % mode, "/tmp/stage-tr-%s" % mode]


def ffprobe_seconds(path):
    """讀長度。0.0＝讀不出來（半成品）。"""
    rc, out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "csv=p=0", path], timeout=30)
    try:
        return float((out or "").strip())
    except ValueError:
        return 0.0


FFPROBE_OK = {"checked": False, "ok": False}


def have_ffprobe():
    """先確認 ffprobe 真的叫得起來。

    不能靠「ffprobe 輸出裡有 not found」來判斷它不存在 —— 半成品的錯誤訊息
    就是「moov atom not found」，那會讓清理整個被跳過。
    """
    if not FFPROBE_OK["checked"]:
        rc, out = sh(["ffprobe", "-version"], timeout=10)
        FFPROBE_OK["ok"] = rc == 0 and "ffprobe version" in (out or "")
        FFPROBE_OK["checked"] = True
    return FFPROBE_OK["ok"]


def sweep_stage(mode):
    """清掉被中斷的半成品。

    落地是「暫存目錄裡有這個檔案就當做完了」，被中斷的半成品會一路被當成完成品
    帶到部署，所以停下來之後要先把讀不出長度的那種清掉，下一輪才會重做。
    只刪 .mp4，而且只在 ffprobe 讀不到任何長度時才刪。
    """
    gone, kept = [], 0
    if not mode:
        return gone, kept
    if not have_ffprobe():
        task_note("stop: ffprobe not found, cannot tell partials apart, "
                  "leaving the staging dir alone")
        return gone, kept
    for d in stage_dirs(mode):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".mp4"):
                continue
            full = os.path.join(d, name)
            secs = ffprobe_seconds(full)
            if secs >= 1.0:
                kept += 1
                continue
            try:
                os.unlink(full)
                gone.append(name)
            except OSError:
                kept += 1
    return gone, kept


def inflight_outputs(targets, mode):
    """被我們殺掉的行程「正在寫哪幾個輸出檔」。

    為什麼非做這件事不可：ffmpeg 收到 SIGTERM 會正常收尾，把 moov 寫出來，
    留下一個讀得出來、卻只有幾十秒的檔案（實測 65.6 秒）。落地是「檔案存在
    就當做完了」，這種短檔會被當成完成品一路播出去。所以被中斷的輸出檔一律
    刪掉，不看它讀不讀得出來。
    """
    if not mode:
        return []
    roots = tuple(stage_dirs(mode) + [os.path.join(MEDIA, ".raw")])
    hits, now = set(), time.time()
    for pid in targets:
        _rc, cmd = sh(["ps", "-o", "command=", "-p", str(pid)], timeout=8)
        for tok in (cmd or "").split():
            tok = tok.strip(chr(39) + chr(34))
            if not tok.endswith(".mp4") or not tok.startswith("/"):
                continue
            if not tok.startswith(roots):
                continue
            try:
                if now - os.path.getmtime(tok) > 120:   # 不是這次在寫的，別動它
                    continue
            except OSError:
                continue
            hits.add(tok)
    return sorted(hits)


def task_note(text_):
    """把 console 自己的訊息也寫進同一個進度檔，使用者在同一個框就看得到。"""
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(TASK_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (time.strftime("%H:%M:%S"), text_))
    except OSError:
        pass


def reap_stopped(targets, mode, inflight):
    """等行程收工；不聽話的補 SIGKILL；最後清半成品。"""
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            _kids, info = ps_snapshot()
            if not any(pid_alive(p, info) for p in targets):
                break
            time.sleep(0.3)
        _kids, info = ps_snapshot()
        hard = [p for p in targets if pid_alive(p, info)]
        for pid in hard:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if hard:
            task_note("stop: %d process(es) ignored SIGTERM, sent SIGKILL" % len(hard))
        killed = []
        for p in inflight:
            try:
                os.unlink(p)
                killed.append(os.path.basename(p))
            except OSError:
                pass
        gone, kept = sweep_stage(mode)
        task_note("stop done: deleted %d interrupted output(s), %d unreadable partial(s) "
                  "(%d complete files kept)%s"
                  % (len(killed), len(gone), kept,
                     ("：" + "、".join(killed + gone)) if (killed or gone) else ""))
    finally:
        with TASK_LOCK:
            TASK["stopping"] = False


def stop_task():
    with TASK_LOCK:
        if not TASK["running"]:
            return {"ok": False, "error": "現在沒有在跑的工作"}
        if TASK.get("stopping"):
            return {"ok": False, "error": "已經在停止中了，等它收尾"}
        TASK["stopping"] = True
        root, mode, action = TASK["pid"], TASK.get("mode") or "", TASK["action"]

    kids, _info = ps_snapshot()
    targets = tree_pids(root, kids) if root else []
    # The launchd refreshwatch also starts mode_build, which is another tree (we are not its parent).
    # Collect the same mode together, or pressing stop leaves another build running in the background.
    if action == "mode-build" and mode:
        for pid, m in find_build_pids(only_here=True).items():
            if pid in targets or pid == os.getpid():
                continue
            if m == mode:
                targets.append(pid)
    targets = sorted(set(targets))

    # Record which files are being written first: once the process is killed it cannot be asked.
    inflight = inflight_outputs(targets, mode)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    threading.Thread(target=reap_stopped, args=(targets, mode, inflight),
                     daemon=True).start()
    task_note("stop: sent SIGTERM to %s"
              % (", ".join("pid %d" % p for p in targets) if targets else "（沒有找到行程）"))
    return {"ok": True, "pids": targets, "mode": mode,
            "detail": "pid %s" % (", ".join(str(p) for p in targets) or "無")
                      + "；停下來後會自動清掉被中斷的半成品"}


def watch_adopted(pid):
    """webui 重啟後接手的那個建置跑完了，要把狀態收回來。"""
    while True:
        _kids, info = ps_snapshot()
        if not pid_alive(pid, info):
            break
        time.sleep(3)
    with TASK_LOCK:
        if TASK.get("pid") == pid:
            TASK.update({"running": False, "rc": None, "adopted": False,
                         "stopping": False})
    task_note("a build that was already running before the console restarted has finished "
              "(not started by this console; exit code unknown)")


def adopt_running_build():
    """webui 重啟時，先前由它拉起的建置還活著（子行程不會跟著父行程死）。

    不接手的話，控制台會以為沒事、允許再按一次建置，兩個建置就會撞在同一個
    暫存目錄上。
    """
    found = find_build_pids(only_here=True)
    if not found:
        return
    pid = sorted(found)[0]
    _rc, start = sh(["ps", "-o", "lstart=", "-p", str(pid)], timeout=8)
    with TASK_LOCK:
        TASK.update({"running": True, "action": "mode-build", "mode": found[pid],
                     "pid": pid, "rc": None, "stopping": False, "adopted": True,
                     "started": (start or "").strip() or "（先前啟動）"})
    threading.Thread(target=watch_adopted, args=(pid,), daemon=True).start()


def build_cmd(action, body):
    """把動作翻成 argv。一律用清單、不經 shell，也不接受使用者給的任意路徑。"""
    if action == "mode-build":
        mode = str(body.get("mode") or "").strip()
        modes = read_json(MODES) or {}
        if mode not in modes:
            return None, "沒有這個模式：%s" % mode
        src = str((modes.get(mode) or {}).get("video_source") or "").strip()
        # Block builds that are bound to fail up front: a run downloads hundreds of MB and takes minutes.
        if not src:
            return None, "%s 還沒填「頻道或播放清單網址」" % mode
        if "YourChannel" in src:
            return None, ("%s 的來源還是範例值 @YourChannel，請先到「來源設定」填真實的頻道網址"
                          % mode)
        if not _is_youtube(src):
            return None, "%s 的來源不是 YouTube 網址：%s" % (mode, src)
        # A full build draws the QR buttons with the interpreter that runs the build, which is this
        # console's sys.executable. Without qrcode the whole run produces QR-less video, so stop it here
        # with the fix instead of letting it fill the disk with files that have to be rebuilt again.
        # --scan-only and --deploy-only never encode, so they stay usable on a machine without qrcode.
        if not (body.get("scan-only") or body.get("deploy-only")) and not have_qrcode():
            return None, ("這個控制台用的 Python 少了 qrcode，建置出來的影片不會有 QR code。"
                          "改用有 qrcode 的 Python 重啟控制台，或安裝："
                          + python_info()["install"])
        # The refresh service starts its own build every few minutes when the source has new videos.
        # Starting a second one would have two processes writing the same playlists; say so instead of
        # spawning a build that immediately stops itself.
        if not (body.get("scan-only") or body.get("deploy-only")) and buildlock.busy(HERE):
            return None, ("已經有另一個建置在跑，請等它結束再按。"
                          + " (pid %s)" % (buildlock.holder(HERE) or "?"))
        cmd = [sys.executable, os.path.join(HERE, "mode_build.py"), "--mode", mode]
        for flag in ("scan-only", "deploy-only", "skip-transitions"):
            if body.get(flag):
                cmd.append("--" + flag)
        if body.get("switch"):
            cmd.append("--switch")
        return cmd, ""
    if action == "concat":
        # Use the list of the mode that is selected, not a hard-coded production one:
        # on a machine where only test was ever built, playlist-local.json does not exist at all.
        mode = str(body.get("mode") or "").strip() or "live"
        pl_name, list_name = EDITION.get(mode, EDITION["live"])
        src = os.path.join(PREFIX, pl_name)
        if not os.path.exists(src):
            return None, "%s 不存在；這個模式要先建置過" % pl_name
        return [sys.executable, os.path.join(HERE, "make_concat_list.py"),
                src, "-o", os.path.join(PREFIX, list_name), "--base-dir", HERE], ""
    if action == "status":
        mode = str(body.get("mode") or "").strip() or "live"
        name = "playlist.json" if mode == "live" else "playlist-%s.json" % mode
        src = os.path.join(PREFIX, name)
        if not os.path.exists(src):
            return None, "%s 不存在；這個模式要先掃描過" % name
        cmd = [sys.executable, os.path.join(HERE, "build_local_content.py"),
               "--playlist", src, "--status"]
        media = MEDIA if mode == "live" else os.path.join(MEDIA, mode)
        if os.path.isdir(media):
            cmd += ["--media-dir", media]
        return cmd, ""
    if action == "loopwatch":
        return [sys.executable, os.path.join(HERE, "loopwatch.py"),
                "--playlist", PLAYLIST_LOCAL, "--lead", "45", "--tail", "90"], ""
    return None, "未知動作：%s" % action


def start_task(action, body):
    if action in ("stop", "stop-build"):
        return stop_task()
    cmd, err = build_cmd(action, body)
    if err:
        return {"ok": False, "error": err}
    # A build must know its mode so that stop knows which staging directory to clean.
    mode = str(body.get("mode") or "").strip() if action == "mode-build" else ""
    with TASK_LOCK:
        if TASK.get("stopping"):
            return {"ok": False,
                    "error": "上一個工作還在收尾（正在停止），等下面顯示「已完成／待機」再按"}
        if TASK["running"]:
            return {"ok": False, "error": "已經有工作在跑：%s" % TASK["action"]}
        os.makedirs(LOGS, exist_ok=True)
        with open(TASK_LOG, "w", encoding="utf-8") as fh:
            fh.write("$ %s\n" % " ".join(cmd))
        out = open(TASK_LOG, "a", encoding="utf-8")
        # start_new_session puts the whole build tree in its own process group, so stop can kill one group
        # without killing the console itself (they used to share a group).
        proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        TASK.update({"running": True, "action": action,
                     "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "pid": proc.pid, "rc": None, "cmd": " ".join(cmd),
                     "mode": mode, "stopping": False, "adopted": False})

    def waiter():
        rc = proc.wait()
        out.close()
        with TASK_LOCK:
            TASK.update({"running": False, "rc": rc})

    threading.Thread(target=waiter, daemon=True).start()
    return {"ok": True, "cmd": " ".join(cmd)}


def task_state():
    with TASK_LOCK:
        st = dict(TASK)
    # While a build runs the whole tail is the progress view. Once it is done only the end matters, and
    # 200 lines of it would push the everyday controls off the page.
    st["log"] = tail(TASK_LOG, 200 if st.get("running") else 40)
    return st


# ── Actions that need privileges ────────────────────────────────────
def start_service(label):
    """把「還沒載入」的服務裝起來：plist 從資料目錄複製到 ~/Library/LaunchAgents
    再 bootstrap。只搬既有檔案，不自己生設定。"""
    src = os.path.join(PREFIX, label + ".plist")
    if not os.path.exists(src):
        return {"ok": False, "error": "資料目錄裡沒有 %s.plist，無法安裝" % label}
    dst = os.path.join(LAUNCH_AGENTS, label + ".plist")
    try:
        os.makedirs(LAUNCH_AGENTS, exist_ok=True)
        same = False
        if os.path.exists(dst):
            with open(dst, "rb") as a, open(src, "rb") as b:
                same = a.read() == b.read()
        if not same:
            shutil.copyfile(src, dst)
    except OSError as exc:
        return {"ok": False, "error": "複製 plist 失敗：%s" % exc}
    rc, out = sh(["launchctl", "bootstrap", "gui/%d" % os.getuid(), dst], timeout=20)
    if rc == 0:
        return {"ok": True, "how": "launchctl bootstrap gui/%d %s" % (os.getuid(), dst)}
    # Bootstrapping an already loaded job fails, so kickstart is used to get it running.
    rc2, _o2 = sh(["launchctl", "kickstart", "gui/%d/%s" % (os.getuid(), label)],
                  timeout=15)
    if rc2 == 0:
        return {"ok": True, "how": "launchctl kickstart gui/%d/%s" % (os.getuid(), label)}
    return {"ok": False, "error": (out or "").strip()[-200:],
            "hint": "系統 domain 的服務要自己來：sudo launchctl bootstrap system "
                    "/Library/LaunchDaemons/%s.plist" % label}


def restart_service(label):
    """先試 system domain（非互動 sudo），不行再試目前使用者的 gui domain。"""
    if not re.match(r"^[A-Za-z0-9_.-]+$", label or ""):
        return {"ok": False, "error": "不合法的服務名稱"}
    _rc, txt = sh(["sudo", "-n", "launchctl", "kickstart", "-k", "system/" + label],
                  timeout=15)
    if _rc == 0:
        return {"ok": True, "how": "sudo launchctl kickstart -k system/" + label}
    rc2, _txt2 = sh(["launchctl", "kickstart", "-k",
                     "gui/%d/%s" % (os.getuid(), label)], timeout=15)
    if rc2 == 0:
        return {"ok": True,
                "how": "launchctl kickstart -k gui/%d/%s" % (os.getuid(), label)}
    return {"ok": False,
            "error": "兩個 domain 都失敗（" + txt.strip()[-200:] + "）",
            "hint": "system domain 需要非互動 sudo，請在 /etc/sudoers.d/loopcastr-webui 加："
                    "  <你的帳號> ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/"
                    + label}


# ── HTTP ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "loopcastr-webui"
    protocol_version = "HTTP/1.1"
    api = "http://127.0.0.1:9997"
    path_name = "live/main"
    token = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(localize_obj(body), ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not self.token:
            return True
        got = self.headers.get("X-Ytpl-Token") or ""
        if not got and "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("token="):
                    got = part[6:]
        return hmac.compare_digest(got, self.token)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 2 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as exc:
            self._fail(exc)

    def _do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._authed():
            return self._send(401, {"error": "需要 token"})
        if path == "/":
            return self._send(200, localize(PAGE), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status(self.api, self.path_name))
        if path == "/api/config":
            return self._send(200, {"settings": read_json(SETTINGS, {}),
                                    "modes": read_json(MODES, {})})
        if path == "/api/schema":
            return self._send(200, {"settings": localize_schema(SETTINGS_SCHEMA)})
        if path == "/api/sponsor-qr":
            return self._send_sponsor_qr()
        if path == "/api/task":
            return self._send(200, task_state())
        return self._send(404, {"error": "not found"})

    def _send_sponsor_qr(self):
        """Serve the sponsor QR picture the videos would use, so the console shows the same thing.

        A picture configured as a local path is served from disk; otherwise a QR is generated
        from the payment link. Both come from the settings file and never from the query string,
        so this endpoint cannot render QR codes for arbitrary links. It deliberately ignores
        sponsor_show: the console previews the QR even while it is hidden from the videos.
        """
        overlay = (read_json(SETTINGS, {}) or {}).get("overlay") or {}
        picture = str(overlay.get("sponsor_qr_image") or "")
        url = str(overlay.get("sponsor_url") or "")
        path = ""
        if picture and "://" not in picture:
            cand = picture if os.path.isabs(picture) else os.path.join(PREFIX, picture)
            if os.path.exists(cand):
                path = cand
        if not path and not url:
            return self._send(404, {"error": "no sponsor QR is configured"})
        try:
            os.makedirs(LOGS, exist_ok=True)
        except OSError:
            pass
        if not path:
            out = os.path.join(LOGS, "sponsor-preview.png")
            try:
                rc = subprocess.run([sys.executable, os.path.join(HERE, "make_qr_png.py"),
                                     url, out, "--scale", "8", "--border", "4"],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=30).returncode
            except (OSError, subprocess.SubprocessError):
                rc = 1
            if rc != 0 or not os.path.exists(out):
                return self._send(500, {"error": "could not render the QR code"})
            path = out
        with open(path, "rb") as fh:
            data = fh.read()
        return self._send(200, data, "image/png")

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        """handler 內出錯時回一個正常的 500，不要直接斷線（斷線在瀏覽器會變成 NetworkError）"""
        try:
            self._send(500, {"error": "內部錯誤：%s" % exc})
        except Exception:
            pass

    def _do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authed():
            return self._send(401, {"error": "需要 token"})
        # Only JSON with the custom header is accepted: a cross-site form cannot set it, so this blocks CSRF too.
        if self.headers.get("X-Ytpl") != "1":
            return self._send(400, {"error": "缺少 X-Ytpl 標頭"})
        body = self._body()
        if path == "/api/config":
            target = {"settings": SETTINGS, "modes": MODES}.get(body.get("kind"))
            data = body.get("data")
            if not target:
                return self._send(400, {"error": "kind 必須是 settings 或 modes"})
            if not isinstance(data, dict) or not data:
                return self._send(400, {"error": "data 必須是非空物件"})
            try:
                write_json(target, data)
            except OSError as exc:
                return self._send(500, {"error": "寫入失敗：%s" % exc})
            return self._send(200, {"ok": True, "wrote": os.path.basename(target),
                                    "note": "下次建置生效；舊版已備份為 .bak"})
        if path == "/api/action":
            return self._send(200, start_task(body.get("action") or "", body))
        if path == "/api/service":
            label = body.get("label") or ""
            if not re.match(r"^[A-Za-z0-9_.-]+$", label):
                return self._send(400, {"error": "不合法的服務名稱"})
            if (body.get("action") or "restart") == "start":
                return self._send(200, start_service(label))
            return self._send(200, restart_service(label))
        if path == "/api/probe":
            return self._send(200, probe_sources(body.get("video_source") or "",
                                                 body.get("shorts_url") or ""))
        if path == "/api/stream-key":
            key = (body.get("key") or "").strip()
            if not re.match(r"^[A-Za-z0-9_-]{8,64}$", key):
                return self._send(400, {"error": "金鑰格式看起來不對（只允許英數與 - _）"})
            try:
                fd = os.open(KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.write(fd, key.encode("utf-8"))
                os.close(fd)
            except OSError as exc:
                return self._send(500, {"error": "寫入失敗：%s" % exc})
            return self._send(200, {"ok": True, "bytes": len(key),
                                    "note": "要重啟 publish 服務才會生效"})
        if path == "/api/lang":
            lang = (body.get("lang") or "").strip().lower()
            if lang not in ("zh", "en"):
                return self._send(400, {"error": "語系只能是 zh 或 en"})
            cfg = read_json(SETTINGS, {}) or {}
            cfg.setdefault("ui", {})["lang"] = lang
            try:
                write_json(SETTINGS, cfg)
            except OSError as exc:
                return self._send(500, {"error": "寫入失敗：%s" % exc})
            return self._send(200, {"ok": True, "lang": lang})
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=HERE,
                    help="data directory (default: the directory of this file). Not needed once "
                         "installed; when running straight from the repo point it at the package root")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api", default=os.environ.get("API", "http://127.0.0.1:9997"))
    ap.add_argument("--path-name", default=os.environ.get("PATH_NAME", "live/main"))
    ap.add_argument("--token-file", default=os.path.join(HERE, "webui-token"))
    a = ap.parse_args()
    set_prefix(a.prefix)
    # A build started by this console before the restart is still alive, so adopt its state (otherwise another could be started).
    adopt_running_build()

    token = ""
    if os.path.exists(a.token_file):
        with open(a.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("拒絕啟動：--host %s 等於對外開放，必須提供 token。" % a.host,
              file=sys.stderr)
        print("  先產生：" , file=sys.stderr)
        print("    openssl rand -hex 16 > %s && chmod 600 %s"
              % (a.token_file, a.token_file), file=sys.stderr)
        return 2

    Handler.api = a.api
    Handler.path_name = a.path_name
    Handler.token = token
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("%s 控制台：http://%s:%d/   （API %s，路徑 %s）"
          % (PROJECT, a.host, a.port, a.api, a.path_name), flush=True)
    if token:
        print("已啟用 token 驗證（%s）" % a.token_file, flush=True)
    print("按 Ctrl-C 結束", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("")
    return 0


# PAGE must be defined before if __name__: running as a script goes straight into
# serve_forever(), and definitions after it never run (measured: GET / returned empty).
# ── Language (console interface) ────────────────────────────────────
# Approach: the source is written in Chinese and the whole string is replaced before it reaches the
# browser (HTML, JS literals and the JSON the API returns). That avoids scattering placeholders through the page and keeps one translation table.
# Note: an English translation must not contain a double quote or a backslash, because the string is inserted straight into HTML / JS / JSON.
UI_TEXT = {
    # ── Page skeleton
    " 控制台": " console",
    "① 來源設定": "1. Sources",
    "② 開始直播": "2. Go live",
    "播出狀態": "Playout status",
    "服務行程": "Service processes",
    "內容": "Content",
    "日誌": "Logs",
    "畫質與版面": "Quality and layout",
    "進階設定（原始 JSON）": "Advanced (raw JSON)",
    "進階選項": "Advanced",
    "平常不用打開。要調畫質與版面、看細節，或改原始 JSON 再展開。":
        "You will not need these day to day. Open one to change quality and layout, look at the "
        "details, or edit the raw JSON.",
    "更多設定（影片數上限、長度、shorts、掃描間隔）":
        "More settings (video limit, lengths, shorts, rescan interval)",
    "直播金鑰": "Stream key",
    "服務": "Services",
    "填這兩個網址 → 按「儲存這個模式」→ 再按下面的「開始直播」。\n「驗證網址」會先實際解析一次，確認網址沒打錯（填錯不用等整場建置跑完才發現）。":
        "Fill in these two URLs → press Save this mode → then press Go live below.\n"
        "Check URLs resolves them once for real, so a typo shows up immediately "
        "instead of after a long build.",
    "第一次會下載與轉檔（每支影片數十 MB，數分鐘到數十分鐘）；已經下載過的會跳過。\n切換會重啟播出端，中斷數秒。按鈕按下去是在背景跑，下面會即時顯示進度。\n按「停止建置」會把整個建置連子行程（含 ffmpeg）一起停掉，並清掉被中斷的半成品；\n已經轉好的檔案會留著，下次建置從缺的補。":
        "The first run downloads and transcodes (tens of MB per video, minutes each); "
        "anything already downloaded is skipped.\nSwitching restarts the playout and "
        "interrupts the stream for a few seconds. It all runs in the background and the "
        "box below shows live progress.\nStop build kills the whole process tree "
        "(including ffmpeg) and deletes the interrupted output files; finished files are "
        "kept and the next build fills in what is missing.",
    "存檔後要重新建置才會套用到已下載的內容（改畫質等於重新轉檔）。":
        "Saving only takes effect after a rebuild: changing quality means re-encoding.",
    "settings.json（畫質、版面、淡化、黑尾門檻）":
        "settings.json (quality, layout, fades, black-tail threshold)",
    "modes.json 原始內容（上面表單沒涵蓋的欄位改這裡；存檔會整份覆蓋）":
        "raw modes.json (edit fields the form above does not cover; saving replaces the whole file)",
    "寫入 stream.key（權限 600）。金鑰只進不出，這個頁面不會把它顯示出來。":
        "Writes stream.key (mode 600). The key is write-only: this page never displays it.",
    "「沒有載入」＝launchd 根本沒這個 job（例如金鑰貼好了卻沒有畫面，就是推流服務沒被載入）。\n按「啟動」會把資料目錄裡的 plist 複製到 ~/Library/LaunchAgents 再 bootstrap。\n系統 domain 的服務需要非互動 sudo；失敗時會顯示要加哪一條 sudoers。":
        "Not loaded means launchd does not have this job at all (the classic case: the key "
        "is saved but YouTube stays black because the publish service was never loaded).\n"
        "Start copies the plist from the data directory into ~/Library/LaunchAgents and "
        "bootstraps it.\nSystem-domain services need non-interactive sudo; on failure the "
        "page tells you which sudoers line to add.",
    # ── Buttons and form
    "建置並切換（開始直播）": "Build and switch (go live)",
    "只建置，不切換": "Build only",
    "只掃描來源": "Scan sources only",
    "重建 concat 清單": "Rebuild concat list",
    "檢查缺哪些檔案": "Check missing files",
    "停止建置": "Stop build",
    "儲存這個模式": "Save this mode",
    "儲存這一段": "Save this section",
    "儲存原始 JSON": "Save raw JSON",
    "▶ 看直播畫面": "▶ Live preview",
    "儲存": "Save",
    "寫入": "Write",
    "驗證網址": "Check URLs",
    "重啟": "Restart",
    "啟動": "Start",
    "模式名稱（顯示用）": "mode name (display only)",
    "① 播放清單網址（要播的影片）": "1. playlist URL (the videos to play)",
    "② 過場 shorts 網址（轉場輪播）": "2. shorts URL (transition carousel)",
    "影片數上限": "max videos",
    "每支長度上限（秒，0＝全長）": "max seconds per video (0 = full length)",
    "每支 short 長度上限（秒）": "max seconds per short",
    "shorts 支數": "shorts count",
    "重新掃描間隔（秒，0＝不掃）": "rescan interval in seconds (0 = never)",
    "一輪播幾趟（0＝用預設）": "passes per round (0 = auto)",
    "只收首播時間在 N 小時內的影片（0＝不限；先用上面的支數取前 N 支再過濾）":
        "keep only videos first aired within N hours (0 = no limit; applied after the video limit)",
    "只播幾小時內首播的（0＝不限）": "only videos first aired within N hours (0 = no limit)",
    "先做幾支就開播（0＝全部做完才切換）":
        "go live after building this many videos (0 = wait for everything)",
    "播放順序": "play order",
    "來源順序": "source order",
    "首播日期：舊→新": "first aired: oldest first",
    "首播日期：新→舊": "first aired: newest first",
    "語系只能是 zh 或 en": "the language must be zh or en",
    "%s 不存在；這個模式要先建置過": "%s does not exist; build this mode first",
    "%s 不存在；這個模式要先掃描過": "%s does not exist; scan this mode first",
    "新聞模式": "News mode",
    "（modes.json 裡沒有可編輯的模式）": "(no editable modes in modes.json)",
    # ── Status and messages
    "可以開始直播": "ready to go live",
    "已經轉好、也切換完成（%s），串流正常": "built and switched (%s); the stream is healthy",
    "還沒建置內容": "no content built yet",
    "還沒填播放清單網址": "no playlist URL yet",
    "正在建置（下載／轉檔中）": "building (downloading / transcoding)",
    "正在停止建置…": "stopping the build...",
    "已轉好，但播出端還在播另一版": "built, but the playout is still on another edition",
    "內容不完整（有檔案不見了）": "content is incomplete (some files are gone)",
    "串流沒有起來": "the stream is not up",
    "不確定的模式": "unknown mode",
    "來源還是範例值 @YourChannel": "the source is still the @YourChannel example",
    "來源還是範例值 @YourChannel —— 建置前請先填上真實的頻道網址（貼上後要按「儲存這個模式」才會生效）。":
        "the source is still the @YourChannel example — set a real channel URL before "
        "building (and press Save this mode for it to take effect).",
    "過場 shorts 網址還是範例值 @YourChannel —— 這個模式不會做過場（要有的話填上真實的 shorts 網址並存檔）。":
        "the shorts URL is still the @YourChannel example — this mode will build no "
        "transitions (fill in a real shorts URL and save to get them).",
    "來源還是範例值 @YourChannel —— 建置前請先填上真實的頻道網址。":
        "the source is still the @YourChannel example — set a real channel URL before building.",
    "請改成你真實的頻道或播放清單網址": "set your real channel or playlist URL",
    "到上面的「① 來源設定」填播放清單與 shorts 網址，存檔後再建置":
        "fill in the playlist and shorts URLs under Sources, save, then build",
    "找不到 %s／%s；按下面的「建置並切換（開始直播）」":
        "cannot find %s / %s; press Build and switch (go live) below",
    "沒有 %s 對應的清單檔名": "no list file is mapped to %s",
    "從 %s 開始，完成前不要開播；下面那個框有即時進度":
        "started at %s; do not go live before it finishes — the box below shows progress",
    "已經送出停止訊號，收工後會自動清掉被中斷的半成品":
        "stop signal sent; interrupted output files are cleaned up when it finishes",
    "%s 已就緒（%s）。目前播的是 %s；按「建置並切換（開始直播）」就會切過去":
        "%s is ready (%s). Now playing %s; press Build and switch (go live) to switch over",
    "（沒有載入播出端）": "(nothing loaded in the playout)",
    "MediaMTX 沒有 ready；看下面的「服務行程」與日誌":
        "MediaMTX is not ready; check Service processes and the logs below",
    "這個模式現在可以正式開播了嗎？": "can this mode go live now?",
    "少了 %d 個：%s": "%d missing: %s",
    "%d 段、單輪約 %d 分": "%d segments, about %d min per round",
    "剛剛": "just now",
    # ── Actions and errors
    "已開始：": "started: ",
    "無法開始：": "cannot start: ",
    "已送出停止：": "stop sent: ",
    "無法停止：": "cannot stop: ",
    "已儲存 ": "saved ",
    "儲存失敗：": "save failed: ",
    "失敗：": "failed: ",
    "查不到：": "query failed: ",
    "驗證中…（會實際解析一次，約數秒）": "checking... (resolves once for real, a few seconds)",
    "驗證失敗：": "check failed: ",
    "JSON 有錯：": "JSON error: ",
    "讀狀態失敗，正在重試…": "status read failed, retrying...",
    "讀狀態失敗（連續 %s 次）：%s": "status read failed (%s times in a row): %s",
    "請先輸入金鑰": "enter the key first",
    "已設定（%s bytes）": "set (%s bytes)",
    "未設定": "not set",
    "已寫入 stream.key（%s bytes）。%s": "wrote stream.key (%s bytes). %s",
    "已經有工作在跑：%s": "a job is already running: %s",
    "上一個工作還在收尾（正在停止），等下面顯示「已完成／待機」再按":
        "the previous job is still winding down; wait for the status below to say idle",
    "現在沒有在跑的工作": "no job is running",
    "已經在停止中了，等它收尾": "already stopping, waiting for it to finish",
    "不合法的服務名稱": "invalid service name",
    "資料目錄裡沒有這個 plist": "no such plist in the data directory",
    "需要 token": "token required",
    "缺少 X-Ytpl 標頭": "missing the X-Ytpl header",
    "內部錯誤：%s": "internal error: %s",
    "未知動作：%s": "unknown action: %s",
    "金鑰格式看起來不對（只允許英數與 - _）":
        "the key format looks wrong (letters, digits, - and _ only)",
    "要重啟 publish 服務才會生效": "restart the publish service for it to take effect",
    "下次建置生效；舊版已備份為 .bak":
        "takes effect on the next build; the previous version is kept as .bak",
    "沒有輸出": "no output",
    "（無）": "(none)",
    "無": "none",
    "未 ready": "not ready",
    "沒有在跑": "not running",
    "執行中 (%s)": "running (%s)",
    "執行中　": "running　",
    "已完成／待機　": "idle　",
    "正在停止…　": "stopping...　",
    "　執行中 pid %s": "　running pid %s",
    "　已載入（沒在跑）": "　loaded (not running)",
    "　沒有載入": "　not loaded",
    "　結束碼 %s%s": "　exit code %s%s",
    "　目錄 ": "　dir ",
    "　讀者 ": "　readers ",
    " 段　": " segments　",
    " 秒": " s",
    " 段": " segments",
    "下次循環": "next loop",
    "單輪": "round",
    "行程": "process",
    "狀態": "state",
    "（被中止）": "(interrupted)",
    "（webui 重啟前啟動的）": " (started before the console restarted)",
    "缺 ": "missing ",
    " 安裝並啟動": " install and start",
    "從 %s 安裝並啟動": "install and start from %s",
    "已啟動 %s（%s）": "started %s (%s)",
    "已重啟 %s（%s）": "restarted %s (%s)",
    "%s：%s %s": "%s: %s %s",
    "%s（%s 秒後）": "%s (%s s later)",
    "已儲存 %s：%s": "saved %s: %s",
    "已寫入 %s（%s）": "wrote %s (%s)",
    "越快＝同流量下畫質越差；veryfast 是多數情況的平衡點":
        "faster means worse quality at the same bitrate; veryfast is the usual balance",
    # ── settings.json form
    "媒體與畫質": "Media and quality",
    "媒體資料夾": "Media folder",
    "留空＝程式目錄下的 media；要放到別顆硬碟就填絕對路徑（例如 /Volumes/media/loopcastr）。改完下次建置生效":
        "empty = the media folder beside the programs; use an absolute path to keep the library on another disk "
        "(for example /Volumes/media/loopcastr). Takes effect on the next build",
    "畫面元素": "On-screen elements",
    "內容處理": "Content handling",
    "解析度": "Resolution",
    "所有片段都正規化到這個尺寸。播出端是純複製，所以全部必須一致":
        "every segment is normalized to this size; the playout copies streams, so they must match",
    "影格率": "Frame rate",
    "一般用 30": "30 is the usual choice",
    "編碼器": "Encoder",
    "libx264 品質穩定但吃 CPU；videotoolbox 走硬體、較省電":
        "libx264 is stable but CPU-hungry; videotoolbox uses hardware and saves power",
    "越高越快但越吃 CPU": "higher is faster but uses more CPU",
    "影片位元率": "Video bitrate",
    "例如 2500k／4000k。最直接影響畫質與上傳頻寬的旋鈕":
        "e.g. 2500k / 4000k; the most direct quality-versus-bandwidth knob",
    "位元率上限": "Max bitrate",
    "通常與位元率相同": "usually the same as the bitrate",
    "位元率緩衝": "Bitrate buffer",
    "通常是位元率的 2 倍": "usually twice the bitrate",
    "音訊位元率": "Audio bitrate",
    "128k 對談話內容足夠": "128k is plenty for speech",
    "音訊取樣率": "Audio sample rate",
    "48000 是通用值": "48000 is the common value",
    "換片淡入淡出（秒）": "Fade in/out (seconds)",
    "每段開頭淡入、結尾淡出。播出端接縫插不了濾鏡，所以要建置時烤進檔案":
        "fade in at the start and out at the end; the playout seam cannot take a filter, "
        "so it is baked in at build time",
    "每支長度上限（秒）": "Max seconds per video",
    "0＝播完整支。模式層級（modes.json）可以再覆寫":
        "0 = full length; the per-mode value in modes.json overrides this",
    "一輪播幾趟": "Passes per round",
    "大於 1 時一輪會重播影片，shorts 池接著往下輪":
        "above 1, videos repeat within a round and the shorts pool keeps advancing",
    "日期前綴": "Date label",
    "浮水印上「首播日期：」那段文字，換語系改這裡":
        "the text before the air time on the watermark; per-language text goes here",
    "浮水印距頂端（px）": "Watermark top offset (px)",
    "左右邊界（px）": "Side margin (px)",
    "跑馬燈速度（px/秒）": "Marquee speed (px/s)",
    "跑馬燈間距（px）": "Marquee gap (px)",
    "兩輪文字之間的空白": "the gap between two marquee rounds",
    "跑馬燈左界（px）": "Marquee left bound (px)",
    "0＝自動用畫面寬度的 1/7，讓開原片左上角的 logo":
        "0 = use 1/7 of the width, leaving room for the original video's top-left logo",
    "文字大小（px）": "Text size (px)",
    "文字描邊（px）": "Text outline (px)",
    "描邊讓字在任何畫面上都看得清": "the outline keeps the text readable on any background",
    "QR 按鈕字級": "QR caption size",
    "QR 邊長（px）": "QR size (px)",
    "越大越好掃，但佔畫面": "bigger scans better but takes screen space",
    "顯示 QR 按鈕": "Show QR button",
    "關掉就只剩跑馬燈": "turning it off leaves only the marquee",
    "按鈕文字（集數）": "Caption (episodes)",
    "集數的按鈕說明": "the caption on episode buttons",
    "顯示剩餘時間倒數": "Show remaining time",
    "QR 下方那一行「剩餘 02:57」": "the line under the QR, e.g. 02:57 left",
    "按鈕文字（過場）": "Caption (transitions)",
    "過場的按鈕說明": "the caption on transition buttons",
    "贊助連結（QR）": "Sponsor link (QR)",
    "贊助 QR 圖片（網址或路徑）": "Sponsor QR picture (URL or path)",
    "有填就用這張圖（可以放自己的 QR 圖，例如付款平台給的）；留空＝用下面的連結自動產生 QR":
        "when set this picture is used, so an operator can supply their own QR image; empty = generate a QR from the link below",
    "沒有上面的圖片時，用這個連結自動產生 QR。只畫在過場影片的右下角（集數不畫）；圖片與連結都留空＝不顯示":
        "used to generate a QR when there is no picture above. Drawn only at the bottom right of transition clips, "
        "never on episodes; empty picture and empty link = no QR",
    "顯示贊助 QR": "Show the sponsor QR",
    "取消勾選＝保留上面的連結但影片不畫 QR（過場重做後生效）":
        "unticking keeps the link above but stops drawing the QR (takes effect once the transitions are rebuilt)",
    "贊助按鈕文字": "Sponsor caption",
    "QR 下方的說明文字": "the caption under the QR",
    "黑尾門檻（秒）": "Black tail threshold (s)",
    "片尾連續黑畫面超過這個秒數就截掉。播出端看不出來，觀眾端是一片黑":
        "a black stretch this long at the tail gets trimmed; the playout cannot see it, "
        "viewers just see black",
    "黑尾容許範圍（秒）": "Black tail slack (s)",
    "黑尾結束點要落在片尾幾秒內才算數":
        "how close to the end the black must finish to count",
    "過場同時編幾個": "Parallel transition encodes",
    "頻道／清單": "Channel / playlist",
    "實際解析一次，確認填的網址是對的（不用等整條建置跑完才發現打錯）。":
        "resolves the URLs once for real, so a typo shows up immediately",
    "只接受 YouTube 家族的網址 —— 這個 API 拿使用者給的網址去呼叫 yt-dlp。":
        "only YouTube-family URLs are accepted — this API passes user URLs to yt-dlp.",
    "%s：OK　第一支 %s（%s）": "%s: OK　first video %s (%s)",
    "%s：失敗　%s": "%s: failed　%s",
    "%s：未填": "%s: empty",
    "%s：只接受 youtube.com／youtu.be 網址": "%s: only youtube.com / youtu.be URLs",
    # ── Messages at startup
    "%s 控制台：http://%s:%d/   （API %s，路徑 %s）":
        "%s console: http://%s:%d/   (API %s, path %s)",
    "拒絕啟動：--host %s 等於對外開放，必須提供 token。":
        "refusing to start: --host %s exposes it publicly, a token is required.",
    "  先產生：": "  create one first:",
    "已啟用 token 驗證（%s）": "token auth enabled (%s)",
    "按 Ctrl-C 結束": "press Ctrl-C to quit",
    "找不到 mediamtx.yml": "mediamtx.yml not found",
    "mediamtx.yml 的 hls 是 no（預設值，要用請改成 yes）":
        "hls is no in mediamtx.yml (the default; set it to yes to use this)",
    "hlsAddress 讀不出埠號：%s": "cannot read a port from hlsAddress: %s",
    "沒有直播畫面預覽：%s": "no live preview: %s",
    # ── The console's own interpreter cannot draw QR codes
    "⚠ 這個控制台用的 Python 少了 qrcode": "⚠ this console's Python has no qrcode",
    "控制台目前跑在 %s，它觸發的建置會沿用同一個 Python，轉出來的影片不會有 QR code。":
        "The console is running on %s and the builds it starts inherit the same Python, "
        "so the videos it produces would have no QR code.",
    "改用有 qrcode 的 Python 重啟控制台，或先安裝：%s":
        "restart the console with a Python that has qrcode, or install it first: %s",
    "這個控制台用的 Python 少了 qrcode，建置出來的影片不會有 QR code。"
    "改用有 qrcode 的 Python 重啟控制台，或安裝：":
        "this console's Python has no qrcode, so a build would produce videos without QR codes. "
        "Restart the console with a Python that has qrcode, or install it: ",
    "已經有另一個建置在跑，請等它結束再按。":
        "another build is already running; wait for it to finish.",
    # ── Sponsor QR block and the remaining settings labels (these were only ever written in Chinese)
    "贊助 QR（只出現在過場的右下角）": "Sponsor QR (transitions only, bottom right)",
    "贊助 QR": "Sponsor QR",
    "這是影片上實際會畫出來的樣子。集數不會有這顆 QR。":
        "This is exactly what is drawn on the video. Episodes never carry this QR.",
    "沒有設定贊助 QR（圖片與連結都是空的）—— 影片上不會出現。":
        "No sponsor QR is configured (the image and the link are both empty), so it never appears.",
    "顯示中：過場影片的右下角會出現這顆 QR，文字是「%s」。":
        "Shown: this QR appears at the bottom right of transitions, captioned %s.",
    "已隱藏：連結還留著，但影片不會畫這顆 QR（重新勾選再重建過場就會回來）。":
        "Hidden: the link is kept, but the video will not draw this QR (tick it again and rebuild the "
        "transitions to bring it back).",
    "來源：贊助 QR 圖片。改圖片、連結或開關之後，要重建過場才會反映到影片上（集數不會重做）。":
        "Source: the sponsor QR image. After changing the image, the link or the switch, rebuild the "
        "transitions for it to reach the video (episodes are not redone).",
    "來源：由贊助連結自動產生。改連結或開關之後，要重建過場才會反映到影片上（集數不會重做）。":
        "Source: generated from the sponsor link. After changing the link or the switch, rebuild the "
        "transitions for it to reach the video (episodes are not redone).",
    "介面與畫面語言": "Interface and on-screen language",
    "後台右上角的切換鈕就是改這個；畫面字樣（首播日期、QR 說明）也跟著換":
        "The switch at the top right of the console sets this; the on-screen wording (the date prefix, "
        "the QR captions) follows it.",
    "ui.lang=en 時用這一個": "used when ui.lang=en",
    "按鈕文字（集數，英文）": "Button caption (episodes, English)",
    "倒數前綴（中文）": "Countdown prefix (Chinese)",
    "中文放在秒數前面（剩餘 02:57）": "the prefix goes before the seconds in Chinese",
    "倒數後綴（英文）": "Countdown suffix (English)",
    "英文放在秒數後面（02:57 left）": "the suffix goes after the seconds in English",
    "按鈕文字（過場，英文）": "Button caption (transitions, English)",
}


def ui_lang():
    cfg = read_json(SETTINGS, {}) or {}
    return str((cfg.get("ui") or {}).get("lang", "zh")).strip().lower()


def T(zh, lang=None):
    """中文原文 -> 依語系挑字串。一次只顯示一種語言（zh／en 切換）。"""
    lang = lang or ui_lang()
    en = UI_TEXT.get(zh)
    if not en:
        return zh
    return en if lang == "en" else zh


_LOC_RE = None


def localize(text, lang=None):
    """中文原文換成英文。用「一次掃描、最長優先」的替換，避免短字串先咬到長字串
    （實測：「%s（%s 秒後）」會被「 秒」先咬掉一半）。"""
    global _LOC_RE
    lang = lang or ui_lang()
    if lang != "en":
        return text
    if _LOC_RE is None:
        keys = sorted(UI_TEXT, key=len, reverse=True)
        _LOC_RE = re.compile("|".join(re.escape(k) for k in keys))
    return _LOC_RE.sub(lambda m: T(m.group(0), lang), text)


# Only messages we generate are translated; user data (mode names, video titles, paths) is left alone.
LOC_KEYS = ("short", "why", "error", "hint", "note", "detail", "how")


def localize_obj(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and k in LOC_KEYS:
                out[k] = localize(v)
            else:
                out[k] = localize_obj(v)
        return out
    if isinstance(obj, list):
        return [localize_obj(x) for x in obj]
    return obj


def localize_schema(schema):
    """只翻欄位標題與說明，**不翻預設值** —— 預設值是要寫進 settings.json 的真字串
    （例如 date_label 的「首播日期：」），翻掉會讓存檔把中文換成英文。"""
    out = []
    for sec in schema:
        fields = []
        for f in sec[2]:
            f = list(f)
            f[1] = localize(f[1])
            if len(f) > 5 and isinstance(f[5], str):
                f[5] = localize(f[5])
            fields.append(tuple(f))
        out.append([sec[0], localize(sec[1]), fields])
    return out


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PROJECT__ 控制台</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg viewBox='0 0 100 100' xmlns='http://www.w3.org/2000/svg'><rect x='4' y='4' width='92' height='92' rx='25' fill='%230B1020'/><path d='M74 34 A29 29 0 0 0 25 36' fill='none' stroke='%2340DCCD' stroke-width='8' stroke-linecap='round'/><path d='M25 36 L24 19 L41 26Z' fill='%2340DCCD'/><path d='M26 66 A29 29 0 0 0 75 64' fill='none' stroke='%237567FF' stroke-width='8' stroke-linecap='round'/><path d='M75 64 L76 81 L59 74Z' fill='%237567FF'/><path d='M42 34 L42 66 L68 50Z' fill='%23fff'/></svg>">
<style>
:root{color-scheme:light dark}
body{font:14px/1.6 -apple-system,Helvetica,Arial,sans-serif;margin:0;padding:20px;max-width:1000px}
h1{font-size:20px;margin:0 0 4px}
h1 a{color:inherit;text-decoration:none;border-bottom:1px dotted #8888}
h1 a:hover{border-bottom-style:solid}
svg.mark{width:24px;height:24px;vertical-align:-5px;margin-right:6px}
#langsw{float:right;font-size:13px}
#langsw a{margin-left:10px;color:inherit;opacity:.55;text-decoration:none}
#langsw a.on{opacity:1;font-weight:700;border-bottom:2px solid currentColor}
h2{font-size:15px;margin:26px 0 8px;padding-bottom:4px;border-bottom:1px solid #8884}
table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:3px 8px 3px 0;vertical-align:top}
th{font-weight:600;white-space:nowrap}
.up{color:#0a0}.down{color:#c00}.dim{opacity:.65}
.sponbox{display:flex;gap:16px;align-items:flex-start;border:1px solid #8884;border-radius:8px;padding:10px 14px;margin:10px 0}
.sponqr{width:120px;height:120px;background:#fff;border-radius:6px;flex:0 0 auto}
pre{background:#8881;padding:8px;border-radius:6px;overflow:auto;max-height:240px;font-size:12px;margin:0}
textarea{width:100%;height:200px;font:12px/1.5 ui-monospace,Menlo,monospace;background:#8881;border-radius:6px;border:1px solid #8884;padding:8px}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid #8886;background:#8882;cursor:pointer;margin:2px 4px 2px 0}
button:hover{background:#8884}
button:disabled{opacity:.45;cursor:default}
button.danger{border-color:#c668}
a.btn{display:inline-block;padding:5px 12px;border-radius:6px;border:1px solid #8886;
background:#8882;text-decoration:none;color:inherit;margin:2px 4px 2px 0}
a.btn:hover{background:#8884}
a.btn.live{border-color:#0a06;font-weight:700}
input,select{font:inherit;padding:5px;border-radius:6px;border:1px solid #8886;background:#8881}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:4px 0}
.modebox{border:1px solid #8884;border-radius:8px;padding:10px 14px;margin:10px 0}
.modebox h3{margin:0 0 8px;font-size:14px}
.modebox label{display:inline-block;min-width:15em}
.modebox input[type=text]{min-width:24em}
#msg{min-height:1.6em;font-weight:600}
.ready-ok,.ready-wait,.ready-bad{border-radius:8px;padding:10px 14px;margin:8px 0;
border:1px solid;line-height:1.5}
.ready-ok{background:#0a01;border-color:#0a06}
.ready-wait{background:#fa01;border-color:#fa06}
.ready-bad{background:#c001;border-color:#c006}
.chip{font-size:12px;padding:1px 8px;border-radius:10px;margin-left:8px;border:1px solid;vertical-align:middle}
.chip-ok{background:#0a01;border-color:#0a06}
.chip-wait{background:#fa01;border-color:#fa06}
.chip-bad{background:#c001;border-color:#c006}
button.primary{font-weight:700;border-color:#0a0}
.warnbox{background:#c001;border:1px solid #c006;border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px;line-height:1.6}
.warnbox code{font-size:12px;background:#0001;padding:1px 4px;border-radius:4px}
details{border:1px solid #8884;border-radius:8px;padding:0 14px 4px;margin:10px 0}
details>summary{cursor:pointer;font-size:15px;font-weight:600;padding:8px 0;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸ ";opacity:.55;font-weight:400}
details[open]>summary::before{content:"▾ "}
details[open]>summary{border-bottom:1px solid #8884;margin-bottom:8px}
details details{border:0;padding:0;margin:6px 0}
details details>summary{font-size:13px;opacity:.9}
</style></head><body>
<div id="langsw"></div>
<h1><svg class="mark" viewBox="0 0 100 100" aria-hidden="true"><path d="M74 34 A29 29 0 0 0 25 36" fill="none" stroke="#40DCCD" stroke-width="8" stroke-linecap="round"/><path d="M25 36 L24 19 L41 26Z" fill="#40DCCD"/><path d="M26 66 A29 29 0 0 0 75 64" fill="none" stroke="#7567FF" stroke-width="8" stroke-linecap="round"/><path d="M75 64 L76 81 L59 74Z" fill="#7567FF"/><path d="M42 34 L42 66 L68 50Z" fill="currentColor"/></svg><a href="__REPO_URL__" target="_blank" rel="noopener" title="GitHub：__PROJECT__">__PROJECT__</a> 控制台</h1>
<div id="warn"></div>
<div class="dim" id="head"></div>
<div class="row" id="quick"></div>
<div id="msg"></div>

<h2>① 來源設定</h2>
<p class="dim">填這兩個網址 → 按「儲存這個模式」→ 再按下面的「開始直播」。
「驗證網址」會先實際解析一次，確認網址沒打錯（填錯不用等整場建置跑完才發現）。</p>
<div id="modes"></div>

<h2>② 開始直播</h2>
<div class="row">
<select id="mode"></select>
<button class="primary" data-need-idle onclick="actSwitch()">建置並切換（開始直播）</button>
<button data-need-idle onclick="actBuild()">只建置，不切換</button>
<button data-need-idle onclick="actScan()">只掃描來源</button>
<button data-need-idle onclick="actConcat()">重建 concat 清單</button>
<button data-need-idle onclick="actStatus()">檢查缺哪些檔案</button>
<button class="danger" id="stopbtn" onclick="actStop()">停止建置</button>
</div>
<p class="dim">第一次會下載與轉檔（每支影片數十 MB，數分鐘到數十分鐘）；已經下載過的會跳過。
切換會重啟播出端，中斷數秒。按鈕按下去是在背景跑，下面會即時顯示進度。
按「停止建置」會把整個建置連子行程（含 ffmpeg）一起停掉，並清掉被中斷的半成品；
已經轉好的檔案會留著，下次建置從缺的補。</p>
<pre id="task"></pre>

<h2>播出狀態</h2><table id="play"></table>

<h2>服務</h2><div class="row" id="svc"></div>
<p class="dim">「沒有載入」＝launchd 根本沒這個 job（例如金鑰貼好了卻沒有畫面，就是推流服務沒被載入）。
按「啟動」會把資料目錄裡的 plist 複製到 ~/Library/LaunchAgents 再 bootstrap。
系統 domain 的服務需要非互動 sudo；失敗時會顯示要加哪一條 sudoers。</p>

<h2>直播金鑰</h2>
<p class="dim">寫入 stream.key（權限 600）。金鑰只進不出，這個頁面不會把它顯示出來。</p>
<div class="row"><input type="password" id="key" size="42" placeholder="xxxx-xxxx-xxxx-xxxx-xxxx">
<button onclick="writeKey()">寫入</button></div>

<h2>進階選項</h2>
<p class="dim">平常不用打開。要調畫質與版面、看細節，或改原始 JSON 再展開。</p>
<details><summary>服務行程</summary><table id="proc"></table></details>
<details><summary>內容</summary><table id="content"></table></details>
<details><summary>日誌</summary><table id="logs"></table></details>
<details><summary>畫質與版面</summary>
<p class="dim">存檔後要重新建置才會套用到已下載的內容（改畫質等於重新轉檔）。</p>
<div id="settings"></div>
</details>
<details><summary>贊助 QR（只出現在過場的右下角）</summary>
<p class="dim">這是影片上實際會畫出來的樣子。集數不會有這顆 QR。</p>
<div id="sponbox"></div>
</details>
<details><summary>進階設定（原始 JSON）</summary>
<details>
<summary>settings.json（畫質、版面、淡化、黑尾門檻）</summary>
<div class="row"><b>settings.json</b><button onclick="saveSettings()">儲存</button></div>
<textarea id="ta-settings" spellcheck="false"></textarea>
</details>
<details>
<summary>modes.json 原始內容（上面表單沒涵蓋的欄位改這裡；存檔會整份覆蓋）</summary>
<div class="row"><b>modes.json</b><button onclick="saveModes()">儲存原始 JSON</button></div>
<textarea id="ta-modes" spellcheck="false"></textarea>
</details>
</details>
<script>
// 組合字串用：fmt("已儲存 %s：%s", a, b)。整句才翻得乾淨。
// 也支援位置參數（%1$s），翻譯時要調換順序才不會卡住。
function fmt(tpl, a, b, c){
  var all = [a, b, c];
  var i = 0;
  return String(tpl).replace(/%(\d+)\$s|%s/g, function(m, n){
    var v;
    if (n) { v = all[parseInt(n, 10) - 1]; }
    else { v = all[i]; i += 1; }
    return (v === undefined || v === null) ? "" : String(v);
  });
}

function esc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function text(id, s){ document.getElementById(id).textContent = s; }
function html(id, s){ document.getElementById(id).innerHTML = s; }
function msg(s){ text("msg", s || ""); }

// 對外開放時（--host 0.0.0.0）要帶 token。?token= 只擋得住第一次載入，
// 之後每個 /api/* 都要自己帶，否則整頁都會 401（這個 bug 在遠端模式下必現）。
var TOKEN = (function(){
  var m = location.search.match(/[?&]token=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : "";
})();

function authHeaders(extra){
  var h = extra || {};
  if (TOKEN) { h["X-Ytpl-Token"] = TOKEN; }
  return h;
}

function getJSON(url){
  return fetch(url, { headers: authHeaders() }).then(function(r){ return r.json(); });
}

function post(url, body){
  return fetch(url, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json", "X-Ytpl": "1" }),
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); });
}

    function badge(ok, s){ return '<span class="' + (ok ? "up" : "down") + '>' + esc(s) + "</span>"; }

// 標題下面那顆「看直播畫面」。位址沒變就不重畫 —— 每 5 秒重建一次的話，
// 剛好按在連結上的那一下會被換掉。
function renderQuick(s){
  var p = s.preview || {};
  var url = p.enabled ? ("http://" + location.hostname + ":" + p.port + "/" + p.path + "/") : "";
  if (url === LAST_LIVE_URL) { return; }
  LAST_LIVE_URL = url;
  html("quick", url
    ? ('<a class="btn live" href="' + esc(url) + '" target="_blank" rel="noopener">▶ 看直播畫面</a>')
    : ('<span class="dim">' + fmt("沒有直播畫面預覽：%s", esc(p.why || "")) + "</span>"));
}

// 控制台自己的 Python 能不能畫 QR code。這裡畫不出來的話，它觸發的每一次建置都畫不出來，
// 所以要在一進頁面就看到，而不是等建置跑完才發現影片沒有 QR。
function renderWarn(s){
  var el = document.getElementById("warn");
  if (!el) { return; }
  var p = s.python || {};
  var sig = p.qrcode ? "ok" : (p.executable + "|" + (p.install || ""));
  if (el.getAttribute("data-sig") === sig) { return; }
  el.setAttribute("data-sig", sig);
  if (p.qrcode) { el.innerHTML = ""; return; }
  el.innerHTML = '<div class="warnbox"><b>⚠ 這個控制台用的 Python 少了 qrcode</b><br>' +
    fmt("控制台目前跑在 %s，它觸發的建置會沿用同一個 Python，轉出來的影片不會有 QR code。", esc(p.executable)) +
    '<br>' + fmt("改用有 qrcode 的 Python 重啟控制台，或先安裝：%s", '<code>' + esc(p.install) + '</code>') +
    '</div>';
}

// 語系切換：兩顆永遠都是「中文」「English」，切到哪個就寫進 settings.json 再重載。
// 這一段由 JS 產生（不是伺服器端的字串），所以不會被語系替換影響。
function renderLang(s){
  var d = document.getElementById("langsw");
  if (!d || d.getAttribute("data-lang") === s.lang) { return; }
  d.setAttribute("data-lang", s.lang);
  d.innerHTML = "";
  [["zh", "中文"], ["en", "English"]].forEach(function(pair){
    var a = document.createElement("a");
    a.href = "#";
    a.textContent = pair[1];
    a.className = (s.lang === pair[0]) ? "on" : "";
    a.onclick = function(){
      post("/api/lang", { lang: pair[0] }).then(function(r){
        if (r.ok) { location.reload(); } else { msg(r.error || ""); }
      });
      return false;
    };
    d.appendChild(a);
  });
}

// 服務狀態：分清楚「沒載入」「載入了沒在跑」「在跑」。沒載入的可以按「啟動」。
function renderServices(s){
  var d = document.getElementById("svc");
  d.innerHTML = "";
  (s.services || []).forEach(function(x){
    var name = x.label.replace(/^com\.[a-z0-9]+\./, "");   // 前綴可能是舊的（com.ytpl.）
    var wrap = document.createElement("span");
    wrap.style.cssText = "display:inline-block;margin:0 16px 6px 0";
    var b = document.createElement("b");
    b.textContent = name;
    var st = document.createElement("span");
    st.className = (x.loaded && x.pid) ? "" : "dim";
    st.textContent = x.loaded ? (x.pid ? fmt("　執行中 pid %s", x.pid) : "　已載入（沒在跑）")
                              : "　沒有載入";
    var btn = document.createElement("button");
    if (x.loaded) {
      btn.textContent = "重啟";
      btn.onclick = function(){ svcCall(x.label, "restart"); };
    } else {
      btn.textContent = "啟動";
      btn.disabled = !x.plist;
      btn.title = x.plist ? fmt("從 %s 安裝並啟動", x.plist) : "資料目錄裡沒有這個 plist";
      btn.onclick = function(){ svcCall(x.label, "start"); };
    }
    wrap.appendChild(b); wrap.appendChild(st); wrap.appendChild(btn);
    d.appendChild(wrap);
  });
}

function svcCall(label, action){
  var name = label.replace(/^com\.[a-z0-9]+\./, "");
  post("/api/service", { label: label, action: action }).then(function(r){
    msg(r.ok ? fmt(action === "start" ? "已啟動 %s（%s）" : "已重啟 %s（%s）", name, r.how)
             : fmt("%s：%s %s", name, r.error || "", r.hint || ""));
    refresh();
  });
}

function refresh(){
  if (REFRESHING) { return; }
  REFRESHING = true;
  getJSON("/api/status").then(function(s){
    STAT_FAIL = 0;
    text("head", s.now + "　目錄 " + s.prefix);
    renderWarn(s);
    renderQuick(s);
    renderServices(s);
    var selEl = document.getElementById("mode");
    var selMode = (selEl && selEl.value) || "";
    if (!selMode && s.playing_mode && (s.ready || {})[s.playing_mode]) { selMode = s.playing_mode; }
    if (!selMode && selEl && selEl.options.length) { selMode = selEl.options[0].value; }
    if (selEl && !MODE_PICKED && s.playing_mode) {
      var hasIt = [].slice.call(selEl.options).some(function(o){ return o.value === s.playing_mode; });
      if (hasIt) { selEl.value = s.playing_mode; selMode = s.playing_mode; MODE_PICKED = true; }
    }
    for (var mk in MODE_CHIPS) {
      var st = (s.ready || {})[mk];
      if (!st) { continue; }
      MODE_CHIPS[mk].textContent = st.short;
      MODE_CHIPS[mk].className = "chip " + (st.state === "ok" ? "chip-ok" : (st.state === "building" ? "chip-wait" : "chip-bad"));
    }
    var rdTop = (s.ready || {})[selMode];
    var badgeTop = rdTop ? ((rdTop.state === "ok" ? "✅ " : "⚠ ") + selMode + "：" + rdTop.short) : "";
    text("head", s.now + "　目錄 " + s.prefix + (badgeTop ? "　·　" + badgeTop : ""));
    var p = "<tr><th>行程</th><th>狀態</th></tr>";
    s.proc.forEach(function(x){
      p += "<tr><td>" + esc(x.name) + "</td><td>" +
           (x.up ? badge(true, fmt("執行中 (%s)", x.count)) : badge(false, "沒有在跑")) +
           "</td></tr>";
    });
    html("proc", p);

    var m = s.mtx.ok
      ? ((s.mtx.ready ? "ready" : "未 ready") + "　讀者 " + s.mtx.readers +
         "　bytesReceived " + s.mtx.bytesReceived)
      : ("查不到：" + s.mtx.error);
    var r2 = "";
    r2 += "<tr><th>MediaMTX</th><td>" + badge(!!(s.mtx.ok && s.mtx.ready), m) + "</td></tr>";
    r2 += "<tr><th>單輪</th><td>" + esc((s.round.segments || 0) + " 段　" +
          (s.round.round_seconds || 0) + " 秒") + "</td></tr>";
    if (s.round.next_loop){
      r2 += "<tr><th>下次循環</th><td>" +
            fmt("%s（%s 秒後）", esc(s.round.next_loop),
                esc(s.round.loop_in_seconds)) + "</td></tr>";
    }
    html("play", r2);

    var c = "";
    c += "<tr><th>concat</th><td>" + s.content.concat_entries + " 段" +
         (s.content.concat_missing.length
           ? "　" + badge(false, "缺 " + s.content.concat_missing.join(", ")) : "") +
         "</td></tr>";
    c += "<tr><th>media</th><td>" + esc(s.content.media_size) + "</td></tr>";
    c += "<tr><th>stream.key</th><td>" +
         (s.content.stream_key.exists
           ? badge(true, fmt("已設定（%s bytes）", s.content.stream_key.bytes))
           : badge(false, "未設定")) + "</td></tr>";
    html("content", c);

    var l = "<tr><th>health</th><td><pre>" + esc((s.logs.health || []).join("\n")) + "</pre></td></tr>";
    l += "<tr><th>alerts</th><td><pre>" + esc((s.logs.alerts || []).join("\n") || "（無）") + "</pre></td></tr>";
    html("logs", l);
  }).catch(function(e){
    STAT_FAIL += 1;
    if (STAT_FAIL === 1) { msg("讀狀態失敗，正在重試…"); setTimeout(refresh, 1500); }
    else { msg(fmt("讀狀態失敗（連續 %s 次）：%s", STAT_FAIL, e)); }
  }).then(function(){ REFRESHING = false; });
}

function loadCfg(){
  getJSON("/api/schema").then(function(sc){
    SETTINGS_SCHEMA = sc.settings || [];
    return getJSON("/api/config");
  }).then(function(c){
    SETTINGS_CACHE = c.settings || {};
    document.getElementById("ta-settings").value = JSON.stringify(c.settings, null, 2);
    document.getElementById("ta-modes").value = JSON.stringify(c.modes, null, 2);
    renderSettings(SETTINGS_SCHEMA, SETTINGS_CACHE);
    renderSponsor(c.settings);
    renderModes(c.modes);
    var sel = document.getElementById("mode");
    sel.onchange = function(){ refresh(); };
    sel.innerHTML = "";
    Object.keys(c.modes || {}).filter(function(k){ return k !== "_comment"; })
      .forEach(function(k){
        var o = document.createElement("option");
        o.value = k;
        o.textContent = k + (c.modes[k].label ? "　" + c.modes[k].label : "");
        sel.appendChild(o);
      });
    refresh();
  });
}

function save(kind){
  var data;
  try { data = JSON.parse(document.getElementById("ta-" + kind).value); }
  catch (e) { return msg("JSON 有錯：" + e.message); }
  post("/api/config", { kind: kind, data: data }).then(function(r){
    msg(r.ok ? fmt("已寫入 %s（%s）", r.wrote, r.note) : ("失敗：" + (r.error || "")));
    if (r.ok) { loadCfg(); }
  }).catch(function(e){ msg("失敗：" + e); });
}

function renderSponsor(cfg){
  var ov = (cfg && cfg.overlay) || {};
  var pic = String(ov.sponsor_qr_image || "");
  var url = String(ov.sponsor_url || "");
  var show = (ov.sponsor_show === undefined) ? true : !!ov.sponsor_show;
  // The video is drawn with the caption of the console language (build_local_content picks
  // sponsor_caption or sponsor_caption_en from ui.lang), so the preview reads the same one.
  var en = (((cfg || {}).ui || {}).lang === "en");
  var cap = String((en ? ov.sponsor_caption_en : ov.sponsor_caption) || (en ? "Support" : "贊助"));
  var host = document.getElementById("sponbox");
  if (!host) { return; }
  host.innerHTML = "";
  var side = document.createElement("div");
  if (!pic && !url) {
    side.className = "dim";
    side.textContent = "沒有設定贊助 QR（圖片與連結都是空的）—— 影片上不會出現。";
    host.appendChild(side);
    return;
  }
  var img = document.createElement("img");
  img.className = "sponqr";
  img.alt = "贊助 QR";
  if (pic && /^https?:/i.test(pic)) {
    img.src = pic;
  } else {
    img.src = "/api/sponsor-qr?token=" + encodeURIComponent(TOKEN)
            + "&v=" + encodeURIComponent(pic || url);
  }
  host.appendChild(img);
  var line = document.createElement("div");
  line.className = show ? "up" : "down";
  line.textContent = show
    ? fmt("顯示中：過場影片的右下角會出現這顆 QR，文字是「%s」。", cap)
    : "已隱藏：連結還留著，但影片不會畫這顆 QR（重新勾選再重建過場就會回來）。";
  side.appendChild(line);
  var hint = document.createElement("p");
  hint.className = "dim";
  hint.textContent = pic
    ? "來源：贊助 QR 圖片。改圖片、連結或開關之後，要重建過場才會反映到影片上（集數不會重做）。"
    : "來源：由贊助連結自動產生。改連結或開關之後，要重建過場才會反映到影片上（集數不會重做）。";
  side.appendChild(hint);
  host.appendChild(side);
}
function saveSettings(){ save("settings"); }
function saveModes(){ save("modes"); }

var SETTINGS_SCHEMA = [];
var SETTINGS_CACHE = {};
var MODE_PICKED = false;
var MODE_CHIPS = {};
var STAT_FAIL = 0;
var REFRESHING = false;
var LAST_LIVE_URL = null;
var FIELDS = [
  ["label", "模式名稱（顯示用）", "text", 20, "新聞模式"],
  ["video_source", "① 播放清單網址（要播的影片）", "text", 56,
   "https://www.youtube.com/@YourChannel/videos"],
  ["shorts_url", "② 過場 shorts 網址（轉場輪播）", "text", 56,
   "https://www.youtube.com/@YourChannel/shorts"],
  ["video_limit", "影片數上限", "number", 6, ""],
  ["sort", "播放順序", "choice",
   [["source", "來源順序"], ["date-asc", "首播日期：舊→新"], ["date-desc", "首播日期：新→舊"]], ""],
  ["max_age_hours", "只播幾小時內首播的（0＝不限）", "number", 6, ""],
  ["first_batch", "先做幾支就開播（0＝全部做完才切換）", "number", 6, ""],
  ["max_seconds", "每支長度上限（秒，0＝全長）", "number", 6, ""],
  ["shorts_count", "shorts 支數", "number", 6, ""],
  ["shorts_seconds", "每支 short 長度上限（秒）", "number", 6, ""],
  ["refresh_seconds", "重新掃描間隔（秒，0＝不掃）", "number", 6, ""],
  ["shorts_passes", "一輪播幾趟（0＝用預設）", "number", 6, ""]
];
// The mode name and the two source URLs are what an operator touches; everything after them is tuning,
// and three modes in a row would otherwise put thirty inputs on screen at once.
var COMMON_FIELDS = 3;
var MODES_CACHE = {};

function renderModes(modes){
  MODES_CACHE = modes || {};
  var host = document.getElementById("modes");
  host.innerHTML = "";
  var keys = Object.keys(MODES_CACHE).filter(function(k){ return k !== "_comment"; });
  if (!keys.length) { host.textContent = "（modes.json 裡沒有可編輯的模式）"; return; }
  keys.forEach(function(mk){
    var m = MODES_CACHE[mk] || {};
    var box = document.createElement("div");
    box.className = "modebox";
    var h = document.createElement("h3");
    h.textContent = mk + (m.label ? "　" + m.label : "");
    var chip = document.createElement("span");
    chip.className = "chip";
    h.appendChild(chip);
    MODE_CHIPS[mk] = chip;
    box.appendChild(h);
    if (String(m.video_source || "").indexOf("YourChannel") >= 0) {
      var warn = document.createElement("p");
      warn.className = "down";
      warn.textContent = "來源還是範例值 @YourChannel —— 建置前請先填上真實的頻道網址（貼上後要按「儲存這個模式」才會生效）。";
      box.appendChild(warn);
    }
    if (String(m.shorts_url || "").indexOf("YourChannel") >= 0) {
      var warn2 = document.createElement("p");
      warn2.className = "down";
      warn2.textContent = "過場 shorts 網址還是範例值 @YourChannel —— 這個模式不會做過場（要有的話填上真實的 shorts 網址並存檔）。";
      box.appendChild(warn2);
    }
    var inputs = {};
    function addField(f, into){
      var row = document.createElement("div");
      row.className = "row";
      var lab = document.createElement("label");
      lab.textContent = f[1];
      var inp;
      if (f[2] === "choice") {
        // 選項可以寫 "值" 或 ["值", "顯示文字"]
        inp = document.createElement("select");
        (f[3] || []).forEach(function(c){
          var pair = Array.isArray(c) ? c : [c, c];
          var o = document.createElement("option");
          o.value = pair[0];
          o.textContent = pair[1];
          inp.appendChild(o);
        });
      } else {
        inp = document.createElement("input");
        inp.type = f[2];
        if (f[3]) { inp.size = f[3]; }
        if (f[4]) { inp.placeholder = f[4]; }
      }
      var cur = (m[f[0]] === undefined || m[f[0]] === null) ? "" : m[f[0]];
      inp.value = cur;
      // 舊的 modes.json 沒有這個 key 時，select 會變成「沒有選中」→ 顯示空白。
      // 退回第一個選項，存檔時就會把預設值寫進去。
      if (inp.tagName === "SELECT"
          && !Array.prototype.some.call(inp.options, function(o){ return o.value === String(cur); })) {
        inp.selectedIndex = 0;
      }
      row.appendChild(lab);
      row.appendChild(inp);
      into.appendChild(row);
      inputs[f[0]] = inp;
    }
    FIELDS.slice(0, COMMON_FIELDS).forEach(function(f){ addField(f, box); });
    var more = document.createElement("details");
    var msum = document.createElement("summary");
    msum.textContent = "更多設定（影片數上限、長度、shorts、掃描間隔）";
    more.appendChild(msum);
    FIELDS.slice(COMMON_FIELDS).forEach(function(f){ addField(f, more); });
    box.appendChild(more);
    var bar = document.createElement("div");
    bar.className = "row";
    var b1 = document.createElement("button");
    b1.textContent = "儲存這個模式";
    b1.onclick = function(){ saveMode(mk, inputs); };
    var b2 = document.createElement("button");
    b2.textContent = "驗證網址";
    b2.onclick = function(){ probe(inputs); };
    bar.appendChild(b1);
    bar.appendChild(b2);
    box.appendChild(bar);
    if (m.note) {
      var p = document.createElement("p");
      p.className = "dim";
      p.textContent = m.note;
      box.appendChild(p);
    }
    host.appendChild(box);
  });
}

function saveMode(mk, inputs){
  var modes = JSON.parse(JSON.stringify(MODES_CACHE));
  if (!modes[mk]) { modes[mk] = {}; }
  FIELDS.forEach(function(f){
    var raw = inputs[f[0]].value.trim();
    if (f[2] === "choice" && !raw) { return; }   // 空值不要寫進去，讓程式用預設
    if (f[2] === "number") {
      var n = parseInt(raw, 10);
      modes[mk][f[0]] = isNaN(n) ? 0 : n;
    } else {
      modes[mk][f[0]] = raw;
    }
  });
  post("/api/config", { kind: "modes", data: modes }).then(function(r){
    msg(r.ok ? fmt("已儲存 %s：%s", mk, r.note) : ("儲存失敗：" + (r.error || "")));
    if (r.ok) { loadCfg(); }
  }).catch(function(e){ msg("儲存失敗：" + e); });
}

function probe(inputs){
  msg("驗證中…（會實際解析一次，約數秒）");
  post("/api/probe", {
    video_source: inputs.video_source.value.trim(),
    shorts_url: inputs.shorts_url.value.trim()
  }).then(function(r){
    msg(r.ok ? r.summary : ("驗證失敗：" + (r.error || "")));
  }).catch(function(e){ msg("驗證失敗：" + e); });
}

function renderSettings(schema, values){
  var host = document.getElementById("settings");
  host.innerHTML = "";
  schema.forEach(function(sec){
    var box = document.createElement("div");
    box.className = "modebox";
    var h = document.createElement("h3");
    h.textContent = sec[1] + "　" + sec[0];
    box.appendChild(h);
    var inputs = {};
    sec[2].forEach(function(f){
      var row = document.createElement("div");
      row.className = "row";
      var lab = document.createElement("label");
      lab.textContent = f[1];
      var el;
      if (f[2] === "bool") {
        el = document.createElement("input");
        el.type = "checkbox";
      } else if (f[2] === "choice") {
        el = document.createElement("select");
        (f[3] || []).forEach(function(c){
          var o = document.createElement("option");
          o.value = c;
          o.textContent = c;
          el.appendChild(o);
        });
      } else {
        el = document.createElement("input");
        el.type = (f[2] === "int" || f[2] === "float") ? "number" : "text";
        if (f[2] === "float") { el.step = "0.1"; }
        if (f[2] === "text") { el.size = 14; }
        if (f[4]) { el.min = f[4][0]; el.max = f[4][1]; }
      }
      var cur = (values[sec[0]] || {})[f[0]];
      if (f[2] === "bool") { el.checked = (cur !== false); }
      else { el.value = (cur === undefined || cur === null) ? (f[6] === undefined ? "" : f[6]) : cur; }
      row.appendChild(lab);
      row.appendChild(el);
      if (f[5]) {
        var sp = document.createElement("span");
        sp.className = "dim";
        sp.textContent = f[5];
        row.appendChild(sp);
      }
      box.appendChild(row);
      inputs[f[0]] = el;
    });
    var bar = document.createElement("div");
    bar.className = "row";
    var btn = document.createElement("button");
    btn.textContent = "儲存這一段";
    btn.onclick = function(){ saveSettingsSection(sec, inputs); };
    bar.appendChild(btn);
    box.appendChild(bar);
    host.appendChild(box);
  });
}

function saveSettingsSection(sec, inputs){
  var s = JSON.parse(JSON.stringify(SETTINGS_CACHE || {}));
  var name = sec[0];
  if (!s[name]) { s[name] = {}; }
  sec[2].forEach(function(f){
    var k = f[0];
    var el = inputs[k];
    if (f[2] === "bool") { s[name][k] = !!el.checked; }
    else if (f[2] === "int" || f[2] === "float") {
      var v = (f[2] === "int") ? parseInt(el.value, 10) : parseFloat(el.value);
      if (isNaN(v)) { delete s[name][k]; }   // 清空＝刪掉這個 key，讓程式的預設值接手
      else { s[name][k] = v; }
    }
    else { s[name][k] = el.value.trim(); }
  });
  post("/api/config", { kind: "settings", data: s }).then(function(r){
    msg(r.ok ? fmt("已儲存 %s：%s", name, r.note) : ("儲存失敗：" + (r.error || "")));
    if (r.ok) { loadCfg(); }
  }).catch(function(e){ msg("儲存失敗：" + e); });
}

function act(action, extra){
  var body = { action: action, mode: document.getElementById("mode").value };
  var e = extra || {};
  for (var k in e) { body[k] = e[k]; }
  post("/api/action", body).then(function(r){
    msg(r.ok ? ("已開始：" + r.cmd) : ("無法開始：" + (r.error || "")));
    pollTask();
  });
}
function actBuild(){ act("mode-build", {}); }
function actScan(){ act("mode-build", { "scan-only": 1 }); }
function actSwitch(){ act("mode-build", { "switch": 1 }); }
function actConcat(){ act("concat", {}); }
function actStatus(){ act("status", {}); }
function actStop(){
  post("/api/action", { action: "stop" }).then(function(r){
    msg(r.ok ? ("已送出停止：" + (r.detail || "")) : ("無法停止：" + (r.error || "")));
    setTimeout(pollTask, 500);
  });
}

// 有工作在跑時把「開始」那一排按鈕鎖住，只留「停止建置」可按；反過來也一樣。
function lockButtons(t){
  var busy = !!t.running;
  var els = document.querySelectorAll("[data-need-idle]");
  for (var i = 0; i < els.length; i++) { els[i].disabled = busy; }
  var sb = document.getElementById("stopbtn");
  if (sb) { sb.disabled = !busy || !!t.stopping; }
}

function pollTask(){
  getJSON("/api/task").then(function(t){
    lockButtons(t);
    var head = (t.running ? (t.stopping ? "正在停止…　" : "執行中　") : "已完成／待機　") +
               (t.action || "") + (t.adopted ? "（webui 重啟前啟動的）" : "") +
               (t.started ? ("　" + t.started) : "") +
               (t.rc === null || t.rc === undefined ? ""
                 : fmt("　結束碼 %s%s", t.rc, t.rc < 0 ? "（被中止）" : ""));
    text("task", head + "\n\n" + (t.log || []).join("\n"));
    if (t.running) { setTimeout(pollTask, t.stopping ? 1000 : 2000); } else { refresh(); }
  });
}

function writeKey(){
  var k = document.getElementById("key").value;
  if (!k) { return msg("請先輸入金鑰"); }
  post("/api/stream-key", { key: k }).then(function(r){
    document.getElementById("key").value = "";
    msg(r.ok ? fmt("已寫入 stream.key（%s bytes）。%s", r.bytes, r.note)
             : ("失敗：" + (r.error || "")));
    refresh();
  });
}

refresh();
loadCfg();
pollTask();
setInterval(refresh, 5000);
</script></body></html>"""

# Placeholders are substituted in the string rather than with %-formatting (CSS contains things like width:100%).
PAGE = PAGE.replace("__REPO_URL__", REPO_URL).replace("__PROJECT__", PROJECT)


if __name__ == "__main__":
    sys.exit(main())
