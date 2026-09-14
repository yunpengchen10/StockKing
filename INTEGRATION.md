# Stock King process and data boundaries

Stock King has one desktop UI. The `daily-engine` directory is a private sidecar used by that UI, not a second Web or Electron application.

```text
Vue components
    │ generated Wails bindings
    ▼
Go/Wails process
    ├─ desktop window and navigation
    ├─ K-lines, watchlists, notes, recommendation history
    ├─ local SQLite and scheduled-task entry points
    └─ sidecar lifecycle and authenticated HTTP bridge
             │ random 127.0.0.1 port
             │ X-Stock-King-Token
             ▼
       Python/FastAPI sidecar
       ├─ adaptive King Picks and official scans
       ├─ screening and market-data fallbacks
       ├─ quant training and backtests
       └─ outcomes, DLM, calibration, and audit storage
```

## Security boundary

- The engine binds to loopback only.
- Every desktop launch creates a new cryptographically random access token.
- Vue only calls generated Go bindings; it cannot read the token or call the sidecar directly.
- Windows starts the frozen sidecar with no-window flags. macOS bundles the native sidecar under `Contents/Resources/daily-engine/stock_analysis`.
- Scheduled jobs call the desktop executable with `--stock-king-task=<slot>`. On macOS, a launchd shell wrapper selects Shanghai trading slots and prevents duplicate starts.

## Ownership

| Capability | Owner |
| --- | --- |
| Navigation, layout, King Picks cards and history drawer | Vue |
| Sidecar authentication/lifecycle, K-lines, notes, local desktop data | Go/Wails |
| M1-M6/NLS scans, snapshots, DLM, labels and learning | Python engine |
| SafeBound/BalancedRank/LimitPulse training and release gates | Python engine |
| Windows task registration and installer migration | NSIS + PowerShell release scripts |
| macOS background schedule | Per-user launchd agent, installed with `scripts/install-macos-schedule.py` |

## Persistence

- `%APPDATA%\Stock King`: databases, King Picks history, user preferences, DLM, and model state.
- `%LOCALAPPDATA%\Stock King`: logs, caches, and other rebuildable data.
- Installation directory: read-only application, frozen sidecar, resources, and license files.
- macOS: persistent data in `~/Library/Application Support/Stock King`; cache and logs in `~/Library/Caches/Stock King`.

`live` scans are persisted for the user-facing history but are excluded from learning samples. Official time-slot results and post-close outcomes form the auditable learning stream.

## Failure behavior

- The last saved King Picks snapshot stays visible while a background refresh is running.
- Missing or stale evidence may yield fewer than five candidates or an empty list.
- Insufficient history suppresses probability claims but does not invent synthetic samples.
- If the sidecar fails, Go exposes status and restart information to the UI; it does not silently fall back to a random recommendation.
