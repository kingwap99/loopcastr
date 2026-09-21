# 24/7 YouTube 直播頻道系統規格書 v1.0

## 0. 文件資訊

| 項目 | 內容 |
|---|---|
| 文件名稱 | 24/7 YouTube 直播頻道系統規格書 |
| 版本 | v1.0（動工前基線） |
| 日期 | 2026-09-16 |
| 狀態 | **待確認**（第 13 節待決事項未定案前不視為凍結） |
| 依據文件 | v3 OBS 插件評估、v4 合併建議、v4.2 聯播方案 A 實測、v4.3 來源 URL 生命週期與看門狗門檻、v4.4 Liquidsoap 提案評估、**v4.5 Liquidsoap 三關實測與更正** |
| 撰寫原則 | 本文件只寫**已實測**或**已決定**的內容；未量測者一律標【待驗證】，推導者標【推論】 |

---

## 1. 專案目標與範圍

### 1.1 目標

把同一個團體（同一創作者）已授權的 YouTube 作品，組成一個**24/7 不中斷**的直播頻道，並具備把外部正在進行的直播**聯播**進來的能力。

### 1.2 範圍內

1. 從 YouTube 取得來源媒體（不重新上傳、不儲存母帶）。
2. 依播放清單順序連續播出，全天候不中斷。
3. 段與段之間的換手不得讓觀眾端中斷。
4. 來源失效、卡住、需重新解析時自動恢復。
5. 輸出推送到 YouTube 直播 ingest。
6. 聯播：把一條正在直播的 YouTube 訊號拉進來再推出去。

### 1.3 範圍外（已明確排除，不再重複評估）

| 排除項目 | 原因 | 出處 |
|---|---|---|
| SPX-GC | 使用者已指示放棄 | 本輪條件更新 |
| CasparCG | 不支援 Apple Silicon | 本輪條件更新 |
| OBS 插件（自製或現成） | 使用者已指示自行實作、不採插件路線 | v3 結論 |
| Liquidsoap 作為**決策層** | 三關雖全過，但決策層換掉拿不到額外能力，且要付出 10 個相依升級 | v4.5 |
| 自行架設轉碼農場 | 目標機為 8 GB M1 單機 | 本輪 |

Liquidsoap 並未被排除為**選配引擎**，見 FR-11 與第 13 節 D3。

---

## 2. 名詞定義

| 名詞 | 定義 |
|---|---|
| **來源（source）** | 一段要被播出的媒體，實際上是 yt-dlp 解析出來的直連 URL |
| **段（segment）** | 播放清單中的一個項目，型別為 `vod` / `live` / `filler` |
| **解析（resolve）** | 用 yt-dlp 把 `videoId` 或 watch URL 轉成可直連的媒體 URL |
| **換手（handover / takeover）** | 由新的發佈程序接手，再把舊的收掉，讓接收端不中斷 |
| **墊片（filler）** | 來源不可用時頂上的備援片段 |
| **看門狗（watchdog）** | 偵測輸出停滯並重開該段的機制 |
| **聯播（simulcast）** | 把正在直播的來源同步再推出去 |
| **工作機** | 使用者日常使用的 MacBook（`<WORK_HOST>`） |
| **目標機** | 目標機（<MODEL>，M1，8 GB，macOS 27.0） |

---

## 3. 系統架構

### 3.1 元件

| 元件 | 實作 | 角色 |
|---|---|---|
| 播放清單 | `playlist.json` | 描述要播什麼、順序、每段長度 |
| 播放引擎 | `loopcastr/relay.py`（Python 3） | 解析來源、排程、換手、看門狗、事件記錄 |
| 解析器 | `yt-dlp` | 把 watch URL 轉成直連媒體 URL |
| 本地媒體樞紐 | `MediaMTX` | 接收 RTMP、**提供接管換手（takeover）讓換手零斷點**、提供 HLS 給驗收、提供 HTTP API 給觀測 |
| 編碼／封裝 | `ffmpeg` | 必要時轉碼或 remux |
| 輸出 | YouTube RTMP ingest | 最終播出 |
| 字卡／轉場（選配） | Liquidsoap | 見 FR-11 |

