# YouTube 直播自動化 v4.5 — Liquidsoap 三關實測（含對 v4.4 兩處結論的更正）

> 承接 `docs/archive/liquidsoap-eval-v4.4.md`。v4.4 列出的三關，本輪全部在機器上跑完。**v4.4 有兩條結論要更正**，先在第 1 節講清楚。
>
> 標記規則：【實測】＝本機真的跑出來、可重現；【推論】＝依量測或欄位推導；【待驗證】＝本輪沒做。

---

## 0. 先講結論

Liquidsoap 2.4.5 在 macOS 27.0 / arm64 上**確定跑得起來**，而且比 v4.4 預期的更好：它**內建 YouTube 直播輸出算子**，也**能原生繪字**。三關全過。

真正要付的代價只有一個：**導入它會連動升級工作機的 10 個套件，其中包含 ffmpeg 與 x265**。

---

## 1. 本輪更正了哪些舊結論

| 舊結論（出處） | 本輪實測 | 更正後 |
|---|---|---|
| 「YouTube RTMP 輸出【待驗證】，一般得再包一層 ffmpeg，而這一條會動搖整個提案」（v4.4 §2.1） | Liquidsoap 有原生 `output.youtube.live.rtmp`，預設 url 就是 `rtmp://a.rtmp.youtube.com/live2`；另有 `output.youtube.live.hls`。實際推上去，MediaMTX 收到 H264 720p + AAC、`inboundFramesInError: 0` | **不必再包一層**。推 YouTube 是一級算子 |
| 「PNG overlay 是這條工具鏈上唯一可行路徑」（v4.4 §1） | `video.add_text` / `video.add_text.native` 可用，liquidsoap 自帶繪字 | **不是唯一路徑**。要畫字，liquidsoap 反而是這台上唯一畫得出來的一條 |
| 「直播輸入必須 `self_sync=false`」（交班時的結論） | 那組數字是把 **VOD 素材當 live 源**測出來的假象。換成真正的即時源重測，`self_sync=true` 是 0.91x、完全正常 | **改成**：`self_sync` 只給「真正的即時來源」；有限素材（檔案、VOD）要留預設 false（§5） |

---

## 2. 第一關：macOS 27 / arm64 bottle — 【實測】通過，但有 3 個坑

【實測】

- `brew info --json=v2 liquidsoap`：版本 `2.4.5`，bottle 標籤**只有 `arm64_tahoe`**，相依只有 `ffmpeg`。
- 把那個 bottle 解到 `/tmp/lsverify/liquidsoap/2.4.5_1` 直接執行：`liquidsoap --version` → `Liquidsoap 2.4.5`，在 **macOS 27.0 / arm64** 上正常。（工作機與目標機都是 macOS 27.0。）
- `brew install --dry-run liquidsoap` →「Would install 1 formula: liquidsoap」，而且抓的是 **bottle manifest**，代表**不會落到原始碼編譯**（也就不會需要 ocaml / opam 工具鏈）。

三個坑（每個都會讓人誤判成「裝不起來」）：

1. **stdlib 路徑是編譯時寫死的。** 解到別的路徑就找不到 `.../share/liquidsoap/libs/stdlib.liq`。走非標準路徑得自己補 symlink，或讓 brew 裝在 `/opt/homebrew`。
2. **相依的是 ffmpeg 9 的 soname。** `otool -L` 實測：liquidsoap 2.4.5 需要 `libavcodec.63` / `libswresample.7` / `libswscale.10` / `libavformat.63` / `libavutil.61` / `libavfilter.12`；工作機的 ffmpeg 8.1.2_1 只提供 `.62` / `.6` / `.9`。少了 ffmpeg 9 → `dyld: Symbol not found: _swr_alloc`，直接不啟動。
3. **`nohup` 會剝掉 `DYLD_*`。** macOS 行為，實測：同一支程式直接跑看得到環境變數，用 `nohup` 跑就變空。之後要用 `&`、`launchd` 或 ssh 直接跑。

