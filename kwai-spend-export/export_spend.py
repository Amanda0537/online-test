#!/usr/bin/env python3
"""每日导出 Kwai Ads 账户维度花费，按日期合并追加到 CSV。

用法:
    python3 export_spend.py                       # 抓最近 days_back 天
    python3 export_spend.py --start X --end Y     # 抓指定区间
    python3 export_spend.py --discover --show     # 探测模式，排查用

退出码: 0 成功 / 2 登录态失效 / 3 没抓到目标区间的数据
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
    extract_daily_rows,
    get_tz,
    launch_kwargs,
    load_config,
    log,
    looks_like_login_page,
    merge_csv,
    now_stamp,
    rows_from_tables,
    today_in,
)
from request_replay import clean_headers, rewrite_body, rewrite_query

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
    """报表页地址。

    自定义报表页 (#/report/customReport) 的日期是页面内部状态、不在 URL 上，
    所以默认不拼日期，改由「重放」机制来指定区间（见 replay_for_range）。
    若你的后台确实支持 URL 带日期，可在 config 里配 report_url_template。
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


def covers(rows: list[dict], start: dt.date, end: dt.date) -> bool:
    """页面这次返回的数据里，是否已经包含目标区间的日子。"""
    return any(start <= dt.date.fromisoformat(r["date"]) <= end for r in rows)


def replay_for_range(context, candidate: dict, start: dt.date, end: dt.date, tz):
    """把报表请求里的日期参数换成目标区间，用同一登录态重发一次。

    这是拿到「指定日期」数据的关键：自定义报表页不把日期放 URL 上，
    但它发出的接口请求里一定带着日期参数，改掉重发即可，不用去点日期控件。
    """
    req = candidate["request"]
    new_url, n_url = rewrite_query(req["url"], start, end)
    new_body, n_body = rewrite_body(
        req["post_data"], req["headers"].get("content-type", ""), start, end
    )
    if not (n_url or n_body):
        log("报表请求里没找到可改写的日期参数，跳过重放")
        return None

    log(f"重放报表请求（改写了 {n_url + n_body} 个日期参数）→ {start} ~ {end}")
    try:
        resp = context.request.fetch(
            new_url,
            method=req["method"],
            headers=clean_headers(req["headers"]),
            data=new_body,
            timeout=60000,
        )
        if not resp.ok:
            log(f"重放请求返回 HTTP {resp.status}，放弃重放")
            return None
        hit = extract_daily_rows(resp.json(), tz)
    except Exception as exc:
        log(f"重放失败（{type(exc).__name__}: {exc}），退回页面首次返回的数据")
        return None

    if not hit:
        log("重放的返回里没识别出日消耗，退回页面首次返回的数据")
        return None
    log(f"重放取到 {len(hit['rows'])} 行")
    return hit


def collect(cfg: dict, headless: bool, discover: bool, url: str, start, end, tz):
    """打开报表页，收集 JSON 响应；必要时重放请求以覆盖目标日期区间。"""
    auth_file = Path(cfg.get("auth_file", "auth.json"))
    if not auth_file.exists():
        sys.exit(f"找不到 {auth_file}，请先执行: python3 login.py")

    hints = [h.lower() for h in cfg.get("response_url_hints", [])]
    captured: list[dict] = []
    result = {"rows": None, "source": "", "tables": [], "final_url": "", "html": "",
              "captured": captured}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, **launch_kwargs())
        context = browser.new_context(storage_state=str(auth_file), locale="zh-CN")
        page = context.new_page()

        def on_response(response):
            if "json" not in (response.headers or {}).get("content-type", "").lower():
                return
            if not discover and hints and not any(h in response.url.lower() for h in hints):
                return
            try:
                body = response.json()
            except Exception:
                return
            req = response.request
            try:
                post_data = req.post_data
            except Exception:
                post_data = None
            captured.append({
                "url": response.url,
                "status": response.status,
                "body": body,
                "request": {
                    "url": req.url,
                    "method": req.method,
                    "headers": dict(req.headers or {}),
                    "post_data": post_data,
                },
            })

        page.on("response", on_response)

        log(f"打开报表页: {url}")
        page.goto(url, timeout=cfg.get("nav_timeout_ms", 60000))
        try:
            page.wait_for_load_state("networkidle", timeout=cfg.get("nav_timeout_ms", 60000))
        except PWError:
            log("networkidle 等待超时，继续（单页应用常有长连接，属正常）")
        page.wait_for_timeout(cfg.get("settle_ms", 8000))

        result["final_url"] = page.url
        result["html"] = page.content()
        try:
            result["tables"] = page.evaluate(TABLE_JS)
        except PWError:
            result["tables"] = []
        if discover:
            page.screenshot(path="discover_page.png", full_page=True)
            browser.close()
            return result

        # 找出返回了日消耗的那个接口
        best = None
        for item in captured:
            hit = extract_daily_rows(item["body"], tz)
            if hit and (best is None or len(hit["rows"]) > len(best["hit"]["rows"])):
                best = {"hit": hit, "item": item}

        if best:
            rows = best["hit"]["rows"]
            keys = f"{best['hit']['date_key']} / {best['hit']['cost_key']}"
            log(f"页面首次返回 {len(rows)} 行（字段 {keys}）")
            # 页面默认区间多半不含目标日期（尤其凌晨跑），这时改写日期重放一次
            if not covers(rows, start, end):
                log(f"首次返回不含目标区间 {start} ~ {end}，尝试重放请求……")
                replayed = replay_for_range(context, best["item"], start, end, tz)
                if replayed:
                    best["hit"] = replayed
            result["rows"] = best["hit"]["rows"]
            result["source"] = f"xhr:{best['item']['url'].split('?')[0]}"

        browser.close()
    return result


