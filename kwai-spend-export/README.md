# Kwai Ads 每日花费 -> 飞书表格

每天 5:00 自动打开 ads.kwai.com 报表页，抓**账户维度的每日消耗**（北京时区），
写入本地 CSV，再按日期幂等同步到飞书电子表格。

```
launchd 每天 05:00
  └─ export_spend.py   Playwright 复用登录态 -> 抓日消耗 -> 合并进 CSV
  └─ push_feishu.py    读 CSV -> 按日期 upsert 进飞书表格
        └─ 任一步失败 -> 飞书群机器人告警
```

两步分开跑是有意的：抓取和推送是两类失败。CSV 是本地事实源，
飞书挂了只需重跑推送，数据不会丢。

## 先看这个：它的稳定性边界

基于登录态的爬虫有一个**无法自动化消除**的环节：cookie 会过期（通常几天到几周），
过期后必须有人手动重登一次。脚本会在过期时立刻发飞书告警，重登也简化成了一条命令
（`./relogin.sh`），但做不到完全无人值守。

要真正无人值守，得走 Kwai 官方 Reporting API（OAuth + refresh_token 自动续期），
需邮件申请授权。这套代码的抓取层是可替换的，将来接 API 时飞书写入、调度、告警都不用动。

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
cp config.example.json config.json
cp .env.example .env
```

然后改 `config.json`（报表地址、时区）和 `.env`（飞书凭证）。

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

## 飞书配置

1. 到[飞书开发者后台](https://open.feishu.cn/app)建一个**自建应用**
2. 权限管理里开通电子表格的**查看、评论、编辑**权限，然后**发布应用**（不发布 API 不生效）
3. 打开你的目标电子表格 → 右上角分享 → 把这个应用添加为**可编辑**协作者
   （这一步最容易漏，漏了会报无权限）
4. 从应用详情页拿 `App ID` / `App Secret`，填进 `.env`
5. 表格 URL 里 `/sheets/` 后面那串是 `FEISHU_SPREADSHEET_TOKEN`，
   `?sheet=` 后面那段是 `FEISHU_SHEET_ID`
6. 可选：建个飞书群机器人，把 webhook 填进 `FEISHU_WEBHOOK`，失败时会推群

配好后**先自检**，别直接上定时任务：

```bash
python3 push_feishu.py --check
```

它会依次验证鉴权、读表、写表，最后把测试行清掉。四步都 OK 才算通。

表格第一列是日期，作为主键。重复推送同一天只会覆盖，不会新增行。

## 每天 5:00 自动跑

```bash
./install_schedule.sh
```

装完立即试跑一次确认：

```bash
launchctl start com.kwai.spend.sync
tail -f logs/run_$(date +%Y-%m).log
```

想换时间：`HOUR=7 MINUTE=30 ./install_schedule.sh`

用 launchd 而不是 cron，是因为笔记本 5AM 多半在休眠 —— cron 会直接漏跑，
launchd 会在唤醒后补跑一次。

**前提：这台机器 5:00 必须开着**（休眠可以，关机不行）。要真正稳定，
放一台常开的机器。不建议放云服务器或 CI：云端 IP 和你平时登录的 IP 不一致，
大概率触发风控，反而更不稳。

### Linux 服务器

```bash
crontab -e
0 5 * * * /绝对路径/kwai-spend-export/run_daily.sh
```

服务器没有图形界面，`login.py` 跑不了。做法是在自己电脑上生成 `auth.json` 再拷过去
（等同于登录凭证，走安全通道传）。

### 为什么抓最近 7 天而不是只抓前一天

5:00 时前一天的消耗可能还在校准。固定回补最近 `days_back` 天，
CSV 和飞书都按日期主键覆盖，历史数字会自动修正，也不会写重复行。

## 登录态会过期

cookie 有有效期（通常几天到几周），过期后任务立刻以退出码 2 结束、推飞书告警，
**不会**去动飞书表格（避免写入半截数据）。续期就一条命令：

```bash
./relogin.sh
```

它会弹浏览器让你登录，然后立刻验证一次抓取。这是这套方案无法自动化掉的一环。

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
| 1 | 连续 3 次抓取失败 |
| 2 | 登录态失效，跑 `./relogin.sh` |
| 3 | 没抓到目标区间的数据，跑 `--discover` 排查 |
| 4 | 飞书配置缺失，检查 `.env` |
| 5 | 数据已进 CSV，但推送飞书失败（数据没丢，重跑 `push_feishu.py` 即可） |

## 自测

```bash
python3 test_extraction.py   # 字段识别、时区、CSV 合并
python3 test_replay.py       # 日期参数改写
python3 test_feishu.py       # 飞书幂等写入（起模拟服务端，不碰真实表格）
```

覆盖日期归一化、金额解析、接口 JSON 的字段识别（含多候选择优、汇总 vs 明细）、
表格兜底、CSV 按日期合并、跨日时区换算、日期参数改写（含格式保持、
不误伤账户 ID 这类数字），以及飞书幂等写入（重推不产生重复行、回补能改老值）。
都不需要网络、登录态或真实飞书应用。

## 安全须知

- `auth.json` 等同于你的后台登录凭证，`.gitignore` 已排除，**不要提交、不要外发**。
- `.env` 里是飞书应用密钥，同样已排除，泄露等于把表格写权限给了别人。
- `discover_dump.json` 含接口原始返回，可能带账户数据，发给别人前先自己过一眼。
- 这是用你自己的登录态访问你自己的账户，属于常规自动化；但它依赖后台前端结构，
  平台改版可能随时让它失效。长期稳定的方案仍是官方 API。

## 已知局限

- **未在真实的 ads.kwai.com 上验证过。** 字段识别、时区换算、日期参数改写都有自测覆盖，
  端到端链路（含请求重放）在本地模拟报表页上跑通过，但真实后台的接口结构、
  日期参数命名需要你跑一次 `--discover` 来确认。
- **飞书接口地址未对真实环境验证过**（写代码的环境访问不了飞书文档站）。
  幂等逻辑用模拟服务端测透了，但接口路径请用 `push_feishu.py --check` 实地验一次；
  若飞书调整了接口版本，改 `feishu_sheet.py` 顶部的常量即可。
- 只做账户维度总消耗，不拆 campaign / adgroup。
- cookie 过期需人工重登，无法自动化（见开头「稳定性边界」）。
- `date` 列以后台报表自身的统计时区为准，脚本不做换算；跨时区部署时请配 `report_timezone`，详见「时区」一节。
