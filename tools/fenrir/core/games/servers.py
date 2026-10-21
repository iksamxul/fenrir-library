"""Dedicated servers for modded games (1.40): installed with SteamCMD (or taken from the game's own folder or a mod), run
as a process with a console of its own whose output goes to a log file, so Fenrir can restart without stopping it and
takes it over again afterwards, like the Minecraft server. Stopped with Ctrl+C first (core/games/consolectl.py), so the
world is saved, and ended only when it does not stop in time. Players counted with Steam's A2S query where the game
answers it. Ports opened on the router with UPnP when the host asks and the router allows.

    DATA/games/servers/<id>/server.json   the settings, the process Fenrir started, the router mappings it made
    DATA/games/servers/<id>/install/       the server's files (SteamCMD's force_install_dir), unless the host picked a folder
    DATA/games/servers/<id>/saves/         its worlds, where the game lets Fenrir choose
    DATA/games/servers/<id>/server.log     its output
"""
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .. import oslayer
from ..util import DATA
from gamekit import a2s

HOME = DATA / "games" / "servers"
CREATE_NO_WINDOW = 0x08000000
MARK = "--- started by Fenrir"
_CODES = {}  # (server, pid, start time) -> the join code that run printed
LOG_ROLL = 16 * 1024 * 1024  # a log past this is kept once as server.old.log at the next start: it never grows without end (2.3)
# how a server looks after itself (2.3, core/games/keeper.py): what the host set under Server settings, these where nothing is
CARE = {"restartOnCrash": True, "comeBack": True, "dailyRestart": "", "backupHours": 0, "emptyStopMin": 0}
BACKUP_HOURS, EMPTY_MIN = (0, 1, 3, 6, 12, 24), (0, 15, 30, 60, 120)
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# a line of a server's log that says what went wrong, and lines that only look like it
ERROR_LINE = re.compile(r"(error|exception)\b|\b(fatal|crash(ed)?|failed|unhandled|access violation|segmentation fault|out of memory)\b", re.I)  # NullReferenceException too
NOT_ERROR = re.compile(r"\b(0 errors?|no errors?|errors?: ?0|without errors?|error ?reporting|crash ?reporter|failed to load steam ?stats)\b", re.I)


def last_error(lines):
    """The last line of a run's log that says what went wrong (or its last line when none does), trimmed for the page."""
    lines = [str(x).strip() for x in lines or [] if str(x).strip() and not str(x).startswith(MARK)]
    for line in reversed(lines[-400:]):
        if ERROR_LINE.search(line) and not NOT_ERROR.search(line):
            return line[:300]
    return lines[-1][:300] if lines else ""


def _roll(log):
    try:
        if log.stat().st_size > LOG_ROLL:
            os.replace(log, log.with_name(log.stem + ".old" + log.suffix))
    except OSError:
        pass

