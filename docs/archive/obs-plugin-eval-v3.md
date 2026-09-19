# OBS 插件評估與自製插件可行性 — 第三版建議

> 版本：v3（2026-09-16）
> 專案：yt_playlist2yt_Livestream
> 目標主機：<TARGET_HOST>（<MODEL> / Apple M1 Pro / macOS 27.0 / arm64）
>
> 本版前提變更：
> 1. 所有影片皆為**同一團體** YouTuber 的作品，已取得**合法授權**，因此不需要下載歸檔，改為**直接從 YouTube 拉流播放**。
> 2. **放棄 SPX-GC**（前版查證：其 Control / Server API 僅付費版提供，且對 OBS 只提供 Browser Source 疊圖，不負責影片播放）。
> 3. 本次焦點：**找現成 OBS 插件**，或**評估把需求寫成自製 OBS 插件**。

## 一、先講結論

三句話：

1. **「從 YouTube 拉影片」不需要插件。** 用 streamlink 在本機開一個 HTTP relay，OBS 的 Media Source（內部就是 ffmpeg_source）當一般網路串流拉即可。本機已實測 ffmpeg 能完整解碼通過，而 OBS 用的是同一套 libavformat，成功率高。
2. **「播放清單 / 24-7 循環 / 插播」現成插件可做到八成。** playout-source 最接近需求；但它與 media-playlist-source 都只做「播放」，不做「大腦」——佇列、規則、排程仍要自己來。
3. **現階段不建議自己寫原生 OBS 插件。** 那是 3～6 週開發加上每版 2～5 天維護的投資，而現有工具（OBS Media Source + playout-source + obs-websocket + Advanced Scene Switcher）已能覆蓋需求。除非出現第七節列出的判準。

## 二、需求 → 誰來做

| 需求 | 現成 OBS / 插件能否勝任 | 建議負責元件 | 本版變化 |
| --- | --- | --- | --- |
| R1 24/7 播放清單循環 | ✅ playout-source / Media Source | OBS | 沿用 |
| R2 動態插播（臨時插入） | ✅ 插播場景 + A/B 雙源切換 | OBS + FastAPI | 調整 |
| R3 插播其他直播（RTMP/SRT/HLS） | ✅ Media Source 可直接吃 | OBS | 沿用 |
| R4 固定轉場（Stinger） | ✅ OBS 內建 Stinger | OBS | 沿用 |
| R5 WebUI 控制台 | ❌ OBS 不提供 | FastAPI + React（自製） | 沿用 |
| R6 疊首播日期文字 | ✅ Browser Source + obs-websocket 更新 | OBS + FastAPI | 沿用 |
| R7 直接從 YouTube 取片 | ❌ OBS 沒有 YouTube 來源 | **streamlink relay** | 新版重點 |
| R8 佇列 / 規則引擎 | ❌ | FastAPI（自製） | 沿用 |

關鍵：**R7 是本版唯一被「直接從 YouTube 拉」逼出來的新元件**，而它由 streamlink 解決，不是靠插件。

## 三、實測證據（本機 M1 Pro，2026-09-16）

本節全部是在規劃主機上實跑出來的結果，不是推論。

### 3.1 直連 URL 餵 ffmpeg → 403（此路不通）

yt-dlp 解出的 googlevideo URL 綁定「取 URL 時所用的 client 特徵 + 來源 IP」，直接丟給 ffmpeg 會被 403 擋掉。也就是「yt-dlp --get-url 抽到的網址直接餵 OBS」這條捷徑不可靠。

### 3.2 哪個 player client 還能取得可播放的流（2026-09-16 實測）

| player client | 結果 |
| --- | --- |
| mweb | ✅ 成功 |
| web_embedded | ✅ 成功 |
| android_vr（yt-dlp 目前預設） | ❌ 下載階段 403 |
| tv | ❌ page needs to be reloaded |
| web / web_safari / ios | ❌ Requested format is not available |

結論：YouTube 對 client 的封鎖是動態的，因此**不要把 client 寫死在程式裡**，要能設定、失敗時自動更換。

### 3.3 streamlink relay 實測成功（本版最重要發現）

指令：

```
streamlink --player-external-http --player-external-http-port 8901 -o /dev/null <URL> best
```

實測結果：

- ffprobe http://127.0.0.1:8901/ → 取得 h264 640x360 + aac
- ffmpeg -i http://127.0.0.1:8901/ -t 6 -f null - → 回傳碼 0（可完整解碼）
- streamlink 日誌出現 Got HTTP request from Lavf/62.12.102 …，Lavf 即 ffmpeg 的 libavformat

而 OBS 的 Media Source 本質就是 ffmpeg_source，內部同為 libavformat。也就是說：**OBS 拉得到這個 relay，而且完全不需要寫任何插件。**

### 3.4 環境盤點

