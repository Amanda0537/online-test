#!/usr/bin/env bash
# 登录态过期时用这一条命令续上。
set -e
cd "$(dirname "$0")"
if [ -x .venv/bin/python3 ]; then P=.venv/bin/python3; else P=python3; fi
echo "即将打开浏览器，请登录并进入你保存的那张报表，然后回终端按回车。"
"$P" login.py
echo "续期完成，顺手验证一次抓取……"
"$P" export_spend.py
