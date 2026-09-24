"""Build the same engine into an isolated resource namespace; never user state."""
import os
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

ROOT = Path(__file__).parent
ENGINE_DIRS = ('src', 'api', 'data_provider', 'bot', 'strategies', 'resources')
EXTENSIONS = {'.py', '.yaml', '.yml', '.json', '.txt', '.csv', '.md', '.html', '.j2', '.jinja2', '.png', '.jpg'}


class Build(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / 'stock_king' / '_engine'
        root = ROOT / 'daily-engine'
        for directory in ENGINE_DIRS:
            for path in (root / directory).rglob('*'):
                if path.is_file() and (path.suffix in EXTENSIONS or path.name in {'LICENSE', 'NOTICE'}) and not any(
                    p in {'__pycache__', 'node_modules', '.git', 'logs', 'output'} for p in path.relative_to(root / directory).parts[:-1]
                ):
                    dest = target / path.relative_to(root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
        for name in ('main.py', 'server.py', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target / name)
        # Native CI supplies one verified application bundle for this platform.
        native = os.environ.get('STOCK_KING_DESKTOP_BUNDLE')
        if native:
            source = Path(native).resolve(strict=True)
            if not source.is_dir():
                raise ValueError('Desktop bundle must be a directory')
            shutil.copytree(source, Path(self.build_lib) / 'stock_king' / '_desktop', dirs_exist_ok=True)


class Wheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        if os.environ.get('STOCK_KING_DESKTOP_BUNDLE'):
            self.root_is_pure = False

    def get_tag(self):
        python, abi, platform = super().get_tag()
        if os.environ.get('STOCK_KING_DESKTOP_BUNDLE'):
            return 'py3', 'none', platform
        return python, abi, platform


setup(cmdclass={'build_py': Build, 'bdist_wheel': Wheel})
