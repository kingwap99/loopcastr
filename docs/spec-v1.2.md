# 24/7 YouTube 直播頻道系統規格書 v1.1

> 本文件為繁體中文。英文說明見 repo 根目錄 [README.md](../README.md) 的 English 一節。
> This document is in Traditional Chinese; see the English section of [README.md](../README.md).

## 0. 文件資訊

| 項目 | 內容 |
|---|---|
| 文件名稱 | 24/7 YouTube 直播頻道系統規格書 |
| 版本 | v1.2（動工中；取代 v1.0、v1.1） |
| 日期 | 2026-09-16 |
| 狀態 | **動工中**：M1／M2／M3 已實測通過並部署至目標機；**M4 進行中**（53 支內容落地執行中） |
| 依據文件 | v1.0、v1.1，加上本輪（2026-09-16 20:00–20:15）在目標機的實測 |
| 撰寫原則 | 只寫**已實測**或**已決定**的內容；未量測者標【待驗證】，推導者標【推論】 |

v1.2 相對 v1.1 的四個實質更正：

1. **更正 R-09（最重要）**：v1.1 寫「匿名解析一律被擋、只有 cookie 能解」，這是**錯的**。同一批影片 05:11 全滅、20:02 用完全相同的條件全部成功 —— 那是**暫時性的 IP 標記**，不是永久封鎖，**不需要 cookie**。封鎖期間另有備援：`player_client=android`（上限 360p）。
2. **新增必要步驟：內容正規化**。實測原始參數不一致（1280×720／1280×718／另有一批 480p），concat 的 `-c copy` 無法直接拼。落地時必須轉成統一參數（3.6）。
3. **修掉兩個靜默錯誤**：ffmpeg 讀 stdin 導致輸出被截斷（137s／163s，無任何錯誤訊息）；HLS(m3u8) 路徑只涵蓋半支影片。
4. **M4 開跑**：53 支落地執行中。

---

## 1. 專案目標與範圍

### 1.1 目標

把同一個團體（同一創作者）已授權的 YouTube 作品，組成一個**24/7 不中斷**的直播頻道，並具備把外部正在進行的直播**聯播**進來的能力。

### 1.2 範圍內

1. 從 YouTube 取得來源媒體。經 R-09 之後，實務上是**先落地成本機檔案**再播出。
2. 依播放清單順序連續播出，全天候不中斷。
3. 段與段之間、以及一輪與下一輪之間，不得讓接收端中斷。
4. 來源失效、卡住、需重新解析時自動恢復。
5. 輸出推送到 YouTube 直播 ingest。
6. 聯播：把一條正在直播的 YouTube 訊號拉進來再推出去。

### 1.3 範圍外（已明確排除，不再重複評估）

| 排除項目 | 原因 | 出處 |
|---|---|---|
| SPX-GC | 使用者已指示放棄 | 條件更新 |
| CasparCG | 不支援 Apple Silicon | 條件更新 |
| OBS 插件（現成或自製） | 使用者已指示自行實作 | v3 結論 |
| Liquidsoap 作為**決策層** | 三關全過但拿不到額外能力，且工作機要升 10 個相依 | v4.5 |
| 自行架設轉碼農場 | 目標機為 8 GB M1 單機 | — |

---

## 2. 名詞定義

| 名詞 | 定義 |
|---|---|
| **來源（source）** | 一段要被播出的媒體；在聯播模式下是 yt-dlp 解析出來的直連 URL |
| **段（segment）** | 播放清單中的一個項目，型別為 `vod` / `live` / `filler` / `file` |
| **解析（resolve）** | 用 yt-dlp 把 videoId／watch URL 轉成可直連的媒體 URL |
| **接力（relay）** | 每段由**各自的** ffmpeg 發佈程序依序接手輸出 |
| **拼接（concat）** | **同一個** ffmpeg 依序讀完整份清單，中間不換發佈程序 |
| **落地／離線化（landing）** | 把來源預先抓成本機檔案 `media/<id>.mp4`，播出時完全不碰網路來源 |
| **換手（takeover）** | 新發佈程序先接手，舊的才被收掉；MediaMTX 的 takeover 是同秒瞬間 |
| **墊片（filler）** | 來源不可用時頂上的備援片段 |
| **推流鏈（publisher chain）** | `playout.sh → MediaMTX → yt_publish.sh → YouTube` 這條固定三跳 |
| **工作機** | 使用者日常 MacBook（`<WORK_HOST>`） |
| **目標機** | 目標機（hostname `<HOSTNAME>.local`，<MODEL>，M1，8 GB，macOS 27.0） |

---

## 3. 系統架構

### 3.1 元件

| 元件 | 實作 | 角色 |
|---|---|---|
| 母清單 | `playlist.json` | 描述要播什麼、順序、每段來源與長度（來源為 YouTube URL） |
| 內容落地 | `build_local_content.py` | 把母清單抓成本機 `media/<id>.mp4`，ffprobe 量長度，產生 `playlist-local.json` |
| 清單轉換 | `make_concat_list.py` | `playlist-local.json` → ffmpeg `concat.txt` |
| **播出端** | `playout.sh` | 單一行程 concat 循環播出 → MediaMTX |
| **推流端** | `yt_publish.sh` | 單一長命 ffmpeg，MediaMTX → YouTube ingest |
| 接力引擎（聯播） | `relay.py` | 解析來源、排程、takeover 換手、看門狗、URL 生命週期、事件記錄 |
| 解析器 | `yt-dlp` | watch URL → 直連媒體 URL（**目前被 R-09 擋住**） |
| 本地媒體樞紐 | `MediaMTX` | 接收 RTMP、提供 takeover 讓換手零斷點、提供 HLS 驗收、提供 HTTP API 觀測 |
| 編碼／封裝 | `ffmpeg` | remux（`-c copy`）為主，必要時轉碼 |
| 驗收觀測 | `gapwatch.py` | 獨立輪詢 API，回報「接收端離線」與 `bytesReceived` 零成長區間 |
| 行程看管 | `launchd` | `com.ytpl.mediamtx` / `com.ytpl.playout` / `com.ytpl.publish` / `com.ytpl.health` |
| 健康監控 | `healthcheck.py` | 每 60 秒檢查「真的有在動」：路徑 ready、有讀者、`bytesReceived` 有成長。狀態變化即告警，持續異常每 30 分鐘重提醒；YouTube 端最多每 15 分鐘查一次（公開查詢太密集會再被 bot 盯上） |
| 字卡／轉場（選配） | Liquidsoap 或 HTML→PNG→overlay | 見 v1.0 的 FR-11 |

