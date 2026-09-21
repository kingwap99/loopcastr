#!/usr/bin/env python3
"""loopcastr 本機控制台：狀態、設定、建置動作。

設計原則
  - 只用標準庫。跟主程式一樣「clone 下來就能跑」，不必先建 venv。
  - 預設只綁 127.0.0.1。要對外開放必須自己帶 token。
  - 不以 root 執行，也不保管任何密碼：需要特權的動作只試 sudo -n（非互動），
    失敗就明確告訴你要加哪一條 sudoers，不會把密碼餵進程式。
  - 寫入類動作只做兩件事：改設定檔、呼叫既有 script。不重寫底層邏輯。

用法
  python3 webui.py                      # http://127.0.0.1:8787
  python3 webui.py --port 9000
  python3 webui.py --host 0.0.0.0 --token-file webui-token   # 對外必帶 token
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

HERE = os.path.dirname(os.path.abspath(__file__))

# 控制台標題連到專案本身。只有這一份是專案自己的位址，不需要參數化。
REPO_URL = "https://github.com/kingwap99/loopcastr"
PROJECT = "loopcastr"

# 程式碼放哪裡（HERE）與資料放哪裡（PREFIX）分開。
#   安裝後：src/ 會攤平到安裝目錄，兩者相同，一切都在 PREFIX 底下。
#   從 repo 跑：程式在 src/，設定也在 src/，資料（media、logs）在安裝目錄。
# 所以每個檔案都用 pick() 兩邊找，找不到才落在 PREFIX。
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


# ── 小工具 ──────────────────────────────────────────────────────────
def sh(cmd, timeout=6):
    """跑一個指令並回傳 (rc, 輸出)。逾時或找不到指令都當成失敗，不丟例外。"""
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
    """原子寫入，並留一份 .bak。設定檔壞掉會讓整條鏈路起不來，所以不做半套。"""
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
    """去掉 concat 清單每行外層的引號（不寫死引號字元，省得在原始碼裡打架）。"""
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


# ── 來源網址驗證 ────────────────────────────────────────────────────
def _is_youtube(url):
    """只接受 YouTube 家族的網址 —— 這個 API 會拿使用者給的網址去呼叫 yt-dlp。"""
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = url.split("//", 1)[1].split("/", 1)[0].lower()
    host = host.split("@")[-1].split(":")[0]
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def probe_sources(video_source, shorts_url):
    """實際解析一次，確認填的網址是對的（不用等整條建置跑完才發現打錯）。"""
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


# ── 設定表單的 schema ───────────────────────────────────────────────
# (段落, 標題, [(key, 欄位標籤, 型別, 選項, 範圍, 說明)])
# 表單由這份 schema 產生，所以新增旋鈕只要加一行；型別支援 text／int／float／bool／choice。
SETTINGS_SCHEMA = [
    # (段落, 標題, [(key, 欄位標籤, 型別, 選項, 範圍, 說明, 預設值)])
    # 表單由這份 schema 產生：新增旋鈕只要加一行。型別支援 text／int／float／bool／choice。
    # 「預設值」是 settings.json 沒有這個 key 時表單要顯示什麼，也是程式的內建預設
    # （兩邊必須一致，否則表單會顯示一個跟實際行為不同的數字）。
    ("ui", "語言", [
        ("lang", "介面與畫面語言", "choice", ["zh", "en"], None,
         "後台右上角的切換鈕就是改這個；畫面字樣（首播日期、QR 說明）也跟著換", "zh"),
    ]),
    ("media", "畫質與流量", [
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
         "QR 下方那一行「01/03　剩餘 02:57」", True),
        ("countdown_prefix", "倒數前綴（中文）", "text", None, None,
         "中文放在秒數前面（剩餘 02:57）", "剩餘 "),
        ("countdown_suffix_en", "倒數後綴（英文）", "text", None, None,
         "英文放在秒數後面（02:57 left）", " left"),
        ("transition_caption", "按鈕文字（過場）", "text", None, None,
         "過場的按鈕說明", "去追劇"),
        ("transition_caption_en", "按鈕文字（過場，英文）", "text", None, None, "", "Watch more"),
        ("sponsor_url", "贊助連結（QR）", "text", None, None,
         "填了就固定在畫面右下角顯示 QR；留空＝不顯示", ""),
        ("sponsor_caption", "贊助按鈕文字", "text", None, None,
         "QR 下方的說明文字", "贊助"),
        ("sponsor_caption_en", "贊助按鈕文字（英文）", "text", None, None, "", "Support"),
        ("sponsor_code", "贊助碼", "text", None, None,
         "填入指定值會關閉贊助 QR（留空＝正常顯示）", ""),
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


# ── 狀態 ────────────────────────────────────────────────────────────
# 用 pgrep 而不是 launchctl：查 system domain 的服務需要 root，而這支程式刻意
# 不以 root 執行。行程在不在、日誌有沒有在動，一樣看得出來。
PROCS = [
    ("mediamtx", "mediamtx"),
    ("playout", "playout.sh"),
    # 播出端的 ffmpeg 一定帶 -stream_loop（concat 循環），用它才不會只在播預設清單時才對得上
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


# 服務（launchd job）清單。前端不再自己列一份，一律用 /api/status 回傳的。
# label 前綴不寫死：改名前（loopcastr 之前叫 ytpl）的安裝是 com.ytpl.*，
# 所以看這個目錄裡實際存在的 plist 決定用哪個前綴。
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
        # 系統 domain 的 job 不會出現在 `launchctl list`。
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
    # 讀「播出端實際載入的那份 concat」：寫死 concat.txt 的話，播 test／news 時會顯示 0 段。
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


def status(api, path_name):
    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prefix": PREFIX,
        "lang": ui_lang(),
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


# ── 開播檢查 ────────────────────────────────────────────────────────
# 「檔案轉好了沒、播出端切換了沒、串流通了沒」是操作者最常問的三件事，
# 直接算成一句結論顯示在頁面上，不要讓他自己讀 log。
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


# ── 動作（背景執行，一次一件）────────────────────────────────────────
TASK = {"running": False, "action": "", "started": "", "pid": None, "rc": None,
        "mode": "", "stopping": False, "adopted": False}
TASK_LOCK = threading.Lock()


# ── 行程樹（停止建置用）──────────────────────────────────────────────
# 為什麼要看整棵樹：webui 拉起的 mode_build 會再開 build_local_content，後者再
# 開 ffmpeg。只殺最上層的話，ffmpeg 會變成孤兒繼續吃 CPU、繼續寫檔。
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
    # launchd 的 refreshwatch 也會拉 mode_build，那是另一棵樹（我們不是它的父行程）。
    # 同一個模式的一起收掉，否則按了停止，背景還有一個建置在跑。
    if action == "mode-build" and mode:
        for pid, m in find_build_pids(only_here=True).items():
            if pid in targets or pid == os.getpid():
                continue
            if m == mode:
                targets.append(pid)
    targets = sorted(set(targets))

    # 先把「正在寫哪些檔案」記下來，殺掉之後行程就問不到了。
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
        # 先擋掉註定失敗的建置：跑一場要下載數百 MB、花好幾分鐘。
        if not src:
            return None, "%s 還沒填「頻道或播放清單網址」" % mode
        if "YourChannel" in src:
            return None, ("%s 的來源還是範例值 @YourChannel，請先到「來源設定」填真實的頻道網址"
                          % mode)
        if not _is_youtube(src):
            return None, "%s 的來源不是 YouTube 網址：%s" % (mode, src)
        cmd = [sys.executable, os.path.join(HERE, "mode_build.py"), "--mode", mode]
        for flag in ("scan-only", "deploy-only", "skip-transitions"):
            if body.get(flag):
                cmd.append("--" + flag)
        if body.get("switch"):
            cmd.append("--switch")
        return cmd, ""
    if action == "concat":
        return [sys.executable, os.path.join(HERE, "make_concat_list.py"),
                PLAYLIST_LOCAL, "-o", CONCAT, "--base-dir", HERE], ""
    if action == "status":
        return [sys.executable, os.path.join(HERE, "build_local_content.py"),
                "--playlist", PLAYLIST, "--status"], ""
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
    # 建置一定要知道自己屬於哪個模式，停止時才知道要清哪個暫存目錄。
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
        # start_new_session：讓整棵建置樹自成一個 process group。停止時殺一組
        # 就夠，不會誤殺 webui 自己（它跟 webui 同組過）。
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
    st["log"] = tail(TASK_LOG, 200)
    return st


# ── 需要特權的動作 ──────────────────────────────────────────────────
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
    # 已經載入過的 job 再 bootstrap 會失敗，那就用 kickstart 讓它跑起來。
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
        if path == "/api/task":
            return self._send(200, task_state())
        return self._send(404, {"error": "not found"})

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
        # 只收帶自訂標頭的 JSON：跨站表單無法帶自訂標頭，這一條同時擋掉 CSRF。
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
                    help="資料目錄（預設＝本檔所在目錄）。安裝後不需指定；"
                         "直接從 repo 跑時指向套件根目錄")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api", default=os.environ.get("API", "http://127.0.0.1:9997"))
    ap.add_argument("--path-name", default=os.environ.get("PATH_NAME", "live/main"))
    ap.add_argument("--token-file", default=os.path.join(HERE, "webui-token"))
    a = ap.parse_args()
    set_prefix(a.prefix)
    # 重啟前由這個控制台拉起的建置還活著，先把狀態認回來（不然會允許再按一次）。
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


# PAGE 必須在 if __name__ 之前定義：以腳本執行時那一行會直接進入
# serve_forever()，寫在它後面的定義都來不及跑到（實測踩過：GET / 回空的）。
# ── 語言（後台介面）─────────────────────────────────────────────────
# 做法：原始碼一律寫中文，回給瀏覽器之前把整份字串換掉（HTML、JS 字面值、
# 以及 API 回的 JSON 都是）。這樣不必在頁面裡散佈佔位符，翻譯表也只有一處。
# 注意：英文翻譯裡不要出現雙引號或反斜線 —— 字串會直接塞進 HTML／JS／JSON。
UI_TEXT = {
    # ── 頁面骨架
    " 控制台": " console",
    "① 來源設定": "1. Sources",
    "② 開始直播": "2. Go live",
    "播出狀態": "Playout status",
    "服務行程": "Service processes",
    "內容": "Content",
    "日誌": "Logs",
    "畫質與版面": "Quality and layout",
    "進階設定（原始 JSON）": "Advanced (raw JSON)",
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
    # ── 按鈕與表單
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
    "語系只能是 zh 或 en": "the language must be zh or en",
    "新聞模式": "News mode",
    "（modes.json 裡沒有可編輯的模式）": "(no editable modes in modes.json)",
    # ── 狀態與訊息
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
    # ── 動作與錯誤
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
    # ── settings.json 表單
    "畫質與流量": "Quality and traffic",
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
    "QR 下方那一行「01/03　剩餘 02:57」": "the line under the QR, e.g. 01/03  02:57 left",
    "按鈕文字（過場）": "Caption (transitions)",
    "過場的按鈕說明": "the caption on transition buttons",
    "贊助連結（QR）": "Sponsor link (QR)",
    "填了就固定在畫面右下角顯示 QR；留空＝不顯示":
        "when set, a QR is pinned to the bottom-right; empty = hidden",
    "贊助按鈕文字": "Sponsor caption",
    "QR 下方的說明文字": "the caption under the QR",
    "贊助碼": "Sponsor code",
    "填入指定值會關閉贊助 QR（留空＝正常顯示）":
        "filling in the magic value hides the sponsor QR (empty = normal)",
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
    # ── 啟動時的訊息
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


# 只翻「我們自己產生的訊息欄位」，不動使用者資料（模式名稱、影片標題、路徑）。
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
</style></head><body>
<div id="langsw"></div>
<h1><svg class="mark" viewBox="0 0 100 100" aria-hidden="true"><path d="M74 34 A29 29 0 0 0 25 36" fill="none" stroke="#40DCCD" stroke-width="8" stroke-linecap="round"/><path d="M25 36 L24 19 L41 26Z" fill="#40DCCD"/><path d="M26 66 A29 29 0 0 0 75 64" fill="none" stroke="#7567FF" stroke-width="8" stroke-linecap="round"/><path d="M75 64 L76 81 L59 74Z" fill="#7567FF"/><path d="M42 34 L42 66 L68 50Z" fill="currentColor"/></svg><a href="__REPO_URL__" target="_blank" rel="noopener" title="GitHub：__PROJECT__">__PROJECT__</a> 控制台</h1>
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
<h2>服務行程</h2><table id="proc"></table>
<h2>內容</h2><table id="content"></table>
<h2>日誌</h2><table id="logs"></table>

<h2>畫質與版面</h2>
<p class="dim">存檔後要重新建置才會套用到已下載的內容（改畫質等於重新轉檔）。</p>
<div id="settings"></div>

<h2>進階設定（原始 JSON）</h2>
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

<h2>直播金鑰</h2>
<p class="dim">寫入 stream.key（權限 600）。金鑰只進不出，這個頁面不會把它顯示出來。</p>
<div class="row"><input type="password" id="key" size="42" placeholder="xxxx-xxxx-xxxx-xxxx-xxxx">
<button onclick="writeKey()">寫入</button></div>

<h2>服務</h2><div class="row" id="svc"></div>
<p class="dim">「沒有載入」＝launchd 根本沒這個 job（例如金鑰貼好了卻沒有畫面，就是推流服務沒被載入）。
按「啟動」會把資料目錄裡的 plist 複製到 ~/Library/LaunchAgents 再 bootstrap。
系統 domain 的服務需要非互動 sudo；失敗時會顯示要加哪一條 sudoers。</p>
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
    renderQuick(s);
    renderLang(s);
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
  }).catch(function(e){ msg("失敗：" + e); });
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
  ["max_age_hours", "只播幾小時內首播的（0＝不限）", "number", 6, ""],
  ["max_seconds", "每支長度上限（秒，0＝全長）", "number", 6, ""],
  ["shorts_count", "shorts 支數", "number", 6, ""],
  ["shorts_seconds", "每支 short 長度上限（秒）", "number", 6, ""],
  ["refresh_seconds", "重新掃描間隔（秒，0＝不掃）", "number", 6, ""],
  ["shorts_passes", "一輪播幾趟（0＝用預設）", "number", 6, ""]
];
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
      warn.textContent = "來源還是範例值 @YourChannel —— 建置前請先填上真實的頻道網址。";
      box.appendChild(warn);
    }
    var inputs = {};
    FIELDS.forEach(function(f){
      var row = document.createElement("div");
      row.className = "row";
      var lab = document.createElement("label");
      lab.textContent = f[1];
      var inp = document.createElement("input");
      inp.type = f[2];
      if (f[3]) { inp.size = f[3]; }
      if (f[4]) { inp.placeholder = f[4]; }
      inp.value = (m[f[0]] === undefined || m[f[0]] === null) ? "" : m[f[0]];
      row.appendChild(lab);
      row.appendChild(inp);
      box.appendChild(row);
      inputs[f[0]] = inp;
    });
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

# 佔位符在字串裡換掉，不用 %-格式化（CSS 裡有 width:100% 這種東西）。
PAGE = PAGE.replace("__REPO_URL__", REPO_URL).replace("__PROJECT__", PROJECT)


if __name__ == "__main__":
    sys.exit(main())