### 3.2 資料流

    playlist.json
         |
         v
    relay.py ──resolve──> yt-dlp ──> 直連媒體 URL（TTL 21600 秒、綁解析當下公網 IP）
         |
         | ffmpeg（copy 或 transcode）
         v
    MediaMTX  rtmp://<host>:1935/<path>
         |            |
         |            └──> HTTP API :9997（觀測 bytesReceived / readers）
         |
         ├──> HLS :8888（本機驗收、內部檢視）
         └──> 推送 YouTube：rtmp://a.rtmp.youtube.com/live2 + stream key

【實測】資料流圖裡的「推送 YouTube」必須由 **MediaMTX 端**發起（`runOnReady` 拉起 `ffmpeg -c copy`），不能讓 `relay.py` 直接把 target 指向 YouTube。原因見 3.4。

### 3.3 部署拓撲

| 角色 | 機器 | 說明 |
|---|---|---|
| 開發／測試 | 工作機 `<WORK_HOST>` | 開發、離段測試、驗收 |
| **正式執行** | **目標機** | 24/7 長駐；使用者已指示裝在這一台 |
| 對外 | 兩台共用同一 NAT | 對外公網 IP `<PUBLIC_IP>`（**動態**，見下） |

【實測】兩台同閘道（`<GATEWAY>`）、同 NAT、同公網 IP，因此解析端與發佈端即使不同機，URL 內的 IP 綁定仍能成立。

【實測】**這個 IP 會變**：同一天稍早量到 `<PUBLIC_IP>`，本次量到 `<PUBLIC_IP>`。IP 是動態的，變動時間尺度可能小於本專案的工作時長。來源 URL 綁的是**解析當下**的出口 IP，一旦換 IP，所有已解析的 URL 立刻失效，必須重新解析才能續播。這不是理論風險，本輪已實際觀察到一次變動。

### 3.4 架構決策紀錄

| 決策 | 結論 | 理由 |
|---|---|---|
| 是否自行實作 | 是 | 使用者指示 |
| 是否用播放引擎取代控制層 | 否 | `relay.py` 已實作並實測事件式換手（6 次換手、接收端離線 0.000 秒） |
| 是否用 Liquidsoap 推 YouTube | 不必要 | 原生 `output.youtube.live.rtmp` 已實測可用，但導入成本兩台機器差很多（見 8.3） |
| 輸出樞紐是否用 MediaMTX | **必須** | 【實測】`relay.py` 的 `api_path_of()` 只認 `127.0.0.1` / `localhost`。target 是本機 MediaMTX 才會走 `takeover`（零斷點）；**直接推 YouTube RTMP 會退化成 `cut`，換手一定有縫隙** |
| 是否直接推 YouTube RTMP | 否 | 同上。YouTube ingest 不接受兩條 publisher 並存，無法做 takeover。正確鏈路：`ffmpeg → 本機 MediaMTX →（MediaMTX 端 push）→ YouTube`，YouTube 只是 MediaMTX 路徑的另一個消費者 |

---

## 4. 功能需求

優先級：**P0**＝正式上線必要；**P1**＝上線後補；**P2**＝選配。