### 3.2 資料流

**主線（本機檔案，24/7 常態）**

    playlist.json（YouTube URL）
         |
         |  build_local_content.py  ← 需要 cookies.txt（R-09），離線做一次
         v
    media/<id>.mp4  +  playlist-local.json
         |
         |  make_concat_list.py
         v
    concat.txt
         |
         |  playout.sh：單一 ffmpeg
         |    -re -f concat -safe 0 -stream_loop -1 -i concat.txt -c copy
         v
    MediaMTX  rtmp://127.0.0.1:1935/live/main
         |          ├── HTTP API :9997（觀測）
         |          ├── HLS :8888（驗收）
         |          └── yt_publish.sh：單一長命 ffmpeg -c copy
         v
    YouTube ingest  rtmp://a.rtmp.youtube.com/live2/<stream key>

**支線（聯播／需要換來源時）**

    relay.py ──resolve──> yt-dlp ──> 直連 URL
         |
         | ffmpeg（copy），每段一個發佈程序，靠 takeover 交班
         v
    MediaMTX ──> yt_publish.sh ──> YouTube

【實測】兩條線可以並存：`relay.py` 與 `playout.sh` 都推同一條 `live/main`，MediaMTX 的 takeover 會讓換手同秒完成。

【實測】「推送 YouTube」**必須**由 MediaMTX 的下游發起，不能讓播出引擎直接把 target 指向 YouTube。原因：`relay.py` 的 `api_path_of()` 只認 `127.0.0.1`／`localhost` 才走 takeover；target 一改成 YouTube，換手就退化成 `cut`，而且 YouTube ingest 不接受兩條 publisher 並存。

### 3.3 部署拓撲

| 角色 | 機器 | 說明 |
|---|---|---|
| 開發／測試 | 工作機 `<WORK_HOST>` | 開發、離段測試 |
| **正式執行** | **目標機** | 24/7 長駐；播出、推流、媒體樞紐都在這一台 |
| 對外 | 兩台共用同一 NAT | 對外公網 IP 動態，本輪量得 `<PUBLIC_IP>` |

【實測】兩台同閘道（`<GATEWAY>`）、同 NAT、同公網 IP。**因此 R-09 的 bot 封鎖與「換機器試試看」無關**——換到目標機一樣被擋。

【實測】公網 IP 是動態的：同日稍早為 `<PUBLIC_IP>`，本輪為 `<PUBLIC_IP>`。落地成檔案之後，這個風險對主線**不再成立**（播出已經不依賴來源 URL）；它只影響聯播支線。

### 3.4 架構決策紀錄

| 決策 | 結論 | 理由 |
|---|---|---|
| 是否自行實作 | 是 | 使用者指示 |
| 播出引擎取代控制層 | 否 | `relay.py` 已實作並實測事件式換手 |
| 輸出樞紐是否用 MediaMTX | **必須** | takeover 只在 MediaMTX 提供；同秒瞬間完成（`closing existing publisher` 與 `stream is available and online` 同秒） |
| 是否直接推 YouTube RTMP | 否 | 見 3.2 註記 |
| **本機檔案用 concat 還是接力** | **concat** | 【實測】接力每段 2.0–3.1 秒縫；concat 在換片與繞回皆 0 縫。見 3.5 |
| **內容是否離線化** | **是** | 解析會**間歇性**被擋（R-09），且離線化同時移除 URL 6 小時過期與 googlevideo 中途 reset 兩個風險。落地時必須正規化，見 3.6 |
| **推流是否用單一長命 publisher** | **是** | 上游怎麼換手都只發生在 MediaMTX 內部，YouTube 端只看到一條連線 |
| 字卡是否需要 freetype | 否 | 兩台 ffmpeg 都沒有 freetype；走路徑 A（HTML→PNG→overlay） |

### 3.5 播出引擎選擇：concat vs 接力（本輪核心實測）

同一組「3 段本機檔案、總長 36 秒、繞圈」的實測對照：

| 播法 | 實測縫隙 | 量測視窗 |
|---|---|---|
| `relay.py` 接力 | 4 段，共 **7.793 秒**（冷啟動 3.066／換片 2.053／換片 2.040／墊片接手 0.635） | 約 40 秒 |
| `playout.sh` concat | **1 段，2.546 秒（僅冷啟動）**；換片與繞回 0 縫 | 70 秒（涵蓋兩輪） |
| `playout.sh` concat（目標機） | **0 段** | 55 秒（涵蓋兩輪） |

根因：【實測】`PUBLISH_TAIL`（原設計用來延長輸出目的地的 `-t`）**無法延長本機檔案來源**。檔案 EOF 即結束 → MediaMTX 立刻踢掉 publisher → 下一個 publisher 暖機期間接收端離線 2 秒。這是**來源端**先結束，不是輸出端，所以調任何輸出參數都救不了。

【實測】concat 的繞回確實發生：日誌出現 `Non-monotonic DTS ... 48000`、`57000`（＝36+12、36+21 秒，第二輪的 t1→t2 邊界），而該處沒有離線區間。

**已知副作用**：concat demuxer 在每個接縫會產生 `Non-monotonic DTS` 警告，實測音訊時間戳重疊約 **11 ms**，ffmpeg 自動夾正，聽感無影響。要求時間軸完全乾淨時，改用離線預接：

    ffmpeg -hide_banner -f concat -safe 0 -i concat.txt -c copy media/all-in-one.mp4

之後播出改讀單一大檔（`-stream_loop -1 -i media/all-in-one.mp4 -c copy`），live path 完全不經過 concat demuxer。

### 3.6 落地正規化（本輪新增的必要步驟）

【實測】這批內容的原始編碼參數**不一致**：

| 影片 | 解析度 |
|---|---|
| `Ig3vtqtXowY` | 1280×720 |
| `YlC65MH0xoc` | **1280×718** |
| 範例清單多數集數 | 640×360（原生 480p 級） |

