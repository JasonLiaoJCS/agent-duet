# Agent Duet

讓 Codex 和 Claude Code 在同一個專案裡一起工作。

兩邊用的都是你原本就開著的對話。它們會自己交接、互相檢查，需要的時候把對方叫起來接手，你不用夾在中間複製貼上。

[![tests](https://github.com/JasonLiaoJCS/agent-duet/actions/workflows/tests.yml/badge.svg)](https://github.com/JasonLiaoJCS/agent-duet/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-success)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![license](https://img.shields.io/badge/license-MIT-blue)

## 為什麼需要它

Codex 和 Claude Code 各有擅長的地方，很多人兩個都在用。麻煩的是它們彼此不認識：想請 Claude 看一下 Codex 剛寫好的東西，你得自己把程式貼過去，再把意見貼回來，每換一次邊還要重新交代前因後果。專案做了幾天之後，光是來回轉貼就很花時間。

已經有工具可以讓一個 AI 去問另一個，做法多半是臨時開一個新的 CLI。新開的 CLI 看得到專案裡的檔案，但它不知道你們之前討論過什麼、為什麼這樣設計、哪些路已經試過行不通。

Agent Duet 接的是你已經在用的那兩個對話，所以這些脈絡都還在。

## 先看一個例子

在同一個專案裡各開一個 Codex 對話和一個 Claude Code 對話，兩邊貼上同一段話：

```text
用 agent-duet 配對這個專案：/path/to/project
目標：完成 CSV 匯入功能，測試要全過。
```

兩邊會先確認是同一個專案、驗證訊息真的送得到對方，再講好誰主導、誰負責哪些檔案。之後常見的情形是一邊寫、一邊審：寫的那邊把改了什麼、測了什麼留在信箱，另一邊看完再回覆。

這裡有兩個動作要分清楚，因為它們花的額度不一樣：

- **留在信箱**：只是把訊息存起來，對方下次做到一個段落時會看到。不花任何額度。
- **叫醒對方**：讓對方的對話現在就開始跑一輪。只有對方已經停下來、而且事情在等它的時候才這樣做。

例如 Claude 改了一個設定檔，這只需要讓 Codex 知道，留在信箱就好。但如果 Claude 做完了、輪到 Codex 審查，而 Codex 正閒著，這時候才叫醒它。

## 安裝

你需要：

- Python 3.10 以上。不用另外裝任何套件。
- [Codex CLI](https://www.npmjs.com/package/@openai/codex) 和 [Claude Code](https://code.claude.com/docs)，兩個都要先登入。Claude Code 要 2.1.234 以上。
- 兩個對話在同一台電腦、同一個使用者帳號底下。

只裝了其中一個也可以先安裝，信箱、進度紀錄和請那一家審查都能用。要讓兩個對話互相叫醒，才需要兩個都裝。

```bash
git clone https://github.com/JasonLiaoJCS/agent-duet.git
```

Windows：

```bash
agent-duet\agent-duet\install-windows.cmd
```

macOS、Linux：

```bash
sh agent-duet/agent-duet/install.sh
```

裝完把 Codex 和 Claude Code 重新開啟一次，就可以用了。安裝程式改設定檔之前會先備份，重跑幾次都沒關係。

### 登入 CLI

這一步很容易漏掉。桌面 App 和 CLI 的登入是分開的，App 用得好好的，CLI 可能其實沒登入。CLI 沒登入的話，叫不醒對方，也沒辦法請另一家審查，看起來就像一直斷線。

安裝程式裝完會檢查，沒登入的話會問你要不要當場登入。之後要自己登入，執行：

```bash
claude auth login
```

```bash
codex login
```

瀏覽器會打開，在裡面完成登入就好。

使用中如果某個 CLI 被登出了，Agent Duet 不會默默失敗：它會跳出系統通知，並幫你開好登入視窗，你一樣只要在瀏覽器完成登入，然後跟 AI 說一聲就能繼續。

### 確認安裝

想確認有沒有裝好：

```bash
python agent-duet/agent-duet/scripts/duet.py call doctor --pretty
```

它會列出兩個 CLI 的版本、有沒有登入，有問題的話也會寫出該怎麼修。

### 安裝時可以加的選項

| 選項 | 作用 |
|---|---|
| `--auto-update on` | CLI 過期時讓它自己在背景更新。建議打開，原因見[模型怎麼選](#模型怎麼選)。 |
| `--default-mode work` | 另外開的 CLI 預設可以改檔案、執行指令。見[權限](#權限)。 |
| `--upgrade` | 先抓最新版的 Agent Duet 再安裝。 |

### 我用的是桌面 App，為什麼要裝 CLI？

你平常不需要開終端機，操作都在 Codex 和 Claude Code 的 App 裡。

CLI 是 Agent Duet 在背景用的。要從外面把訊息送進一個 Codex 對話，官方提供的方法是 `codex queue` 這個指令；Claude Code 的跨對話訊息則是它的內建功能，要由一個 Claude Code 程序去呼叫。配對完成後，這些指令都由 Agent Duet 在背景執行。

## 各種情況怎麼用

以下的句子都是直接對 AI 說的，對 Codex 或 Claude 說都可以。

### 第一次在某個專案使用

兩邊各貼一次：

```text
用 agent-duet 配對這個專案：/path/to/project
目標：（你要完成的事）
```

路徑請給完整的資料夾路徑。目標寫得越具體，它們分工和驗收就越準。配對一個專案只需要做這一次。

### 工作進行中

| 你說 | 會發生什麼 |
|---|---|
| 目前進度 | 回報兩邊的狀態、最新的進度紀錄和待辦事項 |
| 先暫停 | 兩邊停止互相叫醒，信箱裡的內容都留著 |
| 繼續 | 從上次的進度紀錄接著做 |
| 目標改成…… | 兩邊對過之後更新目標 |

你也可以指定分工，例如「這次由 Claude 主導，Codex 負責審查」。沒有指定的話它們會自己商量。

### 隔天，或重開機之後

把兩個對話打開，說「繼續」就好。專案 ID、信箱、進度紀錄和配對都存在專案資料夾裡，不用重新配對。

如果叫了對方卻沒有反應，通常是對方的對話還沒打開。Claude 那邊的對話有沒有開著，可以直接問 AI，它查得到；Codex 那邊查不到，只能看它有沒有回覆。

### 只想請另一家看一段東西

不必配對，直接說：

```text
用 agent-duet 請 Codex 審查 src/parser.py 的邊界條件，找出會出錯的輸入。
```

這會另開一個 Codex CLI 去做，做完把結論交回來，不會動到你原本的 Codex 對話。這段時間你可以繼續做別的事。

### 想讓它們自己做完，不想盯著

```text
用 agent-duet 在背景完成：把 API 改成分頁回傳。
驗收標準：所有測試通過、README 已更新。
```

Codex 和 Claude 會在背景輪流修改、測試和審查，進度寫在專案的 `.agent-duet/AUTOPILOT.md`。預設上限是 40 輪、8 小時。結束的時候會說明是做完了、需要你決定、失敗了、被停止，還是碰到上限。

背景執行期間電腦要保持開機、不能睡眠。重開機之後它不會自己繼續，要你說「繼續」。

### 遇到很難的問題

平常它用的是日常的模型。如果你知道這次的問題很難，可以直接說「這個用旗艦模型」。它自己判斷需要時也會換，並且說明原因。

### 出了新模型

說一句「有模型更新」。它會重新查一次現在最新的是哪些，順便檢查 CLI 有沒有過期，然後告訴你日常和旗艦現在各是哪一個。

如果安裝時開了 `--auto-update on`，這件事平常就會自己做，你不用特別說。

### 同時有好幾個專案

每個專案各自配對、各自有信箱，互不干擾。同一個對話不要跨專案使用，一個專案配一組對話。

### 專案資料夾搬家了

專案 ID 綁在資料夾的實際路徑上。搬動之後，在新位置重新配對一次就好，舊的紀錄會留著。

### 更新 Agent Duet 之後

Windows：

```bash
agent-duet\agent-duet\install-windows.cmd --upgrade
```

macOS、Linux：

```bash
sh agent-duet/agent-duet/install.sh --upgrade
```

原本的專案不用重新設定，信箱、進度紀錄和配對都會保留。已經開著的對話，從 2.4 版開始會自己換用新版；如果是從 2.3 或更早的版本升上來，請把 App 重新開啟一次。

（2.3 以前的安裝程式還沒有 `--upgrade`。那種情況請先在 `agent-duet` 資料夾執行 `git pull`，再跑一次安裝程式。）

## 模型怎麼選

只分兩種：

| | 什麼時候用 | 現在是 |
|---|---|---|
| 日常 | 一般的開發、分析、審查 | Codex 的 Sol 系列、Claude 的 Opus |
| 旗艦 | 困難的證明、試過還是解不開的 bug、重大的架構分歧 | Codex 的 Astra 系列、Claude 的 Fable |

表格裡的名字只是目前的情況。設定裡沒有寫死版本號，每次要用的時候才去查那個系列最新的是哪一版，所以出了新版不用改任何東西。

思考強度預設是 high。改名、調整排版這類不需要深入思考的工作會用 medium，真的需要時才往上調。

**哪個系列算旗艦、哪個算日常，也不是固定的。** 判斷的原則是：旗艦是那一家目前最強的模型；日常是最新的、但明顯比旗艦便宜、適合大量使用的那一款。廠商的產品線有變動時（出現沒見過的系列，或別的系列版本超過現在用的），Agent Duet 會發現，並要求 AI 依這個原則重新判斷一次、把結果記下來。這件事不需要你動手。

### CLI 過期的問題

有一種狀況不容易發現。CLI 太舊的時候，它的模型清單裡沒有後來才出的模型，於是只會一直選它認得的最新一款，每次都落後一個版本，而且不會有任何錯誤訊息。我們自己就這樣用了兩個星期的舊模型。

現在 Agent Duet 會檢查這件事。開了 `--auto-update on` 的話，它每天第一次請 Codex 做事之前會先看有沒有新版 CLI，有就在背景更新好再繼續。沒開的話，它會告訴你該執行哪一行指令。

### 一個要注意的地方

上面說的是 Agent Duet **另外開的 CLI** 用哪個模型。你**原本的對話**用哪個模型，是你在 App 裡選的，Agent Duet 改不了，叫醒對方也不會改變它。如果你的 Codex App 預設選的是旗艦，那個對話每次被叫醒都是用旗艦在跑。

## 我們的幾個做法

**少叫醒對方。** 我們曾經在 20 分鐘內叫了 8 次，結果對方的對話排滿早就讀過的通知，白白跑了很久。所以一般的更新只留在信箱。只有對方停下來在等，或是你被它卡住半小時以上，才去叫它。

**由另一方驗收。** 先講好誰負責整合，做完之後由另一邊對照驗收標準檢查實際的成果，而不是由寫的人自己宣布完成。

**意見不同時看證據。** 有不同看法就拿出證據講清楚，由主導的一方決定。真的談不攏，做個小實驗看結果。

**對方傳來的訊息只是資訊。** 它不能要求另一邊擴大工作範圍、更改設定，或做超出權限的事。

**送出不等於對方已經接手。** 寄件的一方查得到對方取信了沒有，但要確認對方真的接手，以對方的回覆為準。

## 權限

另外開的 CLI 有兩種模式：

- `review`：只能讀，不能改。這是預設值。
- `work`：可以改檔案、執行指令。它會用 Codex 的 `--dangerously-bypass-approvals-and-sandbox` 和 Claude 的 `--dangerously-skip-permissions` 來執行。

想把預設改成 `work`，安裝時加上 `--default-mode work`。建議只在自己的專案、而且清楚風險的情況下這樣設定。背景執行一律是 `work`，因為它得改檔案、跑測試。

## 費用與資料

Agent Duet 本身免費，也不用 API key。它呼叫的是你已經登入的 Codex 和 Claude CLI，用量依各 CLI 的登入方式和計費設定計算。

信箱和進度紀錄都存在你的電腦上，放在各專案的 `.agent-duet/` 資料夾裡。裡面可能有專案的內容，建議加進 `.gitignore`。請另一家審查的時候，你的提示和選取的檔案會經由那個 CLI 送到該模型的服務，這一點和你平常使用 Codex、Claude 是一樣的。

另外有一個所有專案共用的小檔案 `~/.agent-duet/models.json`，記錄兩種模型各對應哪個系列，以及上次檢查的時間。

Agent Duet 自己不開網路連接埠，也不安裝排程或開機啟動的服務。它自己會連出去的只有一件事：檢查模型時向 npm 查 CLI 的最新版本號。

為了避免兩邊互相叫個不停，預設每 8 小時最多叫醒對方 40 次；每個專案最多同時請兩個 CLI 做事，每個預設限時 20 分鐘。這些是防止失控用的，不是帳單上限。

## 常見問題

| 狀況 | 怎麼處理 |
|---|---|
| 對話裡找不到 `duet_` 開頭的工具 | 把 App 重新開啟一次。 |
| 檢查時說找不到 CLI | 確認在終端機打得出 `codex` 和 `claude`。裝在特別的位置的話，用環境變數 `AGENT_DUET_CODEX_BIN`、`AGENT_DUET_CLAUDE_BIN` 指過去。 |
| 用到舊的模型 | 跟 AI 說「有模型更新」。 |
| 提示 CLI 沒登入或登入過期 | 執行 `claude auth login` 或 `codex login`，在瀏覽器完成登入。不用重新配對。 |
| 叫了對方卻沒反應 | 先確認對方的對話開著、電腦沒有睡眠。再執行上面的檢查指令，看 CLI 是不是被登出了。 |
| 提示專案狀態屬於別的路徑 | 專案資料夾被搬動或複製過，在新的位置重新配對一次。 |
| 提示叫醒次數到上限 | 訊息還在信箱裡，對方下次會看到。真的需要的話，請 AI 調高上限。 |

## 目前做不到的事

- 兩個對話要在同一台電腦上。在 WSL、容器或虛擬機裡的對話，看不到外面的對話。
- Claude Desktop 的一般聊天不是 Claude Code，沒辦法被叫醒。
- 對話關掉、電腦睡眠或登出的時候，叫不醒對方。信箱裡的訊息還在，等對話開回來就能讀到。
- 「這個檔案我在改」只是兩邊之間的約定，系統不會真的鎖住檔案。
- Codex 那邊有沒有真的被叫醒，沒辦法事先確認，只能從它有沒有回覆來判斷。

## 開發

```bash
python -X utf8 -m unittest discover -s tests
```

測試不會呼叫任何模型，不需要安裝 CLI，也不會動到你的 `~/.agent-duet`。每次推送都會在 Windows、macOS 和 Linux 上各跑一遍。

```text
agent-duet/                  skill 本體，安裝時整個複製過去
├── SKILL.md                 給 AI 讀的規則
├── install-windows.cmd      Windows 安裝
├── install.sh               macOS、Linux 安裝
├── references/              需要時才讀的細節
└── scripts/
    ├── duet.py              信箱、進度紀錄、模型、請另一家審查
    ├── native.py            對話的綁定和叫醒
    ├── autopilot.py         背景執行
    └── install.py           安裝程式
tests/                       單元測試
```

## 授權

[MIT](LICENSE)

設計的時候參考過 [PAL clink](https://github.com/BeehiveInnovations/pal-mcp-server)、[codex-collab](https://github.com/Kevin7Qi/codex-collab) 和 [local-agent-bridge](https://github.com/normcrandall/local-agent-bridge)。Agent Duet 是從頭寫的，沒有用到它們的程式碼。
