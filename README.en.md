# Stock King

<img src="branding/stock-king-logo.png" alt="Stock King" width="140" />

[简体中文](README.md) | English

A local desktop for stock quantitative research and China A-share research: quotes, charts, watchlists, scheduled picks, account backtests, observation review and manual AI research. Supports Windows x64 and Apple Silicon (M series, macOS 15+).

- Charts and picks share the application's Tongdaxin, Eastmoney, Sina and Tencent market services and caches, retaining source and retrieval timestamps. Scans run with the chart closed.
- Premarket observation at 09:20; independent scans at 09:40, 09:55, 10:30 and 14:55; archiving and due reviews at 15:30; Friday learning at 15:45. All use Asia/Shanghai time and trading-day checks.
- StockKing V1.1 runs locally, deeply verifies at most 30 symbols and selects at most five per round without filling a quota. Picks, review and learning never call an LLM; AI research remains a separate page.
- User data, models and settings are stored locally. No automatic trading.
- Core workflows need no paid API, cloud server or GitHub Actions. Build and validate on your own computer; AI can use local Ollama or manually imported reports.

AI tools can reuse the same quotes through the [read-only MCP quote service](quote-service/README.md). Python execution is supported on Windows and macOS; the ChatGPT cloud connection still requires remote authorization and end-to-end testing.

## Real market demonstration

![StockKing V1.1 recorded market walkthrough](docs/media/stockking-v11-real-market.gif)

**A recorded-data walkthrough, not a desktop screen recording.** The example uses recorded real market data with dates and sources. Demonstration symbols do not imply selection by the original scan, actual user trades or future returns. See the [methodology and reproduction notes](docs/research/stockking-v11-market-sample.en.md) and [source market sample](docs/research/stockking-v11-market-sample.json).

## Quick start

Windows v2.6.1 includes a one-click `Stock-King-Setup-x64-v2.6.1.exe` installer with the local engine and offline WebView2. No Python, Go or Node.js installation is needed. It fixes hidden windows after updates and restores the existing window on repeated launches. See [installation and launch-fix instructions](docs/WINDOWS_INSTALL.en.md). The wheel below is an optional installation method.

1. If you have the Mac package, extract `Stock-King-macos-arm64.zip`, copy `Stock King.app` to `/Applications`, open it, and confirm the interface says “已连接” (Connected). This does not require installing Python, Go or Node.js. Windows users may alternatively use a platform-native wheel. You can also obtain the source from this public repository and follow [local building and distribution](docs/LOCAL_BUILD.en.md); no Actions, paid build platform or cloud server is required.
2. For native wheels, use Python 3.11+ (3.12 recommended), with a native 64-bit interpreter matching your platform. From the wheel directory, run the following command, substituting the actual filename if its version differs:

   ```text
   python -m pip install ./stock_king-2.6.0-py3-none-win_amd64.whl
   stock-king
   ```

   Apple Silicon uses `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl` and requires ARM64 Python. Windows requires WebView2 Runtime. On Mac, you can also use the `.app` archive from the build artifacts.
3. Add research symbols in “自选”, then select “刷新扫描” in “精选 → 本地精选”. Check quote times, actual history coverage, factors and trigger/invalidation conditions; open “图表” to verify data from the shared market service.
4. Use “推荐记录”, “延后复盘” and “学习状态” to inspect saved signals, T+1/3/5 results and sample accumulation. Automatic recommendations and learning have separate switches. V1.1 learns from recorded main-board signals and controls without requiring a separate training universe.
5. Configure a model when you need manual AI research. Independent model research in “策略” still requires a selected universe; account backtesting requires market data and signals recorded before execution.

The project is not published on PyPI and has no public installer download page yet. `pip install stock-king` is not the current installation method for this repository. You can install the research CLI from the public repository:

```text
python -m pip install "stock-king[data] @ git+https://github.com/yunpengchen10/StockKing.git@main"
stock-king doctor
```

Source installation provides the research CLI; use the Windows installer, a platform-native wheel or the macOS `.app` archive for the complete desktop. The display name is **Stock King**, and the GitHub repository is **StockKing**. The command remains `stock-king` for compatibility with existing installations. See [local building and distribution](docs/LOCAL_BUILD.en.md).