concat demuxer 搭配 `-c copy` 要求所有片段參數完全相同，混用會在接縫處產生錯誤或整段失敗。因此**落地時就要轉成統一參數**，而不是留到播出端處理（播出端轉碼會讓 8 GB 的目標機全天候滿載）：

    python3 build_local_content.py --target 720
    # 輸出統一為：H.264 High L3.1 / 1280x720 / yuv420p / 30fps / AAC-LC 48kHz 立體 / +faststart

【實測】修正後抽驗 5 支（含 720p 與 718p 來源），輸出參數完全一致，`-c copy` 拼接無誤。

### 3.7 落地流程的兩個靜默陷阱（本輪修掉）

| 陷阱 | 症狀 | 根因 | 修法 |
|---|---|---|---|
| ffmpeg 讀 stdin | **同一支影片分別得到 137s 與 163s**，沒有任何錯誤訊息，exit code 0 | 腳本常透過 ssh heredoc 執行，ffmpeg 繼承的 stdin 是腳本本身；ffmpeg 把內容當互動指令，讀到 `q` 就正常結束 | 加 `-nostdin`，並在 `subprocess.run` 指定 `stdin=DEVNULL` |
| HLS 路徑只給半支 | `Ig3vtqtXowY` 只抓到 137s，DASH 路徑完整 270.374s | 同一影片同時提供 `m3u8`(HLS) 與 `https`(DASH) 兩套串流，HLS 那條不完整 | 格式限定 `[protocol^=https]` |

另外加了**下載後長度比對**：與清單宣稱長度差超過 5%（或 10 秒）即自動重抓一次，再不行就換 `android` client，避免半支影片混進 concat。

### 3.8 內容層的問題：片尾黑畫面（本輪新增）

播放鏈路的三層檢查各自看得到不同的東西，這件事值得寫清楚，因為本輪第一次遇到的問題正好躲過前兩層：

| 檢查 | 量什麼 | 看得到 | 看不到 |
|---|---|---|---|
| `gapwatch.py` | MediaMTX 的 `ready` / `bytesReceived` | 傳輸中斷 | 畫面內容 |
| PTS/封包分析 | concat 輸出的時間軸 | 時間軸破洞 | 畫面內容 |
| `blackdetect` | 實際像素亮度 | 黑畫面 | — |

【實測】`8jtdcMDuV_A` 片尾有 **67.4 秒**純黑（起點 551.57s）。播出端完全正常：傳輸連續、436,600 個封包零破洞、`framesInError=0`。但觀眾端就是一片黑。

而且它的恢復點幾乎貼齊影片結尾（551.57+67.4 ＝ 618.97，全長 618.9），所以**看起來像是換片造成的**，很容易誤判成拼接點的問題。

對策：`build_local_content.py` 內建黑尾偵測（`--black-tail-min`，預設 5 秒），落地與 `--rescan` 時都會跑。抓到就在 manifest 記下，並在產生 `playlist-local.json` 時寫入 `outpoint`，由 `make_concat_list.py` 轉成 concat demuxer 的截斷指令，**不需要重新編碼**。要只標記不截斷用 `--no-auto-trim`。

【實測】套用後該段播放長度由 618.9s 降為 551.57s，整份清單由 14,530s 降為 **14,487s**；重跑時間軸分析，封包數 436,600 → 434,583（少 2,017 幀 ≈ 67.2 秒），**forward gap 仍為 0**。

註：`make_concat_list.py` 早期把各段秒數逐一取整後相加，會累積約 24 秒誤差、報成 14,463s；已改為浮點相加，現在與實際播放長度一致。

---



## 4. 功能需求

優先級：**P0**＝正式上線必要；**P1**＝上線後補；**P2**＝選配。狀態欄更新至 2026-09-17。

| 編號 | 需求 | 優先 | 狀態 | 驗收依據 |
|---|---|---|---|---|
| FR-01 | 解析 watch URL／videoId 成直連媒體 URL，含 client 備援 | P0 | **通過** | 解析 2.26–2.65 秒；R-09 期間自動退到 `android` client |
| FR-02 | 以 `playlist.json` 描述內容，支援 `vod`/`live`/`filler`/`file` | P0 | **通過** | schema 見 6.2 |
| FR-03 | 依序連續播出，播完自動回第一段 | P0 | **通過** | 【實測】多輪循環無縫 |
| FR-04 | 換手／換片不得讓接收端中斷 | P0 | **通過（concat）** | 【實測】換片與繞回 0 縫；接力模式下每段 2.0–3.1 秒縫 |
| FR-05 | 來源停滯時以墊片先接管畫面 | P1 | **主線不需要** | 主線播出的是本機檔案，沒有「來源停滯」這種狀態；此需求只在聯播支線（`relay.py`）成立，待 T-08 一起驗 |
| FR-06 | 看門狗：輸出停滯超過門檻即重開該段 | P0 | **主線已由其他機制取代** | 主線的三層保護是：`launchd KeepAlive`（行程死掉自動拉起）＋ `healthcheck.py --heal`（流量 3 分鐘不成長就重啟播出端）＋ MediaMTX `readTimeout`。`relay.py` 的看門狗只在聯播支線需要 |
| FR-07 | 來源 URL 生命週期管理 | P0 | **主線已免除** | 落地後不再依賴來源 URL；聯播支線仍需 |
| FR-08 | 聯播模式：`type=live` 長時段來源並定時換手 | P1 | **可測（R-09 已解除）** | 匿名解析已恢復，`relay.py` 的 `type=live` 現在能真的驗；需要 D6 指定來源 |
| FR-09 | 事件記錄 JSONL ＋ API 流量觀測 | P1 | **通過** | `relay-events.jsonl`／`gapwatch.py` |
| FR-10 | 推送最終訊號至 YouTube ingest | P0 | **通過** | 見 T-14 |
| FR-11 | 字卡／台標／字幕 | P2 | **可做，但會推翻「純 copy」** | 兩台都沒有 freetype，只能走路徑 A（HTML→PNG→overlay）；而 overlay 必須**解碼再編碼**，8 GB 的目標機會全天候滿載。要做就必須先決定這個取捨 |
| FR-12 | 連續失敗告警 | P2 | **程式就緒** | `healthcheck.py` 已寫入 `alerts.jsonl`，外送管道的掛勾也在了（`~/ytpl/alert_webhook`），等 D8 給 URL |
| **FR-13** | **把來源落地成本機檔案、正規化、並驗證長度** | **P0** | **完成** | 53 支全部落地（4.7 GB），參數一致、長度驗證、黑尾偵測都通過（T-16／T-17／T-18） |
| **FR-14** | **YouTube 端只有單一長命 publisher** | **P0** | **通過** | 實測 45 秒以上無中斷 |
| **FR-15** | **播出端與推流端由 launchd 各自看管、開機自啟** | **P0** | **通過** | 已改為 LaunchDaemon；重開機後全程無人登入仍自動恢復（T-11） |
| **FR-16** | **片尾黑畫面偵測與截斷** | **P0** | **通過** | `--rescan` ＋ `outpoint`；見 3.8、T-18 |
| **FR-17** | **過場影片（每集之後）** | **P1** | **通過** | `media/_transition.mp4` 存在即自動插入；見 6.2、T-22 |

