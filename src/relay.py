#!/usr/bin/env python3
"""
Option A: process relay v2 - yt_playlist2yt_Livestream

Three fixes in v2 over v1
  1. Watchdog: use the ffmpeg -progress output (out_time_us) as the health signal.
     A stall longer than --watchdog seconds kills and restarts it, and a live segment re-resolves its URL.
     The signal is independent of the output destination, so it works with a YouTube RTMP target too.
  2. Schedule drift: stop is derived from the real start plus seconds, and the next start from the real start too,
     instead of fixing the whole timeline at startup (a known flaw in v1).
  3. Observation noise: use MediaMTX /v3/paths/list so the API log is no longer flooded with 404s.

v3 (added this round) - source URL lifetime
   A manifest URL carries an expire (measured 21600s = 6 hours on YouTube) and is bound to the public IP
   at resolve time, so resolving once at startup and reusing it forever is bound to fail in a 24/7 setting.
   - Every URL is stored together with its resolve time (RESOLVED_AT)
   - --url-max-age: reuse limit (default 1800s), re-resolved beyond it
   - --url-expiry-margin: re-resolve when the time left to expiry is under this (default 300s)
  - A live segment takes over actively when url-max-age is reached: a 0.34s handover instead of waiting for a drop
  - Planned handovers and failure retries are counted separately (refreshes / restarts) so neither eats the other's budget

v4 (added this round) - landing your own content and beating the bot block
  YouTube answers LOGIN_REQUIRED for anonymous player requests from the same public IP:
  "Sign in to confirm you are not a bot". Measured: neither a PO Token (bgutil http provider, verified to
  really produce a token) nor TLS impersonation helps; only a logged-in session works.
  So two things were added:
  - A file segment type: play a local file directly, never touching YouTube. Land the content once with
    fetch_content.sh and 24/7 playout can no longer be affected by the bot check, which also removes
    the 6-hour source URL expiry and mid-stream googlevideo resets.
  - --cookies: pass cookies.txt when pulling from YouTube directly (relaying included) to get through the check.
  - --check: do not broadcast; resolve every source once and report availability.

Usage
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

# Segment types
#   vod / live -> resolve a YouTube source with yt-dlp (needs internet and may hit the bot check)
#   file       -> a local file, handed over after one play (the main type once content is landed)
#   filler     -> filler, looping forever until it is taken over
URL_TYPES = ("vod", "live")              # needs a live source URL resolution
CONTENT_TYPES = ("vod", "live", "file")  # ordinary content segments that take part in a handover

# Source-side resilience: measured, googlevideo resets the connection mid-stream (in the seg-*.log
# "Connection reset by peer" / "Stream ends prematurely at X, should be Y"），
# which ends the segment early. Send a browser UA and Referer and let ffmpeg resume with Range.
HTTP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HTTP_IN = ["-user_agent", HTTP_UA,
           "-headers", "Referer: https://www.youtube.com/\r\n",
           "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "5"]

# vod/live actually play this much longer than the scheduled length. If the old publisher finishes right at
# the handover point, the new publisher still needs 1-2s from connecting over RTMP to MediaMTX really switching (measured), and that is a gap.
PUBLISH_TAIL = 20.0

# How long before a handover to release the old runner (see the comment in main()).
HANDOFF_LEAD = 0.75

# At a handover the old publisher lingers at most this long, waiting for the MediaMTX takeover to drop it.
SUPERSEDE_GRACE = 30.0

STOP = threading.Event()
RUNNERS = []          # keeps only this round runners, so a long 24/7 run cannot accumulate them
RESOLVED = {}
RESOLVED_AT = {}          # when this URL was resolved (seconds)
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
    """rtmp://127.0.0.1:1935/live/test -> live/test; returns None for a non-local MediaMTX."""
    try:
        tail = target.split("://", 1)[1]
        hostport, rest = tail.split("/", 1)
        if "127.0.0.1" not in hostport and "localhost" not in hostport:
            return None
        return rest
    except Exception:
        return None


