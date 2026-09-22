# Kwai Ads 账户每日花费导出

用 Playwright 复用你自己的登录态，打开 ads.kwai.com 报表页，把**账户维度的每日消耗**
合并写入一个 CSV，并支持每天凌晨定时自动跑。

## 它是怎么取数的

不去猜后台页面的 CSS 选择器（后台一改版就失效），而是**监听报表页自己发出的 XHR**，
在返回的 JSON 里自动识别「日期字段 + 消耗字段」。识别不到才回退去抓页面表格。
所以后台前端改版通常不影响它，只有接口字段改名才需要调整。

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

## 日期区间（定时任务前必须确认）

这是最容易踩的坑：**凌晨 1 点跑的时候，报表页默认区间通常是「今天」**，而此时今天
才刚开始、消耗接近 0，前一天的数据可能压根不在返回里。

脚本默认抓 `[今天 - days_back, 今天]`，也支持显式指定：

```bash
python3 export_spend.py --start 2026-09-21 --end 2026-09-21
```

但要让**页面**按这个区间返回数据，得告诉脚本怎么把日期拼进 URL。跑一次 `--discover`，
在后台手动选好日期范围，看地址栏变成什么样，然后在 `config.json` 里加一行，例如：

```json
"report_url_template": "https://ads.kwai.com/?accountId=75566086#/report?startDate={start}&endDate={end}"
```

可用占位符：`{start}` `{end}`（`YYYY-MM-DD`）、`{start_compact}` `{end_compact}`（`YYYYMMDD`）。
不配这一项就按报表页的默认区间取，**上线定时任务前请先验证前一天的数据确实能抓到**。

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
| `report_url` | 报表页地址 |
| `report_url_template` | 可选，带日期占位符的报表页地址，见上文「日期区间」 |
| `auth_file` | 登录态文件路径，默认 `auth.json` |
| `output_csv` | 产出 CSV 路径 |
| `days_back` | 每次回补多少天，默认 7 |
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
python3 test_extraction.py
```

覆盖日期归一化、金额解析、接口 JSON 的字段识别（含多候选择优、汇总 vs 明细）、
中文表头的表格兜底、CSV 按日期合并。不需要网络和登录态。

## 安全须知

- `auth.json` 等同于你的后台登录凭证，`.gitignore` 已排除，**不要提交、不要外发**。
- `discover_dump.json` 含接口原始返回，可能带账户数据，发给别人前先自己过一眼。
- 这是用你自己的登录态访问你自己的账户，属于常规自动化；但它依赖后台前端结构，
  平台改版可能随时让它失效。长期稳定的方案仍是官方 API。

## 已知局限

- **未在真实的 ads.kwai.com 上验证过。** 字段识别逻辑和 CSV 合并有自测覆盖，
  端到端链路在本地模拟报表页上跑通过，但真实后台的接口结构、日期参数格式需要你跑一次
  `--discover` 来确认。
- 只做账户维度总消耗，不拆 campaign / adgroup。
- 日期以后台报表自身的时区为准，脚本不做时区换算。
