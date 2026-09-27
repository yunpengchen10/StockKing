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
# Wails 2.11 binding generation fails with Go 1.27. Keep the toolchain
# reproducible even when Homebrew has installed a newer Go release.
export GOTOOLCHAIN=go1.26.8
if ! GO_VERSION="$(go version)"; then
  echo 'Could not load Go 1.26.8. Check network access or export HTTPS_PROXY/HTTP_PROXY before running this script.' >&2
  exit 1
fi
[[ "$GO_VERSION" == *"go1.26.8 darwin/arm64" ]] || {
  echo "Go 1.26.8 ARM64 is required (got: $GO_VERSION). Check Go toolchain downloads and HTTPS_PROXY." >&2
  exit 1
}
if [[ -z "${PYTHON_BIN:-}" ]]; then
  for candidate in python3.12 python3.11; do
    if command -v "$candidate" >/dev/null &&
       "$candidate" -c 'import platform, pyexpat, sys; assert platform.machine() == "arm64" and sys.version_info[:2] in ((3, 11), (3, 12))' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
[[ -n "${PYTHON_BIN:-}" ]] || { echo 'Install native ARM64 Python 3.11 or 3.12, or set PYTHON_BIN.' >&2; exit 1; }
"$PYTHON_BIN" -c 'import platform, pyexpat, sys; assert platform.machine() == "arm64" and sys.version_info[:2] in ((3, 11), (3, 12)), "Use native ARM64 Python 3.11 or 3.12 with working pyexpat"'
PYTHON_TAG="$("$PYTHON_BIN" -c 'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}-{platform.machine()}")')"
VENV_TAG=""
if [[ -x .venv/bin/python ]]; then
  VENV_TAG="$(.venv/bin/python -c 'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}-{platform.machine()}")' 2>/dev/null || true)"
fi
if [[ "$VENV_TAG" != "$PYTHON_TAG" ]]; then
  echo "Creating ARM64 Python $PYTHON_TAG build environment"
  if ! "$PYTHON_BIN" -m venv --clear .venv; then
    echo 'Could not create .venv. Install native ARM64 Python 3.11 or 3.12 with venv/pip from python.org, then rerun (or set PYTHON_BIN).' >&2
    exit 1
  fi
fi
GO_BIN="$(go env GOBIN)"
if [[ -z "$GO_BIN" ]]; then
  GO_PATH="$(go env GOPATH)"
  GO_BIN="${GO_PATH%%:*}/bin"
fi
WAILS_BIN="$GO_BIN/wails"
if [[ ! -x "$WAILS_BIN" ]] && command -v wails >/dev/null; then
  WAILS_BIN="$(command -v wails)"
fi
WAILS_VERSION=""
if [[ -x "$WAILS_BIN" ]]; then
  WAILS_VERSION="$("$WAILS_BIN" version 2>/dev/null || true)"
fi
if [[ "$WAILS_VERSION" != *v2.11.0* ]]; then
  echo 'Installing Wails v2.11.0'
  if ! go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0; then
    echo 'Could not install Wails. Check network access or export HTTPS_PROXY/HTTP_PROXY before running this script.' >&2
    exit 1
  fi
  WAILS_BIN="$GO_BIN/wails"
fi
[[ -x "$WAILS_BIN" ]] || { echo "Wails install did not create $WAILS_BIN" >&2; exit 1; }
WAILS_VERSION="$("$WAILS_BIN" version)"
[[ "$WAILS_VERSION" == *v2.11.0* ]] || { echo "Wails v2.11.0 is required (got: $WAILS_VERSION)." >&2; exit 1; }
"$PYTHON_BIN" scripts/test_macos_schedule.py
cd desktop
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
go test .
# Keep this pure command test independent of legacy provider-test databases.
go test backend/data/mac_notification_darwin.go backend/data/mac_notification_darwin_test.go
cd "$ROOT"
cd desktop
"$WAILS_BIN" build -clean -platform darwin/arm64
if [[ -d "$ROOT/desktop/build/bin/stock-king.app" && ! -d "$ROOT/desktop/build/bin/Stock King.app" ]]; then
  mv "$ROOT/desktop/build/bin/stock-king.app" "$ROOT/desktop/build/bin/Stock King.app"
fi
BUILT_APP="$ROOT/desktop/build/bin/Stock King.app"
test -d "$BUILT_APP"
cd "$ROOT"
.venv/bin/python -m pip install '.[engine,models]' pyinstaller build
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest daily-engine/tests/test_precision_policy.py daily-engine/tests/test_local_quote_integration.py daily-engine/tests/test_local_observation_review.py daily-engine/tests/test_economy_review.py daily-engine/tests/test_public_market_quotes.py daily-engine/tests/test_recommendation_evidence.py daily-engine/tests/test_complete_market_evidence.py daily-engine/tests/test_intraday_evidence.py daily-engine/tests/test_efinance_frozen_cache.py -q
.venv/bin/python scripts/build-macos-engine.py
mkdir -p "$BUILT_APP/Contents/Resources/daily-engine"
ditto "$ROOT/daily-engine/dist/backend/stock_analysis" "$BUILT_APP/Contents/Resources/daily-engine/stock_analysis"
cp "$ROOT/scripts/macos-schedule.sh" "$BUILT_APP/Contents/Resources/macos-schedule.sh"
ditto "$ROOT/licenses" "$BUILT_APP/Contents/Resources/licenses"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$BUILT_APP/Contents/Resources/"
# Cloud-backed Documents folders immediately restore FinderInfo/fileprovider
# attributes. Sign a metadata-free copy on the local temporary volume.
SIGN_ROOT="$(mktemp -d /private/tmp/stock-king-sign.XXXXXX)"
cleanup_sign_root() {
  if [[ -d "$SIGN_ROOT" && "$SIGN_ROOT" == /private/tmp/stock-king-sign.* ]]; then
    rm -r -- "$SIGN_ROOT"
  fi
}
trap cleanup_sign_root EXIT
APP="$SIGN_ROOT/Stock King.app"
ditto --norsrc --noextattr "$BUILT_APP" "$APP"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
SMOKE="$SIGN_ROOT/smoke"
mkdir -p "$SMOKE"
set +e
APPDATA="$SMOKE/config" LOCALAPPDATA="$SMOKE/cache" "$APP/Contents/MacOS/Stock King" --stock-king-task=invalid
STATUS=$?
set -e
[[ "$STATUS" == 2 ]] || { echo "Packaged desktop failed to execute: $STATUS" >&2; exit 1; }
# Import efinance from the signed bundle and ensure its cache stays outside it.
SCREENING_DATA_DIR="$SMOKE/cache" DSA_PACKAGED_IMPORT_PROBE=efinance \
  "$APP/Contents/Resources/daily-engine/stock_analysis/stock_analysis"
test -d "$SMOKE/cache/efinance"
test ! -e "$APP/Contents/Resources/daily-engine/stock_analysis/_internal/efinance/data"
codesign --verify --deep --strict "$APP"
mkdir -p "$ROOT/artifacts/macos"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/artifacts/macos/Stock-King-macos-$ARCH.zip"
STOCK_KING_MACOS_APP="$APP" .venv/bin/python scripts/build-desktop-wheel.py
.venv/bin/python scripts/smoke-wheel.py --native
echo "Built: $ROOT/artifacts/macos/Stock-King-macos-$ARCH.zip and platform wheel"
shasum -a 256 "$ROOT/artifacts/macos/Stock-King-macos-$ARCH.zip" "$ROOT"/dist/desktop/*macosx_15_0_arm64.whl
