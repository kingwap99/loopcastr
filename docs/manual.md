
# loopcastr 操作手冊（manual）

> 本文件為繁體中文。英文說明見 repo 根目錄 [README.md](../README.md) 的 English 一節。
> This document is in Traditional Chinese; see the English section of [README.md](../README.md).

從架構、部署，到日常操作、故障排除與已知限制。設計取捨與實測數字記在 [變更與實測紀錄](changelog.md)。


> 這份是**現在的狀態與怎麼操作**。逐日的實測、改動與被推翻的假設記在 [變更與實測紀錄](changelog.md)。
> This is the operations manual. Day-by-day measurements and corrections live in [changelog](changelog.md).

## 一句話架構

    media/<模式>/*.mp4 ──concat──> playout.sh（單一 ffmpeg）──> MediaMTX ──yt_publish.sh（單一長命 ffmpeg）──> YouTube ingest

本機檔案走 concat；只有「需要換來源」的 YouTube 直播聯播才回到 `relay.py` 的 takeover 接力。

## 檔案

安裝與設定：

| 路徑 | 角色 |
|---|---|
| `install.sh` | **安裝／升級**：複製程式、代入 plist 佔位符、產生 `mediamtx.yml`、註冊 launchd 服務 |
| `src/settings.json` | 通用設定：畫質、fps、位元率、淡化秒數、浮水印與跑馬燈、黑尾門檻。程式讀它當**預設值**，命令列可覆寫 |
| `src/modes.json` | 播出模式定義：各模式的來源頻道、長度上限、shorts 池、重新掃描頻率 |
| `src/settings.json` 的 `ui.lang` | 語言：`zh`／`en`（後台右上角可切換），同時影響後台介面與畫面字樣 |
| `src/playlist.example.json` | 母清單**範例**（3 筆假 id）。實際的 `playlist.json` 由 `build_playlist.py` 產生，已列入 `.gitignore` |
| `mediamtx.example.yml` | MediaMTX 範例設定。刻意用**路徑白名單**（只開 `live/main`），不是 MediaMTX 預設的全開 |

建置內容：

| 路徑 | 角色 |
|---|---|
| `src/build_playlist.py` | 掃描播放清單或頻道，產生母清單 `playlist.json`（含每支長度） |
| `src/build_local_content.py` | 把母清單抓成本機 `media/<模式>/<id>.mp4`：正規化、長度核對、黑尾偵測、浮水印與倒數、淡入淡出 |
| `src/build_transitions.py` | 產生每集專屬過場 `media/<模式>/_tr_<id>.mp4`（QR 指向該集），可吃 shorts 池輪播 |
| `src/build_test_edition.py` | 產生縮短的測試版：每集剪成固定秒數，右上角燒流水號 |
| `src/mode_build.py` | 依 `modes.json` 跑完整條鏈：掃描 → 落地 → 過場 → 部署 → 重建清單 |
| `src/wmtext.py`、`src/make_qr_png.py` | 浮水印文字與 QR Code 的 PNG 產生（機器上沒有 freetype，所以自己畫） |

內容放在哪裡（`media/` 底下）：

| 路徑 | 是什麼 | 共用嗎 |
|---|---|---|
| `media/<模式>/<id>.mp4` | 正規化後的影片 | 每個模式一份（同一支影片在不同模式可有不同長度上限） |
| `media/<模式>/_tr_<id>.mp4` | 第 1 趟的過場；`_tr_<id>_p2.mp4` 是第 2 趟 | 同上 |
| `media/<模式>/manifest.json` | 該模式的長度／黑尾紀錄 | 同上 |
| `media/.raw/<id>.mp4` | 原始下載檔（`--keep-raw`） | **共用**：是輸入，跟模式無關 |
| `media/short-<id>.mp4` | shorts 池 | **共用**：同上 |

播出：

| 路徑 | 角色 |
|---|---|
| `src/make_concat_list.py` | 把 `playlist-local.json` 轉成 ffmpeg concat 清單 `concat.txt` |
| `src/playout.sh` | **播出端**：單一行程 concat 循環播出，推 MediaMTX。由 `com.loopcastr.playout` 看管 |
| `src/yt_publish.sh` | **推流端**：單一長命 ffmpeg 從 MediaMTX 推到 YouTube ingest。由 `com.loopcastr.publish` 看管 |
| `src/switch_edition.sh` | 切換播出哪一版清單（正式／各模式／測試），並重建清單、改 plist、重啟服務 |
| `src/relay.py` | 聯播／多來源接力引擎（takeover 零斷點換手、看門狗、來源 URL 生命週期） |

觀測與維運：

| 路徑 | 角色 |
|---|---|
| `src/webui.py` | **本機控制台**（只用標準庫）：狀態、設定編輯、建置動作 |
| `src/healthcheck.py` | 健康檢查（每 60 秒）：ready、有讀者、流量有成長；狀態變化才告警，可自動修復 |
| `src/loopwatch.py` | 循環邊界觀測：量繞回清單開頭那一刻有沒有縫 |
| `src/gapwatch.py` | 獨立驗收觀測器：輪詢 MediaMTX API，報「接收端離線」與 `bytesReceived` 零成長區間 |
| `src/refreshwatch.py` | 依 `modes.json` 的 `refresh_seconds` 定期重掃，有新片就在下一個換片點切回清單開頭 |
| `src/yt_side_monitor.py` | YouTube 端長時間監控：拉直播串流跑 blackdetect／freezedetect |

服務定義（模板，`__HOME__`／`__USER__`／`__YT_VIDEO_ID__` 由 `install.sh` 代入）：

| 路徑 | 角色 |
|---|---|
| `launchd/com.loopcastr.mediamtx.plist` | 媒體樞紐（含 8192 fd 的 ResourceLimits） |
| `launchd/com.loopcastr.playout.plist` | 播出端 |
| `launchd/com.loopcastr.publish.plist` | 推流端 |
| `launchd/com.loopcastr.health.plist` | 健康監控（以 root 執行，才能 kickstart system domain） |
| `launchd/com.loopcastr.refresh.plist` | 自動重新掃描 |

## 為什麼播出端用 concat 而不是接力

同樣一段「3 段本機檔案繞圈」：

| 播法 | 換片縫 | 繞回清單開頭的縫 | 冷啟動縫 |
|---|---|---|---|
| `relay.py` 接力（每段一個 publisher） | 每段 2.0–3.1 秒 | 有 | 3.07 秒 |
| `playout.sh` concat（全程一個 publisher） | **0** | **0** | 2.55 秒（只此一次） |

接力會斷的原因很具體：**檔案播完就 EOF，MediaMTX 立刻踢掉 publisher，下一個 publisher 暖機期間接收端是離線的**。
`--url-max-age` 或加大 `tail` 都救不了，因為那是**來源端**先結束，不是輸出端。

實測證據（目標機，2026-09-16 05:13，55 秒涵蓋兩輪清單）：

    接收端離線時段：0 段（整場連續）
    bytesReceived 零成長區間 0 段，最長 0.000s

日誌同時出現 DTS `48000`、`57000`，證明確實繞回第二輪，且繞回點沒有離線。

**唯一副作用**：concat demuxer 在每個接縫會出現 `Non-monotonic DTS` 警告（音訊封包邊界四捨五入，實測重疊約 11 ms）。ffmpeg 會自動夾正，聽感無影響；若要求時間軸完全乾淨，可先跑一次離線預接（見下方「預接成單一大檔」）。

    # 預接成單一大檔（離線做一次，之後播出完全不碰 concat demuxer）
    python3 src/make_concat_list.py playlist-local.json -o concat.txt --base-dir .
    ffmpeg -hide_banner -f concat -safe 0 -i concat.txt -c copy media/all-in-one.mp4

## 部署

用 `install.sh`，它會把程式複製到安裝目錄、把 plist 的 `__HOME__`／`__USER__` 佔位符代入、
產生 `mediamtx.yml`，並註冊 launchd 服務：

    ./install.sh --dry-run     # 先看它會做什麼（不會動任何東西）
    ./install.sh               # 預設裝到 ~/loopcastr，用 LaunchDaemon（需要 sudo）
    ./install.sh --agents      # 裝成 LaunchAgent：不需 root，但要有圖形登入

可以重複執行；已存在的 `mediamtx.yml` 與 `stream.key` 不會被覆蓋。
服務在還沒有播出內容（`playlist-local.json`／`concat.txt`）時不會啟動，
避免 launchd 一直重啟一個註定失敗的行程。

驗收（播出中，另開一個終端）：

    python3 ~/loopcastr/gapwatch.py http://127.0.0.1:9997 live/main 120

## 上線前務必確認

- `playlist-local.json` **必須存在且每段檔案都在磁碟上**，否則 `playout.sh` 會在建立 concat 清單時直接中止（設計如此，避免播出半份清單）。
- `stream.key` 權限 `600`，不得進版控。
- 本機與目標機的 `TZ` 都是 `Asia/Taipei`，日誌時間戳可直接對照。

## 操作手冊

### 日常看一眼

    ssh <USER>@<TARGET_HOST>
    launchctl list | grep loopcastr          # 該有的服務都在嗎（沒有的話看控制台的「服務」區塊）
    tail -3 ~/loopcastr/logs/health.log      # 全鏈路正常嗎
    tail -3 ~/loopcastr/logs/alerts.jsonl    # 有沒有告警過

`health.log` 每 60 秒一行。看到 `OK 全鏈路正常（流量 +N bytes / 6s）` 就是正常，N 大約 2,000,000。

### 換直播金鑰

`yt_publish.sh` 只在**啟動時**讀一次金鑰，改了檔案一定要重啟：

    printf %s 新金鑰 > ~/loopcastr/stream.key && chmod 600 ~/loopcastr/stream.key
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.publish

### 加新集數

平常走控制台就好：填好該模式的來源網址 → 按「建置並切換（開始直播）」。
它會依 `modes.json` 跑完整條鏈（掃描 → 落地 → 過場 → 部署 → 重建清單 → 切換）。

要在命令列做同一件事：

    cd ~/loopcastr
    python3 mode_build.py --mode news --switch     # 掃描＋落地＋過場＋切換
    python3 mode_build.py --mode news --scan-only  # 只重新掃描母清單

只有「手動塞幾支自己準備的片段」才需要碰母清單 `playlist-<模式>.json` 的 `segments`
（`type: vod`、`url`、`seconds`），再自己落地與重建清單：

    python3 build_local_content.py --playlist playlist-news.json --target 720 --keep-raw \
      --media-dir media/news --out-playlist playlist-news-local.json
    python3 make_concat_list.py playlist-news-local.json -o concat-news.txt --base-dir ~/loopcastr
    ./switch_edition.sh news

`build_local_content.py --status` 隨時可以看還缺哪幾支。

### 只想重掃黑尾（不重新下載）

    python3 build_local_content.py --rescan

### 服務開關

先確認是哪一種安裝，指令的 domain 與路徑都不一樣：

> **改名前安裝的舊機器**：這套系統 2026-09-21 由 `ytpl2ytstream` 改名為 `loopcastr`。
> 在那之前裝的機器目錄是 `~/ytpl`、服務是 `com.ytpl.*`（指令裡的路徑與 label 都要照舊）。
> 程式本身兩邊都認（label 前綴是掃目錄裡實際的 plist 決定的），所以更新程式不會壞；
> 要換成新名字得重新安裝一次（或手動搬目錄與改 label）。

| 安裝方式 | domain | plist 位置 | 要不要 sudo |
|---|---|---|---|
| `install.sh --agents` | `gui/$(id -u)` | `~/Library/LaunchAgents/` | 不用 |
| `install.sh` | `system` | `/Library/LaunchDaemons/` | 要 |

以 LaunchAgent（gui）為例：

    # 停
    launchctl bootout gui/$(id -u)/com.loopcastr.publish
    launchctl bootout gui/$(id -u)/com.loopcastr.playout
    # 起
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loopcastr.publish.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loopcastr.playout.plist
    # 重啟（不卸載）
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.playout

LaunchDaemon 版本就是把 `gui/$(id -u)` 換成 `system`、路徑換成 `/Library/LaunchDaemons/`，前面加 `sudo`。
控制台的服務區塊只處理 gui domain（它不以 root 執行），system domain 要自己來。

播出端重啟會從第一段重新開始，並有約 2.5 秒的冷啟動縫。

### 本機控制台（webui.py）

不用 ssh、不用背指令的介面。只用標準庫，不必額外安裝任何東西。

    cd ~/loopcastr
    python3 webui.py                 # http://127.0.0.1:8787
    python3 webui.py --port 9000

    # 要讓它常駐
    nohup python3 ~/loopcastr/webui.py > ~/loopcastr/logs/webui.log 2>&1 &

區塊（由上而下就是操作順序）：

| 區塊 | 內容 |
|---|---|
| ① 來源設定 | 每個模式一張卡片：播放清單網址、shorts 網址、影片支數與長度上限、掃描間隔。卡片右上角有該模式的狀態標籤 |
| ② 開始直播 | 建置／只掃描／建置並切換、重建 concat、檢查缺檔、**停止建置**。背景執行並回報進度（建置可能數十分鐘） |
| 播出狀態／服務行程／內容／日誌 | 服務行程、MediaMTX ready／讀者數／流量、單輪長度與下次循環時間、concat 缺檔、media 大小、health 與 alerts 尾端 |
| 畫質與版面 | 由 schema 產生的表單（位元率、preset、文字與 QR 尺寸、淡化秒數、黑尾門檻…），存檔後下次建置生效 |
| 進階設定 | `settings.json` 與 `modes.json` 原始 JSON。存檔前驗 JSON，舊版留成 `.bak` |
| 服務 | 每個 launchd 服務是「執行中／已載入沒在跑／沒有載入」，沒載入的可以按「啟動」（見下方「服務」一節） |

#### 停止建置

建置可能跑好幾十分鐘，中途想改參數就按「停止建置」。

- 停的是**整棵行程樹**：`mode_build.py` → `build_local_content.py` → `ffmpeg`。
  只殺最上層的話，底下兩個會變孤兒繼續寫同一個檔案。
- 先送 SIGTERM，3 秒內沒收工就補 SIGKILL（實測 ffmpeg 在疊圖的指令下不吃 SIGTERM）。
- **被中斷的輸出檔一律刪掉。** ffmpeg 收到 SIGTERM 有時會正常收尾，留下一個讀得出來、
  但只有幾十秒的檔案；留著會被下一輪當成完成品播出去。已經轉好的檔案不受影響，
  下次建置從缺的補（原始檔在 `media/.raw/`，不會重新下載）。
- 收尾完成前按「開始」會被擋下（上面那排按鈕也會變灰），等狀態回到「已完成／待機」再按。

#### 標題與「看直播畫面」

頁面標題的專案名（`loopcastr`）連到 GitHub 專案頁，另開分頁。
標題下面那顆「▶ 看直播畫面」開的是 MediaMTX 的 HLS 頁：

    http://<主機>:<hlsAddress 的埠>/<路徑>/      # 本機預設 http://127.0.0.1:8888/live/main/

主機名稱由瀏覽器自己填，所以從別台機器開控制台也通。
位址與是否顯示都讀 `mediamtx.yml`：`hls: no`（repo 預設）時按鈕不會出現，
會改成一行灰字說明為什麼沒有。要開就改 `hls: yes` 並重啟 mediamtx。

#### 安全設計

- **預設只綁 `127.0.0.1`。** 要對外開放必須提供 token，否則拒絕啟動：

      openssl rand -hex 16 > ~/loopcastr/webui-token && chmod 600 ~/loopcastr/webui-token
      python3 webui.py --host 0.0.0.0

  之後用 `?token=<值>` 或 `X-Ytpl-Token` 標頭存取。
- **所有寫入都要求自訂標頭 `X-Ytpl: 1`**：跨站表單帶不了這個標頭，等於擋掉 CSRF。
- **不以 root 執行，也不保管密碼。** 需要重啟 system domain 服務時只試 `sudo -n`（非互動），失敗就顯示要加的 sudoers 白名單，不會把密碼餵進程式：

      # /etc/sudoers.d/loopcastr-webui
      <你的帳號> ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.loopcastr.playout
- **stream key 只進不出**：可以寫入，但頁面永遠不會把它顯示出來。
- 動作只呼叫既有 script（argv 清單、不經 shell）；模式名稱必須存在於 `modes.json`，不接受任意路徑。

#### settings.json 是什麼

它是「給人改的」那一份，也是這支控制台在編輯的東西。程式讀它當**預設值**，
命令列參數永遠可以逐次覆寫；檔案不存在時，行為與沒有這個機制時完全相同。

| 區塊 | 內容 |
|---|---|
| `media` | `target`（720／1080／480）、`fps`、`venc`、`abr`、`audio_fade`（換片淡入淡出秒數）、`max_seconds` |
| `overlay` | `date_label`、位置（`overlay_y`／`overlay_margin`／`band_left`）、跑馬燈（`marquee_speed`／`marquee_gap`）、按鈕（`link_button`／`link_caption`）、`countdown`、`transition_caption`。**每個字樣都有一個 `_en` 對應值**（例如 `date_label_en`），`ui.lang=en` 時用那一組 |
| `ui` | `lang`：`zh`（中文）／`en`（英文）。後台介面與畫面字樣都看這個，**一次只顯示一種**；控制台右上角的「中文／English」就是改它 |
| `content` | `black_tail_min`（片尾黑畫面幾秒算黑尾） |

要改「播什麼」請改 `modes.json`，不要在 `settings.json` 裡塞來源資訊 —— 兩份真值會互相打架。

### 故障排除

| 症狀 | 先看 | 常見原因 |
|---|---|---|
| YouTube 沒畫面，但控制台的「看直播畫面」有內容 | `launchctl list` 裡有沒有 `com.loopcastr.publish` | **推流服務沒被載入**（金鑰貼好了也沒用，因為沒人去用它）。控制台服務區塊按「啟動」 |
| 觀眾端黑畫面 | `python3 build_local_content.py --rescan` | 某支影片本身有長黑尾 |
| 黑畫面但 health 正常 | 對本地 HLS 跑 blackdetect | 內容層問題，傳輸與時間軸都看不出來 |
| YouTube 沒畫面但本地正常，且 publish 有在跑 | `tail ~/loopcastr/logs/publish.log` | 金鑰失效、直播活動結束、或 ingest 被拒 |
| health 一直 FAIL | `cat ~/loopcastr/logs/health-state.json` | 看 `problems` 欄位；連續 3 次會自動重啟對應服務 |

### 已知限制

- **重開機需要人工解鎖**：目標機開了 FileVault 且沒有自動登入（見規格書 D16）。
- **連續播出約 49.7 天**會遇到 FLV 32 位元時間戳回繞，建議每月重啟一次播出端。
- **換片點有約 11 ms 的音訊時間戳重疊**（`Non-monotonic DTS` 警告），ffmpeg 會自動夾正，聽感無影響。要完全消除需先離線預接成單一大檔。

### 服務：system domain（`install.sh` 的 LaunchDaemon 安裝）

這種安裝的服務在 /Library/LaunchDaemons/，操作要加 sudo，domain 是 system 不是 gui：

    sudo launchctl list | grep loopcastr
    sudo launchctl kickstart -k system/com.loopcastr.playout
    sudo launchctl bootout system/com.loopcastr.playout
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.playout.plist

playout、publish、mediamtx 以 <USER> 身分執行；health 以 root 執行，因為只有 root 能對 system domain 做 kickstart（那是自動修復的必要條件）。

改成 LaunchDaemon 安裝時，舊的 LaunchAgent 版本建議先移走（例如 `~/loopcastr/launchagents-backup/`），否則兩邊會同時被載入、搶同一條串流。

### 循環邊界觀測（loopwatch.py）

播出端是單一行程 concat 加 -stream_loop -1，時間軸會「連續累加」—— 繞回時 DTS 不會掉回 0，所以不能用 DTS 當訊號。這支改用「開播時間 ＋ 單輪長度」推算循環點，在事件前後高頻取樣 MediaMTX API：

    python3 loopwatch.py --lead 45 --tail 90
    python3 loopwatch.py --at 04:19:07      # 也可直接指定時刻

輸出會列出跨循環點的接收端離線區段與 bytesReceived 零成長區段。單輪長度取自 playlist-local.json（含 outpoint 修正），所以換清單或改長度上限後不用改參數。

### 過場影片（每集一份）

每一集之後（含最後一集之後）插入一段過場。**過場是一集一份**：`media/<模式>/_tr_<影片id>.mp4`，
內容是 short 輪播（或乾淨底）＋ 標題列 ＋ 一顆指向**該集網址**的 QR，讓觀眾掃碼去看剛播完的那一集。

命名用**影片 ID** 而不是序號：序號在換清單時會互相覆蓋，切回舊清單還會拿到錯的 QR。
一輪跑多趟（`--passes`）時第 2 趟之後加 `_pN`，因為同一支影片在不同趟要配不同的 short。

`build_local_content.py` 依序在每一集後面插入對應的 `_tr_<id>.mp4`；萬一某集沒有專屬檔，
會退回共用的 `media/<模式>/_transition.mp4`（舊機制，仍然可用）。

    python3 build_transitions.py --parallel 3 --verify 5     # 全部重做，做完抽驗

    # 舊機制：一體適用的共用過場（只有在沒有每集 QR 時才需要）
    python3 build_local_content.py --transition <YouTube URL 或 video id>
    python3 build_local_content.py --no-transition           # 暫時停用（檔案留著，不插入）

過場會跟其他片段一樣被正規化成 1280x720 / H.264 High L3.1 / 30fps / AAC-LC 48k 立體，
所以 concat 一樣是純 `-c copy`、不需要轉碼。

### 正式版／測試版切換

測試用的清單是暫時的，測完一定要切回來，否則頻道會一直播測試片段。

    ./switch_edition.sh            # 看目前是哪一版
    ./switch_edition.sh live       # 切回正式版（playlist-local.json）
    SUDO_PASS=xxx ./switch_edition.sh test    # 切到測試版

切換會重建 concat 清單、改寫播出端 plist、重啟服務，並自動重掛 loopwatch（單輪長度變了，循環點要重算）。實測切換期間推流只斷 4 秒，YouTube 端維持 `is_live`。

### YouTube 端長時間監控（T-12）

所有「零縫」量測都是 MediaMTX 端。這支用來盯 YouTube 端那層轉碼與分發：

    python3 yt_side_monitor.py --id <video id> --hours 3

它會持續拉 YouTube 的直播串流跑 blackdetect 與 freezedetect，每筆事件都補上實際時間，方便跟本地事件對照。直播位址過期時會自動重新解析續讀。

### 畫面上的 QR Code 有哪幾顆

| 位置 | 內容 | 出現時機 |
|---|---|---|
| 右上角 | 該集原片 `https://youtu.be/<id>`，說明文字「▶ 看原片」（集數）／「去追劇」（過場） | 集數與過場都有，位置與格式**完全一致** |
| 右下角 | 贊助連結（`settings.json` 的 `overlay.sponsor_url`） | 有填才出現；`overlay.sponsor_code` 填指定值可關掉 |

QR 的 PNG 由 `make_qr_png.py` 產生（機器上沒有 qrencode，所以只裝純 Python 的 `qrcode`
拿矩陣，PNG 自己用 zlib + struct 寫）：

    python3 make_qr_png.py "<網址>" out.png --scale 8 --border 4

過場的 QR 是 `build_transitions.py` 每次重建時重新疊上去的，要改內容就改參數重跑，
不要去改已經疊過的檔案（會愈疊愈花）：

    python3 build_transitions.py --button-caption "去追劇" --parallel 3

驗證一定要做 —— 從**編碼後的影片**抽格解碼，不是只看畫面有沒有東西：

   python3 -c "import cv2,subprocess;subprocess.run(['ffmpeg','-y','-ss','10','-i','media/news/_tr_<id>.mp4','-frames:v','1','/tmp/f.png']);print(cv2.QRCodeDetector().detectAndDecode(cv2.imread('/tmp/f.png'))[0])"

### 用 shorts 輪播當過場

過場不一定要用固定一支影片，也可以吃一個 shorts 池輪流播：

    python3 build_transitions.py --playlist playlist-tucheng3.json \
      --shorts-url "https://www.youtube.com/<SHORTS_CHANNEL>/shorts" \
      --shorts-count 3 --seconds 90 --parallel 3 --verify 3

它會抓前 N 支 shorts、正規化成與其他片段一致的參數（1280x720 / H.264 High L3.1 / 30fps / AAC-LC），再依序把每集的 QR 疊上去。
配法是「第 p 趟的第 i 集用池子裡第 `(p × 集數 + i - 1) % N` 支」——
所以 30 支影片配 50 支 shorts、跑 2 趟時，第 2 趟會接著從第 31 支 short 播下去，而不是重頭輪。

**直式短片會被縮小補黑邊**（pillarbox），不裁切也不變形。實測 1080x1920 的 short 縮成 404x720、左右各留約 438px 黑邊。

`--seconds` 是每段過場的長度上限；短片的實際長度若更短就照原長。

過場上也會有**標題跑馬燈**（與集數相同的樣式），但左界給 0 —— 直式短片兩側本來就是黑邊，不需要像集數那樣讓開 logo 的空間。

過場的 QR 與集數用**同一個元件**、**同樣的位置**（右上角）與排版，不做任何區分；
差別只在說明文字：集數是「看原片」，過場是「**去追劇**」
（`--button-caption` 可改，`--no-button` 可整個關掉）。

### 剩餘播放時間倒數

集數的 QR 按鈕下方有一個每秒更新的「剩餘 MM:SS」標籤。

影片畫面不能直接畫字（沒有 freetype），倒數又必須隨時間變化，所以做法是**事先把每一秒的圖都畫好**（`make_countdown_frames`），再交給 ffmpeg 用 `-framerate 1` 的序列輸入；overlay 會依時間自己換圖。180 秒的影片就是 180 張圖。

停用：`--no-countdown`。

輪播池預設抓 **30 支** shorts（上限也是 30）。這代表下載量，抓滿 30 支大約 10–15 分鐘；已經抓過的會跳過。

⚠️ 疊圖用了 `-loop 1`，所以長度上限一定要取「指定上限」與「base 本身長度」的**較小值**。只給 `-t` 上限的話，比它短的 base 會被撐長、尾巴變成凍結的最後一格（實測：53 秒的 short 變成 90 秒）。

### 畫面浮水印：標題 ＋ 首播日期

每支影片的右上角會顯示「**原影片標題　首播日期：YYYY-MM-DD**」。標題太長（超過畫面寬度扣掉邊界）時**自動改成跑馬燈**，以 120 px/s 由右往左捲動。

實作在 `build_local_content.py`：

- 標題取自 `playlist-*.json` 的 `segments[].title`（由 `build_playlist.py` 逐支抓回）
- 文字用 `wmtext.py` 畫成 PNG（PIL ＋ 系統 STHeiti 字型，所以支援中文）
- 放得下就用 `overlay=x=W-w-40`；放不下就換成 `overlay=x='W-mod(t*120,W+w+220)'`

驗證跑馬燈有沒有真的在動：拿標題開頭的幾個字當模板，在不同時間點用模板比對找位置，應該要與公式吻合。

調整浮水印時**務必加 `--keep-raw`**：原始下載檔會留在 `media/.raw/`，可以重複轉檔而不用重新下載，也不會多一次畫質損失。

### 畫面按鈕：連到原影片

標題列下方（右上角）有一個「▶ 看原片」按鈕，內含小 QR 與短網址 youtu.be/<id>。

**必須說清楚的限制**：直播影片的像素不能被點擊，所以這是**視覺提示**而不是真的按鈕。要讓觀眾「一點就到」，只能靠 YouTube 自己的機制：

| 方式 | 可點擊 | 需要什麼 |
|---|---|---|
| 說明欄放連結 | 是 | 在 Studio 設定，或用 YouTube Data API（需 OAuth） |
| 聊天室貼連結 | 是 | YouTube Data API（需 OAuth） |
| 影片資訊卡 | 是 | 只能在 Studio 手動加，API 不支援 |
| 畫面按鈕＋QR（本系統） | 否 | 已具備，掃碼或照打短網址 |

跑馬燈的可視範圍是「左界 ~ 按鈕左緣」：

- **左界固定留畫面寬度的 1/7**（1280 ÷ 7 ≈ 182 px），讓開原片左上角的 logo。不自動判定，因為自動判定容易誤判；要覆寫用 `--band-left <像素>`。
- **右界**是連結按鈕的左緣，每支會因網址字母寬度而略有不同。

實作上不是「限制文字起點」就好 —— 文字往左捲出去時照樣會壓過 logo。所以是「可無縫捲動的長條圖 ＋ 固定視窗裁切」，文字永遠不會畫到視窗之外。驗證方式是用全黑畫面跑同一條濾鏡，檢查每一格的非黑像素有沒有超出視窗。

停用：--no-link-button。跑馬燈的 y 由 40 改為 10（上移半行）。

    python3 build_local_content.py --playlist playlist-tucheng3.json \
      --target 720 --max-seconds 180 --keep-raw

**驗證一定要從編碼後的影片解碼**，只看畫面有東西不算數：

    # build_transitions.py --verify 5 會自己抽樣驗證並印出結果

QR 內容用短網址 `https://youtu.be/<id>`（比 watch?v= 短，模組少、比較好掃）。

改了過場之後要重建清單並重啟播出端（用控制台的「重建 concat 清單」＋「重啟 playout」也一樣）：

    cd ~/loopcastr
    python3 make_concat_list.py playlist-<模式>-local.json -o concat-<模式>.txt --base-dir ~/loopcastr
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.playout     # LaunchDaemon 安裝改成 system/，前面加 sudo

播出端重啟會**從第一段重新開始**，觀眾端會看到內容跳回開頭。

### 換片時的聲音淡入淡出

播出端是 concat ＋ `-c copy`，接縫不可能即時插入濾鏡，所以淡化**必須在落地時就烤進每一段檔案**：每段開頭淡入 **2.5 秒**、結尾淡出 **2.5 秒**（`build_local_content.py` 的 `AUDIO_FADE`）。

集數與過場**每一段都有**，所以接縫兩邊都會收乾淨：前一段淡出、後一段淡入，繞回清單開頭時也一樣。沒有音軌的來源會自動跳過；長度 ≤ 3 秒的片段不套用，避免整段只剩淡化。

實測（目標機，2026-09-19）：把輸出音訊解成 PCM、每 0.1 秒算 RMS，再與未淡化的來源逐窗相減，得到實際增益曲線：

    淡入  -29.2  -23.9  -20.9  -16.3  -14.8  -13.1  ...  （1.0 秒處 -7.5）  ...  0 dB
    淡出  ...  （尾前 1.0 秒 -7.6）  ...  最後一窗 -24.4 dB

六段（3 集 + 3 段過場）在「**淡入 1.0 秒處**」都量到 **-7.3 ~ -7.8 dB**，與 2.5 秒線性淡化（20·log₁₀t）的理論值 -7.5 dB 吻合。這個檢查點就是分辨秒數的關鍵：**1 秒淡化在這裡會是 0 dB，2.5 秒是 -7.5 dB**，相差 7.5 dB，不會被內容變化干擾。過場的標題列像素與舊版一致（20961 vs 20958 等），QR 各自解回正確網址。

調整秒數只要改 `AUDIO_FADE`；改完要重跑 `build_local_content.py`（加 `--keep-raw` 就不用重新下載，3 集約 100 秒）與 `build_transitions.py`（3 段約 15 秒）。

#### 播出中換檔：先寫暫存目錄，再原子置換

`build_local_content.py --out-dir` 與 `build_transitions.py --out-dir` 會把成品寫到指定目錄。播出端是單一行程 concat 加 `-stream_loop -1`，**每個循環都會重新開啟檔案**；直接覆寫正在播的那一支，會讓它讀到寫到一半的內容（沒有 moov，ffmpeg 開不起來，播出端就會重啟並從第一段重來）。先寫到暫存目錄、驗完再 `mv` 進 `media/`，`mv` 在同一個 volume 上是原子置換，播出端只會拿到完整的舊檔或新檔。

    python3 build_local_content.py --playlist playlist-tucheng3.json --target 720 \
      --max-seconds 180 --keep-raw --out-dir /tmp/stage-ep --out-playlist /tmp/pl.json
    python3 build_transitions.py --playlist playlist-tucheng3.json \
      --shorts-url "https://www.youtube.com/<SHORTS_CHANNEL>/shorts" --shorts-count 30 \
      --seconds 90 --button-caption 去追劇 --parallel 2 --out-dir /tmp/stage-tr
    # 驗證通過後
    mv /tmp/stage-ep/*.mp4 media/
    mv /tmp/stage-tr/_tr_*.mp4 media/

⚠️ **暫存目錄請放 `/tmp`，不要放在 `media/` 底下。** 2026-09-19 實測：寫到 `media/.stage*/` 時，ffmpeg 連續三次在收尾階段停滯（檔案大小不再變動、CPU 0%、moov 沒寫出來、主執行緒停在 `sch_wait`），改寫到 `/tmp` 之後同樣的工作 13 秒就完成。同一時間看到 `mediaanalysisd` 吃到 **111% CPU**，正在重複分析我們一直被重寫的 mp4；`/tmp` 不在 Spotlight 索引範圍內。

對策：把 `~/loopcastr` 加進 Spotlight 的隱私清單。這會順便省掉那顆一直在跑的 `mediaanalysisd`（實測累積 346 分鐘 CPU 時間）——多線播出時那些都是白佔的 CPU。

另外，若單次建置中途卡住，**不加 `--force` 重跑就有續傳效果**：要不要處理是以「輸出目錄裡有沒有這個檔案」判斷的，已完成的那幾支會自動跳過，只補沒完成的那一支。

### 容量與資源（fd 上限、HLS）

要把同時直播的線數拉上去之前，先處理兩個會在 100 線附近咬人的設定。

| 項目 | 原本 | 現在 | 為什麼 |
|---|---|---|---|
| `maxfiles` | 256 | **8192** | MediaMTX 每多一條路徑＋讀者約 +2 個 fd（實測：1 條 61、13 條 85），256 大約 97 線就爆 |
| MediaMTX `hls` | `yes` ＋ `hlsAlwaysRemux: yes` | **`no`** | 沒有人在用 HLS，但每條路徑都會白做一次 remux（實測每路徑約 +1.1% CPU） |

上表的 `hls: no` 是**多線產能**的取捨（也正好是 repo 的預設值）。單機自用時
把 `hls: yes` 開回來，控制台標題下面就會多一顆「▶ 看直播畫面」可以直接看播出結果，
代價就是上面那個每路徑約 +1.1% CPU。

```bash
# fd 上限：系統預設 ＋ 服務層（plist 才是重開機後仍然有效的那一層）
sudo launchctl limit maxfiles 8192 unlimited
# mediamtx plist 加上：
#   SoftResourceLimits / HardResourceLimits → NumberOfFiles = 8192
sudo launchctl bootout system/com.loopcastr.mediamtx
sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.mediamtx.plist
```

實測驗證（2026-09-19 06:47–06:50）：

- 新開 shell 的 `ulimit -n` 由 256 → **8192**
- `:8888` 不再 listening（HLS 確實關閉）
- 重啟 MediaMTX 期間 YouTube 端斷約 **10–20 秒**：health 在 06:47:58 記到一次 `FAIL 沒有讀者`，06:49:04 恢復 `OK`。playout 與 publish 都由 launchd 自動接回，**不需要人工 kickstart**
- ⚠️ playout 重啟會**從第一段重新開始**（觀眾端會看到內容跳回開頭），這是播出端重啟的既有行為，不是這次改動造成的

多線與代客服務的產能規劃不在本 repo 範圍內。

### 三種播出模式

模式定義在 `modes.json`，整條內容鏈交給 `mode_build.py` 建置：

| 模式 | 影片 | 每支長度 | shorts 池 | 自動重新掃描 |
|---|---|---|---|---|
| `news` 新聞模式 | 來源頻道最新 N 支 | 全長 | 最新 15 支 | 每 300 秒 |
| `promotion` 推廣模式 | 來源頻道最新 30 支 | 最多 450 秒（不必播完） | 最新 50 支 | 每 1800 秒 |
| `test` 測試模式 | 3 支 | 180 秒 | 6 支 | 不掃描 |

上表是 `src/modes.json` 的**範例值**；實際值就是你在控制台「① 來源設定」填的那些，
存在部署目錄的 `modes.json`（改完下次建置生效）。

每個模式還有一個 `max_age_hours`（0＝不限）：**只播首播時間在 N 小時內的影片**。
判斷順序是「先用 `video_limit` 取前 N 支，再過濾年齡」，所以「只播最近 24 小時」要配一個
夠大的 `video_limit`。過濾後一支都不剩時，掃描會直接失敗（`exit 2`）並且**不覆蓋**原本的清單 ——
播出端拿到空的 concat 清單會中止，寧可這輪不換。

畫面右上角的首播時間格式是 `YYYY/MM/DD HH:MM`（本機時區）。來源是 yt-dlp 的
`release_timestamp`；沒有的話退回 `timestamp`，再沒有就只顯示日期 ＋ `00:00`。

    python3 mode_build.py --mode promotion            # 掃描 → 落地 → 過場 → 部署 → 重建清單
    python3 mode_build.py --mode promotion --switch   # 上面全部做完，再切換播出端
    python3 mode_build.py --mode promotion --scan-only

    ./switch_edition.sh promotion                     # 清單已建好時，只切換播出端

`mode_build.py` 的流程刻意分成「暫存 → 驗證 → mv」：影片先寫到 `/tmp/stage-ep-<模式>/`、過場寫到 `/tmp/stage-tr-<模式>/`，逐檔驗過長度才 mv 進 `media/<模式>/`（為什麼不直接寫 `media/`：播出端是單一行程 concat，直接覆寫正在播的檔案會讓它讀到沒有 moov 的半成品）。中途卡住或中斷時，**不加 `--force` 重跑會自動續傳**（要不要做是以暫存目錄裡有沒有這個檔案判斷）。

#### 邊轉檔邊開播：`first_batch`

整批建置要等全部轉完才開播（news 55 支全長要好幾小時）。設了 `first_batch` 之後：

    scan（全部）→ 做前 N 支＋它們的過場 → 部署 → 用「已就緒的子集」重建清單 → **開播**
      → 下一批 → 部署 → 重建清單 → 等下一個換片點 → 重啟播出端 → …

控制台「① 來源設定」的「先做幾支就開播（0＝全部做完才切換）」就是這個值。
實測 `test` 模式（`first_batch=2`）：第一批做完約 25 秒就上線，之後每批擴充一次，
每次都在**下一個換片點**重啟，所以觀眾看到的是「先播這幾支、之後自動變成完整清單」，
不會看到內容跳回開頭。

注意：**concat 清單是播出端啟動時讀一次**（實測：播放中 append 進清單的片段完全不會被播到），
所以每次擴充都必須重啟播出端 —— 換片點重啟是為了不讓觀眾看到中斷，不是為了省掉重啟。

#### 重新建置時不會重編已經做好的

每支影片與過場都記了「編碼參數指紋 ＋ 檔案大小」，重新建置時如果 media 裡那一份還在、
指紋一樣、大小也一樣就跳過。改了畫質／浮水印／長度上限…指紋就會變，那些檔案才重做。

    python3 mode_build.py --mode news            # 只補缺的（指紋沒變的不重做）
    python3 mode_build.py --mode news --force    # 全部重做

#### 重新掃描與「下一支就從頭開始」

`refreshwatch.py` 依 `refresh_seconds` 用 flat 模式重新掃描來源（幾秒鐘就好），比對影片 ID 與 shorts ID，**有變化才重建**：

    sudo python3 refreshwatch.py --mode promotion     # 前景常駐

重建完成後**等到下一個換片點**才重啟播出端。播出端是單一行程 concat，重啟就是從第一段重來；若在影片播到一半時重啟，觀眾會看到中途被切掉，等到換片點才切，體感就是規格說的「再下一支影片就從頭開始輸播」。換片點是用「開播時間 ＋ 各段累加長度」推算的（跟 `loopwatch.py` 同一套）。

重啟 system domain 的服務需要 root，所以這支要用 root 跑（跟 `com.loopcastr.health` 同一個理由）；非 root 時會退回用 `SUDO_PASS`。

#### 已知取捨

1. **內容已經每個模式一份**（`media/<模式>/`，見上面「內容放在哪裡」）。同一支影片在
   不同模式可以有不同長度上限而不互相蓋掉。**共用**的只有輸入：`media/.raw/` 與 shorts 池。
2. **shorts 池的輪替有上限。** 配法是「第 p 趟的第 i 集用池子裡第 `(p × 集數 + i - 1) % N` 支」，
   `passes` 預設由 `ceil(池子 ÷ 集數)` 自動算、上限 5。所以池子裡前「集數 × passes」支一定輪得到，
   超出的那幾支這一輪不會出現（例：30 集配 200 支 shorts → 只用到前 150 支）。
   要全部都輪到就把 `shorts_passes` 調大，或把 `shorts_count` 縮小。
3. **播放順序是「固定序循環」。** 重建時照來源順序取前 `video_limit` 支（頻道就是最新在前），
   之後每一輪都是同一個順序；新片上架要重新掃描才會進來，進來之後順序會整批往前挪。

### 服務：先確認「有沒有被載入」

要推上 YouTube 一定要有 `com.loopcastr.publish`。沒有它，畫面只到 MediaMTX ——
控制台的「看直播畫面」看得到內容、但 YouTube 端是黑的，因為根本沒有東西連上 YouTube ingest。

控制台最下面的「服務」區塊分三種狀態：

| 顯示 | 意思 | 可以做什麼 |
|---|---|---|
| 執行中 pid N | launchd 有這個 job，行程也在 | 重啟 |
| 已載入（沒在跑） | job 在，行程被 KeepAlive 拉起來中 | 重啟 |
| 沒有載入 | launchd 根本沒有這個 job | **啟動**：把 `~/loopcastr/<label>.plist` 複製到 `~/Library/LaunchAgents` 再 `launchctl bootstrap` |

「沒有載入」是資料目錄裡有 plist、但沒有裝進 launchd 的狀態（例如只裝了三個服務的精簡安裝）。
要自己確認：

    launchctl list | grep loopcastr

`com.loopcastr.health` 與 `com.loopcastr.refresh` 原本是設計成系統 domain 的服務
（要 root 才能重啟播出端），控制台不以 root 執行，這兩個要自己來：

    sudo cp ~/loopcastr/com.loopcastr.health.plist /Library/LaunchDaemons/
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.health.plist

換過 stream key 之後要重啟 publish 才會生效（`yt_publish.sh` 啟動時讀一次金鑰檔）：
按服務區塊的「重啟 publish」，或

    launchctl kickstart -k gui/$(id -u)/com.loopcastr.publish
