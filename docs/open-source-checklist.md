# 開源前檢核與調整清單 v0.1

> 本文件為繁體中文。英文說明見 repo 根目錄 [README.md](../README.md) 的 English 一節。
> This document is in Traditional Chinese; see the English section of [README.md](../README.md).

2026-09-20。針對 `YouTubes2YouTube_Stream` 做一次「如果現在公開，會發生什麼事」的盤點。
每一項都附實際檔案與行號，可以直接照著改。

標記：🔴 公開前必改 ｜ 🟠 建議在 1.0 前改 ｜ 🔵 可以之後再做

---

## 0. 先講三件最重要的事

**（1）金鑰類的東西沒有外洩，這是最幸運的一點。**
用 `git log --all -S` 掃過整份歷史，直播金鑰前綴、Telegram bot token、chat_id、cookies 全部 **0 筆命中**。
也就是說「只留在機器上、不進版控」這個約定從第一天就有守住，不需要動用 `git filter-repo` 來救金鑰。

**（2）但有兩個東西現在就在 repo 裡，一公開等於公開。**
一個是**那台機器的 SSH 密碼**（明碼寫在兩份文件裡），另一個是**內部主機位址、帳號名稱、機型**。兩者都不是「程式邏輯」，而是把 repo 當成工作紀錄寫下來的副作用。

**（3）以現在的樣子公開，別人 clone 下來是跑不起來的。**
缺 `mediamtx.yml`（plist 指向它，但檔案不在 repo）、缺 `LICENSE`、五支 plist 寫死 `/Users/<USER>`。

---

## 0.5 進度（2026-09-20 更新）

已完成（本輪動手做的部分）：

- ✅ **2.1** 兩份 v4 文件中的 SSH 密碼與 `sshpass -p` 用法已移除
- ✅ **2.2** 五支 plist 改成 `__HOME__`／`__USER__` 佔位符（22 處），並新增 `install.sh` 負責代入。
  驗證方式：把產生的 plist 還原後與原檔比對，**五支全部逐位元組相同**
- ✅ **2.3** 新增 `mediamtx.example.yml`，刻意用**路徑白名單**（只開 `live/main`），不是 `all_others`
- ✅ **2.4** 新增 `LICENSE`（MIT）；README 補上第三方元件授權與〈使用聲明〉
- ✅ **3.1** 去識別化：內網 IP、**兩組**公網 IP、帳號、機型、主機名，共 **165 處**，跨 10 份文件
- ✅ 順手修掉 4 處「變數緊接全形字」的 bash 解析地雷（`$rc，`、`$WANT_PL，`、`$SCOPE）`）
- ✅ **P1 參數收斂**：新增 `src/settings.json`（畫質／版面／淡化／黑尾門檻），程式讀它當預設值、
  CLI 仍可覆寫；移掉 `mode_build.py` 裡寫死的 `--target 720` 與按鈕文字
- ✅ **P2 WebUI**：`src/webui.py`（只用標準庫）：狀態／設定編輯／建置動作，預設綁 127.0.0.1、
  寫入要自訂標頭（擋 CSRF）、不以 root、金鑰只進不出
- ✅ **P1 目錄重整**：`src/`、`launchd/`、`docs/`（`docs/archive/` 放過時評估）、ASCII 檔名；
  `install.sh` 移到 repo 根目錄並改讀 `src/`；47 處文件內路徑引用一併更新
- ✅ **README 改為中英並列（中文為主）**，並在三份主要文件加上雙語提示

### 2026-09-20 第二輪（內容身分）

- ✅ **內容身分去識別化（36 處）**：頻道 ID／handle、播放清單 ID、影片 ID、節目名稱、人名，
  以及先前漏掉的 **Google 帳號**（5 處）與 Telegram bot 名稱
- ✅ **`docs/business-plan.md` 已刪除**（商業敏感整份移除；歷史仍在 git 裡）
- ✅ **`src/playlist.json` → `src/playlist.example.json`**：53 支真實影片 ID 換成 3 筆假 id，
  真的母清單列入 `.gitignore`；`stream.json` 一併忽略
- ✅ `YT_VIDEO_ID`（health 的 YouTube 端檢查）改成 plist 佔位符，由 `install.sh` 從環境變數代入
- ✅ 敘述性內容（節目名、人名、頻道顯示名稱）改寫成不指名；`modes.json` 來源頻道改為 `@YourChannel` 範例
- ✅ **`docs/manual.md` 拆成 `manual.md`（現況操作）與 `changelog.md`（逐日實測紀錄）**：
  33 個段落逐字搬移，並用程式驗證每個段落都原封不動落在其中一邊
