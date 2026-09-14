# Stock King

<img src="branding/stock-king-logo.png" alt="Stock King" width="140" />

[简体中文](README.md) | English

A local stock research desktop: quotes, charts, watchlists, scheduled picks, observation review and local model learning.

- Free Tencent/Sina quote fallback with original timestamps and freshness checks.
- Weekday picks at 09:20, 10:30 and 14:55; review at 15:30; Friday learning at 15:45, Asia/Shanghai time.
- Automatic tasks use local models. AI review is manually triggered; no automatic AI API spending.
- User data and models stay local. No automatic trading.

## Recommendation algorithms

Daily picks follow full-market screening → local five-day ranking → quote/risk checks → at most five candidates.

| Stage | Implementation |
| --- | --- |
| Universe and screening | Main-board, non-ST filtering; weighted price change, turnover, volume ratio and traded amount, using 40 / 25 / 20 / 15 screening weights. |
| Local five-day ranking | The independently qualified five-day BalancedRank signal. Missing, unqualified or out-of-scope models produce explicit rule observations; model and rule scores are not mixed. |
| Quote and risk checks | Tencent/Sina fallback, security identifiers, original timestamps, prices and freshness. Incomplete auction evidence remains conditional; unbuyable limit-up names are tracked separately. |
| Review and learning | Persisted source timestamps and observed price changes; unknown evidence stays unknown. Repeated gaps become review reminders. Friday retraining stays within the user's initialized stock universe. |
| Manual AI review | Evidence, risk and disagreement summaries without changing the local ranking; invoked only by the user. |

## Quantitative algorithms

| Module | Algorithms and purpose |
| --- | --- |
| Price/volume features | Alpha158-style momentum, returns, volatility, price/volume relationships and candlestick features using information available through each timestamp. |
| BalancedRank | LightGBM LambdaRank, DoubleEnsemble-inspired hard-sample reweighting and a Ridge baseline for 5/20-day cross-sectional ranking, with exposure neutralization and out-of-sample nonnegative ensemble weights. Daily picks use the qualified five-day branch. |
| SafeBound | LightGBM quantile regression for 20/60-day lower return bounds, out-of-sample quantile corrections, volatility and drawdown estimates, compared with rolling-volatility/HAR-style baselines. |
| LimitPulse | LightGBM and logistic-regression discrete-time first-limit-touch hazards over 1–3 days, per-day sigmoid calibration and cumulative event probability `1 − ∏(1 − hᵢ)`. Touching a limit does not imply a fill or profit. |
| MASTER challenger | A local reimplementation inspired by the Market-Guided Stock Transformer: market-guided feature gates, temporal attention and cross-stock attention; independently qualified local checkpoints only. |
| Model assessment | Purged walk-forward evaluation, separate calibration/holdout periods, RankIC, NDCG, cost-adjusted research labels and time-block bootstrap; qualified champion retention. |
| Account backtesting | Event-driven simulation of point-in-time signals, trading calendars, execution restrictions, slippage and fees, including a one-session-delay comparison. Separate from model-label evaluation. |

SafeBound, BalancedRank and LimitPulse are project module names. Ranking percentiles are not win probabilities. Research labels, paper results and observed price changes are not live trading returns. Models require training on the selected universe; upstream pretrained weights are not distributed.

## Windows

Install Python, Node.js 20+, Go (see `desktop/go.mod`) and Wails 2.11.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r daily-engine/requirements.txt
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
cd desktop
wails dev
```

Build the offline installer with NSIS installed:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-stock-king-v2.ps1
```

## macOS (Apple Silicon)

Install Xcode Command Line Tools, Python 3.12, Node.js 20+, Go and Homebrew `libomp`.

```bash
xcode-select --install
brew install python@3.12 node go libomp
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
export PATH="$PATH:$(go env GOPATH)/bin"
bash scripts/build-macos.sh
```

Extract `artifacts/macos/Stock-King-macos-arm64.zip` and move `Stock King.app` to Applications. The build is ad-hoc signed, not Apple-notarized; first launch may need approval in System Settings → Privacy & Security. The macOS GitHub Actions workflow builds the same archive.

Enable independent background research, including when the desktop window is closed:

```bash
python3 scripts/install-macos-schedule.py
# Remove: python3 scripts/install-macos-schedule.py --remove
```

Stay logged in with the computer awake. Scheduling uses Shanghai time regardless of the Mac timezone. Missed intraday slots are not backfilled. The complete model environment does not currently support Intel Macs.

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
