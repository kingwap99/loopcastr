# YouTube 直播自動化 v4.2 — 聯播（方案 A：行程接力）實測報告

> 版本：v4.2（2026-09-16 02:20，取代 v4.1 草稿）
> 專案：yt_playlist2yt_Livestream
> 測試主機：<MODEL> / Apple M1 Pro / macOS 27.0 / arm64（規劃主機 <TARGET_HOST>）
> 前置文件：`YouTube直播自動化-合併建議-v4.md`、`OBS-插件評估與自製插件可行性-v3.md`
> 本版性質：**可執行原型的實機量測**。每組數字都在這台機器上跑出來，來源逐條標明；「實測」與「推論」分開寫。
> ⚠️ **本版更正 v4.1 草稿的頭號結論**，請以本版為準（見 §1.1）。

環境：yt-dlp 2026.07.04 / ffmpeg 8.1.2 / streamlink 8.4.0 / python3 3.14.6 / MediaMTX 1.21.0。

---

## 一、先回答你的問題

> **「如果想同步某個正在直播的 YT，也就是聯播的話，目前這個架構可以運行？」**

**可以。方案 A（行程接力）已在本機完整跑通**：VOD → 轉場 → 直播 → 轉場 → VOD，全程走同一條 RTMP，接收端沒有斷線。但有四件事必須先接受：

| # | 事實 | 量測值 | 來源 |
| --- | --- | --- | --- |
| 1 | 接力可行 | 6 段接力（含直播段）全程跑通 | 實測 §3.1 |
| 2 | **「同步」做不到，只有「延遲轉播」** | 觀眾端落後原始直播 20–40 秒 | 實測 + 推估 §5 |
| 3 | 換手縫隙可以壓到 | **0.34 秒**（關鍵是「先開再停」） | 實測 §3.1 |
| 4 | **直播源本身是穩的**，不是「每 100 秒必斷一次」 | ffmpeg 直連 100 秒 ready 98.6%；120 秒長測 99.2% | 實測 §3.3 |

一句話：**架構成立，可以上線；但監控要看「位元組有沒有持續成長」，不能看 MediaMTX 的 `ready`——那是一個會騙人的指標（實測見 §3.6）。**

### 1.1 更正聲明（重要）

v4.1 草稿寫了兩句錯的：

> ✗「真正的瓶頸不是換手，是**直播源自己會斷**：100 秒內必斷一次，離線 2–5 秒。」
> ✗「建議：聯播主路徑用 **streamlink 管線**，yt-dlp 直連當備援。」

本輪用乾淨的重測推翻，逐條更正：

1. **直連 m3u8 反而是三種取流方式中最穩的**（ready 98.64%、最長離線 0.74 秒），streamlink（96.66%）與 yt-dlp 管線（94.01%）都比較差。120 秒長測更量到 99.24%。草稿的排序是反的。
2. 草稿引用「接力原型裡直播段開播約 31 秒後斷掉」當證據。那個時間點 ffmpeg 吐的是 `Cannot reuse HTTP connection for different host`（googlevideo 把後續請求導去不同 CDN 機房，ffmpeg 想沿用同一條 HTTP 連線失敗），但**它自己會 `retrying with new connection` 並恢復**。同參數跑 120 秒長測是完整播完的：rc=0、輸出 18339 KiB、time=00:02:00.01。
3. → **主路徑改為「ffmpeg 直連 m3u8 + `-c copy`，不加任何 reconnect 參數」，streamlink 降為備援**（理由見 §3.4）。
4. **看門狗還是要留，但理由完全不同**：不是「來源必斷」，而是「來源會偶發斷 0.3–0.7 秒」＋「manifest URL 有 6 小時生命週期」＋「來源端不可控」。
5. 另外，有一輪 OFF 組數據被前一輪的孤兒產生器污染（兩支 ffmpeg 在寫同一份 playlist），量出「斷源 25 秒完全沒事」的假結果。該輪證據仍在 `logs/stall-test-015937.log`。現在 `stall_test.py` 開跑前會先 `pkill -f tmp/hls` 並清空 `tmp/hls`，量到的才可信。