## Pages and workflow

The interface is primarily Chinese. Chinese menu labels are retained below so that you can find the corresponding controls.

| Page | Purpose and actions |
| --- | --- |
| 市场 — Market | Review the market overview, trading session and research leads before choosing symbols to investigate. |
| 自选 — Watchlist | Search symbols and organize groups; open individual charts or AI research. Watchlist records are not actual brokerage positions. |
| 图表 — Charts | Inspect candles, technical indicators and price/volume structure; verify the current price, trend and trading plan. |
| 精选 → 本地精选 — Local picks | Run V1.1 scans and inspect Early/MainRise scores, risk, history coverage and observable price structure; read selection reasons and trigger/invalidation conditions. |
| 精选 → 推荐记录 / 延后复盘 / 学习状态 — Records / Delayed review / Learning | Filter each round's signals and controls by date, symbol and algorithm version; same-symbol daily records are grouped. Separate observed price changes from simulations and inspect mature samples, promotion gates and shadow validation. |
| 精选 → 策略分组 — Strategy groups | Inspect existing strategy groups and model status, using their own targets and training definitions. |
| AI 研究 — AI research | Select a symbol and template, retrieve evidence, then manually call a configured model. You can also export research packages, import external AI reports and compare consensus, disagreements and evidence times. |
| 策略 — Strategies | In account backtesting, import market data and signals recorded before execution, configure costs and fill constraints, and inspect equity and trade records. Train and check model eligibility within the selected universe. |
| 多图 — Multiple charts | Compare several symbols in one workspace. |
| 工具 — Tools | Access AI platform configuration, reports and deep research, fund research, research assistant, scheduled tasks, and data tools. |
| 设置 — Settings | Manage preferences, quote refresh, notifications and AI analysis settings. Save after making changes. |

Suggested sequence: **Market → Watchlist → Local picks → Chart verification → Recommendation records → Delayed review**. Open the separate “AI 研究” page for additional research. Empty candidate lists, pending verification and expired evidence are valid states. Unknown evidence must not be treated as passed, and observed price changes are not realized trading returns.

## AI configuration

**Free starting point:** Core features work without AI configuration. For local AI, use the configuration steps below with Ollama: Base URL `http://localhost:11434/v1`, local placeholder key `ollama`, and the exact installed model tag. You can also export research packages and manually import existing reports in “AI 研究”. Local models need suitable memory and compute; users choose any external service used to create imported reports.

1. Open “工具 → AI 平台配置” and click “添加AI配置”. The legacy path is “设置 → AI设置”, enable “AI诊股”, then “前往管理”; that switch does not replace model configuration.
2. Enter **configuration name, Base URL, API key and Model ID**. The endpoint must support the OpenAI-compatible Chat Completions format used by the application. Use model IDs actually available from your provider.
3. Click “测试并刷新模型列表”. This checks the model-list endpoint; it does not verify answer generation or reasoning parameters.
4. Click “确定” in the drawer, then **“保存配置”** on the list page. Wait for the save confirmation before leaving.
5. Return to the separate “AI 研究” page, select the platform, model and symbol, then manually start research. Provider charges may apply. AI recommendations, review and model selection have been removed from the picks page.

| Field | How to configure it |
| --- | --- |
| Base URL | Use the API base, retaining required prefixes such as `/v1`. Do not use a chat website or append `/chat/completions`. |
| API Key | Use a key from the corresponding API platform. A website login or chat subscription does not configure API access here. Enter it only in your local configuration page. |
| Model ID | Select from the list or enter the exact ID; a display nickname is not sufficient. |
| Temperature / MaxTokens | Set sampling and maximum output according to the model's capabilities; follow provider guidance for unsupported parameters. |
| Timeout / 深度思考 | Timeout is measured in seconds. Reasoning models may need longer. Enable deep thinking only when both the model and endpoint support it. |
| HTTP proxy | Configure for your network if needed. The model-list test uses the general HTTP client, so it does not verify the proxy assigned to an individual configuration. |

See the [AI configuration manual](docs/AI_CONFIGURATION.en.md) for examples, local Ollama, report imports, key handling and troubleshooting. Saved settings are local, but manual use of a remote AI sends selected evidence, prompts and request content to that provider. Routine scheduled scans do not call AI automatically; separately enabled bots or other AI tasks must be managed individually.