**導入成本（本輪新量到的數字）**【實測】：

    brew install --dry-run liquidsoap
    ==> Would upgrade 10 dependencies for liquidsoap:
    mpg123  libvmaf  libvpx  ca-certificates  openssl@3
    sdl3  sdl2-compat  x265  xz  ffmpeg
    ==> Would install 1 formula: liquidsoap

在工作機裝 liquidsoap ＝ **升級 10 個既有套件（含 ffmpeg 8.1.2_1、x265 4.2）＋新增 1 個**。這是 v4.4 完全沒估到的成本。

---

## 3. 第二關：直推 RTMP / YouTube — 【實測】通過，而且是內建的

v4.4 擔心「沒有 `output.ffmpeg`，得包 ffmpeg」。把 2.4.5 的 17 個 output 算子全部列出來核對：

【實測】`output.audio_video, output.dummy, output.external, output.file, output.file.dash, output.file.hls, output.harbor, output.harbor.hls, output.harbor.hls.base, output.icecast, output.preferred, output.shoutcast, output.udp, output.url, output.video, output.youtube.live.hls, output.youtube.live.rtmp`

確認**沒有** `output.ffmpeg`、`output.rtmp`、`output.srt`、`output.hls`。但這不重要，因為有這兩個：

| 算子 | 簽名 | 預設 url |
|---|---|---|
| `output.youtube.live.rtmp` | `(?id, ?fallible, ?start, ?url, key, encoder, source)` | `rtmp://a.rtmp.youtube.com/live2` |
| `output.youtube.live.hls` | `(?id, ?fallible, ?segment_duration, ?segments, ?segments_overhead, ?start, ?url, key, encoder, source)` | `https://a.upload.youtube.com/http_upload_hls` |

實測把 `output.youtube.live.rtmp` 的 `url` 指到本機 MediaMTX（`rtmp://127.0.0.1:1936/live`、`key="TESTKEY-abcd-1234"`），結果：

- MediaMTX 上出現路徑 `live/TESTKEY-abcd-1234`、`online: true`（key 會被接到 url 後面當 stream name）
- `tracks: ["H264", "MPEG-4 Audio"]`；H264 **1280x720 High/3.1**、AAC **44.1 kHz 立體聲**
- `inboundFramesInError: 0`
- 播放速率 **20.28 秒牆鐘 / 20.228 秒內容 = 1.00x**，完全等速，而且不用碰 `self_sync`

另一條路 `output.url(url=..., %ffmpeg(format="flv", ...), s)` 也【實測】可用（0.99x、0 error）。liquidsoap 對 `output.url` 的說明本身就寫著：

> Useful with encoder with no expected output or to encode to files that need full control from the encoder, **e.g. `%ffmpeg` with `rtmp` output**.

**第二關的答案**：RTMP 串流是內建能力。`encoder` 參數仍然吃 `%ffmpeg`，但 RTMP 的握手與封裝由 liquidsoap 算子負責，不必自己拼 ffmpeg 命令列。

---

## 4. 第三關：原生繪字與疊圖 — 【實測】通過，而且是真正的新增能力

【實測】`video.add_text.native("GATE3OK", s, x=20, y=20, size=32)` 對全黑底影片做 A/B：

- 差異圖是 150×36 灰階 raw，共 5400 個像素
- **非零像素 48 個，全部落在 x=2–22、y=0–2** 這個 21×3 的小框內，正是繪字位置
- 畫面其餘部分完全相同 → 字形確實畫上去了

延伸【實測】：**工作機的兩個 ffmpeg 都沒有 freetype**：

| 版本 | `--enable-libfreetype` | `drawtext` / `subtitles` / `ass` |
|---|---|---|
| brew ffmpeg 8.1.2_1（工作機現行） | 無 | 全部不存在 |
| ffmpeg 9.0.1_1 bottle | 無 | 全部不存在 |

`video.add_text` 的說明是「Uses the first available operator in: camlimages, **SDL**, **FFmpeg**, **gd** or **native**」。這台缺前四個，走 native 成功。**所以「要畫字」在這條工具鏈上，liquidsoap 是唯一畫得出來的一條**；PNG 疊圖則仍可行（`video.add_image`／`video.add_request` 不需要 freetype）。