def yt_resolve(url, fmt, clients, timeout=90, cookies=None):
    """Return (urls, client). urls may be 1 (progressive) or 2 (video+audio).

    cookies is the path to cookies.txt. Once YouTube has flagged this public IP as a bot,
    anonymous requests always get LOGIN_REQUIRED and only a cookie with a logged-in session gets through.
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
    """Read the expiry of a manifest URL (unix seconds); None when it cannot be read.

    YouTube has used both forms: a path form /expire/1789518101/ and a query form
    ?expire=1789518101. Measured locally, a live m3u8 uses the path form with the value at resolve time plus 21600s.
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
    """Return (usable, reason). Not usable means this URL must not be opened by ffmpeg again.

    Two independent risks:
      1. expire: a measured TTL of 6 hours on YouTube, after which segment requests are refused.
      2. bound to the source IP: the URL contains the public IP at resolve time, so a new IP (PPPoE redial, another host)
         invalidates the whole thing. That is why the resolve time must be stored with the URL, not just the URL.
    """
    exp = url_expiry(url)
    if exp is not None and exp - now() <= margin:
        return False, "expires in %.0fs" % (exp - now())
    if max_age > 0 and (now() - resolved_at) > max_age:
        return False, "resolved %.0fs ago" % (now() - resolved_at)
    return True, ""


