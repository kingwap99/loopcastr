# 變更與實測紀錄（changelog）

> 本文件為繁體中文。英文說明見 repo 根目錄 [README.md](../README.md) 的 English 一節。
> This document is in Traditional Chinese; see the English section of [README.md](../README.md).

這裡是**逐日**的實測數字、修正，以及被推翻的假設，依時間排序。
操作方式與系統現況請看 [操作手冊](manual.md)；這裡只回答「當初為什麼這樣做、量到什麼」。

> 新的改動請**往下追加**，不要改上面的歷史：這些數字的價值就在於它們是當時真的量到的。

---

## 目標機環境缺口盤點（2026-09-16 05:11 實測）

| 項目 | 狀態 |
|---|---|
| `brew 7.0.2` | OK |
| `ffmpeg 9.0.1` | OK，**且有 libx264／libx265／h264_videotoolbox**（先前「沒有 libx264」的說法是錯的） |
| `yt-dlp 2026.08.19` | OK |
| `node v26.8.2`／`python3 3.14.7` | OK |
| `mediamtx` | OK，已由 `launchd com.ytpl.mediamtx`（PID 常駐）管理 |
| `streamlink` | 未安裝，**不需要**（`relay.py` 完全沒用到） |
| `ffmpeg` 的 `drawtext` | 0 筆（無 freetype）→ 字卡走路徑 A（HTML→PNG→overlay） |
| 磁碟可用 | 37 GiB（26% 使用） |
| `~/ytpl/stream.key` | 已就位，24 字元 |
| 播出／推流程式 | **本輪已部署**（`relay.py`、`playout.sh`、`yt_publish.sh`、`make_concat_list.py`、`build_local_content.py`、`gapwatch.py`） |
| `playlist-local.json` | 尚未產生（**缺內容，見下**） |

## 唯一的阻塞：YouTube bot 封鎖

【實測 2026-09-16】對外 IP `<PUBLIC_IP>` 的匿名解析一律回：

    ERROR: [youtube] <id>: Sign in to confirm you're not a bot.

測過而且**確認無效**的路徑：換 player client（web／web_embedded／mweb／default／android_vr／tv／ios／tv_embedded／tv_simply／web_safari）、掛 bgutil PO Token provider（日誌確實印出取得 player PO Token 仍擋）、`--impersonate chrome/safari` TLS 偽裝、innertube API 直查。極熱門片（如 `dQw4w9WgXcQ`）例外，其餘全滅 → 是 IP 層級的 session 標記。

**有效解只有一個：登入 cookie。** 兩台機器共用同一條 NAT，所以換機器沒用。

取得方式（任一）：

1. 瀏覽器登入 YouTube 後用擴充套件匯出 Netscape 格式 `cookies.txt`，放到 `~/ytpl/cookies.txt`（`chmod 600`）。
2. 或 `yt-dlp --cookies-from-browser chrome` —— **本機測過不行**：macOS TCC 擋住 Chrome profile（`Operation not permitted`），除非把 Terminal／Codex 加入「完全取用磁碟」。

拿到 `cookies.txt` 之後：

    cd ~/ytpl
    python3 build_local_content.py --status            # 先看缺幾支
    python3 build_local_content.py --cookies cookies.txt --limit 3   # 試抓 3 支
    python3 build_local_content.py --cookies cookies.txt             # 抓滿 53 支
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/ytpl

另一條完全繞開 YouTube 的路：向創作者直接索取原始檔（授權內容），放進 `media/<id>.mp4`，一樣能跑。

## 2026-09-16 晚間更新：內容落地已打通（取代上面「唯一的阻塞」）

### 更正：那不是永久封鎖

同一天稍早（05:11）有一批影片全滅，晚上（20:02）用**完全相同的條件**重測，53 支全部解析成功。所以那是 **暫時性的 IP 標記**，不是永久封鎖，也不需要 cookie。

而且封鎖期間還有備援路徑：**`player_client=android`**。這個 client 在整批失敗時仍然可用，只是上限 360p。`build_local_content.py` 已經把它做成自動備援（`--no-fallback-client` 可關）。

### 新增必要步驟：正規化

實測這批內容的原始參數**不一致**（1280×720、1280×718，另有一批 480p）。concat 的 `-c copy` 要求所有片段完全相同，直接拼會壞掉。所以 `build_local_content.py` 預設在落地時就轉成統一參數：

    python3 build_local_content.py --target 720      # 1280x720 / H.264 High L3.1 / 30fps / AAC-LC 48k

實測驗證 5 支輸出參數完全一致，`-c copy` 拼接無誤。

### 修掉兩個會靜默毀掉播出的 bug

| Bug | 症狀 | 修法 |
|---|---|---|
| **ffmpeg 讀 stdin** | 透過 ssh heredoc 執行時，ffmpeg 把腳本內容當互動指令，讀到 `q` 就提早結束。同一支影片分別得到 137s 與 163s，**完全無錯誤訊息** | 加 `-nostdin` ＋ `stdin=DEVNULL` |
| **HLS 路徑只給半支** | 同一影片的 `m3u8` 串流只涵蓋 137s，`https`(DASH) 才是完整 270s | 格式限定 `[protocol^=https]` |