- ✅ 手冊收尾：去掉殘留日期、補回被切掉的 H1、修掉重複的 `modes.json` 列、6 處標題前補空行、重寫檔案表
- ✅ 清掉程式 docstring 裡殘留的目標機簡寫（`.41`，2 處）

仍待決定（需要你拍板）：

- 🔶 重寫歷史 or 開新 repo（**前置條件：先換掉那台機器的 SSH 密碼**）

---

## 1. 現況盤點

### 1.1 已經做對的部分（不用改）

| 項目 | 查核方式／結果 |
|---|---|
| 金鑰不進版控 | `.gitignore` 有 `stream.key`／`telegram.json`／`youtube-oauth.json`／`cookies.txt`；歷史掃描 0 命中 |
| 作者身分 | 全部 commit 用的是 GitHub 的 noreply 位址，沒有真實 email |
| 程式碼可攜性 | 所有 `.py`／`.sh` 都用 `HERE = os.path.dirname(os.path.abspath(__file__))` 推路徑，**沒有任何一支寫死 `/Users/...`** |
| 服務定義分離 | 監控參數多數已可用環境變數覆寫（`API`、`PATH_NAME`、`SRC`、`DEST`、`KEYFILE`、`VENC`） |
| 內容參數已集中 | `src/modes.json` 已經把「來源頻道／長度上限／shorts 池／掃描頻率」集中在一個檔——這是全 repo 最好的參數化範例，其他部分照這個模式收斂就好 |

### 1.2 掃描到的問題總量

| 類別 | 數量 | 位置 |
|---|---|---|
| 明碼 SSH 密碼 | 2 處 | 兩份 v4 文件 |
| plist 內寫死家目錄 | 22 處 | `launchd/*.plist` 五支 |
| 內部 IP／帳號／機型 | 20+ 處 | README、規格書、v4.3／v4.4、起始套件 README |
| 真實頻道／影片 ID | 10+ 處 | `modes.json`、`playlist.json`、README、`yt_side_monitor.py` |
| 商業敏感內容 | 1 份文件（已刪除） | `docs/business-plan.md` |
| 缺必要檔案 | 5 項 | LICENSE、mediamtx.yml、requirements、install、example config |

---

## 2. 🔴 公開前必改

### 2.1 SSH 密碼明碼寫在文件裡（最嚴重）

    docs/archive/source-url-lifecycle-v4.3.md:19
    docs/archive/liquidsoap-eval-v4.4.md:96
      → sshpass -p <PASSWORD> ssh <USER>@<HOST>

這是那台機器的**實際登入密碼**，連 `sshpass` 的用法一起寫進了工作紀錄。

要做兩件事，順序不能顛倒：

1. **先換密碼**。這兩份文件已經推上 GitHub；即使 repo 現在是 private，也要當成已洩漏處理（而且要確認這段期間有沒有曾經公開過）。
2. **再改文件**。改寫成 `ssh <USER>@<HOST>`，並且**不要**在開源教學裡示範 `sshpass -p`——那等於教人把密碼放在指令列（會進 shell history、會出現在 `ps`）。

另外 `docs/manual.md:95` 雖然沒寫密碼，但也建議把那句「sshpass 只是本機開發方便」整段拿掉，改用金鑰。

### 2.2 launchd plist 寫死家目錄（22 處）

    launchd/com.ytpl.playout.plist   6 處
    launchd/com.ytpl.publish.plist   5 處
    launchd/com.ytpl.refresh.plist   4 處
    launchd/com.ytpl.mediamtx.plist  4 處（含 UserName <USER>）
    launchd/com.ytpl.health.plist    3 處

這同時是**可用性問題**（別人裝不起來）和**隱私問題**（洩漏帳號名稱）。

建議做法：plist 當模板，用佔位符，由 installer 產生。

    # 範本裡寫
    <string>__HOME__/ytpl/playout.sh</string>
    <key>UserName</key><string>__USER__</string>

    # install.sh 裡
    sed -e "s|__HOME__|$HOME|g" -e "s|__USER__|$(id -un)|g" \
        launchd/com.ytpl.playout.plist > /tmp/plist && sudo cp /tmp/plist /Library/LaunchDaemons/

順便解決一個現實問題：現在改路徑要手動改五支 plist，有了 installer 之後只改一個變數。

### 2.3 缺 `mediamtx.yml`：repo 的核心設定不在裡面

