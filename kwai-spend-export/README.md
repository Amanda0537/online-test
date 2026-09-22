# Kwai Ads 账户每日花费导出

用 Playwright 复用你自己的登录态，打开 ads.kwai.com 报表页，把**账户维度的每日消耗**
合并写入一个 CSV，并支持每天凌晨定时自动跑。

## 它是怎么取数的

不去猜后台页面的 CSS 选择器（后台一改版就失效），而是**监听报表页自己发出的 XHR**，
在返回的 JSON 里自动识别「日期字段 + 消耗字段」。识别不到才回退去抓页面表格。

拿指定日期的数据靠**请求重放**：自定义报表页（`#/report/customReport`）的日期区间是
页面内部状态、不在 URL 上，所以拼 URL 行不通。脚本先让页面正常加载一次、捕获它发出的
报表请求，若返回的数据不含目标日期，就把请求里的日期参数改写成目标区间、用同一个登录态
重发一次。这样既不用点日期控件，也不受前端改版影响。

## 安装

需要 Python 3.10+。

```bash
cd kwai-spend-export
pip install -r requirements.txt
playwright install chromium        # 下载浏览器，约 150MB，只需一次
cp config.example.json config.json # 然后按需改 config.json
```

> 如果机器上已有 Playwright 的 chromium，不想再下一份，可以设
> `export PW_CHROMIUM_PATH=/path/to/chrome` 直接复用。

## 先在后台把报表建好（重要）

脚本读的是**你在后台配好的那张自定义报表**，所以先手动准备一次：

1. 进 **Reports → Create Report**
2. 粒度选 **By day**
3. **时区**下拉：确认选的是哪个（默认 `UTC+08:00(CST)`）—— 这个值要和 config.json 里的
   `report_timezone` 对上，详见下文「时区」
4. **Configure Report** 里把指标勾上（至少要有消耗/Cost）
5. **Save** 保存，记下报表名

之后脚本每次打开这张报表页，日期区间由脚本自己改写，你不用管。

## 三步跑通

```bash
# 1. 一次性登录：会弹出浏览器窗口，你手动登录（验证码/二次验证都在这一步做完）
python3 login.py

# 2. 探测：看报表页到底返回了什么，确认能识别出日消耗
python3 export_spend.py --discover --show

# 3. 正式导出
python3 export_spend.py
```

第 2 步会生成 `discover_dump.json`（所有 JSON 接口 + 命中情况）和 `discover_page.png`
（页面截图）。**如果第 3 步取不到数，就是靠这个文件来定位问题的。**

## 日期区间

凌晨 1 点跑有个坑：报表页默认区间通常不含前一天。脚本用**请求重放**解决 —— 发现首次
返回不含目标日期时，自动改写报表请求里的日期参数再发一次。日志里会看到：

```
[01:00:03] 页面首次返回 7 行（字段 statDate / charge）
[01:00:03] 首次返回不含目标区间 2026-09-15 ~ 2026-09-22，尝试重放请求……
[01:00:04] 重放报表请求（改写了 2 个日期参数）→ 2026-09-15 ~ 2026-09-22
[01:00:04] 重放取到 8 行
```

手动指定区间：

```bash
python3 export_spend.py --start 2026-09-21 --end 2026-09-21
```

如果日志里出现「没找到可改写的日期参数」，说明该后台的日期参数命名不在识别范围内 ——
跑一次 `--discover`，把 `discover_dump.json` 里报表请求的参数名看一下，
在 `request_replay.py` 的 `START_KEY_RE` / `END_KEY_RE` 里补上即可。

极少数后台确实把日期放在 URL 上，那种情况可以配 `report_url_template`，
占位符：`{start}` `{end}`（`YYYY-MM-DD`）、`{start_compact}` `{end_compact}`（`YYYYMMDD`）。

## 每日凌晨 1 点自动跑

`run_daily.sh` 是定时入口：带文件锁（防重入）、失败重试（60s/180s）、按月切分日志，
登录态过期时会弹系统通知（设了 `WEBHOOK_URL` 还会推到企微/钉钉/Slack）。

它抓的是最近 `days_back` 天而不是只抓前一天 —— 广告平台的消耗有回传延迟和事后校准，
多回补几天能让历史数字自动修正。CSV 按日期合并，同一天重复抓只会更新、不会新增行。

### macOS（推荐 launchd）

cron 在笔记本休眠时不会补跑，launchd 会在唤醒后补跑一次，更适合凌晨的任务。

把下面存成 `~/Library/LaunchAgents/com.kwai.spend.export.plist`（路径换成你自己的）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kwai.spend.export</string>
  <key>ProgramArguments</key>
  <array><string>/绝对路径/kwai-spend-export/run_daily.sh</string></array>
  <key>WorkingDirectory</key><string>/绝对路径/kwai-spend-export</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>1</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardErrorPath</key><string>/绝对路径/kwai-spend-export/logs/launchd.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.kwai.spend.export.plist
