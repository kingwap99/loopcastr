#!/usr/bin/env python3
"""Broadcast chain health check. launchd runs it every 60 seconds (com.loopcastr.health).

It checks the three points that prove the chain is really moving, rather than only whether a process exists:

  1. whether MediaMTX live/main is ready
  2. whether a reader is attached (= the yt_publish.sh publisher is still connected)
  3. whether inboundBytes really grows within 6 seconds (= the playout is really feeding)

All three catch a live process with a stalled source, or a dropped publisher. --check-youtube
also queries YouTube is_live externally, but that is a public lookup and costs more, so it is off by default.

Alerts only on state changes (ok -> bad, bad -> ok); a fault that persists reminds every 30 minutes
so it does not flood every minute. Alerts go to logs/alerts.jsonl; when an alert_webhook file
(containing one URL) exists, a JSON copy is also POSTed there.

Usage
  python3 healthcheck.py                 # check once, exit 1 when unhealthy
  python3 healthcheck.py --check-youtube # also check the YouTube side
  python3 healthcheck.py --json          # machine-readable output
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
ALERTS = os.path.join(LOGDIR, "alerts.jsonl")
STATE = os.path.join(LOGDIR, "health-state.json")
WEBHOOK_FILE = os.path.join(HERE, "alert_webhook")
TG_FILE = os.path.join(HERE, "telegram.json")

API = os.environ.get("API", "http://127.0.0.1:9997")
PATH_NAME = os.environ.get("PATH_NAME", "live/main")
GROW_WINDOW = 6.0          # window used to measure traffic growth
RE_ALERT_SEC = 1800        # how often a persisting fault is reminded


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def api_path():
    url = "%s/v3/paths/get/%s" % (API.rstrip("/"), PATH_NAME)
    with urllib.request.urlopen(url, timeout=4) as r:
        return json.load(r)


def yt_is_live(video_id):
    if not video_id:
        return None
    try:
        p = subprocess.run(
            ["yt-dlp", "--no-warnings", "--socket-timeout", "15",
             "--skip-download", "--print", "%(live_status)s",
             "https://www.youtube.com/watch?v=" + video_id],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        out = (p.stdout or "").strip().splitlines()
        return out[-1] if out else "unknown"
    except (subprocess.TimeoutExpired, OSError):
        return "query-failed"


def check(youtube_id=None):
    """Return (ok, list of problems, observed values)."""
    problems = []
    obs = {"api_ok": False}
    try:
        d = api_path()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, ["MediaMTX API query failed: %s" % exc], obs
    obs["api_ok"] = True

    obs["ready"] = bool(d.get("ready"))
    readers = d.get("readers") or []
    obs["readers"] = len(readers)
    b1 = int(d.get("bytesReceived") or 0)
    obs["bytes1"] = b1
    if not obs["ready"]:
        problems.append("MediaMTX path not ready (no publisher)")
    if obs["readers"] < 1:
        problems.append("no readers: the publisher (yt_publish.sh) is not connected")

    time.sleep(GROW_WINDOW)
    try:
        d2 = api_path()
        b2 = int(d2.get("bytesReceived") or 0)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        obs["api_ok"] = False
        return False, problems + ["second query failed: %s" % exc], obs
    obs["bytes2"] = b2
    obs["growth"] = b2 - b1
    if b2 <= b1:
        problems.append("bytesReceived did not grow for %.0f s (the playout is stuck)" % GROW_WINDOW)
    obs["framesInError"] = int(d2.get("inboundFramesInError") or 0)

    if youtube_id:
        st = yt_is_live(youtube_id)
        obs["youtube"] = st
        if st != "is_live":
            problems.append("YouTube reports live_status=%s (not is_live)" % st)

    return (not problems), problems, obs


def playout_uptime_days():
    """How long the current playout process has been up, in days, from the last start time

    Why it is needed: the concat -stream_loop -1 timeline accumulates continuously (measured),
    and the 32-bit millisecond limit of FLV is about 49.7 days, after which it wraps. Periodic restarts reset it.
    """
    path = os.path.join(HERE, "logs", "playout.log")
    last = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
                              r" (?:start #\d+|第 \d+ 次啟動)", line)
                if m:
                    last = m.group(1)
    except OSError:
        return None
    if not last:
        return None
    try:
        t = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    return (time.time() - t) / 86400.0


# The service label prefix is not hard-coded: installs from before the rename (loopcastr was ytpl) are com.ytpl.*.
def service_prefix():
    try:
        for name in sorted(os.listdir(HERE)):
            if name.startswith("com.") and name.endswith(".playout.plist"):
                return name[: -len("playout.plist")]
    except OSError:
        pass
    return "com.loopcastr."


SVC = service_prefix()


def kick(label):
    """Run kickstart -k on the given launchd service (which restarts it).

    The system domain (LaunchDaemon) is tried first, then the gui domain (LaunchAgent).
    During a migration from Agent to Daemon both can exist, so neither path breaks.
    Only root can drive the system domain, which is why the health service itself runs as root.
    """
    last = ""
    for target in ("system/%s" % label, "gui/%d/%s" % (os.getuid(), label)):
        try:
            p = subprocess.run(["/bin/launchctl", "kickstart", "-k", target],
                               capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError) as exc:
            last = str(exc)[:200]
            continue
        if p.returncode == 0:
            return True, target
        last = "%s rc=%d %s" % (target, p.returncode,
                                (p.stderr or p.stdout or "").strip()[:160])
    return False, last


def heal(obs):
    """Restart the service that matches the symptom. Returns the list of actions.

    The split is deliberate: no readers means the publisher dropped, so restart the publisher;
    no growth means the playout is stuck, so restart the playout. Restarting it sends the
    timeline back to the first segment, so it is only done when it is really stuck. When the API cannot be reached at all nothing is touched.
    """
    if not obs.get("api_ok"):
        return []
    acts = []
    if (not obs.get("ready")) or obs.get("readers", 0) < 1:
        okk, err = kick(SVC + "publish")
        acts.append({"service": SVC + "publish", "ok": okk, "detail": err})
    if (not obs.get("ready")) or obs.get("growth", 0) <= 0:
        okk, err = kick(SVC + "playout")
        acts.append({"service": SVC + "playout", "ok": okk, "detail": err})
    return acts


def tg_config():
    return load(TG_FILE, {}) or {}


def tg_discover_chat_id(tok):
    """Find the most recent chat id that has talked to the bot, from getUpdates.

    A Telegram bot cannot message a person first; the person has to talk to it once. So on the
    first setup, send /start to the bot and the chat_id is picked up and stored in telegram.json.
    """
    try:
        with urllib.request.urlopen(
                "https://api.telegram.org/bot%s/getUpdates" % tok,
                timeout=15) as r:
            d = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for upd in reversed(d.get("result") or []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"])
    return None


def tg_send(text):
    """Return (ok?, detail). The token lives in telegram.json (chmod 600) and is never committed."""
    cfg = tg_config()
    tok = (cfg.get("token") or "").strip()
    if not tok:
        return False, "no token in telegram.json"
    cid = str(cfg.get("chat_id") or "").strip()
    if not cid:
        cid = tg_discover_chat_id(tok) or ""
        if cid:
            cfg["chat_id"] = cid
            save(TG_FILE, cfg)
            try:
                os.chmod(TG_FILE, 0o600)
            except OSError:
                pass
    if not cid:
        return False, "no chat_id yet: send /start to the bot once"
    body = json.dumps({"chat_id": cid, "text": text,
                       "disable_web_page_preview": True},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % tok, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.load(r)
        return True, "chat_id=%s" % cid
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)[:200]


def notify(payload):
    status = payload.get("status")
    if status == "DOWN":
        text = ("🔴 loopcastr stream down\n%s\nproblems: %s"
                % (payload.get("wall"),
                   "; ".join(payload.get("problems") or [])))
        obs = payload.get("obs") or {}
        if obs:
            text += "\nobserved: " + ", ".join(
                "%s=%s" % (k, v) for k, v in obs.items())
    elif status == "UP":
        text = "🟢 loopcastr recovered\n%s" % payload.get("wall")
    else:
        text = json.dumps(payload, ensure_ascii=False)

    ok, info = tg_send(text)
    if not ok:
        print("[health] Telegram not sent: %s" % info, file=sys.stderr)

    url = ""
    if os.path.exists(WEBHOOK_FILE):
        with open(WEBHOOK_FILE, encoding="utf-8") as fh:
            url = fh.read().strip()
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(64)
    except (urllib.error.URLError, OSError) as exc:
        print("[health] webhook failed: %s" % exc, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-youtube", action="store_true")
    ap.add_argument("--youtube-id", default=os.environ.get("YT_VIDEO_ID", ""))
    ap.add_argument("--youtube-every", type=float, default=900.0,
                    help="how often to poll the YouTube side (seconds). Public queries are expensive,"
                         "and frequent polling attracts bot checks; default 15 minutes")
    ap.add_argument("--heal", action="store_true",
                    help="restart the failing service after this many consecutive failures")
    ap.add_argument("--heal-after", type=int, default=3,
                    help="consecutive failures before acting (default 3, about 3 minutes)")
    ap.add_argument("--heal-cooldown", type=float, default=300.0,
                    help="minimum seconds between two auto-repairs")
    ap.add_argument("--recycle-after-days", type=float, default=0.0,
                    help="proactively restart the playout after this many days (0 = off)."
                         "avoids the FLV 32-bit timestamp wrap at about 49.7 days")
    ap.add_argument("--alert-after", type=int, default=2,
                    help="consecutive failures before the first alert (default 2)."
                         "the publisher reconnects within seconds, so alerting on the first failure would spam for a self-healing hiccup")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one test message to Telegram and exit")
    args = ap.parse_args()

    if args.test_alert:
        okk, info = tg_send("✅ loopcastr alert test (%s)" % time.strftime("%F %T"))
        print("Telegram：%s  %s" % ("OK" if okk else "failed", info))
        return 0 if okk else 1

    now = time.time()
    prev = load(STATE, {}) or {}
    was_ok = prev.get("ok", True)
    last_alert = float(prev.get("last_alert") or 0)
    last_yt = float(prev.get("last_youtube") or 0)

    vid = args.youtube_id or (load(os.path.join(HERE, "stream.json"), {}) or {}).get("video_id")
    do_yt = bool(args.check_youtube and vid)
    if do_yt and last_yt and (now - last_yt) < args.youtube_every:
        do_yt = False          # not time to check YouTube yet; this round only checks the local chain
    ok, problems, obs = check(vid if do_yt else None)
    yt_stamp = int(now) if do_yt else int(last_yt)

    record = {"ts": int(now), "wall": time.strftime("%F %T"), "ok": ok,
              "problems": problems, "obs": obs}

    streak = 0 if ok else int(prev.get("fail_streak") or 0) + 1
    record["fail_streak"] = streak
    last_heal = float(prev.get("last_heal") or 0)
    last_recycle = float(prev.get("last_recycle") or 0)
    if (not ok and args.heal and streak >= args.heal_after
            and (now - last_heal) >= args.heal_cooldown):
        acts = heal(obs)
        if acts:
            record["healed"] = acts
            last_heal = now

    # Preventive restart: when the chain is healthy but the playout has run too long, reset the timeline.
    if ok and args.recycle_after_days > 0 and (now - last_recycle) > 86400:
        days = playout_uptime_days()
        if days is not None and days >= args.recycle_after_days:
            okr, errr = kick(SVC + "playout")
            record["recycled"] = {"uptime_days": round(days, 2),
                                  "ok": okr, "err": errr}
            last_recycle = now

    if not ok:
        overdue = (now - last_alert) > RE_ALERT_SEC
        # The first alert needs a run of failures; while already alerting, keep the 30-minute reminder.
        due = ((was_ok and streak >= args.alert_after)
               or ((not was_ok) and overdue))
        # A recovery notice is only sent after a real outage notice, otherwise a 30-second blip
        # would produce a recovery with no matching outage, which is more confusing than useful.
        alerted_down = bool(prev.get("alerted_down"))
        record["alerted"] = due
        if due:
            record["last_alert"] = int(now)
            alerted_down = True
            with open(ALERTS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + chr(10))
            notify({"service": "loopcastr", "status": "DOWN",
                    "wall": record["wall"], "problems": problems, "obs": obs})
        save(STATE, {"ok": False, "last_alert": record.get("last_alert", last_alert),
                     "last_youtube": yt_stamp, "fail_streak": streak,
                     "last_heal": int(last_heal), "last_recycle": int(last_recycle),
                     "alerted_down": alerted_down,
                     "problems": problems, "since": prev.get("since") or int(now)})
    else:
        if (not was_ok) and prev.get("alerted_down"):
            with open(ALERTS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(record, recovered=True),
                                    ensure_ascii=False) + chr(10))
            notify({"service": "loopcastr", "status": "UP", "wall": record["wall"]})
        save(STATE, {"ok": True, "problems": [], "last_youtube": yt_stamp,
                     "fail_streak": 0, "last_heal": int(last_heal),
                     "last_recycle": int(last_recycle),
                     "alerted_down": False,
                     "since": int(now)})

    if args.json:
        print(json.dumps(record, ensure_ascii=False))
    else:
        tag = "OK  " if ok else "FAIL"
        extra = ""
        if record.get("healed"):
            done = [a["service"] for a in record["healed"] if a.get("ok")]
            bad = ["%s(%s)" % (a["service"], a.get("detail"))
                   for a in record["healed"] if not a.get("ok")]
            if done:
                extra = "| auto-restarted: " + ", ".join(done)
            if bad:
                extra += "| restart failed: " + "; ".join(bad)
        if record.get("recycled"):
            extra += "| preventive playout restart (up %.1f days)" % record["recycled"]["uptime_days"]
        print("[health %s] %s %s"
              % (record["wall"], tag,
                 ("all links OK (traffic %+d bytes / %.0fs)"
                  % (obs.get("growth", 0), GROW_WINDOW)) + extra
                 if ok else "；".join(problems) + extra))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