| 編號 | 需求 | 優先 | 驗收依據 |
|---|---|---|---|
| FR-01 | 能把 watch URL／videoId 解析成直連媒體 URL，並提供 client 備援清單 | P0 | 解析成功且取得可播放 URL；單次耗時 2.26–2.65 秒 |
| FR-02 | 以 `playlist.json` 描述播放內容，支援 `vod` / `live` / `filler` 三種段 | P0 | schema 見 6.2 |
| FR-03 | 依序連續播出，全部播完自動回到第一段；24/7 不中斷 | P0 | 連續跑完一輪以上無停頓 |
| FR-04 | **零斷點換手**：新發佈端先接手，才收掉舊的 | P0 | 接收端離線時間 0.000 秒（實測基準） |
| FR-05 | 來源停滯時以墊片先接管畫面 | P1 | 停滯期間無黑畫面 |
| FR-06 | 看門狗：輸出停滯超過門檻即重開該段，並有啟動寬限與最大重試 | P0 | 人為中斷來源後自動恢復 |
| FR-07 | 來源 URL 生命週期管理：超過 `--url-max-age` 就重新解析；到期前 `--url-expiry-margin` 先換掉 | P0 | 不因 URL 過期中斷 |
| FR-08 | 聯播模式：支援 `type=live` 的長時段來源並主動定時換手 | P1 | 實測 304 秒內 6 次換手、接收端離線 0.000 秒 |
| FR-09 | 事件記錄成 JSONL，並可透過 MediaMTX API 觀測流量 | P1 | 事件檔可完整還原一次播放過程 |
| FR-10 | 將最終訊號推送至 YouTube 直播 ingest | P0 | YouTube 端確認收到訊號 |
| FR-11 | 字卡／台標／字幕（選配） | P2 | 見 4.1 |
| FR-12 | 連續失敗時發出告警 | P2 | 需先定案管道（D8） |

### 4.1 FR-11 的兩條實作路徑

【實測】兩台的 ffmpeg **都沒有 `--enable-libfreetype`**：工作機 `8.1.2`、目標機 `9.0.1`，`drawtext` / `subtitles` / `ass` 全部不存在（以 `ffmpeg -filters` 實查為 0 筆）。也就是說**升級 ffmpeg 不會解決這件事**，Homebrew 的 ffmpeg 就是不編 freetype。

| 路徑 | 做法 | 成本 |
|---|---|---|
| A（低） | HTML → PNG → `video.add_image` 或 ffmpeg `overlay` | 不需動任何既有套件 |
| B（中） | Liquidsoap `video.add_text.native` | 目標機已裝 ffmpeg 9，`brew install --dry-run` 顯示**只裝 1 個 formula、不動任何既有套件**；只有工作機才會拉 10 個升級 |

【實測】路徑 B 確實可行：差異圖 5400 像素中僅 48 個非零、全部落在繪字框內。但依 v4.5 建議，若只是要畫字，先走路徑 A。

---

## 5. 非功能需求

| 編號 | 項目 | 需求 | 依據 |
|---|---|---|---|
| NFR-01 | 記憶體 | 目標機僅 8 GB，同時只允許一條主要編碼／複製路徑 | 【實測】目標機 = 8 GB |
| NFR-02 | 磁碟 | 不落地儲存母帶；僅暫存墊片與日誌。目標機可用 40 GiB 以上 | 【實測】目標機可用 40 GiB |
| NFR-03 | 頻寬 | 上傳需容納輸出位元率的 1.5 倍以上；單條動態內容以 2–3 Mbps 估算 | v4.3 §4 |
| NFR-04 | 可用率 | 24/7；看門狗恢復時間目標 ≤ 10 秒 | v4.3 門檻實測 |
| NFR-05 | 解析延遲 | 單次解析 2.26–2.65 秒，換手前置時間需大於此值 | 【實測】 |
| NFR-06 | 開機自啟 | 以 `launchd` 管理，開機自動拉起並在結束後重啟 | 見第 9 節注意事項 |
| NFR-07 | 安全 | stream key 不得進入版控；以環境變數或受限權限檔案提供 | 見第 9 節 |
| NFR-08 | 可觀測性 | 需有結構化事件日誌與流量查詢端點 | FR-09 |

【實測】環境注意事項：macOS 的 `nohup` 會剝除 `DYLD_*` 環境變數，長駐程序請用 `launchd` 或直接前景執行，不要用 `nohup` 包。

---

## 6. 介面規格

### 6.1 播放引擎命令列介面

【實測】`relay.py` 現行參數（節錄自程式碼）：

