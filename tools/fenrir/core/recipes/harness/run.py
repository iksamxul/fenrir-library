"""The life a recipe runs under the harness (recipes.md section 3.11.3), step by step, each step asserting, and a report
in JSON with a short text beside it:

     1 validate     the schema, Fenrir's own rules, and every pattern timed on lines made to be slow (regexes.py)
     2 install      SteamCMD, a download, a kit or the game's own folder, all fake; then the first-run steps
     3 configure    tests.harness.settings; the fake checks the files the writers wrote (expectFiles)
     4 start        the fake in place of the program, its arguments checked (expectArgs) and the anti-cheat rule held on
                    the final command line (policy.kept_off); then ready within the recipe's timeout
     5 players      counts and names follow the schedule
     6 console      a line reaches it, and chat comes in from its log (and goes out, once the chat bridge is here)
     7 stop         clean, not forced, and the save file is there
     8 backup       the archive holds the save file; a restore brings it back
     9 update       a new build id, the update, Ready again; then an update that fails leaves the old build running
    10 crash        it ends by itself and is seen as crashed (the keeper's restarts come with REC-5)
    11 forced stop  a fake that ignores its stop: the tree end is reported
    12 remove       moved aside, never deleted (REC-5)

Recipe times run ten times faster (a clock the driver is given), so a recipe's minutes are the harness's seconds; the
fake's own times (readyAfter, the players' schedule) are real seconds. Every port setting is moved to free ports first,
and the expected arguments and files follow it, so nothing on this PC is touched and recipes can run side by side.

The driver is Fenrir's own. These are the calls the harness makes of it (REC-2's core/recipes/drivers keeps them; any
other driver given here must too):

    drivers.Instance(sid, recipe, home=..., game_dir=..., kit_dir=..., profile=...)
                                                             one server in its own folder under home (profile where it takes one)
    drivers.for_recipe(recipe) -> drv
    drv.set(inst, changes)                                   the harness's settings
    drv.install(inst, progress=None, steam=fn, fetch=fn)     fn as steamcmd.install's and gamekit.net.download's
                                                             (and run=fn, as subprocess.run's, for first-run steps;
                                                             page=fn, as gamekit.net.get's, for a resolver's page)
    drv.settings(inst, open_secrets=True) -> {key: value}
    drv.context(inst, doc, vals) -> the fill context         install, data, saves, log... as the command line gets them
    drv.start(inst, popen=fn) -> {"ok", "pid"}               fn as subprocess.Popen's
    drv.state(inst, now=None) -> {"state", "said"}           starting, ready, unhealthy, crashed, held, stopped...
    drv.players(inst) -> {"players", "names"?} or None
    drv.console(inst, line)
    drv.stop(inst, force=False, sleep=fn, clock=fn) -> {"ok", "forced"}
    drv.update(inst, progress=None, steam=fn, fetch=fn)
    drv.backup_parts(inst) -> {"paths", "exclude"} or None
    drv.chat(inst, text)                                     optional: the chat bridge (TOG-5)"""
import copy
import fnmatch
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .. import load, policy, values
from . import fake_downloads, fake_pages, fake_steamcmd, play as P, ports, regexes, tool

SPEED = 10  # recipes.md: the harness divides recipe timeouts by ten
BUDGET = 60  # seconds a recipe's whole life may take
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STEPS = ("validate", "install", "configure", "start", "players", "console", "stop", "backup", "update", "crash", "forced stop", "remove")


class Clock:
    """Recipe time, ten times faster than the wall's: what the driver is given as now, sleep and clock."""

    def __init__(self, speed=SPEED):
        self.speed = speed
        self.reset()

    def reset(self):
        self.t0 = time.time()

    def now(self):
        return self.t0 + (time.time() - self.t0) * self.speed

    def sleep(self, seconds):
        time.sleep(max(0.0, float(seconds)) / self.speed)