RECIPES_29 = {  # 2.9's own; since 3.0 the built-in recipes say these (RECIPES, below), and these stand in for one that does not load
    "valheim": {
        "name": "Valheim", "client": ["892970"], "app": 896660, "exe": "valheim_server.exe", "env": {"SteamAppId": "892970"},
        "args": ["-nographics", "-batchmode", "-name", "{name}", "-port", "{port}", "-world", "{world}", "-password", "{password}",
                 "-public", "{public}", "-savedir", "{saves}"],
        "flags": {"crossplay": ["-crossplay"]},  # a join code through Valheim's own relay: no ports, no VPN for friends
        "defaults": {"name": "Fenrir", "world": "Fenrir", "port": 2456, "public": 0, "crossplay": 1, "password": ""},
        "ports": [["{port}", "udp"], ["{port+1}", "udp"]], "query": "{port+1}", "password": {"min": 5},
        "mods": "thunderstore", "stopWait": 60, "joinCode": r"with join code (\d{6})", "connectArgs": ["+connect", "{address}:{port}"],
        "playersLog": r"is active with (\d+) player",  # crossplay's session line: the count when Steam's query is off (-public 0)
        "join": "In Valheim: Join game, Add server, {address}:{port}.",
        "joinWithCode": "In Valheim: Join game, Add server, the join code {code}.",
    },
    "palworld": {
        "name": "Palworld", "client": ["1623730"], "app": 2394010, "exe": "PalServer.exe",
        "realExes": ["Pal/Binaries/Win64/PalServer-Win64-Shipping-Cmd.exe", "Pal/Binaries/Win64/PalServer-Win64-Test-Cmd.exe"],  # what PalServer.exe starts
        "args": ["-port={port}", "-players={players}"], "defaults": {"port": 8211, "players": 16},
        "ports": [["{port}", "udp"]], "stopWait": 45,
        "join": "In Palworld: Join Multiplayer Game, then {address}:{port}.",
    },
    "sevendays": {
        "name": "7 Days to Die", "client": ["251570"], "app": 294420, "exe": "7DaysToDieServer.exe", "env": {"SteamAppId": "251570"}, "mods": "files",
        "args": ["-logfile", "{log}", "-quit", "-batchmode", "-nographics", "-configfile={config}", "-dedicated"],
        "config": {"file": "fenrir-serverconfig.xml", "from": "serverconfig.xml", "set": {"ServerName": "{name}", "ServerPort": "{port}",
                                                                                         "ServerPassword": "{password}", "ServerMaxPlayerCount": "{players}",
                                                                                         "UserDataFolder": "{saves}"}},
        "defaults": {"name": "Fenrir", "port": 26900, "players": 8, "password": ""},
        "ports": [["{port}", "tcp"], ["{port}", "udp"], ["{port+1}", "udp"], ["{port+2}", "udp"], ["{port+3}", "udp"]], "query": "{port}",
        "stopWait": 60, "join": "In 7 Days to Die: Join a Game, Connect to IP, {address}:{port}.",
    },
    "terraria": {
        "name": "Terraria", "client": ["105600"], "fromGame": "TerrariaServer.exe",
        "args": ["-port", "{port}", "-players", "{players}", "-autocreate", "2", "-worldname", "{world}", "-world", "{saves}/{world}.wld",
                 "-password", "{password}"],
        "defaults": {"port": 7777, "players": 8, "world": "Fenrir", "password": ""},
        "ports": [["{port}", "tcp"]], "stopWait": 30,
        "join": "In Terraria: Multiplayer, Join via IP, {address}, port {port}.",
    },
}


def from_recipes(old):
    """2.x's dicts as the built-in recipes say them (3.0, REC-3: fenrir/recipes/builtin, core/recipes/view2x.py), so 2.x's
    code and the recipe driver read the same facts. A recipe that does not load leaves 2.9's own dict in its place."""
    try:
        from ..recipes import view2x
        found = view2x.recipes(old)
    except Exception:  # noqa: BLE001 - 2.9's own dicts stand in: the servers run either way
        found = {}
    return {k: found.get(k) or v for k, v in old.items()}


RECIPES = from_recipes(RECIPES_29)


class ServerError(Exception):
    """Said to the host as it is."""


SETTING_NAMES = {"port": "The port", "players": "Players", "public": "Listed publicly", "crossplay": "Join code (crossplay)", "name": "The server's name",
                 "world": "The world's name", "password": "The password", "key": "The FiveM server key"}
WITH_PASSWORD = " Then type the server's password."


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return k


def proc_created(pid):
    """When a process started (unix seconds), from the system itself (core/oslayer: Windows' creation time, Linux's start
    time after its boot); None when that cannot be read."""
    return oslayer.started(pid)


def end_tree(pid):
    """The server's process and every process it started (taskkill /T on Windows, its process group on Linux): a launcher
    that started the real server leaves nothing behind holding the port and the save."""
    return oslayer.end_tree(pid)


def fill(template, values):
    """"{port+1}" and "{name}" from the server's settings; anything else stays as written."""
    def one(m):
        key, plus = m.group(1), m.group(2)
        v = values.get(key, "")
        if plus:
            v = int(v or 0) + int(plus)
        return str(v)
    return re.sub(r"\{(\w+)(?:\+(\d+))?\}", one, str(template))


