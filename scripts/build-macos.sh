#!/bin/bash
set -euo pipefail
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=4096}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[[ "$(uname -s)" == Darwin ]] || { echo 'Build on a Mac.' >&2; exit 1; }
ARCH="$(uname -m)"
[[ "$ARCH" == arm64 ]] || { echo 'Build on Apple Silicon with native ARM64 tools.' >&2; exit 1; }
command -v go >/dev/null
command -v npm >/dev/null
command -v wails >/dev/null
"${PYTHON_BIN:-python3.12}" scripts/test_macos_schedule.py
cd desktop
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
go test .
# Keep this pure command test independent of legacy provider-test databases.
go test backend/data/mac_notification_darwin.go backend/data/mac_notification_darwin_test.go
cd "$ROOT"
cd desktop
wails build -clean -platform darwin/arm64
if [[ -d "$ROOT/desktop/build/bin/stock-king.app" && ! -d "$ROOT/desktop/build/bin/Stock King.app" ]]; then
  mv "$ROOT/desktop/build/bin/stock-king.app" "$ROOT/desktop/build/bin/Stock King.app"
fi
APP="$ROOT/desktop/build/bin/Stock King.app"
test -d "$APP"
cd "$ROOT"
"${PYTHON_BIN:-python3.12}" -m venv .venv
.venv/bin/python -m pip install '.[engine,models]' pyinstaller build
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest daily-engine/tests/test_precision_policy.py daily-engine/tests/test_local_quote_integration.py daily-engine/tests/test_local_observation_review.py daily-engine/tests/test_economy_review.py daily-engine/tests/test_public_market_quotes.py daily-engine/tests/test_recommendation_evidence.py daily-engine/tests/test_complete_market_evidence.py daily-engine/tests/test_intraday_evidence.py -q
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
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/artifacts/macos/Stock-King-macos-$ARCH.zip"
.venv/bin/python scripts/build-desktop-wheel.py
.venv/bin/python scripts/smoke-wheel.py --native
echo "Built: $ROOT/artifacts/macos/Stock-King-macos-$ARCH.zip and platform wheel"
