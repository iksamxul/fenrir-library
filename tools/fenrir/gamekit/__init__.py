"""The game kit (1.40): what Fenrir and Fenrir Connect share to find the games on a PC, fetch their mods from where their
makers publish them, and put the files into a game folder and out again so that every original comes back. It imports
nothing of either app: each tells it where its records live (set_home), which app it is (set_app: the name and version in
each request, and in the note a game folder keeps of whose mods are in it) and where a line for the person at the PC goes
(on_event). Standard library only.

    library      which games are installed, per store (Steam, Epic, GOG, Ubisoft, EA, Xbox) and by hand
    catalog      each game's rules: anti-cheat, where mods go, how it starts, its kit; Thunderstore's own game data
    deploy       a game's mods placed into its folder and taken out again, the originals kept and put back
    store        one game's mods: the list, installs from each source, Play, and the session watcher
    thunderstore, kits, sources_web, archives, net   the sources, and how files arrive
    hooks, tools what some games need beyond files (Skyrim's plugin list, Seamless Co-op's password, Overstrike)
    procs        the process list a session watcher reads
    oslayer      what differs between Windows, Linux and macOS (3.0): folders, processes, links, case, versions, opening
RESEARCH.md keeps the facts and sources behind each game's rules.
"""
import os
from pathlib import Path

from . import oslayer

_home = [None]
_event = [None]
_app = {"name": "Fenrir", "version": "0"}


def home():
    """Where the kit keeps its records: the folder the app set, else Fenrir's data folder's games."""
    if _home[0]:
        return Path(_home[0])
    base = os.environ.get("FENRIR_DATA") or str(oslayer.data_home("Fenrir"))  # %LOCALAPPDATA%\Fenrir on Windows, as before
    return Path(base) / "games"


def set_home(path):
    _home[0] = Path(path)


def set_app(name, version):
    _app.update(name=str(name), version=str(version))


def app():
    """{"name", "version"} of the app using the kit."""
    return dict(_app)


def on_event(fn):
    """fn(text, level): where a line for the person at the PC goes (Fenrir's timeline, Fenrir Connect's log)."""
    _event[0] = fn


def event(text, level="info"):
    fn = _event[0]
    if fn is None:
        print(f"games: {text}")
        return
    try:
        fn(text, level)
    except Exception:  # noqa: BLE001 - a line that could not be written never stops the work it reports
        pass
