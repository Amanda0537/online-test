#!/usr/bin/env python3
"""把 CSV 里的每日花费推到飞书电子表格（按日期幂等）。

之所以拆成独立一步、而不是抓完直接写飞书：抓取和写入是两类失败。
CSV 是本地的事实源，飞书挂了重跑这一步就行，数据不会丢。

用法:
    python3 push_feishu.py                 # 推最近 days_back 天
    python3 push_feishu.py --all           # 推 CSV 里全部历史
    python3 push_feishu.py --check         # 上线前自检：打通鉴权/读/写，再清掉测试行

凭证从环境变量读，不进配置文件、不进 git:
    FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_SPREADSHEET_TOKEN / FEISHU_SHEET_ID
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

from feishu_sheet import FeishuError, FeishuSheet
from kwai_common import get_tz, load_config, log, today_in

HEADER = ["日期", "账户ID", "花费", "币种", "更新时间", "来源"]
EXIT_CONFIG = 4
EXIT_FEISHU = 5


def build_sheet() -> FeishuSheet:
    missing = [k for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET",
                           "FEISHU_SPREADSHEET_TOKEN", "FEISHU_SHEET_ID")
               if not os.environ.get(k)]
    if missing:
        log(f"缺少环境变量: {', '.join(missing)}")
        log("请参考 README「飞书配置」一节设置，或写进 .env 后 source 一下。")
        sys.exit(EXIT_CONFIG)
    return FeishuSheet(
        app_id=os.environ["FEISHU_APP_ID"],
        app_secret=os.environ["FEISHU_APP_SECRET"],
        spreadsheet_token=os.environ["FEISHU_SPREADSHEET_TOKEN"],
        sheet_id=os.environ["FEISHU_SHEET_ID"],
    )


def read_csv_rows(csv_path: Path, since: dt.date | None) -> list[list]:
    """把 CSV 读成飞书要的二维数组，按日期排序。"""
    if not csv_path.exists():
        log(f"找不到 {csv_path}，先跑 export_spend.py")
        sys.exit(EXIT_CONFIG)
    rows = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            date = (rec.get("date") or "").strip()
            if not date:
                continue
            if since and dt.date.fromisoformat(date) < since:
                continue
            rows.append([
                date,
                rec.get("account_id", ""),
                rec.get("spend", ""),
                rec.get("currency", ""),
                rec.get("fetched_at", ""),
                rec.get("source", ""),
            ])
    rows.sort(key=lambda r: r[0])
    return rows


def run_check(sheet: FeishuSheet) -> int:
    """上线前自检：鉴权 -> 读 -> 写 -> 复核 -> 清理。"""
    log("1/4 获取 tenant_access_token……")
    token = sheet.access_token
    log(f"    OK（{token[:8]}…）")

    log("2/4 读取表格第一列……")
    existing = sheet.read_column("A")
    log(f"    OK，当前 {len(existing)} 行" + (f"，表头: {existing[0]}" if existing else "（空表）"))

    probe_date = "1970-01-01"
    if probe_date in existing:
        log("    表格里已有 1970-01-01 的探针行，先跳过写入测试以免覆盖你的数据")
        return 0

    log("3/4 写入一行测试数据……")
    probe = [probe_date, "SELFCHECK", "0.00", "", dt.datetime.now().isoformat(timespec="seconds"), "selfcheck"]
    sheet.append_rows([probe])
    after = sheet.read_column("A")
    if probe_date not in after:
        log("    写入后没读到探针行，请检查应用是否有该表格的【编辑】权限")
        return EXIT_FEISHU
    log(f"    OK，写在第 {after.index(probe_date) + 1} 行")

    log("4/4 清理测试行……")
    row_no = after.index(probe_date) + 1
    blank = ["", "", "", "", "", ""]
    sheet.update_rows([(row_no, blank)], last_col="F")
    log("    OK（该行已清空）")
    log("自检通过：鉴权、读、写都正常，可以上定时任务了。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把每日花费推送到飞书电子表格")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--all", action="store_true", help="推送 CSV 里全部历史，而不只是最近几天")
    parser.add_argument("--check", action="store_true", help="上线前自检，不推业务数据")
    args = parser.parse_args()

    cfg = load_config(args.config)
    try:
        sheet = build_sheet()
        if args.check:
            return run_check(sheet)

        tz = get_tz(cfg)
        since = None if args.all else today_in(tz) - dt.timedelta(days=cfg.get("days_back", 7))
        rows = read_csv_rows(Path(cfg.get("output_csv", "kwai_daily_spend.csv")), since)
        if not rows:
            log("没有待推送的数据")
            return 0

        log(f"准备推送 {len(rows)} 行（{rows[0][0]} ~ {rows[-1][0]}）")
        added, updated = sheet.upsert_by_date(HEADER, rows, log=log)
        log(f"飞书表格：新增 {added} 行，覆盖 {updated} 行")
        return 0
    except FeishuError as exc:
        log(f"飞书接口出错: {exc}")
        return EXIT_FEISHU


if __name__ == "__main__":
    sys.exit(main())