`com.ytpl.mediamtx.plist` 指向 `/Users/<USER>/ytpl/mediamtx.yml`，但 `git ls-files` 裡沒有這個檔。外部使用者照 README 做，Mediamtx 會直接起不來。

而且原本的設定是 `paths: all_others`（**全開**）——任何人推到 `live/xxx` 都會被接受。開源範例應該給**最小權限**版本：明確列出 `live/main`，其他一律拒絕，並保留 publish/read 的認證欄位當註解。

新增 `launchd/…` 同層放 `mediamtx.example.yml`，installer 複製成 `mediamtx.yml`（不覆蓋已存在的）。

### 2.4 沒有 LICENSE

沒有 LICENSE 的公開 repo，法律上是「保留所有權利」——別人**不能合法使用、修改、再散布**，等於白開源。

建議 MIT（與 MediaMTX、yt-dlp 一致），並在 README 加一段第三方元件致謝：MediaMTX（MIT）、yt-dlp（Unlicense）、FFmpeg（LGPL/GPL，**這條要注意**：散布含 libx264 的 ffmpeg 建置會涉及 GPL，我們的作法是「要求使用者自己 `brew install`」，不是散布 binary，所以沒問題——但要在文件裡寫清楚我們不散布）。

---

## 3. 🟠 隱私去識別化

### 3.1 對照表（照著換就好）

| 位置 | 目前內容 | 建議 |
|---|---|---|
| `README.md:29` | 內網 IP ＋「MacBook Pro M1 / 8 GB / macOS 27」 | 「一台 Apple silicon Mac（8 GB 記憶體即可）」 |
| `modes.json`（6 處） | `https://www.youtube.com/@YourChannel/videos`、`/shorts` | `__CHANNEL__/videos`，並附一個 `modes.example.json` |
| `playlist.json` | 真實影片 ID 清單（＋`seconds` 真實長度） | 換成 3 支公開示範影片，或直接只留骨架 ＋ 註解 |
| `docs/manual.md`（頻道 QR 一節） | 你的頻道 ID | `__CHANNEL_ID__`（範例用一個公開頻道） |
| `docs/manual.md`（shorts 輪播一節） | 你的 shorts 頻道 handle | `@<SHORTS_CHANNEL>/shorts` |
| `src/yt_side_monitor.py:13,14` | `--id GeP67qGcoFs` | `--id <VIDEO_ID>` |
| `docs/YouTube直播自動化-v4.3….md:9-19` | 目標機與工作機的內網 IP、帳號名稱、機型識別碼 | 全部換成 `<HOST>`／`<USER>`／「目標機」「工作機」 |
| `docs/archive/spec-v1.0.md:56,97,101` | 同上 ＋ 預設閘道 IP | 同上 |
| `docs/YouTube直播自動化-v4.4….md:96` | 完整 ssh 指令 | 移除 |
| `docs/business-plan.md`（**已於 2026-09-20 刪除**） | 整份：商業分級、報價基礎、每戶成本、客戶數、硬體型號、Telegram bot 名稱、容量上限 | 已移出 repo；若要連歷史都不留，走 §3.2 的重寫或開新 repo |

判斷原則很簡單：**「這台機器是誰的、跑在哪、帳號叫什麼」全部去識別化；「這個技術選擇為什麼對」全部保留。** 這份 repo 最有價值的內容是實測數據與踩雷紀錄，那些跟你是誰無關。

### 3.2 改檔案不等於改歷史

上面改完，**舊 commit 裡的原值還在**（`git log -p` 就翻得到）。現在那份歷史同時含舊 IP、舊帳號、以及那組 SSH 密碼。

兩條路：

- **重寫歷史**：`git filter-repo --replace-text`，把字串批次換掉。適合「想保留 23 筆 commit 紀錄」。
- **開新 repo（建議）**：把去識別化後的樹當成新專案的 `Initial commit`（或照主題切成幾筆）。這個 repo 的 commit 歷史本身是一份工作日誌（「更正：收尾卡死的真因是…」），對外部讀者其實是干擾大於價值。

不論走哪條，**換掉 SSH 密碼都是先決條件**，因為歷史重寫救不了已經被看到的密碼。

---

## 4. 🟠 參數化盤點（WebUI 的基礎）

參數分三層，**不要全部塞進 WebUI**。現在的問題是第二、三層散在程式常數裡。

### 4.1 內容層（已經有雛形 ✅）

