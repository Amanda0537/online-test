"""飞书电子表格写入：按日期主键幂等 upsert。

只用标准库，不引第三方 HTTP 依赖 —— 定时任务的依赖越少越不容易坏。

⚠️ 接口地址集中在下面的常量里。这些是按飞书开放平台的公开接口写的，
上线前请用 `python3 push_feishu.py --check` 对着你的真实表格验一次
（会打通鉴权、读、写全链路，并把写入的测试行清掉）。
若飞书后续调整了接口版本，改这里的常量即可，其余逻辑不用动。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("FEISHU_BASE", "https://open.feishu.cn")
TOKEN_URL = f"{BASE}/open-apis/auth/v3/tenant_access_token/internal"
SHEETS_META_URL = f"{BASE}/open-apis/sheets/v3/spreadsheets/{{token}}/sheets/query"
VALUES_READ_URL = f"{BASE}/open-apis/sheets/v2/spreadsheets/{{token}}/values/{{range}}"
VALUES_APPEND_URL = f"{BASE}/open-apis/sheets/v2/spreadsheets/{{token}}/values_append"
VALUES_BATCH_UPDATE_URL = f"{BASE}/open-apis/sheets/v2/spreadsheets/{{token}}/values_batch_update"

TIMEOUT = 30
RETRY_STATUS = {429, 500, 502, 503, 504}


class FeishuError(RuntimeError):
    """飞书接口返回了非 0 的 code，或网络彻底失败。"""


def _request(method: str, url: str, *, headers: dict | None = None,
             payload: dict | None = None, retries: int = 3) -> dict:
    """发一次 JSON 请求，对限流和 5xx 做指数退避重试。"""
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            last_err = f"HTTP {exc.code}: {detail}"
            if exc.code not in RETRY_STATUS:
                raise FeishuError(f"{method} {url} -> {last_err}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise FeishuError(f"{method} {url} 重试 {retries} 次仍失败 -> {last_err}")


def _check(result: dict, what: str) -> dict:
    if result.get("code") not in (0, None):
        raise FeishuError(f"{what} 失败: code={result.get('code')} msg={result.get('msg')}")
    return result.get("data", {})


class FeishuSheet:
    """一张电子表格里的一个工作表。"""

    def __init__(self, app_id: str, app_secret: str, spreadsheet_token: str, sheet_id: str):
        if not all([app_id, app_secret, spreadsheet_token, sheet_id]):
            raise FeishuError(
                "飞书配置不完整，需要 app_id / app_secret / spreadsheet_token / sheet_id"
            )
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = spreadsheet_token
        self.sheet_id = sheet_id
        self._access_token = None
        self._expire_at = 0.0

    # ---------------------------------------------------------------- 鉴权

    @property
    def access_token(self) -> str:
        """tenant_access_token，带缓存；提前 5 分钟过期以免边界上失败。"""
        if self._access_token and time.time() < self._expire_at:
            return self._access_token
        data = _request("POST", TOKEN_URL,
                        payload={"app_id": self.app_id, "app_secret": self.app_secret})
        if data.get("code") not in (0, None):
            raise FeishuError(
                f"获取 tenant_access_token 失败: code={data.get('code')} msg={data.get('msg')}。"
                "请检查 app_id / app_secret，以及应用是否已发布。"
            )
        self._access_token = data["tenant_access_token"]
        self._expire_at = time.time() + int(data.get("expire", 7200)) - 300
        return self._access_token

    @property
    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    # ------------------------------------------------------------ 读写原语

    def read_column(self, column: str = "A", max_rows: int = 20000) -> list[str]:
        """读某一列，返回去掉尾部空值的字符串列表（含表头）。"""
        rng = urllib.parse.quote(f"{self.sheet_id}!{column}1:{column}{max_rows}", safe="")
        url = VALUES_READ_URL.format(token=self.token, range=rng)
        data = _check(_request("GET", url, headers=self._auth_headers), "读取表格")
        values = (data.get("valueRange") or {}).get("values") or []
        out = []
        for row in values:
            cell = row[0] if row else None
            out.append("" if cell is None else str(cell).strip())
        while out and not out[-1]:
            out.pop()
        return out

    def append_rows(self, rows: list[list]) -> int:
        """在表尾追加若干行。"""
        if not rows:
            return 0
        url = VALUES_APPEND_URL.format(token=self.token)
        payload = {"valueRange": {"range": f"{self.sheet_id}!A1:A1", "values": rows}}
        _check(_request("POST", url, headers=self._auth_headers, payload=payload), "追加行")
        return len(rows)

    def update_rows(self, updates: list[tuple[int, list]], last_col: str) -> int:
        """按行号覆盖若干行。updates 是 [(行号, 该行的值), ...]，行号从 1 开始。"""
        if not updates:
            return 0
        url = VALUES_BATCH_UPDATE_URL.format(token=self.token)
        payload = {"valueRanges": [
            {"range": f"{self.sheet_id}!A{row}:{last_col}{row}", "values": [values]}
            for row, values in updates
        ]}
        _check(_request("POST", url, headers=self._auth_headers, payload=payload), "覆盖行")
        return len(updates)

    # -------------------------------------------------------------- 幂等写

    def upsert_by_date(self, header: list[str], rows: list[list], log=print) -> tuple[int, int]:
        """按第一列（日期）做主键写入：已存在的日期覆盖，新日期追加。

        这是整条链路的幂等保证 —— 任务重跑、回补最近几天，都不会产生重复行。
        返回 (新增行数, 覆盖行数)。
        """
        if not rows:
            return 0, 0

        last_col = chr(ord("A") + len(header) - 1)
        existing = self.read_column("A")

        if not existing:
            # 空表：先写表头，再把数据全部追加
            log("表格为空，写入表头")
            self.append_rows([header] + rows)
            return len(rows), 0

        # 日期 -> 行号（1-based）。同一日期若有多行，以最后一行为准
        row_of = {date: idx + 1 for idx, date in enumerate(existing) if date}

        to_update, to_append = [], []
        for row in rows:
            date = str(row[0])
            if date in row_of:
                to_update.append((row_of[date], row))
            else:
                to_append.append(row)

        updated = self.update_rows(to_update, last_col)
        added = self.append_rows(to_append)
        return added, updated
