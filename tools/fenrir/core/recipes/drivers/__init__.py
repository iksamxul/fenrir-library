"""Drivers (recipes.md section 3.1): one class for each way a recipe runs, with one interface, so the router, the keeper, the
scheduler, Ask Fenrir, the bot and Fenrir Link call a server without knowing its kind.

    process.py    game, app, database and your own servers: a program Fenrir starts (2.x's GameServer grown up)
    minecraft     the one ServerManager, adapted (REC-4)
    docker        an image or a compose file (REC-9)

An Instance is one server made from a recipe: DATA/games/servers/<id>/ with its server.json (recipes.md section 3.9).
REC-5 makes, lists and removes instances; a driver only runs one. 2.x's servers keep running through core/games until
REC-3 moves them here."""
import json
import os
import re
import threading
from pathlib import Path

from ...games import servers
from ...games.servers import ServerError
from .. import load

STATES = ("stopped", "installing", "starting", "ready", "unhealthy", "stopping", "crashed", "held")
ID = re.compile(r"[a-z][a-z0-9-]{1,48}")  # a recipe's id, or <recipe>-<8 hex>: never a path from a request


class UseButton(ServerError):
    """A console line that would end the server: its Stop or Restart does that instead. use: "stop" or "restart"."""

    def __init__(self, use, text):
        super().__init__(text)
        self.use = use


class SettingRefused(ServerError):
    """A setting's value Fenrir will not keep; key names the field, so the form can mark it."""

    def __init__(self, text, key=None):
        super().__init__(text)
        self.key = key


class Instance:
    """One server made from a recipe, in its own folder: server.json (its settings, care and the process Fenrir started),
    server.log (its output), install/ (its program, unless it comes with the game), saves/ where the recipe says so.
    game_dir is the game's own folder (read only) for a server that comes with the game; kit_dir a kit's folder. profile
    stands in for the running account's profile, where a game keeps its saves when it cannot be told elsewhere: the
    harness gives its own, so a fake run never writes into the host's real profile."""

    def __init__(self, sid, recipe, home=None, game_dir=None, kit_dir=None, platform=None, profile=None):
        if not isinstance(sid, str) or not ID.fullmatch(sid):
            raise ServerError("That is not a server Fenrir knows.")
        self.id, self.recipe = sid, recipe
        self.dir = Path(home or servers.HOME) / sid
        self.state_path, self.log = self.dir / "server.json", self.dir / "server.log"
        self.lock = threading.RLock()
        self.game_dir = Path(game_dir) if game_dir else None
        self.kit_dir = Path(kit_dir) if kit_dir else None
        self.platform = platform or load.here()[0]
        self.profile = Path(profile) if profile else None

    def doc(self):
        """The recipe as it runs on this machine's platform, its overrides merged."""
        return self.recipe.view(self.platform)

    def load(self):
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("settings", {})
        return d

    def save(self, d):
        """Whole or not at all: a temp file moved into place, as 2.x GameServer.save does."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"server.{os.getpid()}-{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(d, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    def install_dir(self):
        """The server's program folder: the game's own for a server that comes with the game (read only), else the one
        the host picked, else install/ in its own folder."""
        if (self.doc().get("get") or {}).get("fromGame"):
            if not self.game_dir:
                raise ServerError(f"{self.recipe.name}'s server comes with the game: install the game first.")
            return self.game_dir
        return Path(self.load().get("installDir") or (self.dir / "install"))


class Driver:
    """The interface every driver keeps. inst is an Instance; progress, where given, takes (percent or None, words)."""
    kinds = ()

    def install(self, inst, progress=None):
        raise NotImplementedError

    def configure(self, inst, when="start"):
        raise NotImplementedError

    def start(self, inst):
        raise NotImplementedError

    def state(self, inst):
        raise NotImplementedError

    def players(self, inst):
        raise NotImplementedError

    def console(self, inst, line):
        raise NotImplementedError

    def stop(self, inst, force=False):
        raise NotImplementedError

    def update(self, inst, progress=None):
        raise NotImplementedError

    def backup_parts(self, inst):
        raise NotImplementedError


HOOK_DRIVERS = {}  # a built-in recipe's hook -> the driver class that adds what data cannot say (REC-3: fivem, skyrimtogether)


def register(hook, cls):
    """Fenrir's own code only: the class a built-in recipe's hook runs with, usually a ProcessDriver of its own."""
    HOOK_DRIVERS[hook] = cls


def for_recipe(recipe):
    """The driver that runs a recipe: its hook's own, for a built-in recipe that names one, else the one for its kind."""
    from .. import hooks  # noqa: F401 - the built-in hooks register their drivers, writers and download pages
    from .process import ProcessDriver
    hook = recipe.doc.get("hooks")
    if hook and recipe.source == "builtin" and hook in HOOK_DRIVERS:  # policy keeps hooks built-in; this never trusts it alone
        return HOOK_DRIVERS[hook]()
    if recipe.kind in ProcessDriver.kinds:
        return ProcessDriver()
    raise ServerError(f"Fenrir cannot run a {recipe.kind} recipe here yet.")
