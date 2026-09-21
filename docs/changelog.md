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
| `mediamtx` | OK，已由 `launchd com.loopcastr.mediamtx`（PID 常駐）管理 |
| `streamlink` | 未安裝，**不需要**（`relay.py` 完全沒用到） |
| `ffmpeg` 的 `drawtext` | 0 筆（無 freetype）→ 字卡走路徑 A（HTML→PNG→overlay） |
| 磁碟可用 | 37 GiB（26% 使用） |
| `~/loopcastr/stream.key` | 已就位，24 字元 |
| 播出／推流程式 | **本輪已部署**（`relay.py`、`playout.sh`、`yt_publish.sh`、`make_concat_list.py`、`build_local_content.py`、`gapwatch.py`） |
| `playlist-local.json` | 尚未產生（**缺內容，見下**） |

## 唯一的阻塞：YouTube bot 封鎖

【實測 2026-09-16】對外 IP `<PUBLIC_IP>` 的匿名解析一律回：

    ERROR: [youtube] <id>: Sign in to confirm you're not a bot.

測過而且**確認無效**的路徑：換 player client（web／web_embedded／mweb／default／android_vr／tv／ios／tv_embedded／tv_simply／web_safari）、掛 bgutil PO Token provider（日誌確實印出取得 player PO Token 仍擋）、`--impersonate chrome/safari` TLS 偽裝、innertube API 直查。極熱門片（如 `dQw4w9WgXcQ`）例外，其餘全滅 → 是 IP 層級的 session 標記。

**有效解只有一個：登入 cookie。** 兩台機器共用同一條 NAT，所以換機器沒用。

取得方式（任一）：

1. 瀏覽器登入 YouTube 後用擴充套件匯出 Netscape 格式 `cookies.txt`，放到 `~/loopcastr/cookies.txt`（`chmod 600`）。
2. 或 `yt-dlp --cookies-from-browser chrome` —— **本機測過不行**：macOS TCC 擋住 Chrome profile（`Operation not permitted`），除非把 Terminal／Codex 加入「完全取用磁碟」。

拿到 `cookies.txt` 之後：

    cd ~/loopcastr
    python3 build_local_content.py --status            # 先看缺幾支
    python3 build_local_content.py --cookies cookies.txt --limit 3   # 試抓 3 支
    python3 build_local_content.py --cookies cookies.txt             # 抓滿 53 支
    python3 make_concat_list.py playlist-local.json -o concat.txt --base-dir ~/loopcastr

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

    file '/Users/<USER>/loopcastr/media/8jtdcMDuV_A.mp4'
    outpoint 551.567

### 新增：健康監控服務

    launchd/com.loopcastr.health.plist   # 每 60 秒一次

檢查「真的有在動」而不是只看程序存不存在：路徑 `ready`、有讀者（推流端連著）、`bytesReceived` 在 6 秒內有成長。狀態變化才告警，持續異常每 30 分鐘重提醒。告警寫進 `logs/alerts.jsonl`。

要外送到 Line／Telegram 等服務，把 webhook URL 放進 `~/loopcastr/alert_webhook`（單行純文字）即可：

    python3 healthcheck.py --check-youtube   # 連 YouTube 端 is_live 一起查（預設 15 分鐘一次）

### 已驗證：4 小時循環邊界

把清單接成兩份實測繞回點：**869,166 個封包、時間軸重置 0、forward gap 0**。時間戳是連續累加的。

附帶得到一個長期風險：FLV 時間戳是 32 位元毫秒，連續播出約 **49.7 天**會回繞，建議每月重啟一次 `com.loopcastr.playout`。

### 目標機現在的服務

    launchctl list | grep loopcastr
    # com.loopcastr.mediamtx   媒體樞紐
    # com.loopcastr.playout    播出端（concat 循環）
    # com.loopcastr.publish    推流端（→ YouTube）
    # com.loopcastr.health     健康監控

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

    launchd/com.loopcastr.refresh.plist   # com.loopcastr.refresh，以 root 執行（才能 kickstart 播出端）

    sudo launchctl print system/com.loopcastr.refresh
    tail -f ~/loopcastr/logs/refreshwatch.out.log

服務帶 `--mode auto`，會**讀播出端 plist 的 PLAYLIST 自己判斷現在是哪個模式**：`switch_edition.sh` 換模式之後監看會自動跟著換，不用改服務設定；重開機也會自動恢復。

#### 短片被撐成 450 秒、音軌卻只到原始長度（2026-09-19）

`--max-seconds` 是「上限」，但實作上只把 `-t` 設成上限、**沒有跟原始長度取較小值**。當上限大於原始長度時：主影片已經 EOF，但疊圖的圖還在（`loop` 濾鏡），overlay 的 repeatlast 會把最後一格重複到上限 —— 結果是「影片被撐長、音軌在原始長度就結束」。

實測：209 秒的影片被做成 450 秒，影格 13500 格（＝450 秒 ✓）但音軌只有 **209.1 秒** ✗。播出端 `-c copy` 走到那段「有影無聲」的尾巴就停住（`bytesReceived` 不再成長），播出端的看門狗 20 秒後把它砍掉重連 —— 觀眾看到的是「播到第二支就跳回第一支」，而且每 **728 秒**循環一次（＝第一支 450 ＋ 過場 44 ＋ 第二支真實 209 ＋ 看門狗 20 ＋ 啟動誤差）。

