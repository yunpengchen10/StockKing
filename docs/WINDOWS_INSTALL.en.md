# Windows installation and fixes

[简体中文](WINDOWS_INSTALL.md)

## v2.6.3 one-click installer

File: `Stock-King-Setup-x64-v2.6.3.exe`. For Windows 10/11 x64. It includes the desktop app, local research engine, and an offline WebView2 installer. Python, Go and Node.js are not required. WebView2 is installed only if missing; market-data downloads still require a network connection.

1. Close Stock King, then double-click the installer.
2. For an upgrade, select the existing installation directory, for example `D:\Stock King`. New users can use the default directory.
3. Select “Open Stock King” on completion, or double-click the desktop shortcut.
4. Confirm that the window appears and the engine connects, then check Picks → recommendation records, delayed review and learning status.

The distribution contains no personal databases, keys, watchlists or trained models. Existing user data lives under `%APPDATA%\Stock King` and `%LOCALAPPDATA%\Stock King`; uninstall preserves these directories. Back them up before upgrading. The installer registers seven current-user background tasks; automatic recommendations and learning retain separate application switches. The computer must stay on and the user must remain logged in.

Compare `Get-FileHash .\Stock-King-Setup-x64-v2.6.3.exe -Algorithm SHA256` with the accompanying `SHA256SUMS.txt`. The Stock King app and installer are not code-signed; the bundled WebView2 installer has a verified Microsoft signature.

## What changed

v2.6.3 fixes prematurely stopped minute-history pagination, partial responses suppressing fallback, backfill stopping before a full 20-session baseline, and price limits lost during quote merging. It improves financial pagination, sector-cache use and quote freshness. Delayed reviews save available results before fetching missing evidence and settling again; observed price paths remain separate from valid training samples. Missing or conflicting critical data restricts formal candidates and is never replaced with zero.

A fresh installation downloads and accumulates data for its own research universe; it contains no distributor history, positions or models. Initial runs and insufficient history can still show gaps, and absent or inconsistent upstream data cannot be fabricated. Upgrades preserve frozen recommendation snapshots and do not backdate newly collected evidence.

v2.6.2 fixes truncated recommendation-record responses exceeding 16 MiB. Previously, this caused `unexpected end of JSON input` and made records unavailable. The update reads and displays complete records while preserving existing signal data.

The previous local updater launched the desktop app hidden, and repeated launches only reported that it was running. v2.6.1 opens the window normally and checks for a visible window before marking the update complete. Launching the shortcut again restores and focuses the existing window.

If only a background process remains, end **Stock King.exe** in Task Manager and reopen it. Keep the user-data directories; do not replace the new engine with an older installer.

## Maintainer packaging

Build and validate the desktop and frozen engine first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-installer.ps1 `
  -BuiltExe 'desktop/build/bin/Stock King.exe' `
  -BuiltEngineDir 'daily-engine/dist/stock_analysis' `
  -WebViewInstaller 'desktop/build/windows/installer/tmp/MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
```

Output is written to `dist/installers`. The script checks versions, the Microsoft signature and database/configuration files in the payload, then emits the installer and a per-file hash manifest. Use `/S /EXTRACTONLY /D=absolute-directory` to extract and verify the payload without registering tasks, shortcuts or uninstall information, starting the app, or installing WebView2. Application smoke tests should use isolated data directories.