---

## 5. 非功能需求

| 編號 | 項目 | 需求 | 依據 |
|---|---|---|---|
| NFR-01 | 記憶體 | 目標機 8 GB，同時只允許一條主要編碼／複製路徑 | 【實測】目標機 8 GB |
| NFR-02 | 磁碟 | 【實測】落地一輪 **4.7 GB**（106 段、15,549 秒 @720p）；目標機目前可用 26 GiB | 【實測】2026-09-17 |
| NFR-03 | 頻寬 | 上傳需容納輸出位元率的 1.5 倍以上 | v4.3 |
| NFR-04 | 可用率 | 24/7。主線的異常恢復改由三大機制定義：行程死掉立即由 `KeepAlive` 拉起；流量停滯由 `healthcheck --heal` 在連續 3 次失敗（約 3 分鐘）後重啟；重開機自動恢復（實測中斷 25 秒） | 更新 2026-09-17 |
| NFR-05 | 解析延遲 | 單次解析 2.26–2.65 秒 | 【實測】 |
| NFR-06 | 開機自啟 | `launchd` **LaunchDaemon**：開機自動拉起（**不需要圖形登入**），結束後自動重啟 | 【實測】T-11 |
| NFR-07 | 安全 | stream key 不得進版控 | 第 9 節 |
| NFR-08 | 可觀測性 | 結構化事件日誌與流量查詢端點 | FR-09 |

---

## 6. 介面規格

### 6.1 `relay.py` 命令列介面（聯播／多來源接力）

v1.1 新增／沿用：

| 參數 | 預設 | 用途 |
|---|---|---|
| `--cookies` | 自動找 `<專案>/cookies.txt` | 來源被 bot 檢查擋住時帶上 cookies.txt |
| `--check` | 關 | 不播出，只把所有來源解析過一輪並回報可用性（全滅回傳碼 2） |
| `--loop` | `1` | 整份清單重複幾輪（`0`＝無限），輪與輪之間走同一套 takeover |
| `--playlist` / `--target` / `--clients` / `--only` | — | 清單與目標覆寫 |
| `--observe` / `--overlap` / `--resolve-lead` | `0.0`／`4.0` | 觀測與換手前置 |
| `--watchdog` / `--startup-grace` / `--flow-threshold` | `3.0`／`12.0`／`5.0` | 看門狗三參數 |
| `--max-restarts` / `--retry-interval` | `6`／`8.0` | 重試政策 |
| `--filler-on-stall` | `assets/transition.mp4` | 停滯墊片 |
| `--url-max-age` / `--url-expiry-margin` | `1800.0`／`300.0` | URL 生命週期 |
| `--restart-mode` | `auto` | `takeover`／`cut`／`auto` |
| `--dry-run` | 關 | 只印不執行 |

**建議上線值**（v4.3 實測）：

    --watchdog 10 --startup-grace 15 --flow-threshold 12 \
    --url-max-age 1800 --url-expiry-margin 600 \
    --max-restarts 20 --retry-interval 8

cookies 優先序：命令列 > playlist 的 `cookies` 欄位 > `<專案>/cookies.txt`。

### 6.2 `playlist.json` schema

    {
      "target": "rtmp://127.0.0.1:1935/live/main",
      "clients": ["web_embedded", "mweb", "default", "android_vr"],
      "segments": [
        { "id": "v1", "type": "vod",    "url": "https://www.youtube.com/watch?v=<id>",
          "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b",
          "mode": "copy", "seconds": 271 },
        { "id": "f1", "type": "filler", "path": "assets/transition.mp4", "seconds": 4 },
        { "id": "l1", "type": "live",   "url": "https://www.youtube.com/watch?v=<id>",
          "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b",
          "mode": "copy", "seconds": 40 },
        { "id": "t1", "type": "file",   "path": "media/t1.mp4", "mode": "copy", "seconds": 12 }
      ]
    }

| 欄位 | 說明 |
|---|---|
| `segments[].type` | `vod`（有限素材）／`live`（即時來源）／`filler`（本機檔，不交班）／**`file`（本機檔，走 takeover 交班）** |
| `segments[].format` | yt-dlp 格式選擇。**必須含 `[ext=mp4]+ba[ext=m4a]`**，否則會挑到 opus 音訊，塞不進 FLV |
| `segments[].seconds` | **必填**。排程器以 `float(spec["seconds"])` 推算 `stop_at`／`start_at`，缺欄位直接拋 `KeyError`。VOD 填實際長度；填超過會讓 ffmpeg 提前結束 → `early_exit` → 切墊片 → 重播同段 |
| `segments[].path` | `file`/`filler` 用，相對於專案根目錄 |
| `segments[].outpoint` | 選配。搭配 `file`，在這一秒停止讀取該檔（concat demuxer 指令）。用來截掉片尾黑畫面，見 3.8；**不需要重新編碼** |
| **過場影片** | 不在 `playlist.json` 裡，而是看 `media/_transition.mp4` 存不存在。存在時 `build_local_content.py` 會在**每一集之後**（含最後一集之後，讓繞回也有過場）插入一段。更換用 `--transition <url|id|path>`，停用用 `--no-transition` |