launchctl start com.kwai.spend.export   # 立刻跑一次验证
```

### Linux / 常开服务器（cron）

```bash
crontab -e
# 每天 01:00
0 1 * * * /绝对路径/kwai-spend-export/run_daily.sh
```

服务器上没有图形界面时，`login.py` 需要有界面的浏览器才能登录。做法是：
在自己电脑上跑 `login.py` 生成 `auth.json`，再把这个文件拷到服务器（它等同于登录凭证，
走安全通道传，别丢群里）。

### Windows（任务计划程序）

1. 打开「任务计划程序」→「创建基本任务」
2. 触发器：每天，01:00
3. 操作：启动程序 → `python`，参数 `export_spend.py`，起始于 `C:\路径\kwai-spend-export`
4. 在任务属性里勾选「不管用户是否登录都要运行」和「唤醒计算机运行此任务」

### 一个前提

**凌晨 1 点机器得是开着的**。笔记本关机/休眠时 cron 不会补跑（launchd 会在唤醒后补）。
要真正稳定，放在一台常开的机器或服务器上跑。

## 登录态会过期

cookie 有有效期（通常几天到几周），过期后任务会以退出码 2 结束并告警。
届时重新在有界面的环境跑一次 `python3 login.py` 即可。这是这套方案无法自动化掉的一环 ——
如果你不想每隔一阵手动续一次，正路是去申请 Kwai Ads 开放平台的 API 授权。

## 时区

**CSV 里 `date` 列的时区 = 你账户在快手后台的报表统计时区**，脚本不做任何换算 ——
后台接口给的统计日期是什么，写进 CSV 的就是什么。这样导出的数字能和你在后台页面上
看到的逐日对上，对账不会错位。

`report_timezone` 这一项**不会改变 `date` 列的取值**，它管的是另外两件事：

1. **「今天/昨天」怎么算。** 定时任务默认抓 `[今天 - days_back, 今天]`。如果你把任务
   挂在一台 UTC 的服务器上、账户报表却按北京时间统计，两边的「今天」会差一天。
   配上这一项，脚本就按报表时区算日期窗口，而不是按机器本地时区。
2. **万一接口返回的是时间戳**（而不是日期字符串），按这个时区落地成日期。
   如果不配就会按机器本地时区转，跨日临界点上会偏一天。

配置示例：

```json
"report_timezone": "Asia/Shanghai"
```

留空则退回机器本地时区。也可以用环境变量 `KWAI_REPORT_TZ` 覆盖。

**怎么确认**：时区在 Kwai 后台是**报表级**的设置 —— 报表页顶部有个时区下拉，
默认 `UTC+08:00(CST)`，可以每张报表单独改。config.json 里的 `report_timezone` 要和
**你那张报表里选的那个值**一致：

| 报表页下拉选的 | config.json 填 |
| --- | --- |
| `UTC+08:00(CST)` | `Asia/Shanghai` |
| `UTC+00:00` | `UTC` |
| `UTC-08:00(PST)` | `America/Los_Angeles` |
| `UTC-05:00(EST)` | `America/New_York` |

改了报表里的时区，记得同步改 config.json，否则「今天/昨天」的判断会错位。
最终验证：跑一次导出，拿 CSV 里某一天的数字和后台页面同一天的数字对一下。

`fetched_at` 列记的是**抓取动作发生的时刻**，带时区偏移（如 `2026-09-22T01:00:11+08:00`），
和 `date` 列不是一回事，不要混用。

## 输出

`kwai_daily_spend.csv`，按日期排序、按日期主键合并：

```csv
date,account_id,spend,currency,fetched_at,source
2026-09-20,75566086,980.00,USD,2026-09-22T01:00:11,xhr:https://ads.kwai.com/rest/...
2026-09-21,75566086,1530.25,USD,2026-09-22T01:00:11,xhr:https://ads.kwai.com/rest/...
```

## config.json 说明

| 字段 | 说明 |
| --- | --- |
| `account_id` | 账户 ID，只用于写进 CSV 的一列 |
| `report_url` | 报表页地址，用自定义报表页 `#/report/customReport` |
| `report_url_template` | 可选，仅当后台把日期放在 URL 上时才需要，见「日期区间」 |
| `auth_file` | 登录态文件路径，默认 `auth.json` |
| `output_csv` | 产出 CSV 路径 |
| `days_back` | 每次回补多少天，默认 7 |
| `report_timezone` | **你账户在快手后台的报表统计时区**（如 `Asia/Shanghai`）。见下文「时区」 |
| `headless` | 定时任务用 `true`；调试时命令行加 `--show` 可临时看窗口 |
| `cost_divisor` | **首次务必核对**：若接口返回的是「厘/分」，这里填 1000 或 100 |
| `currency` | 写进 CSV 的币种标记 |
| `response_url_hints` | 只解析 URL 含这些词的接口，减少误判；取不到数时可放宽 |
| `settle_ms` | 页面加载后额外等待毫秒数，网慢就调大 |

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | 登录态失效，需重跑 `login.py` |
| 3 | 没抓到目标区间的数据，跑 `--discover` 排查 |

## 自测

```bash
python3 test_extraction.py   # 字段识别、时区、CSV 合并
python3 test_replay.py       # 日期参数改写
```

覆盖日期归一化、金额解析、接口 JSON 的字段识别（含多候选择优、汇总 vs 明细）、
表格兜底、CSV 按日期合并、跨日时区换算，以及日期参数改写（含格式保持、
不误伤账户 ID 这类数字）。不需要网络和登录态。

## 安全须知

- `auth.json` 等同于你的后台登录凭证，`.gitignore` 已排除，**不要提交、不要外发**。
- `discover_dump.json` 含接口原始返回，可能带账户数据，发给别人前先自己过一眼。
- 这是用你自己的登录态访问你自己的账户，属于常规自动化；但它依赖后台前端结构，
  平台改版可能随时让它失效。长期稳定的方案仍是官方 API。

## 已知局限

- **未在真实的 ads.kwai.com 上验证过。** 字段识别、时区换算、日期参数改写都有自测覆盖，
  端到端链路（含请求重放）在本地模拟报表页上跑通过，但真实后台的接口结构、
  日期参数命名需要你跑一次 `--discover` 来确认。
- 只做账户维度总消耗，不拆 campaign / adgroup。
- `date` 列以后台报表自身的统计时区为准，脚本不做换算；跨时区部署时请配 `report_timezone`，详见「时区」一节。
