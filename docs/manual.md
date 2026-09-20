
# ytpl 操作手冊（manual）

> 本文件為繁體中文。英文說明見 repo 根目錄 [README.md](../README.md) 的 English 一節。
> This document is in Traditional Chinese; see the English section of [README.md](../README.md).

從架構、部署，到日常操作、故障排除與已知限制。設計取捨與實測數字記在 [變更與實測紀錄](changelog.md)。


> 這份是**現在的狀態與怎麼操作**。逐日的實測、改動與被推翻的假設記在 [變更與實測紀錄](changelog.md)。
> This is the operations manual. Day-by-day measurements and corrections live in [changelog](changelog.md).

## 一句話架構

    media/*.mp4 ──concat──> playout.sh（單一 ffmpeg）──> MediaMTX ──yt_publish.sh（單一長命 ffmpeg）──> YouTube ingest

本機檔案走 concat；只有「需要換來源」的 YouTube 直播聯播才回到 `relay.py` 的 takeover 接力。

## 檔案

安裝與設定：

| 路徑 | 角色 |
|---|---|
| `install.sh` | **安裝／升級**：複製程式、代入 plist 佔位符、產生 `mediamtx.yml`、註冊 launchd 服務 |
| `src/settings.json` | 通用設定：畫質、fps、位元率、淡化秒數、浮水印與跑馬燈、黑尾門檻。程式讀它當**預設值**，命令列可覆寫 |
| `src/modes.json` | 播出模式定義：各模式的來源頻道、長度上限、shorts 池、重新掃描頻率 |
| `src/playlist.example.json` | 母清單**範例**（3 筆假 id）。實際的 `playlist.json` 由 `build_playlist.py` 產生，已列入 `.gitignore` |
| `mediamtx.example.yml` | MediaMTX 範例設定。刻意用**路徑白名單**（只開 `live/main`），不是 MediaMTX 預設的全開 |

建置內容：

| 路徑 | 角色 |
|---|---|
| `src/build_playlist.py` | 掃描播放清單或頻道，產生母清單 `playlist.json`（含每支長度） |
| `src/build_local_content.py` | 把母清單抓成本機 `media/<id>.mp4`：正規化、長度核對、黑尾偵測、浮水印與倒數、淡入淡出 |
| `src/build_transitions.py` | 產生每集專屬過場（QR 指向該集），可吃 shorts 池輪播 |
| `src/build_test_edition.py` | 產生縮短的測試版：每集剪成固定秒數，右上角燒流水號 |
| `src/mode_build.py` | 依 `modes.json` 跑完整條鏈：掃描 → 落地 → 過場 → 部署 → 重建清單 |
| `src/wmtext.py`、`src/make_qr_png.py` | 浮水印文字與 QR Code 的 PNG 產生（機器上沒有 freetype，所以自己畫） |

播出：

| 路徑 | 角色 |
|---|---|
| `src/make_concat_list.py` | 把 `playlist-local.json` 轉成 ffmpeg concat 清單 `concat.txt` |
| `src/playout.sh` | **播出端**：單一行程 concat 循環播出，推 MediaMTX。由 `com.ytpl.playout` 看管 |
| `src/yt_publish.sh` | **推流端**：單一長命 ffmpeg 從 MediaMTX 推到 YouTube ingest。由 `com.ytpl.publish` 看管 |
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
| `launchd/com.ytpl.mediamtx.plist` | 媒體樞紐（含 8192 fd 的 ResourceLimits） |
| `launchd/com.ytpl.playout.plist` | 播出端 |
| `launchd/com.ytpl.publish.plist` | 推流端 |
| `launchd/com.ytpl.health.plist` | 健康監控（以 root 執行，才能 kickstart system domain） |
| `launchd/com.ytpl.refresh.plist` | 自動重新掃描 |

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
    ./install.sh               # 預設裝到 ~/ytpl，用 LaunchDaemon（需要 sudo）
    ./install.sh --agents      # 裝成 LaunchAgent：不需 root，但要有圖形登入

可以重複執行；已存在的 `mediamtx.yml` 與 `stream.key` 不會被覆蓋。
服務在還沒有播出內容（`playlist-local.json`／`concat.txt`）時不會啟動，
避免 launchd 一直重啟一個註定失敗的行程。

驗收（播出中，另開一個終端）：

    python3 ~/ytpl/gapwatch.py http://127.0.0.1:9997 live/main 120

## 上線前務必確認

- `playlist-local.json` **必須存在且每段檔案都在磁碟上**，否則 `playout.sh` 會在建立 concat 清單時直接中止（設計如此，避免播出半份清單）。
- `stream.key` 權限 `600`，不得進版控。
- 本機與目標機的 `TZ` 都是 `Asia/Taipei`，日誌時間戳可直接對照。

## 操作手冊

### 日常看一眼

    ssh <USER>@<TARGET_HOST>
    launchctl list | grep ytpl          # 四個服務都在嗎
    tail -3 ~/ytpl/logs/health.log      # 全鏈路正常嗎
    tail -3 ~/ytpl/logs/alerts.jsonl    # 有沒有告警過

`health.log` 每 60 秒一行。看到 `OK 全鏈路正常（流量 +N bytes / 6s）` 就是正常，N 大約 2,000,000。

### 換直播金鑰

`yt_publish.sh` 只在**啟動時**讀一次金鑰，改了檔案一定要重啟：

    printf %s 新金鑰 > ~/ytpl/stream.key && chmod 600 ~/ytpl/stream.key
    launchctl kickstart -k gui/$(id -u)/com.ytpl.publish

### 加新集數

1. 把新集數加進 `~/ytpl/playlist.json` 的 `segments`（`type: vod`、`url`、`seconds`）。
2. 落地（會自動正規化、檢查長度、偵測黑尾）：

       cd ~/ytpl && python3 build_local_content.py --target 720

3. 重建清單並套用：

       python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/ytpl
       launchctl kickstart -k gui/$(id -u)/com.ytpl.playout

`build_local_content.py --status` 隨時可以看還缺哪幾支。

### 只想重掃黑尾（不重新下載）

    python3 build_local_content.py --rescan

### 服務開關

    # 停
    launchctl bootout gui/$(id -u)/com.ytpl.publish
    launchctl bootout gui/$(id -u)/com.ytpl.playout
    # 起
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ytpl.publish.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ytpl.playout.plist
    # 重啟（不卸載）
    launchctl kickstart -k gui/$(id -u)/com.ytpl.playout

播出端重啟會從第一段重新開始，並有約 2.5 秒的冷啟動縫。

+### 本機控制台（webui.py）

不用 ssh、不用背指令的介面。只用標準庫，不必額外安裝任何東西。

    cd ~/ytpl
    python3 webui.py                 # http://127.0.0.1:8787
    python3 webui.py --port 9000

    # 要讓它常駐
    nohup python3 ~/ytpl/webui.py > ~/ytpl/logs/webui.log 2>&1 &

三個區塊：

| 區塊 | 內容 |
|---|---|
| 狀態 | 服務行程、MediaMTX ready／讀者數／流量、單輪長度與下次循環時間、concat 缺檔、media 大小、health 與 alerts 尾端 |
| 設定 | 直接編輯 `settings.json` 與 `modes.json`。存檔前驗 JSON，舊版留成 `.bak`，下次建置生效 |
| 動作 | 建置／只掃描／建置並切換、重建 concat、檢查缺檔、停止建置。背景執行並回報進度（建置可能數十分鐘） |

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

頁面標題的專案名（`ytpl2ytstream`）連到 GitHub 專案頁，另開分頁。
標題下面那顆「▶ 看直播畫面」開的是 MediaMTX 的 HLS 頁：

    http://<主機>:<hlsAddress 的埠>/<路徑>/      # 本機預設 http://127.0.0.1:8888/live/main/

主機名稱由瀏覽器自己填，所以從別台機器開控制台也通。
位址與是否顯示都讀 `mediamtx.yml`：`hls: no`（repo 預設）時按鈕不會出現，
會改成一行灰字說明為什麼沒有。要開就改 `hls: yes` 並重啟 mediamtx。

#### 安全設計

- **預設只綁 `127.0.0.1`。** 要對外開放必須提供 token，否則拒絕啟動：

      openssl rand -hex 16 > ~/ytpl/webui-token && chmod 600 ~/ytpl/webui-token
      python3 webui.py --host 0.0.0.0

  之後用 `?token=<值>` 或 `X-Ytpl-Token` 標頭存取。
- **所有寫入都要求自訂標頭 `X-Ytpl: 1`**：跨站表單帶不了這個標頭，等於擋掉 CSRF。
- **不以 root 執行，也不保管密碼。** 需要重啟 system domain 服務時只試 `sudo -n`（非互動），失敗就顯示要加的 sudoers 白名單，不會把密碼餵進程式：

      # /etc/sudoers.d/ytpl-webui
      <你的帳號> ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.ytpl.playout
- **stream key 只進不出**：可以寫入，但頁面永遠不會把它顯示出來。
- 動作只呼叫既有 script（argv 清單、不經 shell）；模式名稱必須存在於 `modes.json`，不接受任意路徑。

#### settings.json 是什麼

它是「給人改的」那一份，也是這支控制台在編輯的東西。程式讀它當**預設值**，
命令列參數永遠可以逐次覆寫；檔案不存在時，行為與沒有這個機制時完全相同。

| 區塊 | 內容 |
|---|---|
| `media` | `target`（720／1080／480）、`fps`、`venc`、`abr`、`audio_fade`（換片淡入淡出秒數）、`max_seconds` |
| `overlay` | `date_label`、位置（`overlay_y`／`overlay_margin`／`band_left`）、跑馬燈（`marquee_speed`／`marquee_gap`）、按鈕（`link_button`／`link_caption`）、`countdown`、`transition_caption` |
| `content` | `black_tail_min`（片尾黑畫面幾秒算黑尾） |

要改「播什麼」請改 `modes.json`，不要在 `settings.json` 裡塞來源資訊 —— 兩份真值會互相打架。

### 故障排除

| 症狀 | 先看 | 常見原因 |
|---|---|---|
| 觀眾端黑畫面 | `python3 build_local_content.py --rescan` | 某支影片本身有長黑尾 |
| 黑畫面但 health 正常 | 對本地 HLS 跑 blackdetect | 內容層問題，傳輸與時間軸都看不出來 |
| YouTube 沒畫面但本地正常 | `tail ~/ytpl/logs/publish.log` | 金鑰失效、直播活動結束、或 ingest 被拒 |
| health 一直 FAIL | `cat ~/ytpl/logs/health-state.json` | 看 `problems` 欄位；連續 3 次會自動重啟對應服務 |

### 已知限制

- **重開機需要人工解鎖**：目標機開了 FileVault 且沒有自動登入（見規格書 D16）。
- **連續播出約 49.7 天**會遇到 FLV 32 位元時間戳回繞，建議每月重啟一次播出端。
- **換片點有約 11 ms 的音訊時間戳重疊**（`Non-monotonic DTS` 警告），ffmpeg 會自動夾正，聽感無影響。要完全消除需先離線預接成單一大檔。

### 服務位置與開關

服務現在在 /Library/LaunchDaemons/，操作要加 sudo，domain 是 system 不是 gui：

    sudo launchctl list | grep ytpl
    sudo launchctl kickstart -k system/com.ytpl.playout
    sudo launchctl bootout system/com.ytpl.playout
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.ytpl.playout.plist

playout、publish、mediamtx 以 <USER> 身分執行；health 以 root 執行，因為只有 root 能對 system domain 做 kickstart（那是自動修復的必要條件）。

舊的 LaunchAgent 版本已移到 ~/ytpl/launchagents-backup/，不會再被載入。

### 循環邊界觀測（loopwatch.py）

播出端是單一行程 concat 加 -stream_loop -1，時間軸會「連續累加」—— 繞回時 DTS 不會掉回 0，所以不能用 DTS 當訊號。這支改用「開播時間 ＋ 單輪長度」推算循環點，在事件前後高頻取樣 MediaMTX API：

    python3 loopwatch.py --lead 45 --tail 90
    python3 loopwatch.py --at 04:19:07      # 也可直接指定時刻

輸出會列出跨循環點的接收端離線區段與 bytesReceived 零成長區段。單輪長度取自 playlist-local.json（含 outpoint 修正），約 14,487 秒。

### 過場影片

每一集之後（含最後一集之後）插入一段過場。過場檔是 `media/_transition.mp4`，**只要檔案存在就會自動插入**，所以設定是持久的。

    # 第一次設定（YouTube URL 或 video id 都可以）
    python3 build_local_content.py --transition lj9nUq97uzQ

    # 換一段過場（加 --force 才會重抓）
    python3 build_local_content.py --transition <新的 id 或本機檔案> --force

    # 暫時停用過場（檔案留著，不插入）
    python3 build_local_content.py --no-transition

    # 想完全移除就刪檔
    rm ~/ytpl/media/_transition.mp4

過場會跟其他片段一樣被正規化成 1280x720 / H.264 High L3.1 / 30fps / AAC-LC 48k 立體，所以 concat 一樣是純 `-c copy`、不需要轉碼。實測 106 段（53 集 + 53 段過場）時間軸 forward gap 為 0。

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

### 過場影片上的頻道 QR Code

過場右下角有一個指向頻道的 QR Code（`https://www.youtube.com/channel/<CHANNEL_ID>`），方便觀眾掃碼傳播。

`make_qr_png.py` 負責產生：目標機沒有 qrencode、沒有 PIL，所以只裝純 Python 的 `qrcode` 拿矩陣，PNG 自己用 zlib + struct 寫。

    python3 make_qr_png.py "<網址>" out.png --scale 8 --border 4

**改 QR 內容或換圖**：乾淨版過場留在 `media/_transition-clean.mp4`，重疊時從它出發、不要從已經疊過的版本再疊（會愈疊愈花）：

    python3 make_qr_png.py "<新網址>" /tmp/qr.png --scale 8 --border 4
    ffmpeg -y -i media/_transition-clean.mp4 -i /tmp/qr.png \
      -filter_complex "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v];[v][1:v]overlay=W-w-40:H-h-40[out]" \
      -map "[out]" -map 0:a -t 20 -c:v libx264 -preset veryfast -profile:v high -level 3.1 \
      -g 60 -b:v 2500k -maxrate 2500k -bufsize 5000k -c:a aac -b:a 128k -ar 48000 -ac 2 \
      -movflags +faststart media/_transition.mp4

驗證一定要做 —— 從**編碼後的影片**抽格解碼，不是只看畫面有沒有東西：

   python3 -c "import cv2,subprocess;subprocess.run(['ffmpeg','-y','-ss','10','-i','media/_transition.mp4','-frames:v','1','/tmp/f.png']);print(cv2.QRCodeDetector().detectAndDecode(cv2.imread('/tmp/f.png'))[0])"

### 過場 QR 是「一集一份」

過場的用途是讓觀眾掃碼去看**剛播完的那一集**，所以 QR 內容是那一集的網址，不是頻道網址。53 集就是 53 份過場：`media/_transition-01.mp4` … `_transition-53.mp4`。

    python3 build_transitions.py --parallel 3 --verify 5

`build_local_content.py` 會依序在每一集後面插入對應的 `media/_tr_<影片id>.mp4`；萬一某集沒有專屬檔，會退回共用的 `_transition.mp4`。

用**影片 ID** 而不是序號命名，是為了換清單時兩份的過場不會互相覆蓋（序號會撞號，切回舊清單還會拿到錯的 QR）。

### 用 shorts 輪播當過場

過場不一定要用固定一支影片，也可以吃一個 shorts 池輪流播：

    python3 build_transitions.py --playlist playlist-tucheng3.json \
      --shorts-url "https://www.youtube.com/<SHORTS_CHANNEL>/shorts" \
      --shorts-count 3 --seconds 90 --parallel 3 --verify 3

它會抓前 N 支 shorts、正規化成與其他片段一致的參數（1280x720 / H.264 High L3.1 / 30fps / AAC-LC），再依序把每集的 QR 疊上去。第 i 集用第 `i % N` 支，所以會輪替。

**直式短片會被縮小補黑邊**（pillarbox），不裁切也不變形。實測 1080x1920 的 short 縮成 404x720、左右各留約 438px 黑邊。

`--seconds` 是每段過場的長度上限；短片的實際長度若更短就照原長。

過場上也會有**標題跑馬燈**（與集數相同的樣式），但左界給 0 —— 直式短片兩側本來就是黑邊，不需要像集數那樣讓開 logo 的空間。

過場的 QR 與集數用**同一個元件**（同樣的圓角面板、同樣大小的 QR），差別只在說明文字是「追劇去」且放在 QR **上方**，整個按鈕固定在畫面**右下角**。文字可用 `--button-caption` 改（預設「追劇去」），`--no-button` 可整個關掉。
過場的 QR 與集數**完全一致**：同一個元件、同樣的位置（右上角）、同樣的說明文字與排版，不做任何區分。`--button-caption` 可覆寫文字，`--no-button` 可整個關掉。
過場的 QR 與集數用**同一個元件**、**同樣的位置**（右上角）與排版；差別只在說明文字：集數是「看原片」，過場是「**去追劇**」（`--button-caption` 可改，`--no-button` 可整個關掉）。

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

目前設定：`lj9nUq97uzQ`（20 秒，4K 來源降轉）。單輪總長由 14,487 秒變成 **15,549 秒（4 小時 19 分）**。改動過場後記得重建清單並重啟播出端：

    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/ytpl
    sudo launchctl kickstart -k system/com.ytpl.playout

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

對策：把 `~/ytpl` 加進 Spotlight 的隱私清單。這會順便省掉那顆一直在跑的 `mediaanalysisd`（實測累積 346 分鐘 CPU 時間）——多線播出時那些都是白佔的 CPU。

另外，若單次建置中途卡住，**不加 `--force` 重跑就有續傳效果**：要不要處理是以「輸出目錄裡有沒有這個檔案」判斷的，已完成的那幾支會自動跳過，只補沒完成的那一支。

### 容量與資源（fd 上限、HLS）

要把同時直播的線數拉上去之前，先處理兩個會在 100 線附近咬人的設定。

| 項目 | 原本 | 現在 | 為什麼 |
|---|---|---|---|
| `maxfiles` | 256 | **8192** | MediaMTX 每多一條路徑＋讀者約 +2 個 fd（實測：1 條 61、13 條 85），256 大約 97 線就爆 |
| MediaMTX `hls` | `yes` ＋ `hlsAlwaysRemux: yes` | **`no`** | 沒有人在用 HLS，但每條路徑都會白做一次 remux（實測每路徑約 +1.1% CPU） |

```bash
# fd 上限：系統預設 ＋ 服務層（plist 才是重開機後仍然有效的那一層）
sudo launchctl limit maxfiles 8192 unlimited
# mediamtx plist 加上：
#   SoftResourceLimits / HardResourceLimits → NumberOfFiles = 8192
sudo launchctl bootout system/com.ytpl.mediamtx
sudo launchctl bootstrap system /Library/LaunchDaemons/com.ytpl.mediamtx.plist
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

    python3 mode_build.py --mode promotion            # 掃描 → 落地 → 過場 → 部署 → 重建清單
    python3 mode_build.py --mode promotion --switch   # 上面全部做完，再切換播出端
    python3 mode_build.py --mode promotion --scan-only

    ./switch_edition.sh promotion                     # 清單已建好時，只切換播出端

`mode_build.py` 的流程刻意分成「暫存 → 驗證 → mv」：影片先寫到 `/tmp/stage-ep-<模式>/`、過場寫到 `/tmp/stage-tr-<模式>/`，逐檔驗過長度才 mv 進 `media/`。中途卡住或中斷時，**不加 `--force` 重跑會自動續傳**（要不要做是以暫存目錄裡有沒有這個檔案判斷）。

#### 重新掃描與「下一支就從頭開始」

`refreshwatch.py` 依 `refresh_seconds` 用 flat 模式重新掃描來源（幾秒鐘就好），比對影片 ID 與 shorts ID，**有變化才重建**：

    sudo python3 refreshwatch.py --mode promotion     # 前景常駐

重建完成後**等到下一個換片點**才重啟播出端。播出端是單一行程 concat，重啟就是從第一段重來；若在影片播到一半時重啟，觀眾會看到中途被切掉，等到換片點才切，體感就是規格說的「再下一支影片就從頭開始輸播」。換片點是用「開播時間 ＋ 各段累加長度」推算的（跟 `loopwatch.py` 同一套）。

重啟 system domain 的服務需要 root，所以這支要用 root 跑（跟 `com.ytpl.health` 同一個理由）；非 root 時會退回用 `SUDO_PASS`。

#### 兩個已知取捨

1. **過場與影片檔是跨模式共用的。** 過場用影片 ID 命名（`media/_tr_<id>.mp4`），所以切換模式時該模式的過場會覆蓋上一個模式；同一支影片在兩個模式的長度上限若不同（test 180 秒 vs promotion 450 秒），影片檔也會被覆蓋。目前一次只跑一個模式，這樣最簡單；要讓模式並存，得把內容改放 `media/<模式>/`。
2. **shorts 池是「第 i 支影片配第 i 支 short」。** 30 支影片只會用到池子裡的前 30 支，50 支裡有 20 支這一輪輪不到。要讓 50 支都出現，得在每次重建時把起點偏移（`refreshwatch.py` 已經會定期重建，加上偏移即可）。
