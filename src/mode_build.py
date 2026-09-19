#!/usr/bin/env python3
"""依「播出模式」建置整條內容鏈：掃描來源 → 落地 → 過場 → 部署 → 重建清單。

模式定義在 modes.json（news / promotion / test）。news 與 promotion 由
refreshwatch.py 依 refresh_seconds 定期重新掃描。

為什麼要分階段：播出端是單一行程 concat 加 --stream_loop -1，每個循環都會
重開檔案；直接覆寫正在播的檔案，會讓它讀到沒有 moov 的半成品。所以一律先寫到
暫存目錄，驗完再 mv 進去。暫存目錄放 /tmp，不放 media/ —— media/ 在 Spotlight
索引範圍內，實測會讓 ffmpeg 在收尾階段停滯。

用法
  python3 mode_build.py --mode promotion              # 全流程（不切換播出端）
  python3 mode_build.py --mode promotion --switch     # 全流程 + 切換播出端
  python3 mode_build.py --mode promotion --scan-only  # 只重新掃描母清單

續傳：落地是否要處理是以「暫存目錄裡有沒有這個檔案」判斷，中斷後重跑只補缺的。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")

# 畫質與版面參數集中在 settings.json；這裡讀它，才不會有第二份寫死的值。
sys.path.insert(0, HERE)
try:
    import build_local_content as BLC
except ImportError:
    BLC = None


def setting(section, key, default):
    return BLC.cfg(section, key, default) if BLC else default


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run(cmd, **kw):
    log("執行 " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **kw).returncode


def load_modes():
    return json.load(open(os.path.join(HERE, "modes.json"), encoding="utf-8"))


def files_for(mode):
    return {
        "mother": os.path.join(HERE, "playlist-%s.json" % mode),
        "local": os.path.join(HERE, "playlist-%s-local.json" % mode),
        "concat": os.path.join(HERE, "concat-%s.txt" % mode),
        # 內容每個模式一份：media/<模式>/。同一支影片在不同模式有不同長度上限時
        # 才不會互相蓋掉，manifest 也各自一份（原始檔 media/.raw 仍共用）。
        "media": os.path.join(MEDIA, mode),
        "stage_ep": "/tmp/stage-ep-%s" % mode,
        "stage_tr": "/tmp/stage-tr-%s" % mode,
    }


def passes_for(cfg, f):
    """一輪裡影片要重複幾趟。

    過場是「第 i 支影片配第 i 支 short」，所以一輪只用到 shorts 池的前 N 支。
    讓一輪包含多趟影片，shorts 就會接著往下輪 —— 例：30 支影片配 50 支 shorts
    時 passes=2，第二趟從第 31 支 short 接著播。
    """
    if cfg.get("shorts_passes"):
        return max(1, int(cfg["shorts_passes"]))
    pool = cfg.get("shorts_count") or 0
    n = 0
    if os.path.exists(f["mother"]):
        n = len(json.load(open(f["mother"], encoding="utf-8"))["segments"])
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
    return run(cmd)


def land(cfg, f):
    cmd = [sys.executable, os.path.join(HERE, "build_local_content.py"),
           "--playlist", f["mother"],
           "--target", str(setting("media", "target", "720")), "--keep-raw",
           "--media-dir", f["media"],
           "--out-dir", f["stage_ep"],
           "--out-playlist", "/tmp/stage-%s-playlist.json" % os.path.basename(f["mother"])]
    if cfg.get("max_seconds"):
        cmd += ["--max-seconds", str(cfg["max_seconds"])]
    return run(cmd)


def transitions(cfg, f, passes):
    if not cfg.get("shorts_url"):
        log("這個模式沒有設 shorts_url，跳過過場")
        return 0
    cmd = [sys.executable, os.path.join(HERE, "build_transitions.py"),
           "--playlist", f["mother"],
           "--shorts-url", cfg["shorts_url"],
           "--shorts-count", str(cfg.get("shorts_count") or 15),
           "--seconds", str(cfg.get("shorts_seconds") or 90),
           "--button-caption",
           str(setting("overlay", "transition_caption", "去追劇")),
           "--parallel", "2",
           "--passes", str(passes),
           "--out-dir", f["stage_tr"]]
    return run(cmd)


def deploy(f):
    """把暫存目錄裡驗得過的檔案 mv 進 media/（同 volume，原子置換）。"""
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
                log("!! %s 讀不出長度（未完成），留在暫存目錄" % name)
                skipped += 1
                continue
            shutil.move(src, os.path.join(f["media"], name))
            moved += 1
    log("部署：置換 %d 個，跳過 %d 個" % (moved, skipped))
    return skipped == 0


def fix_manifest(f):
    """落地若用了 --out-dir，manifest 會記成暫存路徑；部署後改回 media/<id>.mp4。"""
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
    log("manifest：修正 %d 筆路徑" % n)


def rebuild_playlist(cfg, f, passes):
    """重跑一次（不帶 --out-dir、不帶 --force）：影片都已在 media/，所以只會
    重新產生清單，並把 media/_tr_<id>.mp4 的過場插進去。"""
    cmd = [sys.executable, os.path.join(HERE, "build_local_content.py"),
           "--playlist", f["mother"],
           "--target", str(setting("media", "target", "720")), "--keep-raw",
           "--media-dir", f["media"], "--passes", str(passes),
           "--out-playlist", f["local"]]
    if cfg.get("max_seconds"):
        cmd += ["--max-seconds", str(cfg["max_seconds"])]
    return run(cmd)


def make_concat(f):
    return run([sys.executable, os.path.join(HERE, "make_concat_list.py"),
                f["local"], "-o", f["concat"], "--base-dir", HERE])


def switch(mode):
    script = os.path.join(HERE, "switch_edition.sh")
    if not os.path.exists(script):
        log("找不到 switch_edition.sh")
        return 1
    log("切換播出端到 %s" % mode)
    return subprocess.run([script, mode], stdin=subprocess.DEVNULL).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--deploy-only", action="store_true")
    ap.add_argument("--switch", action="store_true")
    ap.add_argument("--skip-transitions", action="store_true")
    a = ap.parse_args()

    modes = load_modes()
    if a.mode not in modes:
        log("modes.json 裡沒有這個模式：%s（有 %s）" % (a.mode, ", ".join(modes)))
        return 2
    cfg = modes[a.mode]
    f = files_for(a.mode)
    log("模式 %s（%s）：來源 %s，上限 %s 秒，shorts %s 支"
        % (a.mode, cfg.get("label"), cfg.get("video_source"),
           cfg.get("max_seconds") or "全長", cfg.get("shorts_count")))

    if a.scan_only:
        return scan(cfg, f)

    if not a.deploy_only:
        if scan(cfg, f) != 0:
            log("掃描失敗，中止")
            return 1
        if land(cfg, f) != 0:
            log("落地有失敗；仍會嘗試部署已完成的檔案")

    passes = passes_for(cfg, f)
    if not a.deploy_only and not a.skip_transitions:
        log("一輪播 %d 趟影片（shorts 池 %s 支、影片 %d 支）"
            % (passes, cfg.get("shorts_count"),
               len(json.load(open(f["mother"], encoding="utf-8"))["segments"])
               if os.path.exists(f["mother"]) else 0))
        if transitions(cfg, f, passes) != 0:
            log("過場建置有失敗")

    if not deploy(f):
        log("有檔案沒完成，先不重建清單。修好後重跑即可（會自動續傳）")
        return 3

    fix_manifest(f)
    if rebuild_playlist(cfg, f, passes) != 0:
        log("重建清單失敗")
        return 1
    if make_concat(f) != 0:
        log("產生 concat 清單失敗")
        return 1

    segs = json.load(open(f["local"], encoding="utf-8"))["segments"]
    total = sum(s.get("outpoint") or s["seconds"] for s in segs)
    log("%s 就緒：%d 段、總長 %.0fs（%.2f 小時）"
        % (os.path.basename(f["concat"]), len(segs), total, total / 3600.0))

    if a.switch:
        return switch(a.mode)
    log("要切換播出端請執行：SUDO_PASS=... ./switch_edition.sh %s" % a.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