---

## 二、方案 A 是什麼（原型實作）

「行程接力」= 一段一段起獨立的 ffmpeg 行程，每一段推到**同一條**輸出路徑，靠接收端的 publisher 接管語意換手。原型在 `work/playout/`：

| 檔案 | 作用 |
| --- | --- |
| `relay.py` | 接力排程器＋看門狗＋墊片接管（純 Python 標準庫，無第三方依賴） |
| `playlist.json` | 六段接力劇本（VOD／直播／轉場） |
| `playlist-live.json` | 三段劇本（轉場 → 聯播 70 秒 → 轉場） |
| `playlist-stall.json` | 壓測用單段劇本（本機假直播源） |
| `assets/transition.mp4` | 轉場墊片（1280x720 30fps，H.264+AAC） |
| `mediamtx.yml` | 本機接收端設定（RTMP :1935 / HLS :8888 / API :9997） |
| `live_stability.py` | 三種取流策略的穩定度量測 |
| `live_long.py` | 120 秒長測（base / reconnect / liveedge 三變體） |
| `stall_test.py` | 確定性壓測：假直播源斷源 N 秒，量看門狗＋墊片效果 |
| `overlap_test.py` | 換手真相：同一路徑多 publisher 時到底發生什麼事 |
| `fakelive.sh` | 假直播源（2 秒 HLS 片段，可定時 SIGSTOP） |
| `logs/` | 每一輪的 ffmpeg stderr、事件時間軸、gap 報告 |

### 2.1 排程模型

每個 segment 產生三個事件，下一段可以提前開跑：

    prepare  (t - resolve_lead)   yt-dlp 解析，搶在換手前完成
    start    (t)                  起 ffmpeg，推 RTMP
    stop     (t + seconds)        收掉 ffmpeg

`overlap > 0` 時，下一段的 `start` 提前 `overlap` 秒發生，於是會出現「兩支 ffmpeg 同時在推同一條 RTMP」的重疊視窗。

### 2.2 兩種換手語意

- `overlap = 0`（原本定義的 A）：**先停再開**。接收端先看到 publisher 離線，等下一支連上才恢復。
- `overlap = 3`（建議的 A+）：**先開再停**。新的 publisher 先完成 RTMP 握手，接收端直接接管，舊的被踢掉。

第二種成立，是因為 MediaMTX 與 YouTube 的接收端都支援「同一路徑多 publisher 時以新連線接管」。MediaMTX 實測日誌：

    [RTMP] [conn 127.0.0.1:51655] opened                       <- 新 publisher 連上
    [path live/test] closing existing publisher                <- 主動踢掉舊的
    [path live/test] stream is available and online, 2 tracks  <- 路徑全程沒斷
    [RTMP] [conn 127.0.0.1:51651] closed: terminated           <- 舊 publisher 收工

**接收端從頭到尾都認為「有人在播」，所以離線時間趨近 0。** 這是整個方案裡最有價值的單一發現，A2 與方案 B（§4）都建立在它上面。

### 2.3 這一版新長出來的三個零件

1. **看門狗**（`--watchdog`，預設 3 秒）：盯 ffmpeg 的 `out_time_us`。停滯超過門檻就判定來源失效。
   —— 但**只有「數值確實變大」才算活著**：ffmpeg 停滯時會不斷重印同一個 `out_time_us`，看到那行不代表有進度（這是實作踩到的坑）。
2. **墊片接管**（`--filler-on-stall`）：來源停滯時，先用墊片在同一條路徑接管（0.34 秒換手），再背景每 5 秒探測來源，確認活了才切回直播。
   —— 直接重開來源沒有意義：來源還在斷，重開只是再斷一次（§3.6 的 OFF 組就是 4 次重開全部無效）。
