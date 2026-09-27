# Local building and distribution

[简体中文](LOCAL_BUILD.md) | English · [Back to README](../README.en.md)

Build Stock King on your own computer without GitHub Actions, cloud servers or paid build platforms. Supported targets are Windows x64 and Apple Silicon (macOS 15+). Downloading open-source dependencies still requires a network; local execution uses your storage, memory and compute.

## 1. Installation for users

Prefer a maintainer-supplied native wheel. It contains the desktop and frozen research engine, so users do not need Go, Node.js or Wails. Install 64-bit Python 3.12, then run from the package directory:

```text
python -m pip install ./stock_king-2.6.0-py3-none-win_amd64.whl
stock-king
```

On Apple Silicon, use `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl` with native ARM64 Python. Alternatively, extract the `.app` archive and move it to Applications. Windows requires Microsoft WebView2 Runtime. Use the actual filename when the package version changes.

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

Install native x64 Python 3.12, Node.js 20+ and Go (see `desktop/go.mod` for the version). Use an existing source directory, or clone:

```powershell
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
powershell -ExecutionPolicy Bypass -File scripts/build-windows-wheel.ps1
```

The script creates or uses the project `.venv`, installs dependencies and Wails 2.11, runs focused Python/frontend/Go tests, freezes the engine, builds the desktop, then packages and verifies the native wheel. No NSIS or purchased code-signing certificate is required. Use `-Python C:\path\to\python.exe` to select Python; an existing `.venv` must also be x64.

Output is `dist/desktop/*win_amd64.whl`, with SHA-256 printed in the terminal. Resolve errors before rerunning; do not skip a failed step and label the package validated. Unsigned applications may display operating-system reputation prompts.

## 4. Apple Silicon packaging

Use an Apple Silicon Mac, macOS 15+ and native ARM64 tools, without Rosetta:

```bash
xcode-select --install
brew install python@3.12 node go libomp
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
export PATH="$PATH:$(go env GOPATH)/bin"
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
bash scripts/build-macos.sh
```

The script runs the corresponding tests, builds the desktop and engine, applies an ad-hoc signature, and verifies native-wheel installation. Outputs are `artifacts/macos/Stock-King-macos-arm64.zip` and `dist/desktop/*macosx_15_0_arm64.whl`. No paid Apple developer account is required. This approach does not provide Apple notarization; first launch may require approval in System Settings → Privacy & Security. Users should verify their download source themselves.

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
