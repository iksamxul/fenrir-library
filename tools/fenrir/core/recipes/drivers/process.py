"""The process driver (recipes.md section 3.1): game, app, database and your own servers, each a program Fenrir starts and
looks after. It is 2.x's GameServer grown up (core/games/servers.py) and keeps what years of fixes taught it:

- a start mark in the log before each run, so a run's own lines are told apart;
- the pid with the process's creation time, so a number Windows has given to another program is never taken for the
  server;
- a hidden console of the server's own (CREATE_NO_WINDOW, never a new process group), so Ctrl+C reaches it after a
  Fenrir restart too (core/games/consolectl.py); on Linux a session of its own, so the whole group can be signalled;
- the whole tree ended when it does not stop in time, so no launcher's child keeps the port or the world;
- the log kept once as server.old.log past 16 MB.

On top of that, everything the recipe says: its settings and writers, ready and health, players, stop steps, console.

Secrets are opened from the sealed store only to start (and for RCON): the environment, the files and, with inArgs, the
command line get them, and nothing keeps them after. A console line goes in through the server's input pipe while this
Fenrir runs (console stdin), or as key presses into its own console (console keys, your own server's cmd), which still
works after Fenrir restarts. Nothing here is a route: the API, the keeper and Fenrir Link call it."""
import collections
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
from pathlib import Path

from ... import store
from ...games import servers
from ...games.servers import ServerError
from .. import load, policy, probes, values, writers
from .. import players as player_sources
from . import Driver, SettingRefused, UseButton

CREATE_NO_WINDOW = 0x08000000
READY_EVERY = 2.0  # seconds between ready checks while a server starts: a page polls more often than that
GRACE = 45  # stop.grace's default (2.x stopWait)
READY_TIMEOUT = 300  # a recipe that says how it is ready but not how long it may take


def service_steps(steps, console):
    """A recipe's stop steps as Fenrir Server runs them: the server's own way to stop (a line on its stdin, RCON, its web
    API) wherever the recipe has one, and Ctrl+C, or a line typed into its console, only where nothing else stops it.
    Both of those reach the server's console from a helper process, and whether that works from the service's session 0
    was never checked on a real PC; a stop that does not reach the server ends it by force when its grace runs out
    (core/forcedstops.py), the same grace as ever."""
    def own(s):
        return s.get("do") in ("rcon", "rest", "close") or (s.get("do") == "command" and console == "stdin")
    if not any(own(s) for s in steps):
        return list(steps)
    return [s for s in steps if s.get("do") != "ctrl-c" and not (s.get("do") == "command" and console != "stdin")]


CMD_OWN = '&|<>^%"\r\n\x00'  # what cmd.exe would read as its own: never in an argument that goes through it
RESOLVERS = {}  # "fivem-page" -> fn(doc, platform, page=None) -> [{"url", "sha256"?, "size"?, "unpack"?, "strip"?}]; built-in hooks register (REC-3)
_pipes = {}  # (server id, pid) -> the Popen whose stdin this Fenrir holds (console stdin)
_typed = collections.defaultdict(lambda: collections.deque(maxlen=20))  # server id -> [(when, line)] typed on its Console page
_checked = {}  # (server id, pid) -> when its ready probes last ran


def _alive(pid):
    """It runs, as the system says (core/oslayer; a zombie on Linux has ended)."""
    from ... import oslayer
    return oslayer.alive(pid)


def _born(pid):
    """When the process started, unix seconds, as the system says (core/oslayer): Windows' creation time, or on Linux the
    boot's time plus its start after the boot."""
    return servers.proc_created(pid)


def _end(pid):
    """The process and every one it started."""
    return servers.end_tree(pid)


