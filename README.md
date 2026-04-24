# ccusage-dashboard

> 纯本地、零上传的 Claude Code Token 用量可视化面板。
> 一个 Python 文件 + 一张 HTML 页面 + 一个 LaunchAgent，完事。

基于 [ccusage](https://github.com/ryoppippi/ccusage) 的数据源，在浏览器里看到**按日/按周/按月/按模型**的费用走势，以及**今天每 15 分钟**的实时 Token 消耗曲线（股票走势图风格）。所有数据来自本机 `~/.claude/projects/*.jsonl`，无任何上传行为。

## 为什么做这个

Claude Code 的 token 用量默认只能在 `/cost` 或 `ccusage` CLI 里看到。社区里也有 vibeusage 这类方案，但它**会把用量 metadata 上传到第三方服务器**（公开 profile / leaderboard）。如果你只想**自己看看自己花了多少**，没必要把数据送出去。

这个仓库就是为这种场景：

- ✅ 纯本地：解析本机 jsonl，零网络上传
- ✅ 零第三方 Python 库，只用标准库
- ✅ 零 Claude Code hook 改动（不碰 `~/.claude/settings.json`）
- ✅ 费用数据由 ccusage 权威提供（定价表跟随 LiteLLM 上游）
- ✅ 日内 Token 曲线自己解析 jsonl，精度和 ccusage 对齐
- ✅ 单文件静态 HTML，打开即看；可选 LaunchAgent 自动刷新

## 功能一览

| 区块 | 内容 |
|---|---|
| 顶部 4 卡 | 今日 / 本周 / 本月 / 累计 费用 + tokens |
| **今日实时 Token 走势** | 紫色折线：累计 tokens；绿色柱：每 15 分钟本档；红色虚线：当前时刻 |
| 每日费用（近 60 天） | 按日柱图 |
| 每周费用（近 16 周） | 按周柱图 |
| 每月费用 | 按月柱图 |
| 按模型费用 Top10 | 横向条形图，费用多的模型一目了然 |
| 明细表 | 每日逐行，可按列排序 |

所有柱子都有**鼠标悬停提示**，会显示对应时段或模型的具体数字。

## 安装

### 前置要求

- macOS（LaunchAgent 用）；Linux 下脚本本身也能跑，只是 LaunchAgent 部分要改成 cron / systemd。
- Node.js 20+（装 ccusage 用）
- Python 3.10+（脚本本身，macOS / Homebrew 自带）

### 1. 安装 ccusage

```bash
npm install -g ccusage
ccusage daily   # 验证能看到数据
```

### 2. 放置脚本

```bash
mkdir -p ~/.claude/scripts
curl -o ~/.claude/scripts/claude-usage-report.py \
  https://raw.githubusercontent.com/TZD666/ccusage-dashboard/main/claude-usage-report.py
```

### 3. 先跑一次验证

```bash
python3 ~/.claude/scripts/claude-usage-report.py
open ~/Desktop/token-usage/index.html
```

应当能看到完整面板，含今日实时曲线。

### 4.（可选）配置自动刷新

下载 plist 模板并把两处占位符替换成你的真实路径：

```bash
curl -o /tmp/agent.plist \
  https://raw.githubusercontent.com/TZD666/ccusage-dashboard/main/com.example.claude-usage-report.plist

# __HOME__   -> $HOME      (例如 /Users/alice)
# __PYTHON__ -> $(which python3)
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__PYTHON__|$(which python3)|g" \
    /tmp/agent.plist > ~/Library/LaunchAgents/com.$USER.claude-usage-report.plist

# 同步修改 Label（可选，好处是 launchctl list 里好找）
sed -i '' "s|com.example.claude-usage-report|com.$USER.claude-usage-report|g" \
    ~/Library/LaunchAgents/com.$USER.claude-usage-report.plist

launchctl load ~/Library/LaunchAgents/com.$USER.claude-usage-report.plist
```

加载后会立即生成一次 HTML，然后每 30 秒自动重生成。HTML 本身带 `<meta http-equiv="refresh" content="30">`，浏览器同步自动刷新。**关终端、重启电脑都不会停。**

## 使用方式

### 手动跑一次

```bash
python3 ~/.claude/scripts/claude-usage-report.py
```

### 终端守护（关终端即停，最轻量）

```bash
python3 ~/.claude/scripts/claude-usage-report.py --watch 30
```

### 只改 meta refresh 周期（外部调度用）

```bash
python3 ~/.claude/scripts/claude-usage-report.py --refresh 30
```

### LaunchAgent 管理

```bash
# 查看运行状态（应看到 label + 上次退出码 0）
launchctl list | grep claude-usage

# 停
launchctl unload ~/Library/LaunchAgents/com.$USER.claude-usage-report.plist

# 启
launchctl load ~/Library/LaunchAgents/com.$USER.claude-usage-report.plist

# 看日志
tail -f ~/Library/Logs/claude-usage-report.log
```

## 输出路径

| 路径 | 说明 |
|---|---|
| `~/Desktop/token-usage/index.html` | 生成的面板，浏览器打开即看 |
| `~/Library/Logs/claude-usage-report.log` | LaunchAgent 运行日志 |

脚本顶部两行常量 `OUTPUT_DIR` / `OUTPUT_FILE` 可以自行改位置。

## 数据口径

- **所有费用数字** 来自 `ccusage daily --offline --json`，使用 ccusage 捆绑的 LiteLLM 定价表。你可以 `npm update -g ccusage` 更新定价。
- **今日实时曲线** 直接解析 `~/.claude/projects/**/*.jsonl` 里今天的消息，按 15 分钟分桶，用 `message.id` 去重，跳过 `isSidechain` / 非 assistant 消息。token 合计与 ccusage 一致（容忍因执行时差造成的几秒内的微小偏差）。
- **不涉及 Anthropic API 调用**，这套监控本身不产生任何 token 开销。

## 不是干什么的

- ❌ 不上传数据到任何服务器
- ❌ 不支持 Codex CLI / Gemini CLI / OpenCode（因为作者本人只用 Claude Code；想扩展欢迎 PR）
- ❌ 不做 public leaderboard / shareable profile
- ❌ 不改你任何 Claude Code 配置

## 和 vibeusage / ccusage 的关系

| | ccusage | vibeusage | 本仓库 |
|---|---|---|---|
| 是否上传 | ❌ 否 | ✅ 是 | ❌ 否 |
| 覆盖 CLI | Claude / Codex | 7+ 种 | Claude |
| 形态 | CLI 表格 + 轻 dashboard | 本地 hook + 远端 dashboard | 本地静态 HTML |
| 日内实时 token 曲线 | 部分 (`blocks --live` TUI) | 有 | 有（15 min 分桶） |
| 依赖 | Node | Node + 账户 | Node (ccusage) + Python3 |

简而言之：**ccusage 做采集，本仓库做可视化**。

## 开发

项目本体只有一个 Python 文件（~400 行，零第三方依赖）。主要入口：

- `load_intraday(bucket_min)` — 扫 jsonl，按分钟分桶
- `build_aggregates(daily_rows)` — ccusage daily → 周/月/模型聚合
- `svg_bar_chart` / `svg_hbar` / `svg_intraday_chart` — 手写 SVG 渲染器（无 Chart.js 等 CDN 依赖，完全离线）
- `render_html` — 单文件模板
- `generate_once` — 一次生成的高层入口
- `main` — CLI + `--watch` 守护模式

欢迎 Issue / PR。

## License

MIT

---

觉得有用的话，欢迎 ⭐ Star · 作者 [@TZD666](https://github.com/TZD666)
