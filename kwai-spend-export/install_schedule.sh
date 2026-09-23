#!/usr/bin/env bash
# 安装 macOS 定时任务：每天 5:00 跑一次。
#
# 用 launchd 而不是 cron：笔记本 5AM 多半在休眠，cron 会直接漏跑，
# launchd 会在机器唤醒后补跑一次。

set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.kwai.spend.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

HOUR="${HOUR:-5}"
MINUTE="${MINUTE:-0}"

mkdir -p "$HOME/Library/LaunchAgents" logs

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/run_daily.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
</dict></plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "已安装定时任务 $LABEL，每天 $(printf '%02d:%02d' "$HOUR" "$MINUTE") 运行"
echo "  立即试跑:  launchctl start $LABEL"
echo "  查看状态:  launchctl list | grep ${LABEL}"
echo "  卸载:      launchctl unload $PLIST && rm $PLIST"
echo
echo "提醒：这台机器 $(printf '%02d:%02d' "$HOUR" "$MINUTE") 必须是开机状态（休眠可以，关机不行）。"