## FAQ and local validation

| Symptom | What to do |
| --- | --- |
| An old commit still shows failed checks | Historical jobs were blocked by GitHub billing restrictions. Actions is now disabled; no paid allowance is needed. Historical failures do not become passing results. |
| No installer is available, or a package has expired | Use a maintainer-supplied local build or build again using the local instructions. Installation does not depend on temporary Actions artifacts. |
| Engine not ready, missing quotes or an empty candidate list | Check network and engine status, quote freshness, trading status and actual coverage. Invalid required data can produce no candidates; missing optional factors use the available weights and display their gaps. |
| Connection test passes but AI still fails | Listing models and generating answers use different endpoints. Check model permissions, balance, exact model ID, reasoning parameters and the actual request error. |
| pip says the wheel is unsupported | Check Windows x64 / macOS ARM64, Python architecture and the macOS 15+ requirement. Intel Macs are not supported. |

This repository does not use GitHub Actions or require GitHub Pro or paid build services. Local scripts run the corresponding tests and installation checks. Validate Windows and Apple Silicon on their respective machines. See [local building and distribution](docs/LOCAL_BUILD.en.md) and the [historical build troubleshooting note](docs/CI_TROUBLESHOOTING.en.md).

## Recommendation algorithms

Picks use **StockKing V1.1**: each round rereads Shanghai/Shenzhen main-board, non-ST snapshots, deeply verifies at most 30 symbols and selects at most five. A fresh queue allows newly active stocks to enter each round, and actual scan coverage is recorded. A symbol outside the deep-verification queue has not been ruled out.

The cold-start version is `stockking-v1.1-rules`. It retains Early / MainRise channels and the original manual weights, redistributed across available factors:

```text
FinalScore = clip(max(EarlyScore, MainRiseScore) - 0.2 × DistributionRisk, 0, 100)
```

`0.2` is an unvalidated initial risk coefficient. The score is neither a probability nor a return promise; probability multiplication and fixed 65/75/85 grades are removed. Untrained MFE/MAE forecasts and main-rise/stage probabilities display “待校准” (Pending calibration). Stage descriptions use observable price structure. See [V1.1 data, scoring and learning definitions (Chinese)](docs/stockking-v11-integration.md).

| Stage | Implementation |
| --- | --- |
| Shared market data | Charts and Python picks share quotes and caches through an authenticated localhost Go interface, even with charts closed. Security identifiers, minute-end times, volume in shares and amounts in CNY are normalized; source/retrieval timestamps and adjustment definitions are retained. Unknown amounts remain null, not zero. |
| Factor calculation | Raw data produces momentum, same-time activity, turnover, VWAP, breakout, sector and risk features. The 09:40 scan uses short windows; 15-minute factors activate only when complete. MAD has zero protection, and future or incomplete minutes are excluded. |
| History coverage and gaps | Twenty same-time trading days provide a full baseline; 5–19 use actual samples with lower confidence; below five, the historical factor is null. Missing flow, catalyst or other optional factors redistribute available weights, with actual history coverage displayed. |
| Required checks | Current quotes, security identity and necessary trading status must be valid. Stale quotes indicate insufficient data; suspended or locked limit-up stocks without sell liquidity are not executable recommendations. 09:20 is observation only; ordinary snapshots are not auction fields. |
| Save before display | Signals retain unique IDs, algorithm version, scan/quote times, factors, ranks, reasons, risks and trigger/invalidation conditions. Selected signals and deeply verified non-selected controls are retained for each round; daily records group by symbol. Tasks are idempotent and manual refreshes do not duplicate training observations. |
| Delayed review | After the T+1, T+3 and T+5 closes, the trading calendar determines observed returns, MFE and positive adverse-excursion MAE. Not-yet-due labels stay pending; missing data stays pending completion. Legacy records retain their original definitions and stay outside V1.1 training. |
| Simulated execution | Observed price changes and simulated returns are stored separately. Simulations enter on the first complete minute after the signal and exit at the horizon close, applying account-backtest fill constraints and costs. Unfillable orders remain unfilled. Simulations are not actual user trades. |

### Local background tasks

