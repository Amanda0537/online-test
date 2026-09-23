#!/usr/bin/env python3
"""飞书写入逻辑的自测：起一个模拟飞书服务端，验证幂等 upsert。

不需要真实的飞书应用。用法: python3 test_feishu.py
"""
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8799
os.environ["FEISHU_BASE"] = f"http://127.0.0.1:{PORT}"

SHEET: list[list] = []          # 模拟表格内容
CALLS: list[str] = []           # 记录调用了哪些接口
TOKEN_HITS = [0]                # 记录换 token 的次数，用来验证缓存


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        path = urllib.parse.urlparse(self.path).path
        CALLS.append(f"POST {path}")

        if path.endswith("/tenant_access_token/internal"):
            TOKEN_HITS[0] += 1
            if payload.get("app_secret") != "secret":
                return self._send({"code": 10003, "msg": "invalid app_secret"})
            return self._send({"code": 0, "tenant_access_token": "t-abc123", "expire": 7200})

        if not self.headers.get("Authorization", "").startswith("Bearer t-"):
            return self._send({"code": 99991663, "msg": "missing token"})

        if path.endswith("/values_append"):
            SHEET.extend(payload["valueRange"]["values"])
            return self._send({"code": 0, "data": {}})

        if path.endswith("/values_batch_update"):
            for vr in payload["valueRanges"]:
                row = int(re.search(r"!A(\d+):", vr["range"]).group(1))
                while len(SHEET) < row:
                    SHEET.append([])
                SHEET[row - 1] = vr["values"][0]
            return self._send({"code": 0, "data": {}})

        self._send({"code": 404, "msg": "unknown"})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        CALLS.append(f"GET {path}")
        if "/values/" in path:
            values = [[r[0]] if r else [] for r in SHEET]
            return self._send({"code": 0, "data": {"valueRange": {"values": values}}})
        self._send({"code": 404, "msg": "unknown"})


server = HTTPServer(("127.0.0.1", PORT), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()

from feishu_sheet import FeishuError, FeishuSheet  # noqa: E402  (必须在设置 FEISHU_BASE 之后)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n    got : {got!r}\n    want: {want!r}")


HEADER = ["日期", "账户ID", "花费", "币种", "更新时间", "来源"]


def row(date, spend, at="t1"):
    return [date, "75566086", spend, "USD", at, "xhr"]


sheet = FeishuSheet("app", "secret", "shttoken", "Sheet1")

# ---- 空表：写表头 + 全部数据 ----
added, updated = sheet.upsert_by_date(HEADER, [row("2026-09-20", "980.00"), row("2026-09-21", "1530.25")])
check("空表 新增", (added, updated), (2, 0))
check("空表 表头", SHEET[0], HEADER)
check("空表 行数", len(SHEET), 3)

# ---- 关键：同样的数据再推一次，不能重复 ----
added, updated = sheet.upsert_by_date(HEADER, [row("2026-09-20", "980.00"), row("2026-09-21", "1530.25")])
check("重推 不新增", added, 0)
check("重推 全覆盖", updated, 2)
check("重推 行数不变", len(SHEET), 3)

# ---- 回补：老日期数值被校准 + 新日期追加 ----
added, updated = sheet.upsert_by_date(HEADER, [
    row("2026-09-20", "985.50", "t2"),   # 数值变了
    row("2026-09-21", "1530.25", "t2"),
    row("2026-09-22", "1200.50", "t2"),  # 新的一天
])
check("回补 新增1", added, 1)
check("回补 覆盖2", updated, 2)
check("回补 行数", len(SHEET), 4)
check("回补 老值被改", SHEET[1][2], "985.50")
check("回补 日期没错位", [r[0] for r in SHEET], ["日期", "2026-09-20", "2026-09-21", "2026-09-22"])

# ---- token 应被缓存，不该每次调用都换 ----
check("token 只取一次", TOKEN_HITS[0], 1)

# ---- 凭证错误要给出明确报错，而不是静默失败 ----
bad = FeishuSheet("app", "wrong-secret", "shttoken", "Sheet1")
try:
    bad.access_token
    failures.append("错误凭证 应抛异常但没抛")
except FeishuError as exc:
    check("错误凭证 提示清晰", "tenant_access_token" in str(exc) and "10003" in str(exc), True)

# ---- 配置不全要提前拦住 ----
try:
    FeishuSheet("", "s", "t", "sid")
    failures.append("空配置 应抛异常但没抛")
except FeishuError:
    pass

# ---- 空数据不该发任何请求 ----
before = len(CALLS)
check("空数据 不写", sheet.upsert_by_date(HEADER, []), (0, 0))
check("空数据 零请求", len(CALLS), before)

server.shutdown()

if failures:
    print(f"\n✗ {len(failures)} 项失败:\n")
    for f in failures:
        print("  " + f)
    raise SystemExit(1)
print("✓ 全部通过")
