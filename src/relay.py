#!/usr/bin/env python3
"""
方案 A：行程接力（Process Relay）v2 — yt_playlist2yt_Livestream

v2 相對 v1 的三個修正
  1. 看門狗：改用 ffmpeg 的 -progress 輸出（out_time_us）當健康指標。
     停滯超過 --watchdog 秒就砍掉重開，直播段並會重新解析 URL。
     這個指標與輸出目的地無關，所以 target 換成 YouTube RTMP 也照樣有效。
  2. 排程漂移：stop 由「實際 start + seconds」推算，下一段的 start 也從實際 start 推算，
     不再於啟動時把整條時間軸算死（v1 的已知缺陷）。
  3. 觀測噪音：改用 MediaMTX /v3/paths/list，不再讓 API 日誌被 404 灌滿。

v3（本輪新增）— 來源 URL 生命週期
   manifest URL 有 expire（YouTube 實測 21600s = 6 小時）而且綁「解析當下的
   公網 IP」，所以啟動時解析一次、之後一路沿用在 24/7 場景一定失效。
   - 每條 URL 都跟解析時間一起存（RESOLVED_AT）
   - --url-max-age：沿用上限（預設 1800s），超過就重解析
   - --url-expiry-margin：距到期不足此秒數就重解析（預設 300s）
  - live 段在 url-max-age 到點時主動 takeover 換手，換手 0.34s 而不是等它斷
  - 排定換手與故障重試分開計數（refreshes / restarts），不吃彼此的額度

v4（本輪新增）— 自有內容落地 + 反 bot 封鎖
  YouTube 對「同一個對外 IP 的匿名 player 請求」會回 LOGIN_REQUIRED
  "Sign in to confirm you're not a bot"。實測 PO Token（bgutil http provider，
  已確認真的有產出 token）與 TLS 偽裝都救不了，只有登入 session 有效。
  所以新增兩件事：
  - file 片段型態：直接播本機檔案，完全不必碰 YouTube。內容先用
    fetch_content.sh 落地一次，24/7 播出就再也不會被 bot 檢查影響，
    順便也除掉了來源 URL 6 小時過期與 googlevideo 中途 reset 兩個風險。
  - --cookies：需要直接拉 YouTube 時（含聯播）帶上 cookies.txt 通過檢查。
  - --check：不播出，只把所有來源解析過一輪並回報可用性。

用法
  python3 relay.py --dry-run
  python3 relay.py --check
  python3 relay.py --overlap 3 --observe --watchdog 3
  python3 relay.py --playlist playlist.json --target rtmp://a.rtmp.youtube.com/live2/KEY
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
EVENTS_PATH = os.path.join(LOG_DIR, "relay-events.jsonl")
GAPS_PATH = os.path.join(LOG_DIR, "relay-gaps.json")

DEFAULT_CLIENTS = ["web_embedded", "mweb", "default", "android_vr"]
FMT_VOD = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b"
FMT_LIVE = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b"

# 片段型態
#   vod / live → 走 yt-dlp 解析 YouTube 來源（需要對外網路，可能被 bot 檢查擋）
#   file       → 本機檔案，播一次就交班（自有內容落地後的主力）
#   filler     → 墊片，無限循環直到被收掉
URL_TYPES = ("vod", "live")              # 需要即時解析來源 URL
CONTENT_TYPES = ("vod", "live", "file")  # 參與 takeover 交班的一般內容片段

# 來源端抗斷線：googlevideo 實測會在中途 reset 連線（seg-*.log 的
# "Connection reset by peer" / "Stream ends prematurely at X, should be Y"），
# 造成整段提前結束。帶上瀏覽器 UA 與 Referer，並允許 ffmpeg 用 Range 續傳。
HTTP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HTTP_IN = ["-user_agent", HTTP_UA,
           "-headers", "Referer: https://www.youtube.com/\r\n",
           "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "5"]

# vod/live 實際 -t 比排定長度多播這麼久。舊 publisher 若恰在交班點自己收工，
# 新 publisher 從 RTMP 連上到 MediaMTX 真正換手還要 1~2s（實測），那就是縫。
PUBLISH_TAIL = 20.0

# 交班前多久先對舊 runner 放手（見 main() 註解）。
HANDOFF_LEAD = 0.75

# 交接時舊 publisher 最多再多撐這麼久，等 MediaMTX takeover 把它踢掉。
SUPERSEDE_GRACE = 30.0

STOP = threading.Event()
RUNNERS = []          # 只留「本輪」的 runner，24/7 長跑不會無限累積
RESOLVED = {}
RESOLVED_AT = {}          # 這條 URL 是什麼時候解析出來的（秒）
RUNTIME = {}


def now():
    return time.time()


def stamp(t=None):
    t = now() if t is None else t
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def log(msg, level="INFO"):
    print("[%s] %-5s %s" % (stamp(), level, msg), flush=True)


def emit(rec):
    rec["ts"] = round(now(), 3)
    rec["wall"] = stamp()
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def short(cmd):
    home = os.path.expanduser("~")
    out = []
    for c in cmd:
        c = c.replace(home, "~").replace(HERE, ".")
        if len(c) > 120:
            c = c[:117] + "..."
        out.append(c)
    return " ".join(out)


def sleep_until(ts):
    while not STOP.is_set():
        d = ts - now()
        if d <= 0:
            return
        time.sleep(min(d, 0.2))


def api_path_of(target):
    """rtmp://127.0.0.1:1935/live/test -> live/test；非本機 MediaMTX 回 None。"""
    try:
        tail = target.split("://", 1)[1]
        hostport, rest = tail.split("/", 1)
        if "127.0.0.1" not in hostport and "localhost" not in hostport:
            return None
        return rest
    except Exception:
        return None