class Failed(Exception):
    """A step's assertion: what was expected and what happened."""


class Waiting(Exception):
    """A step that waits for a part of Fenrir not built yet: said, never counted as passed or failed."""


def _takes(fn, name):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _call(fn, *args, **kw):
    """fn with only the seams it takes: a driver without a clock seam still runs, at the wall's speed."""
    return fn(*args, **{k: v for k, v in kw.items() if _takes(fn, k)})


def moved(value, moves):
    """An expected value with the harness's port moves applied: the number alone, or after = or :."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return value
    s = str(value)
    if s in moves:
        return int(moves[s]) if isinstance(value, int) else moves[s]
    for old, new in moves.items():
        for mark in ("=", ":"):
            if s.endswith(mark + old):
                return s[:-len(old)] + new
    return value


class Run:
    """One recipe's whole life. doc is the recipe document (as read from its file), drivers the module that runs it
    (core.recipes.drivers when None), folder where everything of the run lives (a new temporary one when None)."""

    def __init__(self, doc, drivers=None, folder=None, source="builtin", speed=SPEED, platform=None):
        self.doc, self.source = doc, source
        self.platform = platform or load.here()[0]
        self.view = policy.view(doc, self.platform)
        self.h = P.harness_of(self.view)
        self.drivers = drivers
        self.own_folder = folder is None
        self.folder = Path(folder or tempfile.mkdtemp(prefix="fenrir-harness-"))
        self.clock = Clock(speed)
        self.steps, self.pids, self.moves = [], [], {}
        self.inst = self.drv = self.recipe = None
        self.downloads = None
        self.pages = {}  # a resolver's pages by address (fake_pages.py), what the page seam serves
        self.steam_state = self.folder / "steamcmd-state.json"
        self.game_dir = self.kit_dir = None
        self.report_path = self.folder / "fake-report.jsonl"
        self.settings = {}
        self.runs = 0
        self.kept_off = None

    # ---------------------------------------------------------------------------------------------------- helpers
    def events(self, run=None):
        path = self.report_path.with_name(f"fake-{run if run is not None else self.runs}.jsonl")
        try:
            return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        except (OSError, ValueError):
            return []

    def steam(self, app_id, dest, validate=False, beta=None, progress=None, **kw):
        from ...games import steamcmd
        return steamcmd.install(app_id, dest, validate=validate, beta=beta, progress=progress,
                                home=fake_steamcmd.home(self.folder / "steamcmd"), runner=fake_steamcmd.runner(self.steam_state))

    def lay_loader(self, folder, loader, loader_version=None, mc_version=None, progress=None):
        """The loader seam of a Minecraft recipe's install (as core/loaders.py's install_loader): what its installer would
        leave in the server's folder, laid down (play.minecraft_layout), with no installer downloaded and no Java run."""
        P.lay_down(folder, P.minecraft_layout(loader, loader_version, mc_version))

    def page(self, url, **kw):
        """The page seam (as gamekit.net.get's): a resolver's page from fake_pages.py, never the internet's."""
        from gamekit import net
        if url not in self.pages:
            raise net.NetError(f"The harness has no page at {url}.")
        return self.pages[url]

    def steam_set(self, **change):
        state = json.loads(self.steam_state.read_text("utf-8"))
        for a in state["apps"].values():
            a.update(change)
        self.steam_state.write_text(json.dumps(state), "utf-8")

    def first_run(self, argv, **kw):
        """The seam for first-run steps (as subprocess.run): the fake plays the step's program."""
        self.runs += 1
        path = P.write(self.folder / f"play-{self.runs}.json", {**self.play_now(), "report": str(self.report_path.with_name(f"fake-{self.runs}.jsonl"))})
        cmd = tool.command("fake-server", "--play", path, "--as", argv[0], "--first-run", "1", "--", *argv[1:])
        return subprocess.run(cmd, **kw)

    def play_now(self, **flags):
        vals = _call(self.drv.settings, self.inst, open_secrets=True)
        ctx = self.drv.context(self.inst, self.inst.doc(), vals)
        out = P.play(self.inst.doc(), vals, ctx, "", **flags)
        out["expectArgs"] = [moved(a, self.moves) for a in out["expectArgs"]] if out["expectArgs"] is not None else None
        if "args" in (((self.doc.get("platformOverrides") or {}).get(self.platform) or {}).get("run") or {}):
            out["expectArgs"] = None  # written for the recipe's own arguments: this platform's override has its own (7 Days to Die on Linux)
        for f in out["expectFiles"]:
            f["expect"] = {k: moved(v, self.moves) for k, v in f["expect"].items()}
        return out

    def popen(self, flags):
        """The seam for start (as subprocess.Popen): the owner's anti-cheat rule held on the final command line, then
        the fake in place of the program, with the program's own folder, environment and flags."""
        def spawn(argv, **kw):
            if isinstance(argv, str):
                raise OSError("The harness plays programs that are started directly, not through cmd.")
            said = policy.kept_off(self.inst.doc(), argv, kw.get("env"))
            if said:
                self.kept_off = said
                raise OSError(said)
            self.runs += 1
            self.close_file = self.folder / f"close-{self.runs}.request"  # a "close" stop reaches this run's fake here (3.0)
            the_play = {**self.play_now(**flags), "report": str(self.report_path.with_name(f"fake-{self.runs}.jsonl")),
                        "closeFile": str(self.close_file)}
            path = P.write(self.folder / f"play-{self.runs}.json", the_play)
            p = subprocess.Popen(tool.command("fake-server", "--play", path, "--as", argv[0], "--", *argv[1:]), **kw)
            self.pids.append(p)
            return p
        return spawn

    def start(self, **flags):
        self.kept_off = None
        self.clock.reset()
        r = _call(self.drv.start, self.inst, popen=self.popen(flags))
        if not (isinstance(r, dict) and r.get("ok")):
            raise Failed(f"It did not start: {r}")
        return r

    def state(self):
        return _call(self.drv.state, self.inst, now=self.clock.now())

    def wait(self, want, limit):
        end, st = time.time() + limit, {}
        while time.time() < end:
            st = self.state() or {}
            if st.get("state") in want:
                return st
            if st.get("state") in ("unhealthy", "crashed", "held", "stopped") and st.get("state") not in want:
                return st
            time.sleep(0.1)
        return st

    def ready(self):
        timeout = (self.view.get("ready") or {}).get("timeout", 300)
        st = self.wait(("ready",), timeout / self.clock.speed + float(self.h.get("readyAfter") or 0) + 8)
        if st.get("state") != "ready":
            raise Failed(f"It was not ready: {st.get('state')} {st.get('said') or ''}".strip())
        return st

    def stop(self, force=False):
        return _call(self.drv.stop, self.inst, force=force, sleep=self.clock.sleep, clock=self.clock.now, close=self._close)

    def _close(self, pid):
        """A recipe's "close" stop, as the fake hears it: a request file beside its report (the real one is taskkill)."""
        cf = getattr(self, "close_file", None)
        if cf is not None:
            Path(cf).write_text("close", "utf-8")

    def stopped(self):
        """A step that starts the server begins with it stopped, whatever the step before left."""
        if (self.state() or {}).get("state") not in ("stopped", "crashed", "held"):
            self.stop(force=True)

    def fake_said(self, event, run=None):
        return [e for e in self.events(run) if e["event"] == event]

    # ---------------------------------------------------------------------------------------------------- the steps
    def s_validate(self):
        said = load.check(self.doc, self.source)
        if said:
            raise Failed("; ".join(str(p) for p in said[:5]))
        if not self.h:
            raise Failed("The recipe has no tests.harness section, so the harness cannot play it.")
        pats = list(dict.fromkeys((w, p) for plat in ("windows", "linux") for w, p, _ in policy._regexes(policy.view(self.doc, plat))))
        slow = regexes.slow(pats)
        if slow:
            raise Failed("; ".join(f"{s['where']} {s['pattern']!r} took {'too long to finish' if s['ms'] is None else str(round(s['ms'])) + ' ms'}"
                                   " on one 4 KB line" for s in slow))
        return f"{len(pats)} patterns timed"

    def _world(self):
        """The fake world the install reads from, and the recipe the run uses: a download's sha256 is the fake's."""
        get, doc = self.view["get"], copy.deepcopy(self.doc)
        if "steamcmd" in get:
            app = str(get["steamcmd"]["app"])
            self.steam_state.write_text(json.dumps({"apps": {app: {"files": P.layout(self.view), "buildIds": self.h.get("buildIds") or ["1"], "latest": 0}}}), "utf-8")
        elif "download" in get and get["download"].get("resolver"):
            fake = fake_pages.FAKES.get(get["download"]["resolver"])
            if fake is None:
                raise Waiting(f"its download comes from the {get['download']['resolver']} resolver, and the harness has no page of it to serve")
            self.downloads = fake_downloads.FakeDownloads().start()
            self.pages = fake(self.downloads, self.view)
        elif "download" in get:
            self.downloads = fake_downloads.FakeDownloads().start()
            places = [doc["get"]["download"]["files"]]
            for over in (doc.get("platformOverrides") or {}).values():
                if "download" in (over.get("get") or {}):
                    places.append(over["get"]["download"]["files"])
            for files in places:
                for entry in files:
                    data, sha = P.archive(entry, policy.view(doc, self.platform))
                    self.downloads.add(entry["url"], data)
                    entry["sha256"], entry["size"] = sha, len(data)
        elif "fromGame" in get:
            self.game_dir = self.folder / "game"
            P.lay_down(self.game_dir, {get["fromGame"]["file"]: P.PLACEHOLDER, **P.layout(self.view)})
        elif "kit" in get:
            self.kit_dir = self.folder / "kit"
            P.lay_down(self.kit_dir / PurePosixPath(get["kit"]["from"]), P.layout(self.view))
        elif "image" in get:
            raise Waiting("an image runs on the Docker driver (REC-9), with fake_docker.py first on PATH")
        elif "minecraft" in get:
            pass  # its loader goes down through the install's loader seam (lay_loader), where 2.x runs the real installer
        return load.from_doc(doc, self.source)

    def _move_ports(self):
        fl = values.fields(self.view)
        spans = {}
        for p in self.view.get("ports") or []:
            if isinstance(p["port"], str):
                for kind, key, plus in values.parse(p["port"]):
                    if kind == "setting":
                        spans[key] = max(spans.get(key, 0), plus)
        for key, top in spans.items():
            f = fl.get(key) or {}
            old = int(self.settings.get(key, values.default(f)) or 0)
            base = ports.free_base(top + 1, low=f.get("min", 1024), high=f.get("max", 65535))
            self.settings[key] = base
            for k in range(top + 1):
                self.moves[str(old + k)] = str(base + k)

    def s_install(self):
        if self.drivers is None:
            try:
                from .. import drivers
            except ImportError:
                raise Waiting("the process driver (REC-2) is not part of Fenrir yet") from None
            self.drivers = drivers
        self.recipe = self._world()
        self.drv = self.drivers.for_recipe(self.recipe)
        # an id of its own, as a second server of the recipe has (recipes.md 3.9), so nothing kept under a real server's id is touched
        sid = f"{self.recipe.id}-{os.urandom(4).hex()}"
        # a profile of the run's own too: a game that keeps its saves under the account's profile writes them here instead
        self.inst = _call(self.drivers.Instance, sid, self.recipe, home=self.folder / "servers", game_dir=self.game_dir,
                          kit_dir=self.kit_dir, profile=self.folder / "profile")
        self.settings = dict(self.h.get("settings") or {})
        self._move_ports()
        self.drv.set(self.inst, self.settings)
        if self.view.get("firstRun") and not _takes(self.drv.install, "run"):
            raise Failed("The recipe has first-run steps, and the driver's install has no run seam for the harness to play them.")
        mc = {"loader": self.lay_loader} if "minecraft" in self.view["get"] else {}  # only a Minecraft driver takes it
        _call(self.drv.install, self.inst, steam=self.steam, fetch=self.downloads.fetch if self.downloads else None,
              page=self.page if self.pages else None, run=self.first_run, **mc)
        if self.view.get("firstRun") and not any(e["event"] == "firstRun" for r in range(1, self.runs + 1) for e in self.events(r)):
            raise Failed("The first-run steps did not run.")
        return f"through the fake {next(iter(self.view['get']))}" + (f", ports moved {self.moves}" if self.moves else "")

    def s_configure(self):
        vals = _call(self.drv.settings, self.inst, open_secrets=True)
        wrong = {k: v for k, v in self.settings.items() if str(values.as_text(values.fields(self.view).get(k), vals.get(k))) != str(values.as_text(values.fields(self.view).get(k), v))}
        if wrong:
            raise Failed(f"The settings did not read back as set: {wrong}")
        return f"{len(self.settings)} settings; the files are checked as it starts"

    def s_start(self):
        try:
            self.start()
        except Exception as e:  # noqa: BLE001 - the anti-cheat rule, said in the driver's words or the harness's own
            raise Failed(f"It was not started: {self.kept_off or e}") from None
        try:
            st = self.ready()
        except Failed as e:
            args, files = self.fake_said("args"), self.fake_said("files")
            if args and not args[0]["ok"]:
                raise Failed(f"The command line was {args[0]['got']}, and the harness expects {args[0]['want']}") from None
            if files and not files[0]["ok"]:
                raise Failed(f"The files were not as the harness expects: {files[0]['bad'][:3]}") from None
            raise e
        if not self.fake_said("args"):
            raise Failed(f"The fake never ran ({st.get('said') or st.get('state')})")
        started = self.fake_said("started")[0]
        want_env = {k: values.fill(t, self.drv.context(self.inst, self.inst.doc(), _call(self.drv.settings, self.inst, open_secrets=True)))
                    for k, t in ((self.view.get("run") or {}).get("env") or {}).items()}
        if started.get("env") and any(started["env"].get(k) != v for k, v in want_env.items()):
            raise Failed(f"The environment did not reach the program: {started['env']}")
        if not started.get("sameProgram"):
            raise Failed(f"It started {started['program']}, not the recipe's {self.view['run'].get('program')}")
        return f"ready ({st.get('said') or 'its probes answered'})"

    def s_players(self):
        sched = self.h.get("players") or []
        if not sched:
            return "no players in the harness"
        if not (self.view.get("players") or {}).get("sources"):
            return "the recipe cannot count players"
        peak_t = max(float(p["at"]) for p in sched)
        peak = [p["name"] for p in sched if float(p["at"]) <= peak_t and (p.get("leave") is None or float(p["leave"]) > peak_t)]
        seen, last = self._until_players(peak, peak_t, peak_t + 6)
        if seen is None:
            raise Failed(f"The players never came to {sorted(peak)}: the last answer was {last}")
        leaves = [float(p["leave"]) for p in sched if p.get("leave") is not None]
        if leaves:
            end_t = max(leaves)
            left = [p["name"] for p in sched if p.get("leave") is None or float(p["leave"]) > end_t]
            seen, last = self._until_players(left, end_t, end_t + 6)
            if seen is None:
                raise Failed(f"After the schedule's leaves it did not come to {sorted(left)}: the last answer was {last}")
        return f"{len(peak)} at most, through {(self.view['players']['sources'][0])['via']}"

    def _until_players(self, names, not_before, limit_from_start):
        """The players as the driver tells them, until they are these: the count, and the names where its source gives
        names. The first ask waits for the schedule to get there, since Steam's player query remembers who was on for
        ten seconds (core/games/a2splayers.py): asked too early, it would still say so after they came."""
        started = self.fake_said("started")
        # when the fake's clock began: its report says the wall time too, so a report read late does not move the schedule
        t0 = (started[0]["wall"] - started[0]["t"]) if started and "wall" in started[0] else time.time() - (started[0]["t"] if started else 0)
        time.sleep(max(0.0, t0 + not_before + 0.3 - time.time()))
        last = None
        while time.time() - t0 < limit_from_start + 12:
            got = self.drv.players(self.inst)
            last = got
            if isinstance(got, dict) and int(got.get("players") or 0) == len(names) and (got.get("names") is None or sorted(got["names"]) == sorted(names)):
                return got, last
            time.sleep(0.25)
        return None, last

    def s_console(self):
        said = []
        how = (self.view.get("run") or {}).get("console", "none")
        if how in ("stdin", "keys"):
            if how == "keys" and os.name != "nt":
                return "keys are typed only on Windows"
            line = "harness says hello"
            self.drv.console(self.inst, line)
            end = time.time() + 6
            while time.time() < end and not any(e.get("line") == line for e in self.fake_said("command")):
                time.sleep(0.1)
            if not any(e.get("line") == line for e in self.fake_said("command")):
                raise Failed("A line typed into its console did not reach it")
            said.append("a line reached its console")
        else:
            said.append("its console is none")
        chat = self.view.get("chat") or {}
        if (chat.get("receive") or {}).get("log") and self.h.get("chatLine"):
            text = "\n".join(self._output())
            if not any(values.search(chat["receive"]["log"], ln) for ln in text.splitlines()):
                raise Failed("Chat from a player did not show in its output as the recipe reads it")
            said.append("chat came in")
        if chat.get("send"):
            if not hasattr(self.drv, "chat"):
                said.append("chat out waits for the chat bridge (TOG-5)")
            else:
                self.drv.chat(self.inst, "hello from Fenrir")
                end = time.time() + 6
                while time.time() < end and not self.fake_said("chat"):
                    time.sleep(0.1)
                if not self.fake_said("chat"):
                    raise Failed("Chat sent from Fenrir did not reach it")
                said.append("chat went out")
        return "; ".join(said)

    def _output(self):
        lines = []
        for p in {getattr(self.inst, "log", None), *(Path(x) for x in self.play_now().get("alsoWrite") or [])}:
            try:
                lines += Path(p).read_text("utf-8", errors="replace").splitlines()
            except (OSError, TypeError):
                pass
        return lines

    def s_stop(self):
        r = self.stop()
        if not isinstance(r, dict) or r.get("forced"):
            raise Failed(f"It had to be ended: {r}")
        st = self.fake_said("stopped")
        if not st:
            raise Failed("The fake never heard a stop it knows")
        how, saved = st[0]["how"], st[0]["saved"]
        if how not in (self.h.get("saveOn") or []):
            raise Failed(f"It was stopped by {how}, which the harness does not save on {self.h.get('saveOn')}")
        want = self.play_now()["saveFile"]
        if want and (not saved or not Path(want).is_file()):
            raise Failed(f"The save file {want} is not there")
        return f"by {how}" + (", saved" if want else "")

    def s_backup(self):
        want = self.play_now()["saveFile"]
        parts = self.drv.backup_parts(self.inst)
        if not parts:
            if want:
                raise Failed("The recipe names no save folder, so its save would never be backed up")
            return "it keeps no world on this PC"
        if not want:
            return "the harness names no save file"
        arc, roots = self.folder / "backup.zip", [Path(p) for p in parts["paths"]]
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as z:
            for root in roots:
                for f in root.rglob("*") if root.is_dir() else []:
                    rel = f.relative_to(root).as_posix()
                    if f.is_file() and not any(fnmatch.fnmatch(rel, pat) for pat in parts.get("exclude") or []):
                        z.write(f, f"{root.name}/{rel}")
        root = next((r for r in roots if Path(want).resolve().is_relative_to(r.resolve())), None)
        if root is None:
            raise Failed(f"The save file is outside what a backup takes ({[str(r) for r in roots]})")
        entry = f"{root.name}/{Path(want).resolve().relative_to(root.resolve()).as_posix()}"
        with zipfile.ZipFile(arc) as z:
            if entry not in z.namelist():
                raise Failed(f"The backup does not hold {entry}")
            before = hashlib.sha256(Path(want).read_bytes()).hexdigest()
            Path(want).unlink()
            z.extract(entry, root.parent)
        if hashlib.sha256(Path(want).read_bytes()).hexdigest() != before:
            raise Failed("The restored save file is not the one backed up")
        return "the archive holds the save, and a restore brings it back (the harness's archive until REC-3 moves backups to recipes)"

    def s_update(self):
        builds = self.h.get("buildIds") or []
        if "steamcmd" not in self.view["get"]:
            return "only SteamCMD recipes have build ids"
        if len(builds) < 2:
            return "the harness names one build"
        self.stopped()
        self.steam_set(latest=1)
        _call(self.drv.update, self.inst, steam=self.steam, fetch=None)
        app = self.view["get"]["steamcmd"]["app"]
        manifest = Path(self.inst.install_dir()) / "steamapps" / f"appmanifest_{app}.acf"
        if f'"buildid"\t\t"{builds[1]}"' not in manifest.read_text("utf-8"):
            raise Failed("The update did not bring the new build")
        self.start()
        self.ready()
        self.stop()
        self.steam_set(fail=True)
        try:
            _call(self.drv.update, self.inst, steam=self.steam, fetch=None)
        except Exception:  # noqa: BLE001 - the failing update is the point: it must not leave the server broken
            pass
        else:
            raise Failed("An update SteamCMD failed was taken as done")
        self.steam_set(fail=False)
        self.start()
        self.ready()
        self.stop()
        return f"build {builds[0]} to {builds[1]}, Ready again; a failed update left it running"

    def s_crash(self):
        c = self.h.get("crash") or {}
        crash = {"after": float(c.get("after", 1.0)), "code": int(c.get("code", 3)), "line": c.get("line") or "Fatal error: the fake server crashed on purpose"}
        self.stopped()
        self.start(crash=crash)
        st = self.wait(("crashed",), crash["after"] + 10)
        if st.get("state") != "crashed":
            raise Failed(f"It ended by itself and was seen as {st.get('state')}")
        return f"crashed ({st.get('said') or 'no reason given'}); the keeper's restarts and hold come with REC-5"

    def s_forced(self):
        self.stopped()
        self.start(slow_stop=True)
        self.ready()
        r = self.stop()
        if not (isinstance(r, dict) and r.get("forced")):
            raise Failed(f"A server that ignores its stop was not ended: {r}")
        if not self.fake_said("stopIgnored"):
            raise Failed("The fake was never asked to stop before it was ended")
        return "the tree end was reported"

    def s_remove(self):
        raise Waiting("removing a server moves it aside, with REC-5's instances")

    # ---------------------------------------------------------------------------------------------------- the run
    def go(self):
        t0 = time.time()
        plan = [("validate", self.s_validate), ("install", self.s_install), ("configure", self.s_configure), ("start", self.s_start),
                ("players", self.s_players), ("console", self.s_console), ("stop", self.s_stop), ("backup", self.s_backup),
                ("update", self.s_update), ("crash", self.s_crash), ("forced stop", self.s_forced), ("remove", self.s_remove)]
        blocked = None
        try:
            for name, fn in plan:
                t = time.time()
                if blocked:
                    self.steps.append({"step": name, "ok": None, "said": f"not run: {blocked}", "seconds": 0.0})
                    continue
                try:
                    said, ok = fn(), True
                except Waiting as e:
                    said, ok = f"waits: {e}", None
                    if name in ("install",):
                        blocked = str(e)
                except Exception as e:  # noqa: BLE001 - a step's failure is its report, and the steps that need it are not run
                    said, ok = str(e) or e.__class__.__name__, False
                    if name in ("validate", "install", "start"):
                        blocked = f"{name} failed"
                self.steps.append({"step": name, "ok": ok, "said": said, "seconds": round(time.time() - t, 2)})
        finally:
            self.cleanup()
        seconds = round(time.time() - t0, 2)
        done = {s["step"] for s in self.steps if s["ok"]}
        if any(s["ok"] is False for s in self.steps):
            status = "failed"
        elif {"validate", "install", "start", "stop"} <= done:
            status = "passed"
        else:
            status = "incomplete"  # nothing failed, and the life could not be run far enough to vouch for the recipe
        return {"recipe": self.doc.get("id"), "version": self.doc.get("version"), "platform": self.platform, "status": status,
                "ok": status == "passed", "seconds": seconds, "withinBudget": seconds <= BUDGET, "steps": self.steps}

    def cleanup(self):
        """No fake left running, whatever happened, and the run's folder gone when it was the harness's own."""
        if self.inst is not None and self.drv is not None:
            try:
                st = self.state() or {}
                if st.get("state") not in ("stopped", "crashed", "held", None):
                    self.stop(force=True)
            except Exception:  # noqa: BLE001 - the end of a run must not fail over a server that is gone already
                pass
        for p in self.pids:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass
        if self.downloads:
            self.downloads.stop()
        if self.inst is not None:  # the run's settings sealed in the store (its secrets) go with it: the id was the run's own
            try:
                from ... import store
                for e in store.names(f"server.{self.inst.id}."):
                    store.delete(e["name"])
            except Exception:  # noqa: BLE001 - a store that cannot be opened holds nothing of this run
                pass
        if self.own_folder:
            shutil.rmtree(self.folder, ignore_errors=True)


