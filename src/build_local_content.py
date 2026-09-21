#!/usr/bin/env python3
"""把 playlist.json 的 YouTube 影片抓成本機檔案，並產生可離線播出的 playlist-local.json。

為什麼要落地
  YouTube 會對這個對外 IP 的匿名 player 請求「間歇性」回 LOGIN_REQUIRED
  （Sign in to confirm you are not a bot）。實測：05:11 一批影片全滅，
  20:02 同一批用完全相同的條件重測全部成功。所以那是暫時性的 IP 標記，
  不是永久封鎖。封鎖期間唯一仍可用的是 player_client=android（上限 360p，
  由 --no-fallback-client 可關）。因為它會反覆發生，離線副本才是 24/7 播出
  的穩定做法：除了繞開 bot 檢查，還一併移除來源 URL 6 小時過期與
  googlevideo 中途 reset 這兩個風險。內容本來就是同一團體授權的作品。

為什麼要正規化
  concat 的 -c copy 要求所有片段參數完全相同。實測這批內容混了
  1280x720 與 1280x718（另有一批 480p），全部丟進 concat 會壞掉。
  所以預設在落地時就轉成統一參數（--target 720），播出端才能純 copy。

用法
  python3 build_local_content.py --status                # 只回報還缺哪些
  python3 build_local_content.py --limit 3               # 先抓 3 支試水溫
  python3 build_local_content.py --cookies cookies.txt   # 需要登入時
  python3 build_local_content.py --target 480            # 改成統一輸出 480p
  python3 build_local_content.py --no-normalize          # 保留原始參數
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import wmtext                      # 日期浮水印（同目錄的點陣字模組）
except ImportError:
    wmtext = None

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(HERE, "media")
RAW_DIR = os.path.join(MEDIA_DIR, ".raw")
MANIFEST = os.path.join(MEDIA_DIR, "manifest.json")

# ── 共用設定 ────────────────────────────────────────────────────────
# settings.json 是「給人改的」那一份（WebUI 也是編輯它）。下面的值只是預設值，
# 命令列參數永遠可以逐次覆寫；檔案不存在時，行為與沒有這個機制時完全相同。
SETTINGS_FILE = os.path.join(HERE, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


SETTINGS = load_settings()


def cfg(section, key, default):
    """讀 settings.json 的 section.key；沒有、或寫成 null，就回 default。"""
    try:
        v = SETTINGS[section][key]
    except (KeyError, TypeError):
        return default
    return default if v is None else v

# 落地時抓 720p 就夠（正規化目標就是 720p），抓 1080p 只是多花一倍頻寬。
# 一定要排除 m3u8：實測 Ig3vtqtXowY 走 HLS 那條只會拿到 137s，DASH 才是完整 270s。
DEFAULT_FMT = ("bv*[height<=720][protocol^=https]+ba[protocol^=https]/"
               "b[height<=720][protocol^=https]/"
               "bv*[height<=720]+ba/b[height<=720]/b")
FALLBACK_FMT = "b[height<=360]/b"
FALLBACK_CLIENT = "android"

TARGETS = {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)}

BLACK_TAIL_MIN = float(cfg("content", "black_tail_min", 5.0))   # 片尾黑畫面幾秒算黑尾
BLACK_TAIL_SLACK = float(cfg("content", "black_tail_slack", 2.5))  # 黑尾結束點要落在片尾幾秒內

# ── 語言（後台介面與畫面上的字樣）──────────────────────────────────
# zh＝全中文、en＝全英文、both＝雙語。畫面空間有限，所以雙語一律用「／」串起來，
# 共用的開頭符號（▶）只留一個，冒號也只留最後一個。
UI_LANG = str(cfg("ui", "lang", "both")).strip().lower()


def L(zh, en):
    """依 UI_LANG 挑字串。"""
    zh, en = (zh or ""), (en or "")
    if UI_LANG == "en":
        return en or zh
    if UI_LANG == "zh":
        return zh or en
    z, e = zh.strip(), en.strip()
    if not z or not e:
        return z or e
    colon = z.endswith(("：", ":")) or e.endswith(("：", ":"))
    z, e = z.rstrip("：: 　"), e.rstrip("：: 　")
    for sym in ("▶", "►"):
        if z.startswith(sym) and e.startswith(sym):
            e = e[len(sym):].strip()
    return "%s／%s%s" % (z, e, "：" if colon else "")


DATE_LABEL = L(cfg("overlay", "date_label", "首播日期："),
               cfg("overlay", "date_label_en", "First aired: "))
LINK_CAPTION = L(cfg("overlay", "link_caption", "▶ 看原片"),
                 cfg("overlay", "link_caption_en", "▶ Watch original"))
TRANSITION_CAPTION = L(cfg("overlay", "transition_caption", "去追劇"),
                       cfg("overlay", "transition_caption_en", "Watch more"))
# 倒數的文字：中文放前面（「剩餘 02:57」）、英文放後面（「02:57 left」），
# 雙語就是「剩餘 02:57 left」。
_CD_PRE, _CD_SUF = cfg("overlay", "countdown_prefix", "剩餘 "), cfg("overlay", "countdown_suffix", "")
_CD_PRE_EN = cfg("overlay", "countdown_prefix_en", "")
_CD_SUF_EN = cfg("overlay", "countdown_suffix_en", " left")
if UI_LANG == "zh":
    CD_PRE, CD_SUF = _CD_PRE, _CD_SUF
elif UI_LANG == "en":
    CD_PRE, CD_SUF = _CD_PRE_EN, _CD_SUF_EN
else:
    CD_PRE, CD_SUF = _CD_PRE, _CD_SUF_EN
AUDIO_FADE = float(cfg("media", "audio_fade", 2.5))       # 開頭淡入／結尾淡出幾秒
OVERLAY_Y = int(cfg("overlay", "overlay_y", 40))          # 浮水印距離畫面頂端
MARQUEE_Y = OVERLAY_Y - 30                                # 跑馬燈再往上位移半行
OVERLAY_MARGIN = int(cfg("overlay", "overlay_margin", 40))  # 左右邊界
MARQUEE_SPEED = int(cfg("overlay", "marquee_speed", 120))   # 跑馬燈速度（像素／秒）
MARQUEE_GAP = int(cfg("overlay", "marquee_gap", 220))       # 跑馬燈兩輪之間的空白

# 編碼參數。位元率是最直接影響畫質與頻寬的旋鈕，不要寫死在指令列裡。
VIDEO_PRESET = cfg("media", "preset", "veryfast")
VIDEO_LEVEL = cfg("media", "level", "3.1")
VIDEO_BITRATE = cfg("media", "video_bitrate", "2500k")
VIDEO_MAXRATE = cfg("media", "video_maxrate", "2500k")
VIDEO_BUFSIZE = cfg("media", "video_bufsize", "5000k")
AUDIO_RATE = int(cfg("media", "sample_rate", 48000))

# 畫面元素的尺寸
TEXT_SIZE = int(cfg("overlay", "text_size", 44))
TEXT_STROKE = int(cfg("overlay", "text_stroke", 4))
QR_SIZE = int(cfg("overlay", "qr_size", 30))
QR_PX = int(cfg("overlay", "qr_px", 120))

# 贊助／抖內 QR：只畫在過場影片上（集數不畫），固定放右下角。
SPONSOR_URL = cfg("overlay", "sponsor_url", "")
SPONSOR_CAPTION = L(cfg("overlay", "sponsor_caption", "贊助"),
                    cfg("overlay", "sponsor_caption_en", "Support"))
# 贊助碼：填這個值就整個關掉贊助 QR（後台設定的緊急開關）
SPONSOR_CODE = cfg("overlay", "sponsor_code", "")
if SPONSOR_CODE.strip() == "kingwap99":
    SPONSOR_URL = ""

TRANSITION_FILE = os.path.join(MEDIA_DIR, "_transition.mp4")
TRANSITION_ID = "_tr"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def probe_seconds(path):
    """用 ffprobe 量實際長度。排程要靠這個，不能用來源宣稱的長度。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        if p.returncode == 0 and p.stdout.strip():
            return float(p.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return None


def check_duration(got, expect, tol=0.05, min_abs=10.0):
    """下載後比對長度。實測有影片會靜默只抓到一半（137s vs 270s），
    不檢查的話會直接進 concat，變成播出中段突然跳掉。"""
    if not got or not expect:
        return None
    expect = float(expect)
    diff = abs(got - expect)
    if diff > max(min_abs, tol * expect):
        return "duration mismatch: got %.1fs, the list claims %.1fs" % (got, expect)
    return None


def fetch_raw(seg, cookies, fmt, client=None):
    """抓一支影片到 media/.raw/<id>.mp4，回傳 (路徑, 錯誤訊息)。"""
    sid = seg["id"]
    os.makedirs(RAW_DIR, exist_ok=True)
    url = seg.get("url") or ("https://www.youtube.com/watch?v=" + sid)
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist",
           "--sleep-requests", "1", "--retries", "5", "--fragment-retries", "10"]
    if cookies:
        cmd += ["--cookies", cookies]
    if client:
        cmd += ["--extractor-args", "youtube:player_client=" + client]
    cmd += ["-f", fmt, "--merge-output-format", "mp4", "--remux-video", "mp4",
            "-o", os.path.join(RAW_DIR, sid + ".%(ext)s"), url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                       stdin=subprocess.DEVNULL)
    raw = os.path.join(RAW_DIR, sid + ".mp4")
    if p.returncode != 0 or not os.path.exists(raw):
        tail = (p.stderr.strip().splitlines() or [""])[-1]
        return None, tail[:200]
    return raw, ""


