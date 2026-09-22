#!/usr/bin/env python3
"""对提取逻辑的自测。不需要网络，也不需要登录态。

用法: python3 test_extraction.py
"""
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kwai_common import (
    extract_daily_rows,
    get_tz,
    merge_csv,
    normalize_date,
    rows_from_tables,
    to_number,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n    got : {got!r}\n    want: {want!r}")


# ---- 日期归一化 ----
check("date 紧凑", normalize_date("20260921"), "2026-09-21")
check("date 连字符", normalize_date("2026-09-21"), "2026-09-21")
check("date 斜杠", normalize_date("2026/09/21"), "2026-09-21")
check("date 整数", normalize_date(20260921), "2026-09-21")
check("date 毫秒戳", normalize_date(1789948800000, timezone.utc), "2026-09-21")
check("date 非日期", normalize_date("abc"), None)
check("date None", normalize_date(None), None)

# ---- 数字解析 ----
check("num 带币种", to_number("$1,234.56"), 1234.56)
check("num 纯数", to_number(12.5), 12.5)
check("num 空串", to_number("--"), None)
check("num bool", to_number(True), None)

# ---- 时区 ----
SH = ZoneInfo("Asia/Shanghai")
# 2026-09-21 17:00 UTC == 2026-09-22 01:00 北京时间：跨日的临界点
ts = int(datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc).timestamp())
check("时间戳 按UTC", normalize_date(ts, timezone.utc), "2026-09-21")
check("时间戳 按北京时间", normalize_date(ts, SH), "2026-09-22")

# 关键：字符串日期是后台直接给的统计日，任何时区下都必须原样保留，不能被换算
for tz in (None, timezone.utc, SH, ZoneInfo("America/Los_Angeles")):
    check(f"字符串日期不被换算 tz={tz}", normalize_date("20260921", tz), "2026-09-21")

check("get_tz 读配置", get_tz({"report_timezone": "Asia/Shanghai"}), SH)
check("get_tz 留空退回本地", get_tz({}), None)

# 时区要能贯穿到整条提取链路
payload_ts = {"list": [{"statDate": ts * 1000, "charge": 100.0}]}
check("链路 按UTC", extract_daily_rows(payload_ts, timezone.utc)["rows"][0]["date"], "2026-09-21")
check("链路 按北京时间", extract_daily_rows(payload_ts, SH)["rows"][0]["date"], "2026-09-22")


# ---- 接口返回体：常见嵌套形态 ----
payload = {
    "code": 0,
    "data": {
        "total": {"charge": 9999.0},
        "details": [
            {"statDate": "20260919", "charge": 1200.5, "impression": 10000, "ctr": 0.03},
            {"statDate": "20260920", "charge": 980.0, "impression": 8000, "ctr": 0.02},
            {"statDate": "20260921", "charge": 1530.25, "impression": 12000, "ctr": 0.04},
        ],
    },
}
hit = extract_daily_rows(payload)
check("接口 日期字段", hit["date_key"], "statDate")
check("接口 消耗字段", hit["cost_key"], "charge")
check("接口 行数", len(hit["rows"]), 3)
check("接口 首行", hit["rows"][0], {"date": "2026-09-19", "cost_raw": 1200.5})

# 多个花费候选时应挑最专指的那个，且不能挑到 cpc/roi 这类
payload2 = {"rows": [{"date": "2026-09-21", "cpc": 1.2, "roi": 3.0, "cost": 500.0, "amount": 1.0}]}
hit2 = extract_daily_rows(payload2)
check("多候选 选 cost", hit2["cost_key"], "cost")

# 只有汇总没有按天明细 -> 不应误报
check("无日期字段", extract_daily_rows({"data": {"total": {"cost": 100}}}), None)

# 字段名不认识，但值长得像日期
payload3 = {"list": [{"d": "2026-09-21", "consume": 42.0}]}
hit3 = extract_daily_rows(payload3)
check("值推断日期", hit3["rows"], [{"date": "2026-09-21", "cost_raw": 42.0}])

# 应挑行数更多的那组（按天明细 > 单行汇总）
payload4 = {
    "summary": [{"date": "2026-09-21", "cost": 3710.75}],
    "daily": [
        {"date": "2026-09-19", "cost": 1200.5},
        {"date": "2026-09-20", "cost": 980.0},
        {"date": "2026-09-21", "cost": 1530.25},
    ],
}
check("偏好明细", len(extract_daily_rows(payload4)["rows"]), 3)

# ---- DOM 表格兜底（中文表头）----
tables = [
    {"headers": ["账户", "状态"], "rows": [["A", "投放中"]]},
    {
        "headers": ["日期", "展示数", "消耗(USD)"],
        "rows": [["2026-09-20", "8,000", "980.00"], ["2026-09-21", "12,000", "$1,530.25"]],
    },
]
check(
    "表格兜底",
    rows_from_tables(tables),
    [{"date": "2026-09-20", "cost_raw": 980.0}, {"date": "2026-09-21", "cost_raw": 1530.25}],
)
check("表格无匹配", rows_from_tables([tables[0]]), None)

# ---- CSV 按日期合并 ----
with tempfile.TemporaryDirectory() as tmp:
    csv_path = Path(tmp) / "out.csv"
    base = {"account_id": "75566086", "currency": "USD", "fetched_at": "t1", "source": "xhr"}
    added, updated = merge_csv(csv_path, [{"date": "2026-09-20", "spend": "980.00", **base}])
    check("首次写入", (added, updated), (1, 0))

    # 同一天重抓且金额变了 -> 更新而非重复
    added, updated = merge_csv(
        csv_path,
        [
            {"date": "2026-09-20", "spend": "985.00", **base},
            {"date": "2026-09-21", "spend": "1530.25", **base},
        ],
    )
    check("合并写入", (added, updated), (1, 1))

    lines = csv_path.read_text(encoding="utf-8-sig").strip().splitlines()
    check("总行数(含表头)", len(lines), 3)
    check("同日被覆盖", lines[1].split(",")[2], "985.00")
    check("按日期排序", [l.split(",")[0] for l in lines[1:]], ["2026-09-20", "2026-09-21"])

if failures:
    print(f"\n✗ {len(failures)} 项失败:\n")
    for f in failures:
        print("  " + f)
    raise SystemExit(1)
print("✓ 全部通过")
