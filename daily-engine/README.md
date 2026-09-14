# Stock King Research Engine

This directory contains the Python sidecar embedded in the Stock King desktop application. It is an implementation component—not a second product and not a standalone desktop or Web frontend.

The engine provides the private FastAPI endpoints used by Wails for:

- adaptive King Picks (M1-M6, NLS, history, DLM, calibration, and outcomes);
- full-market screening and market-data normalization;
- quantitative training, validation, backtesting, and release gates;
- research reports, history, optional LLM explanations, and notifications;
- local model, task, and audit storage.

## Runtime boundary

In normal operation `desktop/sidecar.go` starts the packaged `stock_analysis.exe` on a random loopback port with a random token. The process binds only to `127.0.0.1`; Vue talks to Go through Wails bindings and never receives the port or token.

The repository intentionally does not include the upstream Daily React/Electron applications. `api/app.py` returns a small diagnostic page at `/`; the supported user interface is `../desktop/frontend`.

## Important paths

| Path | Purpose |
| --- | --- |
| `api/` | FastAPI application, versioned endpoints, and schemas |
| `src/services/yao_scout/` | Adaptive King Picks, scan history, labels, and learning |
| `src/quant/` | Quantitative datasets, models, evaluation, and task orchestration |
| `data_provider/` | Market data adapters and fallbacks |
| `src/storage.py` | Local database models and non-destructive migrations |
| `resources/stocks.index.json` | Stock search index bundled by PyInstaller |
| `scripts/build-backend.ps1` | Windows no-console PyInstaller build |
| `scripts/install-yao-scout-tasks.ps1` | Legacy scheduled-task cleanup only |

`bot/`, `templates/`, and notification modules remain because the research/report pipeline imports their shared message types and renderers. They are engine dependencies, not separate launch targets.

## Development

Create the repository-level virtual environment first:

```powershell
cd ..
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\daily-engine\requirements.txt
cd .\daily-engine
```

Run the API for diagnostics:

```powershell
..\.venv\Scripts\python.exe main.py --serve-only --host 127.0.0.1 --port 8000
```

The preferred end-to-end development command is still `wails dev` from `../desktop`, because it exercises the real lifecycle, environment, and authentication path.

Core release tests:

```powershell
..\.venv\Scripts\python.exe -m pytest `
  tests/test_king_adaptive.py `
  tests/test_yao_scout.py `
  tests/test_stock_king_tier_models.py `
  tests/test_stock_king_quant.py `
  tests/test_quant_training_tasks.py `
  tests/test_stock_king_sidecar_auth.py `
  tests/test_windows_scheduled_tasks.py -q
```

Freeze the sidecar:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-backend.ps1
```

The output is `dist/backend/stock_analysis/stock_analysis.exe`. PyInstaller uses windowed/no-console mode and bundles the stock index from `resources/`.

## Configuration and data

Copy `.env.example` to `.env` only when optional data providers, LLMs, or notifications are needed. Never commit `.env`, API keys, databases, logs, caches, or model checkpoints.

The desktop overrides data locations so persistent state is stored under `%APPDATA%\Stock King` and rebuildable state under `%LOCALAPPDATA%\Stock King`. Direct engine runs default to local development paths ignored by Git.

## Upstream attribution

The engine evolved from [Daily Stock Analysis](https://github.com/ZhuLinsen/daily_stock_analysis) and retains compatible modules required by Stock King. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The trimmed repository does not present itself as a redistribution of the upstream product.