修法：`build_local_content.py` 的 `max_seconds` 一律跟原始長度取 `min`；倒數長條的秒數也一樣（否則會從 07:30 開始倒數，但影片 3 分半就結束了）。受影響的 7 支重建後影音長度一致（209/209、174/174、196/196），單輪從 8.52 小時變成 **7.88 小時**。

## 控制台加「停止建置」（2026-09-20）

原本按鈕只有「開始」：已經有工作在跑時只會回一句「已經有工作在跑」，**沒有辦法取消**。
一輪 promotion（99 支 × 1800 秒）按下去就是好幾小時，中途想改參數只能去砍行程。

### 只殺最上層沒有用

`webui.py` 拉起 `mode_build.py`，它再開 `build_local_content.py`，後者再開 `ffmpeg`。
【實測】只殺 `mode_build.py`，底下兩個會變孤兒繼續吃 CPU、繼續寫同一個檔案。
所以停止是**殺一整棵行程樹**（`ps -Ao pid=,ppid=` 建樹、子孫先收），
並且 `Popen(..., start_new_session=True)` 讓每次建置自成一個 process group
（先前它跟 webui 同組，用 `killpg` 會把控制台自己殺掉）。

### 最大的坑：ffmpeg 收到 SIGTERM 會「正常收尾」，留下一個**讀得出來的短檔**

【實測】帶疊圖的正式指令（`-filter_complex` ＋ `loop` 濾鏡 ＋ 1 fps 倒數序列）送 SIGTERM 後
**3 秒還活著**、還在以約 7 倍速寫檔，最後靠 SIGKILL 才收掉。

【實測】換成單純的 `-vf scale,fps`（無疊圖）送 SIGTERM：**0.23 秒結束，而且把 moov 寫完了** ——
產出一個 ffprobe 讀得出來、長度 `65.633333` 秒的檔案（來源是 1467 秒）。

這件事很致命，因為落地是「暫存目錄裡有這個檔案就當做完了」：

| 只用「讀不讀得出來」判斷 | 結果 |
|---|---|
| 被中斷的輸出檔 | 讀得出來 → 當成完成品 → 部署 → **播出一支 65 秒的影片** |
| 改成正解 | 停止前先記下「哪些行程正在寫哪個輸出檔」，殺掉後**無條件刪掉那幾個檔**，不看它讀不讀得出來 |

所以 `stop_task()` 會在送訊號**之前**先把目標行程的命令列抓下來，取出
`/tmp/stage-{ep,tr}-<模式>/*.mp4` 與 `media/.raw/*.mp4`（只認 120 秒內被寫過的，避免誤刪舊檔）。
剩下的暫存檔才用「ffprobe 讀不到長度」當備援規則清掉。

【實測】SIGTERM 寬限期從 8 秒改成 3 秒（反正被中斷的輸出檔一定要刪，等它優雅收尾沒有意義）。
【實測】`ffprobe` 不存在時**整段清理跳過**：一開始用「輸出裡有 not found」判斷它不存在，
但半成品的錯誤訊息正是 `moov atom not found`，會讓清理整個被跳過。改成先跑 `ffprobe -version` 確認。

### 停止期間不能開新的一輪

【實測】送完 SIGTERM，最上層的 `mode_build.py` 立刻就死了，但 ffmpeg 還活著 3 秒。
那 3 秒內按「建置」會成功、而且會撞到那個還沒刪掉的半成品，該輪直接以
`部署：置換 0 個，跳過 1 個` ＋ `rc=3` 收場。

修法：`stopping` 這個旗標一直保留到整棵樹確認收工、半成品清完才放掉；
這段期間按「開始」會被擋下並提示「上一個工作還在收尾」。前端也同步：
有工作在跑時整排「開始」按鈕變灰、只留「停止建置」可按，反過來也一樣。

### webui 重啟要接手還在跑的建置

【實測】重啟控制台後，先前它拉起的建置**還在跑**（子行程不會跟著父行程死），
但控制台的新行程狀態是空的，會顯示「已完成／待機」並允許再按一次建置 —— 兩個建置撞在同一個暫存目錄。
現在啟動時會掃 `mode_build.py`（只認自己這份安裝的命令列）接手狀態，並派一個 watcher 等它結束。

【實測】接手後按「停止建置」可以停掉它；同一台機器上另一份安裝的建置不會被誤認、也不會被誤殺。

### 順帶挖出「跑到一半突然中斷」的根因：launchd 是殺一整個 process group

【實測】重啟控制台（`launchctl kickstart -k gui/501/com.loopcastr.webui`）時，**先前由控制台拉起的建置會一起死**。
因為 `Popen` 預設繼承父行程的 process group，建置跟 webui 同組（實測 news 建置的 pgid ＝ webui 的 pid），
launchd 收掉那個 job 就整組一起收。這正是先前「跑到一半，突然中斷重來」的原因。

更麻煩的是**孫行程反而逃掉**：launchd 收掉的是它追蹤的那幾個行程，
所以 `mode_build`／`build_local_content` 死了、`ffmpeg` 變成孤兒（ppid 變 1）繼續把那個檔案寫完。
【實測】日誌停在 `21:08:19`（那支影片的倒數長條／跑馬燈都算完了），
但檔案一直到 `21:15` 才寫完 moov —— 也就是說「日誌沒動」不等於「沒有東西在跑」。

修法就是上面那個 `start_new_session=True`。【實測】改完之後：

