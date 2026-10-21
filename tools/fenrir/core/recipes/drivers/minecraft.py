"""The minecraft driver (recipes.md section 4.2, REC-4): Minecraft as a recipe.

The world still runs in core/serverproc.py's ServerManager, unchanged, with its years of fixes (adoption, RCON, crash
loops, lock retries, the watchdog, the stop cap). This driver adapts it to the driver interface, so the server list, the
API and the keeper's kind of look see the world as one more server:

    state     ServerManager's status, ready, freeze and crash loop as the driver's states
    players   its players; console its command; start and stop call it; a backup takes core/backups.py's parts

One world runs at a time, as in 2.x: the one in serverDir is the live one, and every other world Fenrir knows is listed,
stopped. Each world has an id, minecraft:<folder id>, made from its folder's name (worlds, world); 2.x's own id for the
live world, minecraft, still names it everywhere it did.

The loader recipes (fenrir/recipes/builtin/minecraft-*.json, written by build/minecraft_recipes.py) are data: the
new-world wizard's choices, the Java rules (java_for, equal to 2.x's loaders.java_major_for), the library and the site,
the chat bridge's lines. Installing a loader and finding what a folder holds stay core/loaders.py's.

A harness run (core/recipes/harness) runs a loader recipe's life through the process driver's own path, in a folder of
its own: the launch line loaders.detect_server reads from that folder, with the fake in Java's place, and the loader laid
down through the install's loader seam where 2.x runs the real installer. It never touches ServerManager or a world."""
import os
import re
from pathlib import Path

from ...games.servers import ServerError
from ...util import cached
from .. import load
from . import register
from .process import ProcessDriver

PREFIX = "minecraft:"
LOADERS = ("neoforge", "forge", "fabric", "quilt", "vanilla", "paper", "purpur")
WORLD_TASKS = ("install", "pack", "restore", "backup")  # the tasks that write the live world (2.x's kinds)


# ---------------------------------------------------------------------------------------------------- Java
def _numbers(v):
    """A version's first three numbers, as 2.x reads one ("1.20.5-pre1" -> 1, 20, 5; what is missing counts as 0)."""
    return tuple(([int(x) for x in re.findall(r"\d+", str(v))[:3]] + [0, 0, 0])[:3])


def java_for(recipe, mc_version):
    """The lowest Java a Minecraft version's server needs, by the recipe's rules (newest first): the first rule whose
    version it has reached. No version at all is the newest rule's, one below every rule the oldest's."""
    rules = (recipe.doc if hasattr(recipe, "doc") else recipe)["minecraft"]["java"]
    if not mc_version:
        return rules[0]["major"]
    have = _numbers(mc_version)
    return next((r["major"] for r in rules if have >= _numbers(r["from"])), rules[-1]["major"])


# ---------------------------------------------------------------------------------------------------- the worlds
def folder_id(d):
    """A world's id from its folder's name: lower-case letters, digits and dashes, 40 at most ("ATM10 Server": atm10-server)."""
    return re.sub(r"[^a-z0-9]+", "-", Path(str(d)).name.lower()).strip("-")[:40].strip("-") or "world"


@cached("mc_worlds", 3)
def _rows():
    from ... import profiles
    return profiles.servers_list()["servers"]


def worlds(force=False):
    """Every world Fenrir knows, in the Server list's order (core/profiles.py), each with its id minecraft:<folder id>.
    Two folders whose names make one id: the later one in the list gets -2, -3 and so on."""
    out, seen = [], set()
    for r in _rows(force):
        base = folder_id(r["dir"])
        fid, n = base, 2
        while fid in seen:
            fid, n = f"{base}-{n}", n + 1
        seen.add(fid)
        out.append({**r, "id": PREFIX + fid})
    return out


def is_world(sid):
    sid = str(sid or "")
    return sid == "minecraft" or sid.startswith(PREFIX)


def world(sid):
    """The world an id names, matched against the list (never a path made from it): minecraft:<folder id>, or minecraft,
    2.x's id for the live one. None when there is no such world here."""
    sid = str(sid or "")
    if not is_world(sid):
        return None
    rows = worlds()
    if sid == "minecraft":
        return next((w for w in rows if w.get("active")), None)
    return next((w for w in rows if w["id"] == sid), None)


def live_id():
    """The id of the world Fenrir runs now, or None when its folder is not there."""
    w = world("minecraft")
    return w["id"] if w else None