| 參數 | 預設 | 用途 |
|---|---|---|
| `--playlist` | `playlist.json` | 播放清單路徑 |
| `--target` | 取自清單 | 覆寫輸出目標 |
| `--clients` | 取自清單 | 覆寫 yt-dlp client 清單 |
| `--only` | 無 | 只播指定 segment id（逗號分隔） |
| `--observe` | 關 | 用 MediaMTX API 量測縫隙 |
| `--overlap` | `0.0` | 換手重疊秒數 |
| `--resolve-lead` | `4.0` | 提前解析秒數 |
| `--watchdog` | `3.0` | 輸出停滯幾秒算異常（0＝關閉） |
| `--startup-grace` | `12.0` | 首個 progress 前的容忍秒數 |
| `--flow-threshold` | `5.0` | `bytesReceived` 零成長幾秒算異常 |
| `--max-restarts` | `6` | 單段最大重試次數 |
| `--retry-interval` | `8.0` | 來源失效後重試間隔 |
| `--filler-on-stall` | `assets/transition.mp4` | 停滯時頂上的墊片（空字串＝停用） |
| `--url-max-age` | `1800.0` | 來源 URL 最長沿用秒數 |
| `--url-expiry-margin` | `300.0` | 到期前幾秒先換掉 |
| `--probe-seconds` | `3.0` | 探測來源時先抓幾秒 |
| `--probe-wait` | `4.0` | HLS 探測時兩次讀取間隔 |
| `--restart-mode` | `auto` | `takeover`（零斷點）／`cut`／`auto` |
| `--dry-run` | 關 | 只印不執行 |

**建議上線值**（v4.3 實測得出，與程式預設不同，需明確帶入）：

    --watchdog 10 --startup-grace 15 --flow-threshold 12 \
    --url-max-age 1800 --url-expiry-margin 600 \
    --max-restarts 20 --retry-interval 8

### 6.2 `playlist.json` schema

【實測】現行格式：

    {
      "target": "rtmp://127.0.0.1:1935/live/<path>",
      "clients": ["web_embedded", "mweb", "default", "android_vr"],
      "segments": [
        { "id": "v1", "type": "vod",    "url": "https://www.youtube.com/watch?v=<id>",
          "format": "b[height<=720]/b", "mode": "copy", "seconds": 20 },
        { "id": "f1", "type": "filler", "path": "assets/transition.mp4", "seconds": 4 },
        { "id": "l1", "type": "live",   "url": "https://www.youtube.com/watch?v=<id>",
          "format": "b[height<=720]/b", "mode": "copy", "seconds": 40 }
      ]
    }

| 欄位 | 說明 |
|---|---|
| `target` | 輸出目標 RTMP URL |
| `clients` | yt-dlp 解析用的 client 備援順序 |
| `segments[].type` | `vod`（有限素材）／`live`（即時來源）／`filler`（本機檔） |
| `segments[].format` | yt-dlp 格式選擇；`b[height<=720]/b` 為 720p 上限 |
| `segments[].mode` | `copy`＝不轉碼；其餘＝轉碼 |
| `segments[].seconds` | **必填，不可省略**。排程器以 `float(spec["seconds"])` 推算 `stop_at` 與下一段的 `start_at`，缺欄位會直接拋 `KeyError`。VOD 請填**實際長度**；填超過長度會讓 ffmpeg 提前結束，觸發 `early_exit` → 切墊片 → 重播同段。【實測】 |

### 6.3 事件記錄

【實測】`emit()` 以 JSONL 逐行附加，每筆含 `ts`（epoch 秒）、`wall`（時分秒.毫秒）與該事件欄位。

### 6.4 本地媒體樞紐設定

【實測】MediaMTX `v1.21.0` 設定：

| 項目 | 值 |
|---|---|
| API | `:9997` |
| RTMP | `:1935` |
| HLS | `:8888` |
| HLS 變體 | `lowLatency` |

### 6.5 YouTube 輸出入介面