順帶確認可用的疊圖與轉場算子：`video.add_image`、`video.add_request`、`video.add_subtitle`、`video.add_line`、`video.add_rectangle`、`crossfade`、`fallback`、`switch`、`smooth_add`、`request.queue`、`playlist`、`mksafe`。（`video.overlay` **不存在**。）

---

## 5. `self_sync` 的真相：更正交班時的結論

判準：從 RTMP 抓「20 秒內容」要花多少**牆鐘秒數**。1.00x 才是正確等速。

| # | 來源 | `self_sync` 設在哪／設什麼 | 牆鐘 | 內容 | 倍速 | 判定 |
|---|---|---|---|---|---|---|
| a | 檔案 `single()` | **output** `self_sync=true` | 5.93s | 20.14s | **3.40x** | 壞：內容被 3.4 倍速抽乾 |
| b | 檔案 `single()` | 不設（預設 false） | 20.28s | 20.18s | 0.99x | 正確 |
| d | **VOD HLS** | **source** `self_sync=true` | 10.69s | 20.18s | **1.89x** | 壞 |
| e | **VOD HLS** | **source** `self_sync=false` | 20.25s | 20.19s | 1.00x | 正確（但每 10 秒一次 catchup 0.2s） |
| t | **真即時 HLS** | source `self_sync=true` | 22.11s | 20.15s | 0.91x | 正確，**0 次 catchup** |
| f | **真即時 HLS** | source `self_sync=false` | 20.19s | 20.19s | 1.00x | 正確，0 次 catchup |
| g | 檔案 | `output.youtube.live.rtmp`（無此參數） | 20.28s | 20.23s | 1.00x | 正確 |

**可以照著設的規則**：

1. `self_sync` 的意思是「這個元件自己出時鐘」。**只有真正的即時來源才給它 true**（第 t 列：0.91x、0 次 catchup，是最順的一組）。
2. **有限素材（本機檔案、YouTube VOD）一律留預設 false**（第 b 列）。給 true 就變第 a／d 列，內容被抽乾。
3. 交班時寫的「直播源必須 `self_sync=false`」**是錯的**。原因是當時拿 `test-streams.mux.dev/x36xhzz/x36xhzz.m3u8` 當「live 源」，但那支是**靜態 VOD 測試素材、不是直播**，所以 `self_sync=true` 才會像抽檔案一樣把它抽乾。
4. 有個現成陷阱要講明：liquidsoap 對 `output.url` 的說明寫「Set to `true` for output to e.g. `rtmp` output using `%ffmpeg`」。**照著這句話設、又拿檔案或 VOD 當來源，就會得到第 a 列的 3.40x。** 反而是用 `output.youtube.live.rtmp`（第 g 列）不必碰這個參數就自動等速。

【待驗證】本地這組 3.40x 是對著 MediaMTX 量的；真實 YouTube ingest 是否會用 back-pressure 把它壓回等速，本輪沒測。

---

## 6. 對「聯播」（拉一條正在直播的 YT 再推出去）的意義

第 t 列那組就是聯播的模型：**真即時源 → liquidsoap → 推出去**，`self_sync=true`、0.91x、0 次 catchup。聯播在 liquidsoap 上是有解而且穩的。

搭配 v4.3 已實測的來源 URL 生命週期（TTL 21600 秒、綁解析當下公網 IP、解析耗 2.26–2.65 秒）與既有 `relay.py` 換手機制，就能組成「yt-dlp 負責解析、liquidsoap 負責長駐與等速輸出」的分工。

（拉 YouTube 的 manifest 仍要 yt-dlp；liquidsoap 沒有直接吃 YouTube 網址的輸入算子。）

---

## 7. 更新後的建議

三關都過，所以現在的問題已經不是「能不能」，而是「值不值得」。關鍵取捨只有一個：

**liquidsoap 會把工作機的 10 個套件一起升級（含 ffmpeg 8.1.2_1 → 9.x）。**