def journal_id(folder=None):
    """The id of the world a change was made on, for the change journal (core/changes.py records): the world of that
    folder (the live one when none is given), as the Server list names it, else made from the folder's name; minecraft
    when the folder is not there."""
    if folder is None:
        from ...paths import server_dir
        folder = server_dir()
    from ...profiles import same_dir
    hit = next((w["id"] for w in worlds() if same_dir(w["dir"], folder)), None)
    if hit:
        return hit
    return PREFIX + folder_id(folder) if Path(folder).is_dir() else "minecraft"


def aliases(sid):
    """The ids that name one server: the live world answers to minecraft and to its own id (a friend's grant, a hidden
    switch and a journal line may name either); any other server only to its own."""
    sid = str(sid or "")
    if not is_world(sid):
        return {sid}
    live = live_id()
    return {"minecraft", live} if live and sid in ("minecraft", live) else {sid}


def recipe_of(d):
    """(the built-in loader recipe of what a world's folder holds, loaders.detect_server's answer): a plain jar told apart
    by its name (Paper, Purpur); (None, the answer) when nothing Fenrir knows is installed there."""
    from ... import loaders
    info = loaders.detect_server(Path(d))
    key = info.get("loader")
    if key == "vanilla":
        jar = str(info.get("jar") or "").lower()
        key = "purpur" if "purpur" in jar else "paper" if "paper" in jar else "vanilla"
    return (load.builtin().get(f"minecraft-{key}") if key else None), info


def manager():
    """The one ServerManager (core/serverproc.py SRV), which runs the live world."""
    from ...serverproc import SRV
    return SRV


class World:
    """One world as the server list and the routes see a server: its id and name, its folder, the loader recipe of what
    is installed there (r), and ServerManager's word while it is the live one. Nothing here changes the world: its own
    pages and the one door (core/servers/act.py) do."""
    _secrets = ()  # nothing of a world's is a secret here: its RCON password is Fenrir's own (config)

    def __init__(self, row):
        self.id, self.name, self.dir = row["id"], row.get("name") or Path(row["dir"]).name, Path(row["dir"])
        self.active = bool(row.get("active"))
        self.r, self.info = recipe_of(self.dir) if row.get("exists", True) and self.dir.is_dir() else (None, {})
        self.driver = MinecraftDriver()
        self.recipe = {"name": self.name, "ports": [[str(self.port()), "tcp"]], "client": ["minecraft"]}  # 2.x's shape, for the routes

    def props(self):
        from ...packs import read_properties
        return read_properties(self.dir)

    def port(self):
        from ...packs import port_of
        return port_of(self.props())

    def running(self, *a):
        return self.active and manager().running()

    def settings(self):
        """The recipe's settings as this world has them: server.properties' port, slots and name, the memory file's heap, and
        the console link's port (Fenrir's own, config)."""
        from ...config import CFG
        p = self.props()
        out = {"port": self.port(), "motd": p.get("motd", ""), "rconPort": int(CFG.get("rconPort") or 25575)}
        try:
            out["players"] = int(str(p.get("max-players") or "").strip())
        except ValueError:
            pass
        try:
            m = re.search(r"^-Xmx(\d+)G", (self.dir / "user_jvm_args.txt").read_text("utf-8", errors="replace"), re.M)
            if m:
                out["memoryGB"] = int(m.group(1))
        except OSError:
            pass
        return out

    def players(self):
        return self.driver.players(self)

    def care(self):
        return None  # the world's own care (its daily restart, backups, coming back) lives on its own pages


def server(sid):
    """The World object of an id, or ServerError."""
    w = world(sid)
    if w is None:
        raise ServerError("That world is not set up here.")
    return World(w)


def row(sid):
    """One world in the server list's one shape (core/api3/servers.py)."""
    sv = server(sid)
    st = sv.driver.state(sv)
    up = st["state"] in ("starting", "ready", "unhealthy", "stopping")
    pl = sv.driver.players(sv) if up else None
    task = None
    if sv.active:
        from ...util import TASKS
        t = [x for x in TASKS.running() if x.get("kind") in WORLD_TASKS]
        task = {"id": t[0]["id"], "label": t[0]["label"]} if t else None
    return {"id": sv.id, "kind": "minecraft", "name": sv.name, "node": None, "active": sv.active,
            "recipe": {"id": sv.r.id, "version": sv.r.version, "source": sv.r.source} if sv.r else None,
            "state": st["state"], "running": up, "players": {"n": pl["players"], "max": pl.get("max")} if pl else None, "task": task}