def yt_resolve(url, fmt, clients, timeout=90, cookies=None):
    """回傳 (urls, client)。urls 可能是 1 個（漸進式）或 2 個（video+audio）。

    cookies 是 cookies.txt 路徑。這個對外 IP 被 YouTube 標記成 bot 之後，
    匿名請求一律 LOGIN_REQUIRED，只有帶登入 session 的 cookie 能過。
    """
    for c in clients:
        cmd = ["yt-dlp", "--no-warnings", "--no-playlist"]
        if cookies:
            cmd += ["--cookies", cookies]
        cmd += ["--extractor-args", "youtube:player_client=" + c,
                "-f", fmt, "-g", url]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log("resolve timeout client=%s" % c, "WARN")
            continue
        if p.returncode == 0 and p.stdout.strip():
            urls = [ln for ln in p.stdout.strip().splitlines() if ln.startswith("http")]
            if urls:
                return urls, c
        tail = (p.stderr.strip().splitlines() or [""])[-1]
        log("resolve FAIL client=%s :: %s" % (c, tail[:110]), "WARN")
    return None, None


def url_expiry(url):
    """抓 manifest URL 的到期時間（unix 秒），抓不到回 None。

    YouTube 兩種寫法都出現過：path 形式 /expire/1789518101/ 與 query 形式
    ?expire=1789518101。本機實測直播 m3u8 是 path 形式、值 = 解析當下 + 21600s。
    """
    if not url:
        return None
    if "/expire/" in url:
        head = url.split("/expire/", 1)[1].split("/", 1)[0]
        if head.isdigit():
            return float(head)
    q = url.split("?", 1)[1] if "?" in url else ""
    for part in q.split("&"):
        if part.startswith("expire="):
            v = part.split("=", 1)[1]
            if v.isdigit():
                return float(v)
    return None


def url_freshness(url, resolved_at, max_age, margin):
    """回傳 (可用, 原因)。不可用＝這條 URL 不該再拿去開 ffmpeg。

    兩個獨立風險：
      1. expire——YouTube 實測 TTL 6 小時，到期後 segment 請求會被拒。
      2. 綁來源 IP——URL 內含解析當下的公網 IP，換 IP（PPPoE 重撥／換主機）
         就整條失效。所以「解析時間」必須跟著 URL 一起記，不能只記 URL。
    """
    exp = url_expiry(url)
    if exp is not None and exp - now() <= margin:
        return False, "expire 只剩 %.0fs" % (exp - now())
    if max_age > 0 and (now() - resolved_at) > max_age:
        return False, "已解析 %.0fs 前" % (now() - resolved_at)
    return True, ""


def resolve_set(i, seg, clients, label=None):
    """解析來源，並把「解析時間」跟 URL 一起存起來。回傳 urls（失敗 None）。

    24/7 場景只有一組 RESOLVED 不夠——沒有解析時間就無法判斷新舊。
    """
    fmt = seg.get("format") or (FMT_LIVE if seg["type"] == "live" else FMT_VOD)
    a = now()
    urls, client = yt_resolve(seg["url"], fmt, clients,
                              cookies=RUNTIME.get("cookies"))
    if urls:
        RESOLVED[i] = urls
        RESOLVED_AT[i] = now()
    tail = ("  <- " + label) if label else ""
    log("resolve %-6s %s client=%s (%.2fs)%s"
        % (seg.get("id", i), "OK" if urls else "FAIL", client, now() - a, tail),
        "INFO" if urls else "ERROR")
    emit({"event": "resolve", "seg": str(seg.get("id", i)),
          "ok": bool(urls), "client": client, "reason": label or "",
          "ms": int((now() - a) * 1000)})
    return urls