| 項目 | 值 |
|---|---|
| 頻道帳號 | `<GOOGLE_ACCOUNT>` |
| 直播控制室 | `https://studio.youtube.com/video/<VIDEO_ID>/livestreaming` |
| RTMP 端點 | `rtmp://a.rtmp.youtube.com/live2` |
| stream key | **未取得**，見 D1 |
| ingest 現況 | 【實測】`<VIDEO_ID>` 回報「This live event will begin in a few moments.」＝已排程、尚未開播 |

---

## 7. 參數基準表（全部為實測值）

| 參數 | 實測值 | 意義 |
|---|---|---|
| 來源 URL TTL | **21600 秒（6 小時）** | 超過即失效 |
| 來源 URL 綁定 | 解析當下的公網 IP | 換出口即失效 |
| 單次解析耗時 | 2.26–2.65 秒 | 換手前置時間下限 |
| 換手接收端離線 | **0.000 秒** | 304 秒內 6 次換手 |
| 看門狗下限／建議 | 6 秒／10 秒 | 低於 6 秒會誤判 |
| 啟動寬限 | 15 秒 | `--startup-grace` |
| 流量停滯門檻 | 12 秒 | `--flow-threshold` |
| 單條頻寬估算 | 2–3 Mbps | 動態內容 |
| 對外公網 IP | **`<PUBLIC_IP>`**（同日稍早為 `<PUBLIC_IP>`） | 動態；換 IP 會使既有來源 URL 全部失效 |

**已知未修缺陷**：啟動期同一條 URL 會被解析兩次，每次約白花 2 秒。【實測】

---

## 8. 環境與硬體規格

### 8.1 目標機

| 項目 | 【實測】值 |
|---|---|
| 機型 | `<MODEL>`（hostname `<HOSTNAME>.local`） |
| 晶片／架構 | Apple M1 / `arm64` |
| 記憶體 | 8 GB |
| 作業系統 | macOS 27.0 |
| 磁碟可用 | 39 GiB |
| Homebrew | `7.0.2`；**101 個 formula ＋ 4 個 cask，落後 0 個（已完全更新）** |
| `ffmpeg` | `9.0.1`（比工作機新） |
| `yt-dlp` | `2026.08.19` |
| Homebrew `python3` | `3.14.7` |
| `node` | `v26.8.2` |
| `streamlink` | **不需要**（【實測】`relay.py` 完全沒用到 streamlink） |
| `mediamtx` | **未安裝**（M1 必須補；見 3.4，它是零斷點換手的必要元件） |
| `liquidsoap` | 未安裝；`brew install --dry-run` 顯示只裝 1 個 formula、不動既有套件 |
| 既有檔案 | `~/loopcastr/relay.py`（34,828 bytes） |
| 墊片素材 | `~/loopcastr/assets/transition.mp4` **不存在**（FR-05／T-05 需先補上） |
| 對外公網 IP | `<PUBLIC_IP>`（動態；同日稍早為 `<PUBLIC_IP>`） |
| `launchd` 注意 | 非互動式 shell 的 `PATH` 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，**`/opt/homebrew/bin` 不在裡面**；`launchd` 同理，所有指令必須寫絕對路徑 |

### 8.2 工作機（開發與驗收）

| 項目 | 【實測】值 |
|---|---|
| 位址 | `<WORK_HOST>` |
| `ffmpeg` | `8.1.2`（**無 freetype**） |
| `x265` | `4.2` |
| `yt-dlp` | `2026.07.04` |
| `streamlink` | `8.4.0` |
| `mediamtx` | `v1.21.0` |
| Python | 3.14.6（Homebrew） |
| Homebrew | `7.0.2`，262 formula ＋ 14 cask |
| 落後套件數 | 【實測】**仍是 133 個**，`ffmpeg` 仍 `8.1.2`、`x265` 仍 `4.2`。使用者表示已重跑 `brew upgrade`，複查未見變化；當時沒有任何 `brew` 程序在執行 |
| 與目標機的落差 | 工作機 `ffmpeg 8.1.2` / `yt-dlp 2026.07.04`，目標機 `ffmpeg 9.0.1` / `yt-dlp 2026.08.19`。**在工作機驗收通過不必然等於正式機會動**；建議主要驗收直接在目標機上做 |