def text(report):
    """The report in a few lines: each step's mark, name and what it said."""
    marks = {True: "ok  ", False: "FAIL", None: "wait"}
    passed = sum(1 for s in report["steps"] if s["ok"])
    waiting = sum(1 for s in report["steps"] if s["ok"] is None)
    head = (f"{report['recipe']} {report['version']} on {report['platform']}: {report['status'].upper() if report['status'] == 'failed' else report['status']}, "
            f"{passed} of {len(report['steps'])} steps passed, {waiting} waiting ({report['seconds']} s"
            + ("" if report.get("withinBudget", True) else f", over the {BUDGET} s budget") + ")")
    return "\n".join([head, *(f"  {marks[s['ok']]}  {s['step']}: {s['said']}" for s in report["steps"])])


def run(source, drivers=None, folder=None, origin="builtin"):
    """A recipe file's (or document's) whole life under the harness: the report."""
    try:
        doc = source if isinstance(source, dict) else load.parse(Path(source).read_bytes())
    except (OSError, ValueError) as e:  # a file that is not a recipe at all: its first step fails, and nothing else runs
        return {"recipe": Path(str(source)).stem, "version": "", "platform": load.here()[0], "status": "failed", "ok": False,
                "seconds": 0.0, "withinBudget": True,
                "steps": [{"step": "validate", "ok": False, "said": str(e), "seconds": 0.0},
                          *({"step": s, "ok": None, "said": "not run: validate failed", "seconds": 0.0} for s in STEPS[1:])]}
    return Run(doc, drivers=drivers, folder=folder, source=origin).go()