| 檢查點 | 結果 |
|---|---|
| 建置的 pgid | ＝自己的 pid（不再跟 webui 同組） |
| `kickstart -k` 重啟控制台後 | 建置**還活著**，ppid 變成 1 |
| 新控制台 | 接手顯示「執行中（webui 重啟前啟動的）」，結束時由 watcher 收回狀態 |

## 控制台標題與「看直播畫面」按鈕（2026-09-21）

- 標題改成 `loopcastr 控制台`，專案名連到 GitHub 專案頁，**另開分頁**
  （`target="_blank" rel="noopener"`）。瀏覽器分頁標題與啟動時印的橫幅也一起改。
- 標題下面新增「▶ 看直播畫面」，開 MediaMTX 的 HLS 頁。

HLS 的位址不寫死，讀 `mediamtx.yml` 的 `hls` 與 `hlsAddress`：

| `mediamtx.yml` | 控制台顯示 |
|---|---|
| `hls: yes` ＋ `hlsAddress: 127.0.0.1:8888`（部署端） | 按鈕連到 `http://127.0.0.1:8888/live/main/` |
| `hls: no`（repo 預設值） | **不顯示按鈕**，改一行灰字說明「hls 是 no」 |
| 找不到 `mediamtx.yml` | 同上，說明改成「找不到 mediamtx.yml」 |

【實測】部署端 `/api/status` 回 `{'enabled': True, 'port': 8888, 'path': 'live/main'}`，
頁面渲染出 `<a class="btn live" href="http://127.0.0.1:8888/live/main/" target="_blank" rel="noopener">`；
`/live/main/` 回 HTTP 200、`/live/main/index.m3u8` 回 302（MediaMTX 的轉址，正常）。
只回傳埠號與路徑、主機名稱由瀏覽器填，所以從別台機器開控制台也連得到。

## 「金鑰貼好了、HLS 也看得到，YouTube 卻沒畫面」＝推流服務沒被載入（2026-09-21）

【實測】症狀：控制台的「看直播畫面」有內容（＝MediaMTX 有收到播出端的流），
stream.key 也存好了，但 YouTube 的直播是黑的。原因不是設定，是**推流服務根本沒被載入**：

    $ launchctl list | grep loopcastr
    58562	0	com.loopcastr.playout
    79055	-15	com.loopcastr.webui
    53634	-15	com.loopcastr.mediamtx        # 沒有 com.loopcastr.publish

`~/loopcastr/com.loopcastr.publish.plist` 一直在，只是沒有複製到 `~/Library/LaunchAgents`，
所以 launchd 沒有這個 job，YouTube 端當然不會有人連上去（`lsof` 也看不到往 1935 的連線）。

修法（一行）：

    cp ~/loopcastr/com.loopcastr.publish.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.loopcastr.publish.plist

【實測】載入後：

| 檢查點 | 結果 |
|---|---|
| `launchctl list` | `70064  0  com.loopcastr.publish` |
| `lsof` 對外連線 | `192.168.31.39:55762 -> 108.177.125.134:1935 (ESTABLISHED)`（YouTube ingest） |
| MediaMTX `live/main` | readers 從 0 變 1，型別 `rtmpConn`（就是推流端自己） |
| `publish.log` | 只有 1 次「第 1 次連線」，4 分鐘沒有重連 |

### 控制台補上「服務有沒有被載入」

先前控制台的「服務行程」只會說 publish「沒有在跑」，看不出它**從來沒被載入過** ——
這兩件事的處理方式完全不同，所以服務區塊改成三態：

| 顯示 | 意思 | 按鈕 |
|---|---|---|
| 執行中 pid N | 有載入、有行程 | 重啟 |
| 已載入（沒在跑） | 有載入但行程不在（會被 KeepAlive 拉起來） | 重啟 |
| 沒有載入 | launchd 沒這個 job | **啟動**（從資料目錄複製 plist 到 `~/Library/LaunchAgents` 再 bootstrap） |

【實測】用一個拋棄式服務 `com.loopcastr.svctest`（`/bin/sleep 600`）驗證「啟動」：

    {"ok": true, "how": "launchctl bootstrap gui/501 /Users/yangqingyuan/Library/LaunchAgents/com.loopcastr.svctest.plist"}
    launchctl list  → 71398  0  com.loopcastr.svctest

再按一次會走 kickstart（bootstrap 對已載入的 job 會失敗，不是錯誤）、
資料目錄沒有 plist 時回「無法安裝」、名稱帶 `../` 會被擋（400）。測完已 bootout 並刪除。

## 操作手冊與程式對帳（2026-09-21）

`docs/manual.md` 累積了不少跟程式對不上的敘述，趁這次一起校正：

| 手冊原本寫的 | 實際上 |
|---|---|
| 內容在 `media/<id>.mp4` | `media/<模式>/<id>.mp4`（每個模式一份，見 2026-09-19 的改動） |
| 過場是共用的 `media/_transition.mp4` | 每集一份 `media/<模式>/_tr_<id>.mp4`（多趟時 `_tr_<id>_pN.mp4`）；共用檔只剩退回用 |
| 每集過場叫 `media/_transition-01.mp4` … | 用影片 ID 命名，不是序號 |
| 過場右下角是「頻道 QR」 | 沒有頻道 QR；QR 指向**該集原片**，說明文字「去追劇」，位置與集數一致（右上角） |
| 過場 QR 說明文字有三種互相矛盾的说法 | 只留一種：集數「看原片」、過場「去追劇」 |
| 改 QR 要從 `media/_transition-clean.mp4` 重疊 | 改參數重跑 `build_transitions.py`，不要改已經疊過的檔 |
| shorts 池「50 支裡有 20 支輪不到」 | 已經有 `--passes` 輪替（上限 5 趟），並補上實際配法與上限 |
| 服務開關寫死 gui domain，另一段又寫死 system | 分成「看你是哪一種安裝」兩種，並說明控制台只管 gui domain |
| `+### 本機控制台` | 修掉那個多出來的字元（heading 壞掉） |
| 單輪 14,487 秒／15,549 秒等舊數字 | 拿掉寫死的數字，改成看 `playlist-*.json` |
| 故障排除沒有一列講「服務沒被載入」 | 補上（就是上面那個 publish 的坑） |