- 主機：<MODEL> / Apple M1 Pro / macOS 27.0（arm64）
- OBS：**尚未安裝**
- 已具備：yt-dlp 2026.07.04、ffmpeg 8.1.2、streamlink 8.4.0、VLC 3.0.23
- ⚠️ **本機目前區網 IP 是 <WORK_HOST>，不是規劃的 <TARGET_HOST>**；且上一輪看到的是工作機，代表位址是浮動取得的。若要固定在目標機，須在路由器做 DHCP 保留（或改設靜態 IP），否則服務位址會漂移，WebUI 與 relay 的連線設定都會跟著失效。

## 四、現成 OBS 插件盤點

先回答最直接的問題：**有沒有插件能直接吃 YouTube？** 沒有。OBS 生態裡沒有官方或主流社群插件把 YouTube 當播放來源；原因是 3.1 的「URL 短時效 + client/IP 綁定」，社群共識是先解出或轉成一般串流，再讓 OBS 當一般來源拉。以下是與需求相關、值得評估的插件。

### playout-source（Exeldro，GPL-2.0，0.0.4 / 2025-04，有 macOS universal pkg）— 最推薦先試

- 性質：OBS 內的「CasparCG 縮小版」。內部建立多個私有 ffmpeg_source，支援項目之間的轉場、section、loop、auto play、speed、seek_start。
- 可覆蓋：R1（循環播放清單）、R4（項目間轉場可用它內建的）。
- 限制：沒有 hotkey / proc handler，外部程式無法直接遙控它的內部佇列；控制要回到 OBS 場景 / filter 層。
- 結論：**作為「主片循環」的引擎先試它**，插播與規則仍走 OBS 場景切換 + FastAPI。

### media-playlist-source（CodeYan01，GPL-2.0，0.1.3）— 不建議當主引擎

證據：

- Issue #50（未關）：OBS 32 起，音訊不論來源是否可見都會出現在所有音軌——對「插播要能獨立混音」的架構是致命 bug。
- PR #60（未關）：作者自承換檔會出現透明 / 空白幀，需要 A/B 雙源才能無縫——代表它自己也不保證 seamless。
- Issue #55「arm64 support?」講的是 **Raspberry Pi**，不是 macOS（上一版誤讀，此處更正）。

結論：可當備案，不當主引擎。

### Advanced Scene Switcher（WarmUpTill，GPL-2.0，1.36.1 / 2026-08，活躍，universal）— 保險絲

- 具備 Http / Websocket / Run / Media / Script 等 action，可在 OBS 內做條件判斷（例如「主片播完 → 換場景」）。
- 用途：當 FastAPI 之外的第二層保險，或快速原型。

### VLC 源（OBS 內建）— 已否決用來吃 YouTube

VLC 於 2025-09-28 移除 share/lua/playlist/youtube.lua（commit cd94dcaec949，訊息 So we can fallback to YT-DLP），已進 3.0.x 分支。因此 VLC 源不再內建解析 YouTube，不能靠它直接吃 YouTube URL。

### 沒有找到的部分

沒有任何插件提供「規則引擎 / 佇列 / 排程 / WebUI」——這些一律靠外部大腦（FastAPI）加上 obs-websocket。

## 五、建議架構 v3

```
YouTube（同一團體、已授權影片）
    │  streamlink：解流 + 本機 HTTP relay（127.0.0.1:8901）
    ▼
OBS Studio arm64
  ├─ Media Source（主片循環；必要時改用 playout-source）
  ├─ Media Source / 場景（插播、其他直播 RTMP/SRT/HLS）
  ├─ Browser Source（首播日期等疊字）
  └─ Stinger 轉場
    ▲ obs-websocket（控制面）
    │
FastAPI（大腦）：佇列 / 規則 / 排程 / relay 生命週期 / SQLite
    ▲ REST + WebSocket
    │
React WebUI
```

三個關鍵決策：

1. **YouTube 一律走 relay，不讓 OBS 直接碰 YouTube URL。** 由 streamlink 開一個固定 port 的 relay，OBS 只認 http://127.0.0.1:8901/。理由：3.1 的 403、短時效 URL、client 變動都被 relay 吸收；換片時只要重起 relay，OBS 端設定不變。
2. **主片插播用 A/B 雙 Media Source，不要單源換檔。** 證據是 media-playlist-source #60 顯示單源換檔會出透明 / 空白幀。做法是兩個 source 都 preload，切場景時才顯示，畫面才 seamless。
3. **佇列推進用事件驅動，不用計時器。** obs-websocket 提供 MediaInputPlaybackEnded 事件與 GetMediaInputStatus；用事件推進比「按片長計時」可靠（直播長度與轉檔長度會有誤差）。

obs-websocket 可用 API（已查證）：

- SetInputSettings / TriggerMediaInputAction / GetMediaInputStatus / SetMediaInputCursor / OffsetMediaInputCursor
- 事件：MediaInputPlaybackEnded
- CallVendorRequest

而 VLC 源、playout-source、media-playlist-source 都實作了 obs_source_info.media_*，所以 websocket 能直接控制它們的播放清單，不必繞 hotkey。

## 六、自製 OBS 插件的可行性

三條路線：

### A. 原生 C/C++ 插件（obs-plugintemplate）

