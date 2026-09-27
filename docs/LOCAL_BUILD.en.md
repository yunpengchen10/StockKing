# Local building and distribution

[简体中文](LOCAL_BUILD.md) | English · [Back to README](../README.en.md)

Build Stock King on your own computer without GitHub Actions, cloud servers or paid build platforms. Supported targets are Windows x64 and Apple Silicon (macOS 15+). Downloading open-source dependencies still requires a network; local execution uses your storage, memory and compute.

## 1. Installation for users

Prefer a maintainer-supplied native wheel. It contains the desktop and frozen research engine, so users do not need Go, Node.js or Wails. Install 64-bit Python 3.11+ (3.12 recommended), then run from the package directory:

```text
python -m pip install ./stock_king-2.6.0-py3-none-win_amd64.whl
stock-king
```

On Apple Silicon, use `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl` with native ARM64 Python. Alternatively, extract `Stock-King-macos-arm64.zip`, move `Stock King.app` to `/Applications`, open it, and check that the interface says “已连接” (Connected). The `.app` route does not require Python on the user's Mac. Windows requires Microsoft WebView2 Runtime. Use the actual filename when the package version changes.

The repository is currently in private testing, with no PyPI release or public download page. Reading source requires repository access; an existing package can be installed on your own computer. The display name is **Stock King** and the repository is **StockKing**. The `stock-king` command and `stock_king` Python module remain compatible.

## 2. Research tools from source

Install Git and Python 3.11+ (3.12 recommended), and obtain access to the current private repository. Configure Git access through normal GitHub authentication; never embed a token in the installation command.

```text
python -m pip install "stock-king[data] @ git+https://github.com/yunpengchen10/StockKing.git@main"
stock-king doctor
stock-king evidence 600519
```

`[data]` provides AKShare evidence research; `[engine]` adds the local research API; `[engine,models]` adds LightGBM/PyTorch dependencies. Source installation does not include a compiled desktop; install a native wheel or build it separately. Historical evidence uses existing archives only, without backfilling current data into the past.

## 3. Windows x64 packaging

Install native x64 Python 3.12, Node.js 20+ and Go 1.26 (required by `desktop/go.mod`). Go 1.27.1 failed during this project's Wails 2.11 binding build on macOS, so both packaging scripts select Go 1.26.8. Use an existing source directory, or clone:

```powershell
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
powershell -ExecutionPolicy Bypass -File scripts/build-windows-wheel.ps1
```

The script creates or uses the project `.venv`, installs dependencies and Wails 2.11, runs focused Python/frontend/Go tests, freezes the engine, builds the desktop, then packages and verifies the native wheel. No NSIS or purchased code-signing certificate is required. Use `-Python C:\path\to\python.exe` to select Python; an existing `.venv` must also be x64.

Output is `dist/desktop/*win_amd64.whl`, with SHA-256 printed in the terminal. Resolve errors before rerunning; do not skip a failed step and label the package validated. Unsigned applications may display operating-system reputation prompts.

## 4. Apple Silicon packaging

Use an Apple Silicon Mac, macOS 15+ and native ARM64 tools, without Rosetta. Go must be 1.26; Go 1.27.1 failed while compiling this project's Wails 2.11 bindings. First install an official macOS universal2 Python 3.12 (or 3.11) from [python.org](https://www.python.org/downloads/macos/). If `brew` is missing, follow the [official Homebrew installer](https://brew.sh/). Check the command line tools with `xcode-select -p`; if missing, run `xcode-select --install` and wait for installation to finish. Then run the following commands from a Terminal session with access to the private repository:

```bash
xcode-select -p
brew install node go@1.26 libomp
export PATH="$(brew --prefix go@1.26)/bin:$PATH"
go version  # must report go1.26.x
export PYTHON_BIN=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
"$PYTHON_BIN" -c 'import platform, pyexpat; assert platform.machine() == "arm64"'
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
bash scripts/build-macos.sh
```

The source build accepts only native ARM64 Python 3.11 or 3.12. If you installed Python 3.11, set `PYTHON_BIN` to `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11` instead. The `pyexpat` check above catches a damaged or incomplete interpreter before the long build. With `PYTHON_BIN` unset, the script tries native ARM64 `python3.12` and then `python3.11` on `PATH`.

Before building, the script checks the selected interpreter's version and ARM64 architecture, uses Go 1.26.8, and installs Wails 2.11 if needed. It recreates an existing `.venv` when its Python version or architecture does not match the selected interpreter. If dependency downloads need a proxy, export your normal `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` variables before invoking the script; they are inherited by CLI build commands. The first build downloads Go, Node and Python dependencies, runs frontend, Go and Python tests, freezes the engine, builds the desktop, and verifies the native wheel. This can take substantial time, especially on a cold machine.

The script signs the application in a temporary location outside cloud-synced directories, avoiding extended attributes that can invalidate ad-hoc signatures. Outputs are `artifacts/macos/Stock-King-macos-arm64.zip` and `dist/desktop/*macosx_15_0_arm64.whl`. No paid Apple developer account is required. This approach does not provide Apple notarization; first launch may require approval in System Settings → Privacy & Security.

To install the built application, extract the ZIP, move `Stock King.app` to `/Applications`, and open it. Confirm that the interface shows “已连接” (Connected). This confirms the local desktop has connected to its packaged engine; the build's automated smoke checks run separately.

If you want scheduled research while the desktop window is closed, install the optional background jobs from the source checkout **after** the application is in `/Applications`:

```bash
python3 scripts/install-macos-schedule.py
# Remove later: python3 scripts/install-macos-schedule.py --remove
```

The schedule installer is independent of the build and registers five user `launchd` jobs. Keep the Mac awake and the user logged in for the scheduled slots. On weekdays, preparation starts at 09:10, 10:20 and 14:45, with refreshes at 09:20, 10:30 and 14:55. Review runs at 15:30, and Friday learning at 15:45. All times are Asia/Shanghai; missed intraday slots are not backfilled.

## 5. Generic research-package checks

Maintainers can run these commands on either supported platform:

```text
python -m pip install build
python -m build
python scripts/smoke-wheel.py
```

This checks the generic research package, not the native desktop. Complete Windows and Apple Silicon builds require their respective machines; Windows results cannot establish Mac hardware validation.

## 6. Distribution to more users

Prepare locally built packages for both platforms, SHA-256 hashes, bilingual quick starts and release notes. Packages can be uploaded manually to GitHub Releases without Actions. The maintainer decides later whether to make the repository public or publish on PyPI; no automatic upload or public release is performed.

Distribute only the application, dependencies and licenses, excluding personal databases, API keys, quote archives, trained weights and notification recipients. Retain GPL-3.0 and third-party notices. The default new-user flow is free quotes → watchlist → local picks → chart verification, with optional local Ollama or report imports and no paid API requirement.

During private testing, only authorized users can access the source. Public promotion requires separately preparing publicly accessible source and release entry points.