`src/modes.json` 已具備：`video_source`、`video_limit`、`max_seconds`、`shorts_url`、`shorts_count`、`shorts_seconds`、`refresh_seconds`、`shorts_passes`。

要補的：

| 參數 | 現在 | 建議 |
|---|---|---|
| 播放清單／頻道來源 | `build_local_content.py --playlist`、`build_playlist.py --url` | 收進 config |
| 播放模式清單 | `modes.json` 硬編 news/promotion/test 三種 | 改成任意鍵，程式別假設有三個 |
| 每個 mode 的輸出目錄 | `media/<模式>/` | 已有，寫進 config 讓它可換根目錄 |

### 4.2 版面層（散在常數裡，最需要收斂）

`src/build_local_content.py` 常數區（約 57-70 行）：

| 常數 | 現值 | 說明 |
|---|---|---|
| `AUDIO_FADE` | 2.5 | 換片淡入淡出秒數 |
| `DATE_LABEL` | `首播日期：` | 浮水印文字（**多語系時必須可換**） |
| `OVERLAY_Y` / `MARQUEE_Y` / `OVERLAY_MARGIN` | 40 / 10 / 40 | 浮水印位置 |
| `MARQUEE_SPEED` / `MARQUEE_GAP` | 120 / 220 | 跑馬燈速度與間距 |
| `BLACK_TAIL_MIN` / `BLACK_TAIL_SLACK` | 5.0 / 2.5 | 黑尾偵測門檻 |
| `TARGETS` | 1080/720/480 | 已可選，但 fps 固定 30 |
| 編碼參數 `2500k / -maxrate 2500k / -bufsize 5000k` | 寫死三處（`build_local_content.py:161-169`、`build_transitions.py:37`、`playout.sh:21`、`build_test_edition.py:128`） | **同一個數字抄了四份**，是最容易改不一致的地方 |
| 按鈕文字 | `看原片`／`去追劇`（程式 default） | 已有 `--button-caption`，收進 config |
| QR 尺寸 | `size=30, qr_px=120`（程式中） | 收進 config |

### 4.3 基礎設施層（給 installer／環境變數，不必進 WebUI）

| 參數 | 現值 | 出現位置 |
|---|---|---|
| RTMP 進／出 | `rtmp://127.0.0.1:1935/live/main` | `playout.sh:17`、`yt_publish.sh:14`、plist、Python 預設值 |
| MediaMTX API | `http://127.0.0.1:9997` | 6 支程式（多數已有 `API` 環境變數 ✅） |
| 路徑名稱 | `live/main` | `healthcheck.py`、`loopwatch.py`、`gapwatch.py` |
| 推流目的地 | `rtmp://a.rtmp.youtube.com/live2` | `publish.plist`（已是環境變數 ✅） |
| 金鑰檔路徑 | `KEYFILE` 環境變數 | ✅ 已可覆寫 |
| launchd label 前綴 | `com.ytpl.*` | 五支 plist ＋ `switch_edition.sh` 的 PlistBuddy 操作 |
| 家目錄 | `/Users/<USER>/ytpl` | 22 處（見 2.2） |
| 健康檢查參數 | 間隔 60s、`GROW_WINDOW=6.0`、`RE_ALERT_SEC=1800`、連續 3 次、`--recycle-after-days 25` | `healthcheck.py` |
| relay 參數 | `--url-max-age 1800`、`--url-expiry-margin 300`、`--watchdog 3`、`DEFAULT_CLIENTS`、`FALLBACK_CLIENT=android` | `relay.py` |
| 系統上限 | `NumberOfFiles 8192` | `com.ytpl.mediamtx.plist` |

### 4.4 建議的設定檔形狀

沿用 `modes.json` 已經證明可行的模式，收斂成一份（或 `config/` 一個目錄）：

    config/
      settings.json      影像／音訊／版面／淡化等通用參數
      sources.json       要播什麼（＝現在的 modes.json）
      infra.json         路徑、port、label（由 installer 產生，不進版控）

關鍵設計：**程式只讀 config，CLI 參數只用來覆寫**（目前 `build_local_content.py` 已經有一半是這樣，把它補完即可）。這樣 WebUI 只要會寫這幾個檔，不用去改程式、也不用重寫邏輯。

---

## 5. 🔵 WebUI：做與不做

### 5.1 定位先講清楚

**它是「單機單管理者的本機控制台」，不是多租戶後台。**
一旦它變成對外的後台，就要面對帳號、權限、審計、備份——那是另一個產品。開源版做本機控制台就好，多租戶留給另一條服務線。