def has_audio(path):
    """這支檔案有沒有音軌。淡入淡出只對有音軌的做。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
        return bool((p.stdout or "").strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def normalize(raw, out, w, h, venc, abr, fps, overlays=None, max_seconds=0,
              fade_sec=AUDIO_FADE):
    """轉成統一參數，讓播出端可以純 -c copy 拼接。

    overlays 是 [(PNG 路徑, 前置濾鏡或 None, overlay 位置表達式), ...]，
    依序疊上去。前置濾鏡用來先把圖裁成固定視窗（跑馬燈靠它才不會溢出）。
    位置表達式若有逗號，一定要用單引號包起來，否則會被當成 filter 的參數
    分隔 —— 跑馬燈就會踩到這個。
    max_seconds > 0 時只保留前幾秒（做縮短的測試版用）。
    """
    vf = ("scale=w=%d:h=%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,format=yuv420p"
          % (w, h, w, h, fps))
    if venc == "h264_videotoolbox":
        vargs = ["-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
                 "-profile:v", "high", "-g", str(fps * 2)]
    else:
        vargs = ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-profile:v", "high",
                 "-level", VIDEO_LEVEL, "-g", str(fps * 2), "-b:v", VIDEO_BITRATE,
                 "-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE]
    # -nostdin 是必要的：這個腳本常被 ssh heredoc 帶著跑，ffmpeg 若去讀
    # stdin 會把腳本內容當成互動指令，讀到 q 就提早結束，輸出被靜默截斷
    # （實測同一支影片分別得到 137s 與 163s）。stdin=DEVNULL 是第二層保險。
    tail = ["-c:a", "aac", "-b:a", abr, "-ar", str(AUDIO_RATE), "-ac", "2"]
    # 長度上限一定要跟原始檔長度取較小值，兩個理由：
    #   1) 疊圖的 -loop 1／loop 讓圖永遠不結束，不給上限就會一直編下去
    #      （實測踩過：檔案無限長大）。
    #   2) 就算給了上限，上限若「大於」原始長度，因為圖還在、主影片已經 EOF，
    #      overlay 會 repeatlast 把最後一格重複到上限 —— 影片被撐長、音訊卻在
    #      原始長度就結束。實測 209 秒的影片被做成 450 秒：影格 13500 格（450 秒）
    #      但音軌只有 209.1 秒。播出端 -c copy 走到那段「有影無聲」的尾巴會卡住，
    #      我的看門狗 20 秒後把播出端砍掉重連，觀眾看到的就是「播到第二支又跳回
    #      第一支」（2026-09-19 實測，每 728 秒循環一次）。
    raw_d = int(probe_seconds(raw) or 0)
    if max_seconds and raw_d:
        max_seconds = min(int(max_seconds), int(raw_d + 0.999))
    elif overlays and not max_seconds and raw_d:
        max_seconds = int(raw_d + 0.999)
    if max_seconds:
        tail += ["-t", str(int(max_seconds))]

    # 音訊淡入淡出：切換影片時，前後兩段的接縫兩邊都有淡化，不會突然斷掉或
    # 蹦一聲。只動音訊，影像仍然是 copy（或走上面的 overlay 鏈）。
    dur = int(max_seconds) if max_seconds else (probe_seconds(raw) or 0)
    af = ""
    if fade_sec and dur > fade_sec * 2 + 1 and has_audio(raw):
        af = ("afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f"
              % (fade_sec, dur - fade_sec, fade_sec))
    # 以前這裡有 -movflags +faststart。實測（2026-09-19）它會讓 ffmpeg 隨機卡在收尾
    # 階段：資料都寫完了、moov 沒寫出來、CPU 0%、主執行緒停在 sch_wait，同一支影片
    # 重跑有時又正常（60 秒的短片也會中，跟長度無關；今天 4 次）。faststart 是為了
    # 網路漸進播放，我們的播出端是本機檔案 + concat + -c copy，ffmpeg 會自己去檔尾
    # 讀 moov，不需要它。拿掉之後同一支 60 秒素材 8.6 秒完成。
    tail += [out]
    if overlays:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
               "-y", "-i", raw]
        for ov in overlays:
            if len(ov) > 3 and ov[3]:
                # 每秒一張的序列（倒數用）：1 fps 輸入，overlay 依時間自動換圖
                cmd += ["-thread_queue_size", "512", "-framerate", "1",
                        "-start_number", "0", "-i", ov[0]]
            else:
                # 圖輸入是「單格」，重複的動作交給下面的 loop 濾鏡，不再用 -loop 1。
                # -loop 1 是 demuxer 每一格都重送一次封包，等於每個輸出影格都把整張
                # PNG 重解一次；倒數長條（450 秒 = 266x25650 px）的代價隨片長平方
                # 成長，實測同一支 450 秒素材要 197 秒才編完。loop 濾鏡只解碼一次、
                # 之後重複同一個 frame：同一支降到 47 秒（4.2 倍），而且畫面上的效果
                # 一樣 —— 它照樣輸出帶遞增時間戳的影格，crop 的時間表達式照常運作。
                #
                # -thread_queue_size 是修另一件事：無限輸入若由主執行緒餵，主執行緒
                # 一旦卡在等濾鏡圖（sch_wait）就會互相等死，症狀是資料都寫完了、
                # moov 沒寫出來、CPU 0%、其他執行緒全部閒置。實測 3/3 正常。
                cmd += ["-thread_queue_size", "512", "-framerate", str(fps),
                        "-i", ov[0]]
        parts = ["[0:v]%s[v0]" % vf]
        cur = "v0"
        for i, ov in enumerate(overlays, start=1):
            png, pre, pos = (list(ov) + [None, None, None])[:3]
            is_seq = len(ov) > 3 and ov[3]
            src = "%d:v" % i
            # 單格 PNG 用 loop 濾鏡重複（只解碼一次）；1 fps 序列輸入本來就是有限的，
            # 不需要 loop（加了反而會凍在第一格）。
            chain = "" if is_seq else "loop=loop=-1:size=1:start=0"
            if pre:
                chain = (chain + "," + pre) if chain else pre
            if chain:
                parts.append("[%s]%s[o%d]" % (src, chain, i))
                src = "o%d" % i
            nxt = "out" if i == len(overlays) else "v%d" % i
            parts.append("[%s][%s]overlay=%s[%s]" % (cur, src, pos, nxt))
            cur = nxt
        if af:
            parts.append("[0:a]%s[aout]" % af)
            maps = ["-map", "[out]", "-map", "[aout]"]
        else:
            maps = ["-map", "[out]", "-map", "0:a?"]
        cmd += ["-filter_complex", ";".join(parts)] + maps + vargs + tail
    else:
        cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-i", raw, "-vf", vf] + (["-af", af] if af else []) + vargs + tail)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0 or not os.path.exists(out):
        return (p.stderr.strip().splitlines() or [""])[-1][:200]
    return ""


def make_countdown_frames(total, out_dir, size=28, prefix=""):
    """產生每秒一張的倒數圖（前綴 ＋ 剩餘 MM:SS），檔名是 5 位數流水號。

    為什麼要一秒一張：影片畫面不能直接畫字（沒有 freetype），倒數又必須隨
    時間變化。所以事先把每一秒的圖畫好，交給 ffmpeg 用 1 fps 的序列輸入，
    overlay 就會依時間自己換圖。
    """
    n = int(total)
    os.makedirs(out_dir, exist_ok=True)
    for k in range(n):
        remain = n - k
        wmtext.render_badge("%s%s%02d:%02d%s"
                            % (prefix, CD_PRE, remain // 60, remain % 60, CD_SUF),
                            os.path.join(out_dir, "%05d.png" % k), size=size)
    return n


def black_runs(path, min_len=BLACK_TAIL_MIN):
    """回傳 [(start, end, duration)]。只解關鍵帧，一趟約 1 秒，很便宜。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
           "-skip_frame", "nokey", "-i", path,
           "-vf", "blackdetect=d=%.2f:pix_th=0.10" % min_len,
           "-an", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=900, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return []
    return [(float(a), float(b), float(c)) for a, b, c in re.findall(
        r"black_start:([0-9.]+) black_end:([0-9.]+) "
        r"black_duration:([0-9.]+)", p.stderr)]