# ---------------------------------------------------------------------------------------------------- the driver
def world_state(w):
    """ServerManager's word as the driver's states, for the live world; every other world is stopped."""
    if not w.active:
        return {"state": "stopped", "said": ""}
    srv = manager()
    with srv.lock:
        status, ready, frozen, loop = srv.status, srv.ready, srv.frozen, srv.crash_loop
        code, manual, progress = srv.last_exit, srv.manual_stop, srv.progress
    if loop:
        return {"state": "held", "said": "It crashed again and again, so Fenrir stopped starting it."}
    if status == "stopping":
        return {"state": "stopping", "said": ""}
    if status == "starting" or (status == "running" and not ready):
        return {"state": "starting", "said": str(progress or "")}
    if status == "running":
        return {"state": "unhealthy", "said": "It stopped answering."} if frozen else {"state": "ready", "said": ""}
    if code not in (0, None) and not manual:
        return {"state": "crashed", "said": f"It ended with code {code}."}
    return {"state": "stopped", "said": ""}


class MinecraftDriver(ProcessDriver):
    """A world: ServerManager, adapted. A harness run's folder: the process driver's own path, Java's launch line from the
    folder (loaders.detect_server), the loader laid down by the install's seam."""
    kinds = ("minecraft",)

    @staticmethod
    def _live(w):
        if not w.active:
            raise ServerError("Switch to this world first: one world runs at a time.")

    # ---- the world: ServerManager's own
    def state(self, inst, now=None, reach=None):
        if isinstance(inst, World):
            return world_state(inst)
        return super().state(inst, now=now, reach=reach)

    def running(self, inst):
        if isinstance(inst, World):
            return inst.running()
        return super().running(inst)

    def players(self, inst, reach=None):
        if isinstance(inst, World):
            if not inst.running():
                return None
            srv = manager()
            with srv.lock:
                names = sorted(srv.players)
            mx = inst.settings().get("players")
            return {"players": len(names), "max": mx, "name": inst.name, "names": names}
        return super().players(inst, reach)

    def console(self, inst, line, typer=None):
        if isinstance(inst, World):
            self._live(inst)
            r = manager().command(str(line))
            if not r.get("ok"):
                raise ServerError(r.get("error") or "The world did not take that line.")
            return r
        return super().console(inst, line, typer)

    def start(self, inst, popen=None):
        if isinstance(inst, World):
            self._live(inst)
            return manager().start()
        return super().start(inst, popen=popen)

    def stop(self, inst, force=False, **kw):
        if isinstance(inst, World):
            self._live(inst)
            return manager().stop(force=force)
        return super().stop(inst, force=force, **kw)

    def backup_parts(self, inst):
        if isinstance(inst, World):
            from ... import backups
            return {"paths": [inst.dir / p for p in backups.BACKUP_ITEMS if (inst.dir / p).exists()], "exclude": [],
                    "whileRunning": "pause", "dump": None}
        return super().backup_parts(inst)

    # ---- a harness run's folder: the process driver's path
    def installed(self, inst, doc=None):
        if isinstance(inst, World):
            return bool(inst.info.get("loader"))
        from ... import loaders
        try:
            return bool(loaders.detect_server(inst.install_dir()).get("loader"))
        except (ServerError, OSError):
            return False

    def command(self, inst, doc, ctx, vals):
        """Java and the launch line loaders.detect_server reads from the folder, as ServerManager starts it (cmd = [java,
        *launch], in the server's folder). The java here is the one on the PATH: only a harness run comes this way, and the
        harness puts its fake in Java's place."""
        from ... import loaders
        info = loaders.detect_server(inst.install_dir())
        if not info.get("loader"):
            raise ServerError("No server runtime found in the folder (NeoForge, Forge, Fabric, Quilt or a server jar).")
        return ["java", *info["launch"]], dict(os.environ), inst.install_dir()

    def install(self, inst, progress=None, validate=False, loader=None, **kw):
        """The loader into a harness run's own folder: loader(folder, loader, loader version, Minecraft version, progress),
        as core/loaders.py's install_loader, the harness's fake in its place. A world is made with New world, as in 2.x."""
        if isinstance(inst, World) or loader is None:
            raise ServerError("A Minecraft world is made with New world on the Servers page.")
        doc, vals = inst.doc(), self.settings(inst)
        dest = inst.install_dir()
        with inst.lock:
            dest.mkdir(parents=True, exist_ok=True)
            if not self.installed(inst, doc):
                loader(dest, doc["minecraft"]["loader"], vals.get("loaderVersion") or None, vals.get("mcVersion") or None, progress)
            if not self.installed(inst, doc):
                raise ServerError("The loader's installer left no server Fenrir can start.")
            d = inst.load()
            d["installedAt"] = __import__("time").time()
            inst.save(d)
        return {"ok": True, "message": f"The {doc['name']} server is ready to start"}

    def update(self, inst, progress=None, **kw):
        raise ServerError("A world's loader is updated with its pack, on the Server Pack page.")


register("minecraft", MinecraftDriver)
