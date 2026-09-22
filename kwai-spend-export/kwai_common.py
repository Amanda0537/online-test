"""Kwai Ads 每日花费导出 —— 公共逻辑。

设计要点：不依赖后台页面的 CSS 选择器（改版即失效），而是监听报表页
发出的 XHR，在返回的 JSON 里启发式地定位「日期 + 消耗」这对字段。
DOM 表格抓取只作为兜底。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 字段识别

# 后台/接口里日期字段可能的写法
DATE_KEY_RE = re.compile(
    r"^(stat_?date|stat_?time|date|dt|day|logdate|log_?date|report_?date)$", re.I
)
# 消耗字段可能的写法（charge/cost/consume/spend 是 Kwai 系常见命名）
COST_KEY_RE = re.compile(
    r"^(stat_?cost|total_?cost|cost|charge|charged?|charge_?amount|spend|spending|"
    r"consume|consumption|expense|amount)$",
    re.I,
)
# 明显不是「花费」的字段，避免误命中
COST_KEY_DENY_RE = re.compile(r"(rate|ratio|avg|average|per|cpc|cpm|cpa|roi|roas)", re.I)

DATE_VALUE_RE = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})$")


def normalize_date(value) -> str | None:
    """把 '20260921' / '2026-09-21' / '2026/09/21' / 毫秒时间戳 统一成 YYYY-MM-DD。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 10 位秒 / 13 位毫秒时间戳
        num = int(value)
        if 10**12 <= num <= 10**13:
            num //= 1000
        if 10**9 <= num <= 2 * 10**9:
            return dt.datetime.utcfromtimestamp(num).strftime("%Y-%m-%d")
        value = str(num)
    text = str(value).strip()
    m = DATE_VALUE_RE.match(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # 形如 '2026-09-21 00:00:00'
    m = DATE_VALUE_RE.match(text.split(" ")[0].split("T")[0].replace("-", "").rjust(8, "0")[:8])
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def to_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------- JSON 启发式提取


def iter_dict_lists(node, path="$"):
    """深度遍历，产出所有 (path, list_of_dicts)。"""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield path, node
        for i, item in enumerate(node):
            yield from iter_dict_lists(item, f"{path}[{i}]")
    elif isinstance(node, dict):
        for key, val in node.items():
            yield from iter_dict_lists(val, f"{path}.{key}")


def score_rows(rows: list[dict]) -> tuple[str, str] | None:
    """在一组行里找出 (日期字段, 消耗字段)；找不到返回 None。"""
    sample = rows[0]
    date_key = next((k for k in sample if DATE_KEY_RE.match(str(k))), None)
    if date_key is None:
        # 字段名不认识时，看值长得像不像日期
        date_key = next(
            (k for k, v in sample.items() if normalize_date(v) is not None), None
        )
    if date_key is None:
        return None

    candidates = [
        k
        for k in sample
        if COST_KEY_RE.match(str(k)) and not COST_KEY_DENY_RE.search(str(k))
    ]
    if not candidates:
        return None
    # 多个候选时优先最「专指花费」的命名
    priority = ["statcost", "totalcost", "cost", "charge", "charged", "spend", "consume"]

    def rank(key: str) -> int:
        flat = re.sub(r"[_\s]", "", str(key)).lower()
        return priority.index(flat) if flat in priority else len(priority)

    candidates.sort(key=rank)
    return date_key, candidates[0]


def extract_daily_rows(payload) -> list[dict] | None:
    """从一个接口返回体里抽出 [{date, cost_raw, source_keys}, ...]。"""
    best = None
    for path, rows in iter_dict_lists(payload):
        keys = score_rows(rows)
        if not keys:
            continue
        date_key, cost_key = keys
        parsed = []
        for row in rows:
            date = normalize_date(row.get(date_key))
            cost = to_number(row.get(cost_key))
            if date is None or cost is None:
                continue
            parsed.append({"date": date, "cost_raw": cost})
        if not parsed:
            continue
        # 行数越多越可能是「按天的明细」而不是「汇总的一行」
        if best is None or len(parsed) > len(best["rows"]):
            best = {
                "rows": parsed,
                "path": path,
                "date_key": date_key,
                "cost_key": cost_key,
            }
    return best


# ------------------------------------------------------------- DOM 兜底抓取

TABLE_JS = r"""
() => {
  const out = [];
  for (const table of document.querySelectorAll('table')) {
    const headers = [...table.querySelectorAll('thead th, thead td')]
      .map(e => e.innerText.trim());
    const rows = [...table.querySelectorAll('tbody tr')].map(tr =>
      [...tr.querySelectorAll('td, th')].map(td => td.innerText.trim()));
    if (headers.length && rows.length) out.push({ headers, rows });
  }
  return out;
}
"""


def rows_from_tables(tables: list[dict]) -> list[dict] | None:
    """从页面表格里找 日期列 + 消耗列。表头是中文，所以单独匹配一套关键词。"""
    date_words = ("日期", "时间", "date", "day")
    cost_words = ("消耗", "花费", "花销", "费用", "支出", "cost", "spend", "charge")
    for table in tables:
        headers = table["headers"]
        di = next(
            (i for i, h in enumerate(headers) if any(w in h.lower() or w in h for w in date_words)),
            None,
        )
        ci = next(
            (i for i, h in enumerate(headers) if any(w in h.lower() or w in h for w in cost_words)),
            None,
        )
        if di is None or ci is None:
            continue
        parsed = []
        for cells in table["rows"]:
            if di >= len(cells) or ci >= len(cells):
                continue
            date = normalize_date(cells[di])
            cost = to_number(cells[ci])
            if date is None or cost is None:
                continue
            parsed.append({"date": date, "cost_raw": cost})
        if parsed:
            return parsed
    return None


# ------------------------------------------------------------------- CSV 落盘

CSV_FIELDS = ["date", "account_id", "spend", "currency", "fetched_at", "source"]


def merge_csv(path: str | Path, new_rows: list[dict]) -> tuple[int, int]:
    """按 date 主键合并写入（同一天重复抓取会覆盖为最新值）。返回 (新增, 更新)。"""
    path = Path(path)
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if row.get("date"):
                    existing[row["date"]] = row

    added = updated = 0
    for row in new_rows:
        if row["date"] in existing:
            if existing[row["date"]].get("spend") != row["spend"]:
                updated += 1
        else:
            added += 1
        existing[row["date"]] = row

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for date in sorted(existing):
            writer.writerow({k: existing[date].get(k, "") for k in CSV_FIELDS})
    return added, updated


# ---------------------------------------------------------------- 配置 / 杂项


def load_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"找不到配置文件 {cfg_path}。\n"
            f"请先执行: cp config.example.json config.json 然后按需修改。"
        )
    with cfg_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def looks_like_login_page(url: str, html: str) -> bool:
    if re.search(r"/(login|passport|signin)", url, re.I):
        return True
    return ("请登录" in html or "登录后台" in html) and "报表" not in html


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def launch_kwargs() -> dict:
    """允许用 PW_CHROMIUM_PATH 指定 chromium 可执行文件。

    正常情况下装完 `playwright install chromium` 就不用管这个；只有当机器上
    已有一份浏览器、不想再下一遍时才需要设置。
    """
    path = os.environ.get("PW_CHROMIUM_PATH")
    return {"executable_path": path} if path else {}
