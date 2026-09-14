#!/bin/sh
# launchd polls while logged in; all trading times remain Asia/Shanghai.
set -eu
APP="$1"
STAMP="$(TZ=Asia/Shanghai date +%u-%H%M)"
case "$STAMP" in
  [1-5]-0910) SLOT=0920 ;;
  [1-5]-1020) SLOT=1030 ;;
  [1-5]-1445) SLOT=1455 ;;
  [1-5]-1530) SLOT=review ;;
  5-1545) SLOT=weekly ;;
  *) exit 0 ;;
esac
[ "$SLOT" = "$2" ] || exit 0
STATE="$HOME/Library/Application Support/Stock King/schedule"
mkdir -p "$STATE"
# Atomic claim prevents duplicate runs after an agent restart in the same minute.
mkdir "$STATE/$(TZ=Asia/Shanghai date +%F)-$SLOT" 2>/dev/null || exit 0
exec "$APP/Contents/MacOS/Stock King" "--stock-king-task=$SLOT"
