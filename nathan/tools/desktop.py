"""Desktop tools: the agent opens your games, apps and websites, and prepares messages.

Deliberately narrow. It can *open* things (a Steam game, a Start-menu app, a web page)
and *prepare* messages: a Discord DM typed into the box, an email filled in. You press
Send. Discord bans accounts that send automatically ("self-bots"), so the last step is
always yours. It can't delete anything or run arbitrary commands.
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


# ---- messages: prepared by Nathan, sent by you -----------------------------------------

def _foreground_title() -> str:
    if sys.platform != "win32":
        return ""
    import ctypes

    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _keys(*combo: str) -> None:
    """Press a key combination (e.g. 'ctrl', 'k') on Windows."""
    import ctypes

    vk = {"ctrl": 0x11, "enter": 0x0D, "esc": 0x1B, "v": 0x56, "k": 0x4B, "a": 0x41}
    down = [vk[k] for k in combo]
    for code in down:
        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    for code in reversed(down):
        ctypes.windll.user32.keybd_event(code, 0, 2, 0)  # 2 = key up


def _paste(text: str) -> None:
    import time

    import pyperclip

    pyperclip.copy(text)
    time.sleep(0.15)
    _keys("ctrl", "v")


def copy_text(text: str) -> str:
    import pyperclip

    pyperclip.copy(text)
    return "Copied to your clipboard."


def discord_message(friend: str, message: str) -> str:
    """Open the DM with `friend` in the Discord app and type `message` into the box."""
    import time

    import pyperclip

    if sys.platform != "win32":
        pyperclip.copy(message)
        _start("discord://-/channels/@me")
        return "Opened Discord and copied the message: open the chat, paste it, and press Enter."
    _start("discord://-/channels/@me")
    for _ in range(20):  # wait for Discord to come to the front
        time.sleep(0.4)
        if "discord" in _foreground_title().lower():
            break
    else:
        pyperclip.copy(message)
        return "Discord didn't come to the front, so I didn't type anything. The message is on your clipboard."
    time.sleep(0.8)
    _keys("ctrl", "k")  # Discord's quick switcher: jump to any DM, server or channel
    time.sleep(0.6)
    _paste(friend)
    time.sleep(1.2)
    if "discord" not in _foreground_title().lower():
        pyperclip.copy(message)
        return "Discord lost focus, so I stopped. The message is on your clipboard."
    _keys("enter")
    time.sleep(1.5)
    _paste(message)
    return (f"Opened your chat with {friend} in Discord and typed the message. "
            "Check it's the right chat, then press Enter to send.")


def compose_email(to: str, subject: str, body: str) -> str:
    from urllib.parse import quote

    _start(f"mailto:{quote(to, safe='@,')}?subject={quote(subject)}&body={quote(body)}")
    return f"Opened an email to {to} in your mail app, ready to send."


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
        Tool("discord_message", "Message a friend on Discord: opens the chat in the Discord app and types "
             "the message into the box. The person presses Enter to send.",
             params(friend=("string", "their Discord name, e.g. 'WaiterofTables'"),
                    message=("string", "the exact message to send")), discord_message),
        Tool("compose_email", "Open an email in the person's mail app with recipient, subject and text filled in.",
             params(to=("string", "email address"), subject=("string", "subject"), body=("string", "message")),
             compose_email),
        Tool("copy_text", "Put text on the person's clipboard.", params(text=("string", "text")), copy_text),
    ]
