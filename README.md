# loopcastr - turn a YouTube playlist into a 24/7 always-on live channel

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg?v=2">
    <img src="assets/logo-light.svg?v=2" alt="loopcastr" width="340">
  </picture>
</p>

[English](#english) | [中文](#中文)　　Bilingual document, **English first**

> ⚠️ **Check that you are allowed to use the source content before you start.** This system downloads
> the videos as local files and **re-transmits them publicly** after processing, so it only suits content
> you own or are licensed to use. See the legal notice at the end.

> ⚠️ **使用前請確認你有權使用來源內容。** 本系統會把影片抓成本機檔案、加工後**重新公開傳輸**，
> 只適用於你擁有著作權、或已取得權利人授權的內容。詳見文末〈使用聲明〉。

---

## English

### What this is

A self-hosted system that turns a YouTube playlist into a 24/7 live channel with no visible seams,
and can also relay an external YouTube live stream into your own channel.

    playlist.json (YouTube URLs)
         |  build_local_content.py   download, normalize, verify duration, detect black tails
         v
    media/<mode>/<id>.mp4 + media/<mode>/_tr_<id>.mp4   (per-episode transition; its QR points at that episode)
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

Requirements: macOS with Python 3, plus `ffmpeg`, `yt-dlp` and `mediamtx` on `PATH`
(`brew install ffmpeg yt-dlp mediamtx`) and the Python packages `qrcode` and `pillow`
(`python3 -m pip install --user qrcode pillow`). `install.sh` checks all of them and prints what is
missing; `opencv` is optional and only used by `build_transitions.py --verify`.
It also picks the interpreter that has `qrcode` (Homebrew python3 when it is installed) and writes that
exact path into the service plists, so what the checks verify is what the services run.

    git clone https://github.com/kingwap99/loopcastr && cd loopcastr
    ./install.sh --dry-run
    ./install.sh

The first command is a dry run: it prints what it would do and changes nothing. The second copies the
programs to `~/loopcastr` and generates the service plists.

(The clone can live anywhere: `install.sh` copies the programs into `~/loopcastr`, or wherever `--prefix`
points. Installing into the clone itself works too - `src/` is flattened into that directory, so the clone
root gains copies of the programs, plus the settings and service plists, all as untracked files. `src/`
itself is never modified, so a later `git pull` stays clean.)

That second run deliberately does **not** register the launchd services: with no content to play they
would only restart forever. Build the content, then run the installer again to register and start them:

    cd ~/loopcastr
    python3 build_playlist.py --url '<playlist or channel URL>' -o playlist.json
    python3 build_local_content.py --playlist playlist.json --target 720
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/loopcastr
    printf %s '<YouTube stream key>' > stream.key && chmod 600 stream.key
    cd <the clone> && ./install.sh

The last `./install.sh`, run from the clone, sees the content and registers and starts the services.
Add `--force-services` to the first run if you would rather register them before any content exists.

This command-line path is the minimum that plays: it plays the videos without transitions. Per-mode
sources, transitions, incremental builds and the on-screen QR/captions all come from `modes.json` and
the console's "Build and switch (go live)", which runs the whole chain for you.

`install.sh --agents` installs LaunchAgents instead (no root, but a graphical login is required);
`./install.sh --help` lists every option.

**To upgrade**: `git pull` and then run `./install.sh` again. The pull only updates `src/`; the programs
that actually run (and that the service plists point at) are the copies `install.sh` puts in the install
directory.

### Local console

Day-to-day operation needs no shell commands. The installer registers the console as a launchd service
(`com.loopcastr.webui`), so once the services are running it is already there:

    http://127.0.0.1:8787/

(Before the services exist, start it by hand with the interpreter the services use - on a Homebrew
machine that is `/opt/homebrew/bin/python3 ~/loopcastr/webui.py`. The console builds with **its own**
interpreter, so starting it as `/usr/bin/python3` (which has no `qrcode`) makes every build it triggers
write videos with no QR codes. When that is the case the console shows a red warning at the top and
refuses to start a build.)

The console binds to localhost only. To reach it from another machine, install with
`./install.sh --webui-host 0.0.0.0` (`webui.py` refuses to run exposed without a token, so the
installer generates `<prefix>/webui-token` when there is none; change the path with
`--webui-token-file`). Those two settings live in the service plist, so pass them on every upgrade -
`install.sh` warns when it is about to regenerate a console plist that had extra arguments.

It is one page of blocks, top to bottom in the order you use them:

| Block | Contents |
|---|---|
| 1 Sources | one card per mode: playlist URL, shorts URL, video count and length cap, rescan interval, plus that mode's status chip |
| 2 Go live | build / scan only / build and switch, rebuild concat, check for missing files, **stop build** (kills the whole process tree and deletes the interrupted output files); runs in the background with live progress |
| Playout status / services / content / logs | processes, MediaMTX ready/readers/traffic, round length and next loop time, missing concat entries, media size, the tail of health and alerts |
| Quality and layout | the form generated from `settings.json` (bitrate, preset, text and QR sizes, fade seconds, black-tail threshold, media folder) |
| Advanced settings | the raw JSON of `settings.json` and `modes.json` (validated before saving, previous version kept as `.bak`) |
| Services | each launchd service as running / loaded but idle / not loaded; a not-loaded **gui-domain** service can be started from here (`com.loopcastr.publish`, the one that pushes to YouTube, is often exactly this case). A system-domain install has to be started with `sudo launchctl` instead |

Standard library only, so there is nothing to install. It binds to `127.0.0.1` by default; exposing it
requires a token or it refuses to start. It never runs as root and never stores passwords, and the
**stream key is write-only** (the page can set it but will never display it).

### Go live without waiting for the whole build

Each mode can set `first_batch` (the console calls it "go live after building this many videos"):
the first N videos are built, deployed and put on air, then each further batch extends the list —
restarting the playout **at the next segment boundary**, so viewers never see it jump back to the
start. A 55-video `news` build that used to take hours is live after the first couple of videos.

Rebuilds only fill in what is missing: every video and transition records an
"encode-parameter fingerprint plus file size", and unchanged files are skipped
(`--force` redoes everything).

Each mode can also set `sort` — `source` (whatever order the source gave), `date-asc` or `date-desc`
(by first-air time). The video limit and the age filter are applied first, then that selection is sorted,
so "the last 24 hours, played oldest first" is `max_age_hours=24` with `sort=date-asc`.

Brand assets (logo / icon / colours / tagline) live in [`assets/`](assets/):
`icon.svg`, `icon-dark.svg`, `logo-dark.svg`, `logo-light.svg`. The console uses
`icon-dark` as its favicon.

The project name in the page title links to GitHub (opens in a new tab), and the "▶ 看直播畫面"
button below it opens MediaMTX's HLS page — the stream that is actually being sent out. When
`hls` is `no` in `mediamtx.yml` (the repo default) that button is not shown, because there is no
such page to open.

### Where settings live

| File | Covers |
|---|---|
| `src/settings.json` | **media folder location**, resolution, fps, bitrate, **audio fade seconds**, watermark and marquee, black-tail threshold |
| `src/modes.json` | per-mode sources: channel, duration cap, shorts pool, rescan interval |
| `src/playlist.example.json` | sample master list; generate the real `playlist.json` with `build_playlist.py` (gitignored) |

These files are read as **defaults**; command-line flags always override them for a single run.
Change what plays in `modes.json`, not in `settings.json` - two sources of truth will drift.

### Layout

    install.sh                 install/upgrade: copy programs, expand plist placeholders, create mediamtx.yml, register services
    mediamtx.example.yml       sample MediaMTX config (path allowlist: only live/main)
    src/                       all programs (.py/.sh) and default settings
      webui.py                 local console: status, settings and build actions
      settings.json            general settings (quality, layout, on-screen captions)
      modes.json               broadcast mode definitions (source, length cap, shorts pool)
      playlist.example.json    master list example (the real playlist.json comes from build_playlist.py)
    launchd/                   service definitions (__HOME__/__USER__ are placeholders expanded by install.sh)
    docs/                      specification and operations manual
      spec-v1.2.md             system spec: architecture, requirements, measurements, risks (core document)
      manual.md                operations manual: deploy, day-to-day, troubleshooting, limitations
      changelog.md             measurements, fixes and refuted hypotheses, in date order (Traditional Chinese)
      archive/                 earlier evaluations and corrections, including refuted hypotheses (Traditional Chinese)

### Reference deployment (measured environment)

| Item | Value |
|---|---|
| Host | one Apple silicon Mac (test machine: M1 / 8 GB / macOS 27); Python 3 from Homebrew plus the `qrcode` and `pillow` packages |
| Services | com.loopcastr.mediamtx / .playout / .publish / .health / .refresh / .webui (LaunchDaemons: start at boot, no login needed) |
| Playout | single-process concat with `-c copy`; 0 second seam at every cut |
| Boot recovery | measured: after a reboot with nobody logged in, the chain recovered automatically; 25 second interruption |

### Secrets are not in this repo

The stream key, Telegram bot token, YouTube OAuth token, cookies and the WebUI token stay **only on the
deployed machine** as separate `chmod 600` files. See `.gitignore`.

### Known limitations

- Booting unattended requires the host disk to be unencrypted (the test machine has FileVault off); with FileVault on, someone must unlock it at the machine.
- After roughly 49.7 days of continuous playout the 32-bit FLV timestamp wraps; restart the playout service monthly.
- Title/description/chat synchronisation needs YouTube Data API authorisation and is not wired up yet.
- The changelog and the archived evaluations under `docs/` are Traditional Chinese; the README, the spec and the manual are English.

### Support this project

Transition clips carry a small donation QR at the bottom right by default. It ships with the author's
QR, so a fresh install shows it with no setup at all, and it is an ordinary setting rather than
something baked in:

- `overlay.sponsor_qr_image` is the QR picture (a path or a URL), so **you can point it at your own QR
  image** - the one your payment provider gives you, or any square picture.
- `overlay.sponsor_url` is a payment link, used to generate a plain QR when no picture is given.
- Clear both to remove the QR, or untick `overlay.sponsor_show` to keep them but stop drawing it
  (the change takes effect once the transitions are rebuilt).

Episodes never carry it, only the transitions between them, and the console previews exactly what is
configured and says whether it is currently drawn.

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

---

## 中文
### 這套系統在做什麼

    playlist.json（YouTube 網址）
         │  build_local_content.py —— 落地、正規化、驗證長度、偵測片尾黑畫面
         ▼
    media/<模式>/<id>.mp4 ＋ media/<模式>/_tr_<id>.mp4（每集專屬過場，QR 指向該集）
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

需求：macOS ＋ Python 3，`PATH` 上要有 `ffmpeg`、`yt-dlp`、`mediamtx`
（`brew install ffmpeg yt-dlp mediamtx`），以及 Python 套件 `qrcode`、`pillow`
（`python3 -m pip install --user qrcode pillow`）。`install.sh` 會逐項檢查並告訴你缺什麼；
`opencv` 是選配，只有 `build_transitions.py --verify` 會用到。
它也會挑一個能 `import qrcode` 的直譯器（有裝 Homebrew 就用它的 python3），並把該路徑寫進服務
plist，所以「檢查的那顆」和「服務實際跑的那顆」是同一顆。

    git clone https://github.com/kingwap99/loopcastr && cd loopcastr
    ./install.sh --dry-run
    ./install.sh

第一行是空跑：只印出它會做什麼、不會動任何東西。第二行才會把程式複製到 `~/loopcastr`、產生
服務 plist。

（clone 放哪裡都可以：`install.sh` 會把程式複製到 `~/loopcastr`，或用 `--prefix` 指定的位置。
直接裝在 clone 目錄裡也可以——`src/` 會被攤平到該目錄，所以 clone 根目錄會多出程式的複本、
設定檔與服務 plist，全部都是未進版控的檔案；`src/` 本身不會被改動，之後 `git pull` 不會有
本地修改的衝突。）

第二行**刻意不註冊** launchd 服務：還沒有內容可播時，它們只會一直重啟。先建內容，再跑一次安裝
讓服務註冊並啟動：

    cd ~/loopcastr
    python3 build_playlist.py --url '<播放清單或頻道網址>' -o playlist.json
    python3 build_local_content.py --playlist playlist.json --target 720
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/loopcastr
    printf %s '<YouTube 串流金鑰>' > stream.key && chmod 600 stream.key
    cd <剛才 clone 的目錄> && ./install.sh

最後那行（在 clone 目錄執行）看到內容存在，就會註冊並啟動服務。想在還沒有內容時就先註冊，
第一次執行加 `--force-services` 即可。

上面這條命令列路徑是**最小可播**版本（沒有過場）；每個模式的來源、過場、分批建置、畫面上的
QR 與字樣，都是 `modes.json` ＋ 控制台「建置並切換（開始直播）」在處理的。

`install.sh --agents` 可裝成 LaunchAgent（不需要 root，但要有圖形登入才會跑）；`./install.sh --help` 看全部選項。

**升級**：`git pull` 之後要**再跑一次 `./install.sh`**。pull 只更新 `src/`；實際在跑、服務 plist
指向的是安裝目錄裡那份被攤平的複本，那正是 `install.sh` 負責更新的。

### 本機控制台

日常操作不必再背指令。安裝程式會把控制台也註冊成 launchd 服務（`com.loopcastr.webui`），所以
服務跑起來之後它就在那裡：

    http://127.0.0.1:8787/

（服務還沒建立時，自己用「服務用的那一顆」啟動——Homebrew 機器上是
`/opt/homebrew/bin/python3 ~/loopcastr/webui.py`。控制台建置用的是**它自己的**直譯器，所以用
`/usr/bin/python3`（沒有 `qrcode`）啟動的話，它觸發的每一次建置都會產出沒有 QR code 的影片。
發生這種情況時，控制台最上面會出現紅色警告，並且拒絕開始建置。）

控制台預設只綁 localhost。要從別台機器連，安裝時加 `./install.sh --webui-host 0.0.0.0`
（`webui.py` 對外開放時沒有 token 會拒絕啟動，所以安裝程式會在沒有 token 檔時產生
`<prefix>/webui-token`；路徑可用 `--webui-token-file` 改）。這兩個設定是寫在服務 plist 裡的，
所以每次升級都要帶著；`install.sh` 要覆蓋一個帶有額外參數的控制台 plist 之前會先警告。

它是一頁由上而下的區塊，順序就是你操作的順序：

| 區塊 | 內容 |
|---|---|
| ① 來源設定 | 每個模式一張卡片：播放清單網址、shorts 網址、影片支數與長度上限、掃描間隔，以及該模式的狀態標籤 |
| ② 開始直播 | 建置／只掃描／建置並切換、重建 concat、檢查缺檔、**停止建置**（連子行程一起收並清掉半成品）；背景執行並即時顯示進度 |
| 播出狀態／服務行程／內容／日誌 | 服務行程、MediaMTX ready／讀者數／流量、單輪長度與下次循環時間、concat 缺檔、media 大小、health 與 alerts 尾端 |
| 媒體與畫質 | 由 `settings.json` 產生的表單（位元率、preset、文字與 QR 尺寸、淡化秒數、黑尾門檻、媒體資料夾） |
| 進階設定 | `settings.json` 與 `modes.json` 原始 JSON（存檔前驗 JSON，舊版留成 `.bak`） |
| 服務 | 每個 launchd 服務是「執行中／已載入沒在跑／沒有載入」；**gui domain** 且沒載入的可以從這裡按「啟動」（推流用的 `com.loopcastr.publish` 常常就是這一種）。system domain 的安裝要用 `sudo launchctl` 自己啟動 |

只用標準庫，不必額外安裝。預設只綁 `127.0.0.1`，要對外開放**必須**帶 token 否則拒絕啟動；
不以 root 執行、不保管密碼，**stream key 只進不出**。細節見 [操作手冊](docs/manual.md)。

### 不用等整批轉完才開播

每個模式可以設 `first_batch`（控制台是「先做幾支就開播」）：先做前 N 支就上線，
之後每批擴充一次，每次都在**下一個換片點**重啟播出端，所以觀眾不會看到內容跳回開頭。
`news`（55 支全長）原本要等好幾小時，設 `first_batch=2` 之後第一批做完就能播。

重新建置時也只補缺的：每支影片與過場都記了「編碼參數指紋 ＋ 檔案大小」，
沒變就跳過（`--force` 才全部重做）。實測 `.22` 上重跑一次 `test` 模式：
`3 skipped by fingerprint`、`0 transitions, 6 skipped`，整輪 15 秒、0 支重編。

每個模式也可以設 `sort` —— `source`（來源給的順序）、`date-asc`／`date-desc`（依首播日期）。
順序是「先取影片數上限、再過濾首播時間，之後才排序」，所以「最近 24 小時、由舊到新播」就是
`max_age_hours=24` ＋ `sort=date-asc`。控制台在「① 來源設定 → 更多設定」有對應的下拉選單。

品牌資產（Logo／Icon／配色／標語）在 [`assets/`](assets/)：`icon.svg`、`icon-dark.svg`、
`logo-dark.svg`、`logo-light.svg`。控制台用 `icon-dark` 當 favicon。

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
| 執行環境 | 一台 Apple silicon Mac（測試機為 M1 / 8 GB / macOS 27）；Python 3（Homebrew 的即可）＋ `qrcode`、`pillow` 兩個套件 |
| 服務 | com.loopcastr.mediamtx / .playout / .publish / .health / .refresh（LaunchDaemon，開機自啟、不需登入） |
| 播出方式 | 單一行程 concat ＋ `-c copy`，換片縫 0 秒 |
| 開機自啟 | 已實測：重開機後全程無人登入仍自動恢復，中斷約 25 秒 |

### 機密不在這個 repo

stream key、Telegram bot token、YouTube OAuth token、cookies、WebUI token 一律**只留在部署機器上**，
各自是 `chmod 600` 的獨立檔案，不進版控。詳見 `.gitignore`。

### 已知限制

- 重開機需要機器本身不加密（測試機關閉了 FileVault）；若目標機開了 FileVault，重開機必須有人在機器前解鎖。
- 連續播出約 49.7 天會遇到 FLV 32 位元時間戳回繞，建議每月重啟一次播出端。
- 標題／說明／聊天室同步需要 YouTube Data API 授權，尚未接上。
- `docs/` 底下的 changelog 與 archive 是繁體中文；README、規格書與操作手冊是英文。

### 贊助這個專案

過場影片的右下角預設會有一顆小額贊助 QR。`src/settings.json` 的 `overlay.sponsor_url` 出廠就帶著
作者的贊助連結，所以全新安裝不必任何設定就會顯示。它是普通設定、不是寫死的：把 `overlay.sponsor_url`
清空就完全移除 QR，或取消勾選 `overlay.sponsor_show` 保留連結但不畫 QR（重建過場後生效）。
集數不會有這顆 QR，只有集與集之間的過場才有；後台會直接預覽畫面上實際出現的樣子。

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