### 8.3 若導入 Liquidsoap（D3 通過時才適用）

【實測】成本**因機器而異**，差別來自 ffmpeg 版本：

| 項目 | 工作機 | 目標機 |
|---|---|---|
| 版本／bottle | `liquidsoap 2.4.5`，bottle 標籤**只有 `arm64_tahoe`** | 同左 |
| 是否會落到編譯 | 否，抓 bottle manifest | 否 |
| `brew install --dry-run` 結果 | **會升級 10 個套件**：`mpg123 libvmaf libvpx ca-certificates openssl@3 sdl3 sdl2-compat x265 xz ffmpeg` | **只裝 1 個 formula（liquidsoap），不動任何既有套件** |
| 原因 | ffmpeg 停在 `8.1.2`，達不到 Liquidsoap 要的 ffmpeg 9 | ffmpeg `9.0.1` 已就位 |
| 動態連結需求 | `libavcodec.63` / `libswresample.7` / `libswscale.10` / `libavformat.63` / `libavutil.61` / `libavfilter.12`（＝ffmpeg 9） | 已滿足 |

三個坑（兩台都適用）：stdlib 路徑編譯時寫死；缺 ffmpeg 9 會 `Symbol not found: _swr_alloc`；`nohup` 剝 `DYLD_*`。

附帶【實測】：目標機有 3 個未受信任的 tap（`agentgazer/tap`、`anomalyco/tap`、`steipete/tap`），Homebrew 會發出警告並忽略其 formula；不影響本次安裝，但之後在那台機器跑 `brew` 要有心理準備。

---

## 9. 安全與合規

1. **授權**：使用者聲明所有使用的 YouTube 影片皆為合法授權。本系統不重新上傳、不儲存母帶，僅即時轉送。
2. **stream key 管理**：stream key 等同頻道控制權。
   - 不得寫入 `playlist.json` 或任何進版控的檔案。
   - 以環境變數或 `chmod 600` 的獨立檔案提供，並在 `.gitignore` 排除。
   - 日誌必須遮罩，禁止印出完整 RTMP URL。
3. **帳號**：頻道帳號為 `<GOOGLE_ACCOUNT>`。本規格書不儲存任何密碼或金鑰。
4. **法遵**：播出內容的著作權與政治性內容申報責任由頻道所有者承擔。

---

## 10. 測試計畫

### 10.1 測試資源（本次提供）

| 資源 | 內容 | 【實測】狀態 |
|---|---|---|
| 播放清單 | `https://www.youtube.com/playlist?list=<PLAYLIST_ID>` | **公開可讀** |
| 清單名稱 | `範例清單` | 頻道：<CHANNEL_NAME>（`<CHANNEL_HANDLE>`） |
| 段數 | 53 段 | 全部取得長度 |
| 總長 | **14,582 秒 ＝ 4 小時 3 分 2 秒** | 一輪循環長度 |
| 段長分布 | 最短 91 秒／最長 619 秒／平均 275 秒 | 短片為主 |
| 每日循環次數 | 約 5.9 輪 | 【推論】24 ÷ 4.0506 |
| 直播帳號 | `studio.youtube.com/video/<VIDEO_ID>/livestreaming` | 已排程、尚未開播 |
| 帳號 | `<GOOGLE_ACCOUNT>` | 未提供 stream key |

測試用清單檔案已產出：`work/playlist_chen.csv`（格式 `videoId|秒數|標題`）。

### 10.2 測試項目

