"""The swap of a Fenrir update, run by the new build itself.

core/selfupdate.py unpacks the new build into <app>/.update/new and starts `Fenrir.exe --finish-update <plan>` from there;
app.py hands that to finish() below before any heavy import. Then:

    1. take a handle on the old Fenrir and say this helper is alive: the old one closes only after that, so a build that
       cannot start on this PC changes nothing
    2. wait for the old Fenrir and its watcher to end (the world, the tunnels and the playit agent keep running)
    3. copy the new Fenrir.exe and _internal in beside the old ones (Fenrir.exe.new, _internal.new): slow, changes nothing
    4. four renames: the old pair into .update/old, the new pair into place (_internal first, the exe last)
    5. start the new Fenrir, which must answer on its port with the new build within two minutes
    6. otherwise: end it, rename the old pair back and start the old Fenrir

Where each half of each build sits is tracked at every rename, so a step that fails is undone exactly; when the old pair
cannot be made whole again, the new pair is made whole instead (copied afresh from the stage), because a complete Fenrir
of either build beats a mix or a gap. Only when neither can be made whole is the update "broken", and then a message says
how to put Fenrir back. Nothing else in the folder moves: a Fenrir installed with Setup keeps its server, packs and
tools there. Each step is written to the status file, which the next Fenrir to start reads (core/selfupdate.py report)."""
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .util import DATA

STATUS = DATA / "update-status.json"
PLAN = DATA / "update-plan.json"
STAGE = ".update"
PROGRAM = ("_internal", "Fenrir.exe")  # what an update replaces, in this order: the exe last, so a half-done swap never starts
HEALTH_WAIT = 120
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
WORKING = ("waiting", "copying", "swapping", "started", "rollback")  # a helper may still be at work in these
FINAL = ("done", "rolledback", "failed", "kept")  # the folder holds one whole build: what the update left beside it may go
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this PC itself: never through a system proxy


class SwapError(Exception):
    """Said to the host as it is."""