### 6.3 播出端 `playout.sh`（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `PLAYLIST` | `<dir>/playlist-local.json` | 輸入清單 |
| `LIST` | `<dir>/concat.txt` | 產生的 concat 清單 |
| `DEST` | `rtmp://127.0.0.1:1935/live/main` | 輸出 |
| `LOG` | `<dir>/logs/playout.log` | 日誌 |
| `NORMALIZE` | `0` | `1`＝重編碼成統一參數（來源參數不一致時才用） |
| `VENC` / `AENC` | `libx264 …2.5M` / `aac 128k` | 僅 `NORMALIZE=1` 時生效 |

行為：啟動時先建 concat 清單，**任一片段檔案不存在就中止（回傳碼 78）**，避免播出半份清單。

### 6.4 推流端 `yt_publish.sh`（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `SRC` | `rtmp://127.0.0.1:1935/live/main` | 上游 |
| `KEYFILE` | `<dir>/stream.key` | 串流金鑰檔（`chmod 600`） |
| `PUSH_URL` | `rtmp://a.rtmp.youtube.com/live2` | ingest 端點 |
| `API` / `PATH_NAME` | `http://127.0.0.1:9997` / `live/main` | 等待上游 ready |

行為：先等上游 `ready:true` 才連 YouTube（**避免在沒有內容時每 3 秒敲一次 ingest**）；`-c copy`、`-flvflags no_duration_filesize`；斷線 3 秒後重連。

### 6.5 事件記錄

【實測】`emit()` 以 JSONL 逐行附加，每筆含 `ts`（epoch 秒）、`wall`（時分秒.毫秒）與該事件欄位。

### 6.6 MediaMTX 設定（目標機實測）

| 項目 | 值 |
|---|---|
| API | `127.0.0.1:9997` |
| RTMP | `:1935` |
| HLS | `:8888`（`hlsVariant: lowLatency`、`hlsAlwaysRemux: yes`） |
| `readTimeout` | `30s`（刻意調高，讓換手由播出端主導而不是被 MediaMTX 先開槍） |
| 版本 | `v1.21.0` |

### 6.7 YouTube 輸出入介面

| 項目 | 值 |
|---|---|
| 頻道帳號 | `<GOOGLE_ACCOUNT>` |
| 頻道名稱 | 使用者指定一個中文名稱；【實測】該頻道在 YouTube 上的**實際顯示名稱與指定名稱不一致**（`<CHANNEL_ID>`），且對應的 `@handle` 與 `/c/` 路徑皆回 404 → 見 D15 |
| 直播控制室 | `https://studio.youtube.com/video/<LIVE_VIDEO_ID>/livestreaming` |
| RTMP 端點 | `rtmp://a.rtmp.youtube.com/live2` |
| stream key | 存於 `目標機:~/ytpl/stream.key`（`chmod 600`）。**不寫入本文件、不進版控**；本文件也刻意不記錄任何指紋或長度特徵。注意 `yt_publish.sh` **只在啟動時讀一次**，換金鑰必須 `launchctl kickstart -k` |
| 公開播出 | 【實測 2026-09-16 21:02】**已成立**：`<LIVE_VIDEO_ID>` 回 `live_status=is_live`、`is_live=True`；YouTube 端已長出 144p–720p 完整轉檔階梯（720p @ 2448 kbps），證明收到的是真實畫面 |
| 換金鑰流程 | 【實測】21:01:39 覆寫金鑰後 `kickstart`，**20 秒內**重新連上 ingest；之後 120 秒量測離線 0 段 |

---

## 7. 參數基準表（全部為實測值）

| 參數 | 實測值 | 意義 |
|---|---|---|
| 來源 URL TTL | 21600 秒（6 小時） | 超過即失效（主線已不使用） |
| 來源 URL 綁定 | 解析當下的公網 IP | 換出口即失效（主線已不使用） |
| 單次解析耗時 | 2.26–2.65 秒 | 換手前置時間下限 |
| MediaMTX takeover 完成 | 同秒瞬間 | 換手零斷點的前提 |
| **concat 換片／繞回縫** | **0 秒** | 55 秒涵蓋兩輪，離線 0 段 |
| **接力換片縫** | **2.0–3.1 秒／段** | 檔案 EOF 即踢掉 publisher |
| concat 冷啟動縫 | 2.55 秒 | 僅開場一次 |
| 接力冷啟動縫 | 3.07 秒 | — |
| concat 接縫 DTS 重疊 | 約 11 ms | ffmpeg 自動夾正，聽感無影響 |
| 看門狗下限／建議 | 6 秒／10 秒 | 低於 6 秒會誤判 |
| 啟動寬限 | 15 秒 | `--startup-grace` |
| 流量停滯門檻 | 12 秒 | `--flow-threshold` |
| 測試素材輸出位元率 | 3.21 Mbps | testsrc2 極難壓縮；實片會低很多 |
| 落地一輪所需磁碟 | 約 4.5–5.5 GB | 14,582 秒 @720p【推論】 |
| 對外公網 IP | `<PUBLIC_IP>`（同日前為 `<PUBLIC_IP>`） | 動態 |

**已知未修缺陷**：啟動期同一條 URL 會被解析兩次，每次約白花 2 秒。

---

## 8. 環境與硬體規格

### 8.1 目標機（2026-09-16 05:11 實測）