def black_tail(path, seconds, min_len=BLACK_TAIL_MIN):
    """片尾黑畫面的起點秒數；沒有黑尾就回 None。

    為什麼要這個：實測 8jtdcMDuV_A 片尾有 66 秒黑畫面。播出端完全看不出來
    （傳輸連續、時間軸也沒有洞），但觀眾端就是一片黑。黑畫面是內容問題，
    只有看像素才抓得到，所以要在落地時就擋掉。
    """
    if not seconds:
        return None
    tails = [r for r in black_runs(path, min_len)
             if r[1] >= float(seconds) - BLACK_TAIL_SLACK]
    return min(r[0] for r in tails) if tails else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist.json"))
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--status", action="store_true", help="only report what is missing, do not download")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--limit", type=int, default=0, help="max videos to fetch this run (0 = no limit)")
    ap.add_argument("--format", default=DEFAULT_FMT, help="yt-dlp format selector")
    ap.add_argument("--no-fallback-client", action="store_true",
                    help="disable the android client fallback")
    ap.add_argument("--no-normalize", action="store_true",
                    help="skip normalization, keep the source parameters (concat will break)")
    ap.add_argument("--target", default=cfg("media", "target", "720"),
                    choices=sorted(TARGETS), help="normalization target resolution")
    ap.add_argument("--venc", default=cfg("media", "venc", "libx264"),
                    choices=["libx264", "h264_videotoolbox"])
    ap.add_argument("--abr", default=cfg("media", "abr", "128k"),
                    help="audio bitrate")
    ap.add_argument("--fps", type=int, default=int(cfg("media", "fps", 30)))
    ap.add_argument("--audio-fade", type=float, default=AUDIO_FADE,
                    help="audio fade in/out seconds per segment (default: settings.json)")
    ap.add_argument("--respect-playlist-format", action="store_true",
                    help="use the per-segment format from the list (default: ignore, use --format)")
    ap.add_argument("--rescan", action="store_true",
                    help="no fetching; rescan black tails of local files and rebuild the playout list")
    ap.add_argument("--black-tail-min", type=float, default=BLACK_TAIL_MIN,
                    help="seconds of black at the tail to count as a black tail (default 5.0)")
    ap.add_argument("--no-auto-trim", action="store_true",
                    help="only mark black tails, do not trim them")
    ap.add_argument("--transition", default="",
                    help="transition video (YouTube URL / video id / local file path)."
                         "kept once landed; add --force to fetch it again")
    ap.add_argument("--no-transition", action="store_true",
                    help="disable transitions even if the shared _transition.mp4 exists")
    ap.add_argument("--no-date-overlay", action="store_true",
                    help="do not burn in the first-air watermark (drawn top-right when the list has air_date)")
    ap.add_argument("--out-playlist", default=os.path.join(HERE, "playlist-local.json"),
                    help="output playout list path (default playlist-local.json)."
                         "point it elsewhere for a trial run so the live one is untouched")
    # 播出中要換檔時，先寫到暫存目錄、驗完再 mv 進 media/：mv 是原子置換，
    # 播出端（每個循環重開檔案）只會拿到完整的舊檔或新檔。要搭配 --force。
    ap.add_argument("--out-dir", default="",
                    help="directory for normalized files (default media/)."
                         "for live replacement point it at a staging dir, then mv the verified files in")
    ap.add_argument("--max-seconds", type=int,
                    default=int(cfg("media", "max_seconds", 0)),
                    help="keep at most this many seconds per video (0 = full length); used for short test editions")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the downloaded originals (media/.raw/) so you can re-encode"
                         "without downloading again and without another quality loss")
    ap.add_argument("--no-link-button", action="store_true",
                    default=not bool(cfg("overlay", "link_button", True)),
                    help="do not overlay the link button (QR + short URL)")
    ap.add_argument("--no-countdown", action="store_true",
                    default=not bool(cfg("overlay", "countdown", True)),
                    help="do not show the remaining-time countdown under the QR")
    ap.add_argument("--link-caption", default=LINK_CAPTION,
                    help="caption on the episode link button (zh/en/both per ui.lang)")
    ap.add_argument("--band-left", type=int, default=int(cfg("overlay", "band_left", 0)),
                    help="marquee left bound in pixels (0 = use 1/7 of the width)."
                         "that space is reserved for the original video's top-left logo; use a larger value for more")
    # 內容改成「每個模式一份」：media/<模式>/。同一支影片在不同模式有不同長度
    # 上限時才不會互相蓋掉，manifest 也各自一份（原始檔 media/.raw 仍共用）。
    ap.add_argument("--media-dir", default="",
                    help="content directory (default media/); use media/<mode> when several modes coexist")
    # 過場是「第 i 支影片配第 i 支 short」，一輪只用到池子前 N 支。--passes 讓
    # 一輪播出包含多趟影片，shorts 接著往下輪（第 2 趟從第 31 支起）。
    ap.add_argument("--passes", type=int, default=int(cfg("media", "passes", 1)),
                    help="how many passes of videos per round (default 1) so the whole shorts pool gets used")
    args = ap.parse_args()

    if args.media_dir:
        global MEDIA_DIR, MANIFEST, TRANSITION_FILE
        MEDIA_DIR = os.path.abspath(args.media_dir)
        MANIFEST = os.path.join(MEDIA_DIR, "manifest.json")
        TRANSITION_FILE = os.path.join(MEDIA_DIR, "_transition.mp4")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    pl = load_json(args.playlist, None)
    if not pl:
        raise SystemExit("playlist not found: %s" % args.playlist)

    cookies = args.cookies
    if cookies is None:
        auto = os.path.join(HERE, "cookies.txt")
        cookies = auto if os.path.exists(auto) else ""

    manifest = load_json(MANIFEST, {})
    segs = [s for s in pl["segments"] if s.get("type") in (None, "vod", "live")]

    missing = []
    for seg in segs:
        path = os.path.join(args.out_dir or MEDIA_DIR, seg["id"] + ".mp4")
        if args.force or not os.path.exists(path):
            missing.append(seg)

    mode = "source parameters (not recommended)" if args.no_normalize else "%dx%d" % TARGETS[args.target]
    log("list has %d segments, %d present, %d missing; output format %s"
        % (len(segs), len(segs) - len(missing), len(missing), mode))

    if args.status:
        for seg in missing:
            log("  missing %s" % seg["id"])
        have = [m for m in manifest.values() if m.get("seconds")]
        total = sum(m.get("seconds") or 0 for m in have)
        log("manifest: %d segments measured, %.0fs total (%.2f h)"
            % (len(have), total, total / 3600.0))
        return 0 if not missing else 2

    if args.rescan:
        hits = 0
        for seg in segs:
            path = os.path.join(MEDIA_DIR, seg["id"] + ".mp4")
            if not os.path.exists(path):
                continue
            sec = probe_seconds(path)
            cut = black_tail(path, sec, args.black_tail_min)
            rec = manifest.get(seg["id"]) or {}
            rec["file"] = os.path.relpath(path, HERE)
            rec["seconds"] = round(sec, 3) if sec else None
            rec["bytes"] = os.path.getsize(path)
            if cut:
                rec["black_tail_start"] = round(cut, 3)
                rec["black_tail_len"] = round((sec or 0) - cut, 3)
                if not args.no_auto_trim:
                    rec["outpoint"] = round(cut, 3)
                hits += 1
                log("WARN %-13s black tail %.1fs (starts %.2fs)%s"
                    % (seg["id"], rec["black_tail_len"], cut,
                       "" if args.no_auto_trim else " -> trimmed"))
            else:
                for k in ("outpoint", "black_tail_start", "black_tail_len"):
                    rec.pop(k, None)
            manifest[seg["id"]] = rec
        save_json(MANIFEST, manifest)
        log("rescan done: %d segments scanned, %d with a black tail" % (len(segs), hits))
        missing = []

    if not missing:
        log("nothing to fetch; rebuilding the playout list")

    w, h = TARGETS[args.target]
    done = fail = 0
    # 影片在母清單裡的序號（倒數標籤要顯示「第幾支／共幾支」）
    seg_ord = {s["id"]: i for i, s in enumerate(segs, 1)}
    for seg in missing[:args.limit or None]:
        sid = seg["id"]
        t0 = time.time()
        fmt = seg.get("format") if args.respect_playlist_format else args.format
        attempts = [(fmt or DEFAULT_FMT, None), (fmt or DEFAULT_FMT, None)]
        if not args.no_fallback_client:
            attempts.insert(1, (FALLBACK_FMT, FALLBACK_CLIENT))
        raw = err = bad = None
        kept = os.path.join(RAW_DIR, sid + ".mp4")
        if args.keep_raw and os.path.exists(kept):
            raw = kept
            log("     %s reusing the kept original (--keep-raw)" % sid)
        else:
            for (afmt, aclient) in attempts:
                tag = aclient or "default"
                raw, err = fetch_raw(seg, cookies, afmt, aclient)
                if not raw:
                    log("     %s [%s] download failed" % (sid, tag))
                    continue
                bad = check_duration(probe_seconds(raw), seg.get("seconds"))
                if not bad:
                    break
                log("     %s [%s] %s" % (sid, tag, bad))
                try:
                    os.remove(raw)
                except OSError:
                    pass
                raw = None
        if not raw:
            fail += 1
            log("FAIL %-13s %s" % (sid, bad or err))
            save_json(MANIFEST, manifest)
            continue

        out = os.path.join(args.out_dir or MEDIA_DIR, sid + ".mp4")
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
        if args.no_normalize:
            shutil.move(raw, out)
            nerr = ""
        else:
            overlays = []
            os.makedirs(RAW_DIR, exist_ok=True)

            # 先做連結按鈕：它貼齊右上角、佔掉一段寬度，跑馬燈可用範圍要靠它算。
            btn_w = 0
            btn_h = 0
            if not args.no_link_button and wmtext:
                try:
                    btn = os.path.join(RAW_DIR, "btn-%s.png" % sid)
                    btn_w, btn_h = wmtext.render_link_button(
                        "https://youtu.be/%s" % sid, btn, size=QR_SIZE, qr_px=QR_PX,
                        caption=args.link_caption)
                    overlays.append((btn, None, "x=W-w:y=0"))
                except Exception as exc:
                    log("      %s link button failed, skipping the overlay: %s" % (sid, exc))
                    btn_w = btn_h = 0

            # 倒數：QR 按鈕下方顯示這支影片的剩餘播放時間。
            # 用「每秒一張圖」的序列疊上去，overlay 會依時間自動換圖。
            if btn_w and wmtext and not args.no_countdown:
                # 倒數長條的秒數也要跟原始長度取較小值，否則畫面會從 07:30 開始
                # 倒數，但影片 3 分半就結束了。
                raw_d = probe_seconds(raw) or 0
                total = args.max_seconds or raw_d
                if args.max_seconds and raw_d:
                    total = min(args.max_seconds, raw_d)
                try:
                    if total >= 10:
                        prefix = "%02d/%02d　" % (seg_ord.get(sid, 0),
                                                  len(segs))
                        strip = os.path.join(RAW_DIR, "cd-%s.png" % sid)
                        bw, bh = wmtext.render_countdown_strip(
                            prefix, total, strip, pre=CD_PRE, suf=CD_SUF)
                        # 用時間裁切挑出當下那一格（跟跑馬燈同一套機制）。
                        # 不能用 1 fps 的序列輸入：跟 30 fps 主畫面在 overlay
                        # 裡對不起來，整層會消失而且不會報錯（實測）。
                        pre = ("crop=w=%d:h=%d:x=0:y='floor(t)*%d'"
                               % (bw, bh, bh))
                        overlays.append((strip, pre,
                                         "x=W-w:y=%d" % btn_h))
                        log("      %s countdown strip %dx%d (%d s, starts at '%s%s%02d:%02d%s')"
                            % (sid, bw, bh * int(total), int(total), prefix, CD_PRE,
                               int(total) // 60, int(total) % 60, CD_SUF))
                except Exception as exc:
                    log("      %s countdown failed, skipping the overlay: %s" % (sid, exc))

            # 標題列的可視範圍：[原片 logo 讓出的左界, 按鈕的左緣]
            # 左界固定留畫面寬度的 1/7 給原片左上角的 logo（自動判定容易誤判，
            # 所以採用固定比例；要覆寫就用 --band-left 給像素值）
            band_left = args.band_left if args.band_left > 0 else w // 7
            band_right = w - btn_w
            span = band_right - band_left
            air = seg.get("air_date")
            if air and not args.no_date_overlay and wmtext and span > 200:
                try:
                    text = "%s%s" % (DATE_LABEL, air)
                    title = (seg.get("title") or "").strip()
                    if title:
                        text = "%s　%s" % (title, text)
                    label = os.path.join(RAW_DIR, "label-%s.png" % sid)
                    ow, _ = wmtext.render(text, label, size=TEXT_SIZE, stroke=TEXT_STROKE)
                    if ow > span:
                        strip = os.path.join(RAW_DIR, "strip-%s.png" % sid)
                        res = wmtext.render_marquee_strip(
                            text, strip, tile_gap=MARQUEE_GAP, size=TEXT_SIZE,
                            stroke=TEXT_STROKE)
                        if not res:
                            raise RuntimeError("marquee strip generation failed")
                        sw, sh, tile = res
                        cw = min(span, sw)
                        pre = ("crop=w=%d:h=%d:x='mod(t*%d,%d)':y=0"
                               % (cw, sh, MARQUEE_SPEED, tile))
                        overlays.append((strip, pre,
                                         "x=%d:y=%d" % (band_left, MARQUEE_Y)))
                        log("      %s title %dpx > %dpx available, marquee (window "
                            "%d~%d, crop width %d)"
                            % (sid, ow, span, band_left, band_right, cw))
                    else:
                        overlays.append((label, None,
                                         "x=%d-w:y=%d"
                                         % (band_right, OVERLAY_Y)))
                except Exception as exc:
                    log("      %s watermark failed, skipping the overlay: %s" % (sid, exc))
            nerr = normalize(raw, out, w, h, args.venc, args.abr, args.fps,
                             overlays, args.max_seconds,
                             fade_sec=args.audio_fade)
            if not nerr and not args.keep_raw:
                try:
                    os.remove(raw)
                except OSError:
                    pass
        if nerr:
            fail += 1
            log("FAIL %-13s normalization failed: %s" % (sid, nerr))
            save_json(MANIFEST, manifest)
            continue

        done += 1
        seconds = probe_seconds(out)
        mrec = {
            "file": os.path.relpath(out, HERE),
            "seconds": round(seconds, 3) if seconds else None,
            "bytes": os.path.getsize(out),
            "fetched_at": int(time.time()),
            "target": "raw" if args.no_normalize else "%dx%d" % (w, h),
        }
        cut = black_tail(out, seconds, args.black_tail_min)
        if cut:
            mrec["black_tail_start"] = round(cut, 3)
            mrec["black_tail_len"] = round((seconds or 0) - cut, 3)
            if not args.no_auto_trim:
                mrec["outpoint"] = round(cut, 3)
            log("WARN %-13s black tail %.1fs (starts %.2fs)%s"
                % (sid, mrec["black_tail_len"], cut,
                   "" if args.no_auto_trim else " -> trimmed"))
        manifest[sid] = mrec
        save_json(MANIFEST, manifest)
        log("OK   %-13s %.1fs / %s  (%.1fs)"
            % (sid, seconds or -1, "%.1f MB" % (os.path.getsize(out) / 1e6),
               time.time() - t0))

    # 過場影片：--transition 給來源（URL／video id／本機檔案）。
    # 已落地就不重抓，--no-transition 可停用。
    if args.transition and (args.force or not os.path.exists(TRANSITION_FILE)):
        src = os.path.expanduser(args.transition)
        raw = None
        if os.path.exists(src):
            raw = src
        else:
            if "://" not in src and not src.startswith("www."):
                src = "https://www.youtube.com/watch?v=" + src
            log("fetching the transition video...")
            raw, err = fetch_raw({"id": TRANSITION_ID, "url": src},
                                 cookies, args.format)
            if not raw:
                log("transition download failed: %s" % err)
        if raw and os.path.exists(raw):
            nerr = normalize(raw, TRANSITION_FILE, w, h, args.venc,
                             args.abr, args.fps, fade_sec=args.audio_fade)
            if nerr:
                log("transition normalization failed: %s" % nerr)
            else:
                if os.path.abspath(raw) != os.path.abspath(TRANSITION_FILE):
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
                log("transition ready: %.1fs" % (probe_seconds(TRANSITION_FILE) or -1))

    # 重新產生離線播出清單：只納入真的在磁碟上、也量得到長度的檔案。
    def transition_for(episode_id, p=0):
        """某一集後面要接的過場。

        優先使用「一集一份」的 media/_tr_<影片id>.mp4 —— 它的 QR Code 指向
        剛播完那一集，觀眾掃碼就能去看原片；沒有專屬檔就退回共用的
        _transition.mp4。用影片 ID 而不是序號，換清單時才不會互相蓋掉。

        p > 0（多趟輪播）時用 _tr_<id>_p<N>.mp4：同一支影片在不同趟要配不同的
        short，所以每一趟都要有自己的過場檔。
        """
        if args.no_transition:
            return None
        names = ["_tr_%s.mp4" % episode_id]
        if p:
            names.insert(0, "_tr_%s_p%d.mp4" % (episode_id, p + 1))
        path = ""
        for n in names:
            cand = os.path.join(MEDIA_DIR, n)
            if os.path.exists(cand):
                path = cand
                break
        if not path:
            path = TRANSITION_FILE
        if not os.path.exists(path):
            return None
        ts = probe_seconds(path)
        if not ts:
            return None
        return {"id": TRANSITION_ID, "type": "file",
                "path": os.path.relpath(path, HERE),
                "mode": "copy", "seconds": round(ts, 3)}

    out_segs = []
    default_rel = os.path.relpath(MEDIA_DIR, HERE)
    for p in range(max(1, args.passes)):
        for seg in segs:
            m = manifest.get(seg["id"]) or {}
            path = m.get("file") or os.path.join(default_rel, seg["id"] + ".mp4")
            full = os.path.join(HERE, path)
            if not os.path.exists(full):
                continue
            seconds = m.get("seconds") or probe_seconds(full)
            if not seconds:
                log("skipping %s: no duration" % seg["id"], file=sys.stderr)
                continue
            rec = {"id": seg["id"], "type": "file", "path": path,
                   "mode": "copy", "seconds": round(seconds, 3)}
            if m.get("outpoint"):
                rec["outpoint"] = round(min(float(m["outpoint"]), seconds), 3)
            out_segs.append(rec)
            tr = transition_for(seg["id"], p)
            if tr:
                out_segs.append(tr)

    local = {"target": pl.get("target"), "clients": pl.get("clients"),
             "generated_from": os.path.basename(args.playlist),
             "normalized_to": "raw" if args.no_normalize else "%dx%d" % (w, h),
             "segments": out_segs}
    save_json(args.out_playlist, local)

    if os.path.isdir(RAW_DIR) and not os.listdir(RAW_DIR):
        os.rmdir(RAW_DIR)

    total = sum(s.get("outpoint") or s["seconds"] for s in out_segs)
    log("this run: OK=%d FAIL=%d; %s has %d segments, %.0fs total (%.2f h)"
        % (done, fail, os.path.basename(args.out_playlist),
           len(out_segs), total, total / 3600.0))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
