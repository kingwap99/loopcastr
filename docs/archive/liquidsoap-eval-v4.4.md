# YouTube 直播自動化 v4.4 — Liquidsoap 提案評估（可引用 / 須更正 / 待驗證）

> **本檔部分結論已被實測推翻。** §2.1（RTMP 輸出【待驗證】）與 §1（PNG overlay 為唯一路徑）已由 `docs/archive/liquidsoap-tests-v4.5.md` 實測更正：Liquidsoap 內建 `output.youtube.live.rtmp`，且可原生繪字。請以 v4.5 為準。

> 來源：ChatGPT 分享連結 `6aa99547-d6a4-83e9-9c7c-4c933c56570d`，2026-09-16 02:28 的使用者提問與回覆（Liquidsoap 能否補強）。本版不重做舊實測，只把該提案逐條對照既有實測結果。
>
> 標記規則：【實測】＝本輪或前輪在機器上真的跑出來、可重現；【待驗證】＝本輪沒做；【推論】＝依已知欄位或行為推導。

---

## 0. 那個連結新增了什麼

| 時間 | 角色 | 內容 |
|---|---|---|
| 2026-09-16 02:28 | 使用者 | 貼出 `https://github.com/savonet/liquidsoap`，問「這個可以拿來補強嗎？」 |
| 2026-09-16 02:28 | 回覆 | 主張把 Liquidsoap 放進架構，取代原本的 FFmpeg 播放控制層 |

回覆的三個核心主張：

1. Liquidsoap 當**長駐播放引擎**（動態 Queue、fallback、switch、crossfade、插播），FastAPI 只做決策與事件。
2. Queue **只存 videoId**，播放前約 30 秒才 resolve 成實際 URL。
3. Overlay 不用播放引擎畫，改成 HTML → PNG → 貼圖。

最終定位：**FastAPI（決策層）+ Liquidsoap（長駐播放引擎）+ yt-dlp（URL Resolver）**。

---

## 1. 可引用、而且被實測支持的部分

| 提案主張 | 我的實測結果 | 引用方式 |
|---|---|---|
| Queue 只存 `videoId`，不存解析後的 URL | URL 路徑含 `/expire/<unix>/`，值＝解析當下 + **21600 秒（6 小時）**；且含 `/ip/<PUBLIC_IP>`，**綁定解析當下的公網 IP** | 直接引用。並把提案較含糊的「有時效性」升級成可操作的數字：**6 小時 TTL ＋ IP 綁定** |
| 播放前才 resolve | 解析一次耗 **2.26–2.65 秒**；用「先解析新 URL → 新 publisher 接手 → 才收掉舊的」可做到 **6 次換手、接收端離線 0.000 秒** | 概念可引用。但**提前 30 秒不必要**，2.5 秒就夠；30 秒只是安全邊際，成本極低，可以接受 |
| 引擎維持一條長駐 pipeline，控制層只送事件 | 現有 `relay.py` 已是事件式：`_restart(planned=True)` 走換手、`planned=False` 走故障重試，`refreshes` 與 `restarts` 分開計數 | 可引用「概念」。但**不需要因此換引擎**——等效行為已經實測達成 |
| 不要用播放引擎畫 UI，改 HTML → PNG 疊圖 | 工作機 ffmpeg 是 brew `8.1.2_1`，`configuration` **沒有** `--enable-libfreetype`；`drawtext` / `subtitles` / `ass` 全部不存在 | **要從「建議」升級為「結論」**。PNG overlay 不是美感選擇，是這條工具鏈上唯一可行路徑 |

補充一條提案沒提到、但我實測到的：**解析端與發佈端要在同一台**。跨機共用同一條 URL 在現況可行（兩台同 NAT、同公網 IP，實測兩邊都 `http_code=200`、`3,371,253 bytes`），但一旦 PPPoE 重撥或改走不同出口，URL 內的 `ip=` 就對不上，而且在「同 NAT」的現況下測不出來。

---

## 2. 須更正或打折的部分

### 2.1 「YouTube RTMP 輸出 ✅」──【待驗證】，而且這一條會動搖整個提案

Liquidsoap 的原生輸出以 `output.icecast`、`output.harbor`、`output.srt`、`output.hls`、`output.file`、`output.external` 為主；要推 YouTube RTMP 一般得再包一層 ffmpeg（走 `output.ffmpeg` 或 `output.external`）。

