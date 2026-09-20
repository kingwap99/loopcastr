#!/usr/bin/env python3
"""ytpl 本機控制台：狀態、設定、建置動作。

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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

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


def set_prefix(path):
    global PREFIX, MEDIA, LOGS, MODES, SETTINGS, PLAYLIST, PLAYLIST_LOCAL
    global CONCAT, PLAYOUT_LOG, KEYFILE, TASK_LOG
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
    PLAYOUT_LOG = os.path.join(LOGS, "playout.log")
    TASK_LOG = os.path.join(LOGS, "webui-task.log")

PLAYOUT_START = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) 第 ([0-9]+) 次啟動")


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
        ("countdown", "顯示剩餘時間倒數", "bool", None, None,
         "QR 下方那一行「01/03　剩餘 02:57」", True),
        ("transition_caption", "按鈕文字（過場）", "text", None, None,
         "過場的按鈕說明", "去追劇"),
        ("sponsor_url", "贊助連結（QR）", "text", None, None,
         "填了就固定在畫面右下角顯示 QR；留空＝不顯示", ""),
        ("sponsor_caption", "贊助按鈕文字", "text", None, None,
         "QR 下方的說明文字", "贊助"),
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
    ("playout ffmpeg", "concat.txt"),
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


def round_info():
    """單輪長度（讀 playlist-local.json）與下一次循環的時間點。"""
    d = read_json(PLAYLIST_LOCAL)
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
        info["playout_run"] = int(last.group(2))
        info["next_loop"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nxt))
        info["loop_in_seconds"] = int(nxt - now)
    return info


def content_info():
    entries = []
    if os.path.exists(CONCAT):
        for line in tail(CONCAT, 600):
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
        "proc": procs(),
        "mtx": mtx(api, path_name),
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
    rc, txt = sh(["launchctl", "print", "gui/%d/com.ytpl.playout" % os.getuid()], timeout=5)
    if rc != 0:
        rc, txt = sh(["sudo", "-n", "launchctl", "print", "system/com.ytpl.playout"], timeout=5)
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
    if running:
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
TASK = {"running": False, "action": "", "started": "", "pid": None, "rc": None}
TASK_LOCK = threading.Lock()


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
    cmd, err = build_cmd(action, body)
    if err:
        return {"ok": False, "error": err}
    with TASK_LOCK:
        if TASK["running"]:
            return {"ok": False, "error": "已經有工作在跑：%s" % TASK["action"]}
        os.makedirs(LOGS, exist_ok=True)
        with open(TASK_LOG, "w", encoding="utf-8") as fh:
            fh.write("$ %s\n" % " ".join(cmd))
        out = open(TASK_LOG, "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT)
        TASK.update({"running": True, "action": action,
                     "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "pid": proc.pid, "rc": None, "cmd": " ".join(cmd)})

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
            "hint": "system domain 需要非互動 sudo，請在 /etc/sudoers.d/ytpl-webui 加："
                    "  <你的帳號> ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/"
                    + label}


# ── HTTP ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "ytpl-webui"
    api = "http://127.0.0.1:9997"
    path_name = "live/main"
    token = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
        path = self.path.split("?", 1)[0]
        if not self._authed():
            return self._send(401, {"error": "需要 token"})
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status(self.api, self.path_name))
        if path == "/api/config":
            return self._send(200, {"settings": read_json(SETTINGS, {}),
                                    "modes": read_json(MODES, {})})
        if path == "/api/schema":
            return self._send(200, {"settings": SETTINGS_SCHEMA})
        if path == "/api/task":
            return self._send(200, task_state())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
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
            return self._send(200, restart_service(body.get("label") or ""))
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
    print("ytpl 控制台：http://%s:%d/   （API %s，路徑 %s）"
          % (a.host, a.port, a.api, a.path_name), flush=True)
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
PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ytpl 控制台</title>
<style>
:root{color-scheme:light dark}
body{font:14px/1.6 -apple-system,Helvetica,Arial,sans-serif;margin:0;padding:20px;max-width:1000px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:26px 0 8px;padding-bottom:4px;border-bottom:1px solid #8884}
table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:3px 8px 3px 0;vertical-align:top}
th{font-weight:600;white-space:nowrap}
.up{color:#0a0}.down{color:#c00}.dim{opacity:.65}
pre{background:#8881;padding:8px;border-radius:6px;overflow:auto;max-height:240px;font-size:12px;margin:0}
textarea{width:100%;height:200px;font:12px/1.5 ui-monospace,Menlo,monospace;background:#8881;border-radius:6px;border:1px solid #8884;padding:8px}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid #8886;background:#8882;cursor:pointer;margin:2px 4px 2px 0}
button:hover{background:#8884}
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
button.primary{font-weight:700;border-color:#0a0}
</style></head><body>
<h1>ytpl 控制台</h1>
<div class="dim" id="head"></div>
<div id="msg"></div>

<h2>① 來源設定</h2>
<p class="dim">填這兩個網址 → 按「儲存這個模式」→ 再按下面的「開始直播」。
「驗證網址」會先實際解析一次，確認網址沒打錯（填錯不用等整場建置跑完才發現）。</p>
<div id="modes"></div>

<h2>② 開始直播</h2>
<div id="ready"></div>
<div class="row">
<select id="mode"></select>
<button class="primary" onclick="actSwitch()">建置並切換（開始直播）</button>
<button onclick="actBuild()">只建置，不切換</button>
<button onclick="actScan()">只掃描來源</button>
<button onclick="actConcat()">重建 concat 清單</button>
<button onclick="actStatus()">檢查缺哪些檔案</button>
</div>
<p class="dim">第一次會下載與轉檔（每支影片數十 MB，數分鐘到數十分鐘）；已經下載過的會跳過。
切換會重啟播出端，中斷數秒。按鈕按下去是在背景跑，下面會即時顯示進度。</p>
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
<p class="dim">system domain 需要非互動 sudo；失敗時會顯示要加哪一條 sudoers。</p>
<script>
var LABELS = ["com.ytpl.mediamtx","com.ytpl.playout","com.ytpl.publish","com.ytpl.health","com.ytpl.refresh"];

function esc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function text(id, s){ document.getElementById(id).textContent = s; }
function html(id, s){ document.getElementById(id).innerHTML = s; }
function msg(s){ text("msg", s || ""); }

function post(url, body){
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Ytpl": "1" },
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); });
}

    function badge(ok, s){ return '<span class="' + (ok ? "up" : "down") + '>' + esc(s) + "</span>"; }

    function renderReady(r){
      var host = document.getElementById("ready");
      if (!r) { host.innerHTML = ""; return; }
      var cls = { "ok": "ready-ok", "building": "ready-wait", "not_switched": "ready-wait" }[r.state] || "ready-bad";
      var icon = { "ok": "✅", "building": "⏳", "not_switched": "🟡" }[r.state] || "⚠";
      host.innerHTML = '<div class="' + cls + '"><b>' + icon + " " + esc(r.short) + "</b><br>" + esc(r.detail) + "</div>";
    }


function refresh(){
  fetch("/api/status").then(function(r){ return r.json(); }).then(function(s){
    text("head", s.now + "　目錄 " + s.prefix);
    var selEl = document.getElementById("mode");
    var selMode = (selEl && selEl.value) || "";
    if (selEl && !MODE_PICKED && s.playing_mode) {
      var hasIt = [].slice.call(selEl.options).some(function(o){ return o.value === s.playing_mode; });
      if (hasIt) { selEl.value = s.playing_mode; selMode = s.playing_mode; MODE_PICKED = true; }
    }
    renderReady((s.ready || {})[selMode]);
    var rdTop = (s.ready || {})[selMode];
    var badgeTop = rdTop ? ((rdTop.state === "ok" ? "✅ " : "⚠ ") + rdTop.short) : "";
    if (badgeTop) { text("head", s.now + "　目錄 " + s.prefix + "　·　" + badgeTop); }
    var p = "<tr><th>行程</th><th>狀態</th></tr>";
    s.proc.forEach(function(x){
      p += "<tr><td>" + esc(x.name) + "</td><td>" +
           (x.up ? badge(true, "執行中 (" + x.count + ")") : badge(false, "沒有在跑")) +
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
      r2 += "<tr><th>下次循環</th><td>" + esc(s.round.next_loop) + "（" +
            esc(s.round.loop_in_seconds) + " 秒後）</td></tr>";
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
           ? badge(true, "已設定（" + s.content.stream_key.bytes + " bytes）")
           : badge(false, "未設定")) + "</td></tr>";
    html("content", c);

    var l = "<tr><th>health</th><td><pre>" + esc((s.logs.health || []).join("\n")) + "</pre></td></tr>";
    l += "<tr><th>alerts</th><td><pre>" + esc((s.logs.alerts || []).join("\n") || "（無）") + "</pre></td></tr>";
    html("logs", l);
  }).catch(function(e){ msg("讀狀態失敗：" + e); });
}

function loadCfg(){
  fetch("/api/schema").then(function(r){ return r.json(); }).then(function(sc){
    SETTINGS_SCHEMA = sc.settings || [];
    return fetch("/api/config");
  }).then(function(r){ return r.json(); }).then(function(c){
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
        o.textContent = k + (c.modes[k].label ? "（" + c.modes[k].label + "）" : "");
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
    msg(r.ok ? ("已寫入 " + r.wrote + "（" + r.note + "）") : ("失敗：" + (r.error || "")));
  }).catch(function(e){ msg("失敗：" + e); });
}
function saveSettings(){ save("settings"); }
function saveModes(){ save("modes"); }

var SETTINGS_SCHEMA = [];
var SETTINGS_CACHE = {};
var MODE_PICKED = false;
var FIELDS = [
  ["label", "模式名稱（顯示用）", "text", 20, "新聞模式"],
  ["video_source", "① 播放清單網址（要播的影片）", "text", 56,
   "https://www.youtube.com/@YourChannel/videos"],
  ["shorts_url", "② 過場 shorts 網址（轉場輪播）", "text", 56,
   "https://www.youtube.com/@YourChannel/shorts"],
  ["video_limit", "影片數上限", "number", 6, ""],
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
    h.textContent = mk + (m.label ? "（" + m.label + "）" : "");
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
    msg(r.ok ? ("已儲存 " + mk + "：" + r.note) : ("儲存失敗：" + (r.error || "")));
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
    h.textContent = sec[1] + "（" + sec[0] + "）";
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
    msg(r.ok ? ("已儲存 " + name + "：" + r.note) : ("儲存失敗：" + (r.error || "")));
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

function pollTask(){
  fetch("/api/task").then(function(r){ return r.json(); }).then(function(t){
    var head = (t.running ? "執行中　" : "已完成／待機　") + (t.action || "") +
               (t.started ? ("　" + t.started) : "") +
               (t.rc === null || t.rc === undefined ? "" : ("　結束碼 " + t.rc));
    text("task", head + "\n\n" + (t.log || []).join("\n"));
    if (t.running) { setTimeout(pollTask, 2000); } else { refresh(); }
  });
}

function writeKey(){
  var k = document.getElementById("key").value;
  if (!k) { return msg("請先輸入金鑰"); }
  post("/api/stream-key", { key: k }).then(function(r){
    document.getElementById("key").value = "";
    msg(r.ok ? ("已寫入 stream.key（" + r.bytes + " bytes）。" + r.note)
             : ("失敗：" + (r.error || "")));
    refresh();
  });
}

function mkSvc(){
  var d = document.getElementById("svc");
  d.innerHTML = "";
  LABELS.forEach(function(l){
    var b = document.createElement("button");
    b.textContent = "重啟 " + l.replace("com.ytpl.", "");
    b.onclick = function(){
      post("/api/service", { label: l }).then(function(r){
        msg(r.ok ? ("已重啟（" + r.how + "）") : ((r.error || "") + " " + (r.hint || "")));
        refresh();
      });
    };
    d.appendChild(b);
  });
}

refresh();
loadCfg();
pollTask();
mkSvc();
setInterval(refresh, 5000);
</script></body></html>"""


if __name__ == "__main__":
    sys.exit(main())
