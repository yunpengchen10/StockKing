# Stock King Desktop

This is Stock King v2.5.0's only user-facing application: Wails 2 + Go + Vue 3.

Local screening and scheduled research never call a language model. Manual picks and single-stock reviews share the engine's durable BYOK budget and cache. New watchlist records retain immutable entry quotes; legacy records remain without a baseline. Group deletion retains stocks. The retired portfolio route redirects to the watchlist and stored portfolio data remains intact.

- `frontend/` contains the complete desktop UI, including Adaptive King Picks and its history drawer.
- `sidecar.go` starts the Python engine on a random private port, injects a per-process token, captures logs, restarts on failure, and terminates it on exit.
- `stock_king_api.go` bridges King Picks, persisted snapshots, recommendation history, quant tasks, research notes, and settings.
- `build/windows/` contains the public icons and NSIS hooks used by the Windows installer.

Run from source after preparing the repository `.venv` and frontend dependencies:

```powershell
cd .\frontend
npm ci
npm run build
cd ..
go test .
wails dev
```

Build the complete offline installer from the repository root with `scripts/build-stock-king-v2.ps1`. Do not package this directory alone: the formal installer includes the frozen Python engine and its licenses.