## 首播時間到分、只播 N 小時內的影片、雙語字樣（2026-09-21）

### 1. 首播日期改成 `YYYY/MM/DD HH:MM`

`build_playlist.py` 原本只抓 `upload_date`／`release_date`（都是 `YYYYMMDD`，**沒有時間**），
所以畫面上只有日期。改用 `release_timestamp`（首播那一刻的 unix 秒）再格式化成本地時間：

    $ yt-dlp --skip-download --print "%(release_timestamp)s|%(timestamp)s|%(upload_date)s" ...
    1782388806|1782388806|20260625        # 有時間

【實測】`--limit 4` 抓 TPP 頻道：

    3s3GiSWmcnE  2026/09/18 19:00
    L-zviGr6mFs  2026/09/12 19:00
    FJy0b34m51s  2026/09/04 21:00
    -HBsA990jdg  2026/08/29 19:00

沒有 `release_timestamp` 時退回 `timestamp`，再沒有就退回日期 ＋ `00:00`。
清單多了 `air_ts`（同一個時刻的 unix 秒），給下面的年齡過濾用。

### 2. `max_age_hours`：只播 N 小時內首播的影片

`modes.json` 每個模式可以設 `max_age_hours`（0＝不限），過濾在 `build_playlist.py`
取完 metadata 之後做（順序：先用 `video_limit` 取前 N 支，再過濾年齡）。
沒有時間戳的影片一律視為太舊（無法證明它新）。

【實測】同一份清單（最新一支是 71.7 小時前）：

| `--max-age-hours` | 結果 |
|---|---|
| 72 | keep 1, drop 3 → 寫出 1 支 |
| 24 | keep 0 → **拒寫**、exit 2，原本的清單不動 |

會拒絕寫出空清單是刻意的：播出端拿到空的 concat 清單會直接中止，寧可這輪不換。

### 3. 語言：`ui.lang` ＝ `zh`／`en`（切換，不是同時顯示）

畫面上的字樣（首播日期前綴、QR 按鈕說明、贊助、倒數）在 `settings.json` 的
`overlay` 裡各有一組 `*_en` 對應值，**一次只顯示一種語言**：

| 元素 | zh | en |
|---|---|---|
| 首播 | 首播日期： | First aired: |
| 集數 QR | ▶ 看原片 | ▶ Watch original |
| 過場 QR | 去追劇 | Watch more |
| 贊助 QR | 贊助 | Support |
| 倒數 | 剩餘 02:57 | 02:57 left |

【實測】用 45 秒的測試片段重編、抽格看畫面：`zh` 是
`土城十講｜第二十三講 用善良戰勝惡意　首播日期：2026/06/25 19:00` ＋
`01/01　剩餘 00:25`；`en` 是 `First aired: 2026/06/25 19:00` ＋ `01/01　00:25 left`。

### 4. 程式訊息改成英文

`build_playlist.py`、`build_local_content.py`、`mode_build.py`、`build_transitions.py`、
`gapwatch.py`、`loopwatch.py`、`refreshwatch.py`、`healthcheck.py`、`playout.sh`、
`yt_publish.sh`、`switch_edition.sh` 的 log／錯誤／`--help` 都改成英文。