Automatic recommendations and automatic learning have separate switches. Windows tasks and macOS scheduling use Asia/Shanghai time, trading-day checks and cross-process deduplication. Missed slots are recorded without fabricating historical recommendations.

| Time (Asia/Shanghai) | Work |
| --- | --- |
| 09:20 | Premarket watchlist |
| 09:40 | Independent scan with short windows |
| 09:55, 10:30, 14:55 | Reread main-board snapshots and independently scan |
| 15:30 | Daily archiving and due T+1/3/5 reviews |
| Friday 15:45 | Training, validation and model evaluation |

### Automatic learning and promotion

V1.1 uses recorded, confirmed main-board signals and controls with separate model and label versions. Logistic regression is the baseline and LightGBM the candidate. Promotion testing starts only after at least **120 independent mature trading days and 1,000 valid samples**. Training, calibration and test intervals follow time order, with at least five trading days purged at boundaries.

A candidate enters **20 trading days of prospective shadow validation** only when the **95% lower confidence bounds** of both out-of-sample returns after costs and its return advantage over current rules are positive, with no deterioration in drawdown. It activates automatically only after passing. Insufficient samples or failed gates retain the current rules or a previously qualified version. These are promotion requirements, not claims that a profitable model already exists.

The learned ranking combines executable return and downside risk, mapped to calibration-set percentiles. The previous model is retained, with automatic fallback on inference failure or version incompatibility. Recommendations, review, training and promotion run locally without LLM calls.

## Quantitative algorithms

These are research modules in the independent “策略” page and strategy groups, with their own training universes and evaluation targets. V1.1 picks use the signal records and promotion process above.

| Module | Algorithms and purpose |
| --- | --- |
| Price/volume features | Alpha158-style momentum, returns, volatility, price/volume relationships and candlestick features using information available through each timestamp. |
| BalancedRank | LightGBM LambdaRank, DoubleEnsemble-inspired hard-sample reweighting and a Ridge baseline for 5/20-day cross-sectional ranking, with exposure neutralization and out-of-sample nonnegative ensemble weights. |
| SafeBound | LightGBM quantile regression for 20/60-day lower return bounds, out-of-sample quantile corrections, volatility and drawdown estimates, compared with rolling-volatility/HAR-style baselines. |
| LimitPulse | LightGBM and logistic-regression discrete-time first-limit-touch hazards over 1–3 days, per-day sigmoid calibration and cumulative event probability `1 − ∏(1 − hᵢ)`. Touching a limit does not imply a fill or profit. |
| MASTER challenger | A local reimplementation inspired by the Market-Guided Stock Transformer: market-guided feature gates, temporal attention and cross-stock attention; independently qualified local checkpoints only. |
| Model assessment | Purged walk-forward evaluation, separate calibration/holdout periods, RankIC, NDCG, cost-adjusted research labels and time-block bootstrap; qualified champion retention. |
| Account backtesting | Event-driven simulation of point-in-time signals, trading calendars, execution restrictions, slippage and fees, including a one-session-delay comparison. Separate from model-label evaluation. |

SafeBound, BalancedRank and LimitPulse are project module names. Ranking percentiles are not win probabilities. Research labels, paper results and observed price changes are not live trading returns. These independent research modules require training on the selected universe; upstream pretrained weights are not distributed.

## Windows

Python 3.11+ can install the research tools from this public repository through pip; 3.12 is recommended. Desktop wheels are built locally and are not published on PyPI. See [local building and distribution](docs/LOCAL_BUILD.en.md) for installation and platform differences.

Development requires Python, Node.js 20+, Go 1.26 (as required by `desktop/go.mod`) and Wails 2.11. Go 1.27.1 failed during this project's Wails 2.11 binding build; the packaging scripts select Go 1.26.8.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r daily-engine/requirements.txt
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
cd desktop
wails dev
```

Build a native desktop wheel (no NSIS, Actions or paid certificate required):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-wheel.ps1
```

## macOS (Apple Silicon)

