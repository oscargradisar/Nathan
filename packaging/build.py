"""Build the Nathan app folder with PyInstaller: `uv run python packaging/build.py`.

Output: dist/Nathan/ (Windows) or dist/Nathan.app (macOS). The installers wrap these.
"""

import os
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
sep = os.pathsep  # PyInstaller's --add-data uses "src;dest" on Windows, "src:dest" elsewhere

args = [
    str(ROOT / "packaging" / "nathan_app.py"),
    "--name", "Nathan", "--windowed", "--onedir", "--noconfirm", "--clean",
    "--icon", str(ROOT / "packaging" / "nathan.ico" if sys.platform == "win32" else ROOT / "packaging" / "nathan.png"),
    # config.py looks for providers.yaml next to the nathan package
    "--add-data", f"{ROOT / 'providers.yaml'}{sep}.",
    "--add-data", f"{ROOT / 'nathan' / 'web' / 'static'}{sep}nathan/web/static",
    *[a for f in ("manifest.json", "background.js", "content.js", "core.js", "README.md")
      for a in ("--add-data", f"{ROOT / 'extension' / f}{sep}extension")],
    # loaded by name at runtime, so PyInstaller can't see them
    "--collect-submodules", "uvicorn", "--collect-submodules", "keyring",
    "--collect-submodules", "nathan",
    "--exclude-module", "pytest",
]
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "com.oscargradisar.nathan"]
PyInstaller.__main__.run(args)
