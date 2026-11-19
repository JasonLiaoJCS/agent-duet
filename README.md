# Agent Duet

讓 Codex 和 Claude Code 在同一個專案裡合作：兩邊保留各自原本的對話，透過一個本機信箱互相交接、審查，需要時把對方叫起來繼續做。

[![tests](https://github.com/JasonLiaoJCS/agent-duet/actions/workflows/tests.yml/badge.svg)](https://github.com/JasonLiaoJCS/agent-duet/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-success)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![license](https://img.shields.io/badge/license-MIT-blue)

## 為什麼做這個

同時用 Codex 和 Claude Code 的人，大概都當過兩個 AI 之間的傳話筒：把這邊寫好的程式貼到那邊請它審查，再把意見貼回來。每換一次邊，就得重新交代一次背景。

另一種常見的做法，是讓一個 AI 另開一個全新的 CLI 工作者去問另一家。這能拿到第二意見，但那個工作者是從零開始的：它沒讀過你的程式，也不知道昨天討論到哪裡，背景得重新交代。

Agent Duet 的做法不一樣：它配對的是你**已經開著、已經有上下文**的那兩個對話。Codex 那邊照樣是原本的 Codex 對話，Claude 那邊也是原本的 Claude Code 對話，它們之間多了一條通道。

這套工具是在幾個跨好幾天的專案裡邊用邊改出來的，喚醒和交接的規則都來自實際使用時遇到的問題。

## 它提供什麼

**專案信箱。** 每個專案資料夾有一個 SQLite 信箱。訊息在對方確認處理之前不會消失，重送不會重複，回覆會串在原訊息下面。

**原對話喚醒。** 對方閒著的時候，可以把它叫起來在原本的對話裡接著做。配對紀錄存在專案資料夾裡，隔天或重開機後，把兩個對話重新打開就能繼續。

**專案身分。** 每個專案有一個綁在絕對路徑上的 ID，每一則訊息都要帶著它。ID 或路徑和紀錄對不上時，操作會被拒絕，所以同名的資料夾或複製過來的舊狀態不會被誤認成同一個專案。

**檢查點。** 交接時存下做了什麼、驗證了什麼、還剩什麼。隔天或重開機後，從最新的檢查點接著做。

**單次諮詢。** 臨時需要第二意見時，開一個新的 CLI 工作者去審查、規劃或除錯，跑完回報。

**背景模式。** 不想開著對話時，由背景程式輪流派兩家的工作者實作，再交給另一家審查，直到完成或碰到上限。

**模型自動跟上。** 只分兩檔：日常用的，和最難的任務才用的旗艦。版本號不寫死，新模型發布後不需要改設定；CLI 過期時它可以自己更新。詳見[模型怎麼選](#模型怎麼選)。

## 安裝

需要的東西：

- Python 3.10 以上。只用標準函式庫，不必 `pip install`。
- [Codex CLI](https://www.npmjs.com/package/@openai/codex) 和 [Claude Code](https://code.claude.com/docs)，都已登入。Claude Code 需要 2.1.234 以上才有跨對話訊息。
- 兩個對話在同一台電腦、同一個使用者底下。

只裝了其中一個 CLI 也能安裝，信箱、檢查點和那一家的諮詢都能用；要讓兩個原對話互相喚醒，才需要兩個都裝。

> **平常用的是桌面 App，為什麼還要裝 CLI？**
> 你不需要開終端機，日常操作都在 Codex 和 Claude Code 的桌面 App 裡。CLI 只是 Agent Duet 在背景用來「把訊息送進對方對話」的管道：要從外部把訊息放進一個 Codex 對話，官方支援的方式是 `codex queue`；Claude Code 的跨對話訊息則是它內建的工具，得由一個 Claude Code 程序來呼叫。這些指令都由 Agent Duet 自己執行，不會跳出視窗。

```bash
git clone https://github.com/JasonLiaoJCS/agent-duet.git
```

Windows：

```bash
agent-duet\agent-duet\install-windows.cmd
```

macOS / Linux：

```bash
sh agent-duet/agent-duet/install.sh
```

安裝程式會把 skill 複製到 `~/.codex/skills/agent-duet` 和 `~/.claude/skills/agent-duet`，並在兩邊註冊名為 `agent-duet` 的本機 MCP server（有 Claude Desktop 的話也一併註冊）。修改設定檔之前會先備份，也可以重複執行。

如果希望它在 CLI 過期時自己更新，安裝時加上 `--auto-update on`（之後也可以請 AI 開啟或關閉）。

裝完後把 Codex 和 Claude Code 重新開啟一次，讓 `duet_*` 工具載入。接著可以跑一次健康檢查：

```bash
python agent-duet/agent-duet/scripts/duet.py call doctor --pretty
```

它會列出兩個 CLI 的版本、是否支援原對話喚醒，以及每個問題的修正方式。

## 開始使用

### 配對兩個原對話

在同一個專案裡各開一個 Codex 對話和 Claude Code 對話，兩邊貼上同一段話：

```text
用 agent-duet 配對這個專案：/path/to/project
目標：完成 CSV 匯入功能並通過測試。
使用兩邊原本對話的上下文，互相分工、審查到完成。只有真的需要我決定的事才問我。
```

接下來兩邊會自己確認專案路徑和目標、登記各自的對話、用一個隨機碼來回驗證通道確實雙向可用，然後分配誰主導、誰負責哪些檔案、驗收標準是什麼。通道沒有驗證過，它們不會說「已連線」。

配對只需要做一次。專案 ID、信箱和配對關係都存在專案資料夾裡，重開機之後把兩個對話打開，就能接著用。

之後跟哪一邊講話都可以，常用的幾句：

| 你說 | 效果 |
|---|---|
| 目前進度 | 回報雙方狀態、最新檢查點和待辦 |
| 先暫停 | 停止互相喚醒，信箱內容保留 |
| 繼續 | 從最新檢查點接著做 |
| 目標改成…… | 兩邊協調後更新目標，舊目標留作紀錄 |

### 只要第二意見

```text
用 agent-duet 請 Codex 審查 src/parser.py 的邊界條件，找出會出錯的輸入。
```

這會開一個新的 Codex 工作者，不是你原本的 Codex 對話。它跑完會把結論交回來，期間你的對話可以繼續做別的事。

### 交給背景跑

```text
用 agent-duet 在背景完成：把 API 改成分頁回傳。
驗收標準：所有測試通過、README 已更新。
```

進度會寫在專案的 `.agent-duet/AUTOPILOT.md`。預設上限是 40 輪、8 小時。結束時會標明是完成、需要你決定、失敗、被停止，還是碰到上限，這幾種狀態不會混在一起。

## 模型怎麼選

只有兩檔，思考強度預設都是 high：

| 檔位 | 用在哪裡 | Codex | Claude |
|---|---|---|---|
| 一般 | 日常開發、分析、審查 | 最新的 Sol | 最新的 Opus |
| 困難 | 證明、試過仍解不開的 bug、重大的架構分歧 | 最新的 Astra | 最新的 Fable |

「最新的」是每次呼叫時現查的，設定裡沒有任何版本號，所以新模型發布後不需要動手改。AI 會在每次合作開始時跑一次 `duet_models`，確認兩檔各自對應到哪個模型；你說一句「有模型更新」，它會重新檢查一次，連 CLI 本身是否過期也一起看。

它也會處理一種不容易察覺的狀況：CLI 太舊時看不到之後才發布的模型，只會一直選它認得的最新版，於是每次諮詢都默默落後一個版本。我們自己就這樣用了兩個星期的舊模型才發現。現在 `duet_models` 會偵測到這件事；開啟自動更新的話，它會在背景把 CLI 更新好再繼續，沒開的話會告訴你該執行哪一行指令。

哪一檔該用哪個模型家族，記在 `~/.agent-duet/models.json`。只有在廠商把產品線改名時才需要更新，而這件事也是 AI 查過官方文件後自己改。

## 幾個刻意的設計

**預設不喚醒對方。** 每喚醒一次，對方的對話就要跑完整的一輪。我們試過 20 分鐘喚醒 8 次，結果對方的對話排滿了早就讀過的通知。所以一般更新只留在信箱，對方做到一個段落自然會看；只有對方停下來等，或你被它卡住半小時以上，才喚醒它。

**寫的人不能自己宣布完成。** 完成要由另一方對照驗收標準，檢查實際的成果和 diff。

**一個主導，一個輔助。** 主導的一方定方向、做最後決定；另一方負責實作、獨立審查和找盲點。有異議就拿證據，講一次，然後由主導方決定；真正談不攏的用一個小實驗解決，而不是多辯一輪。

**訊息是資料，不是命令。** 對方傳來的訊息不能讓你擴大範圍、更改設定，或做超出自己權限的事。

**「已送出」不等於「已收到」。** 工具回報 `queued` 只代表送出去了。要有對方帶著正確隨機碼的回覆，才算通。

## 權限

用 `duet_consult` 開出來的工作者有兩種模式：

- `review`（預設）：唯讀，只能看不能改。
- `work`：完整權限，可以改檔、跑指令。它會以 Codex 的 `--dangerously-bypass-approvals-and-sandbox` 和 Claude 的 `--dangerously-skip-permissions` 執行。

想把預設改成 `work`，安裝時加上參數：

```bash
sh agent-duet/agent-duet/install.sh --default-mode work
```

這適合用在自己的專案、而且清楚風險的情況。背景模式一律使用 `work`，因為它必須能改檔和跑測試。

## 費用

Agent Duet 本身不收費，也不需要 API key。它呼叫的是你已經登入的 Codex 和 Claude CLI，用量照你原本的方案計算。

它有幾道避免失控的上限：原對話喚醒預設每 8 小時最多 40 次；同一個專案最多同時跑兩個諮詢；每個諮詢預設 20 分鐘。這些是運作上的限制，不是帳單上的保證。

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 對話裡沒有 `duet_*` 工具 | 重新開啟 App，或重新載入 MCP 工具。裝好之後，已經開著的對話不會自動載入。 |
| `doctor` 找不到 CLI | 確認 `codex`、`claude` 在 PATH 上，或用 `AGENT_DUET_CODEX_BIN`、`AGENT_DUET_CLAUDE_BIN` 指到實際的執行檔。 |
| 諮詢用到舊模型 | 跟 AI 說「有模型更新」，它會重新檢查並更新 CLI。 |
| 諮詢失敗，提示登入過期 | 在終端機執行一次 `claude` 或 `codex login` 重新登入。 |
| 喚醒顯示已送出，對方沒動靜 | 確認對方的對話開著、電腦沒有睡眠。 |
| 重開機後喚醒沒反應 | 確認兩個對話都已經打開。不需要重新配對；可以請 AI 檢查「對方的對話現在開著嗎」。 |
| 提示專案狀態屬於別的路徑 | `.agent-duet/` 被複製或搬動過。在新路徑重新開始，舊的留著當紀錄。 |

## 限制

- 兩個對話必須在同一台電腦、同一個使用者底下。WSL、容器或 VM 裡的對話看不到主機上的對話。
- Claude Desktop 的一般聊天不是 Claude Code，無法被喚醒。
- 對話關閉、電腦睡眠或登出時，喚醒會中斷，信箱內容不受影響。
- 檔案的「認領」只是雙方的約定，不是作業系統層級的鎖；`review` 模式也不是安全沙箱。
- 背景模式不會在重開機後自己恢復，需要你確認後再繼續。

## 資料放在哪裡

所有狀態都在專案資料夾的 `.agent-duet/` 裡，包含信箱、檢查點和諮詢紀錄。裡面可能有專案內容，建議加進 `.gitignore`。

跨專案共用的只有一個小檔案 `~/.agent-duet/models.json`，記錄兩個檔位對應的模型家族和上次檢查的時間。

Agent Duet 不開網路連接埠，不經過雲端中繼，也不安裝排程或開機服務。它自己唯一的連外請求，是檢查模型時向 npm 查詢 CLI 的最新版本號；模型的呼叫都由你已安裝的 CLI 負責。

## 專案結構

```text
agent-duet/                  skill 本體，安裝時整個複製
├── SKILL.md                 AI 讀的主要規則
├── install-windows.cmd      Windows 安裝
├── install.sh               macOS / Linux 安裝
├── references/              需要時才讀的細節
└── scripts/
    ├── duet.py              MCP server 與 CLI：信箱、檢查點、諮詢、模型
    ├── native.py            原對話的綁定與喚醒
    ├── autopilot.py         背景模式
    └── install.py           安裝程式
tests/                       單元測試
```

## 開發

```bash
python -X utf8 -m unittest discover -s tests
```

測試不會呼叫任何模型，不需要安裝任何 CLI，也不會動到你的 `~/.agent-duet`。每次推送都會在 Windows、macOS 和 Linux 上各跑一遍。

## 授權

[MIT](LICENSE)。

設計時參考過 [PAL clink](https://github.com/BeehiveInnovations/pal-mcp-server)、[codex-collab](https://github.com/Kevin7Qi/codex-collab) 和 [local-agent-bridge](https://github.com/normcrandall/local-agent-bridge)。Agent Duet 是獨立寫的，沒有使用它們的程式碼。