| 項目 | 值 |
|---|---|
| 機型 | `<MODEL>`（hostname `<HOSTNAME>.local`） |
| 晶片／架構 | Apple M1 / `arm64`（8 CPU） |
| 記憶體 | 8 GB |
| 作業系統 | macOS 27.0 |
| 磁碟 | 228 GiB 總，可用 **37 GiB**，使用率 26% |
| Homebrew | `7.0.2` |
| `ffmpeg` | `9.0.1`；**有 libx264／libx265／h264_videotoolbox／aac**（更正 v1.0：先前「沒有 libx264」是錯的） |
| `yt-dlp` | `2026.08.19` |
| `python3` | `3.14.7`（Homebrew） |
| `node` | `v26.8.2` |
| `mediamtx` | 已安裝，`launchd com.ytpl.mediamtx` 常駐（v1.21.0） |
| `streamlink` | **未安裝，且不需要** |
| ffmpeg freetype | 無（`drawtext` 0 筆）→ 字卡走路徑 A |
| 已部署到 `~/ytpl` | `relay.py`、`playout.sh`、`yt_publish.sh`、`make_concat_list.py`、`build_local_content.py`、`gapwatch.py`、`mediamtx.yml`、`stream.key`、`playlist.json` |
| 服務管理 | 【實測 2026-09-17】四個服務已從 LaunchAgent 改為 **LaunchDaemon**（`/Library/LaunchDaemons/`）。`mediamtx`／`playout`／`publish` 設 `UserName=<USER>`；`health` **以 root 執行**，因為只有 root 能對 system domain 做 `kickstart`。開機即啟動，不需要圖形登入 |
| FileVault | 【實測】**已關閉**（2026-09-17 00:07），開機不再需要人工解鎖 |
| 睡眠 | 【實測】`pmset -a sleep 0 disksleep 0 disablesleep 1`，`SleepDisabled 1`。原本是 `sleep 1`，只是被一個 `caffeinate` 擋著 |
| 待補 | `assets/transition.mp4`（D5，僅聯播支線需要）、實際重開機驗證（D17） |
| POT provider | `~/pot`（bgutil 2.0.0）與 plugin 已裝，**但實測證明對本封鎖無效**（R-09） |

### 8.2 工作機（開發與驗收）

| 項目 | 值 |
|---|---|
| 位址 | `<WORK_HOST>` |
| `ffmpeg` | `9.0.1`（**無 freetype**，有 libx264） |
| `yt-dlp` | `2026.08.19` |
| `mediamtx` | 已安裝（驗收時本機拉起） |
| `python3` | `3.14.7` |
| `node` | `v22.23.0` |
| Homebrew | `7.0.2`；使用者已執行 `brew upgrade`，**本輪複查沒有任何 brew 程序在跑** |
| Chrome profile | 【實測】存在，但 **macOS TCC 擋住**（`Operation not permitted`），`--cookies-from-browser chrome` 失敗 |

---

## 9. 安全與合規

1. **授權**：使用者聲明所有使用的 YouTube 影片皆為合法授權。落地成本機檔案是**為播出穩定性**，不是要規避授權。
2. **stream key 管理**：
   - 不寫入 `playlist.json` 或任何進版控的檔案。
   - 以 `chmod 600` 的獨立檔案提供（`目標機:~/ytpl/stream.key`）。
   - 日誌遮罩，禁止印出完整 RTMP URL。`yt_publish.sh` 只把「連線目標名稱」寫進日誌，不含金鑰。
3. **cookies.txt 管理**：同等敏感（等同帳號登入態）。`chmod 600`、不進版控、用完可輪替。
4. **帳號**：頻道帳號 `<GOOGLE_ACCOUNT>`。本文件不儲存任何密碼、金鑰或 cookie。
5. **法遵**：內容著作權與申報責任由頻道所有者承擔。

---

## 10. 測試計畫

### 10.1 測試資源

| 資源 | 內容 |
|---|---|
| 播放清單 | `https://www.youtube.com/playlist?list=<PLAYLIST_ID>`（`範例清單`，頻道 <CHANNEL_NAME> `<CHANNEL_HANDLE>`） |
| 段數／總長 | 53 段／14,582 秒（4 小時 3 分 2 秒） |
| 段長分布 | 最短 91 秒／最長 619 秒／平均 275 秒 |
| 測試素材 | `目標機:~/ytpl/media/t1.mp4`（12 秒）、`t2.mp4`（9 秒）、`t3.mp4`（15 秒），`testsrc2` 1280×720@30 + 正弦音，參數一致可 `-c copy` |

### 10.2 測試項目與結果

| 編號 | 項目 | 方法 | 結果 |
|---|---|---|---|
| T-01 | 單段播放 | 一段本機素材推到 MediaMTX | **通過** |
| T-02 | 連續換手 | 接力 3 段並加 `--observe` | **通過但揭露缺陷**：換片每段 2.0–3.1 秒縫 |
| T-03 | 換手量測 | MediaMTX API | `inboundFramesInError: 0` |
| T-04 | 看門狗 | 人為切斷來源 | 【待驗證】 |
| T-05 | 墊片接管 | 來源停滯 | 【待驗證】（素材未定 D5） |
| T-06 | URL 生命週期 | 跑超過 `--url-max-age` | 主線已免除 |
| T-07 | 真實清單跑滿一輪 | 測試版 106 段、2 小時 56 分 | **通過（並被實測）**：【實測】2026-09-17 05:45:56 的循環點，`loopwatch.py` 取樣 650 筆／135 秒，**接收端離線 0 段、bytesReceived 零成長 0 段**。測試版共跑約 10 小時（3.4 輪），確認多次繞回都正常。測完已用 `switch_edition.sh live` 切回正式版 |
| T-08 | 聯播 | `type=live` 拉一條正在直播的來源 | **通過**：以台視新聞台 24 小時直播（`9iRAqBMakXY`）實測，3 輪 × 120 秒。**兩次換手本身 0 縫**；量到的 6.206 秒離線是「停掉播出端 → relay 解析完成」的冷啟動期，不是換手造成的 |
| T-09 | YouTube 輸出 | 推送到 ingest | **通過**（ingest 收流，持續 45 秒以上） |
| T-10 | 長時穩定度 | 連續 72 小時 | 【待驗證】 |
| T-11 | 開機自啟 | 實際重開機，全程不登入 | **通過**。原本三個阻礙（FileVault On、無自動登入、`sleep 1`）已排除。重開機後實測：`up 3 mins, **0 users**`（完全沒有圖形登入），四個服務自動起來、行程以 `<USER>` 身分執行、鏈路 `ready=True err=0 readers=1`、YouTube 恢復 `is_live`。**播出中斷約 25 秒**（00:17:37 下指令 → 00:18:04 推流重新連上） |
| T-12 | YouTube 端最終縫隙 | 對 YouTube 端做長時間畫面監控 | **進行中**：`yt_side_monitor.py` 於 2026-09-17 12:38 起對 YouTube 輸出連續監控 3 小時（blackdetect ＋ freezedetect），涵蓋數十個換片點。初步已抓到 1 次 4.2 秒的 frame freeze，待釐清是內容的靜止畫面還是真凍結 |
| **T-13** | **concat 零縫循環** | 目標機連續 55 秒涵蓋兩輪 | **通過**：離線 0 段、零成長 0 段 |
| **T-14** | **端到端推流** | 目標機全鏈路 + 真實 stream key | **通過**：`ready/online=true`、`readers=1`、publisher 連續 |
| **T-15** | **匿名解析可用性** | 同一批影片在 05:11 與 20:02 各測一次 `yt-dlp --simulate` | **05:11 全滅、20:02 全部成功** → R-09 是暫時性標記。失敗期間 `player_client=android` 仍可用（360p） |
| **T-16** | **落地參數一致性** | 落地後比對所有片段 codec 參數 | **通過**：H.264 High L3.1／1280×720／yuv420p／30fps／AAC-LC 48k，`-c copy` 可拼 |
| **T-17** | **落地長度正確性** | 下載後與清單宣稱長度比對 | **通過（修正後）**：修掉 3.7 的兩個 bug，`Ig3vtqtXowY` 由 137s → **270.374s**，與來源一致 |
| **T-18** | **片尾黑畫面偵測與截斷** | 對 53 支掃 `blackdetect`，套用 `outpoint` 後重跑時間軸分析 | **通過**：抓到 `8jtdcMDuV_A` 片尾 67.4s 純黑並截掉；封包 436,600 → 434,583（≈67.2s），forward gap 仍為 **0** |
| **T-19** | **播出與 YouTube 端實測** | 換片點前後各 330 秒對本地與 YouTube 兩端同時跑 `blackdetect` | **通過**：兩端皆無黑畫面 → 換片點本身乾淨，先前的黑屏確認為內容問題 |
| **T-20** | **循環邊界（4 小時繞回第一段）** | 把 `concat.txt` 接成兩份，掃描接縫處的時間軸 | **通過**：869,166 個封包、**時間軸重置 0、forward gap 0**；時間戳連續累加（第二輪結束 28,973.9s ≈ 2×14,487s＋固定偏移）。因為時間戳不會歸零，`loopwatch.py` 改用「啟動時間 ＋ 單輪長度」推算循環點，在前後 45／90 秒高頻取樣 |
| **T-22** | **過場影片插入** | 全清單掃描時間軸、比對音訊參數 | **通過**：53 集 ＋ 53 段過場（`lj9nUq97uzQ`，20s，4K 降轉 720p）＝ 106 段、466,436 個封包、**forward gap 0、時間軸重置 0**；過場音訊 `aac fltp 48000 2` 與其他集數完全一致。單輪由 14,487s 變為 **15,549s（4h19m）** |
| **T-21** | **健康監控的異常偵測** | 正常跑一次；再以不存在的路徑跑一次 | **通過**：正常回 OK（+1.95 MB / 6s）；異常回 FAIL、exit 1，並寫入 `logs/alerts.jsonl` |