def resolve_set(i, seg, clients, label=None):
    """Resolve a source and store the resolve time with the URL. Returns urls (None on failure).

    A single RESOLVED set is not enough in a 24/7 setting: without the resolve time there is no way to tell old from new.
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
        # Local files: pace them with -re, otherwise ffmpeg floods
        # MediaMTX at full speed, the 20s PUBLISH_TAIL is over in an instant and the handover rhythm collapses.
        path = seg["path"]
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        inargs += ["-re", "-i", path]
    elif stype in URL_TYPES:
        if not src_urls:
            raise RuntimeError("no source url for segment " + str(seg.get("id")))
        if stype == "vod":
            inargs.append("-re")        # -re must come immediately before the -i it belongs to
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
        # vod/live play PUBLISH_TAIL seconds longer. If the old publisher finishes on its own at the scheduled
        # handover point (-t reached or source EOF), the new publisher still needs time from connecting over RTMP
        # to MediaMTX really switching (measured 04:39:03 conn opened -> 04:39:05 online), and that is a gap.
        # Playing a little longer keeps it alive until the takeover; when it really stops is decided by stop_at/supersede in run().
        tail = PUBLISH_TAIL if stype in CONTENT_TYPES else 0.0
        outargs += ["-t", str(seg["seconds"] + tail)]
    outargs += ["-f", "flv", "-flvflags", "no_duration_filesize", target]
    return head + inargs + outargs


class Observer(threading.Thread):
    """Sample the MediaMTX API at high frequency to measure when the receiver really has a stream.

    v2 uses /v3/paths/list: a missing path simply means one fewer entry and leaves no 404 in the MediaMTX log.
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
        """Return the offline windows where ready=False as [(t0, t1, seconds), ...]."""
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
        """Return the windows where bytes stopped growing for >= threshold seconds.

        ready only means a publisher is attached to MediaMTX; a MediaMTX takeover leaves ready True
        while the picture is frozen (measured in overlap_test.py). What really says the picture is moving is
        whether bytesReceived keeps growing.

        Note that HLS transfers in bursts of whole segments, so a healthy source shows a zero-growth window
        every segment length (2-6s is common) and that is not a fault. Read it by whether the longest
        zero-growth interval clearly exceeds the segment length, not by counting the windows.
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
    """The full life of one segment: start -> watchdog -> stop at the scheduled time."""

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
                # Already taken over by the next segment: leave once the MediaMTX takeover drops the old publisher,
                # and do not finish on your own before that.
                if all(p is None or p.poll() is not None
                       for p in (self.proc, self.slate_proc)):
                    return
                continue
            why = self._refresh_due()
            if why:
                log("%-6s proactive handover with a fresh source URL (%s)" % (self.sid, why))
                emit({"event": "url_refresh", "seg": self.sid, "reason": why})
                self.refresh_try = now()
                self._restart(planned=True)
                continue
            self._health()

    def supersede(self):
        """The next segment has taken over the same path: release this one (watchdog included) and let the receiver drop it.

        stop_at must be pushed back here too. The run() main loop checks stop_at first and superseded second,
        and stop_at lands exactly on the handover point; without relaxing it the old publisher SIGINTs itself when it
        reaches the time (MediaMTX logs closed: EOF) while the new publisher is only just opening its source connection,
        leaving a measured 1.1-2.7s gap. Finishing is instead left to the MediaMTX takeover
        (closing existing publisher), which switches as soon as the new publisher connects.
        """
        self.superseded = True
        self.stop_at = max(self.stop_at, now() + SUPERSEDE_GRACE)

    # --------------------------------------------------------- url lifecycle
    def _fresh_urls(self):
        """Get the source URL this segment should use now, re-resolving in place when it expired or is too old.

        Always verify before starting: a manifest URL carries an expire and is bound to the public IP at resolve time,
        so resolve-once-at-startup-then-reuse always breaks in a 24/7 setting.
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
            log("%-6s source URL expired (%s), re-resolving" % (self.sid, why), "WARN")
            emit({"event": "url_stale", "seg": self.sid, "reason": why})
        return resolve_set(self.i, self.spec, self.clients, label="URL updated")

    def _refresh_due(self):
        """Return the reason to take over actively, or None when there is none.

        Only live is affected: reopening a VOD mid-way restarts it from the beginning, which is a bug rather than a fix.
        A 24/7 relay uses this to swap ffmpeg for a new one before the URL expires, and the handover goes through takeover
        so the receiver barely breaks (see v4.2 section 2.2: 0.34s with overlap=3).
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
            return ("resolved %.0fs ago / expires in %.0fs"
                    % (now() - at, (exp - now()) if exp else -1))
        return None

    # -------------------------------------------------------------- launching
    def _launch(self):
        if self.spec["type"] in ("vod", "live"):
            urls = self._fresh_urls()
            if not urls:
                log("skip %s: no usable source" % self.sid, "ERROR")
                emit({"event": "skip", "seg": self.sid})
                return False
        else:
            urls = None
        if urls and self.spec.get("seconds"):
            exp = url_expiry(urls[0])
            if exp and exp - now() < self.spec["seconds"] + \
                    self.args.url_expiry_margin:
                log("%-6s warning: URL lives %.0fs, shorter than this segment %ss plus slack"
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
        """One counter state per ffmpeg process.

        During a handover the old and new process coexist briefly; if they shared one last_value, the old process reading
        a large old value just before dying would pin the small value of the new process and the watchdog would report a stall.
        """
        st = {"proc": proc, "last_value": -1.0, "last": now(), "seen": False}
        self._prog = st
        threading.Thread(target=self._read_progress, args=(proc, st),
                         daemon=True).start()

    def _read_progress(self, proc, st):
        """A stalled ffmpeg keeps reprinting the same out_time_us.

        So only a value that really increases is evidence of life; seeing the line alone is not.
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
            # Not being able to read progress blinds the watchdog, so it must not be swallowed silently.
            if proc.poll() is None and not STOP.is_set():
                log("%-6s progress read interrupted: %r" % (self.sid, exc), "WARN")

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
                log("%-6s process exited early rc=%s" % (self.sid, p.returncode), "WARN")
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
            log("%-6s output stalled %.1fs (threshold %.1fs)" % (self.sid, stalled, limit),
                "WARN")
            self._recover(stalled)
            self._prog["last"] = now()

    def _restart(self, planned=False):
        """planned=True is a scheduled handover (URL updated) and does not count as a failure retry.

        The two must be counted separately: a 24/7 relay with --url-max-age 30 minutes hands over 48 times a day, and
        sharing a max_restarts budget would leave real failures with no retry left.
        """
        old = self.proc
        if planned:
            self.refreshes += 1
        else:
            self.restarts += 1
        if self.spec["type"] in ("vod", "live") and not self.spec.get("direct"):
            resolve_set(self.i, self.spec, self.clients,
                        label="pre-emptive refresh" if planned else "retry")
        if self.restart_mode == "takeover":
            # Let the new publisher take over the same path first and only then drop the old one: zero break at the receiver.
            if not self._launch():
                return
            time.sleep(0.5)
            self._terminate(old)
        else:
            self._terminate(old)
            self._launch()

    def _recover(self, stalled=None):
        """When the source fails: take over the same path with filler first, then retry the source in the background.

        Reopening the same source immediately is pointless: the source is still broken and a reopen just breaks again.
        So switch to filler first (a 0.34s handover at the receiver, measured in relay-gaps) and switch back once the source is really alive.
        """
        if not self.args.filler_on_stall or self.spec["type"] == "filler":
            self._restart()
            return
        old = self.proc
        self.restarts += 1
        if (self.retrying and self.slate_proc is not None
                and self.slate_proc.poll() is None):
            return                      # filler is still running and already waiting for the source, so do nothing
        self._launch_slate()
        self.degraded = True
        if self.restart_mode != "takeover":
            self._terminate(old)
        # takeover: do not drop the old publisher actively. When filler attaches to the same path MediaMTX
        # drops the old one itself (closing existing publisher), which keeps the handover from leaving a 1.5s gap at the receiver.
        if not self.retrying:
            self.retrying = True
            threading.Thread(target=self._retry_loop, daemon=True).start()

    def _launch_slate(self):
        spec = {"id": self.sid + "-slate", "type": "filler",
                "path": self.args.filler_on_stall}
        cmd = build_cmd(spec, self.target, None)
        log("%-6s switching to the filler %s (%s)" % (self.sid, self.args.filler_on_stall,
                                     self.restart_mode))
        self.slate_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=self.logfh,
                                           stdin=subprocess.DEVNULL)
        # stdout must be captured: -progress writes to stdout, and setting DEVNULL would leave the
        # watchdog with nothing to read (and the exception would be swallowed, failing silently).
        self.proc = self.slate_proc
        self._watch_progress(self.slate_proc)
        emit({"event": "slate", "seg": self.sid, "pid": self.slate_proc.pid})

    def _probe(self):
        """Lightly probe whether the source is alive again: fetch a few seconds and stop; completing means alive.

        Returns the usable URL list (None on failure), and carries the newly resolved URLs back
        so the recovery does not reopen with an old, possibly expired manifest URL.
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
            # Once live HLS stalls the server still answers with the old list, so a download probe would call it alive.
            # So the playlist is required to move forward instead (see _hls_advancing).
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
        """Liveness of a live HLS stream: the playlist must move forward; merely being readable does not count.

        When the source stalls the old playlist still downloads and still yields several seconds of existing segments,
        which is the source of false positives; compare media-sequence and the last
        segment between two samples.
        """
        snap = []
        for i in range(2):
            try:
                with urllib.request.urlopen(url, timeout=6) as r:
                    txt = r.read().decode("utf-8", "replace")
            except Exception as exc:
                log("%-6s probe read failed %s" % (self.sid, exc), "WARN")
                return False
            seq, last = None, None
            for ln in txt.splitlines():
                ln = ln.strip()
                if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    seq = ln.split(":", 1)[1].strip()
                elif ln.startswith("#EXT-X-ENDLIST"):
                    return True          # a finished VOD segment still plays normally
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
            log("%-6s source recovered, back to live" % self.sid)
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
    resolve_set(i, seg, RUNTIME["clients"], label="schedule prefix")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist.json"))
    ap.add_argument("--target", default=None)
    ap.add_argument("--overlap", type=float, default=0.0)
    ap.add_argument("--resolve-lead", type=float, default=4.0)
    ap.add_argument("--clients", default=None)
    ap.add_argument("--only", default=None, help="comma-separated segment ids")
    ap.add_argument("--observe", action="store_true", help="measure gaps through the MediaMTX API")
    ap.add_argument("--cookies", default=None,
                    help="path to a YouTube cookies.txt (for when bot checks block the source)."
                         "when empty, look for <project>/cookies.txt")
    ap.add_argument("--check", action="store_true",
                    help="only resolve every source and report availability; do not play")
    ap.add_argument("--loop", type=int, default=1, metavar="N",
                    help="how many rounds of the whole list (0 = endless, for 24/7)."
                         "rounds hand over with the same takeover, no process restart needed")
    ap.add_argument("--flow-threshold", type=float, default=5.0,
                    help="seconds of flat bytesReceived before it counts as an anomaly (default 5, must exceed the source segment length)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watchdog", type=float, default=3.0,
                    help="restart the segment after this many seconds of stalled output (0 = off)")
    ap.add_argument("--startup-grace", type=float, default=12.0,
                    help="seconds tolerated before the first progress appears")
    ap.add_argument("--max-restarts", type=int, default=6)
    ap.add_argument("--filler-on-stall", default="assets/transition.mp4",
                    help="take over with this filler when the source stalls (empty = off)")
    ap.add_argument("--url-max-age", type=float, default=1800.0,
                    help="max seconds to reuse a source URL before re-resolving; live segments also"
                         "hand over proactively at that point (0 = no limit, default 30 minutes)")
    ap.add_argument("--url-expiry-margin", type=float, default=300.0,
                    help="refresh the manifest URL this many seconds before it expires (default 300)")
    ap.add_argument("--retry-interval", type=float, default=8.0,
                    help="seconds between retries after the source goes bad")
    ap.add_argument("--probe-seconds", type=float, default=3.0,
                    help="seconds to pull when probing a source")
    ap.add_argument("--probe-wait", type=float, default=4.0,
                    help="seconds between playlist reads during an HLS probe")
    ap.add_argument("--restart-mode", choices=["auto", "takeover", "cut"],
                    default="auto",
                    help="auto: local MediaMTX uses takeover (no gap), everything else cuts")
    args = ap.parse_args()

    with open(args.playlist, encoding="utf-8") as fh:
        pl = json.load(fh)
    segs = pl["segments"]
    if args.only:
        keep = set(args.only.split(","))
        segs = [s for s in segs if s["id"] in keep]
    if not segs:
        raise SystemExit("no playable segment")
    target = args.target or pl["target"]
    clients = (args.clients.split(",") if args.clients
               else pl.get("clients") or DEFAULT_CLIENTS)
    RUNTIME["clients"] = clients

    # cookies precedence: command line > the playlist cookies field > cookies.txt in the project root
    cookies = args.cookies or pl.get("cookies") or ""
    if not cookies:
        auto = os.path.join(HERE, "cookies.txt")
        cookies = auto if os.path.exists(auto) else ""
    if cookies:
        cookies = os.path.expanduser(cookies)
    RUNTIME["cookies"] = cookies or None

    log("target=%s" % target)
    log("cookies=%s" % (cookies or "(none)"))
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
                log("check %-10s %-6s skipped (local file / filler)"
                    % (seg.get("id", i), seg["type"]))
                continue
            urls = resolve_set(i, seg, clients, label="check")
            if urls:
                ok += 1
                exp = url_expiry(urls[0])
                log("check %-10s OK   %d URL(s)%s"
                    % (seg.get("id", i), len(urls),
                       (", expires in %.0fs" % (exp - now())) if exp else ""))
            else:
                bad += 1
                log("check %-10s FAIL (source unavailable)" % (seg.get("id", i)), "ERROR")
        log("check done: OK=%d FAIL=%d SKIP=%d" % (ok, bad, skip))
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
            log("--observe needs a local MediaMTX target, disabled", "WARN")

    for i, seg in enumerate(segs):
        # "direct": true means url is already an address ffmpeg can take, so yt-dlp resolution is skipped.
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
                    # Release early: the old runner SIGINTs the old publisher as soon as it reaches stop_at, while the new
                    # publisher still needs 1-2s from connecting over RTMP to MediaMTX really switching (measured).
                    # Superseding HANDOFF_LEAD seconds before the handover keeps the old publisher alive until the takeover.
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
        log("KeyboardInterrupt, stopping", "WARN")
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
        log("observation samples %d / span %.2fs (api_fail=%d)"
            % (len(obs.samples), elapsed, obs.api_fail))
        if not wins:
            log("receiver offline windows: 0 (continuous)", "OK")
        else:
            for (a, b, d) in wins:
                log("receiver offline %s -> %s = %.3fs" % (stamp(a), stamp(b), d), "WARN")
            log("receiver offline total %.3fs (%d windows)" % (total_off, len(wins)), "WARN")
        log("flat-data windows: %d, longest %.3fs (HLS pulls and delivers in chunks,"
            "so a gap of one segment length is normal with a healthy source)"
            % (len(fwins), longest), "INFO")
        if bad:
            for (a, b, d) in bad:
                log("flat >= %.1fs: %s -> %s = %.3fs"
                    % (args.flow_threshold, stamp(a), stamp(b), d), "WARN")
        else:
            log("no flat window >= %.1fs" % args.flow_threshold, "OK")
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
        log("gap report written to %s" % GAPS_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
