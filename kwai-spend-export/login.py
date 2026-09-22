#!/usr/bin/env python3
"""一次性登录：打开有界面的浏览器，你手动登录（账号密码 / 验证码 / 二次验证），
登录成功后把 cookie 和 localStorage 存到 auth.json，后续每日导出复用它。

用法:  python3 login.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from kwai_common import launch_kwargs, load_config, log

LOGIN_URL = "https://ads.kwai.com/"


def main() -> int:
    cfg = load_config()
    auth_file = Path(cfg.get("auth_file", "auth.json"))

    with sync_playwright() as pw:
        # 登录必须有界面：验证码、滑块、短信码都要你自己点
        browser = pw.chromium.launch(headless=False, **launch_kwargs())
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()

        log(f"正在打开 {LOGIN_URL} ，请在弹出的浏览器窗口里完成登录……")
        page.goto(LOGIN_URL, timeout=cfg.get("nav_timeout_ms", 60000))

        print()
        print("=" * 62)
        print("  请在浏览器窗口中登录 Kwai Ads，并进入你要导出的账户后台。")
        print("  登录完成后，回到这个终端窗口，按 回车 保存登录态。")
        print("=" * 62)
        print()
        try:
            input("登录好了就按回车 > ")
        except (EOFError, KeyboardInterrupt):
            log("已取消，未保存。")
            browser.close()
            return 1

        current = page.url
        context.storage_state(path=str(auth_file))
        browser.close()

    # auth.json 里是等同于你登录态的凭证，权限收紧
    try:
        auth_file.chmod(0o600)
    except OSError:
        pass

    log(f"登录态已保存到 {auth_file.resolve()}（当前页面: {current}）")
    log("注意：这个文件等同于你的登录凭证，不要提交到 git、不要外发。")
    log("接下来可以跑: python3 export_spend.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