3. **HLS 前進探測**（`_hls_advancing()`）：HLS 的存活判斷不能只看「讀得到」。來源卡住時舊的 playlist 照樣下載得到、也解得出好幾秒的既有片段，這是探測假陽性的來源。改要求播放清單的 media-sequence 與最後一段必須往前走。
4. **觀測尺**（`Observer.flow_windows()`）：量「`bytesReceived` 停止成長」的時段。這是本輪最關鍵的一把尺，理由見 §3.6。

---

## 三、實測數據

### 3.1 換手縫隙：overlap = 0 vs overlap = 3

測試腳本：6 段接力（VOD 20s → 轉場 4s → 直播 40s → 轉場 4s → VOD 20s → 轉場 4s），全部推到本機 MediaMTX，同時以 MediaMTX API 每 50ms 取樣 `ready`。

**接收端離線時間（已排除開場暖機與直播源斷線造成的缺口）：**

| 換手次數 | overlap = 0 | overlap = 3 |
| --- | --- | --- |
| 第 1 次 | 1.501 s | 0.339 s |
| 第 2 次 | 1.012 s | 0.339 s |
| 第 3 次 | 3.757 s | —（被直播源斷線蓋住） |
| 第 4 次 | 1.522 s | —（同上） |
| 第 5 次 | 0.287 s | —（同上） |
| **平均** | **1.62 s** | **0.34 s** |

- 循序接力（overlap=0）單次換手成本落在 **1.0–1.5 秒**；若該段 yt-dlp 解析拖得比較久（該次 4.17 秒），會放大到 **3.8 秒**。
- 重疊接力（overlap=3）把同樣的換手壓到 **0.34 秒**，殘值幾乎只是 MediaMTX 內部切換 publisher 的過場。
- 原始數據：`work/playout/logs/relay-gaps-overlap0.json`、`logs/relay-events-overlap0.jsonl`。

### 3.2 一個副作用：排程會漂移

該次 v1 的 yt-dlp 解析花了 4.43 秒（> `resolve_lead` 的 4 秒），v1 實質晚 2.9 秒才開始，但它的 `stop` 仍按原始時間表觸發，第一段被縮短。

**這是原型的已知缺陷，不是量測誤差。** 上線版要改成「以實際 start 時間往後推算」，不要啟動時就把整條時間軸算死。在 overlap>0 時影響有限（舊的反正會被接管），但長時間跑會累積。

### 3.3 直播源穩定度（**更正後的主路徑結論**）

同一支 NASA ISS 直播（`M3HKLzjvKPc`，1280x720 59.94fps，itag 300），三種取流方式各推 100 秒到 MediaMTX：

| 策略 | ready 占比 | 斷線次數 | 最長離線 | 離線總計 |
| --- | --- | --- | --- | --- |
| **ffmpeg 直連 m3u8（`-c copy`）** | **98.64 %** | 1 | **0.74 s** | **1.28 s** |
| streamlink 管線 → ffmpeg | 96.66 % | 1 | 2.01 s | 2.98 s |
| yt-dlp 管線 → ffmpeg | 94.01 % | 1 | 5.10 s | 5.63 s |

再加一組 120 秒長測（同一支直播）：

| 變體 | ready 占比 | 最長離線 | rc | 說明 |
| --- | --- | --- | --- | --- |
| **base（等同 relay.py 現行寫法）** | **99.24 %** | **0.43 s** | 0 | 不加任何額外參數 |
| `-live_start_index -1` + 放寬超時 | 96.53 % | 3.59 s | 224 | 貼近直播邊緣反而更差 |
| 加 `-reconnect*` 系列 + 關 `http_persistent` | **0.0 %** | 180.49 s | —（跑到被外部收掉） | 見 §3.4，這是反例 |

**結論（實測）：聯播主路徑 = `ffmpeg -i <m3u8> -c copy`，不加 reconnect 參數。streamlink 當備援。**
原始數據：`logs/live-stability.json`、`logs/live-long.json`。