# ---------------------------------------------------------------------------------------------------- the status file
def write_json(p, data, tries=40, pause=0.05):
    """Whole or not at all: a temp file renamed over the old one. Windows refuses that rename while any other process has
    the file open, and during an update the old Fenrir reads the status four times a second while the helper writes it
    (the end-to-end run hit it): try again for a moment, then write in place, which a reader may see half-written once
    and simply reads again."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, "utf-8")
    for _ in range(tries):
        try:
            os.replace(tmp, p)
            return data
        except PermissionError:
            time.sleep(pause)
    try:
        p.write_text(text, "utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return data


def write_status(state, path=None, **fields):
    return write_json(path or STATUS, {"state": state, "t": time.time(), **fields})


def read_status(path=None):
    try:
        d = json.loads(Path(path or STATUS).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


# ---------------------------------------------------------------------------------------------------- processes (Windows)
def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    k.K32EnumProcesses.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.WaitForSingleObject.restype = ctypes.c_uint32
    return k


def alive(pid):
    if not pid:
        return False
    if os.name != "nt":  # the kernel's own word (core/oslayer: /proc, never a zombie)
        from .oslayer import alive as running
        return running(pid)
    k = _k32()
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_uint32()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


class ProcessWaiter:
    """A handle on the old Fenrir, taken while it surely runs (it waits for this helper's word). Waiting on the handle
    cannot be fooled by Windows handing the same process ID to another program a moment after Fenrir ends."""

    def __init__(self, pid):
        self.h = None
        if os.name == "nt" and pid:
            try:
                self.h = _k32().OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            except OSError:
                self.h = None

    def ended(self, seconds):
        """True when the process has ended within `seconds`, False when it still runs, None when there is no handle."""
        if not self.h:
            return None
        return _k32().WaitForSingleObject(self.h, int(max(0, seconds) * 1000)) == 0  # WAIT_OBJECT_0

    def close(self):
        if self.h:
            _k32().CloseHandle(self.h)
            self.h = None


def end(pid):
    """That one process, never its tree: the world and the tunnels it started keep running."""
    if os.name != "nt" or not pid:
        return False
    k = _k32()
    h = k.OpenProcess(0x0001 | 0x1000, False, int(pid))  # PROCESS_TERMINATE
    if not h:
        return False
    try:
        return bool(k.TerminateProcess(h, 1))
    finally:
        k.CloseHandle(h)


def running(exe):
    """Processes whose image is exactly this file (the old Fenrir, its watcher, a copy someone started by hand)."""
    if os.name != "nt":
        return []
    k = _k32()
    arr = (ctypes.c_uint32 * 8192)()
    needed = ctypes.c_uint32()
    if not k.K32EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    want = os.path.normcase(os.path.abspath(str(exe)))
    out = []
    for pid in arr[: needed.value // 4]:
        if not pid or pid == os.getpid():
            continue
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(32768)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and os.path.normcase(buf.value) == want:
                out.append(int(pid))
        finally:
            k.CloseHandle(h)
    return out


def launch(exe, args, cwd):
    p = subprocess.Popen([str(exe), *args], cwd=str(cwd), creationflags=DETACHED if os.name == "nt" else 0, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid


def probe(port, timeout=2.0):
    """The build a Fenrir on this port answers with (its own page's token, then /api/version), or None. Straight to this PC,
    never through a proxy Windows is set to: urllib would send 127.0.0.1 to it on a PC with "Use a proxy server" on."""
    try:
        with LOCAL.open(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            page = r.read(300000).decode("utf-8", "replace")
        if "<title>Fenrir</title>" not in page:
            return None
        m = re.search(r'name="fenrir-token" content="([^"]+)"', page)
        if not m:
            return None
        for path in ("/api/version", "/api/about"):  # /api/version answers at once; /api/about's first call is slow on a fresh start
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Fenrir-Token": m.group(1), "Connection": "close"})
            try:
                with LOCAL.open(req, timeout=timeout + 8) as r:
                    return str(json.loads(r.read()).get("version") or "") or None
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    return None
        return None
    except Exception:  # noqa: BLE001 - not up yet, another program, a refused connection
        return None


def listening():
    try:
        from .procs import listening_ports
        return listening_ports()
    except Exception:  # noqa: BLE001
        return None


def webview_children(data):
    """WebView2 processes still on Fenrir's own window profile: the new window waits until the old one's have gone."""
    try:
        from .procs import find_processes
        return [r["pid"] for r in find_processes("msedgewebview2.exe", contains=str(Path(data) / "webview"))]
    except Exception:  # noqa: BLE001
        return []


def message(text):
    """The one thing a host must read when an update could not put any whole Fenrir back."""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Fenrir", 0x30)
    except (AttributeError, OSError):
        print(text)


def tree_size(p):
    p = Path(p)
    if p.is_file():
        return p.stat().st_size, 1
    size = files = 0
    for f in p.rglob("*"):
        if f.is_file():
            size += f.stat().st_size
            files += 1
    return size, files


def _rm(p):
    p = Path(p)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------------- the swap
class Swap:
    """One update, from the plan the old Fenrir wrote. The process-facing parts are attributes, so tests replace them."""

    def __init__(self, plan, sleep=time.sleep, clock=time.time):
        self.plan = plan
        self.root = Path(plan["root"])
        self.stage = Path(plan["stage"])
        self.box = self.root / STAGE
        self.status_path = Path(plan.get("status") or STATUS)
        self.data = Path(plan.get("data") or DATA)
        self.goodbye_path = Path(plan.get("goodbye") or (self.data / "goodbye"))
        self.sleep, self.clock = sleep, clock
        self.alive, self.end, self.running, self.launch, self.probe = alive, end, running, launch, probe
        self.webview, self.listening, self.message, self.make_waiter = webview_children, listening, message, ProcessWaiter
        self.retry_for = 30.0
        self.health_wait = HEALTH_WAIT
        self.t0 = clock()
        self.marks = {}  # seconds since the helper started, per step: closed, copied, swapped, started, answered
        self.where = {n: {"old": "root" if (self.root / n).exists() else "none", "new": "stage"} for n in PROGRAM}
        self.old_gone = False

    # ---- small things
    def exe(self):
        return self.root / "Fenrir.exe"

    def mark(self, step):
        self.marks[step] = round(self.clock() - self.t0, 1)

    def down(self):
        """How long no Fenrir answered: from the old one's end to the new one's (or the old one's again) first answer."""
        a, b = self.marks.get("closed"), self.marks.get("answered")
        return round(b - a, 1) if a is not None and b is not None else None

    def say(self, state, **fields):
        return write_status(state, self.status_path, id=self.plan.get("id"), helper=os.getpid(), **{"from": self.plan.get("from"), "to": self.plan.get("to")},
                            seconds=round(self.clock() - self.t0, 1), steps=dict(self.marks), down=self.down(), **fields)

    def goodbye(self, reason):
        """Read by the new Fenrir's watcher (core/watch.py) when its Fenrir ends: then it does not start it again."""
        try:
            self.goodbye_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.goodbye_path.with_name(f"goodbye.{os.getpid()}.tmp")
            tmp.write_text(f"{time.time()} {reason}", "utf-8")
            for _ in range(40):
                try:
                    os.replace(tmp, self.goodbye_path)
                    return
                except PermissionError:
                    time.sleep(0.05)
            self.goodbye_path.write_text(f"{time.time()} {reason}", "utf-8")
            tmp.unlink()
        except OSError:
            pass

    def wait_gone(self, pids_fn, limit):
        t0 = self.clock()
        while pids_fn():
            if self.clock() - t0 >= limit:
                return False
            self.sleep(0.25)
        return True

    def rename(self, a, b):
        """A DLL is released a moment after its process ends, and a scanner may hold a fresh file: keep trying for a while."""
        t0 = self.clock()
        while True:
            try:
                os.rename(a, b)
                return
            except PermissionError:
                if self.clock() - t0 >= self.retry_for:
                    raise
                self.sleep(0.5)

    def start(self):
        """The Fenrir now in the folder, with the old one's arguments. False when Windows would not start it."""
        try:
            self.launch(self.exe(), self.plan.get("args") or [], self.root)
            return True
        except OSError as e:
            self.start_error = str(e)
            return False

    def whole(self, which):
        return all(self.where[n][which] in ("root",) or (which == "old" and self.where[n]["old"] == "none") for n in PROGRAM)

    # ---- the steps
    def wait_old(self):
        pid = int(self.plan.get("pid") or 0)
        ended = self.waiter.ended(90) if self.waiter else None
        if ended is None:  # no handle (the process had gone already, or not Windows): ask by its ID
            ended = self.wait_gone(lambda: [pid] if self.alive(pid) else [], 90)
        if not ended:
            raise SwapError("The running Fenrir did not close, so nothing was changed.")
        self.old_gone = True
        if not self.wait_gone(lambda: self.running(self.exe()), 20):  # its watcher reads the goodbye and ends by itself
            for p in self.running(self.exe()):
                self.end(p)
            if not self.wait_gone(lambda: self.running(self.exe()), 10):
                raise SwapError("Another Fenrir from this folder kept running, so nothing was changed.")
        self.mark("closed")
        if self.plan.get("window", True) and not self.wait_gone(lambda: self.webview(self.data), 10):  # the window's own processes
            for p in self.webview(self.data):
                self.end(p)

    def copy_in(self):
        need = sum(tree_size(self.stage / n)[0] for n in PROGRAM)
        if shutil.disk_usage(self.root).free < need + 64 * 2 ** 20:
            raise SwapError(f"There is not enough free disk space for the update: it needs about {need // 2 ** 20 + 64} MB.")
        for n in PROGRAM:
            _rm(self.root / (n + ".new"))
        for n in PROGRAM:
            src, dst = self.stage / n, self.root / (n + ".new")
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            if tree_size(src) != tree_size(dst):
                raise SwapError("The new files did not copy whole (an antivirus may have taken one), so nothing was changed.")
            self.where[n]["new"] = "dotnew"

    def swap_in(self):
        old = self.box / "old"
        _rm(old)
        old.mkdir(parents=True, exist_ok=True)
        for n in PROGRAM:
            cur, new = self.root / n, self.root / (n + ".new")
            if self.where[n]["old"] == "root":
                self.rename(cur, old / n)
                self.where[n]["old"] = "box"
            self.rename(new, cur)
            self.where[n]["new"] = "root"

    def back(self):
        """The old pair back in the folder, from wherever each half is. True when the folder holds the whole old build."""
        bad = self.box / "bad"
        bad.mkdir(parents=True, exist_ok=True)
        for n in reversed(PROGRAM):
            w = self.where[n]
            try:
                if w["new"] == "root":
                    _rm(bad / n)
                    self.rename(self.root / n, bad / n)
                    w["new"] = "bad"
                if w["old"] == "box":
                    self.rename(self.box / "old" / n, self.root / n)
                    w["old"] = "root"
            except OSError:
                continue
        return self.whole("old")

    def forward(self):
        """When the old pair cannot be made whole: the new pair whole instead, copied afresh from the stage where a half is
        missing. True when the folder holds the whole new build."""
        aside = self.box / "aside"
        for n in PROGRAM:
            w = self.where[n]
            if w["new"] == "root":
                continue
            cur = self.root / n
            try:
                if cur.exists():
                    aside.mkdir(parents=True, exist_ok=True)
                    _rm(aside / n)
                    self.rename(cur, aside / n)
                    if w["old"] == "root":
                        w["old"] = "aside"
                src = self.stage / n
                if src.is_dir():
                    shutil.copytree(src, cur)
                else:
                    shutil.copy2(src, cur)
                w["new"] = "root"
            except OSError:
                return False
        return self.whole("new")

    def discard_new(self):
        for n in PROGRAM:
            _rm(self.root / (n + ".new"))

    def end_all(self):
        for p in self.running(self.exe()):
            self.end(p)
        self.wait_gone(lambda: self.running(self.exe()), 15)

    def wait_healthy(self):
        """The new Fenrir answers on its port with the new build. It looks for a free port from the one its settings name
        (or --port), which can be lower than the one the old Fenrir ended up on, so both are where asking starts. Only
        ports something listens on are asked (procs.listening_ports): a closed local port on Windows costs a timeout, not
        a refusal, and nineteen of them made one round of asking take forty seconds. Without that list, the two likely
        ports are asked every round and the others every fifth."""
        ports = [int(p) for p in (self.plan.get("port"), self.plan.get("portSeen")) if p]
        base = min(ports) if ports else 8765
        likely = set(ports) or {base}
        want = str(self.plan.get("to") or "")
        t0, other, rounds = self.clock(), None, 0
        while self.clock() - t0 < self.health_wait:
            listen = self.listening()
            rounds += 1
            for port in range(base, max(likely) + 20):
                if (port not in listen) if listen is not None else (port not in likely and rounds % 5):
                    continue
                v = self.probe(port)
                if v == want:
                    self.mark("answered")
                    return True, ""
                if v and other is None:
                    other = self.clock()
            if other is not None and self.clock() - other >= 20:  # another build answers and the new one is nowhere: no use waiting on
                break
            self.sleep(1)
        if other:
            return False, "A Fenrir answered, but not the new build."
        return False, "The new Fenrir did not answer within two minutes."

    def broken(self, why):
        text = (f"{why} Fenrir could not put a whole copy of itself back. Install it again with Fenrir Setup from "
                "https://iksamxul.github.io/fenrir/download - your server, worlds and settings are not touched by that.")
        self.say("broken", error=text)
        self.message(text)
        return 3

    def run(self):
        self.waiter = self.make_waiter(int(self.plan.get("pid") or 0))  # before the word below: the old Fenrir surely runs now
        self.say("waiting")
        try:
            self.wait_old()
        except SwapError as e:
            if self.old_gone and not self.running(self.exe()):
                self.start()
            self.say("failed", error=str(e))
            return 1
        finally:
            if self.waiter:
                self.waiter.close()
        try:
            self.say("copying")
            self.copy_in()
            self.mark("copied")
            self.say("swapping")
            self.swap_in()
            self.mark("swapped")
        except Exception as e:  # noqa: BLE001 - the old Fenrir closed for this: it comes back as it was
            why = str(e) if isinstance(e, SwapError) else f"The new files could not be put in place ({e})."
            if self.back():
                self.discard_new()
                started = self.start()
                self.say("failed", error=why.rstrip(".") + "." + ("" if started else " Fenrir could not be started again: open it from the Start menu."))
                return 1
            if not self.forward():  # neither build whole by renames: copy the new one in whole from the stage
                return self.broken(why)
            self.discard_new()
        if self.start():
            self.mark("started")
            self.say("started")
            ok, why = self.wait_healthy()
        else:
            ok, why = False, f"Windows did not start the new Fenrir ({getattr(self, 'start_error', 'no reason given')})."
        if ok:
            self.say("done")
            return 0
        self.say("rollback", error=why)
        self.goodbye("update-rollback")  # the new Fenrir's watcher must not start it again
        self.end_all()
        if self.back():
            self.start()
            self.say("rolledback", error=why)
            return 2
        if self.forward():  # the old build cannot come back whole: the new one, whole, is still better than none
            self.start()
            self.say("kept", error=f"{why.rstrip('.')}, and the previous build could not be put back, so Fenrir kept the new one.")
            return 4
        return self.broken(why)


def finish(plan_path):
    """app.py --finish-update: never leaves the host without a Fenrir if it can help it."""
    try:
        plan = json.loads(Path(plan_path).read_text("utf-8"))
    except (OSError, ValueError):
        return 1
    try:
        return Swap(plan).run()
    except BaseException as e:  # noqa: BLE001 - the last line of defence
        try:
            write_status("failed", plan.get("status") or STATUS, id=plan.get("id"), error=f"The update stopped: {e}", **{"from": plan.get("from"), "to": plan.get("to")})
            root = Path(plan["root"])
            if (root / "Fenrir.exe").exists() and (root / "_internal").is_dir() and not running(root / "Fenrir.exe"):
                launch(root / "Fenrir.exe", plan.get("args") or [], root)
        except Exception:  # noqa: BLE001
            pass
        return 1
