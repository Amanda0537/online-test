#!/usr/bin/env python3
"""日期参数改写的自测。不需要网络。"""
import datetime as dt
import json

from request_replay import (
    clean_headers,
    format_like,
    rewrite_body,
    rewrite_json,
    rewrite_query,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n    got : {got!r}\n    want: {want!r}")


S = dt.date(2026, 9, 21)
E = dt.date(2026, 9, 21)

# ---- 格式保持：接口对日期格式很挑，必须按原样渲染 ----
check("格式 连字符", format_like("2025-10-22", S), "2026-09-21")
check("格式 紧凑", format_like("20251022", S), "20260921")
check("格式 斜杠", format_like("2025/10/22", S), "2026/09/21")
check("格式 带时间", format_like("2025-10-22 00:00:00", S), "2026-09-21 00:00:00")
check("格式 非日期不动", format_like("abc", S), None)
check("格式 数字ID不动", format_like("75566086", S), None)
check("格式 非法月份不动", format_like("20251399", S), None)
check("格式 年份离谱不动", format_like("19001022", S), None)
check("格式 合法紧凑仍可改", format_like("20251022", S), "20260921")

# ---- URL query ----
url = "https://ads.kwai.com/rest/report?accountId=75566086&startDate=2025-10-22&endDate=2025-10-28&granularity=day"
new, n = rewrite_query(url, S, E)
check("query 改动数", n, 2)
check("query 结果", new,
      "https://ads.kwai.com/rest/report?accountId=75566086&startDate=2026-09-21&endDate=2026-09-21&granularity=day")

# accountId 是数字但不是日期，绝不能被改
check("query 不误伤ID", "accountId=75566086" in new, True)

# 无日期参数时原样返回
check("query 无日期", rewrite_query("https://x.com/a?b=1", S, E), ("https://x.com/a?b=1", 0))
check("query 无query", rewrite_query("https://x.com/a", S, E), ("https://x.com/a", 0))

# 其它命名风格
u2, n2 = rewrite_query("https://x.com/a?begin_date=20251022&end_date=20251028", S, E)
check("query 下划线命名", (u2, n2), ("https://x.com/a?begin_date=20260921&end_date=20260921", 2))

# ---- JSON body（嵌套）----
body = json.dumps({
    "accountId": 75566086,
    "granularity": "DAY",
    "filter": {"startDate": "2025-10-22", "endDate": "2025-10-28"},
    "metrics": ["charge"],
})
new_body, n = rewrite_body(body, "application/json", S, E)
parsed = json.loads(new_body)
check("json 改动数", n, 2)
check("json start", parsed["filter"]["startDate"], "2026-09-21")
check("json end", parsed["filter"]["endDate"], "2026-09-21")
check("json 不误伤账户ID", parsed["accountId"], 75566086)
check("json 不误伤metrics", parsed["metrics"], ["charge"])

# 整数形式的紧凑日期，改完仍应是整数
b2, n2 = rewrite_body(json.dumps({"startDate": 20251022, "endDate": 20251028}), "application/json", S, E)
check("json 整数日期", json.loads(b2), {"startDate": 20260921, "endDate": 20260921})

# ---- 表单编码 ----
fb, n = rewrite_body("startDate=2025-10-22&endDate=2025-10-28&x=1", "application/x-www-form-urlencoded", S, E)
check("form 改写", (fb, n), ("startDate=2026-09-21&endDate=2026-09-21&x=1", 2))

# ---- 不该动的情况 ----
check("body 空", rewrite_body(None, "application/json", S, E), (None, 0))
check("body 非法JSON", rewrite_body("{bad", "application/json", S, E), ("{bad", 0))
check("body 无日期", rewrite_body('{"a":1}', "application/json", S, E), ('{"a":1}', 0))

# ---- 请求头清理 ----
cleaned = clean_headers({
    "Content-Type": "application/json", "Content-Length": "123",
    "Host": "ads.kwai.com", "Cookie": "x=1", "Accept-Encoding": "gzip",
})
check("头 保留必要", sorted(cleaned), ["Content-Type", "Cookie"])

# ---- 区间跨多天 ----
u3, _ = rewrite_query("https://x.com/a?startDate=2025-10-22&endDate=2025-10-28",
                      dt.date(2026, 9, 15), dt.date(2026, 9, 21))
check("多天区间", u3, "https://x.com/a?startDate=2026-09-15&endDate=2026-09-21")

if failures:
    print(f"\n✗ {len(failures)} 项失败:\n")
    for f in failures:
        print("  " + f)
    raise SystemExit(1)
print("✓ 全部通过")
