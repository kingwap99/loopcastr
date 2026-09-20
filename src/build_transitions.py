#!/usr/bin/env python3
"""為每一集產生專屬的過場影片：右下角 QR Code 指向「剛播完那一集」的原始網址。

為什麼要一集一份：過場的用途是讓觀眾掃碼去看剛播完的影片，所以 QR 內容必須
是那一集的網址，而不是頻道網址。53 集就是 53 份過場。

來源是 media/_transition-clean.mp4（沒有 QR 的乾淨版）。改 QR 一律從它出發，
不要從已經疊過的版本再疊，否則會愈疊愈花。

用法
  python3 build_transitions.py                 # 全部重做
  python3 build_transitions.py --limit 3       # 試跑前 3 集
  python3 build_transitions.py --verify 5      # 做完後抽 5 支從影片解碼驗證
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import build_local_content as blc      # 重用它的正規化（參數才會一致）
except ImportError:
    blc = None

import wmtext                              # 標題列與 QR 按鈕的繪製

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
CLEAN = os.path.join(MEDIA, "_transition-clean.mp4")
TMP = "/tmp/wm"



def make_qr(url, path, scale=8, border=4):
    p = subprocess.run([sys.executable, os.path.join(HERE, "make_qr_png.py"),
                        url, path, "--scale", str(scale), "--border", str(border)],
                       capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
    return "" if p.returncode == 0 and os.path.exists(path) else (p.stderr or "")[:160]


def title_overlays(title, air, w=1280, tag="", band_left=0):
    """標題列的 overlay：放得下就靜態置右，放不下就跑馬燈（固定視窗裁切）。

    過場的空間大（直式短片兩側是黑邊），所以 band_left 給 0，不用保留 logo 的
    位置；集數那條則會讓開 logo。
    """
    if blc is None or not air:
        return []
    span = w - band_left
    if span <= 200:
        return []
    text = (("%s　%s%s" % (title, blc.DATE_LABEL, air)) if title
            else ("%s%s" % (blc.DATE_LABEL, air)))
    label = os.path.join(TMP, "label-%s.png" % tag)
    ow, _ = wmtext.render(text, label, size=blc.TEXT_SIZE, stroke=blc.TEXT_STROKE)
    if ow > span:
        strip = os.path.join(TMP, "strip-%s.png" % tag)
        res = wmtext.render_marquee_strip(text, strip,
                                              tile_gap=blc.MARQUEE_GAP,
                                              size=blc.TEXT_SIZE, stroke=blc.TEXT_STROKE)
        if not res:
            return []
        sw, sh, tile = res
        pre = "crop=w=%d:h=%d:x='mod(t*%d,%d)':y=0" % (
            min(span, sw), sh, blc.MARQUEE_SPEED, tile)
        return [(strip, pre, "x=%d:y=%d" % (band_left, blc.MARQUEE_Y))]
    return [(label, None, "x=%d-w:y=%d" % (w, blc.OVERLAY_Y))]


def overlay(base, out, seconds, overlays):
    """把多層 overlay 疊到 base 上。base 可以是固定過場，也可以是輪播的 short。

    長度上限要取「指定上限」與「base 本身長度」的較小值：疊圖用了 -loop 1
    之後永遠不會結束，若只給 -t 上限，比它短的 base 會被撐長、尾巴變成凍結的
    最後一格（實測踩過：53 秒的 short 變成 90 秒）。
    """
    base_d = blc.probe_seconds(base) or 0
    lim = int(min(seconds, base_d)) if base_d else int(seconds)
    return blc.normalize(base, out, 1280, 720, "libx264", "128k", 30,
                         overlays, lim)


SHORTS_FMT = ("bv*[height<=1080][protocol^=https]+ba[protocol^=https]/"
              "b[height<=1080][protocol^=https]/b[height<=1080]/b")


def fetch_shorts(url, count):
    p = subprocess.run(["/opt/homebrew/bin/yt-dlp", "--no-warnings",
                        "--socket-timeout", "25", "--flat-playlist",
                        "-I", "1:%d" % count, "--print", "%(id)s", url],
                       capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL)
    return [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]


def land_short(sid, media_dir, raw_dir):
    """下載一支 short 並正規化成與其他片段一致的參數。

    shorts 是直式（例如 1080x1920），這裡會等比縮小後補黑邊放進 1280x720，
    也就是「放不下就縮小」的做法 —— 不裁切、不變形。
    """
    dst = os.path.join(media_dir, "short-%s.mp4" % sid)
    if os.path.exists(dst):
        return dst, ""
    os.makedirs(raw_dir, exist_ok=True)
    raw = os.path.join(raw_dir, "short-%s.mp4" % sid)
    p = subprocess.run(["/opt/homebrew/bin/yt-dlp", "--no-warnings",
                        "--no-playlist", "--retries", "5",
                        "--fragment-retries", "10", "-f", SHORTS_FMT,
                        "--merge-output-format", "mp4", "--remux-video", "mp4",
                        "-o", raw,
                        "https://www.youtube.com/watch?v=%s" % sid],
                       capture_output=True, text=True, timeout=1800,
                       stdin=subprocess.DEVNULL)
    if not os.path.exists(raw):
        return None, (p.stderr or "").strip()[-160:]
    err = blc.normalize(raw, dst, 1280, 720, "libx264", "128k", 30, None, 0)
    if err:
        return None, err
    try:
        os.remove(raw)
    except OSError:
        pass
    return dst, ""


def verify(path):
    """從編碼後的影片抽格解碼 —— 只看畫面有東西不算數。"""
    try:
        import cv2
    except ImportError:
        return None
    subprocess.run(["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-nostdin",
                    "-loglevel", "error", "-y", "-ss", "10", "-i", path,
                    "-frames:v", "1", os.path.join(TMP, "_v.png")], check=True)
    img = cv2.imread(os.path.join(TMP, "_v.png"))
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--parallel", type=int,
                    default=int(blc.cfg("content", "transitions_parallel", 3)) if blc else 3)
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--verify", type=int, default=0,
                    help="做完後抽幾支做解碼驗證（0＝不驗）")
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist-local.json"),
                    help="從哪份清單決定集數順序（預設 playlist-local.json）。"
                         "要對新清單產生過場時，指向那份母清單即可")
    ap.add_argument("--shorts-url", default="",
                    help="改成用該網址的 shorts 輪播當過場（例如頻道的 /shorts）。"
                         "留空則用固定的 _transition-clean.mp4")
    ap.add_argument("--shorts-count", type=int, default=30,
                    help="抓幾支 shorts 當輪播池（預設 30，上限 50）")
    ap.add_argument("--button-caption",
                    default=(blc.cfg("overlay", "transition_caption", "去追劇")
                             if blc else "去追劇"),
                    help="過場 QR 按鈕的說明文字（預設「去追劇」；集數是「看原片」）")
    ap.add_argument("--no-button", action="store_true",
                    help="過場不要疊 QR 按鈕")
    # 播出中要換過場時，先輸出到暫存目錄、驗完再 mv 進去：mv 是原子置換，
    # 播出端（-c copy 每個循環重開檔案）只會拿到完整的舊檔或新檔。
    ap.add_argument("--out-dir", default="",
                    help="輸出目錄（預設 media/）。給暫存目錄可避免播出端讀到半成品")
    # shorts 是獨立輪動、不跟影片趟數對齊：第 p 趟第 i 支用的是池子裡第
    # (p*集數 + i - 1) 支。所以 30 支影片配 50 支 shorts 時，passes=2 會讓
    # 第二趟從第 31 支 short 接著播。
    ap.add_argument("--passes", type=int, default=1,
                    help="影片重複幾趟（預設 1）。用來讓 shorts 池全部輪到")
    a = ap.parse_args()

    out_dir = a.out_dir or MEDIA
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.exists(CLEAN) and not a.shorts_url:
        print("找不到 %s，請先保留一份沒有 QR 的乾淨過場" % CLEAN, file=sys.stderr)
        return 2

    d = json.load(open(a.playlist, encoding="utf-8"))
    eps = [s for s in d["segments"] if s["id"] != "_tr"]
    if a.limit:
        eps = eps[:a.limit]
    os.makedirs(TMP, exist_ok=True)

    # 過場底稿：固定一支，或一池 shorts 輪流用
    if a.shorts_url:
        n = min(a.shorts_count or 30, 50)
        ids = fetch_shorts(a.shorts_url, n)
        print("取得 %d 支 shorts，開始下載與正規化…" % len(ids), flush=True)
        bases = []
        raw_dir = os.path.join(MEDIA, ".raw")
        for k, sid in enumerate(ids, 1):
            dst, err = land_short(sid, MEDIA, raw_dir)
            if dst:
                bases.append(dst)
                print("  short %2d/%d %-13s -> %s"
                      % (k, len(ids), sid, os.path.basename(dst)), flush=True)
            else:
                print("  short %-13s 失敗：%s" % (sid, err), flush=True)
        if not bases:
            print("沒有任何 short 可用，中止", file=sys.stderr)
            return 3
    else:
        bases = [CLEAN]

    jobs = []
    for p in range(max(1, a.passes)):
        for i, seg in enumerate(eps, 1):
            sid = seg["id"]
            url = "https://youtu.be/%s" % sid
            # 用影片 ID 命名，不要用序號 —— 序號在換清單時會互相覆蓋，
            # 而且切回舊清單時會拿到錯的 QR。第 2 趟之後加 _pN，因為同一支影片
            # 在不同趟要配不同的 short。
            name = "_tr_%s.mp4" % sid if p == 0 else "_tr_%s_p%d.mp4" % (sid, p + 1)
            out = os.path.join(out_dir, name)

            # QR：位置與格式都跟集數一模一樣（右上角、同樣的面板與說明文字），
            # 不做任何區分。先做按鈕才知道佔多寬，跑馬燈的右界要靠它算。
            btn = os.path.join(TMP, "qrb-%s.png" % sid)
            btn_w = 0
            if not a.no_button:
                kw = {"caption": a.button_caption} if a.button_caption else {}
                btn_w, _ = wmtext.render_link_button(url, btn, size=blc.QR_SIZE,
                                                 qr_px=blc.QR_PX,
                                                 **kw)

            # 標題列：右界到按鈕左緣，左界 0（直式短片兩側是黑邊，不用讓開 logo）
            ov = title_overlays(seg.get("title") or "", seg.get("air_date") or "",
                                w=1280 - btn_w, tag=sid, band_left=0)
            if not a.no_button:
                ov.append((btn, None, "x=W-w:y=0"))
            if not a.no_button and blc.SPONSOR_URL:
                sb = os.path.join(TMP, "sponsor-%s.png" % sid)
                wmtext.render_link_button(blc.SPONSOR_URL, sb, size=blc.QR_SIZE,
                                          qr_px=blc.QR_PX,
                                          caption=blc.SPONSOR_CAPTION)
                ov.append((sb, None, "x=W-w:y=H-h"))

                # shorts 連續輪動：第 p 趟第 i 支用池子裡第 (p*len(eps) + i - 1) 支。
                idx = p * len(eps) + (i - 1)
                jobs.append({"i": i, "pass": p, "sid": sid, "url": url, "out": out,
                             "base": bases[idx % len(bases)], "ov": ov})

    print("產生 %d 段過場（標題列 ＋ QR）…" % len(jobs), flush=True)
    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        futs = {ex.submit(overlay, j["base"], j["out"], a.seconds, j["ov"]): j
                for j in jobs}
        for f, j in list(futs.items()):
            err = f.result()
            if err:
                fail += 1
                print("FAIL %02d %-14s %s" % (j["i"], j["sid"], err), flush=True)
            else:
                done += 1
    print("編碼完成 OK=%d FAIL=%d，耗時 %.0f 秒" % (done, fail, time.time() - t0))

    if a.verify and fail == 0:
        print("抽樣驗證（從影片解碼）…", flush=True)
        step = max(1, len(jobs) // a.verify)
        bad = 0
        for j in jobs[::step][:a.verify]:
            got = verify(j["out"])
            want = j["url"]
            ok = (got == want)
            if not ok:
                bad += 1
            print("  %02d %-14s %s  %r"
                  % (j["i"], j["sid"], "OK " if ok else "壞", got))
        print("驗證結果：%d/%d 正確" % (a.verify - bad, a.verify))
        return 0 if bad == 0 else 1
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
