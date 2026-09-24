"""Cross-platform CLI. Importing the package performs no network or file writes."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import tempfile

from . import __version__


def data_directory():
    from platformdirs import user_data_path
    return Path(os.environ.get('STOCK_KING_HOME') or user_data_path('Stock King', appauthor=False))


def engine_root():
    packaged = Path(__file__).parent / '_engine'
    if packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[2] / 'daily-engine'
    if source.is_dir():
        return source
    raise RuntimeError('Engine resources are missing; reinstall the wheel')


def desktop_path():
    configured = os.environ.get('STOCK_KING_DESKTOP')
    if configured:
        return Path(configured).expanduser().resolve(strict=True)
    bundled = Path(__file__).parent / '_desktop'
    for relative in ('Stock King.app.zip', 'Stock King.app', 'stock-king.exe', 'Stock King.exe'):
        if (bundled / relative).exists():
            return bundled / relative
    if sys.platform == 'darwin':
        for path in (Path('/Applications/Stock King.app'), Path.home()/'Applications/Stock King.app'):
            if path.is_dir():
                return path
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')
        for relative in ('Programs/Stock King/stock-king.exe', 'Stock King/stock-king.exe'):
            if (base / relative).is_file():
                return base / relative
    return None


def materialize_desktop(executable):
    if sys.platform != 'darwin' or not str(executable).endswith('.app.zip'):
        return executable
    with executable.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()[:20]
    applications = data_directory()/'applications'
    destination = applications/digest
    app = destination/'Stock King.app'
    if not app.is_dir():
        applications.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=applications) as directory:
            subprocess.run(['ditto', '-x', '-k', str(executable), directory], check=True)
            if not (Path(directory)/'Stock King.app/Contents/MacOS/Stock King').is_file():
                raise RuntimeError('Invalid macOS desktop archive')
            try:
                Path(directory).rename(destination)
            except FileExistsError:
                if not app.is_dir():
                    raise
    return app


def launch_desktop():
    executable = desktop_path()
    if executable is None:
        raise RuntimeError('Install a locally built Windows/macOS desktop wheel (see docs/LOCAL_BUILD.en.md), '
                           'or set STOCK_KING_DESKTOP to your installed app. The source wheel provides research commands.')
    executable = materialize_desktop(executable)
    if sys.platform == 'darwin' and executable.suffix == '.app':
        subprocess.run(['open', str(executable)], check=True)
    elif sys.platform == 'win32' and executable.suffix.lower() == '.exe':
        subprocess.Popen([str(executable)], cwd=executable.parent, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        raise RuntimeError('This desktop bundle does not match this operating system')


def prepare_engine():
    root = engine_root()
    sys.path.insert(0, str(root))
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stock King desktop and research')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('desktop', help='Open the installed/bundled desktop app')
    commands.add_parser('doctor', help='Print runtime status without reading secrets or requesting data')
    evidence = commands.add_parser('evidence', help='Fetch archived AKShare financial/event evidence')
    evidence.add_argument('code')
    evidence.add_argument('--as-of', help='Historical ISO timestamp; archive-only, no network')
    serve = commands.add_parser('engine', help='Run the authenticated loopback research API')
    serve.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            print(json.dumps({'version': __version__, 'platform': platform.system(), 'architecture': platform.machine(),
                'python': platform.python_version(), 'dataDirectory': str(data_directory()),
                'engineResources': str(engine_root()), 'desktop': str(desktop_path() or ''),
                'dependencies': {name: importlib.util.find_spec(name) is not None for name in ('akshare', 'fastapi', 'torch', 'lightgbm')}},
                ensure_ascii=False, indent=2))
        elif args.command == 'evidence':
            prepare_engine()
            from src.services.akshare_context import AkshareContextProvider, for_stock, timestamp
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            cutoff = timestamp(args.as_of) if args.as_of else now
            provider = AkshareContextProvider(data_directory()/'daily/king-adaptive/akshare-context')
            bundle = provider.collect(cutoff, refresh=not bool(args.as_of))
            decision = cutoff if args.as_of else datetime.now(ZoneInfo('Asia/Shanghai'))
            print(json.dumps(for_stock(bundle, args.code, decision), ensure_ascii=False, indent=2))
        elif args.command == 'engine':
            if not 1 <= args.port <= 65535:
                parser.error('port must be between 1 and 65535')
            prepare_engine()
            state = data_directory()
            state.mkdir(parents=True, exist_ok=True)
            os.chdir(state)
            defaults = {'DATABASE_PATH': str(state/'daily/research.db'), 'LOG_DIR': str(state/'logs'),
                'SCREENING_DATA_DIR': str(state/'cache/screening'), 'YAO_SCOUT_DATA_DIR': str(state/'daily/king-adaptive'),
                'MODEL_DIR': str(state/'models'), 'CORS_ALLOW_ALL': 'false', 'SCREENING_ENABLED': 'true'}
            for key, value in defaults.items():
                os.environ.setdefault(key, value)
            for folder in ('daily', 'logs', 'cache/screening', 'daily/king-adaptive', 'models'):
                (state/folder).mkdir(parents=True, exist_ok=True)
            token = os.environ.get('STOCK_KING_SIDECAR_TOKEN') or secrets.token_hex(32)
            os.environ['STOCK_KING_SIDECAR_TOKEN'] = token
            credential = state/'engine-token'
            credential.write_text(token, encoding='utf-8')
            if os.name != 'nt':
                credential.chmod(0o600)
            print(f'Loopback API: http://127.0.0.1:{args.port}; token file: {credential}', flush=True)
            import uvicorn
            from api.app import create_app
            uvicorn.run(create_app(), host='127.0.0.1', port=args.port)
        else:
            launch_desktop()
    except ImportError as exc:
        parser.exit(2, f'Missing dependency: {exc.name}. Install stock-king[engine] (or [data] for evidence).\n')
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, str(exc) + '\n')