- 需求：Xcode 16 + CMake 3.30.5 + macOS 14.5+
- 成本：估 3～6 週開發，之後每版 2～5 天維護（OBS ABI / API 會變動）
- 適合：需要 frame-accurate、需要在 OBS 內畫自訂 UI、要交付給別人安裝的產品

### B. Lua / Python 腳本（OBS 內建 scripting）

- 官方文件明列 **Script Sources (Lua Only)**，可註冊新的 source 型別；Lua 有 obs_source_media_* 與 proc_handler_call；Exeldro 的 obs-lua 有 media-cue.lua 等範例。
- 限制：無法註冊 websocket vendor。
- 成本：數天～2 週，但彈性受限。

### C. 寄生 Advanced Scene Switcher 的 Script action 跑 Python

- 可透過 py 綁定拿到 libobs 全 API，但要寄生在 ASS 上，極度耦合。
- 不建議長期使用，適合一次性 spike。

### 我的判斷：現階段不要寫原生插件

判準（出現任一項才重新評估）：

1. 轉場會出現黑格，且現成解法（A/B 雙源）壓不下來。
2. 需要 frame-accurate 的無縫接點（例如硬切必須零誤差）。
3. 要交付給其他人安裝的產品（而非自用）。
4. 需要在 OBS 內畫自訂 UI。

若真要寫，最小可行設計：

- 插件內部維護 A/B 兩個私有 ffmpeg_source，preload 後才切（解決 seamless）。
- 用 obs_websocket_register_vendor 開 CallVendorRequest，讓外部 FastAPI 呼叫它。
- 佇列本身**不要放進插件**，放在 FastAPI，插件保持薄。

### 授權

- 自製的 FastAPI / WebUI 可採 MIT。
- OBS 本體是 GPL-2.0；用 obs-websocket 以獨立程序控制，**不構成 copyleft 傳染**。
- 若真要散布原生插件，須採 GPL-2.0 相容授權。
- 影片本身：同一團體作品且已授權，這條風險已解除；但仍要遵守 YouTube 服務條款對直播的要求（見第七節）。

## 七、風險與待驗證

- **Relay 長時間穩定性**：streamlink relay 跑 24 小時會不會累積延遲 / 斷流？→ spike 8.1。
- **OBS 控制面**：改 Media Source 的 settings（例如換 URL）會不會中斷正在播的項目？→ spike 8.2。這題會決定「換片」要不要靠 A/B 雙源。
- **轉場黑格**：Stinger 與 source 切換是否出現單幀黑 / 透明？→ spike 8.3。
- **YouTube 政策**：先前擔心的「12 小時」是 **VOD 歸檔**限制，不是直播時長限制；24/7 直播本身被允許。上限為每頻道 10 路直播、每串流金鑰 3 路。→ 可降級為一般注意事項。
- **IP 漂移**：目前工作機（曾工作機），目標目標機 → 必須做 DHCP 保留。
- **player client 漂移**：3.2 顯示 client 可用性會變 → relay 層要做「client 可設定 + 自動重試換 client」。

## 八、三個 spike（建議依序做）

- **8.2 obs-websocket 控制面（優先）**：驗證「改 settings 是否中斷播放」＋「事件驅動推進佇列」。這題直接決定架構要不要 A/B 雙源。
- **8.3 轉場黑格逐格檢視**：用 ffmpeg 錄下切換瞬間，逐格檢查有無黑 / 透明幀。
- **8.1 streamlink relay 24 小時耐久測試**：量測延遲漂移與斷流次數（耗時長，可最後做）。

## 九、決策摘要

一句話：**架構由「FastAPI + CasparCG + OBS」改為「streamlink relay（取值）→ OBS Studio arm64（播放）→ FastAPI（大腦）→ React WebUI」，且現階段不寫自製插件。**

- 放棄：CasparCG（不支援 Apple Silicon）、SPX-GC（付費 API 且不做播放）、「yt-dlp 直連 URL 餵 OBS」（403）。
- 新增：streamlink relay（本版最重要，且零插件）。
- 沿用：OBS + obs-websocket + Browser Source 疊字 + Stinger。
- 自製：僅 FastAPI（佇列 / 規則 / 排程 / WebUI）與 React WebUI，皆可 MIT。
- 先試：playout-source 當主片循環；A/B 雙 Media Source 當插播。

## 附錄：驗證指令

以下指令都在本機（M1 Pro）實跑過，對應第三節。

A) player client 矩陣（3.2）：迴圈測試 tv / web_safari / web / ios / android_vr / mweb / web_embedded，完整見 work/probe2.sh

B) 直連 URL 403（3.1）：見 work/probe.sh

C) streamlink relay（3.3）：

```
streamlink --player-external-http --player-external-http-port 8901 -o /dev/null <URL> best
ffprobe http://127.0.0.1:8901/
ffmpeg -i http://127.0.0.1:8901/ -t 6 -f null -
```

完整見 work/probe4.sh

D) 環境（3.4）：

```
yt-dlp --version; ffmpeg -version | head -1; streamlink --version; sw_vers -productVersion
```