def build_cmd(seg, target, src_urls):
    stype = seg["type"]
    mode = seg.get("mode", "copy")
    head = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:1"]
    inargs = []

    if stype == "filler":
        path = seg["path"]
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        inargs += ["-stream_loop", "-1", "-re", "-i", path]
    elif stype == "file":
        # 本機檔案：用 -re 壓成即時速度，否則 ffmpeg 會用超快速度灌完
        # MediaMTX、20s 的 PUBLISH_TAIL 一瞬間就跑完，整個交班節奏就崩了。
        path = seg["path"]
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        inargs += ["-re", "-i", path]
    elif stype in URL_TYPES:
        if not src_urls:
            raise RuntimeError("no source url for segment " + str(seg.get("id")))
        if stype == "vod":
            inargs.append("-re")        # -re 必須緊接在它對應的 -i 之前
        for u in src_urls:
            if u.startswith("http"):
                inargs += HTTP_IN
            inargs += ["-i", u]
    else:
        raise RuntimeError("unknown segment type: " + stype)

    outargs = []
    if stype in ("vod", "live") and len(src_urls) == 2:
        outargs += ["-map", "0:v:0", "-map", "1:a:0"]
    if mode == "copy":
        outargs += ["-c", "copy"]
    else:
        outargs += ["-c:v", "h264_videotoolbox", "-b:v", "6000k",
                    "-maxrate", "6000k", "-bufsize", "12000k",
                    "-pix_fmt", "yuv420p", "-g", "60",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    if seg.get("seconds"):
        # vod/live 多播 PUBLISH_TAIL 秒。排定交班點上若讓舊 publisher 自己收工
        # （-t 到點或來源 EOF），新 publisher 從 RTMP 連上到 MediaMTX 真正換手
        # 還有 1~2s（實測 04:39:03 conn opened -> 04:39:05 online），那就是縫。
        # 多播一點讓它活到被 takeover；何時真正停由 run() 的 stop_at/supersede 決定。
        tail = PUBLISH_TAIL if stype in CONTENT_TYPES else 0.0
        outargs += ["-t", str(seg["seconds"] + tail)]
    outargs += ["-f", "flv", "-flvflags", "no_duration_filesize", target]
    return head + inargs + outargs


class Observer(threading.Thread):
    """以 MediaMTX API 高頻取樣，量測接收端真正有 stream 的時間軸。

    v2 改走 /v3/paths/list：路徑不存在時只會少一筆，不會在 MediaMTX 日誌留下 404。
    """

    def __init__(self, path, interval=0.05):
        super().__init__(daemon=True)
        self.path = path
        self.interval = interval
        self.samples = []
        self.api_fail = 0

    def _get(self):
        url = "http://127.0.0.1:9997/v3/paths/list"
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                data = json.loads(r.read().decode())
        except Exception:
            self.api_fail += 1
            return None
        for item in data.get("items") or []:
            if item.get("name") == self.path:
                return item
        return {"ready": False, "readers": [], "bytesReceived": 0}

    def run(self):
        while not STOP.is_set():
            d = self._get()
            if d is not None:
                self.samples.append((now(), bool(d.get("ready")),
                                     len(d.get("readers") or []),
                                     d.get("bytesReceived", 0)))
            STOP.wait(self.interval)

    def windows(self):
        """回傳 ready=False 的離線時段 [(t0, t1, 秒數), ...]。"""
        out, t0 = [], None
        for (t, ready, _r, _b) in self.samples:
            if not ready and t0 is None:
                t0 = t
            elif ready and t0 is not None:
                out.append((t0, t, t - t0))
                t0 = None
        if t0 is not None and self.samples:
            out.append((t0, self.samples[-1][0], self.samples[-1][0] - t0))
        return out

    def flow_windows(self, threshold=0.0):
        """回傳「位元組停止成長 >= threshold 秒」的時段。

        ready 只代表 MediaMTX 上掛了 publisher；MediaMTX takeover 會讓畫面
        凍住時 ready 依然是 True（見 overlap_test.py 實測）。真正代表「畫面
        在動」的是 bytesReceived 有沒有持續長大。

        注意 HLS 是「整段拉、整段送」的突發式傳輸，來源正常時每隔一個片段
        長度（常見 2~6s）就會有一段零成長，這不是故障。因此判讀要看
        「最長零成長間隔」有沒有明顯超過片段長度，而不是數有幾段。
        """
        out, stop0, last_b = [], None, None
        for (t, _ready, _r, b) in self.samples:
            if last_b is not None and b == last_b:
                if stop0 is None:
                    stop0 = t
            else:
                if stop0 is not None and (t - stop0) >= threshold:
                    out.append((stop0, t, t - stop0))
                stop0 = None
            last_b = b
        if stop0 is not None and self.samples:
            end = self.samples[-1][0]
            if end - stop0 >= threshold:
                out.append((stop0, end, end - stop0))
        return out

class SegmentRunner(threading.Thread):
    """一個 segment 的完整生命週期：啟動 -> 看門狗 -> 到點停止。"""

    def __init__(self, i, spec, target, clients, args, start_at):
        super().__init__(daemon=True, name="seg-" + str(spec.get("id", i)))
        self.i = i
        self.spec = spec
        self.sid = str(spec.get("id", i))
        self.target = target
        self.clients = clients
        self.args = args
        self.start_at = start_at
        self.stop_at = start_at + float(spec["seconds"])
        self.proc = None
        self.restarts = 0
        self.refreshes = 0
        self.refresh_try = 0.0
        self.superseded = False
        self.stopping = False
        self._prog = {"proc": None, "last_value": -1.0, "last": start_at,
                      "seen": False}
        self.log_path = os.path.join(LOG_DIR, "seg-%s.log" % self.sid)
        self.logfh = None
        self.slate_proc = None
        self.degraded = False
        self.retrying = False
        if args.restart_mode == "auto":
            self.restart_mode = "takeover" if api_path_of(target) else "cut"
        else:
            self.restart_mode = args.restart_mode

    # -------------------------------------------------------------- lifecycle
    def run(self):
        if not self._launch():
            return
        while not STOP.is_set() and not self.stopping:
            time.sleep(0.25)
            if now() >= self.stop_at:
                self._stop("scheduled")
                return
            if self.superseded:
                # 已被下一段接手：等 MediaMTX takeover 收掉舊 publisher 就退場，
                # 在那之前不可以自己收工。
                if all(p is None or p.poll() is not None
                       for p in (self.proc, self.slate_proc)):
                    return
                continue
            why = self._refresh_due()
            if why:
                log("%-6s 主動換手更新來源 URL（%s）" % (self.sid, why))
                emit({"event": "url_refresh", "seg": self.sid, "reason": why})
                self.refresh_try = now()
                self._restart(planned=True)
                continue
            self._health()

    def supersede(self):
        """下一段已接手同一條路徑：這一段（含看門狗）就此放手，讓接收端自己踢掉它。

        這裡必須同時把 stop_at 往後推。run() 的主迴圈先判 stop_at、再判 superseded，
        而 stop_at 正好落在交接點上；不放寬的話舊 publisher 會在到點時自己 SIGINT
        收工（MediaMTX 日誌看到 closed: EOF），新 publisher 這時才剛開始開來源連線，
        中間就留下實測 1.1～2.7s 的縫。收工改由 MediaMTX 的 takeover
        （closing existing publisher）決定，新 publisher 一接上就換手。
        """
        self.superseded = True
        self.stop_at = max(self.stop_at, now() + SUPERSEDE_GRACE)

    # --------------------------------------------------------- url lifecycle
    def _fresh_urls(self):
        """取這一段目前該用的來源 URL；過期或太舊就就地重解析。

        啟動前一律驗：manifest URL 有 expire、又綁解析當下的公網 IP，
        "啟動時解析一次、之後一路沿用" 在 24/7 場景一定會踩到。
        """
        if self.spec.get("direct"):
            return [self.spec["url"]] if self.spec.get("url") else None
        if self.spec["type"] not in ("vod", "live"):
            return None
        urls = RESOLVED.get(self.i)
        at = RESOLVED_AT.get(self.i, 0.0)
        if urls and at:
            ok, why = url_freshness(urls[0], at, self.args.url_max_age,
                                    self.args.url_expiry_margin)
            if ok:
                return urls
            log("%-6s 來源 URL 失效（%s），重解析" % (self.sid, why), "WARN")
            emit({"event": "url_stale", "seg": self.sid, "reason": why})
        return resolve_set(self.i, self.spec, self.clients, label="URL 更新")

    def _refresh_due(self):
        """回傳該主動換手的理由，不需要就回 None。

        只對 live 生效：VOD 中途重開會從頭播，那是 bug 不是修復。
        24/7 聯播靠這個在 URL 到期前就把 ffmpeg 換成新的，換手走 takeover
        所以接收端幾乎不斷（見 v4.2 §2.2：overlap=3 時 0.34s）。
        """
        if self.spec.get("direct") or self.spec["type"] != "live":
            return None
        if self.args.url_max_age <= 0:
            return None
        at = max(RESOLVED_AT.get(self.i, 0.0), self.refresh_try)
        if not at:
            return None
        urls = RESOLVED.get(self.i) or [""]
        exp = url_expiry(urls[0])
        hard = min([at + self.args.url_max_age]
                   + ([exp - self.args.url_expiry_margin] if exp else []))
        if now() >= hard:
            return ("已解析 %.0fs / 到期剩 %.0fs"
                    % (now() - at, (exp - now()) if exp else -1))
        return None

    # -------------------------------------------------------------- launching
    def _launch(self):
        if self.spec["type"] in ("vod", "live"):
            urls = self._fresh_urls()
            if not urls:
                log("skip %s: 無可用來源" % self.sid, "ERROR")
                emit({"event": "skip", "seg": self.sid})
                return False
        else:
            urls = None
        if urls and self.spec.get("seconds"):
            exp = url_expiry(urls[0])
            if exp and exp - now() < self.spec["seconds"] + \
                    self.args.url_expiry_margin:
                log("%-6s 警告：URL 剩 %.0fs 短於本段 %ss + 餘裕"
                    % (self.sid, exp - now(), self.spec["seconds"]), "WARN")
        cmd = build_cmd(self.spec, self.target, urls)
        self.logfh = open(self.log_path, "ab")
        tag = "  (restart #%d)" % self.restarts if self.restarts else ""
        log("start %-6s %-5s%s" % (self.sid, self.spec["type"], tag))
        log("      " + short(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=self.logfh, stdin=subprocess.DEVNULL)
        self._watch_progress(self.proc)
        emit({"event": "publish_start", "seg": self.sid, "pid": self.proc.pid,
              "restart": self.restarts})
        return True

    def _watch_progress(self, proc):
        """每個 ffmpeg 行程各自一份計數狀態。

        換手時新舊兩個行程會短暫並存；若共用同一組 last_value，舊行程臨死前
        讀到的舊大數值會把新行程的小數值鎖死，看門狗就會誤判成停滯。
        """
        st = {"proc": proc, "last_value": -1.0, "last": now(), "seen": False}
        self._prog = st
        threading.Thread(target=self._read_progress, args=(proc, st),
                         daemon=True).start()

    def _read_progress(self, proc, st):
        """ffmpeg 停滯時會不斷重印同一個 out_time_us。

        因此只有「數值確實變大」才算是活著的證據；單純看到這一行不算。
        """
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("out_time_us="):
                    continue
                try:
                    v = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                if v > st["last_value"]:
                    st["last_value"] = v
                    st["last"] = now()
                    if v > 0:
                        st["seen"] = True
        except Exception as exc:
            # 讀不到 progress 等於看門狗瞎了，不能靜默吞掉。
            if proc.poll() is None and not STOP.is_set():
                log("%-6s progress 讀取中斷: %r" % (self.sid, exc), "WARN")

    # -------------------------------------------------------------- watchdog
    def _health(self):
        p = self.proc
        if p is None:
            return
        if p.poll() is not None:
            if self.stopping or STOP.is_set():
                return
            if self.restarts < self.args.max_restarts:
                emit({"event": "early_exit", "seg": self.sid, "rc": p.returncode})
                log("%-6s 行程提前結束 rc=%s" % (self.sid, p.returncode), "WARN")
                self._recover()
            return
        st = self._prog
        limit = self.args.watchdog if st["seen"] else self.args.startup_grace
        stalled = now() - st["last"]
        if limit > 0 and stalled > limit:
            if self.restarts >= self.args.max_restarts:
                return
            emit({"event": "watchdog", "seg": self.sid,
                  "stalled_s": round(stalled, 2), "mode": self.restart_mode})
            log("%-6s 輸出停滯 %.1fs（門檻 %.1fs）" % (self.sid, stalled, limit),
                "WARN")
            self._recover(stalled)
            self._prog["last"] = now()

    def _restart(self, planned=False):
        """planned=True 是排定的換手（URL 更新），不算失敗重試。

        兩者必須分開計數：--url-max-age 30 分鐘的 24/7 聯播一天會換手 48 次，
        若共用 max_restarts 額度，真正的故障就沒有重試機會了。
        """
        old = self.proc
        if planned:
            self.refreshes += 1
        else:
            self.restarts += 1
        if self.spec["type"] in ("vod", "live") and not self.spec.get("direct"):
            resolve_set(self.i, self.spec, self.clients,
                        label="預先更新" if planned else "重試")
        if self.restart_mode == "takeover":
            # 先讓新 publisher 接手同一條路徑，再收掉舊的 -> 接收端零斷點。
            if not self._launch():
                return
            time.sleep(0.5)
            self._terminate(old)
        else:
            self._terminate(old)
            self._launch()

    def _recover(self, stalled=None):
        """來源失效時：先用墊片接管同一條路徑，再在背景重試來源。

        直接重開同一條來源沒有意義——來源還在斷，重開只是再斷一次。
        所以先切墊片（接收端 0.34s 換手，見 relay-gaps 實測），等來源真的活了再切回來。
        """
        if not self.args.filler_on_stall or self.spec["type"] == "filler":
            self._restart()
            return
        old = self.proc
        self.restarts += 1
        if (self.retrying and self.slate_proc is not None
                and self.slate_proc.poll() is None):
            return                      # 墊片還在跑、也已在等來源，不必再動
        self._launch_slate()
        self.degraded = True
        if self.restart_mode != "takeover":
            self._terminate(old)
        # takeover：不主動收掉舊 publisher。墊片接上同一條路徑時 MediaMTX 會自己
        # 把舊的踢掉（closing existing publisher），換手才不會在接收端留下 1.5s 的縫。
        if not self.retrying:
            self.retrying = True
            threading.Thread(target=self._retry_loop, daemon=True).start()

    def _launch_slate(self):
        spec = {"id": self.sid + "-slate", "type": "filler",
                "path": self.args.filler_on_stall}
        cmd = build_cmd(spec, self.target, None)
        log("%-6s 切墊片 %s（%s）" % (self.sid, self.args.filler_on_stall,
                                     self.restart_mode))
        self.slate_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=self.logfh,
                                           stdin=subprocess.DEVNULL)
        # stdout 必須接管：-progress 是寫到 stdout，設成 DEVNULL 會讓
        # 看門狗讀不到任何東西（而且例外會被吞掉，變成靜默失效）。
        self.proc = self.slate_proc
        self._watch_progress(self.slate_proc)
        emit({"event": "slate", "seg": self.sid, "pid": self.slate_proc.pid})

    def _probe(self):
        """輕量探測來源是否還活著：抓幾秒就收，能收完就算活。

        回傳可用的 URL 清單（失敗回 None）。順手把新解析的 URL 帶回去，
        免得恢復後還拿舊的、可能已經過期的 manifest URL 重開。
        """
        if self.spec.get("direct"):
            url = self.spec.get("url")
            urls = [url] if url else None
        else:
            fmt = self.spec.get("format") or (
                FMT_LIVE if self.spec["type"] == "live" else FMT_VOD)
            urls, _c = yt_resolve(self.spec["url"], fmt, self.clients)
        if not urls:
            return None
        url = urls[0]
        if ".m3u8" in url.split("?")[0]:
            # 直播 HLS 一旦卡住，伺服器仍會回應舊清單，用下載探測會誤判成「活」。
            # 所以改要求播放清單必須往前走（見 _hls_advancing）。
            return urls if self._hls_advancing(url) else None
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
               "-i", url, "-t", str(self.args.probe_seconds),
               "-c", "copy", "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL,
                               timeout=self.args.probe_seconds + 20)
        except subprocess.TimeoutExpired:
            return None
        return urls if r.returncode == 0 else None

    def _hls_advancing(self, url):
        """HLS 直播的存活判斷：播放清單必須「往前走」，光是讀得到不算。

        來源卡住時舊的 playlist 照樣能下載、也能解出好幾秒的既有 segment，
        這就是探測的假陽性來源；要比對兩次取樣之間 media-sequence 與最後
        一個 segment 是否改變。
        """
        snap = []
        for i in range(2):
            try:
                with urllib.request.urlopen(url, timeout=6) as r:
                    txt = r.read().decode("utf-8", "replace")
            except Exception as exc:
                log("%-6s 探測讀取失敗 %s" % (self.sid, exc), "WARN")
                return False
            seq, last = None, None
            for ln in txt.splitlines():
                ln = ln.strip()
                if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    seq = ln.split(":", 1)[1].strip()
                elif ln.startswith("#EXT-X-ENDLIST"):
                    return True          # 已收播的 VOD 片段，仍可正常播出
                elif ln and not ln.startswith("#"):
                    last = ln
            snap.append((seq, last))
            if i == 0:
                time.sleep(self.args.probe_wait)
        return snap[0] != snap[1]

    def _retry_loop(self):
        while not STOP.is_set() and not self.stopping and not self.superseded:
            time.sleep(self.args.retry_interval)
            if STOP.is_set() or self.stopping or self.superseded:
                break
            if now() >= self.stop_at:
                break
            urls = self._probe()
            if not urls:
                emit({"event": "probe_fail", "seg": self.sid})
                continue
            RESOLVED[self.i] = urls
            RESOLVED_AT[self.i] = now()
            ok = self._launch()
            emit({"event": "probe_ok", "seg": self.sid, "launched": ok})
            if not ok:
                continue
            time.sleep(0.5)
            self._terminate(self.slate_proc)
            self.slate_proc = None
            self.degraded = False
            log("%-6s 來源恢復，切回直播" % self.sid)
            break
        self.retrying = False

    # -------------------------------------------------------------- shutdown
    def _stop(self, reason):
        self.stopping = True
        suffix = "  restarts=%d" % self.restarts if self.restarts else ""
        if self.refreshes:
            suffix += "  refreshes=%d" % self.refreshes
        log("stop  %-6s (%s%s)" % (self.sid, reason, suffix))
        p = self.proc
        self._terminate(p)
        self._terminate(self.slate_proc)
        emit({"event": "publish_stop", "seg": self.sid, "reason": reason,
              "rc": p.returncode if p else None, "restarts": self.restarts})

    @staticmethod
    def _terminate(proc):
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def prepare_job(i, seg, at):
    sleep_until(at)
    if STOP.is_set():
        return
    resolve_set(i, seg, RUNTIME["clients"], label="排程前置")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist.json"))
    ap.add_argument("--target", default=None)
    ap.add_argument("--overlap", type=float, default=0.0)
    ap.add_argument("--resolve-lead", type=float, default=4.0)
    ap.add_argument("--clients", default=None)
    ap.add_argument("--only", default=None, help="逗號分隔的 segment id")
    ap.add_argument("--observe", action="store_true", help="用 MediaMTX API 量測縫隙")
    ap.add_argument("--cookies", default=None,
                    help="YouTube cookies.txt 路徑（來源被 bot 檢查擋住時用）。"
                         "留空則自動找 <專案>/cookies.txt")
    ap.add_argument("--check", action="store_true",
                    help="只解析所有來源並回報可用性，不播出")
    ap.add_argument("--loop", type=int, default=1, metavar="N",
                    help="整份清單重複幾輪（0 = 無限，24/7 用）。"
                         "輪與輪之間走同一套 takeover 交班，不必重啟行程")
    ap.add_argument("--flow-threshold", type=float, default=5.0,
                    help="bytesReceived 零成長幾秒算異常（預設 5，須大於來源片段長度）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watchdog", type=float, default=3.0,
                    help="輸出停滯幾秒就重開該段（0 = 關閉）")
    ap.add_argument("--startup-grace", type=float, default=12.0,
                    help="首個 progress 出現前的容忍秒數")
    ap.add_argument("--max-restarts", type=int, default=6)
    ap.add_argument("--filler-on-stall", default="assets/transition.mp4",
                    help="來源停滯時先接管成這個墊片（空字串 = 停用）")
    ap.add_argument("--url-max-age", type=float, default=1800.0,
                    help="來源 URL 最長沿用秒數，超過就重新解析；live 段還會在"
                         "同一時間點主動換手（0 = 不限，預設 30 分鐘）")
    ap.add_argument("--url-expiry-margin", type=float, default=300.0,
                    help="manifest URL 到期前幾秒就先換掉（預設 300）")
    ap.add_argument("--retry-interval", type=float, default=8.0,
                    help="來源失效後每隔幾秒重試一次")
    ap.add_argument("--probe-seconds", type=float, default=3.0,
                    help="探測來源時先抓幾秒")
    ap.add_argument("--probe-wait", type=float, default=4.0,
                    help="HLS 探測時，兩次讀取播放清單之間等幾秒")
    ap.add_argument("--restart-mode", choices=["auto", "takeover", "cut"],
                    default="auto",
                    help="auto：本機 MediaMTX 走 takeover（零斷點），其餘 cut")
    args = ap.parse_args()

    with open(args.playlist, encoding="utf-8") as fh:
        pl = json.load(fh)
    segs = pl["segments"]
    if args.only:
        keep = set(args.only.split(","))
        segs = [s for s in segs if s["id"] in keep]
    if not segs:
        raise SystemExit("沒有可播的 segment")
    target = args.target or pl["target"]
    clients = (args.clients.split(",") if args.clients
               else pl.get("clients") or DEFAULT_CLIENTS)
    RUNTIME["clients"] = clients

    # cookies 優先序：命令列 > playlist 的 cookies 欄位 > 專案根目錄的 cookies.txt
    cookies = args.cookies or pl.get("cookies") or ""
    if not cookies:
        auto = os.path.join(HERE, "cookies.txt")
        cookies = auto if os.path.exists(auto) else ""
    if cookies:
        cookies = os.path.expanduser(cookies)
    RUNTIME["cookies"] = cookies or None

    log("target=%s" % target)
    log("cookies=%s" % (cookies or "(無)"))
    log("segments=%d overlap=%.1fs watchdog=%.1fs clients=%s"
        % (len(segs), args.overlap, args.watchdog, ",".join(clients)))

    if args.dry_run:
        for i, seg in enumerate(segs):
            urls = ["<url>"]
            log("  plan %-8s %-6s %ss" % (seg["type"], seg.get("id", i),
                                          seg.get("seconds")))
            log("  cmd  " + short(build_cmd(seg, target, urls)))
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)

    if args.check:
        ok = bad = skip = 0
        for i, seg in enumerate(segs):
            if seg["type"] not in URL_TYPES:
                skip += 1
                log("check %-10s %-6s 略過（本機檔案/墊片）"
                    % (seg.get("id", i), seg["type"]))
                continue
            urls = resolve_set(i, seg, clients, label="check")
            if urls:
                ok += 1
                exp = url_expiry(urls[0])
                log("check %-10s OK   %d 條 URL%s"
                    % (seg.get("id", i), len(urls),
                       ("，expire 剩 %.0fs" % (exp - now())) if exp else ""))
            else:
                bad += 1
                log("check %-10s FAIL（來源不可用）" % (seg.get("id", i)), "ERROR")
        log("check 完成：OK=%d FAIL=%d SKIP=%d" % (ok, bad, skip))
        return 0 if bad == 0 else 2

    emit({"event": "run_start", "target": target, "overlap": args.overlap,
          "segments": len(segs), "watchdog": args.watchdog})

    obs = None
    if args.observe:
        p = api_path_of(target)
        if p:
            obs = Observer(p)
            obs.start()
            log("observer started on MediaMTX path '%s'" % p)
        else:
            log("--observe 需要本機 MediaMTX target，已停用", "WARN")

    for i, seg in enumerate(segs):
        # "direct": true 代表 url 已經是可直接餵給 ffmpeg 的位址，跳過 yt-dlp 解析。
        if seg.get("direct") and seg["type"] in ("vod", "live"):
            RESOLVED[i] = [seg["url"]]

    t_start = now()
    prev = None
    lap = 0
    try:
        while not STOP.is_set() and (args.loop == 0 or lap < args.loop):
            RUNNERS.clear()
            for i, seg in enumerate(segs):
                if prev is None:
                    start_at = now() + 1.5
                else:
                    start_at = (prev.start_at
                                + float(prev.spec["seconds"]) - args.overlap)
                if seg["type"] in URL_TYPES and not seg.get("direct"):
                    threading.Thread(target=prepare_job,
                                     args=(i, seg, start_at - args.resolve_lead),
                                     daemon=True).start()
                if prev is not None:
                    # 提早放手：舊 runner 一走到 stop_at 就 SIGINT 舊 publisher，而新
                    # publisher 從 RTMP 連上到 MediaMTX 真正換手還有 1~2s（實測）。在
                    # 交班前 HANDOFF_LEAD 秒先 supersede，舊 publisher 才活得到 takeover。
                    sleep_until(start_at - HANDOFF_LEAD)
                    prev.supersede()
                sleep_until(start_at)
                if STOP.is_set():
                    break
                if prev is not None:
                    prev.supersede()
                r = SegmentRunner(i, seg, target, clients, args, start_at)
                RUNNERS.append(r)
                r.start()
                prev = r
            lap += 1
        for r in list(RUNNERS):
            r.join(timeout=max(1.0, r.stop_at - now()) + 30.0)
    except KeyboardInterrupt:
        log("KeyboardInterrupt，收工", "WARN")
    finally:
        STOP.set()
        for r in list(RUNNERS):
            for p in (r.proc, r.slate_proc):
                if p is not None and p.poll() is None:
                    p.send_signal(signal.SIGINT)
        for r in list(RUNNERS):
            for p in (r.proc, r.slate_proc):
                if p is None:
                    continue
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

    elapsed = now() - t_start
    restarts = sum(r.restarts for r in RUNNERS)
    emit({"event": "run_end", "elapsed": round(elapsed, 3), "restarts": restarts})

    if obs is not None:
        time.sleep(0.4)
        wins = obs.windows()
        total_off = sum(w[2] for w in wins)
        fwins = obs.flow_windows(0.0)
        longest = max((w[2] for w in fwins), default=0.0)
        bad = [w for w in fwins if w[2] >= args.flow_threshold]
        log("觀測取樣 %d 筆 / 涵蓋 %.2fs (api_fail=%d)"
            % (len(obs.samples), elapsed, obs.api_fail))
        if not wins:
            log("接收端離線時段：0 段（整場連續）", "OK")
        else:
            for (a, b, d) in wins:
                log("接收端離線 %s -> %s = %.3fs" % (stamp(a), stamp(b), d), "WARN")
            log("接收端離線總計 %.3fs（%d 段）" % (total_off, len(wins)), "WARN")
        log("資料零成長間隔 %d 段，最長 %.3fs（HLS 是整段拉整段送，"
            "來源正常時每隔一個片段長度本來就會空一段）"
            % (len(fwins), longest), "INFO")
        if bad:
            for (a, b, d) in bad:
                log("零成長 >= %.1fs：%s -> %s = %.3fs"
                    % (args.flow_threshold, stamp(a), stamp(b), d), "WARN")
        else:
            log("沒有 >= %.1fs 的零成長區間" % args.flow_threshold, "OK")
        with open(GAPS_PATH, "w", encoding="utf-8") as fh:
            json.dump({"overlap": args.overlap, "elapsed": round(elapsed, 3),
                       "samples": len(obs.samples),
                       "watchdog": args.watchdog, "restarts": restarts,
                       "flow_threshold": args.flow_threshold,
                       "offline_windows": [
                           {"from": stamp(a), "to": stamp(b), "seconds": round(d, 3)}
                           for (a, b, d) in wins],
                       "offline_total": round(total_off, 3),
                       "stagnation_max": round(longest, 3),
                       "stagnation_total": round(sum(w[2] for w in fwins), 3),
                       "stagnation_over_threshold": [
                           {"from": stamp(a), "to": stamp(b), "seconds": round(d, 3)}
                           for (a, b, d) in bad]},
                      fh, ensure_ascii=False, indent=2)
        log("gap 報告寫入 %s" % GAPS_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
