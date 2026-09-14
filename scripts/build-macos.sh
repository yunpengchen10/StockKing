#!/bin/bash
set -euo pipefail
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=4096}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'Build on an Apple Silicon Mac.' >&2; exit 1; }
command -v go >/dev/null
command -v npm >/dev/null
command -v wails >/dev/null
"${PYTHON_BIN:-python3.12}" scripts/test_macos_schedule.py
cd desktop
npm --prefix frontend ci
npm --prefix frontend run build
go test .
# Keep this pure command test independent of legacy provider-test databases.
go test backend/data/mac_notification_darwin.go backend/data/mac_notification_darwin_test.go
cd "$ROOT"
cd desktop
wails build -clean -platform darwin/arm64
mv "$ROOT/desktop/build/bin/stock-king.app" "$ROOT/desktop/build/bin/Stock King.app"
APP="$ROOT/desktop/build/bin/Stock King.app"
test -d "$APP"
cd "$ROOT"
"${PYTHON_BIN:-python3.12}" -m venv .venv
.venv/bin/python -m pip install -r daily-engine/requirements.txt pyinstaller
.venv/bin/python scripts/build-macos-engine.py
mkdir -p "$APP/Contents/Resources/daily-engine"
ditto "$ROOT/daily-engine/dist/backend/stock_analysis" "$APP/Contents/Resources/daily-engine/stock_analysis"
cp "$ROOT/scripts/macos-schedule.sh" "$APP/Contents/Resources/macos-schedule.sh"
ditto "$ROOT/licenses" "$APP/Contents/Resources/licenses"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
SMOKE="$(mktemp -d)"
set +e
APPDATA="$SMOKE/config" LOCALAPPDATA="$SMOKE/cache" "$APP/Contents/MacOS/Stock King" --stock-king-task=invalid
STATUS=$?
set -e
[[ "$STATUS" == 2 ]] || { echo "Packaged desktop failed to execute: $STATUS" >&2; exit 1; }
mkdir -p "$ROOT/artifacts/macos"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/artifacts/macos/Stock-King-macos-arm64.zip"
echo "Built: $ROOT/artifacts/macos/Stock-King-macos-arm64.zip"