若真是這樣，那「Liquidsoap 取代 FFmpeg 播放控制層」實際上是「**把 ffmpeg 包進 Liquidsoap 裡**」，淨增益會縮到只剩 queue 操作 API 與 fallback / crossfade 算子。這是整個提案最關鍵、也最需要先驗證的一點。

### 2.2 「可以跑在 macOS Apple Silicon」──【部分實測】

| 檢查 | 結果【實測】 |
|---|---|
| brew 是否有 liquidsoap | 有，`liquidsoap 2.4.5 (bottled)`，來源 `homebrew-core` |
| 依賴 | 只有 `ffmpeg` 一項 |
| bottle 標籤 | **只有 `arm64_tahoe`**，沒有其他 arm64 標籤 |
| 本機 OS | macOS **27.0** |
| 安裝熱度 | 近 30 天 **14** 次、近 365 天 **217** 次；**build-error 6 次** |

風險：若 macOS 27 不接受只標 `tahoe` 的 bottle，brew 會改成**原始碼編譯**（OCaml / opam 工具鏈）。這在 **8 GB** 的目標機上是實務風險，不是理論風險。安裝熱度也顯示 macOS 使用者基數非常小，踩到問題時可參考的資料少。

### 2.3 導入成本被低估：目標機沒有 homebrew

【實測】目標機目前 `homebrew`、`yt-dlp`、`ffmpeg`、`streamlink`、`MediaMTX` **全部未安裝**，內建 Python 是 3.9.6。

走 Liquidsoap 路線等於要在該機裝起整套 Homebrew 生態（brew → ffmpeg → liquidsoap），跟我 v4.3 §5.5 規劃的「免安裝靜態二進位」是兩條不同路線。這個決策成本提案完全沒提。

### 2.4 `youtube-pl:` 可以整段略過

提案自己也說不建議依賴。同意，而且理由更強：底層依賴 youtube-dl 系列解析、插播與排程仍得自建，而 Queue 只存 videoId 的做法（第 1 節）已經涵蓋同樣需求。

### 2.5 沒提到的：目標機只有 8 GB 記憶體

【實測】目標機是 `<MODEL>`、**8 GB RAM**（工作機的一半）。任何「單機長駐引擎」的提案都必須把這個數字算進去；併發條數請以動態內容 2～3 Mbps／條估算（v4.3 §4），不要用 Lofi Girl 的 0.71 Mbps 當通則。

---

## 3. 建議

**短期：不導入 Liquidsoap，只借用第 1 節的四個概念。** 其中三個（videoId-only Queue、事件式控制、換手零斷點）在 `relay.py` 已經實作並實測；第四個（PNG overlay）列為後續做字幕／台標時的唯一路徑。

**什麼時候再回頭評估**：需要「真正的 crossfade 轉場」或「多來源自動 fallback」時——那時 Liquidsoap 的算子才有不可替代的價值。

**導入前必過三關（依序，任一關失敗即否決）**：

| 順序 | 驗證項目 | 判定標準 |
|---|---|---|
| 1 | 在目標機（macOS 27 / arm64）`brew install liquidsoap` | 能否用 bottle 裝起、不落到原始碼編譯；裝完 `liquidsoap --version` 可執行 |
| 2 | `output.ffmpeg` 或 `output.external` 直推 YouTube RTMP | 能推上 `rtmp://a.rtmp.youtube.com/live2/<key>` 且對方收到 |
| 3 | `video.add_text` 在此 bottle 是否可用 | 需要 freetype；本機 ffmpeg 已經沒有，需確認 liquidsoap 是否自帶 |

---

## 附錄：本輪可重現的檢查指令

```sh
ssh <USER>@<TARGET_HOST> \
  'sw_vers -productVersion; sysctl -n hw.memsize; for t in brew ffmpeg yt-dlp mediamtx; do printf "%s: " $t; command -v $t || echo MISSING; done'

brew info liquidsoap
brew info --json=v2 liquidsoap | python3 -c 'import json,sys; print(json.load(sys.stdin)["formulae"][0]["bottle"]["stable"]["files"].keys())'
ffmpeg -hide_banner -version | head -3   # 檢查 configuration 內有無 --enable-libfreetype
ffmpeg -hide_banner -filters | grep -E 'drawtext|subtitles'   # 空 = 不存在
```
