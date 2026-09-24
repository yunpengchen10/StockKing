"""Install the built wheel outside the checkout and verify bundled resources."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    wheels = sorted((ROOT/'dist/desktop' if args.native else ROOT/'dist').glob('*.whl'))
    assert len(wheels) == 1, 'Use a clean output directory with exactly one wheel'
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert any(n.endswith('_engine/src/services/akshare_context.py') for n in names)
        assert any(n.endswith('_engine/resources/stocks.index.json') for n in names)
        assert not any(n.endswith(('.db', '.sqlite', '/.env', '.pt', '.pkl', '.joblib')) for n in names)
        assert any('.dist-info/licenses/LICENSE' in n for n in names)
        assert any(n.endswith('/licenses/desktop/internal/windowstoast/LICENSE') for n in names)
        assert any(n.endswith('/licenses/daily-engine/src/services/screening/LICENSE') for n in names)
    (ROOT/'.package-check').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'.package-check') as directory:
        work = Path(directory)
        environment = work/'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        subprocess.run([str(python), '-m', 'pip', 'install', str(wheels[0])+'[data]'], check=True)
        env = {**os.environ, 'STOCK_KING_HOME': str(work/'isolated-state'), 'PYTHONUTF8': '1'}
        result = subprocess.run([str(python), '-m', 'stock_king', 'doctor'], cwd=work, env=env,
                                check=True, capture_output=True, text=True, encoding='utf-8')
        doctor = json.loads(result.stdout)
        assert str(environment) in doctor['engineResources']
        assert not (work/'isolated-state').exists(), 'doctor must be read-only'
        result = subprocess.run([str(python), '-m', 'stock_king', 'evidence', '600001',
                                 '--as-of', '2020-01-01T10:30:00+08:00'], cwd=work, env=env,
                                check=True, capture_output=True, text=True, encoding='utf-8')
        assert not any(json.loads(result.stdout)['coverage'].values())
        if args.native:
            assert doctor['desktop'], 'Native wheel is missing the application'
            probe = """
from stock_king.cli import desktop_path, materialize_desktop
import subprocess, sys
app = materialize_desktop(desktop_path())
executable = app/'Contents/MacOS/Stock King' if sys.platform == 'darwin' else app
result = subprocess.run([str(executable), '--stock-king-task=invalid'], timeout=30)
assert result.returncode == 2, result.returncode
"""
            subprocess.run([str(python), '-c', probe], cwd=work, env=env, check=True)
        print('Clean pip install, resource isolation, read-only doctor and archive-only replay passed')


if __name__ == '__main__':
    main()