### 5.2 該做（依價值排序）

| 功能 | 為什麼值得 | 底層呼叫 |
|---|---|---|
| **狀態頁** | 現在看狀態要 ssh ＋ 背四條指令 | 讀 `logs/health.log`、`logs/alerts.jsonl`、MediaMTX `/v3/paths/get/live/main`、YouTube `is_live` |
| **來源設定** | 換頻道／長度／shorts 是最常改的 | 編輯 `sources.json` ＋ 觸發重建 |
| **版面設定** | 浮水印／QR／按鈕文字，客戶最在意 | 寫 `settings.json`，重跑 build |
| **金鑰更新** | 現在要 ssh ＋ `kickstart` | 寫 `stream.key`（600）＋ 重啟 publish |
| **服務開關／切換模式** | 已有 `switch_edition.sh`，包一層就好 | 呼叫既有 script |
| **即時預覽** | MediaMTX 已有 HLS，改設定就能看到效果 | 嵌 HLS 播放器（注意 §5.4 的 CPU 代價） |

### 5.3 不要做

- **不要把 stream key 顯示出來**（只能寫入、不能讀出；顯示前 4 碼當識別即可）
- **不要用 root 跑 WebUI**。需要重啟 system domain 服務時，用 **sudoers 白名單**限定特定指令：

      # /etc/sudoers.d/ytpl-webui
      webui ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.ytpl.playout
      webui ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.ytpl.publish

  尤其注意 `refreshwatch.py`／`switch_edition.sh` 目前支援 `SUDO_PASS` 環境變數把密碼餵給 `sudo -S`——**這個機制在服務化之後就不需要了**，用 sudoers 白名單取代，順便讓「密碼放在環境變數」這件事從程式裡消失。
- **不要開放公網**。預設綁 `127.0.0.1`；要給同網段用時，加一組 token（放 `webui-token`，600）並在 README 標明風險。
- **不要做同步的長請求**。重建要下載＋轉檔，動輒數十分鐘，HTTP 一定 timeout。要做成**背景工作 ＋ 進度查詢**（把 `build_local_content.py` 的 log 接出來就好，它本來就有 `log()`）。
- 不要重寫底層邏輯。現在每一件事都已經有一支 script 在做，WebUI 只負責「設參數 ＋ 呼叫 ＋ 顯示結果」。

### 5.4 兩個容易忽略的坑

- **WebUI ＝ 頻道控制權**。能改金鑰、能重啟、能改內容，等於帳號被盜等級。所以：綁 localhost、要 token、寫入類操作記 log。
- **開啟 HLS 預覽會增加 CPU**。實測「HLS 沒人用卻每路徑白做 remux ≈ +1.1% CPU」，當初就是為了這個關掉。預覽要用的話，建議**按需開啟、用完關閉**，而不是設成常開。

### 5.5 技術選型

不需要重框架。`build_local_content.py` 已經 700 行、零第三方依賴（除了 PIL／qrcode 那幾個），WebUI 用 **Python 標準庫 `http.server` ＋ 單檔 HTML** 就能涵蓋 §5.2 全部功能，好處是「跟主程式一樣：clone 下來就能跑，不必先建 venv」。若之後要做多租戶再換 FastAPI。

---

## 6. 🟠 開源基本配備清單

| 項目 | 現況 | 說明 |
|---|---|---|
| `LICENSE` | ❌ 缺 | 建議 MIT |
| `README.md`（英文版） | ❌ 缺 | 至少要有英文的「這是什麼／怎麼跑／授權」 |
| `requirements.txt` 或依賴檢查 | ❌ 缺 | 外部依賴：`ffmpeg`、`yt-dlp`、`mediamtx`、`pillow`、`qrcode`、`opencv-python-headless`、`numpy`（後三個只給驗證用）。建議在程式啟動時檢查並給明確錯誤訊息 |
| `install.sh` | ❌ 缺 | 取代手動 `scp` ＋ 手改 plist（見 2.2） |
| `mediamtx.example.yml` | ❌ 缺 | 見 2.3 |
| `config.example.json` | ❌ 缺 | 見 4.4 |
| 目錄結構 | 🟠 | 程式碼藏在 `src/`。建議 `src/`、`docs/`、`examples/`、`launchd/`；中文目錄名對外部使用者不友善 |
| `.gitignore` 補強 | 🟠 | 目前沒忽略執行期產物：`media/`、`playlist-local.json`、`concat*.txt`、`media/manifest.json`、每個 mode 的輸出目錄。避免有人 `git add -A` 就把 13 GB 內容與真實清單推上去 |
| 測試／CI | 🔵 | 至少 `ruff` ＋ `python -m py_compile`；這類系統的「測試」其實是實測，但語法與 lint 可以自動化 |
| `SECURITY.md` | 🔵 | 說明金鑰怎麼保管、漏洞怎麼回報 |
| 檔名語言 | 🔵 | 開源後建議程式檔名英文、文件可保留中文（再加英文摘要） |