class ProcessDriver(Driver):
    kinds = ("game", "app", "database", "own")

    # ---------------------------------------------------------------------------------------------------- settings
    def settings(self, inst, open_secrets=False):
        """Every setting's value for this server. Secret ones are opened from the sealed store only when asked
        (server.<id>.setting.<key>); a value 2.x kept in server.json is used until REC-3 moves it into the store."""
        doc, stored = inst.doc(), inst.load().get("settings") or {}
        out = values.current(doc, stored)
        if open_secrets:
            for k, f in values.fields(doc).items():
                if f["type"] == "secret":
                    v = store.get(f"server.{inst.id}.setting.{k}")
                    out[k] = v if v is not None else stored[k] if isinstance(stored.get(k), str) else ""
        return out

    def set(self, inst, changes):
        """The settings form's Save: every change checked before anything is kept (values.check). A secret goes into the
        sealed store and server.json keeps {"secret": true}; an empty secret, or {"keep": true}, keeps the one already
        set, and {"clear": true} removes it. Files written at save (when: save) are written while the server is stopped.
        Returns whether a restart is needed for the changes to take effect."""
        doc = inst.doc()
        fl = values.fields(doc)
        plain, secrets_, clear = {}, {}, set()
        for k, v in (changes or {}).items():
            if k not in fl:
                continue
            if fl[k]["type"] == "secret":
                if isinstance(v, dict):
                    if v.get("clear"):
                        clear.add(k)
                    elif isinstance(v.get("set"), str) and v["set"]:
                        secrets_[k] = v["set"]
                elif isinstance(v, str) and v:
                    secrets_[k] = v
            else:
                plain[k] = v
        with inst.lock:
            try:
                kept = values.check(doc, {**plain, **secrets_}, self.settings(inst, open_secrets=True))
            except values.SettingError as e:  # said to the host as it is, as every other refusal of a server's
                raise SettingRefused(str(e), e.key) from None
            d = inst.load()
            for k, v in kept.items():
                if k in secrets_:
                    store.put(f"server.{inst.id}.setting.{k}", v, kind="password", label=f"{doc['name']}: {fl[k]['label']}", used_by=[doc["name"]])
                    d["settings"][k] = {"secret": True}
                else:
                    d["settings"][k] = v
            for k in clear:
                store.delete(f"server.{inst.id}.setting.{k}")
                d["settings"].pop(k, None)
            inst.save(d)
            running = self.running(inst)
            if not running and any(w.get("when") == "save" for w in doc.get("files") or []):
                self.configure(inst, "save")
        changed = set(kept) | clear
        return {"ok": True, "restart": running and any(fl[k].get("restart", True) for k in changed)}

    # ---------------------------------------------------------------------------------------------------- folders and values
    def program(self, inst, doc=None):
        """The program Fenrir starts: inside the server's program folder (the game's own for a server that comes with
        it), or the one the host picked for your own server."""
        doc = doc or inst.doc()
        prog = (doc.get("run") or {}).get("program") or ""
        if doc["kind"] == "own" and Path(prog).is_absolute():
            return Path(prog)
        base = inst.install_dir()
        ctx = {"install": str(base), "data": str(inst.dir), "game": str(inst.game_dir or base)}
        if not prog.startswith("{"):
            prog = "{install}/" + prog
        values.real_path(prog, ctx, ("install", "data", "game"))  # checked by its real place, started by its plain path (2.x's)
        return Path(values.fill(prog, ctx))

    def installed(self, inst, doc=None):
        try:
            return self.program(inst, doc).is_file()
        except ServerError:
            return False

    def context(self, inst, doc, vals, secrets=True, **extra):
        """What the recipe's templates are filled from for this server: its settings, ports and folders, and its sealed
        values (only when secrets is true)."""
        try:
            install = inst.install_dir()
        except ServerError:  # a server that comes with the game, the game not here: what needs no {install} still works (its saves)
            install = None
        parts = {"install": install, "data": inst.dir, "game": inst.game_dir, "profile": getattr(inst, "profile", None) or Path.home()}
        base = values.context(doc, vals, **parts)
        saves = (doc.get("saves") or {}).get("path")
        if saves:
            if doc["kind"] == "own" and Path(saves).is_absolute():
                saves = Path(saves)  # picked by the host in Fenrir's own dialog (2.9 custom.py)
            else:  # checked by its real place; the templates get the plain path, as 2.x's str(dir / "saves") was
                values.real_path(saves, base, ("data", "install", "profile"))
                saves = Path(values.fill(saves, base))
            said = values.saves_problem(saves, inst.dir, inst.dir.parent)
            if said:
                raise ServerError(said)
        out = (doc.get("run") or {}).get("output")
        log = None
        if isinstance(out, dict):
            values.real_path(out["file"], {**base, "saves": str(saves or "")}, ("data", "install"))
            log = Path(values.fill(out["file"], {**base, "saves": str(saves or "")}))
        secret = (lambda name: store.get(f"server.{inst.id}.{name}")) if secrets else None
        return values.context(doc, vals, secret=secret, **parts, saves=saves, log=log, **extra)

    def command(self, inst, doc, ctx, vals):
        """(argv, environment, working folder) for one start. No shell reads the argv."""
        run = doc.get("run") or {}
        argv = [str(self.program(inst, doc)), *values.args(run.get("args"), ctx, doc, vals)]
        env = {**os.environ, **{k: values.fill(t, ctx) for k, t in (run.get("env") or {}).items()}}
        if run.get("portEnv") and ctx.get("port"):
            env[run["portEnv"]] = next(iter(ctx["port"].values()))
        if run.get("cwd") and Path(run["cwd"]).is_absolute() and doc["kind"] == "own":
            cwd = Path(run["cwd"])
        elif run.get("cwd"):  # checked by its real place, given by its plain path (2.x's)
            values.real_path(run["cwd"], ctx, ("install", "data", "saves", "game"))
            cwd = Path(values.fill(run["cwd"], ctx))
        else:
            cwd = Path(argv[0]).parent
        return argv, env, cwd

    def output(self, inst, doc=None):
        """Where the server's own lines are: the file its arguments tell it to write (7 Days to Die's -logfile), or its
        output, which goes to server.log."""
        doc = doc or inst.doc()
        out = (doc.get("run") or {}).get("output")
        if isinstance(out, dict):
            try:
                return Path(self.context(inst, doc, self.settings(inst), secrets=False)["log"])
            except ServerError:
                return inst.log
        return inst.log

    def tail(self, inst, n=200, doc=None, start=None, limit=1024 * 1024):
        """The last n lines of its output (at most the last 1 MB), or those after a byte offset."""
        try:
            with open(self.output(inst, doc), "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                begin = max(0, size - limit) if start is None or start > size else max(int(start), size - limit)
                f.seek(begin)
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        return data.splitlines()[-n:]

    def this_run(self, inst, doc=None, n=400):
        """The lines of the run going now: after Fenrir's start mark in server.log, or after where the server's own log
        file stood when this run started (all of it when it starts anew each run)."""
        doc = doc or inst.doc()
        out = (doc.get("run") or {}).get("output")
        if isinstance(out, dict):
            return self.tail(inst, n, doc, start=0 if out.get("perRun") else inst.load().get("logOffset") or 0)
        lines = self.tail(inst, n * 4, doc)
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(servers.MARK):
                return lines[i + 1:][-n:]
        return lines[-n:]

    # ---------------------------------------------------------------------------------------------------- the process
    def pid(self, inst):
        return int(inst.load().get("pid") or 0)

    def running(self, inst):
        """The process Fenrir started is alive: the same number and the same start time, read from the system each time.
        What cannot be told counts as not running, so Stop never touches a process that is not the server's."""
        d = inst.load()
        pid, born = int(d.get("pid") or 0), d.get("created")
        if not pid or born is None or not _alive(pid):
            return False
        now = _born(pid)
        return now is not None and abs(now - float(born)) < 2

    def install(self, inst, progress=None, validate=False, steam=None, fetch=None, run=None, page=None):
        """Gets the server the way its recipe says: SteamCMD anonymously, a download checked by its sha256, a kit's copy, or
        nothing (the game's own server, your own program); then its firstRun steps, once. steam and fetch stand in for
        SteamCMD and the download, run for a first-run step's program, and page for the page a resolver reads (as
        gamekit.net.get), in tests and the harness."""
        doc = inst.doc()
        get = doc["get"]
        with inst.lock:
            if self.running(inst):
                raise ServerError(f"Stop the {doc['name']} server first.")
            d = inst.load()
            d["installing"] = time.time()
            inst.save(d)
            done = False
            try:
                dest = inst.install_dir()
                if "steamcmd" in get:
                    from ...games import steamcmd
                    (steam or steamcmd.install)(get["steamcmd"]["app"], dest, validate=validate, beta=get["steamcmd"].get("beta"), progress=progress)
                elif "download" in get:
                    self._download(inst, doc, dest, progress, fetch, page)
                elif "kit" in get:
                    self._kit(inst, get["kit"], dest)
                self._first_run(inst, doc, run)
                done = True
            finally:
                d = inst.load()
                d.pop("installing", None)
                if done:  # an install that failed is not one: the page offers it again
                    d["installedAt"] = time.time()
                inst.save(d)
        return {"ok": True, "message": f"The {doc['name']} server is ready to start"}

    def _kit(self, inst, kit, dest):
        """A copy of the kit's server folder, never links: a running server must not lock the kit, and the kit's next
        version must not change a running server's files. The folders the recipe keeps (Skyrim Together's config) stay."""
        if not inst.kit_dir:
            raise ServerError("Get the kit first (Before you start): this server comes with it.")
        src = Path(inst.kit_dir) / kit["from"]
        if not src.is_dir():
            raise ServerError("The kit's server folder is not there: get the kit again.")
        keep = {k.lower() for k in kit.get("keep") or []}
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            if p.is_file() and rel.parts[0].lower() not in keep:
                t = dest / rel
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, t)

    def _download(self, inst, doc, dest, progress, fetch, page=None):
        from gamekit import archives, net
        dl = doc["get"]["download"]
        os_name, arch = inst.platform, load.here()[1]
        files = dl.get("files") or []
        if dl.get("resolver"):
            if dl["resolver"] not in RESOLVERS:
                raise ServerError("This recipe's download needs a part of Fenrir that is not here.")
            files = RESOLVERS[dl["resolver"]](doc, os_name, **({"page": page} if page else {}))
            for f in files:  # a resolver's answer is held to the recipe's own list of hosts
                if urllib.parse.urlsplit(f["url"]).hostname not in (dl.get("hosts") or []):
                    raise ServerError("The download's address is not one this recipe allows.")
        pick = next((f for f in files if f.get("platform") in (f"{os_name}-{arch}", None)), None) or next((f for f in files if f.get("platform") == "any"), None)
        if not pick:
            raise ServerError(f"This server has no download for {os_name} on {arch}.")
        work = Path(tempfile.mkdtemp(prefix="fenrir-get-", dir=inst.dir if inst.dir.is_dir() else None))
        try:
            name = Path(urllib.parse.urlsplit(pick["url"]).path).name or "download"
            got = (fetch or net.download)(pick["url"], work / name, size=pick.get("size"), sha256=pick.get("sha256"), progress=progress)
            got = Path(got or work / name)
            unpack = pick.get("unpack", "zip")
            dest.mkdir(parents=True, exist_ok=True)
            if unpack == "none":
                shutil.copy2(got, dest / (pick.get("rename") or name))
                return
            out = work / "unpacked"
            if unpack == "zip":
                archives.unpack_zip(got, out)
            else:
                with tarfile.open(got, "r:gz" if unpack == "tar.gz" else "r:xz") as t:
                    t.extractall(out, filter="data")  # no path outside, no links out, no devices
            root = out
            for _ in range(int(pick.get("strip") or 0)):
                kids = [k for k in root.iterdir()]
                if len(kids) != 1 or not kids[0].is_dir():
                    raise ServerError("The download is not laid out the way its recipe says.")
                root = kids[0]
            for p in root.rglob("*"):  # over the folder, as SteamCMD updates one: a world kept inside stays
                if p.is_file():
                    t = dest / p.relative_to(root)
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, t)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _first_run(self, inst, doc, run=None):
        """The steps a server needs once after install (initdb, an accepted EULA), each with its own time limit. A step's
        file (a password for initdb) exists only while the step runs."""
        d = inst.load()
        if d.get("firstRunDone") or not doc.get("firstRun"):
            return
        vals = self.settings(inst, open_secrets=True)
        ctx = self.context(inst, doc, vals)
        for st in doc["firstRun"]:
            prog = st["program"] if st["program"].startswith("{") else "{install}/" + st["program"]
            argv = [str(values.real_path(prog, ctx, ("install", "data"))), *values.args(st.get("args"), ctx, doc, vals)]
            once = None
            if st.get("writes"):
                once = values.real_path(st["writes"]["file"], ctx, ("data", "install"))
                writers.whole(once, values.fill(st["writes"]["text"], ctx).encode("utf-8"))
            try:
                with open(inst.log, "ab") as log:
                    log.write(f"--- {st.get('say') or 'first run'}\n".encode())
                    log.flush()
                    r = (run or subprocess.run)(argv, cwd=str(inst.install_dir()), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=st.get("timeout", 300), creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
                if r.returncode != 0:
                    raise ServerError(f"{st.get('say') or 'A first-run step'} did not work (it ended with {r.returncode}): see its log.")
            except subprocess.TimeoutExpired:
                raise ServerError(f"{st.get('say') or 'A first-run step'} took too long, so Fenrir stopped it.") from None
            except OSError as e:
                raise ServerError(f"{st.get('say') or 'A first-run step'} could not start ({e.strerror or e}).") from None
            finally:
                if once is not None:
                    once.unlink(missing_ok=True)
        d = inst.load()
        d["firstRunDone"] = time.time()
        inst.save(d)

    def configure(self, inst, when="start", ctx=None):
        """The recipe's writers for this moment: every start (when: start), or a Save while stopped (when: save). A
        server that comes with the game never has a file written in the game's own folder."""
        doc = inst.doc()
        ctx = dict(ctx or self.context(inst, doc, self.settings(inst, open_secrets=True)))
        if (doc.get("get") or {}).get("fromGame"):
            ctx.pop("install", None)  # {install} is the game's own folder there: read only
        return [writers.write(w, ctx) for w in doc.get("files") or [] if w.get("when", "start") == when]

    def start(self, inst, popen=None):
        """Starts it: checks, writers, then the program in a hidden console of its own with its output in server.log.
        Refused, before anything runs, when a setting is missing or the command line would turn the anti-cheat on."""
        with inst.lock:
            if self.running(inst):
                return {"ok": True, "message": "It is running already"}
            doc = inst.doc()
            inst.install_dir()  # a server that comes with the game says so first: install the game
            if not self.installed(inst, doc):
                raise ServerError("Install the server first.")
            vals = self.settings(inst, open_secrets=True)
            said = values.start_problem(doc, vals)
            if said:
                raise ServerError(said)
            self.make_secrets(inst, doc)
            ctx = self.context(inst, doc, vals)
            if ctx.get("saves"):
                Path(ctx["saves"]).mkdir(parents=True, exist_ok=True)
            self.configure(inst, "start", ctx)
            argv, env, cwd = self.command(inst, doc, ctx, vals)
            said = policy.kept_off(doc, argv, env)
            if said:
                raise ServerError(said)
            run, offset = doc.get("run") or {}, None
            if ctx.get("log") and Path(ctx["log"]).exists():
                offset = Path(ctx["log"]).stat().st_size
            inst.dir.mkdir(parents=True, exist_ok=True)
            servers._roll(inst.log)
            from ... import oslayer
            oslayer.prepare_children()  # on Windows children inherit "Ctrl+C works": the clean stop depends on it
            mark = f"{servers.MARK} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode()
            spawn = popen or subprocess.Popen
            if run.get("shell") == "cmd":  # your own server's .bat or .cmd (2.9): cmd hosts it in its console
                bad = next((a for a in argv if any(c in a for c in CMD_OWN)), None)
                if bad is not None or "%" in str(inst.log):
                    raise ServerError("A value on its command line holds & | < > ^ % or a quote mark, which the console would read as its own.")
                with open(inst.log, "ab") as log:
                    log.write(mark)
                line = f'cmd /d /v:off /s /c "{subprocess.list2cmdline(argv)} 1>>"{inst.log}" 2>&1"'
                p = spawn(line, cwd=str(cwd), env=env, creationflags=CREATE_NO_WINDOW)
            elif run.get("console") == "keys" and oslayer.pty_consoles():  # Linux: a console of keys is a pty this engine holds
                with open(inst.log, "ab") as log:
                    log.write(mark)
                p = oslayer.spawn_console(argv, cwd, env, inst.log, popen=popen)
            else:
                with open(inst.log, "ab") as log:
                    log.write(mark)
                    log.flush()
                    p = spawn(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.PIPE if run.get("console") == "stdin" else subprocess.DEVNULL, **oslayer.spawn_kwargs())
            for k, old in list(_pipes.items()):  # the pipes of runs that ended by themselves (a crash) go
                if old.poll() is not None:
                    _pipes.pop(k, None)
                    try:
                        old.stdin.close()
                    except (OSError, AttributeError):
                        pass
            if run.get("console") == "stdin":
                _pipes[(inst.id, p.pid)] = p
            d = inst.load()
            d.update(pid=p.pid, startedAt=time.time(), created=_born(p.pid), aliveAt=time.time(), logOffset=offset or 0)
            for k in ("ended", "held", "readyAt", "unhealthy", "health", "stopping"):  # a run of its own now
                d.pop(k, None)
            inst.save(d)
            return {"ok": True, "pid": p.pid}

    # ---------------------------------------------------------------------------------------------------- ready, health, players
    def reach(self, inst, doc=None, d=None, now=None):
        """What the probes and the player sources may ask of this server while it runs (probes.Reach). now is the
        state's own clock, so an alive probe counts on it as the ready timeout does (the harness runs it ten times faster)."""
        doc, d = doc or inst.doc(), d or inst.load()
        rc = doc.get("rcon")
        vals = self.settings(inst, open_secrets=bool(rc))
        ports = {p["id"]: p for p in values.ports(doc, vals)}
        ctx = values.context(doc, vals)
        now = now or time.time()
        return probes.Reach(ports=ports, lines=lambda: self.this_run(inst, doc, 2000), alive=now - float(d.get("startedAt") or now),
                            url=lambda t: values.local_url(t, ctx), rcon_port=ports[rc["port"]]["port"] if rc else None,
                            rcon_password=self.open_secret(inst, rc["password"], vals) if rc else None,
                            auth=lambda spec: self._signed(inst, spec))

    def state(self, inst, now=None, reach=None):
        """{"state": one of STATES, "said": what was seen}. Starting until the recipe's ready probes answer; unhealthy
        when its ready timeout passes first, or when its health probe fails its retries in a row after Ready."""
        now = now or time.time()
        d = inst.load()
        if d.get("installing"):
            return {"state": "installing", "said": ""}
        if not self.running(inst):
            if d.get("held"):
                return {"state": "held", "said": "It crashed again and again, so Fenrir stopped starting it."}
            if d.get("pid"):
                return {"state": "crashed", "said": servers.last_error(self.this_run(inst))}
            return {"state": "stopped", "said": ""}
        if d.get("stopping"):
            return {"state": "stopping", "said": ""}
        doc, key = inst.doc(), (inst.id, d["pid"])
        if not d.get("readyAt"):
            if not doc.get("ready"):
                d["readyAt"] = now
                inst.save(d)
            elif now - _checked.get(key, 0) >= READY_EVERY or reach is not None:
                _checked[key] = now
                ok, why = probes.ready(doc, reach or self.reach(inst, doc, d, now))
                if ok:
                    d["readyAt"] = now
                    inst.save(d)
                else:
                    limit = doc["ready"].get("timeout", READY_TIMEOUT)
                    if now - float(d.get("startedAt") or now) > limit:
                        minutes = max(1, round(limit / 60))
                        return {"state": "unhealthy", "said": f"It did not say it was ready in {minutes} minute{'s' if minutes != 1 else ''}"
                                + (f": {servers.last_error(self.this_run(inst, doc))}" if self.this_run(inst, doc) else ".")}
                    return {"state": "starting", "said": why}
            else:
                return {"state": "starting", "said": ""}
        return self._health(inst, doc, d, now, reach)

    def _health(self, inst, doc, d, now, reach):
        h = doc.get("health")
        if not h:
            return {"state": "ready", "said": ""}
        rec = d.get("health") or {}
        if now - float(d["readyAt"]) >= h.get("grace", 60) and now - float(rec.get("at") or 0) >= h.get("every", 30):
            ok, why = probes.check(h["probe"], reach or self.reach(inst, doc, d, now))
            rec = {"at": now, "failures": 0 if ok else int(rec.get("failures") or 0) + 1, "said": "" if ok else why}
            d["health"] = rec
            inst.save(d)
        if int(rec.get("failures") or 0) >= h.get("retries", 3):
            return {"state": "unhealthy", "said": rec.get("said") or "Its health check fails.", "failures": rec["failures"]}
        return {"state": "ready", "said": ""}

    def restart_due(self, inst):
        """Its health failed restartAfter times in a row: the keeper restarts it (0: it is only said)."""
        h = inst.doc().get("health") or {}
        n = int((inst.load().get("health") or {}).get("failures") or 0)
        return bool(h.get("restartAfter")) and n >= h["restartAfter"]

    def needs(self, inst, kit_there=None):
        """What has to happen before it can start, in plain words, or "": the recipe's needs in order (2.x
        hosting.needs). The kit is there (kit_there, or the kit's folder), the game is installed, a setting the host types
        in (a password, FiveM's key) is set."""
        doc, vals = inst.doc(), None
        for n in doc.get("needs") or []:
            if n.get("kit"):
                there = kit_there if kit_there is not None else bool(inst.kit_dir and Path(inst.kit_dir).is_dir())
                if not there:
                    return n["say"]
            elif n.get("game"):
                if not self.installed(inst, doc):
                    return n["say"]
            elif n.get("setting"):
                vals = vals if vals is not None else self.settings(inst, open_secrets=True)
                if vals.get(n["setting"]) in (None, "", []):
                    return n["say"]
        return ""

    def players(self, inst, reach=None):
        """Who is on, or None when it cannot be told (then a stop from the phone asks first, as if friends were on)."""
        if not self.running(inst):
            return None
        doc = inst.doc()
        return player_sources.count(doc, reach or self.reach(inst, doc), self.settings(inst))

    def join(self, inst):
        """The line for friends: with this run's join code when the server printed one, {address} left for Fenrir
        Connect to fill, and the password note when one is set (2.x WITH_PASSWORD)."""
        doc = inst.doc()
        j = doc.get("join") or {}
        vals = self.settings(inst)
        ctx = values.context(doc, vals, address="{address}")
        code = None
        if j.get("withCode") and self.running(inst):
            for line in reversed(self.this_run(inst, doc)):
                m = values.search(j["withCode"]["code"], line)
                if m:
                    code = m.group(1)
                    break
        text = values.fill(j["withCode"]["text"], {**ctx, "code": code}) if code else values.fill(j.get("text") or "", ctx)
        pw = (values.fields(doc).get("password") or {}).get("type") == "secret" and (inst.load()["settings"].get("password") or "")
        if text and pw and j.get("passwordSay", True):
            text += servers.WITH_PASSWORD
        return {"text": text, "code": code}

    # ---------------------------------------------------------------------------------------------------- console and stop
    def _stop_words(self, doc):
        st = doc.get("stop") or {}
        words = set(st.get("stopWords") or [])
        words |= {s["line"].split()[0].lower() for s in st.get("steps") or [] if s["do"] == "command" and s.get("line", "").split()}
        return words

    # ---------------------------------------------------------------------------------------------------- secrets and the server's own APIs
    def make_secrets(self, inst, doc=None):
        """The values the recipe says Fenrir makes for this server (secrets), each made once and sealed in the store as
        server.<id>.<name>: never written in the recipe, never shown on a page."""
        import secrets as _rand
        from ... import store
        doc = doc or inst.doc()
        for s in doc.get("secrets") or []:
            name = f"server.{inst.id}.{s['name']}"
            if store.get(name):
                continue
            g = s.get("generate") or {}
            n = int(g.get("length") or 24)
            alphabet = "0123456789abcdef" if g.get("alphabet") == "hex" else "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
            store.put(name, "".join(_rand.choice(alphabet) for _ in range(n)), kind="password",
                      label=f"{doc.get('name')}: {s.get('label') or s['name']}", used_by=[doc.get("name") or inst.id])

    def open_secret(self, inst, ref, vals=None):
        """A secret the recipe names, opened from the store: setting.KEY (a secret setting) or secret.NAME (one it makes)."""
        from ... import store
        kind, _, name = str(ref or "").partition(".")
        if kind == "secret":
            return store.get(f"server.{inst.id}.{name}") or ""
        if kind == "setting":
            vals = vals if vals is not None and name in vals else self.settings(inst, open_secrets=True)
            return vals.get(name) or ""
        return ""

    def _signed(self, inst, auth):
        """HTTP Basic sign-in headers for a call to the server's own API on this PC, or {} when the recipe names none."""
        if not auth:
            return {}
        import base64
        pair = f"{auth.get('user') or ''}:{self.open_secret(inst, auth.get('password'))}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}

    def _rest(self, inst, doc, step, minutes=None):
        """One REST call of the recipe's (a stop step or a warning): its address checked to be on this PC, its body
        filled in, signed in where it says. Returns (status, answer)."""
        vals = self.settings(inst)
        ctx = values.context(doc, vals, **({"minutes": str(minutes)} if minutes is not None else {}))
        url = values.local_url(step["url"], ctx)
        body = values.fill(step["body"], ctx) if step.get("body") is not None else None
        return probes.http_call(url, step.get("method", "POST"), body, headers=self._signed(inst, step.get("auth")))

    def warn(self, inst, minutes):
        """The recipe's stop.warn: players told the server restarts in {minutes} minutes, through its console (a line, or a
        command step), its RCON, or its own web API. True when it went; False when the recipe has none or it did not."""
        doc = inst.doc()
        w = (doc.get("stop") or {}).get("warn")
        if not w or not self.running(inst):
            return False
        vals = self.settings(inst)
        ctx = values.context(doc, vals, minutes=str(minutes))
        try:
            if isinstance(w, str):
                self.console(inst, values.fill(w, ctx))
            elif w.get("do") == "command":
                self.console(inst, values.fill(w["line"], ctx))
            elif w.get("do") == "rcon":
                self.reach(inst, doc).rcon(values.fill(w["command"], ctx))
            elif w.get("do") == "rest":
                status, _answer = self._rest(inst, doc, w, minutes)
                return status is not None and 200 <= status < 300
            else:
                return False
        except (ServerError, OSError, ValueError):
            return False
        return True

    def console(self, inst, line, typer=None):
        """One line into its console, as if typed there. A line that would end the server is refused, and its Stop or
        Restart offered: ended from its console, Fenrir would take it for a crash and start it again."""
        doc = inst.doc()
        line = str(line if line is not None else "")
        if values.ONE_LINE.search(line):
            raise ServerError("A console line is one line of text.")
        line = line.strip()
        if not line:
            raise ServerError("Type a command first.")
        if len(line) > 250:
            raise ServerError("A console line is 250 characters at most.")
        words = line.lstrip("/").split()
        first = words[0].lower() if words else ""
        if first in self._stop_words(doc):
            if first == "restart":
                raise UseButton("restart", "Use its Restart button for that: restarted from its console, it would look like a crash to Fenrir.")
            raise UseButton("stop", "Use its Stop button for that: stopped from its console, Fenrir would take it for a crash and start it again.")
        self._send(inst, doc, line, typer)
        _typed[inst.id].append((time.time(), line))
        return {"ok": True}

    def _send(self, inst, doc, line, typer=None):
        if not self.running(inst):
            raise ServerError(f"{doc['name']} is not running: start it first.")
        mode, pid = (doc.get("run") or {}).get("console") or "none", self.pid(inst)
        if mode == "keys":
            from ... import oslayer
            if not (typer or oslayer.write_console)(pid, line):  # the helper's key presses on Windows, the pty on Linux
                raise ServerError("The line did not reach its console. If it keeps happening, restart it from its Dashboard.")
            return
        if mode == "stdin":
            p = _pipes.get((inst.id, pid))
            if p is None or p.poll() is not None or p.stdin is None:
                raise ServerError("Fenrir has started again since this server did, so its console is out of reach until it starts again.")
            try:
                p.stdin.write((line + "\n").encode("utf-8"))
                p.stdin.flush()
            except OSError:
                raise ServerError("The line did not reach its console.") from None
            return
        raise ServerError(f"{doc['name']}'s server takes no console lines.")

    def player_command(self, inst, what, name, typer=None):
        """Kick, ban, unban or mute one player: through RCON where the recipe has it, else its console."""
        doc = inst.doc()
        line = player_sources.command(doc, what, name)
        if doc.get("rcon"):
            self.reach(inst, doc).rcon(line)
        else:
            self._send(inst, doc, line, typer)
        return {"ok": True}

    def stop(self, inst, force=False, sleep=time.sleep, clock=time.time, ctrl_c=None, end=None, typer=None, grace=None, close=None):
        """The recipe's stop steps in order, then the rest of its grace time (or the grace given, as 2.x's stop(wait=)
        takes one); the whole tree is ended only when it has not stopped by then (2.x stopWait), and that is said.
        force=True ends it at once."""
        with inst.lock:
            pid = self.pid(inst)
            if not pid or not self.running(inst):
                d = inst.load()
                for k in ("pid", "ended", "held", "stopping", "readyAt", "health"):  # stopped by the host: not "stopped by itself"
                    d.pop(k, None)
                inst.save(d)
                return {"ok": True, "message": "It was not running"}
            doc = inst.doc()
            st = doc.get("stop") or {"steps": [{"do": "ctrl-c"}]}
            from ... import oslayer
            from ...desktop import in_service
            windows_service = in_service() and oslayer.system() == "windows"  # under systemd a signal and a pty reach as anywhere
            steps = service_steps(st["steps"], (doc.get("run") or {}).get("console") or "none") if windows_service else st["steps"]
            wait = grace if grace is not None else st.get("grace", GRACE)
            limit = clock() + wait
            d = inst.load()
            d["stopping"] = clock()
            inst.save(d)
            forced = False
            try:
                for step in [] if force else steps:
                    if not self.running(inst):
                        break
                    if step["do"] == "end":
                        force = True
                        break
                    self._step(inst, doc, pid, step, limit, sleep, clock, ctrl_c, typer, close)
                while not force and clock() < limit and self.running(inst):
                    sleep(0.5)
                if self.running(inst):  # still this server, by number and start time: its whole tree goes
                    (end or _end)(pid)
                    forced = True
                    if not force:  # a recipe or a caller that asked to end it at once: no forced end
                        from ... import forcedstops
                        forcedstops.note(inst.id, doc.get("name"), wait)
            finally:
                d = inst.load()
                for k in ("pid", "ended", "held", "stopping", "readyAt", "health", "unhealthy"):
                    d.pop(k, None)
                d["stoppedAt"] = time.time()
                inst.save(d)
                p = _pipes.pop((inst.id, pid), None)
                if p is not None:
                    try:
                        if p.stdin:
                            p.stdin.close()
                        p.poll()  # its exit read, so nothing is left waiting on it
                    except OSError:
                        pass
            return {"ok": True, "forced": forced, "message": "Stopped" if not forced else "It did not stop by itself in time, so Fenrir ended it"}

    def _step(self, inst, doc, pid, step, limit, sleep, clock, ctrl_c, typer, close=None):
        """One stop step. One that cannot be done (its console is out of reach, RCON does not answer) hands over to the
        next, and in the end to the tree's end: a stop always stops."""
        do = step["do"]
        try:
            if do == "ctrl-c":
                from ... import oslayer
                (ctrl_c or oslayer.interrupt)(pid)  # the helper's Ctrl+C on Windows, SIGINT to its group on Linux
            elif do == "command":
                self._send(inst, doc, step["line"], typer)
            elif do == "rcon":
                self.reach(inst, doc).rcon(step["command"])
            elif do == "rest":
                self._rest(inst, doc, step)
            elif do == "signal":
                from ... import oslayer
                oslayer.signal(pid, "SIGTERM" if step.get("signal") == "SIGTERM" else "SIGINT")  # Linux's own: nothing on Windows
            elif do == "close":  # 3.0: asked to close, as Core Keeper's own launcher does (taskkill without /F; SIGTERM on Linux)
                from ... import oslayer
                (close or oslayer.close)(pid)
            elif do == "wait":
                until = min(limit, clock() + step["seconds"])
                while clock() < until and self.running(inst):
                    sleep(0.5)
        except (ServerError, OSError, ValueError):
            pass

    # ---------------------------------------------------------------------------------------------------- update and backups
    def update(self, inst, progress=None, **kw):
        """A new version over the old one (SteamCMD's update, or the download again); REC-8's safe update wraps this with
        its backup, its Ready check and its way back."""
        return self.install(inst, progress, **kw)

    def backup_parts(self, inst):
        """What a backup of this server takes: its save folder, what to leave out, how to read it while it runs, and a
        database's dump step. None when the recipe names no save folder."""
        doc = inst.doc()
        sv = doc.get("saves")
        if not sv:
            return None
        ctx = self.context(inst, doc, self.settings(inst), secrets=False)
        dump = (doc.get("backup") or {}).get("dump")
        return {"paths": [Path(ctx["saves"])], "exclude": list(sv.get("exclude") or []), "whileRunning": sv.get("whileRunning", "copy"),
                "dump": {"program": dump["program"], "args": values.args(dump.get("args"), ctx, doc, self.settings(inst)),
                         "file": values.fill(dump["file"], ctx)} if dump else None}

    def typed(self, inst):
        return [[t, x] for t, x in _typed[inst.id]]