Supports Apple Silicon (M series) and macOS 15+; no Intel Mac build is provided. For a source build, first install an official macOS universal2 Python 3.11 or 3.12 (3.12 recommended) from [python.org](https://www.python.org/downloads/macos/). Use its native ARM64 interpreter. Also install Node.js 20+, Go 1.26, Xcode Command Line Tools and `libomp`; if `brew` is missing, follow the [official Homebrew installer](https://brew.sh/) first. Check the command line tools with `xcode-select -p`; if missing, run `xcode-select --install` and wait for installation to finish. Go 1.27.1 failed during this project's Wails 2.11 binding build. The build script checks the Python interpreter and project virtual environment before the expensive build stages.

Build from source:

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

If you installed Python 3.11, change `PYTHON_BIN` to `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11`. The script uses Go 1.26.8, installs Wails 2.11 if needed, and recreates an incompatible project `.venv` before the build. Standard `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` settings are inherited by build commands when your network needs a proxy. Dependency downloads and the full test, desktop, engine and wheel builds can take considerable time on a first run.

The outputs are `artifacts/macos/Stock-King-macos-arm64.zip` and a wheel in `dist/desktop/`. Extract the archive, move `Stock King.app` to `/Applications`, open it, and confirm “已连接” (Connected) in the interface. The build is ad-hoc signed and requires no purchased developer certificate; it is not Apple-notarized, so first launch may need approval in System Settings → Privacy & Security. The script stages the signed application outside cloud-synced folders to avoid extended attributes invalidating the signature.

From the source checkout, enable independent background research, including when the desktop window is closed:

```bash
python3 scripts/install-macos-schedule.py
# Remove later: python3 scripts/install-macos-schedule.py --remove
```

This is a separate, optional step after installing the `.app`; the desktop build does not install background jobs. Jobs look for `/Applications/Stock King.app` by default. Stay logged in with the computer awake. Weekday preparation starts at 09:10, 09:30, 09:45, 10:20 and 14:45, for the 09:20 watchlist and 09:40, 09:55, 10:30 and 14:55 scans. Archiving and review run at 15:30, and Friday learning at 15:45. All times are Asia/Shanghai regardless of the Mac timezone. Missed intraday slots are not backfilled. The native `macosx_15_0_arm64` wheel includes LightGBM and PyTorch for MASTER.

## Acknowledgements

Thank you to these authors, teams and all contributors. Stock King integrates or adapts their work and retains applicable copyright and license notices.

| Authors / team | Project and contribution |
| --- | --- |
| [ArvinLovegood](https://github.com/ArvinLovegood) and contributors | [go-stock](https://github.com/ArvinLovegood/go-stock): Go/Wails/Vue desktop foundation (GPL-3.0). |
| [ZhuLinsen](https://github.com/ZhuLinsen) and contributors | [Daily Stock Analysis](https://github.com/ZhuLinsen/daily_stock_analysis): research engine (MIT); [AlphaSift](https://github.com/ZhuLinsen/alphasift): screening code and strategy material (Apache-2.0). |
| Tong Li, Zhaoyang Liu, Yanyan Shen, Xue Wang, Haokun Chen, Sen Huang / SJTU-DMTai | [MASTER](https://github.com/SJTU-DMTai/MASTER), AAAI 2024: market-guided attention architecture reference (MIT). |
| Microsoft Qlib team and contributors | [Qlib](https://github.com/microsoft/qlib): Alpha158-style features, DoubleEnsemble ideas and quantitative research references; the local implementation is not a claim of full reproduction. |
| LightGBM, PyTorch and scikit-learn authors and maintainers | [LightGBM](https://github.com/lightgbm-org/LightGBM), [PyTorch](https://github.com/pytorch/pytorch), [scikit-learn](https://github.com/scikit-learn/scikit-learn): training, ranking, neural networks and statistical modelling. |
| AKShare, Wails, Vue, Naive UI and dependency maintainers | [AKShare](https://github.com/akfamily/akshare) and the data, desktop and interface ecosystems. |

See [third-party notices](THIRD_PARTY_NOTICES.md) and [licenses/](licenses/) for license details.

## Development and license

`desktop/` contains the Wails/Vue app; `daily-engine/` contains the Python engine. Run relevant Python tests, frontend `npm test`, and desktop `go test .` after changes.

See [research workflow](docs/RESEARCH_WORKFLOW.md), [backtest methodology](docs/EXECUTABLE_BACKTEST.md), and [integration](INTEGRATION.md).

See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for project and upstream licenses. For research only; no investment advice or promised returns.
