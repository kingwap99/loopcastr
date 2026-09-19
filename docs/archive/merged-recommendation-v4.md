# YouTube 直播自動化 — 合併建議 v4

> 版本：v4（2026-09-16）
> 專案：yt_playlist2yt_Livestream
> 目標主機：<TARGET_HOST>（<MODEL> / Apple M1 Pro / macOS 27.0 / arm64）
>
> 本版合併三份來源：
> 1. 第一份分享連結（原始需求與 CasparCG / SPX-GC 盤點）。
> 2. 第二份分享連結**最後一輪（2026-09-16 00:22）的結論**：改為「YouTube Playlist → 24/7 YouTube Live Automation Server」，全用 FastAPI + FFmpeg + yt-dlp + HTML Overlay，不依賴 CasparCG / OBS / SPX-GC。
> 3. 我在**規劃主機上的實測**（本版新增，且推翻了上一版一個重要結論）。
>
> 使用者決策：**不管第三方插件，全部自己來。**

## 一、先講結論

1. **架構定調為「純 FFmpeg 管線」**，採納第二份連結最終結論：不自建 OBS 插件、不用 SPX-GC、不用 CasparCG。OBS 降為備案（例如日後想要手動導播時）。
2. **但該結論裡「yt-dlp -g 取到的 URL 直接餵 FFmpeg」需要加一道但書。** 我重測發現：這條路**可行**，但是否成功**取決於 player client**——上一版我說「直連一律 403」是錯的，本版更正（見第三節）。
3. **真正還沒被任何一份文件解決的是「24/7 連續推流不中斷」**。純 FFmpeg 若一支一支跑，每支之間 RTMP 會斷；v4 的核心新增就是 **Normalizer + 段接（concat）+ 用轉場影片遮蔽重連縫隙**。這是本版最有價值的一段。

## 二、兩份結論合併：一致、衝突與裁決

| 項目 | 我的 v3（OBS 路線） | 第二連結最終結論 | v4 裁決 |
| --- | --- | --- | --- |
| 播放引擎 | OBS Studio | FFmpeg | **FFmpeg** |
| 取片 | streamlink relay | yt-dlp -g | **yt-dlp 為主、streamlink 為備援** |
| 轉場 | OBS Stinger | FFmpeg 合成 | **FFmpeg（concat 或 filter_complex）** |
| 疊字 | OBS Browser Source | HTML Canvas → PNG | **PNG + drawtext** |
| 大腦 | FastAPI（一致） | FastAPI | **FastAPI** |
| 佇列模型 | 事件驅動 | Metadata / Runtime 兩層 | **採納兩層模型** |
| 狀態存放 | SQLite | SQLite | **SQLite** |
| 第三方插件 | 盤點後不用 | 不用 | **不用** |

兩份的差異其實只有一個：**播放層是 OBS 還是 FFmpeg**。既然你決定全部自己來，FFmpeg 勝出，理由是它沒有 GUI 依賴、適合 headless 24/7、且完全在我們控制之下。

## 三、本機重測：推翻上一版的「直連 403」結論

上一版（v3）我寫「yt-dlp 解出的 URL 直接餵 ffmpeg 會 403，此路不通」。**這句是錯的**，我把單一情境當成通則。本版在規劃主機上重測，結果如下（2026-09-16，測試影片 aqz-KE-bpKQ）。

### 3.1 直連 URL + 原生 ffmpeg：client 矩陣（漸進式格式 b[height<=720]）

| player client | yt-dlp -g 解析 | ffmpeg 直連解碼 |
| --- | --- | --- |
| default（預設） | ✅ | ✅ |
| android_vr | ✅ | ✅ |
| mweb | ✅ | ✅ |
| web_embedded | ✅ | ✅ |
| web_safari | ✅ | ✅ |
| tv | ❌ page needs to be reloaded | — |
| web | ❌ Requested format is not available | — |
| ios | ❌ Requested format is not available | — |

### 3.2 真正的地雷是 DASH URL，不是「直連」本身

當我改用自適應格式（bv*[height<=720]+ba/b，也就是高畫質必用的「video 與 audio 分開」）時：

| player client | 取得的 URL 數 | video URL | audio URL |
| --- | --- | --- | --- |
| default | 2 | ❌ 403 Forbidden | ❌ 403 Forbidden |
| android_vr | 2 | ❌ 403 Forbidden | ❌ 403 Forbidden |
| web_embedded | 2 | ✅ OK | ✅ OK |
| mweb | 1（退回漸進式） | ✅ OK | — |
| web_safari | 解析失敗 | — | — |

**結論：403 不是「直連」的原罪，而是「default / android_vr 這兩個 client 產生的 DASH URL 會被拒」。** 換成 web_embedded 就正常。這代表：

- 若只用 720p 以下的漸進式單檔，直連幾乎都能過。
- 若要 1080p 以上（一定要 DASH），**client 必須挑選並在啟動時驗證**，不能寫死。

### 3.3 三條餵流路徑都可用，各有適用場合

