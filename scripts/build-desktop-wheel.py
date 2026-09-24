"""Wrap an already built native desktop + sidecar in a platform-specific wheel."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    (ROOT/'build').mkdir(exist_ok=True)
    # A fresh build tree prevents pure/native wheels contaminating each other.
    with tempfile.TemporaryDirectory(prefix='native-wheel-', dir=ROOT/'build') as temporary:
        work = Path(temporary)
        bundle = work/'bundle'
        bundle.mkdir()
        if sys.platform == 'darwin' and platform.machine() == 'arm64':
            app = ROOT/'desktop/build/bin/Stock King.app'
            if not (app/'Contents/Resources/daily-engine/stock_analysis/stock_analysis').is_file():
                raise RuntimeError('Build and smoke-test the macOS app and engine first')
            # ditto preserves framework symlinks, execute bits and the signature.
            # pip's wheel extractor does not preserve these bundle properties.
            subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                            str(app), str(bundle/'Stock King.app.zip')], check=True)
            tag = 'macosx_15_0_arm64'
        elif sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64'):
            app = ROOT/'desktop/build/bin/Stock King.exe'
            if not app.is_file():
                app = ROOT/'desktop/build/bin/stock-king.exe'
            engine = ROOT/'daily-engine/dist/backend/stock_analysis'
            if not (engine/'stock_analysis.exe').is_file():
                raise RuntimeError('Build and smoke-test the Windows sidecar first')
            shutil.copy2(app, bundle/'Stock King.exe')
            shutil.copytree(engine, bundle/'daily-engine/stock_analysis')
            shutil.copytree(ROOT/'licenses', bundle/'licenses')
            shutil.copy2(ROOT/'desktop/internal/windowstoast/LICENSE', bundle/'licenses/go-toast-MIT.txt')
            for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
                shutil.copy2(ROOT/name, bundle/name)
            tag = 'win_amd64'
        else:
            raise RuntimeError('Native wheels require Windows x64 or Apple Silicon with native ARM64 Python')
        subprocess.run([sys.executable, 'setup.py', 'build', '--build-base', str(work/'lib'),
                        'bdist_wheel', '--plat-name', tag, '--dist-dir', str(ROOT/'dist/desktop')],
                       cwd=ROOT, env={**os.environ, 'STOCK_KING_DESKTOP_BUNDLE': str(bundle)}, check=True)


if __name__ == '__main__':
    main()
