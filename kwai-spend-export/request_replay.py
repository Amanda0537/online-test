"""把报表接口请求里的日期参数改写成目标区间，用于「重放」。

背景：ads.kwai.com 的自定义报表页 (#/report/customReport) 不把日期放在 URL 上，
日期区间是页面内部状态。所以想抓指定日期，靠拼 URL 行不通。

做法：先让页面正常加载一次、捕获它发出的报表请求，然后把请求里的日期参数替换成
我们要的区间，用同一个浏览器上下文（带着登录 cookie）重新发一次。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse as up

# 起始日期参数可能的命名
START_KEY_RE = re.compile(
    r"^(start|begin|from)_?(date|time|day)?$|^(date|time|day)_?(start|begin|from)$", re.I
)
# 结束日期参数可能的命名
END_KEY_RE = re.compile(
    r"^(end|stop|until|to)_?(date|time|day)?$|^(date|time|day)_?(end|stop|to)$", re.I
)

_COMPACT_RE = re.compile(r"^\d{8}$")
_DASHED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLASHED_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})([ T].*)$")


def _is_real_date(text: str) -> bool:
    """确认这串数字真的是个日期，而不是碰巧 8 位的账户 ID。"""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            parsed = dt.datetime.strptime(text[:10] if "-" in text or "/" in text else text, fmt)
        except ValueError:
            continue
        return 2000 <= parsed.year <= 2100
    return False


def format_like(sample: str, target: dt.date) -> str | None:
    """按原值的格式来渲染新日期 —— 接口对格式通常很挑，不能随便换。

    返回 None 表示原值看着不像日期，不要动它。
    """
    text = str(sample)
    if _COMPACT_RE.match(text):
        # 8 位数字也可能是账户 ID 之类，必须确认能解析成合理年份的日期
        return target.strftime("%Y%m%d") if _is_real_date(text) else None
    if _DASHED_RE.match(text):
        return target.strftime("%Y-%m-%d") if _is_real_date(text) else None
    if _SLASHED_RE.match(text):
        return target.strftime("%Y/%m/%d") if _is_real_date(text) else None
    m = _DATETIME_RE.match(text)
    if m and _is_real_date(m.group(1)):
        # 形如 "2025-10-22 00:00:00"，保留后面的时间部分
        return target.strftime("%Y-%m-%d") + m.group(2)
    return None


def _new_value(key: str, old, start: dt.date, end: dt.date):
    """根据参数名判断这是起始还是结束日期，返回改写后的值；不该动则返回 None。"""
    if isinstance(old, (list, dict)) or old is None:
        return None
    if START_KEY_RE.match(str(key)):
        target = start
    elif END_KEY_RE.match(str(key)):
        target = end
    else:
        return None
    formatted = format_like(old, target)
    if formatted is None:
        return None
    return int(formatted) if isinstance(old, int) else formatted


def rewrite_query(url: str, start: dt.date, end: dt.date) -> tuple[str, int]:
    """改写 URL query 里的日期参数。返回 (新URL, 改动个数)。"""
    parts = up.urlsplit(url)
    if not parts.query:
        return url, 0
    pairs = up.parse_qsl(parts.query, keep_blank_values=True)
    changed = 0
    out = []
    for key, val in pairs:
        new = _new_value(key, val, start, end)
        if new is not None and str(new) != val:
            changed += 1
            out.append((key, str(new)))
        else:
            out.append((key, val))
    if not changed:
        return url, 0
    return up.urlunsplit(parts._replace(query=up.urlencode(out))), changed


def rewrite_json(node, start: dt.date, end: dt.date) -> int:
    """就地改写 JSON 结构里的日期字段，返回改动个数。"""
    changed = 0
    if isinstance(node, dict):
        for key, val in list(node.items()):
            if isinstance(val, (dict, list)):
                changed += rewrite_json(val, start, end)
            else:
                new = _new_value(key, val, start, end)
                if new is not None and new != val:
                    node[key] = new
                    changed += 1
    elif isinstance(node, list):
        for item in node:
            changed += rewrite_json(item, start, end)
    return changed


def rewrite_body(post_data: str | None, content_type: str, start: dt.date, end: dt.date):
    """改写请求体。支持 JSON 和表单编码两种。返回 (新body, 改动个数)。"""
    if not post_data:
        return post_data, 0

    if "json" in (content_type or "").lower() or post_data.lstrip()[:1] in "{[":
        try:
            payload = json.loads(post_data)
        except (ValueError, TypeError):
            return post_data, 0
        changed = rewrite_json(payload, start, end)
        if not changed:
            return post_data, 0
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), changed

    # 表单编码
    if "=" in post_data:
        pairs = up.parse_qsl(post_data, keep_blank_values=True)
        if pairs:
            changed = 0
            out = []
            for key, val in pairs:
                new = _new_value(key, val, start, end)
                if new is not None and str(new) != val:
                    changed += 1
                    out.append((key, str(new)))
                else:
                    out.append((key, val))
            if changed:
                return up.urlencode(out), changed
    return post_data, 0


# 重放时不能照抄的请求头：长度会变，host 由 URL 决定，编码交给底层
SKIP_HEADERS = {"content-length", "host", "accept-encoding", "connection"}


def clean_headers(headers: dict) -> dict:
    return {k: v for k, v in (headers or {}).items() if k.lower() not in SKIP_HEADERS}
