# assets

品牌資產。**選錯版本會看不見**：`logo-dark.svg` 的 `loop` 是白的（`#F8FAFC`），貼在白底會消失；`logo-light.svg` 的 `loop` 是深藍（`#0B1020`），貼在深底會消失。

| 檔案 | 用在哪 |
|---|---|
| `icon.svg` | 透明底；淺色背景、內嵌 HTML、favicon 用（播放鍵用 `currentColor`，跟著文字顏色） |
| `icon-dark.svg` | 深海軍藍圓角底（`#0B1020`）；GitHub／App 圖示，深淺底都看得見 |
| `logo-dark.svg` | **深色底**用（`loop` 白、`castr` 青綠、標語 `#8D9AB8`） |
| `logo-light.svg` | **淺色底**用（`loop` 深藍、`castr` 紫、標語 `#5A6478`） |

## README 怎麼用

GitHub 有淺色／深色主題，所以用 `<picture>` 讓它自己挑（目前根目錄 README 就是這樣寫）：

    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
      <img src="assets/logo-light.svg" alt="loopcastr" width="340">
    </picture>

其他地方（簡報、網站、深色底素材）就直接挑對應版本，不要只放一種。

## 配色

| 名稱 | 色碼 | 用途 |
|---|---|---|
| Loop Teal | `#40E0D0` | 循環、訊號 |
| Cast Violet | `#7567FF` | 播送、影音科技 |
| Midnight Navy | `#0B1020` | 深色底 |
| Card | `#151D35` | 圖示卡片的底 |

標語：`ALWAYS ON. ALWAYS PLAYING.`