### 3.4 反例：加 `-reconnect` 會把直播段打掛

`logs/long-reconnect.log` 整份長這樣（不斷重複）：

    [https @ 0x777a810000] Will reconnect at 9360 in 0 second(s), error=End of file.
        Last message repeated 618 times
    [https @ 0x777a810000] Will reconnect at 8044 in 0 second(s), error=End of file.
        Last message repeated 600 times

- ready 占比 **0.0 %**，180 秒完全沒有推出去任何東西，CPU 空轉，`rc` 從缺（要外部 kill）。
- 原因：直播 HLS 播放清單走到尾端時本來就會出現「End of file」，`reconnect` 把它當成連線錯誤並立刻無限重連，等於把自己卡死在迴圈裡。
- **可攜結論：直播 HLS 來源不要加 `-reconnect`。** 要處理斷線，用 §2.3 的「看門狗 + 墊片接管」，那是行程層的做法，比 HTTP 層的 reconnect 可靠得多（而且斷了能看到、能告警）。

### 3.5 keepalive 的假警報（為什麼不能只看 stderr）

跑得最順的 base 長測，stderr 裡照樣有這兩行：

    [https @ ...] Cannot reuse HTTP connection for different host: rr2---sn-un57enel... != rr3---sn-ipoxu-un5el...
    [in#0/hls @ ...] keepalive request failed for '<...>/seg.ts' with error: 'Invalid argument'
    when opening url, retrying with new connection

**但整段是正常播完的。** 這只是 ffmpeg 對 googlevideo 多機房輪替的內部處理，它自己就接回去了。
→ **不要把單行 stderr 當故障訊號**；判斷健康要看「資料有沒有持續成長」（見 §3.6）。

### 3.6 看門狗與墊片接管：確定性壓測（本輪核心實驗）

方法：`fakelive.sh` 產生本機 HLS 假直播源（2 秒片段、`hls_list_size 6` = 12 秒窗），在開播第 20 秒用 `SIGSTOP` 凍住產生器 25 秒（等同來源完全斷訊），relay 以 `--watchdog 3 --retry-interval 5` 推流；Observer 每 50 ms 取 MediaMTX API。

| 設定 | 接收端 `ready` 離線時間 | **最長「資料零成長」** | restarts | 實際發生的事 |
| --- | --- | --- | --- | --- |
| **墊片接管：開** | 1.617 s（僅開播前空窗） | **4.611 s** | 1 | 斷源 3.1 秒後切墊片，之後畫面持續有動 |
| **墊片接管：關** | 1.590 s（僅開播前空窗） | **22.662 s** | **4** | `ready` 全程正常，但畫面實際凍住 22.7 秒；每 7.5 秒盲目重開一次，四次全部無效 |

同一實驗在前一輪以相同參數跑過一次，結果一致（可重現）：

| 設定 | 最長零成長（第一輪 / 第二輪） | restarts |
| --- | --- | --- |
| 墊片開 | 4.737 s / **4.611 s** | 1 |
| 墊片關 | 24.617 s / **22.662 s** | 4 |

**兩個關鍵發現：**

1. **`ready` 是會騙人的指標。** 兩組的 `ready` 離線時間幾乎一樣（1.62 s vs 1.59 s，而且都只是開播前的暖機空窗），但實際畫面一個凍 4.6 秒、一個凍 22.7 秒。來源斷了但 publisher 還掛著時，MediaMTX 的 `ready` 依然是 True。**所以監控必須盯「位元組有沒有持續成長」。**
2. **墊片接管的價值：把「畫面真的凍住」從 22.7 秒壓到 4.6 秒**（同一輪內、同樣的斷源條件）。這是「為什麼一定要做墊片接管」的硬證據。

墊片開（`logs/stall-test-020651.log`）時間軸：

    [02:07:00.587] INFO  start p1     live
    [fakelive] source STALLED at 02:07:14
    [02:07:16.343] WARN  p1     輸出停滯 3.1s（門檻 3.0s）
    [02:07:16.343] INFO  p1     切墊片 assets/transition.mp4（takeover）
    [fakelive] source RESUMED at 02:07:39
    [02:07:47.417] INFO  start p1     live   (restart #1)
    [02:07:47.930] INFO  p1     來源恢復，切回直播

墊片關（`logs/stall-test-020802.log`）時間軸——注意四次重開，資料還是沒動：

    [02:08:11.568] INFO  start p1     live
    [fakelive] source STALLED at 02:08:24
    [02:08:27.502] WARN  p1     輸出停滯 3.2s（門檻 3.0s）   -> restart #1
    [02:08:35.075] WARN  p1     輸出停滯 3.0s（門檻 3.0s）   -> restart #2
    [02:08:42.656] WARN  p1     輸出停滯 3.1s（門檻 3.0s）   -> restart #3
    [fakelive] source RESUMED at 02:08:49
    [02:08:50.217] WARN  p1     輸出停滯 3.0s（門檻 3.0s）   -> restart #4
    ...
    "stagnation_max": 22.662   （從 02:08:27.665 到 02:08:50.327）

### 3.7 反應時間分解（本輪量到的延遲清單）

| 環節 | 實測值 | 備註 |
| --- | --- | --- |
| 來源斷 → 看門狗判定 | **3.1 s** | 等於門檻 3.0 s，判定本身幾乎不花時間 |
| 判定 → 墊片第一筆資料出去 | **約 1.5 s** | 含墊片開檔＋RTMP 握手＋接管 |
| **合計：斷源 → 畫面恢復** | **約 4.6 s** | 就是上表的 `stagnation_max` |
| 來源恢復 → 切回直播 | **約 9 s** | `retry-interval 5 s` ＋ 探測約 4 s；是現行最慢的一環 |
| ffmpeg 對來源斷訊的自我緩衝 | **幾乎沒有**（≲1 s） | 各輪都是斷源後 3.1 秒就判定停滯，代表 ffmpeg 已追到直播邊緣、手上沒有多餘片段 |

**門檻怎麼設（推論）：** 來源正常時，HLS 是「整段拉、整段送」，每隔一個片段長度本來就會有一段零成長。本機假源是 2 秒片段，實測正常零成長就是 2.0–2.05 秒，所以門檻設 3 秒剛好。YouTube 直播常見 5 秒片段，**推論門檻要設 8–10 秒才不會誤觸發**（這個還沒在真實 YT 直播源上壓測過，列為待辦）。

### 3.8 附帶發現：這台機器的 ffmpeg 沒有 `drawtext`

    $ ffmpeg -filters | grep -E 'drawtext|subtitles|ass'
    （無輸出）
    $ ffmpeg -version | grep libfreetype
    （無輸出）

Homebrew 的 ffmpeg 8.1.2 編譯時未啟用 `libfreetype`，所以 `drawtext`、`subtitles`、`ass` 全部不存在；可用的只有 `overlay`、`drawbox`、`drawgrid`。
**修正：疊字必須走「預先產生的 PNG + `overlay` 濾鏡」**（v4 決策 3 的另一個選項），或另外編一個帶 freetype 的 ffmpeg。這也連帶影響 §5：疊字一定得重編，無法 `-c copy`。

---

## 四、聯播的三種模式與取捨

| 模式 | 做法 | 換手縫隙 | 疊字 | 額外常駐服務 | 適用時機 |
| --- | --- | --- | --- | --- | --- |
| **A1 循序接力** | 停舊 → 開新 | 1.0–1.6 s | 可（需重編） | 無 | 最低成本，可接受短暫黑畫面 |
| **A2 重疊接力（本輪建議）** | 先開新 → 接收端接管 | **≈ 0.34 s** | 可（需重編） | 無 | 本機現況，改一行參數就有 |
| **B 本機匯流排** | 來源全推 MediaMTX，輸出端單一長命 ffmpeg 只訂閱匯流排 | 0（對 YouTube 完全無感） | 可 | 需 MediaMTX 常駐 | 追求 YouTube 端零重連 |

三種的**觀眾端延遲都是同一量級**（見 §5），差別只在換手瞬間好不好看，以及 YouTube 端會不會看到 publisher 重連。
本次只做 A，但 **B 的關鍵零件已經就位**：MediaMTX 1.21.0 已裝好並在本機跑起來（RTMP :1935 / API :9997）。A2 要升級成 B，只要把「各段直接推 target」改成「各段推 MediaMTX，另起一支固定 ffmpeg 從 MediaMTX 拉到 YouTube」——**A2 的程式碼幾乎可以直接沿用**。

---

## 五、延遲：為什麼「同步」做不到

聯播是「拉別人的流 → 再推出去」，兩段延遲會疊加：

| 延遲來源 | 量級 | 依據 |
| --- | --- | --- |
| YouTube 直播 HLS 本身的分段緩衝 | 約 3 個 segment | 實測 manifest `playlist_duration/30`、segment `dur/5.005`，ffmpeg 從 live edge 往後退約 3 段 |
| 本機 relay 的讀取/寫入緩衝 | 1–3 s | 實測 ffmpeg 起播到 MediaMTX 收到首個 byte 的間隔 |
| YouTube 對「我們這條新流」的 ingest + 轉碼 | 數秒到十餘秒 | 推估（未實測） |
| 合計（觀眾看到的落後量） | **約 20–40 秒** | 推估，尚缺一個帶時鐘的實測來源 |

「同步」在物理上需要我們與來源共用同一個時間基準，而 YouTube 直播不提供這個介面，**所以能做到的只有延遲轉播**。你的頻道與來源頻道會是兩條獨立時間軸，這是架構的本質限制，不是實作問題。

---

## 六、v4.2 架構（把 Relay 掛進去）

    YouTube（同一團體、已授權的作品）
         │
         ▼
    ┌──────────────────┐
    │ ① Resolver        │  yt-dlp -g（需指定可用 client）
    │                  │  主：ffmpeg 直連 m3u8；備：streamlink relay
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ ② Normalizer      │  選配。要疊 PNG／統一響度才走進來
    │  （需重編）        │  VideoToolbox 硬編；不疊字則整段略過
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ ③ Relay / Bus     │  ★本版新增：relay.py
    │                  │  ├ 靜態段：VOD / 轉場墊片
    │                  │  ├ 聯播段：m3u8 → -c copy（零重編）
    │                  │  ├ 看門狗：盯 bytesReceived 成長，不看 ready
    │                  │  └ 墊片接管：斷源時先墊，來源活了再切回
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ ④ Playout         │  RTMP 推 YouTube Live（overlap 換手）
    └────────┬─────────┘
             ▼
       YouTube Live
             ▲
    ┌──────────────────┐
    │ ⑤ Supervisor      │  relay.py 現為 CLI；上線版由 FastAPI 接手
    │                  │  負責：排程、看門狗、重連、狀態、告警
    └────────┬─────────┘
             ▼
          WebUI

與 v4 的差異：**多了一層 ③ Relay，它是「可切換來源」的層。** v4 的 Bus 只處理靜態段串接，v4.2 的 Relay 要同時處理「靜態段 + 正在直播的外部流 + 斷源時的墊片接管」。

---

## 七、上線前必做清單（已依本輪數據重新排序）

1. **URL 生命週期與來源 IP 綁定（最高優先）**
   實測 `expire` − 目前時間 = **21602 秒（恰好 6 小時）**，且 URL 內含 `ip=<PUBLIC_IP>`，**綁來源 IP**。
   → 長時聯播中途斷線必須重新解析，不能用啟動時抓到的舊 URL；換網路（Wi-Fi 重連、IP 變動）手上的 URL 直接失效。
   → 規劃主機的區網 IP 在測試期間一直在 `目標機 / 工作機 / 工作機` 之間漂，**建議在路由器做 DHCP 保留**。
2. **看門狗門檻依來源片段長度設定**（本機 2 秒片段 → 3 秒可用；YT 直播 5 秒片段 → 推論 8–10 秒，待實測）。
3. **來源恢復偵測太慢**（實測約 9 秒才切回直播）。縮短 `retry-interval`，或用更輕的探測（只比對 playlist 的 media-sequence，不下載片段）把探測時間從 4 秒壓下來。
4. **排程漂移修正**（§3.2）：改成以實際 start 時間往後推算。
5. **疊字與零重編的取捨**：要疊首播日期／Logo 就得進 Normalizer 重編（且本機 ffmpeg 沒有 `drawtext`，只能走 PNG + `overlay`）；`-c copy` 聯播最省但畫面純淨。兩者互斥，先決定聯播時要不要疊字。
6. **MediaMTX API 輪詢噪音**：50ms 輪詢太兇，上線版降到 0.2–0.5 秒即可（本版已改用 `/v3/paths/list` 一次列全部，不再產生 404 噪音）。
7. **延遲實測**：目前缺一個畫面帶時鐘的直播來源來驗證 §5 的 20–40 秒。

---

## 八、附錄

### 8.1 重現指令

    # 0) 起本機接收端（RTMP :1935 / HLS :8888 / API :9997）
    /opt/homebrew/opt/mediamtx/bin/mediamtx work/playout/mediamtx.yml

    # 1) 乾跑：只印排程與 ffmpeg 指令，不真的推流
    python3 work/playout/relay.py --dry-run

    # 2) 六段接力（含直播段）：循序 vs 重疊
    python3 work/playout/relay.py --playlist work/playout/playlist.json --overlap 0 --observe
    python3 work/playout/relay.py --playlist work/playout/playlist.json --overlap 3 --observe

    # 3) 換手真相：同一條路徑上多 publisher 接管
    python3 work/playout/overlap_test.py

    # 4) 直播源取流策略穩定度（各 100 秒）＋ 120 秒長測
    python3 work/playout/live_stability.py
    python3 work/playout/live_long.py 120

    # 5) 看門狗＋墊片接管壓測（確定性；每輪約 63–86 秒）
    cd work/playout
    python3 stall_test.py 20 25 110 80 on      # 墊片開
    python3 stall_test.py 20 25 110 80 off     # 墊片關

### 8.2 證據檔案對照

| 結論 | 檔案 |
| --- | --- |
| 換手 0.34 s、MediaMTX takeover | `logs/relay-gaps-overlap0.json`、`logs/relay-events-overlap0.jsonl`、`logs/ov-live.err` |
| 三策略穩定度 | `logs/live-stability.json`、`logs/ls-s1.log`、`logs/ls-s2.log`、`logs/ls-s3.log` |
| 120 秒長測（含 reconnect 反例） | `logs/live-long.json`、`logs/long-base.log`、`logs/long-liveedge.log`、`logs/long-reconnect.log` |
| 墊片開／關對照（本輪） | `logs/stall-test-020651.log`、`logs/stall-test-020802.log`、`logs/repro-on.txt`、`logs/repro-off.txt` |
| 墊片開／關對照（前一輪，可重現） | `logs/stall-test-020214.log`、`logs/stall-test-020325.log`、`logs/gaps-on-r1.json`、`logs/gaps-off-r1.json` |
| 被污染的假結果（勿引用） | `logs/stall-test-015937.log` |

### 8.3 這份報告的實測 / 推論分界

- **實測**：§3.1 換手縫隙、§3.3 穩定度兩張表、§3.4 reconnect 反例、§3.5 keepalive 假警報、§3.6 墊片開關對照、§3.7 前三列反應時間、§3.8 ffmpeg 濾鏡盤點。
- **推論**：§3.7 門檻在 YT 直播源上應該設 8–10 秒、§5 觀眾端 20–40 秒延遲、§4 方案 B 的 YouTube 端零重連。