class GameServer:
    def __init__(self, sid, recipe=None, home=None):
        if sid not in RECIPES and recipe is None:
            raise ServerError("Fenrir does not host that game's server.")
        self.id, self.recipe = sid, recipe or RECIPES[sid]
        self.dir = Path(home or HOME) / sid
        self.state_path = self.dir / "server.json"
        self.log = self.dir / "server.log"
        self.lock = threading.RLock()

    # ---- settings and state
    def load(self):
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("settings", dict(self.recipe.get("defaults") or {}))
        return d

    def save(self, d):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"server.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    def programs(self, game_dir=None):
        """Every program of this server that takes friends' connections: the one Fenrir starts, and the real server a
        launcher starts in turn (Palworld's PalServer.exe starts PalServer-Win64-Shipping-Cmd.exe; 2.8, the bug hunt: the
        firewall rule named only the launcher, so friends were still blocked)."""
        out = [self.exe(game_dir)]
        out += [self.install_dir() / rel for rel in self.recipe.get("realExes") or [] if (self.install_dir() / rel).is_file()]
        return out

    def install_dir(self):
        return Path(self.load().get("installDir") or (self.dir / "install"))

    def exe(self, game_dir=None):
        if self.recipe.get("fromGame"):
            if not game_dir:
                raise ServerError(f"{self.recipe['name']}'s server comes with the game: install the game first.")
            return Path(game_dir) / self.recipe["fromGame"]
        return self.install_dir() / self.recipe["exe"]

    def settings(self):
        return {**(self.recipe.get("defaults") or {}), **self.load()["settings"]}

    def care(self):
        """How it looks after itself (2.3): started again after a crash, back after Windows restarts, a daily restart, timed
        backups, stopped when empty. The defaults for what the host never set."""
        c = self.load().get("care") or {}
        return {k: (c[k] if k in c and type(c[k]) is type(v) else v) for k, v in CARE.items()}

    def set_care(self, **changes):
        """Only the known ones, each checked: a time like 05:00, how often from the list."""
        out = {}
        for k, v in changes.items():
            if k in ("restartOnCrash", "comeBack"):
                out[k] = bool(v)
            elif k == "dailyRestart":
                v = str(v or "").strip()
                if v and not CLOCK.match(v):
                    raise ServerError("Write the daily restart as a time, like 05:00, or leave it empty.")
                out[k] = v
            elif k in ("backupHours", "emptyStopMin"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = -1
                if v not in (BACKUP_HOURS if k == "backupHours" else EMPTY_MIN):
                    raise ServerError("Pick one of the choices in the list.")
                out[k] = v
        with self.lock:
            d = self.load()
            d["care"] = {**self.care(), **out}
            self.save(d)
        return self.care()

    def can_count(self):
        """It can say how many are on (Steam's query, or its log): a stop for being empty is only ever asked of one that can."""
        return bool(self.recipe.get("query") or self.recipe.get("playersLog"))

    def set(self, **changes):
        """Only the recipe's own settings, each as the kind its default is (a port stays a number, a switch 0 or 1)."""
        defaults = self.recipe.get("defaults") or {}
        with self.lock:
            d = self.load()
            for k, v in changes.items():
                if k not in defaults:
                    continue
                if isinstance(defaults[k], int):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        raise ServerError(f"{SETTING_NAMES.get(k, k)} has to be a number.") from None
                else:
                    v = str(v if v is not None else "").replace("\n", " ").strip()[:120]
                d["settings"][k] = v
            self.save(d)
        return self.settings()

    def validate(self, changes):
        """What a Save would make wrong, said before anything is kept: a port outside 1024 to 65000, a number that is not one,
        a password too short or inside the server's name (Valheim refuses both). A setting not given is not judged, so a
        name can be saved before there is a password."""
        defaults = self.recipe.get("defaults") or {}
        for k, v in changes.items():
            if k in defaults and isinstance(defaults[k], int):
                try:
                    int(v)
                except (TypeError, ValueError):
                    raise ServerError(f"{SETTING_NAMES.get(k, k)} has to be a number.") from None
        if "port" in changes and not 1024 <= int(changes["port"]) <= 65000:
            raise ServerError("Pick a port between 1024 and 65000.")
        if "players" in changes and int(changes["players"]) < 1:
            raise ServerError("Players has to be at least 1.")
        pw = self.recipe.get("password") or {}
        s = {**self.settings(), **changes}
        p = str(s.get("password") or "")
        if pw.get("min") and changes.get("password") and len(str(changes["password"])) < pw["min"]:
            raise ServerError(f"The server's password needs at least {pw['min']} characters.")
        if pw and ("password" in changes or "name" in changes) and p and p.lower() in str(s.get("name") or "").lower():
            raise ServerError("The password may not be part of the server's name (Valheim refuses it).")

    def check_settings(self, s):
        pw = self.recipe.get("password")
        if pw:
            p = str(s.get("password") or "")
            if len(p) < pw.get("min", 0):
                raise ServerError(f"The server's password needs at least {pw['min']} characters.")
            if p and p.lower() in str(s.get("name") or "").lower():
                raise ServerError("The password may not be part of the server's name (Valheim refuses it).")
        try:
            port = int(s.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 1024 <= port <= 65000:
            raise ServerError("Pick a port between 1024 and 65000.")

    # ---- install
    def install(self, progress=None, validate=False):
        if self.recipe.get("fromGame"):
            return {"ok": True, "message": "This server comes with the game itself."}
        from . import steamcmd
        steamcmd.install(self.recipe["app"], self.install_dir(), validate=validate, progress=progress)
        d = self.load()
        d["installedAt"] = time.time()
        self.save(d)
        return {"ok": True, "message": f"The {self.recipe['name']} server is installed"}

    def installed(self, game_dir=None):
        try:
            return self.exe(game_dir).exists()
        except ServerError:
            return False

    # ---- running
    def values(self, game_dir=None):
        s = self.settings()
        saves = self.dir / "saves"
        values = {**s, "saves": str(saves), "log": str(self.dir / "game-output.log"), "config": str(self.install_dir() / (self.recipe.get("config") or {}).get("file", ""))}
        return values

    def write_config(self, values):
        conf = self.recipe.get("config")
        if not conf:
            return
        src, dst = self.install_dir() / conf["from"], self.install_dir() / conf["file"]
        try:
            text = src.read_text("utf-8")
        except OSError as e:
            raise ServerError(f"The server's own {conf['from']} is missing; install the server again.") from e
        for key, tmpl in conf["set"].items():
            v = fill(tmpl, values).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            text, n = re.subn(rf'(<property\s+name="{re.escape(key)}"\s+value=")[^"]*(")', lambda m: m.group(1) + v + m.group(2), text)
            if not n:
                text = text.replace("</ServerSettings>", f'\t<property name="{key}" value="{v}"/>\n</ServerSettings>')
        dst.write_text(text, "utf-8")

    def command(self, game_dir=None):
        values = self.values(game_dir)
        args = [fill(a, values) for a in self.recipe.get("args") or []]
        if self.recipe.get("password") is None and "-password" in args:  # an empty password means no password
            i = args.index("-password")
            if not args[i + 1]:
                del args[i:i + 2]
        for key, extra in (self.recipe.get("flags") or {}).items():  # switches that are there only when on
            if str(values.get(key) or "0") not in ("0", "", "False", "false"):
                args += [fill(a, values) for a in extra]
        return [str(self.exe(game_dir)), *args]

    def pid(self):
        return int(self.load().get("pid") or 0)

    def running(self, game_dir=None):
        """The process Fenrir started for this server is alive: the same process number and the same start time, read from
        Windows each time (it reuses numbers: after a crash or a restart of the PC a number may belong to a browser). What
        cannot be told counts as not running, so Stop never touches a process that is not the server's."""
        alive = oslayer.alive
        d = self.load()
        pid, born = int(d.get("pid") or 0), d.get("created")
        if not pid or born is None or not alive(pid):
            return False
        now_born = proc_created(pid)
        return now_born is not None and abs(now_born - float(born)) < 2

    def start(self, game_dir=None, popen=None):
        with self.lock:
            if self.running(game_dir):
                return {"ok": True, "message": "It is running already"}
            if not self.installed(game_dir):
                raise ServerError("Install the server first.")
            s = self.settings()
            self.check_settings(s)
            values = self.values(game_dir)
            Path(values["saves"]).mkdir(parents=True, exist_ok=True)
            self.write_config(values)
            oslayer.prepare_children()  # on Windows children inherit "Ctrl+C works": the clean stop depends on it
            env = {**os.environ, **{k: fill(v, values) for k, v in (self.recipe.get("env") or {}).items()}}
            self.dir.mkdir(parents=True, exist_ok=True)
            _roll(self.log)
            log = open(self.log, "ab")
            log.write(f"{MARK} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())  # what this run said starts here
            log.flush()
            try:
                p = (popen or subprocess.Popen)(self.command(game_dir), cwd=str(self.exe(game_dir).parent), env=env, stdout=log, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, **oslayer.spawn_kwargs())
            finally:
                log.close()
            d = self.load()
            d.update(pid=p.pid, startedAt=time.time(), created=proc_created(p.pid), aliveAt=time.time())
            for k in ("ended", "held"):  # a run of its own now: how the last one ended, and a crash loop's pause, are over
                d.pop(k, None)
            self.save(d)
            return {"ok": True, "pid": p.pid}

    def stop(self, wait=None, ctrl_c=None, end=None, sleep=time.sleep):
        """Ctrl+C (the world is saved), then wait; ended only when it does not stop in time."""
        with self.lock:
            pid = self.pid()
            if not pid or not self.running():
                d = self.load()
                for k in ("pid", "ended", "held"):  # stopped by the host: not "stopped by itself" any more
                    d.pop(k, None)
                self.save(d)
                return {"ok": True, "message": "It was not running"}
            (ctrl_c or oslayer.interrupt)(pid)  # Ctrl+C on Windows, SIGINT to its group on Linux
            limit = time.time() + (wait if wait is not None else self.recipe.get("stopWait", 45))
            while time.time() < limit and self.running():
                sleep(0.5)
            forced = False
            if self.running():  # still this server (number and start time): its whole tree goes (Palworld's launcher starts the real server)
                (end or end_tree)(pid)
                forced = True
                from .. import forcedstops
                forcedstops.note(self.kind, self.recipe.get("name"), wait if wait is not None else self.recipe.get("stopWait", 45))
            d = self.load()
            for k in ("pid", "ended", "held"):
                d.pop(k, None)
            d["stoppedAt"] = time.time()
            self.save(d)
            return {"ok": True, "forced": forced, "message": "Stopped" if not forced else "It did not stop by itself in time, so Fenrir ended it"}

    # ---- what the page shows
    def output(self):
        """Where the server's own lines are: its output (server.log), or the file its arguments tell it to write instead
        (7 Days to Die's -logfile; 2.8, the bug hunt: its Console stayed empty). Such a file starts anew with each run."""
        return self.dir / "game-output.log" if "{log}" in (self.recipe.get("args") or []) else self.log

    def tail(self, n=200):
        try:
            with open(self.output(), "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        return data.splitlines()[-n:]

    def players(self):
        """Who is on: Steam's query where the server answers it, else the count the server writes in its log (Valheim with
        crossplay); None when it cannot be told (then a stop from the phone asks first, as if friends were on)."""
        if not self.running():
            return None
        q = self.recipe.get("query")
        port = int(fill(q, self.settings())) if q else None
        info = a2s.query("127.0.0.1", port) if q else None
        if info:
            out = {"players": info["players"], "max": info["max"], "name": info["name"]}
            if info["players"]:  # who, where the server says it (2.5): one more round trip, only while someone is on
                from . import a2splayers
                who = a2splayers.players("127.0.0.1", port)
                if who:
                    out["names"] = [p["name"] for p in who][:32]
            return out
        rx = self.recipe.get("playersLog")
        for line in reversed(self.this_run(400)) if rx else []:
            m = re.search(rx, line)
            if m:
                return {"players": int(m.group(1)), "max": None, "name": None}
        return None

    def this_run(self, n=400):
        """The log lines of the run going now (after Fenrir's start mark)."""
        lines = self.tail(n)
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(MARK):
                return lines[i + 1:]
        return lines

    def join_code(self):
        """Valheim's crossplay join code, as the running server printed it; kept once seen for that run."""
        rx = self.recipe.get("joinCode")
        if not rx or not self.running():
            return None
        d = self.load()
        key = (self.id, d.get("pid"), d.get("created")) if d.get("pid") and d.get("created") is not None else None  # a run known by both
        if key in _CODES:
            return _CODES[key]
        for line in reversed(self.this_run()):
            m = re.search(rx, line)
            if m:
                if key:
                    _CODES[key] = m.group(1)
                return m.group(1)
        return None

    def status(self, game_dir=None):
        d = self.load()
        run = self.running(game_dir)
        s = self.settings()
        code = self.join_code() if run else None
        join = fill(self.recipe.get("joinWithCode") or "", {**s, "code": code}) if code else fill(self.recipe.get("join") or "", {**s, "address": "{address}"})
        if join and s.get("password"):
            join += WITH_PASSWORD
        return {"id": self.id, "name": self.recipe["name"], "installed": self.installed(game_dir), "running": run,
                "startedAt": d.get("startedAt") if run else None, "settings": {k: v for k, v in s.items() if k != "password"},
                "passwordSet": bool(s.get("password")), "passwordNeeded": bool((self.recipe.get("password") or {}).get("min")),
                "ports": [[int(fill(p, s)), proto] for p, proto in self.recipe.get("ports") or []],
                "join": join, "joinCode": code, "players": self.players() if run else None, "log": self.this_run(40) if run else self.tail(12),
                "stoppedItself": (bool(d.get("pid")) or bool(d.get("ended"))) and not run,  # Fenrir's own Stop forgets the process: one gone ended by itself
                "ended": None if run else d.get("ended"), "held": None if run else d.get("held"), "care": self.care(), "canCount": self.can_count(),
                "crashesWeek": sum(1 for x in d.get("crashTimes") or [] if time.time() - float(x) < 7 * 86400)}


class SkyrimTogether(GameServer):
    """Skyrim Together Reborn's own server. It comes with the Skyrim Together kit (Before you start): its folder is linked
    into this server's own (no second copy on disk), where the server keeps config\\STServer.ini, which Fenrir writes
    while it is stopped ([GameServer] uPort, sServerName, sPassword, uMaxPlayerCount; [LiveServices] bAnnounceServer)."""
    RECIPE_29 = {"name": "Skyrim Together", "client": ["489830"], "exe": "SkyrimTogetherServer.exe", "args": [],
                 "defaults": {"name": "Fenrir", "port": 10578, "players": 8, "password": ""}, "ports": [["{port}", "udp"], ["{port}", "tcp"]],
                 "stopWait": 20, "kit": "skyrimtogether",
                 "join": "In Skyrim Together: open its menu (Right Ctrl), Connect, then {address}:{port}."}
    RECIPE = from_recipes({"skyrimtogether": RECIPE_29})["skyrimtogether"]

    def __init__(self, home=None, kit_dir=None):
        super().__init__("skyrimtogether", recipe=self.RECIPE, home=home)
        self.kit_dir = Path(kit_dir) if kit_dir else None

    def _kit_server(self):
        return (self.kit_dir or Path("-")) / "Data" / "SkyrimTogetherReborn"

    def install(self, progress=None, validate=False):
        """A copy of the kit's server folder (a copy, not links: a running server must never lock the kit, and the kit's
        next version must never change a running server's files)."""
        import shutil
        src = self._kit_server()
        if not (src / "SkyrimTogetherServer.exe").is_file():
            raise ServerError("Get Skyrim Together Reborn first (Before you start): its server comes with it.")
        if self.running():
            raise ServerError("Stop the Skyrim Together server first.")
        dst = self.install_dir()
        for p in src.rglob("*"):
            if p.is_file() and p.relative_to(src).parts[0].lower() != "config":  # the server's own settings stay its own
                t = dst / p.relative_to(src)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, t)
        d = self.load()
        d["installedAt"] = time.time()
        self.save(d)
        return {"ok": True, "message": "The Skyrim Together server is ready"}

    def start(self, game_dir=None, popen=None):
        """After a new Skyrim Together kit, the server's copy is refreshed first: client and server must be one version."""
        kit, here = self._kit_server() / "SkyrimTogetherServer.exe", self.install_dir() / "SkyrimTogetherServer.exe"
        try:
            stale = kit.is_file() and (not here.is_file() or kit.stat().st_size != here.stat().st_size or abs(kit.stat().st_mtime - here.stat().st_mtime) > 2)
        except OSError:
            stale = False
        if stale and not self.running():
            self.install()
        return super().start(game_dir, popen)

    def write_config(self, values):
        import configparser
        ini = self.install_dir() / "config" / "STServer.ini"
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # the server's keys keep their case (uPort, sServerName)
        try:
            cp.read(ini, encoding="utf-8")
        except configparser.Error:
            cp = configparser.ConfigParser(interpolation=None)
            cp.optionxform = str
        for section in ("GameServer", "LiveServices"):
            if not cp.has_section(section):
                cp.add_section(section)
        clean = lambda v: str(v).replace("\n", " ").replace("\r", " ")[:64]  # noqa: E731
        cp.set("GameServer", "uPort", str(int(values["port"])))
        cp.set("GameServer", "sServerName", clean(values.get("name") or "Fenrir"))
        cp.set("GameServer", "sPassword", clean(values.get("password") or ""))
        cp.set("GameServer", "uMaxPlayerCount", str(max(1, min(16, int(values.get("players") or 8)))))
        cp.set("LiveServices", "bAnnounceServer", "false")  # friends only: never on the public list
        ini.parent.mkdir(parents=True, exist_ok=True)
        with open(ini, "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=False)