| 編號 | 項目 | 方法 | 通過標準 |
|---|---|---|---|
| T-01 | 單段播放 | `--only` 播一段 720p VOD 到本機 MediaMTX | 本機 HLS 可看、無錯誤 |
| T-02 | 連續換手 | 播 6 段以上並加 `--observe` | 接收端離線 0.000 秒 |
| T-03 | 換手量測 | 304 秒內至少 6 次換手 | 同上，且 `inboundFramesInError` 為 0 |
| T-04 | 看門狗 | 中途切斷來源 | 10 秒內自動恢復 |
| T-05 | 墊片接管 | 來源停滯 | 期間無黑畫面 |
| T-06 | URL 生命週期 | 跑超過 `--url-max-age` | 全程不中斷、有重新解析事件 |
| T-07 | 真實清單 | 用 53 段清單跑滿一輪（4 小時） | 無中斷、記憶體穩定 |
| T-08 | 聯播 | `type=live` 拉一條正在直播的來源 | 連續 300 秒以上不中斷 |
| T-09 | YouTube 輸出 | 推送到 `<VIDEO_ID>` 對應 ingest | YouTube 端確認收到（**需 stream key**） |
| T-10 | 長時穩定度 | 連續 72 小時 | 無需人工介入 |
| T-11 | 開機自啟 | 重啟目標機 | `launchd` 自動拉起（指令一律絕對路徑） |
| T-12 | **YouTube 端最終縫隙** | 在目標機跑完整鏈路，同時錄下 YouTube 端畫面 | 【待驗證】MediaMTX 本機路徑的 0.000 秒**不能直接外推到 YouTube**；必須實際量測換手時觀眾端斷不斷 |

### 10.3 驗收順序

T-01 → T-02／T-03 → T-04／T-05 → T-06 → T-08 → **T-09（阻塞於 D1）** → T-12 → T-07 → T-11 → T-10。

---

## 11. 里程碑與工作分解

| 里程碑 | 內容 | 產出 | 前置 |
|---|---|---|---|
| M1 | 目標機環境建置 | 目標機可執行 `relay.py` 與 MediaMTX | D7 |
| M2 | 單段打通 | T-01 通過 | M1 |
| M3 | 連續與零斷點 | T-02／T-03／T-04 通過 | M2 |
| M4 | 真實清單上線 | 53 段清單跑滿一輪 | M3 |
| M5 | 推送 YouTube | T-09 通過 | **D1（stream key）** |
| M6 | 聯播模式 | T-08 通過 | M3 |
| M7 | 長時穩定度 | T-10 通過 | M5 |

---

## 12. 風險登錄表

| 編號 | 風險 | 影響 | 對策 |
|---|---|---|---|
| R-01 | 來源 URL 6 小時過期、且綁公網 IP；**出口 IP 本身是動態的**（本輪已觀察到一次變動） | 播出中斷 | `--url-max-age 1800`＋`--url-expiry-margin 600` 把曝險壓在 30 分鐘內；看門狗重解析；長期穩定需固定出口（D10） |
| R-02 | 目標機僅 8 GB，且剛開機時系統背景程序（如 `ANECompilerService` 曾佔用 100% CPU） | 掉格 | 限制同時編碼數；避免轉碼、優先 `copy` |
| R-03 | yt-dlp 解析因 YouTube 改版失效 | 無法取得來源 | client 備援清單；保留降級路徑 |
| R-04 | stream key 外洩 | 頻道被盜用 | 第 9 節做法 |
| R-05 | 上游對 24/7 重播的政策風險 | 頻道受影響 | 使用者已聲明授權；播出紀錄留存 |
| R-06 | 導入 Liquidsoap 造成相依升級 | 工作機要升 10 個套件；目標機無此問題 | 若真要 Liquidsoap，**裝在目標機**（只裝 1 個 formula）；不導入則無事（D3） |
| R-07 | 長時運行記憶體累積 | 程序崩潰 | T-07／T-10 納入驗收 |
| R-08 | 工作機與目標機的 `ffmpeg`／`yt-dlp` 版本不同 | 工作機驗收通過、正式機出問題 | 主要驗收直接在目標機上做（T-01 起），工作機只當開發機 |