---

## 7. 🔴 法務：這件事必須寫在最前面

這套系統的核心行為是「**用 yt-dlp 抓公開影片，正規化後重新公開傳輸**」。以你自己的內容完全合法，但工具一旦開源，使用者會拿它去做什麼你控制不了。

README 開頭要有明確聲明（不是藏在最後）：

1. 本工具僅供**你擁有著作權或已取得授權**的內容使用。
2. 重製與再公開傳輸的責任由使用者自負，與本專案作者無關。
3. 下載行為仍受各平台服務條款約束。
4. 本專案不散布 FFmpeg／yt-dlp／MediaMTX 的執行檔，請使用者自行安裝並遵守其授權。

這不是法律免責萬靈丹，但它是「有沒有盡到告知義務」的差別。你目前的 README 寫的是「同一創作者已授權的作品」，方向對，但那是**描述你的用法**，不是**規範使用者的用法**——兩者要分開寫。

---

## 8. 🟠 文件與程式已經不同步

外部使用者會直接撞到這幾個：

| 文件說 | 程式實際 | 位置 |
|---|---|---|
| 過場命名 `media/_transition-NN.mp4` | `media/_tr_<影片id>.mp4`（多趟時 `_tr_<id>_p2.mp4`） | `README.md:10`、`docs/manual.md:340` |
| 單輪 106 段、53 集 | 已經改成 `modes.json` 三種模式（news/promotion/test），集數由掃描決定 | `README.md:31` |
| 用 `fetch_content.sh` 落地內容 | repo 裡沒有這支檔案 | `relay.py:28` |
| 過場 QR 在右下角／說明文字「追劇去」 | 後來改成與集數一致（右上角）／「去追劇」 | `docs/manual.md:360-366` 附近有新舊兩版敘述並存 |

另外 `docs/manual.md` 已經 38 KB、45 個章節，内容是「操作手冊 ＋ 逐日更新紀錄」混在一起。**已於 2026-09-20 拆成 `docs/manual.md`（現況操作）與 `docs/changelog.md`（逐日實測紀錄，這份反而是最有價值的內容）。**

---

## 9. 建議執行順序

**P0 — 公開前必做（估半天）**

1. 換掉那台機器的 SSH 密碼（先做，其他都是其次）
2. 移除文件中的密碼與 `sshpass -p` 用法
3. 補 `mediamtx.example.yml`（並改成最小權限 path）
4. 補 `LICENSE`（MIT）＋ 第三方致謝
5. plist 改成佔位符 ＋ 寫 `install.sh`

**P1 — 開源品質（估 1–2 天）**

6. 去識別化（§3.1 對照表）＋ 決定「重寫歷史 or 開新 repo」
7. ~~把代客規劃移出公開範圍~~ → 已刪除（2026-09-20）
8. 參數收斂：先把「2500k 抄四份」與版面常數收進 `config/settings.json`
9. ~~目錄重整 ＋ 英文 README~~ → 已完成（改成中英並列的單一 README，中文在前）

**P2 — 之後**

10. WebUI（§5，先做狀態頁 ＋ 來源設定 ＋ 金鑰更新三件事就有 80% 價值）
11. `.gitignore` 補強、CI、SECURITY.md

---

## 附錄：本輪使用的查核指令

    # 歷史裡有沒有出現過機密
    # 把下面換成你自己的金鑰前綴、token 片段、chat_id、頻道 ID
    for s in "<金鑰前綴>" "<token 片段>" "<chat_id>" "<頻道 ID>"; do
      printf '%s -> ' "$s"; git log --all -S"$s" --oneline | wc -l
    done

    # 作者身分
    git log --format='%an | %ae' | sort | uniq -c

    # 工作區裡的機密／個資（含絕對路徑與內網 IP）
    git grep -n -I -E 'sshpass|-p .{1,20}|192\.168\.|/Users/[a-z]|stream\.key|telegram\.json'

    # 開源後別人跑不起來的地方
    git ls-files | rg -i 'license|requirement|install|mediamtx\.yml|example'
