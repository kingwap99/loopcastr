# ytpl — 把 YouTube 播放清單變成 24/7 不中斷的直播頻道

[中文](#中文)｜[English](#english)　　雙語文件，**中文為主**

> ⚠️ **使用前請確認你有權使用來源內容。** 本系統會把影片抓成本機檔案、加工後**重新公開傳輸**，
> 只適用於你擁有著作權、或已取得權利人授權的內容。詳見文末〈使用聲明〉。

---

## 中文

### 這套系統在做什麼

    playlist.json（YouTube 網址）
         │  build_local_content.py —— 落地、正規化、驗證長度、偵測片尾黑畫面
         ▼
    media/<id>.mp4 ＋ media/_tr_<id>.mp4（每集專屬過場，QR 指向該集）
         │  make_concat_list.py
         ▼
    concat.txt
         │  playout.sh —— 單一行程 concat 循環播出（純 copy，不轉碼）
         ▼
    MediaMTX  rtmp://127.0.0.1:1935/live/main
         │  yt_publish.sh —— 單一長命 publisher，含自我看門
         ▼
    YouTube 直播 ingest

- **播出端是單一行程 concat**，不是每段換手接力。實測接力的換片縫是每段 2.0–3.1 秒，concat 是 0。
- **整條鏈路純 copy**，8 GB 的 M1 上兩條 ffmpeg 加起來只有約 6% CPU。
- **四層防護**：launchd KeepAlive（行程死掉自動拉起）、推流端自我看門（卡住 30 秒內自救）、
  `healthcheck.py`（流量停滯後自動重啟）、Telegram 告警。
- **換片一律淡出淡入。** 播出端是純 copy、接縫插不了濾鏡，所以淡化是落地時就烤進每一段檔案。

### 快速開始

    git clone <repo> && cd <repo>
    ./install.sh --dry-run     # 先看它會做什麼
    ./install.sh               # 裝到 ~/ytpl，並註冊 launchd 服務（需要 sudo）

服務**不會**在還沒有播出內容時啟動，避免 launchd 一直重啟一個註定失敗的行程。建內容的順序：

    cd ~/ytpl
    python3 build_playlist.py --url '<播放清單或頻道網址>' -o playlist.json
    python3 build_local_content.py --playlist playlist.json --target 720
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/ytpl
    printf %s '<YouTube 串流金鑰>' > stream.key && chmod 600 stream.key

`install.sh --agents` 可裝成 LaunchAgent（不需要 root，但要有圖形登入才會跑）；`--help` 看全部選項。

### 本機控制台

裝好之後，日常操作不必再背指令：

    python3 ~/ytpl/webui.py     # http://127.0.0.1:8787

| 區塊 | 內容 |
|---|---|
| 狀態 | 服務行程、MediaMTX ready／讀者數／流量、單輪長度與下次循環時間、concat 缺檔、health 與 alerts |
| 設定 | 直接編輯 `settings.json` 與 `modes.json`（存檔前驗 JSON，舊版留 `.bak`） |
| 動作 | 建置／只掃描／建置並切換、重建 concat、檢查缺檔（背景執行並回報進度）、**停止建置**（連子行程一起收，並清掉被中斷的輸出檔） |

只用標準庫，不必額外安裝。預設只綁 `127.0.0.1`，要對外開放**必須**帶 token 否則拒絕啟動；
不以 root 執行、不保管密碼，**stream key 只進不出**。細節見 [操作手冊](docs/manual.md)。

頁面標題的專案名連到 GitHub（另開分頁）；標題下方有一顆「▶ 看直播畫面」，直接開
MediaMTX 的 HLS 頁（就是播出端真正送出去的那一路）。`mediamtx.yml` 的 `hls` 是 `no`
（repo 的預設值）時不會顯示那顆按鈕 —— 沒開 HLS 就沒有那個頁面，按了只會連到空的。

### 設定放哪裡

| 檔案 | 管什麼 |
|---|---|
| `src/settings.json` | 畫質、fps、位元率、**淡入淡出秒數**、浮水印與跑馬燈、黑尾門檻 |
| `src/modes.json` | 每個播出模式：來源頻道、長度上限、shorts 池、重新掃描頻率 |
| `src/playlist.example.json` | 母清單範例；實際的 `playlist.json` 用 `build_playlist.py` 產生（已列入 `.gitignore`） |

程式讀這些當**預設值**，命令列參數永遠可以逐次覆寫。要改「播什麼」改 `modes.json`，
不要在 `settings.json` 裡塞來源資訊 —— 兩份真值會互相打架。

### 目錄

    install.sh                 安裝／升級：複製程式、代入 plist 佔位符、產生 mediamtx.yml、註冊服務
    mediamtx.example.yml       MediaMTX 範例設定（路徑白名單，只開 live/main）
    src/                       全部程式（.py／.sh）與預設設定
      webui.py                 本機控制台（狀態／設定／建置）
      settings.json            通用設定
      modes.json               播出模式定義
      playlist.example.json    母清單範例（實際的 playlist.json 由 build_playlist.py 產生）
    launchd/                   launchd 服務定義（__HOME__／__USER__ 為佔位符，由 install.sh 代入）
    docs/                      規格書與操作手冊
      spec-v1.2.md             系統規格書：架構、需求、實測數據、風險、待決事項（核心文件）
      manual.md                操作手冊：部署、日常操作、故障排除、已知限制
      changelog.md             變更與實測紀錄：逐日的量測、修正與被推翻的假設
      archive/                 過程中的評估與更正紀錄（包含被推翻的假設，實測數字都在裡面）

### 參考部署（實測環境）

| 項目 | 值 |
|---|---|
| 執行環境 | 一台 Apple silicon Mac（測試機為 M1 / 8 GB / macOS 27），用系統內建 Python 即可 |
| 服務 | com.ytpl.mediamtx / .playout / .publish / .health / .refresh（LaunchDaemon，開機自啟、不需登入） |
| 播出方式 | 單一行程 concat ＋ `-c copy`，換片縫 0 秒 |
| 開機自啟 | 已實測：重開機後全程無人登入仍自動恢復，中斷約 25 秒 |

### 機密不在這個 repo

stream key、Telegram bot token、YouTube OAuth token、cookies、WebUI token 一律**只留在部署機器上**，
各自是 `chmod 600` 的獨立檔案，不進版控。詳見 `.gitignore`。

### 已知限制

- 重開機需要機器本身不加密（測試機關閉了 FileVault）；若目標機開了 FileVault，重開機必須有人在機器前解鎖。
- 連續播出約 49.7 天會遇到 FLV 32 位元時間戳回繞，建議每月重啟一次播出端。
- 標題／說明／聊天室同步需要 YouTube Data API 授權，尚未接上。
- 長時間的規格與操作文件目前只有繁體中文；需要英文版請開 issue。

### 授權

MIT，見 [LICENSE](LICENSE)。

#### 第三方元件

本專案**不散布**下列執行檔，請自行安裝並遵守各自的授權：

| 元件 | 用途 | 授權 |
|---|---|---|
| FFmpeg | 轉檔、播出、推流 | LGPL／GPL（視建置選項；含 libx264 的建置為 GPL） |
| yt-dlp | 解析與下載來源影片 | Unlicense |
| MediaMTX | 本機媒體樞紐 | MIT |
| Pillow／qrcode | 浮水印與 QR Code 產生 | HPND／MIT |

### 使用聲明

1. 本工具僅供**你擁有著作權、或已取得權利人授權**的內容使用。
2. 下載與再公開傳輸的行為及其後果，由使用者自行承擔；本專案作者不對使用者的使用方式負責。
3. 下載行為仍受各平台服務條款約束，請自行確認你的情境合規。
4. 音樂、肖像、新聞畫面等第三方權利，須由內容提供者具結處理。

---

## English

### What this is

A self-hosted system that turns a YouTube playlist into a 24/7 live channel with no visible seams,
and can also relay an external YouTube live stream into your own channel.

    playlist.json (YouTube URLs)
         |  build_local_content.py   download, normalize, verify duration, detect black tails
         v
    media/<id>.mp4 + media/_tr_<id>.mp4   (per-episode transition; its QR points at that episode)
         |  make_concat_list.py
         v
    concat.txt
         |  playout.sh   single long-lived ffmpeg: concat + -stream_loop -1, pure stream copy
         v
    MediaMTX  rtmp://127.0.0.1:1935/live/main
         |  yt_publish.sh   single long-lived publisher with its own watchdog
         v
    YouTube live ingest

Design points, each measured on the target machine:

- **Single-process concat, not per-segment relay.** Relaying with one publisher per segment showed a
  2.0-3.1 second seam at every cut; concat shows 0.
- **The whole chain is a stream copy.** Two ffmpeg processes together use about 6% CPU on an 8 GB M1.
- **Four layers of protection:** launchd KeepAlive, a publisher watchdog (self-recovers after a 30s stall),
  `healthcheck.py` (restarts the chain after a traffic stall), and Telegram alerts.
- **Audio always fades out and back in at a cut.** The playout path is a pure copy and cannot insert
  filters at the seam, so the fade is baked into every file at build time.

### Quick start

    git clone <repo> && cd <repo>
    ./install.sh --dry-run     # see what it would do
    ./install.sh               # install to ~/ytpl and register launchd services (needs sudo)

The services deliberately do **not** start until there is content to play, so launchd does not
restart a process that cannot succeed. To build content:

    cd ~/ytpl
    python3 build_playlist.py --url '<playlist or channel URL>' -o playlist.json
    python3 build_local_content.py --playlist playlist.json --target 720
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/ytpl
    printf %s '<YouTube stream key>' > stream.key && chmod 600 stream.key

`install.sh --agents` installs LaunchAgents instead (no root, but requires a GUI login). See `--help`.

### Local console

Once installed, day-to-day operation needs no shell commands:

    python3 ~/ytpl/webui.py     # http://127.0.0.1:8787

| Tab | Contents |
|---|---|
| Status | processes, MediaMTX ready/readers/traffic, round length and next loop time, missing concat entries, health and alerts |
| Settings | edit `settings.json` and `modes.json` (JSON is validated; the previous version is kept as `.bak`) |
| Actions | build / scan only / build and switch, rebuild concat, check for missing files, **stop build** (kills the whole process tree and deletes the interrupted output files); runs in the background with progress |

Standard library only, so there is nothing to install. It binds to `127.0.0.1` by default; exposing it
requires a token or it refuses to start. It never runs as root and never stores passwords, and the
**stream key is write-only** (the page can set it but will never display it).

The project name in the page title links to GitHub (opens in a new tab), and the "▶ 看直播畫面"
button below it opens MediaMTX's HLS page — the stream that is actually being sent out. When
`hls` is `no` in `mediamtx.yml` (the repo default) that button is not shown, because there is no
such page to open.

### Where settings live

| File | Covers |
|---|---|
| `src/settings.json` | resolution, fps, bitrate, **audio fade seconds**, watermark and marquee, black-tail threshold |
| `src/modes.json` | per-mode sources: channel, duration cap, shorts pool, rescan interval |
| `src/playlist.example.json` | sample master list; generate the real `playlist.json` with `build_playlist.py` (gitignored) |

These files are read as **defaults**; command-line flags always override them for a single run.
Change what plays in `modes.json`, not in `settings.json` - two sources of truth will drift.

### Layout

    install.sh                 install/upgrade: copy programs, expand plist placeholders, create mediamtx.yml, register services
    mediamtx.example.yml       sample MediaMTX config (path allowlist: only live/main)
    src/                       all programs (.py/.sh) and default settings
    launchd/                   service definitions (__HOME__/__USER__ are placeholders expanded by install.sh)
    docs/                      spec and operations manual (Chinese)
      spec-v1.2.md             system spec: architecture, requirements, measurements, risks (core document)
      manual.md                operations manual: deploy, day-to-day, troubleshooting, limitations
      changelog.md             measurements, fixes and refuted hypotheses, in date order
      archive/                 earlier evaluations and corrections, including refuted hypotheses

### Reference deployment (measured environment)

| Item | Value |
|---|---|
| Host | one Apple silicon Mac (test machine: M1 / 8 GB / macOS 27); the system Python is enough |
| Services | com.ytpl.mediamtx / .playout / .publish / .health / .refresh (LaunchDaemons: start at boot, no login needed) |
| Playout | single-process concat with `-c copy`; 0 second seam at every cut |
| Boot recovery | measured: after a reboot with nobody logged in, the chain recovered automatically; 25 second interruption |

### Secrets are not in this repo

The stream key, Telegram bot token, YouTube OAuth token, cookies and the WebUI token stay **only on the
deployed machine** as separate `chmod 600` files. See `.gitignore`.

### Known limitations

- Booting unattended requires the host disk to be unencrypted (the test machine has FileVault off); with FileVault on, someone must unlock it at the machine.
- After roughly 49.7 days of continuous playout the 32-bit FLV timestamp wraps; restart the playout service monthly.
- Title/description/chat synchronisation needs YouTube Data API authorisation and is not wired up yet.
- The long-form spec and manual are Traditional Chinese only for now; open an issue if you need English.

### License

MIT, see [LICENSE](LICENSE).

#### Third-party components

This project does **not** redistribute any of the following; install them yourself and follow their licenses:

| Component | Role | License |
|---|---|---|
| FFmpeg | transcoding, playout, publishing | LGPL/GPL depending on build flags (builds with libx264 are GPL) |
| yt-dlp | resolving and downloading source videos | Unlicense |
| MediaMTX | local media hub | MIT |
| Pillow / qrcode | watermark and QR code generation | HPND / MIT |

### Legal notice

1. Use this tool only with content you own or are licensed to use.
2. You are responsible for how you download and re-transmit content; the authors accept no liability for your use.
3. Downloading is still subject to each platform terms of service - check that your use case complies.
4. Third-party rights (music, likeness, news footage) must be cleared by whoever supplies the content.
