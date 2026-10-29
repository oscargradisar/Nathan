"""Desktop tools: the agent can open your games, apps and websites when you ask.

Deliberately narrow: it can *open* things (a Steam game by name, a Start-menu app, a
web page). It can't type into them, delete anything, or run arbitrary commands.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

from nathan.tools.base import Tool, params


def _steam_root() -> Path | None:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return Path(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
        for p in (Path(os.getenv("ProgramFiles(x86)", "")) / "Steam", Path("C:/Program Files/Steam")):
            if p.exists():
                return p
    mac = Path.home() / "Library/Application Support/Steam"
    return mac if mac.exists() else None


def steam_library() -> dict[str, str]:
    """{game name: appid} for every installed Steam game."""
    root = _steam_root()
    if root is None:
        return {}
    libs = {root}
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        libs |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))}
    games = {}
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            text = acf.read_text(errors="ignore")
            appid, name = re.search(r'"appid"\s+"(\d+)"', text), re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and "Steamworks" not in name.group(1):
                games[name.group(1)] = appid.group(1)
    return games


def _best(name: str, options: list[str]) -> str | None:
    low = {o.lower(): o for o in options}
    q = name.lower().strip()
    hits = [o for k, o in low.items() if q in k] or difflib.get_close_matches(q, list(low), n=1, cutoff=0.5)
    return (low.get(hits[0], hits[0]) if hits else None)


def _start(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # Explorer opens it exactly as a double-click would
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])


def _start_menu() -> dict[str, str | Path]:
    if sys.platform == "win32":  # every Start-menu app, Store apps (Spotify...) included
        import json

        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                             check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            rows = json.loads(out.stdout or "[]")
            return {r["Name"]: f"shell:AppsFolder\\{r['AppID']}" for r in (rows if isinstance(rows, list) else [rows])
                    if "uninstall" not in r["Name"].lower()}
        except (ValueError, KeyError, TypeError):
            pass  # fall back to scanning shortcut folders
    roots = [Path(os.getenv("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
             Path("/Applications")]
    apps: dict[str, str | Path] = {}
    for r in roots:
        if r.exists():
            for f in list(r.rglob("*.lnk")) + list(r.glob("*.app")):
                if "uninstall" not in f.stem.lower():
                    apps.setdefault(f.stem, f)
    return apps


def list_games(query: str = "") -> str:
    games = steam_library()
    names = sorted(n for n in games if query.lower() in n.lower())
    return "\n".join(names) if names else ("no Steam games found" if not games else f"no installed game matches {query!r}")


def play_game(name: str) -> str:
    games = steam_library()
    pick = _best(name, list(games))
    if not pick:
        return f"No installed Steam game matches {name!r}. Installed: {', '.join(sorted(games)[:30]) or 'none'}"
    _start(f"steam://rungameid/{games[pick]}")
    return f"Launching {pick} through Steam."


def open_app(name: str) -> str:
    apps = _start_menu()
    pick = _best(name, list(apps))
    if not pick:
        return f"No app called {name!r} in the Start menu."
    target = str(apps[pick])
    if target.startswith("shell:"):
        subprocess.Popen(["explorer.exe", target])  # how the Start menu itself launches apps
    else:
        _start(target)
    return f"Opened {pick}."


def open_link(url: str) -> str:
    if not re.match(r"^https?://", url):
        return "ERROR: only http(s) links can be opened"
    webbrowser.open(url)
    return f"Opened {url} in your browser."


def make_tools() -> list[Tool]:
    return [
        Tool("list_games", "List installed Steam games (optionally filtered).",
             params([], query=("string", "part of a name, or empty for all")), list_games),
        Tool("play_game", "Launch an installed Steam game by (part of) its name.",
             params(name=("string", "game name, e.g. 'rhythia'")), play_game),
        Tool("open_app", "Open an app from the Start menu by (part of) its name.",
             params(name=("string", "app name, e.g. 'Spotify'")), open_app),
        Tool("open_link", "Open a web page in the person's own browser.",
             params(url=("string", "http(s) URL")), open_link),
    ]