| 路徑 | 實測 | 優點 | 風險 |
| --- | --- | --- | --- |
| yt-dlp -g → ffmpeg 直連 | ✅（需指定可用 client） | 最省資源、零下載、可 seek | DASH URL 依 client 而異、會過期、綁 IP |
| yt-dlp -o - \| ffmpeg pipe:0 | ✅ | 由 yt-dlp 自己處理 HTTP，避開 403 問題 | 無法 seek、單向 |
| streamlink relay | ✅ | 最穩、有重試機制、OBS 類播放器都吃 | 多一個行程 |

這三條我都在本機跑通了（指令見附錄）。**建議：Resolve 層以 yt-dlp -g 為主，但把 streamlink relay 與 pipe 當作 fallback**，任何一條失敗就換下一條。

### 3.4 段接（bus）測試：這是 v4 才發現的真問題

我測了「把多段已正規化的 MPEG-TS 串成連續流」：

- 直接 cat 兩段 TS → **ffmpeg 報 non-monotonically increasing dts**，代表時間戳重疊，不能這樣接。
- 用 concat demuxer（-f concat -c copy）→ 乾淨輸出 6.01 秒、無警告、解碼 rc=0。

**所以：段接要用 concat demuxer（或等價的時間戳重基準），不能用檔案串接。**

## 四、v4 建議架構（純 FFmpeg，自建）

```
        YouTube（同一團體、已授權）
             │
   ┌─────────┴─────────┐
   │  ① Resolver       │  yt-dlp -g（驗證過的 client）／pipe／streamlink relay
   └─────────┬─────────┘
             ▼
   ┌───────────────────┐
   │  ② Normalizer     │  每支影片 → 統一到固定格式（例：H.264 720p30 + AAC 48k 立體聲）
   │  （新增，關鍵）   │  含 loudnorm 音量正規化
   └─────────┬─────────┘
             ▼
   ┌───────────────────┐
   │  ③ Overlay        │  PNG / drawtext：首播日期、標題、Logo、跑馬燈
   └─────────┬─────────┘
             ▼
   ┌───────────────────┐
   │  ④ Bus / Stitcher │  concat demuxer 串接；轉場影片夾在中間
   └─────────┬─────────┘
             ▼
   ┌───────────────────┐
   │  ⑤ Playout        │  單一 ffmpeg：-c copy → RTMP 推 YouTube Live
   └─────────┬─────────┘
             ▼
        YouTube Live

   ▲ 以上全部由 FastAPI（⑥ Supervisor）控制
   │  佇列 / 規則 / 排程 / 直播插播 / relay 生命週期 / SQLite
   ▼
   React WebUI（http://<TARGET_HOST>:8000）
```

與第二連結結論的 6 個模組對應：①=Stream Resolver、②=新增、③=Overlay Engine、④⑤=新增（把「一支一支跑」升級成「連續 bus」）、⑥ 取代它原本的 Transition Engine 與 Queue Engine 的一部分。Playlist Sync、Queue Engine、WebUI、Schedule 全部沿用。

## 五、三個關鍵設計決策

### 決策 1：為什麼要 Normalizer，而不是一支影片一支 ffmpeg

不同影片的解析度、fps、音訊取樣率都不同。若直接接續，ffmpeg 無法用 -c copy 串接，且播放器端會出現格式跳動。**先正規化到同一組編碼參數，之後才能一路 -c copy 到 RTMP**，CPU 花費集中在這一層（M1 Pro 可用 VideoToolbox 硬體編碼）。

### 決策 2：轉場影片一魚兩吃

你原本要的「固定插入轉場影片」在這裡剛好兼任第二個角色：**遮蔽 RTMP 重連的縫隙**。當需要插播直播或重啟 playout 時，先播 1～2 秒轉場，觀眾看到的是正常轉場，而不是黑畫面。這讓「每 N 支重連一次」的策略在觀感上變得可接受。

### 決策 3：Overlay 不用 OBS Browser Source

第二連結建議 HTML Canvas → PNG。方向可以，但要注意 ffmpeg 不是每幀去跑瀏覽器。務實做法：

- **每支影片固定不變的**（首播日期、標題）→ 在 Normalizer 階段用 overlay filter 疊一張預先產生的 PNG，或直接用 drawtext。
- **需要動態變化的**（跑馬燈、倒數）→ drawtext 搭配 textfile 重載，或固定頻率重新產生 PNG 讓 ffmpeg 輪替。

## 六、對第二連結結論的取捨