其中一個**有耦合**的地方：`playout.sh` 的「第 N 次啟動」是四個程式在解析的日誌格式
（`loopwatch.py`、`healthcheck.py`、`refreshwatch.py`、`webui.py`）。改成 `start #N` 之後，
四個解析器同時改成**新舊格式都認**，否則正在跑的那一輪會算不出循環點：

    (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:start #(\d+)|第 (\d+) 次啟動)

### 5. 後台介面可切換語系（`ui.lang`）

控制台右上角有「中文／English」兩顆，按下去就寫進 `settings.json` 的 `ui.lang` 並重載頁面。
**一次只顯示一種語言**（第一版做成了「兩種一起顯示」，但那不是要的）。

實作是**執行時整份字串替換**：原始碼一律寫中文，回給瀏覽器之前才換（HTML、JS 字面值、
API 回的 JSON 都一樣）。這樣不必在頁面裡散佈一百多個佔位符，翻譯表也只有一處（`UI_TEXT`）。

三個實作細節，都是踩過才定下來的：

1. **一次掃描、最長優先。** 逐條 `str.replace` 的話短字串會先咬到長字串 ——
   實測「`%s（%s 秒後）`」被「` 秒`」先換掉一半。改成一個「長｜短」的 regex 一次掃過去。
2. **只翻訊息欄位，不動使用者資料。** API 的 JSON 只翻 `short／why／error／hint／note／detail／how`
   這幾個鍵（`localize_obj`），模式名稱、影片標題、路徑原樣保留；設定表單的 schema
   另用 `localize_schema`，而且**不翻預設值** —— 那些預設值是要寫進 `settings.json` 的真字串
   （例如 `date_label` 的「首播日期：」），翻掉會讓存檔把中文標籤換成英文。
3. **切換鈕本身不能被翻譯。** 它是由 JS 產生（不是伺服器端的字串），所以「中文」那顆
   在英文模式下仍然是「中文」—— 否則英文使用者就找不到切回中文的路。

【實測】`en` 模式下抓整頁（HTML 文字節點 ＋ 單雙引號的 JS 字面值），剩下的中文只有一條：
切換鈕的「中文」（刻意保留）。順帶抓到一個漏翻：`▶ 看直播畫面` 這顆按鈕 ——
第一次的檢查只掃雙引號字面值，而它是單引號，所以漏掉了。

【實測】`POST /api/lang {"lang":"en"}` → `<title>` 變 `loopcastr console`、
`settings.json` 的 `ui.lang` 變 `en`、舊檔留成 `.bak`；送 `both` 會被擋（`語系只能是 zh 或 en`）。

### 6. 其餘腳本的訊息也英文化

`make_concat_list.py`、`make_qr_png.py`、`yt_side_monitor.py`、`build_test_edition.py`、
`relay.py`（聯播接力引擎，38 處訊息）都改完了。

**還沒做的是「註解與 docstring」**：全 repo 還有約兩千行中文註解。那些不是程式執行時會
吐出來的訊息，所以先留著；要給外國人看原始碼的話這是下一個該做的工。

## 改名 loopcastr（2026-09-21）

專案名稱由 `ytpl2ytstream` 改成 **`loopcastr`**（GitHub repo 也改了：
`kingwap99/ytpl2ytstream` → [`kingwap99/loopcastr`](https://github.com/kingwap99/loopcastr)）。

### 換掉的東西

| 舊 | 新 | 影響 |
|---|---|---|
| repo 名 `ytpl2ytstream` | `loopcastr` | GitHub 會自動轉址舊網址 |
| 安裝目錄 `~/ytpl` | `~/loopcastr` | `install.sh` 的預設前綴 |
| launchd label `com.ytpl.*` | `com.loopcastr.*` | 5 個 plist 檔名與內容 |
| `/etc/sudoers.d/ytpl-webui` | `loopcastr-webui` | 對外開放時的 sudoers 檔名 |
| 控制台標題 `ytpl2ytstream 控制台` | `loopcastr 控制台` | `webui.py` 的 `PROJECT`／`REPO_URL` |

一共 19 個檔案、5 個 plist 改名，repo 裡已經沒有 `ytpl` 這個字串。

### 但**舊安裝要能用**：label 前綴改成「看現場」

這是最容易踩的地方：如果把 label 寫死成 `com.loopcastr.*`，那台還在跑舊 label 的機器
（`com.ytpl.*`）一更新程式，控制台就會把三個在跑的服務全顯示成「沒有載入」，
按「重啟」也找不到東西。

所以 `webui.py`／`healthcheck.py`／`refreshwatch.py` 都改成**掃這個目錄裡實際的 plist**
來決定前綴（`service_prefix()`），找不到才退回 `com.loopcastr.`；
`refreshwatch.py` 連 plist 的位置也兩種安裝都找（LaunchDaemon／LaunchAgent）。

【實測】部署端（`~/ytpl`，服務還是 `com.ytpl.*`）更新程式後：

    services: [('com.ytpl.mediamtx','跑'), ('com.ytpl.playout','跑'),
               ('com.ytpl.publish','跑'), ('com.ytpl.health','停'), ('com.ytpl.refresh','停')]

控制台顯示 `mediamtx 執行中 pid 53634／重啟`、`publish 執行中 pid 70064／重啟` ——
改名後程式照樣認得舊安裝。前端原本用 `label.replace("com.ytpl.", "")` 去掉前綴，
也改成認任何前綴（`/^com\.[a-z0-9]+\./`），否則服務名稱會變成整串 label。

### 品牌資產

Logo／Icon 是從品牌提案裡還原出來的向量圖（`assets/`）：

| 檔案 | 用途 |
|---|---|
| `assets/icon.svg` | 透明底 icon（淺色背景用） |
| `assets/icon-dark.svg` | 深海軍藍圓角底（GitHub／App 圖示） |
| `assets/logo-dark.svg` | 主視覺（深色底） |
| `assets/logo-light.svg` | 主視覺（淺色底） |

配色：Loop Teal `#40E0D0`、Cast Violet `#7567FF`、Midnight Navy `#0B1020`（卡片 `#151D35`）；
標語 `ALWAYS ON. ALWAYS PLAYING.`。控制台把 icon 內嵌成 favicon 與標題前的小圖。

### 改名順手抓到的兩個 bug

改名讓「寫死 label」的地方全部現形。除了上面三個程式，還有兩處：

1. **`loaded_edition()`**（控制台判斷「播出端載入的是哪一版」）也是寫死 label 讀 launchctl。
   部署端更新程式之後，它找不到 `com.loopcastr.playout`，於是**誤報**
   「news 已轉好，但播出端還在播另一版」—— 但實際上播出端好好地在播 `concat-news.txt`。
   改成用 `service_prefix()` 之後：`playing: news`、`news: ok 可以開始直播`。
2. **`switch_edition.sh`** 的 `PLIST`／`INSTALLED`／`LABEL` 也是寫死的，
   改成掃同目錄的 `com.*.playout.plist` 決定前綴。

另外修掉一個**本來就存在**的顯示錯誤：控制台的「單輪長度」永遠讀 `playlist-local.json`，
所以播 news 模式時會顯示預設版（測試版 3 段）的長度。
改成讀「播出端實際載入的那一份」：

【實測】部署端（播 news）：單輪 **220 段、94768 秒（26.3 小時）**，
下次循環 2026-09-22 19:52:14；修改前顯示的是 3 段／540 秒。

## 測試機搬到 .22，.39 退場（2026-09-22）

以後測試都在 `192.168.31.22`（MacBook-Air-FS.local，macOS 26.6.2）；
`.39`（原本的開發機）整套停掉不用。

### .22 的環境與安裝

| 項目 | 狀態 |
|---|---|
| brew／ffmpeg／ffprobe／python3 3.14.3／node | 本來就有 |
| yt-dlp | **用 pip 裝**（`pip install --break-system-packages yt-dlp`） |
| mediamtx v1.21.1 | 官方 darwin_arm64 單檔，放 `/opt/homebrew/bin/` |
| Pillow 12.3.0／qrcode | `pip install --user --break-system-packages` |
| 安裝方式 | `./install.sh --prefix ~/loopcastr --agents --no-services` |

【實測】**brew 完全不能用**：`brew install` 直接回「You have not agreed to the Xcode
license」。接受授權是系統層變更，所以改走「不碰授權」的路 —— yt-dlp 走 pip、
mediamtx 抓官方單檔。要改用 brew 管理就得先 `sudo xcodebuild -license accept`。

【實測】GitHub release 對那台很慢（約 100 KB/s，25.6 MB 抓了 4 分鐘），而且第一次
抓 yt-dlp 時卡在 11.9 MB 不動 —— 所以 mediamtx 的下載改成「`-C -` 續傳 ＋ 重試 6 次」。

### 控制台遠端模式的 bug（修掉才能用）

`?token=` 只擋得住第一次載入：頁面載進來之後，**每個 `/api/*` 都沒有帶 token**，
所以遠端開控制台會整頁 401。修法：頁面記住 `location.search` 的 token
（`var TOKEN`），所有 GET 走新的 `getJSON()`、POST 走 `post()`，兩者都掛
`X-Ytpl-Token`。

【實測】`--host 0.0.0.0 --token-file webui-token`：curl 不帶 token 回 401、
帶 `X-Ytpl-Token` 回 200、瀏覽器開 `?token=…` 時狀態列與服務區塊都正常（API 有帶到 token）。

### install.sh 補兩個洞

1. **服務清單漏了 `webui`**：`for s in mediamtx playout publish health refresh`
   是寫死的，所以控制台從來沒被 install.sh 裝過（.39 那顆是手動加的）。
   現在多了 `launchd/com.loopcastr.webui.plist` 模板，清單也補上 webui。
2. **PATH**：從 ssh／腳本呼叫時 PATH 沒有 brew，依賴檢查會誤報「缺少 ffmpeg／yt-dlp／mediamtx」。
   改成腳本開頭自己補 `/opt/homebrew/bin`。

### .22 的現況與 .39 的退場

【實測】.22 控制台：`services: mediamtx(跑) playout(停) publish(停) health(停) refresh(停)`、
`開播檢查: news 還沒建置內容／promotion 來源還是範例值／test 還沒建置內容`。

mediamtx 與 webui 已用 LaunchAgent 起著；**playout／publish／health／refresh 先不啟動** ——
install.sh 的「還沒有播出內容就先不裝」保護本來就會擋，而且那四個要等內容建好才有意義。
它們的 plist 已經在 `~/loopcastr/`，所以控制台服務區塊會顯示「沒有載入 ＋ 啟動」，
第一次建置完成後直接在那裡按「啟動」即可（gui domain，不需要 sudo）。

.39 的處理：`launchctl bootout` 四個服務（先停 publish，YouTube 端才是有序結束），
再把 plist 移到 `~/ytpl/launchagents-disabled/`，這樣重開機登入也不會自己回來。
**資料 28 GB 原封不動留在 `~/ytpl`**，要恢復就是把 plist 搬回去再 bootstrap。

### 順手修掉第一次建置的死路

全新安裝時播出端還沒被載入，`switch_edition.sh` 原本遇到「兩個 domain 都找不到服務」
只會印一行警告就結束（exit 6）—— 也就是說第一次按「建置並切換（開始直播）」
**建好內容卻永遠不會開播**，得自己先去 `./install.sh` 或手動 bootstrap。
現在那個分支會直接把剛寫好的 plist 載起來（gui 或 system 都支援），
所以「建置並切換」在全新機器上可以一次到位。

### 事後補充：.22 的 brew 是別人的（2026-09-22）

接受 Xcode 授權之後 `brew --version`／`brew info` 都通了，但 `brew install` 仍然失敗：

    Error: /opt/homebrew is not writable.
    sudo chown -R yangqingyuan /opt/homebrew ...

【實測】`ls -ld /opt/homebrew` → **擁有者是 `neoyang`**（那台機器另一個帳號，
brew 是他裝的）。`/opt/homebrew/bin` 可以寫入，所以手動放執行檔沒問題，
但 `brew install` 需要整個 prefix 的寫入權。

**沒有動那個 chown**：那會把 brew 從另一個帳號手上拿走（neoyang 那邊跑著 go2rtc wall），
屬於影響別人的系統變更。目前的做法是：

| 工具 | 來源 | 升級方式 |
|---|---|---|
| yt-dlp | pip（`~/Library/Python/3.14/bin/yt-dlp`），`/opt/homebrew/bin/yt-dlp` 是指向它的 symlink | `python3 -m pip install -U --break-system-packages yt-dlp` |
| mediamtx v1.21.1 | 官方 darwin_arm64 單檔 | 重新下載覆蓋 |
| Pillow／qrcode | pip --user | `python3 -m pip install -U --user --break-system-packages pillow qrcode` |

用 symlink 而不是複製，是為了讓 pip 升級直接生效（複製的話 `/opt/homebrew/bin` 那份會變舊）。
要改成 brew 管理，就得跑上面那個 `chown`（等於把 brew 收給 `yangqingyuan`）。

#### 後續：brew 處理好了，三個套件改回 brew 管理

使用者把 `/opt/homebrew` 收給 `yangqingyuan`（`brew 7.0.6`，可寫）。之後：

    brew install yt-dlp mediamtx pillow     # INSTALL_RC=1：link 步驟被既有檔案擋住
    brew link --overwrite yt-dlp mediamtx   # LINK_RC=0
    python3 -m pip uninstall -y --break-system-packages yt-dlp pillow

【實測】最終來源：

| 工具 | 來源 |
|---|---|
| yt-dlp | `/opt/homebrew/Cellar/yt-dlp/2026.8.19_1/bin/yt-dlp` |
| mediamtx | `/opt/homebrew/Cellar/mediamtx/1.21.1/bin/mediamtx`（重啟後確認跑在這份） |
| Pillow | `/opt/homebrew/lib/python3.14/site-packages/PIL` |
| qrcode | 仍是 pip --user（**brew 沒有這個 formula**，`brew info qrcode` 會建議 qrencode） |

**教訓（踩過）**：第一次是先刪掉 `/opt/homebrew/bin` 裡的檔案才跑 `brew install`，
結果 install 失敗 → 那兩個指令一度消失（mediamtx 還靠已刪除的 inode 在跑，重啟就會掛）。
正確順序是「**先 install（Cellar 裝好就好）、再 `brew link --overwrite`**」——
link 之前原本的檔案都還在，install 失敗也不會斷。

## .22 端到端實測：臣心報報（2026-09-22）

用 `test` 模式（臣心報報清單 `PL1DCTrWM6hndbHwmlB6FhqzIayctuJNRb`、
3 支 × 180 秒、shorts 用 `@chiu_chenyuan/shorts` 6 支）在 `.22` 跑完整條鏈。

### 結果：整條通

    02:45:48  mode test: 3 支、limit 180 s、6 shorts
    02:47:45  deploy: moved 3, skipped 0
    02:47:45  concat-test.txt ready: 6 segments, 1080s total
    02:48:14  switched to test（播出端自動載入）

控制台（`/api/status`）：

    MediaMTX: ready=True readers=1 bytes=2641245 tracks=['H264', 'MPEG-4 Audio']
    playing: test | concat 段數: 6 | media: 423.5 MB
    開播檢查: news 還沒建置內容／promotion 來源還是範例值／test 可以開始直播

【實測】從**另一台機器**拉 `.22` 的 HLS（`hlsAddress: :8888`）抽一格畫面：
1280x720、標題列 `…小事　首播日期：2023/07/29 18:00`（到分的新格式）、
右上 QR `看原片` ＋ `youtu.be/Ig3vtqtXowY`、倒數 `01/03　剩餘 02:54`。

### 這輪測試抓到並修掉的東西

1. **`switch_edition.sh` 的「自動載入」少了一種情況**（第一次建置就卡在這）：
   我前一輪加的分支只找「已裝進 launchd 的 plist」，但 `.22` 的狀況是
   **plist 只在安裝目錄裡**（install.sh 因為還沒有內容而跳過安裝）→ 仍然 exit 6，
   「建好內容卻沒開播」。補上第三種情況：plist 在安裝目錄時就裝成 LaunchAgent 再 bootstrap。
2. **控制台「服務行程」的 `playout ffmpeg` 永遠顯示 0**：pattern 寫的是 `concat.txt`，
   只對得上預設清單，播 test／news 時就對不上。改成 `stream_loop`（播出端一定帶這個參數）。
3. **控制台「內容」的 concat 段數永遠顯示 0**：讀的是預設 `concat.txt`。
   改成讀「播出端實際載入的那一份」（跟前面單輪長度的修法一致）。

### 環境陷阱：brew 升級 Python 之後要重啟 python 服務

【實測】控制台一度每個 API 都回 `500 內部錯誤：No module named '_strptime'`。
原因不是程式：`brew` 把 `python@3.14` 從 **3.14.3 升到 3.14.7**，
而控制台是在升級**之前**啟動的 —— 行程還在跑舊的直譯器，但它的 stdlib 目錄已經被刪掉，
所以延遲載入的 `_strptime` 找不到。重啟服務即恢復（`ps -o lstart` 可以看到行程時間早於升級）。

教訓：**brew 升級 python 之後，所有用 python 跑的服務都要重啟**（控制台、health、refresh）。

### .22 的 HLS 打開了

為了能在控制台按「看直播畫面」直接看測試結果，`.22` 的 `mediamtx.yml` 改成
`hls: yes` ＋ `hlsAddress: :8888`（綁所有介面，區網可看）。這是**每台部署的選擇**，
repo 的範例值仍然是 `hls: no`（多線產能時每條路徑約 +1.1% CPU）。

### 迴歸：控制台的設定表單整片不見（2026-09-22，使用者發現）

【實測】症狀：控制台只剩狀態列與服務區塊，**「① 來源設定」的三張模式卡片、
模式下拉、畫質表單全部空白** —— 也就是使用者唯一能自己改設定的入口整個消失。

根因是上一輪「遠端模式修 token」那批改動：把 GET 換成 `getJSON()` 時，
`loadCfg()` 的鏈變成

    getJSON("/api/schema").then(... return getJSON("/api/config"); )
      .then(function(r){ return r.json(); })   // ← getJSON 已經 parse 過了

`r` 是物件、`r.json` 不存在 → TypeError → 後面整串（`renderSettings`、
`renderModes`、下拉選項、`refresh()`）全部沒跑。修法是拿掉那個多餘的 `.then`。

**教訓**：那批改動我只驗了「狀態列與服務區塊有沒有出來」（因為修的是 token），
沒有驗**設定表單**——而表單正是使用者唯一能改來源與畫質的地方。
驗證要涵蓋「使用者真的會走的那條路」，不是只驗自己剛改的那一段。

【實測】修好後：

    模式卡片 3 張、每張 10 個輸入欄位（含新增的 max_age_hours）
    模式下拉 3 個選項（news／promotion／test）
    畫質表單（語言 ui 區塊）有內容、原始 JSON textarea 有內容
    JS 錯誤 0
    儲存路徑：POST /api/config 改 test.shorts_seconds 90 → 91 → 還原 90，
              每次都回 ok、wrote modes.json、舊版留 .bak

## 控制台全面回歸測試（2026-09-22，.22）

使用者要求「都測過一遍」。這次不只掃 API，也用**真實瀏覽器把每一顆按鈕點過**。

### API 層（18 項）

| 項目 | 結果 |
|---|---|
| 不帶 token → 401／帶 token → 200 | OK |
| POST 缺 `X-Ytpl` → 400（CSRF） | OK |
| `/api/status` 欄位齊全（now/prefix/lang/proc/mtx/round/content/ready/preview/services/logs） | OK |
| `/api/config`（modes＋settings）、`/api/schema`、`/api/task` | OK |
| `/api/probe` 實際解析（臣心報報清單＋ shorts 都 OK） | OK |
| `/api/lang` zh／en 切換、非法值擋掉 | OK |
| `/api/config` 寫入 modes → 生效 → 還原（留 .bak） | OK |

### UI 層（用瀏覽器真的點）

| 動作 | 結果 |
|---|---|
| 語言切換 中文 ↔ English | 標題、區塊、狀態列都跟著換 |
| 「驗證網址」（test 卡片） | `頻道／清單：OK 第一支 Ig3vtqtXowY（臣心報報#52）｜shorts：OK` |
| 「只掃描來源」＋「停止建置」 | 掃 news 55 支時按停止 → `sent SIGTERM to pid …×3`、按鈕狀態正確切換 |
| 「只建置」 | 跑完整條鏈：落地 → 過場 → 部署 → concat → `6 segments, 1080s` |
| 「儲存這個模式」 | 改 `shorts_seconds` 90→91→90，兩次都回 ok |
| 「儲存原始 JSON」 | 回 `wrote modes.json`（也證明 textarea 有被填內容） |
| 服務「重啟」（playout） | pid 由 50106 → 53690 |
| 服務「啟動」（refresh） | `bootstrap gui/501 …`、狀態由 not loaded → running |
| 直播金鑰「寫入」 | 合法值寫入成功；`bad key with spaces!` 被擋（格式檢查） |
| 「▶ 看直播畫面」 | 新分頁開 `192.168.31.22:8888/live/main/` |

### 這輪又修掉兩個

1. **`loadCfg()` 多一次 `.then(r => r.json())`**（見上一節）：整個設定介面消失。
2. **「重建 concat 清單」「檢查缺哪些檔案」寫死正式版**：它們固定用
   `playlist-local.json`／`playlist.json`，所以在只建過 `test` 的機器上直接爆
   `FileNotFoundError` 並把 Python traceback 倒進進度框。改成用**目前選的模式**
   （`playlist-<mode>-local.json` → `concat-<mode>.txt`），而且檔案不存在時回
   一句清楚的話（`playlist-news-local.json 不存在；這個模式要先建置過`）而不是 traceback。

### 一個還沒動的發現：每次建置都重新編碼全部影片

【實測】只建置 test 時，log 出現 `list has 3 segments, 0 present, 3 missing` ——
明明 `media/test/` 裡三支都在，還是全部重編。原因是落地一律寫到
`/tmp/stage-ep-<mode>/`（暫存 → 驗證 → mv 的原子置換設計），而「已完成就跳過」是看
**暫存目錄**有沒有那個檔案；部署後暫存目錄被搬空，所以下一輪又是 0 present。

影響：`test`（3 支 × 180 秒）大約 1 分鐘，可接受；**`news`（55 支全長）每次重建都要好幾小時**。

要修的話方向是「記下每支的編碼參數指紋（畫質／浮水印／長度上限…），沒變就跳過，
`--force` 才全部重做」—— 這會改變建置語意（目前「存檔後重新建置就會套用新設定」是
靠每次重編在保證的），所以先提出來等決定，沒有直接動。
