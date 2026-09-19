#!/usr/bin/env python3
"""從 playlist 產生 ffmpeg concat 清單，供「單一行程零縫播出」使用。

只有本機檔案片段（type=file / vod_local）會被寫進清單；YouTube 之類的
遠端片段必須先落地（build_local_content.py），否則播放中斷會變成縫。
"""

import argparse
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist")
    ap.add_argument("-o", "--out", default="concat.txt")
    ap.add_argument("--base-dir", default=".")
    a = ap.parse_args()

    with open(a.playlist) as f:
        d = json.load(f)
    segs = d.get("segments") or []

    lines = []
    missing = []
    skipped = []
    total = 0
    trims = 0
    for s in segs:
        t = s.get("type")
        if t not in ("file", "vod_local"):
            skipped.append("%s(%s)" % (s.get("id"), t))
            continue
        p = s.get("path") or ""
        if not os.path.isabs(p):
            p = os.path.normpath(os.path.join(a.base_dir, p))
        if not os.path.exists(p):
            missing.append(p)
            continue
        lines.append("file '%s'" % p.replace("'", "'\\''"))
        if s.get("outpoint"):
            lines.append("outpoint %.3f" % float(s["outpoint"]))
            trims += 1
        # 用浮點相加：逐段取整累積誤差會到 20 秒以上，跟實際播放長度對不上
        total += float(s.get("outpoint") or s.get("seconds") or 0)

    with open(a.out, "w") as f:
        f.write("\n".join(lines) + "\n")

    print("寫出 %s：%d 段（%d 行），總長 %.0fs"
          % (a.out, len(lines) - trims, len(lines), total))
    if skipped:
        print("略過非本機片段：%s" % ", ".join(skipped))
    if missing:
        print("缺少檔案：%s" % ", ".join(missing))
    return 0 if lines and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