另外加了**下載後長度比對**：與清單宣稱長度差超過 5%（或 10 秒）就自動重抓，避免半支影片混進 concat。

### 目前狀態

53 支已全數落地（4.7 GB、總長 14,463 秒），四個 launchd 服務都在目標機上常駐，並已用真實金鑰推上 YouTube 公開播出。

## 2026-09-16 深夜更新：黑尾截斷、循環邊界、健康監控

### 新增：片尾黑畫面自動偵測（本輪最重要的修正）

實測 `8jtdcMDuV_A` 片尾有 **67.4 秒**純黑。播出端完全正常（傳輸連續、436,600 個封包零破洞、`framesInError=0`），但觀眾端就是一片黑。它恢復的瞬間幾乎貼齊影片結尾，所以**看起來像換片造成的**。這種問題只能看像素，`gapwatch` 與時間軸分析都抓不到。

現在 `build_local_content.py` 內建偵測（只解關鍵帧，53 支約 70 秒）：

    python3 build_local_content.py --rescan        # 對已落地檔案重掃，不用重新下載
    python3 build_local_content.py --no-auto-trim # 只標記不截斷
    python3 build_local_content.py --black-tail-min 8

抓到會寫入 `outpoint`，由 `make_concat_list.py` 轉成 concat demuxer 指令，**不需要重新編碼**：

    file '/Users/<USER>/ytpl/media/8jtdcMDuV_A.mp4'
    outpoint 551.567

### 新增：健康監控服務

    launchd/com.ytpl.health.plist   # 每 60 秒一次

檢查「真的有在動」而不是只看程序存不存在：路徑 `ready`、有讀者（推流端連著）、`bytesReceived` 在 6 秒內有成長。狀態變化才告警，持續異常每 30 分鐘重提醒。告警寫進 `logs/alerts.jsonl`。

要外送到 Line／Telegram 等服務，把 webhook URL 放進 `~/ytpl/alert_webhook`（單行純文字）即可：

    python3 healthcheck.py --check-youtube   # 連 YouTube 端 is_live 一起查（預設 15 分鐘一次）

### 已驗證：4 小時循環邊界

把清單接成兩份實測繞回點：**869,166 個封包、時間軸重置 0、forward gap 0**。時間戳是連續累加的。

附帶得到一個長期風險：FLV 時間戳是 32 位元毫秒，連續播出約 **49.7 天**會回繞，建議每月重啟一次 `com.ytpl.playout`。

### 目標機現在的服務

    launchctl list | grep ytpl
    # com.ytpl.mediamtx   媒體樞紐
    # com.ytpl.playout    播出端（concat 循環）
    # com.ytpl.publish    推流端（→ YouTube）
    # com.ytpl.health     健康監控

### 目標機 system層設定（2026-09-17 變更，重要）

原本這台機器無法無人值守重開機：FileVault 開著、沒有自動登入，所以每次重開都要有人到機器前面解鎖。已處理：

| 項目 | 原本 | 現在 |
|---|---|---|
| FileVault | On（重開需人工解鎖） | **Off** |
| 睡眠 | AC 電源 sleep 1（只是被一個 caffeinate 擋著） | sleep 0 disksleep 0 disablesleep 1 |
| 服務型態 | LaunchAgent（只在圖形登入後啟動） | **LaunchDaemon**（開機即啟動，不需登入） |

### 修掉「收尾卡死」：`-loop 1` 的無限圖輸入（2026-09-19）

正常化用的濾鏡圖會把 PNG 疊上去，PNG 用 `-loop 1` 餵（單格圖重複播放，跑馬燈與倒數靠它跟時間裁切）。實測這種**無限的圖輸入**會讓 ffmpeg 隨機卡死：

    資料都寫完了、moov 沒寫出來（或已寫完但行程不結束）、CPU 0%、
    主執行緒停在 sch_wait、其他執行緒全部閒置

同一支影片重跑有時又正常，60 秒的短片也會中（**跟長度無關**），一天內遇到 6 次以上，每次都讓整批建置停在那裡乾等。方向是：無限輸入若由主執行緒餵，主執行緒一旦卡在等濾鏡圖（`sch_wait`）就會互相等死。

對照實驗（同一支 60 秒素材、同一組濾鏡，各重跑 3 次）：

| 變體 | 結果 |
|---|---|
| 原樣 | **HANG / HANG / OK**（3 次中 2 次） |
| 圖輸入加 `-thread_queue_size 512`（給它自己的解碼執行緒） | **OK / OK / OK** |
| 圖輸入加 `-t <片長>`（讓它變成有限長度） | **OK / OK / OK** |

兩個都有效、機制互補，所以兩個都加。修好之後影片以約 7 倍速完成（450 秒素材約 80 秒）。

#### 附帶更正：`-movflags +faststart` 不是元凶