| 第二連結的結論 | 我的評價 | 說明 |
| --- | --- | --- |
| 改用 FastAPI + FFmpeg + yt-dlp，不用 OBS / CasparCG / SPX-GC | ✅ 保留 | 與你的「自己來」一致，且 headless 友善 |
| yt-dlp -g 直接餵 FFmpeg，不用下載整支影片 | ⚠️ 修正 | 方向對，但必須指定並驗證 client；DASH URL 會 403 |
| 把播放 URL 視為短效 Token，播放前約 10 秒才解析（兩層架構） | ✅ 保留，且很好 | 這是整份結論裡我最認同的一段 |
| Queue 不是 YouTube Playlist，而是內部可插播的佇列 | ✅ 保留 | 正確 |
| Overlay 用 HTML Canvas 產生 PNG | ⚠️ 部分修正 | 靜態用 PNG/drawtext，動態用 drawtext+textfile |
| 6 個模組 / WebUI / API / 目錄結構 | ✅ 保留 | 可直接沿用為骨架 |
| （未提到）24/7 連續推流的邊界處理 | ➕ 補強 | v4 新增 Normalizer + Bus + 轉場遮蔽 |
| （未提到）音訊響度不一致 | ➕ 補強 | Normalizer 加入 loudnorm |
| （未提到）player client 可用性會漂移 | ➕ 補強 | Resolver 要做多 client 自動探測與 fallback |

## 七、風險與待驗證

- **RTMP 連續性**：這是最大的未知。要先確認「每 N 支重連」在 YouTube 端看起來是否可接受、觀眾端延遲多少。
- **DASH URL 的 client 漂移**：3.2 已證明會變。Resolver 需內建探測器，開機與每次解析失敗時自動換 client。
- **URL 過期**：googlevideo URL 是短效且綁 IP 的 token。第二連結的「播放前才解析」已正確處理；只要確保 24/7 播放中不會用到幾小時前解析的 URL。
- **音訊響度**：各影片原始音量不同 → Normalizer 必須含 loudnorm，否則直播音量忽大忽小。
- **資源**：M1 Pro 同時做「解碼 YouTube + 正規化編碼 + 推流」；1080p30 建議走 VideoToolbox 硬編，先量測 CPU/GPU 餘裕。
- **YouTube 政策**：先前擔心的「12 小時」是 **VOD 歸檔**限制，不是直播時長限制；24/7 直播本身被允許（每頻道 10 路、每串流金鑰 3 路）。影片為同一團體授權作品，這條風險已大幅降低，但仍需符合服務條款。
- **IP 漂移**：本機現在是 <WORK_HOST>（上一輪還是工作機），目標是目標機 → 必須做 DHCP 保留，否則 WebUI 與 relay 位址會失效。

## 八、下一步 spike（建議依序）

1. **端到端 30 分鐘不中斷測試**（最優先）：Resolver → Normalizer → concat → Playout → RTMP，跑 30 分鐘，記錄重連次數與觀眾端畫面。這一項決定整個架構是否成立。
2. **client 自動探測器**：開機時對 default / web_embedded / mweb / android_vr 各取一次 -g 並用 ffmpeg 驗 3 秒，挑第一個可用的並快取結果。
3. **Overlay + 轉場合成驗證**：確認 PNG overlay 與轉場影片在 Normalizer 階段能正確合成，且不影響 -c copy 段接。

## 九、決策摘要

一句話：**採納「純 FFmpeg 自動化伺服器」，但在它與 YouTube 之間補上 Resolver 的 client 驗證、Normalizer 的格式正規化、以及用 concat + 轉場遮蔽的連續推流層。**

- 定案：不自建 OBS 插件、不用 SPX-GC、不用 CasparCG；播放層用 FFmpeg。
- 更正：上一版「yt-dlp 直連 URL 一律 403」為誤判；實情是 DASH URL 依 client 而定（default/android_vr 403、web_embedded 可用）。
- 新增：Normalizer（統一格式 + loudnorm）、Bus（concat 段接）、轉場兼遮蔽重連。
- 沿用：FastAPI 大腦、SQLite、兩層（Metadata / Runtime）解析模型、React WebUI、目錄結構骨架。
- 最高風險：24/7 RTMP 連續性 → 由 spike 1 驗證。

## 附錄：本版重測指令與原始結果

A) 直連 URL × client 矩陣（3.1）：work/matrix.sh
   對 default / tv / web / web_safari / ios / android_vr / mweb / web_embedded 取 -g 後以 ffmpeg 解 4 秒。

B) DASH URL 測試（3.2）：work/dash.sh
   對 bv*[height<=720]+ba/b 的兩個 URL 分別解碼，觀察 403。

C) 三條餵流路徑（3.3）：work/verify_g.sh

```
yt-dlp --extractor-args "youtube:player_client=mweb" -f "b[height<=720]/b" -g <URL>
yt-dlp --extractor-args "youtube:player_client=mweb" -f "b[height<=720]/b" -o - <URL> | ffmpeg -i pipe:0 -t 5 -f null -
streamlink --player-external-http --player-external-http-port 8901 -o /dev/null <URL> best
```

D) 段接測試（3.4）：work/bus.sh、work/bus3.sh

```
ffmpeg -f concat -safe 0 -i list.txt -c copy all.ts
ffprobe -show_entries format=duration all.ts
```

原始結果：naive cat 出現 non-monotonically increasing dts；concat demuxer 輸出 6.0135 秒、無警告、解碼 rc=0。

