#!/usr/bin/env bash
# 每日定时入口：抓 Kwai 花费 -> 写 CSV -> 推飞书表格。
# 由 launchd（macOS）或 cron（Linux）在每天 5:00 调用。
#
# 两步分开跑是有意的：抓取和推送是两类失败。CSV 是本地事实源，
# 飞书挂了只需重跑推送，数据不会丢。

set -uo pipefail
cd "$(dirname "$0")" || exit 1

LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_$(date +%Y-%m).log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }

# 失败重试的退避基数（秒）。抓取用 2 倍，推送用 1 倍。测试时可设成 0 跳过等待。
RETRY_BASE="${RETRY_BASE:-60}"

# 凭证从 .env 读（不进 git）
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# venv 优先，没有就用系统 python3
if [ -x .venv/bin/python3 ]; then
  PYTHON=".venv/bin/python3"
else
  PYTHON="${PYTHON:-python3}"
fi

# 防止上次没跑完就又被拉起
LOCK="$LOG_DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  log "上一次任务仍在运行，本次跳过"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

notify() {
  local msg="$1"
  log "告警: $msg"
  if [ -n "${FEISHU_WEBHOOK:-}" ]; then
    curl -sS -m 10 -X POST -H 'Content-Type: application/json' \
      -d "$(printf '{"msg_type":"text","content":{"text":"[Kwai花费同步] %s"}}' "$msg")" \
      "$FEISHU_WEBHOOK" >>"$LOG_FILE" 2>&1
  fi
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$msg\" with title \"Kwai 花费同步\"" 2>/dev/null
  fi
}

log "===== 任务开始 ====="

# ---- 第一步：抓取 ----
scrape_ok=0
for attempt in 1 2 3; do
  "$PYTHON" export_spend.py >>"$LOG_FILE" 2>&1
  code=$?
  if [ $code -eq 0 ]; then
    log "抓取成功（第 $attempt 次尝试）"
    scrape_ok=1
    break
  fi
  if [ $code -eq 2 ]; then
    # 登录态过期，重试无意义，必须人工重登
    notify "登录态已过期，需人工重登：cd $(pwd) && ./relogin.sh"
    exit 2
  fi
  log "抓取第 $attempt 次失败（退出码 $code）"
  [ "$attempt" -lt 3 ] && [ "$RETRY_BASE" -gt 0 ] && sleep $((attempt * RETRY_BASE * 2))
done

if [ $scrape_ok -eq 0 ]; then
  notify "连续 3 次抓取失败，日志: $(pwd)/$LOG_FILE"
  exit 1
fi

# ---- 第二步：推飞书 ----
for attempt in 1 2 3; do
  "$PYTHON" push_feishu.py >>"$LOG_FILE" 2>&1
  code=$?
  if [ $code -eq 0 ]; then
    log "推送飞书成功（第 $attempt 次尝试）"
    log "===== 任务完成 ====="
    exit 0
  fi
  if [ $code -eq 4 ]; then
    notify "飞书配置缺失（检查 .env 里的 FEISHU_* 变量）"
    exit 4
  fi
  log "推送第 $attempt 次失败（退出码 $code）"
  [ "$attempt" -lt 3 ] && [ "$RETRY_BASE" -gt 0 ] && sleep $((attempt * RETRY_BASE))
done

# 抓取成功了，数据已在 CSV 里，只是没推上去 —— 告警但不算数据丢失
notify "数据已抓到本地 CSV，但推送飞书连续 3 次失败，日志: $(pwd)/$LOG_FILE"
exit 5