---

## 13. 待決事項

| 編號 | 待決事項 | 需要誰決定 | 阻塞 |
|---|---|---|---|
| **D1** | **YouTube stream key**。直播控制室需登入才能取得，我無法登入；請你提供，或自行填入 `chmod 600` 的設定檔 | 使用者 | **M5 / T-09** |
| D2 | 輸出解析度與位元率（候選：720p / 2500 kbps） | 使用者 | M5 |
| D3 | 是否導入 Liquidsoap（代價：目標機只裝 1 個 formula、工作機要 10 個升級，另加一層 DSL） | 使用者 | FR-11 走 A 或 B |
| D4 | 播放順序政策：固定序循環／每日固定表／隨機 | 使用者 | M4 |
| D5 | 墊片素材是否沿用 `assets/transition.mp4` | 使用者 | FR-05 |
| D6 | 聯播來源清單（要聯播哪些頻道） | 使用者 | M6 |
| D7 | ~~目標機是否安裝 Homebrew~~ **已解決**：目標機的 Homebrew 已裝好且落後 0。剩下的只是「要不要補裝 `mediamtx`」 | 使用者 | M1 |
| D8 | 告警管道（Line／Telegram／Email） | 使用者 | FR-12 |
| D9 | 頻道名稱、說明、是否要節目表（EPG） | 使用者 | 上線前 |
| D10 | 是否需要固定出口 IP（固定 IP 或自建出口），以消除來源 URL 綁定失效風險 | 使用者 | R-01 |

---

## 附錄 A：本規格書的數字出處

| 數字 | 出處 |
|---|---|
| URL TTL 21600 秒、解析 2.26–2.65 秒、6 次換手 0.000 秒 | v4.3 實測 |
| 看門狗門檻與建議參數 | v4.3 實測 |
| 單條 2–3 Mbps 估算 | v4.3 §4 |
| Liquidsoap 三關結果、ffmpeg 無 freetype | v4.5 實測 |
| 清單 53 段／14,582 秒 | 本輪以 yt-dlp 對真實清單實測 |
| 兩台機器規格、工具版本、落後套件數 | 本輪 ssh 實測 |
| 目標機裝 Liquidsoap 只會動 1 個 formula | 本輪在目標機執行 `brew install --dry-run liquidsoap` |
| ffmpeg 9.0.1 同樣沒有 freetype | 本輪在目標機以 `ffmpeg -filters` 實查 |
| `api_path_of()` 只認 127.0.0.1／localhost | 本輪讀 `work/playout/relay.py` 原始碼 |

## 附錄 B：驗證指令

    # 清單盤點（產生 videoId|秒數|標題）
    yt-dlp --flat-playlist --no-warnings --print '%(id)s|%(duration)s|%(title)s' \
      'https://www.youtube.com/playlist?list=<PLAYLIST_ID>'

    # 直播事件狀態
    yt-dlp --no-warnings --skip-download --print '%(live_status)s|%(title)s' \
      'https://www.youtube.com/watch?v=<VIDEO_ID>'

    # 本地樞紐狀態
    curl -s http://127.0.0.1:9997/v3/paths/list

    # 遠端環境盤點（注意：非互動式 ssh 的 PATH 不含 /opt/homebrew/bin，
    # 直接寫 command -v brew 會誤判成「未安裝」）
    ssh <USER>@<TARGET_HOST> 'export PATH=/opt/homebrew/bin:$PATH; \
      brew --version; ffmpeg -version | head -1; yt-dlp --version'

    # ffmpeg 有沒有 freetype（回 0 表示沒有 drawtext）
    ffmpeg -hide_banner -filters | grep -c drawtext

    # 目標機裝 Liquidsoap 的實際代價
    ssh <USER>@<TARGET_HOST> 'export PATH=/opt/homebrew/bin:$PATH; brew install --dry-run liquidsoap'