| 路線 | 得到 | 代價 |
|---|---|---|
| 維持現況：`relay.py` 事件式換手（v4.3 已實測 6 次換手、接收端離線 0.000 秒） | 不動環境；換手零斷點已達成 | 沒有真正的 crossfade；要畫字得先做 PNG 產生器 |
| 加裝 liquidsoap 當長駐引擎 | `crossfade` / `smooth_add` / `fallback` / `switch` 現成；**原生繪字**；內建 `output.youtube.live.rtmp` | 工作機升級 10 個套件；目標機得從零裝 brew 加整套相依（8 GB／43 GiB 可用的 M1）；多一層要學的 DSL |

建議：

1. **決策層不用動。** `relay.py` 已經把「videoId 佇列、事件式換手、零斷點」做掉並實測過，改用 liquidsoap 的 queue 拿不到額外的東西。
2. **把 liquidsoap 定位成「可選的輸出／轉場引擎」。** 值得為它付出升級代價的具體理由只有這三個，而且要先確定真的需要：
   - 真正的 `crossfade` / `smooth_add` 轉場
   - 多來源自動 `fallback`（主源掛掉自動換備源）
   - **原生畫字**（字幕、台標）——這一項在這台機器上目前是唯一可行的一條，權重最高
3. **只是要畫字的話，不必整包裝。** 可以先走「HTML → PNG → `video.add_image` 或 ffmpeg overlay」，成本遠低於動 ffmpeg 版本。
4. **目標機不建議當第一個試點。** 它 8 GB、要從零裝 brew，而且剛重開機後 `ANECompilerService` 還在吃 100% CPU（系統背景編譯）。要試，先在工作機的隔離目錄試——本輪就是這樣做的（§8）。

---

## 8. 本輪環境狀態（已還原）

- **工作機**：測試全程隔離在 `/tmp/lsverify/`（bottle 解壓，未經 `brew install`）。過程中為了繞過 stdlib 寫死路徑，曾在 `/opt/homebrew/Cellar/liquidsoap/2.4.5_1` 放過一個 symlink，**已刪除並確認乾淨**（`brew list` 查不到 liquidsoap、Cellar 內已無指向 `/tmp` 的連結）。
- **未動**：工作機 ffmpeg 仍是 `8.1.2_1`（brew 現行版）、x265 仍是 `4.2`；使用者自己的服務（`web-iptv-wall-player` :8080、`opencodex` :10100）持續在跑。
- **無殘留**：liquidsoap 與 MediaMTX 程序、1935／1936／8888／8889／9997／9998 埠都已清空。
- **目標機**【實測】：macOS 27.0 / arm64 / 8 GB / 可用 43 GiB；`brew`、`ffmpeg`、`yt-dlp`、`streamlink`、`mediamtx` 全部 MISSING；Python 3.9.6；`~/loopcastr/relay.py`（34,828 bytes）已在。

---

## 附錄：可重現指令

    # 第一關：bottle 標籤、相依、會不會落到編譯
    brew info --json=v2 liquidsoap | python3 -c 'import json,sys; d=json.load(sys.stdin)["formulae"][0]; print(d["versions"]["stable"], list(d["bottle"]["stable"]["files"]), d["dependencies"])'
    brew install --dry-run liquidsoap        # 看 Would upgrade 清單，不會真的裝

    # 第一關：soname 相依
    otool -L /path/to/liquidsoap | grep -E 'libav|libsw'

    # 第二關：列出所有 output 算子（確認沒有 output.ffmpeg，但有 output.youtube.live.rtmp）
    liquidsoap -h | grep -i '^output'

    # 第三關：這台 ffmpeg 到底有沒有 freetype
    ffmpeg -hide_banner -version | grep -o -- '--enable-[a-z0-9-]*freetype'
    ffmpeg -hide_banner -filters | grep -E ' drawtext| subtitles| ass '

    # self_sync 判準：抓 20 秒內容要花幾秒牆鐘（1.00x 才是對的）
    S0=$(python3 -c 'import time;print(time.time())')
    ffmpeg -v error -i rtmp://127.0.0.1:1936/live/liq -c copy -t 20 -y /tmp/out.flv
    python3 -c "import time;print('wall=%.2f'%(time.time()-$S0))"