def run_discover(cfg: dict, headless: bool, url: str, tz) -> int:
    res = collect(cfg, headless, True, url, None, None, tz)
    if looks_like_login_page(res["final_url"], res["html"]):
        log("看起来登录态已失效，请重新执行: python3 login.py")
        return EXIT_AUTH

    summary = []
    for item in res["captured"]:
        hit = extract_daily_rows(item["body"], tz)
        summary.append({
            "url": item["url"],
            "method": item["request"]["method"],
            "status": item["status"],
            "has_post_body": bool(item["request"]["post_data"]),
            "matched": bool(hit),
            "matched_path": hit["path"] if hit else None,
            "date_key": hit["date_key"] if hit else None,
            "cost_key": hit["cost_key"] if hit else None,
            "row_count": len(hit["rows"]) if hit else 0,
            "sample_rows": hit["rows"][:3] if hit else None,
        })

    Path("discover_dump.json").write_text(
        json.dumps({
            "final_url": res["final_url"],
            "captured_count": len(res["captured"]),
            "candidates": summary,
            "page_tables": res["tables"],
            "raw_responses": res["captured"],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    matched = sum(s["matched"] for s in summary)
    log(f"共抓到 {len(res['captured'])} 个 JSON 响应，{matched} 个像日消耗数据")
    for s in summary:
        if s["matched"]:
            log(f"  命中 {s['method']} {s['url'][:80]}  {s['date_key']}/{s['cost_key']}  {s['row_count']} 行")
    log("明细已写入 discover_dump.json，截图 discover_page.png")
    log("注意：dump 里含接口原始返回和请求头（可能含 cookie），发我之前自己先过一眼。")
    return 0


def run_export(cfg: dict, headless: bool, url: str, start: dt.date, end: dt.date, tz) -> int:
    res = collect(cfg, headless, False, url, start, end, tz)

    if looks_like_login_page(res["final_url"], res["html"]):
        log("登录态已失效（页面跳回登录）。请重新执行: python3 login.py")
        return EXIT_AUTH

    rows, source = res["rows"], res["source"]
    if not rows:
        log("接口里没识别出日消耗，改用页面表格兜底……")
        rows = rows_from_tables(res["tables"], tz) or []
        source = "dom-table"
        if rows:
            log(f"从页面表格取到 {len(rows)} 行")

    if not rows:
        log("没抓到任何日消耗数据。建议跑 `python3 export_spend.py --discover --show` 排查。")
        return EXIT_NO_DATA

    divisor = cfg.get("cost_divisor", 1) or 1
    fetched_at = now_stamp(tz)
    out_rows = [
        {
            "date": r["date"],
            "account_id": str(cfg.get("account_id", "")),
            "spend": f"{r['cost_raw'] / divisor:.2f}",
            "currency": cfg.get("currency", ""),
            "fetched_at": fetched_at,
            "source": source,
        }
        for r in rows
        if start <= dt.date.fromisoformat(r["date"]) <= end
    ]

    if not out_rows:
        got = sorted({r["date"] for r in rows})
        log(f"抓到的数据都不在 {start} ~ {end} 区间内（实际拿到: {got[:5]}）。")
        log("说明重放没能改到日期参数，跑 --discover 看看报表请求长什么样。")
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
