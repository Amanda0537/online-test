#!/usr/bin/env bash
# 每日定时入口（macOS / Linux）。由 cron 或 launchd 调用。
#
#   ./run_daily.sh
#
# 行为：抓取最近 days_back 天（必定覆盖前一天）并合并进 CSV。
# 之所以不只抓前一天：广告平台的消耗数据会有回传延迟和事后校准，
# 多回补几天能让历史数字自动修正 —— CSV 按日期合并，不会产生重复行。

set -uo pipefail

cd "$(dirname "$0")" || exit 1

LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_$(date +%Y-%m).log"
PYTHON="${PYTHON:-python3}"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }

# 防止上一次还没跑完就又被拉起（比如网络卡住）
LOCK="$LOG_DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  log "上一次任务仍在运行（$LOCK 存在），本次跳过"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

notify() {
  local msg="$1"
  log "告警: $msg"
  # macOS 弹通知；非 macOS 静默跳过
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$msg\" with title \"Kwai 花费导出\"" 2>/dev/null
  fi
  # 想推到企微/钉钉/Slack，就设 WEBHOOK_URL 环境变量
  if [ -n "${WEBHOOK_URL:-}" ]; then
    curl -sS -m 10 -X POST -H 'Content-Type: application/json' \
      -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"[Kwai花费导出] $msg\"}}" \
      "$WEBHOOK_URL" >>"$LOG_FILE" 2>&1
  fi
}

log "===== 任务开始 ====="

# 网络抖动重试：间隔 60s / 180s
for attempt in 1 2 3; do
  "$PYTHON" export_spend.py >>"$LOG_FILE" 2>&1
  code=$?
  case $code in
    0)
      log "任务成功（第 $attempt 次尝试）"
      exit 0
      ;;
    2)
      # 登录态失效重试没有意义，必须人工重新登录
      notify "登录态已过期，请在这台机器上执行: cd $(pwd) && python3 login.py"
      exit 2
      ;;
    *)
      log "第 $attempt 次尝试失败（退出码 $code）"
      [ "$attempt" -lt 3 ] && sleep $((attempt * 120))
      ;;
  esac
done

notify "连续 3 次抓取失败，请查看日志 $(pwd)/$LOG_FILE"
exit 1