### 10.3 驗收順序

T-01 → T-13 → T-14 → **T-11** → T-16～T-18 → T-20 → T-22 → **T-07（進行中）** → **T-12（長時間）** → T-08（等 D6） → T-10（72 小時）。

---

## 11. 里程碑與工作分解

| 里程碑 | 內容 | 狀態 |
|---|---|---|
| M1 | 目標機環境建置 | **完成**（工具鏈齊全、MediaMTX 常駐、程式已部署） |
| M2 | 單段打通 | **完成**（T-01） |
| M3 | 連續與零斷點 | **完成**（T-13：concat 換片與繞回 0 縫） |
| M4 | 真實清單上線 | **完成**：53 支落地、黑尾截斷、過場插入，均已上線播出 |
| M5 | 推送 YouTube | **完成**：2026-09-16 21:02 起公開播出（`is_live`） |
| M6 | 聯播模式 | **可開始**：R-09 已解除，等 D6 指定來源 |
| M7 | 長時穩定度 | **進行中**：72 小時計時自 2026-09-17 00:18 重開機後起算 |

---

## 12. 風險登錄表

| 編號 | 風險 | 影響 | 對策 |
|---|---|---|---|
| R-01 | 來源 URL 6 小時過期、綁動態公網 IP | 播出中斷 | **主線已由內容落地移除**；聯播支線以 `--url-max-age 1800`＋margin 600 壓在 30 分鐘內 |
| R-02 | 目標機僅 8 GB | 掉格 | 限制同時編碼；優先 `-c copy` |
| R-03 | yt-dlp 解析因 YouTube 改版失效 | 無法取得來源 | client 備援清單；內容已落地時不影響播出 |
| R-04 | stream key 外洩 | 頻道被盜用 | 第 9 節 |
| R-05 | 上游對 24/7 重播的政策風險 | 頻道受影響 | 使用者已聲明授權；留存播出紀錄 |
| R-06 | 導入 Liquidsoap 造成相依升級 | 工作機升 10 套件 | 若真要，裝在目標機（只裝 1 formula） |
| R-07 | 長時運行記憶體累積 | 程序崩潰 | T-07／T-10 納入驗收 |
| R-08 | 工作機與目標機版本不同 | 驗收結果不能外推 | 主要驗收直接在目標機做 |
| **R-09** | **YouTube 的匿名解析會「間歇性」回 `LOGIN_REQUIRED`** | 進行中的落地或聯播中斷 | 【更正 v1.1】這**不是**永久封鎖：同一批影片 05:11 全滅、20:02 全部成功，是 IP 層級的暫時標記。**不需要 cookie**。封鎖期間的備援是 `player_client=android`（上限 360p），已寫進 `build_local_content.py` 自動重試。根本對策仍是內容落地（FR-13）——落地後主線完全不碰來源 URL |
| **R-12** | **下載靜默截斷** | 半支影片混進 concat，播出中段突然跳掉 | 已修掉兩個根因（3.7）：ffmpeg 未加 `-nostdin`、HLS(m3u8) 路徑不完整。並加下載後長度比對，容差 5%／10 秒，不符即自動重抓 |
| **R-13** | **來源影片本身含長黑尾**（實測 66–67 秒） | 觀眾端長時間黑畫面，但**所有傳輸與時間軸監控都顯示正常** | 落地時就偵測並以 `outpoint` 截掉（3.8）。注意：這類問題前兩層監控看不到，必須靠像素層的 `blackdetect` |
| **R-14** | **FLV 時間戳 32 位元回繞** | 連續播出約 **49.7 天**後時間戳回繞，YouTube 端可能中斷 | 【實測】`-stream_loop -1` 的時間戳是**連續累加**、不會每輪歸零，所以時間軸會一路長上去。對策：每月重啟一次 `com.ytpl.playout`，時間軸即歸零 |
| **R-15** | **無法無人值守重開機**（FileVault 開啟 ＋ 無自動登入） | 停電、當機、或任何重開機都需要**人到機器前解鎖**；遠端完全無法恢復 | 已做：`pmset -a sleep 0 disksleep 0 disablesleep 1`（避免睡眠造成的假性停播）。待決：關閉 FileVault（安全取捨，見 D16），並把服務從 LaunchAgent 改成 **LaunchDaemon** —— LaunchAgent 只在圖形介面登入後才會啟動，對無人值守的機器不適合 |
| **R-10** | **落地內容需要磁碟（一輪 4.5–5.5 GB）** | 磁碟不足 | 目標機可用 37 GiB，足夠；若要多輪備份需先清磁碟 |
| **R-11** | **concat 接縫的 DTS 重疊（約 11 ms）** | 時間軸不完美 | ffmpeg 自動夾正；必要時改離線預接單一大檔 |

