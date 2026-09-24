#!/usr/bin/env python3
"""Build the whole content chain for a broadcast mode: scan source -> land -> transitions -> deploy -> rebuild list.

Modes are defined in modes.json (news / promotion / test). news and promotion are rescanned
periodically by refreshwatch.py according to refresh_seconds.

Why it is staged: the playout is one concat process with --stream_loop -1 and it reopens files
every loop, so overwriting a file that is on air would let it read a half-written file with no
moov. Everything is therefore written to a staging directory first and moved in once it is
verified. Staging lives in /tmp rather than media/, because media/ is inside the Spotlight index and (measured) makes ffmpeg stall while finishing.

Usage
  python3 mode_build.py --mode promotion              # full run, without switching the playout
  python3 mode_build.py --mode promotion --switch     # full run plus switching the playout
  python3 mode_build.py --mode promotion --scan-only  # rescan the master list only

Resume: whether landing has to run is decided by whether the file is already in staging, so a rerun after an interruption only fills the gaps.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Quality and layout parameters live in settings.json; they are read from there so there is no second hard-coded copy.
sys.path.insert(0, HERE)
try:
    import build_local_content as BLC
except ImportError:
    BLC = None

import playout_ctl as pctl      # boundary calculation and restarting (shared with refreshwatch)
import buildlock                # only one build may write the playlists at a time

# settings.json media.dir can put the library on another disk; the builders share one resolver.
MEDIA = BLC.media_root() if BLC else os.path.join(HERE, "media")


def setting(section, key, default):
    return BLC.cfg(section, key, default) if BLC else default


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run(cmd, **kw):
    log("run " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kw).returncode


def load_modes():
    return json.load(open(os.path.join(HERE, "modes.json"), encoding="utf-8"))


def files_for(mode):
    return {
        "mother": os.path.join(HERE, "playlist-%s.json" % mode),
        "local": os.path.join(HERE, "playlist-%s-local.json" % mode),
        "concat": os.path.join(HERE, "concat-%s.txt" % mode),
        # One copy of the content per mode: media/<mode>/. When the same video has a different
        # length cap per mode they cannot overwrite each other, and each keeps its own manifest (the raw files in media/.raw are still shared).
        "media": os.path.join(MEDIA, mode),
        "stage_ep": "/tmp/stage-ep-%s" % mode,
        "stage_tr": "/tmp/stage-tr-%s" % mode,
    }


def is_active_mode(f):
    """Is this mode the one the loaded playout service is configured to play?"""
    return (os.path.abspath(pctl.active_playlist_path() or "") ==
            os.path.abspath(f["local"]))


def passes_for(cfg, f):
    """How many passes over the videos one round contains.

    A transition pairs video i with short i, so one round only uses the first N shorts of the pool.
    Letting a round contain several passes carries the shorts onward: with 30 videos and 50 shorts,
    passes=2 makes the second pass continue from short 31.
    """
    if cfg.get("shorts_passes"):
        return max(1, int(cfg["shorts_passes"]))
    pool = cfg.get("shorts_count") or 0
    n = 0
    if os.path.exists(f["mother"]):
        n = len(json.load(open(f["mother"], encoding="utf-8"))["segments"])
    if n <= 1:
        # Do not run several passes when there is only one video: that plays the same video five
        # times over (measured on .22 news: a master list of 1 gives passes=5, so a round is that video
        # five times plus five different transitions, which looks like only one video with almost no transitions).
        return 1
    if pool and n:
        return max(1, min(5, -(-pool // n)))
    return 1


def probe_seconds(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def scan(cfg, f):
    cmd = [sys.executable, os.path.join(HERE, "build_playlist.py"),
           "--url", cfg["video_source"], "--out", f["mother"]]
    if cfg.get("video_limit"):
        cmd += ["--limit", str(cfg["video_limit"])]
    if cfg.get("max_age_hours"):
        cmd += ["--max-age-hours", str(cfg["max_age_hours"])]
    return run(cmd)


def land(cfg, f, force=False, limit=0):
    cmd = [sys.executable, os.path.join(HERE, "build_local_content.py"),
           "--playlist", f["mother"],
           "--target", str(setting("media", "target", "720")), "--keep-raw",
           "--media-dir", f["media"],
           "--out-dir", f["stage_ep"],
           "--out-playlist", "/tmp/stage-%s-playlist.json" % os.path.basename(f["mother"])]
    if cfg.get("max_seconds"):
        cmd += ["--max-seconds", str(cfg["max_seconds"])]
    if force:
        cmd.append("--force")
    if limit:
        cmd += ["--limit", str(limit)]      # how many to do this round (used by the batched build)
    return run(cmd)


def transitions(cfg, f, passes, force=False, limit=0):
    if not cfg.get("shorts_url"):
        log("no shorts_url for this mode, skipping transitions")
        return 0
    cmd = [sys.executable, os.path.join(HERE, "build_transitions.py"),
           "--playlist", f["mother"],
           "--shorts-url", cfg["shorts_url"],
           "--shorts-count", str(cfg.get("shorts_count") or 15),
           "--seconds", str(cfg.get("shorts_seconds") or 90),
           "--button-caption",
           str(BLC.TRANSITION_CAPTION if BLC else "去追劇"),
           "--parallel", "2",
           "--passes", str(passes),
           "--out-dir", f["stage_tr"]]
    if cfg.get("video_limit"):
        # Fixed stride: the episode count changes during a batched build, and without a fixed
        cmd += ["--stride", str(cfg["video_limit"])]
    if force:
        cmd.append("--force")
    else:
        # stride every finished transition would be redone. Unchanged fingerprints are skipped, so a batched build only makes the new ones.
        cmd += ["--skip-dir", f["media"]]
    if limit:
        cmd += ["--limit", str(limit)]      # only build transitions for the first N episodes
    return run(cmd)


def deploy(f):
    """Move the verified files from the staging dir into media/<mode>/.

    Same volume, so this is an atomic replace: the playout process (which reopens
    files every loop) only ever sees a complete old or a complete new file.
    """
    moved = skipped = 0
    os.makedirs(f["media"], exist_ok=True)
    for d in (f["stage_ep"], f["stage_tr"]):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".mp4"):
                continue
            src = os.path.join(d, name)
            secs = probe_seconds(src)
            if secs < 1.0:
                log("!! %s has no readable duration (incomplete), left in staging" % name)
                skipped += 1
                continue
            shutil.move(src, os.path.join(f["media"], name))
            moved += 1
    log("deploy: moved %d, skipped %d" % (moved, skipped))
    return skipped == 0


def fix_manifest(f):
    """If landing used --out-dir the manifest records the staging path; rewrite it."""
    mp = os.path.join(f["media"], "manifest.json")
    if not os.path.exists(mp):
        return
    m = json.load(open(mp, encoding="utf-8"))
    rel = os.path.relpath(f["media"], HERE)
    n = 0
    for sid, rec in m.items():
        want = os.path.join(rel, sid + ".mp4")
        if rec.get("file") != want:
            rec["file"] = want
            n += 1
    json.dump(m, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log("manifest: fixed %d paths" % n)


def rebuild_playlist(cfg, f, passes, mother=None):
    """Run once more (without --out-dir and without --force): the videos are already in media/, so this only
    regenerates the list and inserts the media/_tr_<id>.mp4 transitions."""
    cmd = [sys.executable, os.path.join(HERE, "build_local_content.py"),
           "--playlist", mother or f["mother"],
           "--target", str(setting("media", "target", "720")), "--keep-raw",
           "--media-dir", f["media"], "--passes", str(passes),
           "--out-playlist", f["local"]]
    if cfg.get("max_seconds"):
        cmd += ["--max-seconds", str(cfg["max_seconds"])]
    return run(cmd)


def ready_mother(f):
    """Write the segments whose files already exist in media/ as a temporary master list.

    The intermediate rounds of a batched build use it to rebuild the playout list: rebuilding from
    the full master list would make build_local_content build the not-yet-done segments too, wasting the batching.
    """
    d = json.load(open(f["mother"], encoding="utf-8"))
    segs = [s for s in d["segments"]
            if os.path.exists(os.path.join(f["media"], s["id"] + ".mp4"))]
    out = "/tmp/ready-%s.json" % os.path.basename(f["mother"])
    d = dict(d)
    d["segments"] = segs
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    return out, len(segs)


def batched_build(cfg, f, a, batch, bsize):
    """Go on air after the first batch, then extend once per batch.

    Why: waiting for everything before going on air means hours of delay for news (55 full-length videos).
    With batching, the first batch (say 2 videos) goes live and the rest is filled in behind it.
    Every extension restarts the playout at the next segment boundary and rotates the list, so the audience never sees the content jump back to the top.
    """
    total = len(json.load(open(f["mother"], encoding="utf-8"))["segments"])
    passes = passes_for(cfg, f)
    ends = list(range(batch, total, bsize))
    if not ends or ends[-1] != total:
        ends.append(total)
    # If the requested mode is already on air and the user explicitly asked
    # to switch, keep the physical order currently used by ffmpeg and apply a
    # continuity rotation at each batch boundary.  A build-only action stays
    # off-air until the user switches it deliberately.
    live = bool(a.switch) and is_active_mode(f)
    if live:
        sync_local_to_concat(f)
        log("continuing the active playlist; batch updates will preserve position")
    prev = 0
    for end in ends:
        log("batch: building up to %d of %d" % (end, total))
        want = end - prev
        prev = end
        if land(cfg, f, a.force, want) != 0:
            log("landing had failures; still deploying whatever completed")
        if not a.skip_transitions and transitions(cfg, f, passes, a.force, end) != 0:
            log("some transitions failed")
        if not deploy(f):
            log("some files are incomplete; not rebuilding the list")
            return 3
        fix_manifest(f)
        ready, n = ready_mother(f)
        log("ready so far: %d segments" % n)

        # f["local"] is the physical order used by the currently running
        # ffmpeg.  Save it before rebuild_playlist overwrites it with the new
        # canonical order; otherwise the next boundary cannot be mapped and a
        # restart would begin at the first episode again.
        active_before = None
        if live and os.path.exists(f["local"]):
            active_before = "/tmp/loopcastr-active-%s-%s.json" % (a.mode, os.getpid())
            shutil.copyfile(f["local"], active_before)
        if rebuild_playlist(cfg, f, passes, mother=ready) != 0:
            log("rebuilding the playout list failed")
            return 1
        if not live:
            if make_concat(f) != 0:
                log("generating the concat list failed")
                return 1
            if not a.switch:
                log("batch is ready; build-only mode is not switching the playout")
                continue
            live = True
            rc = switch(a.mode)
            if rc != 0:
                log("switching failed rc=%s" % rc)
                return rc
            log("on air: first batch is playing")
            continue

        # The concat demuxer reads its list once at ffmpeg startup, so the new
        # list is rotated to the next segment before the restart.
        if active_before:
            if pctl.same_round(active_before, f["local"]):
                # This batch added nothing, so the list is the one already on air. Restarting would
                # stop the stream for a few seconds and change nothing at all.
                log("the list on air is already this one; not restarting the playout for it")
            else:
                continue_from(f, active_before)
        else:
            log("no snapshot of the on-air list; keeping the playout untouched")
        if active_before:
            try:
                os.remove(active_before)
            except OSError:
                pass

    segs = json.load(open(f["local"], encoding="utf-8"))["segments"]
    tot = sum(s.get("outpoint") or s["seconds"] for s in segs)
    log("%s ready: %d segments, %.0fs total (%.2f h)"
        % (os.path.basename(f["concat"]), len(segs), tot, tot / 3600.0))
    return 0


def make_concat(f):
    return run([sys.executable, os.path.join(HERE, "make_concat_list.py"),
                f["local"], "-o", f["concat"], "--base-dir", HERE])


def sync_local_to_concat(f):
    """Make the playlist JSON match the order ffmpeg actually loaded.

    playout.sh regenerates the concat list from this JSON when it starts, but a
    later rebuild rewrites the JSON while ffmpeg keeps playing the older order.
    Without this, restarting the builder or the console would compute the next
    boundary from the wrong list and cut the segment that is on air.  The concat
    file is the record of what was loaded, so rotate the JSON back to its first
    segments.  Idempotent: when both already agree this is a no-op.
    """
    def rel(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))

    try:
        head = []
        with open(f["concat"], encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("file "):
                    head.append(os.path.normpath(line[5:].strip().strip("'")))
                    if len(head) >= 3:
                        break
        if not head:
            return
        with open(f["local"], encoding="utf-8") as fh:
            doc = json.load(fh)
        segs = doc.get("segments") or []
        if not segs:
            return
        paths = [rel(s.get("path") or "") for s in segs]
        want = head[:min(3, len(head))]
        start = None
        for i in range(len(segs)):
            if [paths[(i + j) % len(segs)] for j in range(len(want))] == want:
                start = i
                break
        if start in (None, 0):
            return
        doc["segments"] = segs[start:] + segs[:start]
        doc["_concat_resync"] = {"start": start}
        tmp = f["local"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, f["local"])
        log("resynced the playlist to the concat list that is on air (start %d)" % start)
    except (OSError, ValueError, TypeError):
        pass


def continue_from(f, active_before):
    """Hand the freshly built list over without going back to the first video.

    ``active_before`` is the order the running ffmpeg loaded; the newly built
    ``f["local"]`` replaces it.  Wait for the end of the segment that is on air,
    rotate the new list to the segment that starts at that moment, then restart.
    On any doubt the old list is put back and the playout is left alone: the
    channel keeps running rather than jumping to the top.
    """
    tgt, next_index = pctl.next_boundary_info(active_before)
    if tgt is None or next_index is None:
        log("cannot compute the next segment boundary; keeping the on-air list")
        shutil.copyfile(active_before, f["local"])
        return False
    wait = (tgt - datetime.datetime.now()).total_seconds()
    if wait > 20:
        log("waiting %.0f s for the next segment boundary (%s) before restarting"
            % (wait, tgt.strftime("%F %T")))
        time.sleep(wait)
    else:
        log("no boundary wait (%.0f s away)" % wait)
    if not pctl.rotate_playlist_to(active_before, f["local"], next_index, f["local"]):
        # Nothing that is on air maps into the new list. The guard was written to avoid jumping back
        # to the first video, but it has no way out: once the source window has moved on far enough
        # that none of the on-air ids survive, every future hand-over gives up and the channel stays
        # on its old list forever. Measured on a 24/7 news mode: 183 hand-overs in a row gave up, and
        # the channel was still playing a list from two days earlier with every new upload missing.
        # Hand over anyway. The wait for the segment boundary above already happened, so no video is
        # cut off; the round simply starts at the first video of the new list, which is what a mode
        # that rescans for new uploads promises to do. A partial scan cannot reach this branch:
        # build_playlist.py refuses to write a shorter list unless the source itself changed.
        log("nothing on air maps into the new list; handing over at this boundary "
            "(the new round starts at the first video)")
    if make_concat(f) != 0:
        log("generating the concat list failed; keeping the on-air list")
        shutil.copyfile(active_before, f["local"])
        return False
    how = pctl.restart_playout()
    log("playout restarted (%s)" % how if how else "could not restart the playout")
    return bool(how)


def switch(mode):
    script = os.path.join(HERE, "switch_edition.sh")
    if not os.path.exists(script):
        log("switch_edition.sh not found")
        return 1
    log("switching the playout to %s" % mode)
    return subprocess.run([script, mode], stdin=subprocess.DEVNULL).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--deploy-only", action="store_true")
    ap.add_argument("--switch", action="store_true")
    ap.add_argument("--skip-transitions", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="ignore fingerprints and redo every video and transition")
    ap.add_argument("--first-batch", type=int, default=0,
                    help="go on air after N videos and extend per batch (0 = wait for everything before switching)")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="how many videos per batch after the first (0 = use --first-batch)")
    a = ap.parse_args()

    modes = load_modes()
    if a.mode not in modes:
        log("no such mode in modes.json: %s (have %s)" % (a.mode, ", ".join(modes)))
        return 2
    # Two builds write the same playlist, concat list and manifest. Take the slot or stop: racing the
    # other build interleaves those writes, and whichever finishes last decides what is on air.
    lock = buildlock.BuildLock(HERE)
    if not lock.acquire():
        log("another build is already running (pid %s); stopping instead of writing the same "
            "files as it. Nothing is broken - that build is doing this work."
            % (buildlock.holder(HERE) or "?"))
        return buildlock.EXIT_BUSY
    cfg = modes[a.mode]
    f = files_for(a.mode)
    log("mode %s (%s): source %s, limit %s s, %s shorts, max age %s h"
        % (a.mode, cfg.get("label"), cfg.get("video_source"),
           cfg.get("max_seconds") or "full length", cfg.get("shorts_count"),
           cfg.get("max_age_hours") or "no limit"))

    if a.scan_only:
        return scan(cfg, f)

    if not a.deploy_only:
        if scan(cfg, f) != 0:
            log("scan failed, stopping")
            return 1
        batch = int(a.first_batch or cfg.get("first_batch") or 0)
        if batch > 0:
            # Small first batch, larger ones after: every batch restarts the playout once, and tiny batches keep cutting the stream
            bsize = int(a.batch_size or cfg.get("batch_size") or max(batch, 10))
            return batched_build(cfg, f, a, batch, max(1, bsize))
        if land(cfg, f, a.force) != 0:
            log("landing had failures; still deploying whatever completed")

    passes = passes_for(cfg, f)
    if not a.deploy_only and not a.skip_transitions:
        log("%d pass(es) per round (shorts pool %s, videos %d)"
            % (passes, cfg.get("shorts_count"),
               len(json.load(open(f["mother"], encoding="utf-8"))["segments"])
               if os.path.exists(f["mother"]) else 0))
        if transitions(cfg, f, passes, a.force) != 0:
            log("some transitions failed")

    if not deploy(f):
        log("some files are incomplete; not rebuilding the list. Rerun to resume.")
        return 3

    fix_manifest(f)
    # Snapshot the on-air order before the rebuild overwrites it, so an active
    # mode can be handed over at the next segment instead of the first one.
    live = a.switch and is_active_mode(f)
    active_before = None
    if live:
        sync_local_to_concat(f)
        if os.path.exists(f["local"]):
            active_before = "/tmp/loopcastr-active-%s-%s.json" % (a.mode, os.getpid())
            shutil.copyfile(f["local"], active_before)
    if rebuild_playlist(cfg, f, passes) != 0:
        log("rebuilding the playout list failed")
        return 1
    if not live:
        if make_concat(f) != 0:
            log("generating the concat list failed")
            return 1

    segs = json.load(open(f["local"], encoding="utf-8"))["segments"]
    total = sum(s.get("outpoint") or s["seconds"] for s in segs)
    log("%s ready: %d segments, %.0fs total (%.2f h)"
        % (os.path.basename(f["concat"]), len(segs), total, total / 3600.0))

    if a.switch:
        if live:
            if active_before and pctl.same_round(active_before, f["local"]):
                log("the list on air is already this one; not restarting the playout for it")
                return 0
            if active_before and continue_from(f, active_before):
                try:
                    os.remove(active_before)
                except OSError:
                    pass
                return 0
            log("the new list was built but not switched in; the playout keeps "
                "running the list that is on air")
            return 3
        return switch(a.mode)
    log("to switch the playout: ./switch_edition.sh %s" % a.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