最初以為是 `+faststart`（單次對照看起來像），**後來證明不是**。真正原因就是上面那個。拿掉 faststart 的理由改成：播出端是本機檔案 ＋ concat ＋ `-c copy`，ffmpeg 會自己去檔尾讀 moov，**不需要** faststart；留著只是多做一次整檔重寫。`media/` 裡先前產的檔案仍帶 faststart，混用沒問題。

#### 最終採用的組合：`loop` 濾鏡 ＋ `-thread_queue_size`

上面那組實驗只解決「卡住」，沒解決「慢」。真正讓建置慢到不能用的是另一件事：**`-loop 1` 是 demuxer 每格重送封包，等於每個輸出影格都把整張 PNG 重解一次**。倒數長條的尺寸隨片長線性成長（450 秒 = 266×25650 px），所以成本是**片長的平方**：

| 圖輸入寫法 | 450 秒素材 | 推估 30 支 |
|---|---|---|
| `-loop 1`（每格重解一次整張圖） | **197 秒** | 約 100 分鐘 |
| `loop` 濾鏡（只解碼一次，之後重複同一個 frame） | **47 秒** | 約 25 分鐘 |

改成「單格圖輸入 ＋ `loop=loop=-1:size=1:start=0` 濾鏡」之後 PNG 只解一次，crop 的時間表達式照常運作（loop 濾鏡輸出的影格帶遞增時間戳）。畫面對照：新舊方法在同一支影片的平均像素差只有 **1.9–3.1 / 255**，倒數與跑馬燈的逐格變化量也一致（67.6 vs 67.3）。

**所以最終配置是單格圖輸入 ＋ `loop` 濾鏡（解效能）＋ `-thread_queue_size 512`（解卡死）。**

### 模式並存、shorts 輪動、服務化（2026-09-19）

#### 1. 內容改成每個模式一份：`media/<模式>/`

同一支影片在不同模式會有不同長度上限（test 180 秒 vs promotion 450 秒），原本共用一個 `media/` 會互相蓋掉。現在：

    media/<模式>/<影片id>.mp4      正規化的影片
    media/<模式>/_tr_<id>.mp4      第 1 趟的過場
    media/<模式>/_tr_<id>_p2.mp4   第 2 趟的過場（見下）
    media/<模式>/manifest.json     該模式的長度／黑尾紀錄

`build_local_content.py --media-dir` 決定這一切；`media/.raw/`（原始下載檔）與 `media/short-*.mp4`（shorts 池）仍然共用 —— 它們是輸入，跟模式無關。

#### 2. shorts 不再跟影片同步：`--passes`

過場是「第 i 支影片配第 i 支 short」，所以一輪只用到池子的前 N 支（30 支影片配 50 支 shorts 時，後 20 支永遠輪不到）。`--passes` 讓一輪包含多趟影片，shorts 接著往下輪：

| 趟 | 影片 | 配到的 shorts |
|---|---|---|
| 第 1 趟 | 1-30 | 1-30 |
| 第 2 趟 | 1-30 | **31-50，然後 1-10** |

`mode_build.py` 會自動算 `passes = ceil(shorts 池 / 影片數)`（上限 5）。推廣模式是 50/30 → **2 趟**，一輪因此變成 8.52 小時（120 段）。第 2 趟的過場檔名加 `_p2`，因為同一支影片要配不同的 short。

#### 3. 自動重新掃描做成 launchd 服務

    launchd/com.ytpl.refresh.plist   # com.ytpl.refresh，以 root 執行（才能 kickstart 播出端）

    sudo launchctl print system/com.ytpl.refresh
    tail -f ~/ytpl/logs/refreshwatch.out.log

服務帶 `--mode auto`，會**讀播出端 plist 的 PLAYLIST 自己判斷現在是哪個模式**：`switch_edition.sh` 換模式之後監看會自動跟著換，不用改服務設定；重開機也會自動恢復。

#### 短片被撐成 450 秒、音軌卻只到原始長度（2026-09-19）

`--max-seconds` 是「上限」，但實作上只把 `-t` 設成上限、**沒有跟原始長度取較小值**。當上限大於原始長度時：主影片已經 EOF，但疊圖的圖還在（`loop` 濾鏡），overlay 的 repeatlast 會把最後一格重複到上限 —— 結果是「影片被撐長、音軌在原始長度就結束」。

實測：209 秒的影片被做成 450 秒，影格 13500 格（＝450 秒 ✓）但音軌只有 **209.1 秒** ✗。播出端 `-c copy` 走到那段「有影無聲」的尾巴就停住（`bytesReceived` 不再成長），播出端的看門狗 20 秒後把它砍掉重連 —— 觀眾看到的是「播到第二支就跳回第一支」，而且每 **728 秒**循環一次（＝第一支 450 ＋ 過場 44 ＋ 第二支真實 209 ＋ 看門狗 20 ＋ 啟動誤差）。

修法：`build_local_content.py` 的 `max_seconds` 一律跟原始長度取 `min`；倒數長條的秒數也一樣（否則會從 07:30 開始倒數，但影片 3 分半就結束了）。受影響的 7 支重建後影音長度一致（209/209、174/174、196/196），單輪從 8.52 小時變成 **7.88 小時**。