---

## 13. 待決事項

| 編號 | 待決事項 | 需要誰 | 阻塞 |
|---|---|---|---|
| **D1** | ~~YouTube stream key~~ | — | **已解決**：已取得並置於 `目標機:~/ytpl/stream.key` |
| D2 | ~~輸出解析度與位元率~~ | — | **實務上已定案**：落地統一 1280×720，播出 2500 kbps，YouTube 端實際長出 720p @ 2448 kbps。要改再議 |
| D3 | ~~是否導入 Liquidsoap~~ | — | **已無必要**：當初是為了字卡，而字卡走路徑 A（ffmpeg overlay）就夠，不需要多一層 DSL。若日後需要進階排程再議 |
| D4 | ~~播放順序政策~~ **固定序循環**（使用者 2026-09-17 定案） | — | **已解決** |
| D5 | 墊片素材 | 使用者 | FR-05 |
| D6 | 聯播來源清單 | 使用者 | M6 |
| D7 | ~~目標機是否安裝 Homebrew／MediaMTX~~ | — | **已解決** |
| D8 | ~~告警管道~~ | — | **已解決（2026-09-17 03:14）**：改用 Telegram Bot `<BOT_NAME>`。token 存 `~/ytpl/telegram.json`（`chmod 600`，不進版控），chat_id 由 `getUpdates` 自動取得並回寫。實測 `--test-alert` 與模擬故障演練都送出成功 |
| D9 | 頻道名稱與說明需在 Studio 手動設定（API 不支援） | 使用者 | 上線前 |
| **D14** | ~~YouTube Studio 端的直播活動狀態~~ | — | **已解決**：改用 `<LIVE_VIDEO_ID>` 與新金鑰後，2026-09-16 21:02 已成功公開播出 |
| **D15** | 直播標題需在 Studio 設定或用 YouTube Data API（RTMP 帶不進標題） | 使用者 | 對外觀感 |
| **D16** | ~~是否關閉目標機的 FileVault~~ | — | **已解決（2026-09-17 00:07）**：FileVault 已關閉、睡眠永久停用、四個服務已改為 LaunchDaemon。只剩實際重開機驗證 |
| **D17** | ~~實際重開機驗證~~ | — | **已解決（2026-09-17 00:20）**：重開機後全鏈路自動恢復，全程無人登入，中斷約 25 秒 |
| D10 | 是否需要固定出口 IP | 使用者 | 已降級（主線不再依賴） |
| **D11** | ~~提供 YouTube 登入 cookie~~ | — | **已不需要**：R-09 更正為暫時性標記，且已有 `android` client 備援。若日後遇到長期封鎖再議 |
| **D13** | 是否要保留落地後的正規化副本堆疊（重編碼一次 = 一次畫質損失） | 使用者 | 畫質政策 |
| **D12** | 是否採「離線預接成單一大檔」以消除 DTS 警告 | 使用者 | R-11 |

---

## 附錄 A：本規格書的數字出處

| 數字 | 出處 |
|---|---|
| URL TTL／解析耗時／看門狗門檻 | v4.3 實測 |
| 6 次換手 0.000 秒、MediaMTX takeover 同秒 | v4.3 實測 |
| Liquidsoap 三關、ffmpeg 無 freetype | v4.5 實測 |
| 接力縫隙 3.066／2.053／2.040／0.635 秒 | 本輪 `logs/local-relay2.out` |
| concat 換片與繞回 0 縫 | 本輪目標機 gapwatch 55 秒（506 樣本、api_fail 0） |
| concat 冷啟動 2.546 秒 | 本輪工作機 gapwatch 70 秒（641 樣本） |
| DTS 重疊 11 ms | 本輪 `logs/playout.log` |
| 目標機工具版本、libx264 存在、磁碟 37 GiB | 本輪 ssh 實測 |
| Chrome cookie 被 TCC 擋 | 本輪 `yt-dlp --cookies-from-browser chrome` 實測 |
| 4 支影片全 `LOGIN_REQUIRED` | 本輪 yt-dlp 實測（`Ig3vtqtXowY`／`cJx8-vsH14E`／`7xJR7o1gB8c`／`aqz-KE-bpKQ`） |
| 端到端推流成功 | 本輪目標機 05:13 實測（T-14） |

## 附錄 B：驗證指令

    # 播出中，量接收端縫隙（另開終端）
    python3 ~/ytpl/gapwatch.py http://127.0.0.1:9997 live/main 120

    # 本地樞紐狀態
    curl -s http://127.0.0.1:9997/v3/paths/get/live/main

    # 匿名解析是否仍被擋（回 2 表示全滅）
    python3 ~/ytpl/relay.py --playlist ~/ytpl/playlist.json --check

    # 落地進度
    python3 ~/ytpl/build_local_content.py --status

    # 服務狀態
    launchctl list | grep ytpl
    tail -f ~/ytpl/logs/playout.log ~/ytpl/logs/publish.log

    # ffmpeg 有沒有 freetype（回 0 表示沒有 drawtext）
    ffmpeg -hide_banner -filters | grep -c drawtext
