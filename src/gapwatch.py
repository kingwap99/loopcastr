#!/usr/bin/env python3
"""獨立觀測器：輪詢 MediaMTX API，報告某條路徑的離線與零成長區間。

relay.py --observe 把觀測做在行程內；這支是同一套量測的獨立版本，
用途是驗收「不是 relay 在推」的播出方式（例如單一行程 concat 播整份清單）。

用法
  python3 gapwatch.py [API] [path] [秒數]
  python3 gapwatch.py http://127.0.0.1:9997 live/main 80
"""

import json
import sys
import time
import urllib.error
import urllib.request


def stamp(t):
    lt = time.localtime(t)
    return time.strftime("%H:%M:%S", lt) + ".%03d" % int((t % 1) * 1000)


def main():
    api = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9997"
    name = sys.argv[2] if len(sys.argv) > 2 else "live/main"
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    url = "%s/v3/paths/get/%s" % (api.rstrip("/"), name)

    samples = []
    fails = 0
    t_end = time.time() + dur
    while time.time() < t_end:
        t = time.time()
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                d = json.load(r)
            samples.append((t, bool(d.get("ready")), int(d.get("bytesReceived") or 0)))
        except (urllib.error.URLError, OSError, ValueError):
            fails += 1
            samples.append((t, None, None))
        time.sleep(0.1)

    def windows(key):
        out = []
        start = None
        for (t, ok, val) in samples:
            bad = (val is None) if val is None else key(ok, val)
            if bad and start is None:
                start = t
            elif not bad and start is not None:
                out.append((start, t, t - start))
                start = None
        if start is not None:
            out.append((start, samples[-1][0], samples[-1][0] - start))
        return out

    prev = {"v": None}

    def stagnated(ok, val):
        if prev["v"] is None:
            prev["v"] = val
            return False
        same = (val == prev["v"])
        prev["v"] = val
        return same

    off = windows(lambda ok, val: not ok)
    flow = windows(stagnated)
    total = sum(w[2] for w in off)

    print("samples %d / span %.2fs (api_fail=%d)" % (len(samples), dur, fails))
    if not off:
        print("receiver offline windows: 0 (continuous)")
    else:
        for (a, b, d) in off:
            print("  offline %s -> %s = %.3fs" % (stamp(a), stamp(b), d))
        print("receiver offline total %.3fs (%d windows)" % (total, len(off)))
    longest = max((w[2] for w in flow), default=0.0)
    print("bytesReceived flat windows: %d, longest %.3fs"
          % (len(flow), longest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
