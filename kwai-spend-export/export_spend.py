#!/usr/bin/env python3
"""每日导出 Kwai Ads 账户维度花费，按日期合并追加到 CSV。

用法:
    python3 export_spend.py              # 正常导出
    python3 export_spend.py --discover   # 探测模式：把报表页的所有 JSON 接口 dump 下来
    python3 export_spend.py --show       # 关掉 headless，肉眼看它在点什么

退出码: 0 成功 / 2 登录态失效（需重跑 login.py）/ 3 没抓到数据
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from kwai_common import (
    TABLE_JS,
    get_tz,
    launch_kwargs,
    extract_daily_rows,
    load_config,
    log,
    looks_like_login_page,
    merge_csv,
    now_stamp,
    rows_from_tables,
    today_in,
)

EXIT_AUTH = 2
EXIT_NO_DATA = 3


def date_window(cfg: dict, args, tz) -> tuple[dt.date, dt.date]:
    """要抓取的日期区间。默认 [今天-days_back, 今天]，必定覆盖前一天。

    「今天」按报表时区算，不是按机器本地时区 —— 跨时区的服务器上这两者可能差一天。
    """
    end = dt.date.fromisoformat(args.end) if args.end else today_in(tz)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=cfg.get("days_back", 7))
    if start > end:
        sys.exit(f"起始日期 {start} 晚于结束日期 {end}")
    return start, end


def build_url(cfg: dict, start: dt.date, end: dt.date) -> str:
    """若配置了 report_url_template，就把日期填进去；否则用 report_url 原样打开。

    模板里可用的占位符: {start} {end} (YYYY-MM-DD) 和 {start_compact} {end_compact}
    (YYYYMMDD)。具体该写成什么样，跑一次 --discover 看后台报表页的真实 URL 就知道。
    """
    template = cfg.get("report_url_template")
    if not template:
        return cfg["report_url"]
    return template.format(
        start=start.isoformat(),
        end=end.isoformat(),
        start_compact=start.strftime("%Y%m%d"),
        end_compact=end.strftime("%Y%m%d"),
    )


def collect(cfg: dict, headless: bool, discover: bool, target_url: str):
    """打开报表页，收集所有 JSON 响应 + 页面表格。"""
    auth_file = Path(cfg.get("auth_file", "auth.json"))
    if not auth_file.exists():
        sys.exit(f"找不到 {auth_file}，请先执行: python3 login.py")

    hints = [h.lower() for h in cfg.get("response_url_hints", [])]
    captured: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, **launch_kwargs())
        context = browser.new_context(storage_state=str(auth_file), locale="zh-CN")
        page = context.new_page()

        def on_response(response):
            url = response.url
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            # 探测模式收全部 JSON；正常模式只看像报表接口的
            if not discover and hints and not any(h in url.lower() for h in hints):
                return
            try:
                body = response.json()
            except Exception:
                return
            captured.append({"url": url, "status": response.status, "body": body})

        page.on("response", on_response)

        url = target_url
        log(f"打开报表页: {url}")
        page.goto(url, timeout=cfg.get("nav_timeout_ms", 60000))
        try:
            page.wait_for_load_state("networkidle", timeout=cfg.get("nav_timeout_ms", 60000))
        except PWError:
            log("networkidle 等待超时，继续（单页应用常有长连接，属正常）")
        # 单页应用的报表接口往往在首屏之后才发，多等一会
        page.wait_for_timeout(cfg.get("settle_ms", 8000))

        final_url = page.url
        html = page.content()
        try:
            tables = page.evaluate(TABLE_JS)
        except PWError:
            tables = []
        if discover:
            page.screenshot(path="discover_page.png", full_page=True)

        browser.close()

    return captured, tables, final_url, html


def run_discover(cfg: dict, headless: bool, url: str, tz) -> int:
    captured, tables, final_url, html = collect(cfg, headless, True, url)
    if looks_like_login_page(final_url, html):
        log("看起来登录态已失效，请重新执行: python3 login.py")
        return EXIT_AUTH

    out = Path("discover_dump.json")
    summary = []
    for item in captured:
        hit = extract_daily_rows(item["body"], tz)
        summary.append(
            {
                "url": item["url"],
                "status": item["status"],
                "matched": bool(hit),
                "matched_path": hit["path"] if hit else None,
                "date_key": hit["date_key"] if hit else None,
                "cost_key": hit["cost_key"] if hit else None,
                "row_count": len(hit["rows"]) if hit else 0,
                "sample_rows": hit["rows"][:3] if hit else None,
            }
        )
    out.write_text(
        json.dumps(
            {
                "final_url": final_url,
                "captured_count": len(captured),
                "candidates": summary,
                "page_tables": tables,
                "raw_responses": captured,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"共抓到 {len(captured)} 个 JSON 响应，{sum(s['matched'] for s in summary)} 个像日消耗数据")
    for s in summary:
        if s["matched"]:
            log(f"  命中 {s['url'][:90]}  {s['date_key']}/{s['cost_key']}  {s['row_count']} 行")
    log(f"明细已写入 {out.resolve()}，截图 discover_page.png")
    log("注意：dump 里含接口原始返回，可能带账户信息，发我之前自己先过一眼。")
    return 0


def run_export(cfg: dict, headless: bool, url: str, start: dt.date, end: dt.date, tz) -> int:
    captured, tables, final_url, html = collect(cfg, headless, False, url)

    if looks_like_login_page(final_url, html):
        log("登录态已失效（页面跳回登录）。请重新执行: python3 login.py")
        return EXIT_AUTH

    best, source = None, ""
    for item in captured:
        hit = extract_daily_rows(item["body"], tz)
        if hit and (best is None or len(hit["rows"]) > len(best["rows"])):
            best, source = hit, f"xhr:{item['url'].split('?')[0]}"

    if best:
        rows = best["rows"]
        log(f"从接口取到 {len(rows)} 行（字段 {best['date_key']} / {best['cost_key']}）")
    else:
        log("接口里没识别出日消耗，改用页面表格兜底……")
        rows = rows_from_tables(tables, tz) or []
        source = "dom-table"
        if rows:
            log(f"从页面表格取到 {len(rows)} 行")

    if not rows:
        log("没抓到任何日消耗数据。建议跑 `python3 export_spend.py --discover --show` 看看页面到底返回了什么。")
        return EXIT_NO_DATA

    divisor = cfg.get("cost_divisor", 1) or 1
    fetched_at = now_stamp(tz)

    out_rows = []
    for row in rows:
        if not (start <= dt.date.fromisoformat(row["date"]) <= end):
            continue
        out_rows.append(
            {
                "date": row["date"],
                "account_id": str(cfg.get("account_id", "")),
                "spend": f"{row['cost_raw'] / divisor:.2f}",
                "currency": cfg.get("currency", ""),
                "fetched_at": fetched_at,
                "source": source,
            }
        )

    if not out_rows:
        got = sorted({r["date"] for r in rows})
        log(f"抓到的数据都不在 {start} ~ {end} 区间内（实际拿到: {got[:5]}）。")
        log("多半是报表页的默认日期范围不含目标日期 —— 见 README「日期区间」一节配置 report_url_template。")
        return EXIT_NO_DATA

    csv_path = cfg.get("output_csv", "kwai_daily_spend.csv")
    added, updated = merge_csv(csv_path, out_rows)
    log(f"写入 {Path(csv_path).resolve()}：新增 {added} 天，更新 {updated} 天")
    for row in sorted(out_rows, key=lambda r: r["date"])[-5:]:
        log(f"  {row['date']}  {row['spend']} {row['currency']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 Kwai Ads 账户每日花费")
    parser.add_argument("--discover", action="store_true", help="探测模式：dump 所有 JSON 接口")
    parser.add_argument("--show", action="store_true", help="显示浏览器窗口（默认无头）")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD（默认 结束日期 - days_back）")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    headless = cfg.get("headless", True) and not args.show
    tz = get_tz(cfg)
    start, end = date_window(cfg, args, tz)
    url = build_url(cfg, start, end)
    if args.discover:
        return run_discover(cfg, headless, url, tz)
    log(f"目标区间: {start} ~ {end}（时区: {cfg.get('report_timezone') or '机器本地时区'}）")
    return run_export(cfg, headless, url, start, end, tz)


if __name__ == "__main__":
    sys.exit(main())
