#!/usr/bin/env python3
"""從 YouTube 播放清單產生母清單 JSON（給 build_local_content.py 用）。

為什麼要逐支抓：flat 模式一次拿整個清單很快，但**拿不到首播日期**
（upload_date/release_date 都是 NA）。日期要拿來當畫面上的浮水印，
所以只能一支一支問。

用法
  python3 build_playlist.py --url "https://www.youtube.com/playlist?list=XXX" \
      --out playlist-tucheng.json
  python3 build_playlist.py --url ... --out ... --limit 3   # 試跑

輸出格式與現有 playlist.json 相容，另外多了三個欄位：
  source_playlist / playlist_title   來源資訊，換清單時可追溯
  segments[].air_date                YYYY-MM-DD，畫面上會顯示這個
  segments[].title                   方便對照
"""

import argparse
import json
import subprocess
import sys
import time

YTDLP = "/opt/homebrew/bin/yt-dlp"
FMT = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b"


def run(args, timeout=180):
    p = subprocess.run([YTDLP, "--no-warnings", "--socket-timeout", "25"] + args,
                       capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return p


def list_ids(url):
    p = run(["--flat-playlist", "--print", "%(id)s", url])
    ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
    if not ids:
        print("拿不到影片清單：%s" % (p.stderr or "")[:200], file=sys.stderr)
    return ids


def meta(vid, retries=3):
    """回傳 (title, seconds, air_date)。失敗回 (None, None, None)。"""
    for i in range(retries):
        p = run(["--skip-download",
                 "--print", "%(title)s\t%(duration)s\t%(upload_date)s\t%(release_date)s",
                 "https://www.youtube.com/watch?v=%s" % vid])
        line = (p.stdout or "").strip().splitlines()
        if line:
            parts = line[-1].split("\t")
            if len(parts) == 4:
                title, dur, up, rel = parts
                date = (rel if rel and rel != "NA" else up)
                if date and date != "NA" and len(date) == 8:
                    date = "%s-%s-%s" % (date[:4], date[4:6], date[6:])
                else:
                    date = ""
                try:
                    secs = float(dur)
                except ValueError:
                    secs = 0.0
                return title, secs, date
        time.sleep(3 + i * 2)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="播放清單網址（或 watch?v=...&list=...）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", default="rtmp://127.0.0.1:1935/live/main")
    a = ap.parse_args()

    url = a.url
    if "list=" in url and "/playlist?" not in url:
        url = "https://www.youtube.com/playlist?list=" + url.split("list=")[1].split("&")[0]
    elif "list=" in url and "/playlist?" in url:
        url = "https://www.youtube.com/playlist?list=" + url.split("list=")[1].split("&")[0]

    ids = list_ids(url)
    if a.limit:
        ids = ids[:a.limit]
    print("清單共 %d 支，開始逐支取 metadata…" % len(ids), flush=True)

    segs = []
    fail = 0
    for i, vid in enumerate(ids, 1):
        title, secs, date = meta(vid)
        if not title:
            fail += 1
            print("  FAIL %s" % vid, flush=True)
            continue
        segs.append({"id": vid, "type": "vod",
                     "url": "https://www.youtube.com/watch?v=%s" % vid,
                     "format": FMT, "mode": "copy",
                     "seconds": int(secs) if secs else 0,
                     "air_date": date, "title": title})
        print("  %2d/%d %-13s %6.0fs  %s  %s"
              % (i, len(ids), vid, secs, date or "(無日期)", title[:40]), flush=True)

    total = sum(s["seconds"] or 0 for s in segs)
    out = {"target": a.target,
           "clients": ["web_embedded", "mweb", "default", "android_vr"],
           "source_playlist": url, "segments": segs}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("寫出 %s：%d 支、總長 %.0fs（%.1f 小時），FAIL=%d"
          % (a.out, len(segs), total, total / 3600.0, fail))
    nodate = [s["id"] for s in segs if not s["air_date"]]
    if nodate:
        print("沒有日期的：%s" % ", ".join(nodate))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

