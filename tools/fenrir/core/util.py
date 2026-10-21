"""Shared helpers: paths, subprocess, caching, event bus, background tasks."""
import contextvars
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from .actor import SERVER, current_actor, current_server

FROZEN = bool(getattr(sys, "frozen", False))


def _find_repo(start: Path) -> Path:
    """Walk up from the exe folder to the project folder (the one holding tools/ and server/)."""
    for p in [start, *start.parents]:
        if (p / "tools" / "firewall-radmin.ps1").exists() or (p / "server").is_dir():
            return p
    return start


if FROZEN:
    ROOT = Path(sys.executable).resolve().parent            # app/
    STATIC = Path(getattr(sys, "_MEIPASS", ROOT)) / "static"
    KNOWHOW = Path(getattr(sys, "_MEIPASS", ROOT)) / "knowhow"  # universal-modder's guides and notes (2.2), shipped beside static
    RECIPE_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "recipes"  # the recipe schema and the built-in recipes (3.0), shipped beside static
    REPO = _find_repo(ROOT)
    DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Fenrir"
else:
    ROOT = Path(__file__).resolve().parent.parent            # fenrir/
    STATIC = ROOT / "static"
    KNOWHOW = ROOT / "knowhow"
    RECIPE_DIR = ROOT / "recipes"
    REPO = ROOT.parent                                       # ATMMCP/
    from .oslayer import data_home
    DATA = data_home(ROOT)  # the checkout itself; on Linux systemd's /var/lib/fenrir, or $XDG_DATA_HOME/fenrir for a package's code
if os.environ.get("FENRIR_DATA"):  # tests (tests/_isolate.py) keep every data file in a folder of their own
    DATA = Path(os.environ["FENRIR_DATA"])
DATA.mkdir(parents=True, exist_ok=True)
EXPORTS = DATA / "exports"


def _game_event(text, level="info"):
    try:
        from .activity import log
        log("system", text, level)
    except Exception:  # noqa: BLE001
        pass


try:  # the game kit (fenrir/gamekit, shared with Fenrir Connect) keeps its records beside Fenrir's own, lines on the timeline
    import gamekit
    gamekit.set_home(DATA / "games")
    gamekit.on_event(_game_event)
    try:
        from connect import VERSION as _FENRIR_VERSION
        gamekit.set_app("Fenrir", _FENRIR_VERSION)
    except ImportError:
        pass
except ImportError:  # a tool that loads core without the kit on its path
    pass

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------- subprocess
def run_quiet(args, timeout=15, cwd=None):
    """Run a command with no console window; return combined stdout+stderr (never raises)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def powershell(script, timeout=20):
    return run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout)


def ps_quote(text):
    """Text as a PowerShell string in single quotes: every quote mark that ends one doubled, the typographic ones too
    (PowerShell takes U+2018 to U+201B for one, so a Windows account folder like C:\\Users\\Zoë’s ended the string)."""
    return "'" + re.sub("['\u2018\u2019\u201a\u201b]", lambda m: m.group(0) * 2, str(text)) + "'"


def is_junction(path):
    """os.path.isjunction, which came with Python 3.12: on 3.11 (the engine on Linux, server.md §3.3.1) a Windows junction is
    a reparse point that is not a symbolic link, and Linux has none."""
    fn = getattr(os.path, "isjunction", None)
    if fn is not None:
        return fn(path)
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) and not os.path.islink(path)  # FILE_ATTRIBUTE_REPARSE_POINT


def ascii_name(text):
    """A Windows Firewall rule's name in printable ASCII, for every rule Fenrir makes and every check that looks for one (2.9):
    netsh lists rules in the console's code page (437, 850, 1252...), so a name with Zoë or ’ in it never read back as it was
    made. Letters lose their accents (NFKD, the combining marks dropped), typographic apostrophes become ', double quotes go
    (netsh cannot take one in a name), and whatever is still outside printable ASCII becomes "-", a run of them one "-":
    "Zoë’s Valheim" is "Zoe's Valheim"."""
    s = re.sub("[\u2018\u2019\u201a\u201b\u2032]", "'", str(text))
    s = re.sub('["\u201c\u201d\u201e\u201f\u2033]', "", s)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    return re.sub("-{2,}", "-", re.sub(r"[^\x20-\x7e]", "-", s))


# --------------------------------------------------------------------------- caching
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def cached(key, ttl):
    """Memoize a zero-arg function for ttl seconds. cache_clear() invalidates everything."""
    def deco(fn):
        def wrapper(force=False):
            now = time.time()
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit and not force and now - hit[0] < ttl:
                return hit[1]
            val = fn()
            with _CACHE_LOCK:
                _CACHE[key] = (now, val)
            return val
        wrapper.cache_clear = lambda: _CACHE.pop(key, None)
        return wrapper
    return deco


def cache_clear():
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------- misc
def now_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def offline_uuid(name: str) -> str:
    """Java's UUID.nameUUIDFromBytes("OfflinePlayer:" + name) — what offline-mode servers assign."""
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    x = h.hex()
    return f"{x[:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:]}"


USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")


def valid_username(n: str) -> bool:
    return bool(n) and USERNAME_RE.fullmatch(n) is not None


def safe_filename(n: str, suffixes=(".jar",)) -> bool:
    return bool(n) and n == Path(n).name and ".." not in n and n.endswith(suffixes) and len(n) < 200


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text("utf-8")) if Path(path).exists() else default
    except Exception:  # noqa: BLE001
        return default


def write_text_whole(path: Path, text: str):
    """Whole or not at all: a temp file moved into place, so a crash mid-write never leaves half a file (player lists,
    server.properties). Windows refuses a move for a moment while another writer or a virus scanner has the file, so the
    move is tried a few times; only a file another program keeps open is written in place."""
    p = Path(path)
    tmp = p.with_name(f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp")  # one temp file per writer: two writes of one file never share it
    tmp.write_text(text, "utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.03 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    p.write_text(text, "utf-8")


def write_json(path: Path, data):
    write_text_whole(path, json.dumps(data, indent=2))


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------- event bus (SSE fan-out)
class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []

    def subscribe(self):
        q = queue.Queue(maxsize=4000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def emit(self, event, data):
        msg = (event, json.dumps(data))
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


BUS = EventBus()


# --------------------------------------------------------------------------- background tasks with progress
class TaskCancelled(Exception):
    """The host cancelled the task, or Fenrir stopped it when it reported nothing for too long: said as it is."""


class TaskRunner:
    """Runs long operations on a thread and streams progress over the bus as `task` events.

    A task can be cancelled (2.3): its progress callback raises TaskCancelled at its next report, and the programs it
    started (SteamCMD, winget: attach()) are ended at once. One that reports nothing for STALL seconds is cancelled the
    same way, and one that still has not ended a minute after a cancel is let go: the rest of Fenrir (sleep, updates,
    drills, one task per server) stops waiting for it. Restores and pack updates are never cancelled midway."""
    STALL = 1800
    KEEP_GOING = ("restore", "update", "updates", "pack")  # midway, these change the world or the pack: never cancelled

    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}
        self.ctrl = {}  # id -> {"event", "procs", "reason", "stall", "at"}
        self._local = threading.local()
        self._watching = False

    def _emit(self, t):
        BUS.emit("task", dict(t))

    def start(self, label, fn, kind="task", quiet=False, stall=None, keep=False):
        """fn(progress) where progress(pct=None, detail=None). Returns task id. quiet: no "finished" toast (the page that
        started it shows how it ended, as the mashup builder does). stall: seconds without a report before Fenrir stops it
        (STALL by default; 0 never). keep: never cancelled midway, as KEEP_GOING's kinds are (a server's update, which is
        one of its own tasks, changes its world and its files).

        3.0: the task runs as whoever started it (core/actor.py), on its own thread too, so every timeline line it writes
        and every task it starts in turn says who asked; the record says it as well, and which server the task is about
        (the one being worked on, else the one a per-server task is named after)."""
        tid = uuid.uuid4().hex[:10]
        keep = bool(keep) or str(kind).split(":")[0] in self.KEEP_GOING
        named = str(kind)[7:] if str(kind).startswith("server:") else ""  # the one task a server may run is named after it
        server = current_server() or named or None
        ctx = contextvars.copy_context()
        ctx.run(SERVER.set, server)  # its thread is about that server too, so the lines it writes say which
        t = {"id": tid, "label": label, "kind": kind, "status": "running", "pct": None, "detail": "", "started": time.time(), "result": None, "error": None,
             "quiet": bool(quiet), "cancellable": not keep, "beat": time.time(), "actor": current_actor().public(), "server": server}
        ctrl = {"event": threading.Event(), "procs": [], "reason": "", "stall": 0 if keep else (self.STALL if stall is None else stall), "at": None}
        with self.lock:
            old = time.time() - 3600  # finished tasks are kept for an hour, so the list does not grow for as long as Fenrir runs
            for k in [k for k, v in self.tasks.items() if v["status"] != "running" and v.get("finished", v["started"]) < old]:
                del self.tasks[k]
                self.ctrl.pop(k, None)
            self.tasks[tid] = t
            self.ctrl[tid] = ctrl
        self._emit(t)
        self._watch_start()

        def progress(pct=None, detail=None):
            t["beat"] = time.time()
            if ctrl["event"].is_set():
                raise TaskCancelled(ctrl["reason"] or "Cancelled")
            if pct is not None:
                t["pct"] = max(0, min(100, round(pct, 1)))
            if detail is not None:
                t["detail"] = str(detail)[:300]
            self._emit(t)

        def run():
            self._local.ctrl = ctrl
            try:
                res = fn(progress)
                if isinstance(res, dict) and res.get("ok") is False:
                    raise RuntimeError(res.get("error") or "failed")
                status, result, error = "done", res, None
            except Exception as e:  # noqa: BLE001
                status, result, error = "error", None, (ctrl["reason"] if ctrl["event"].is_set() else str(e))[:500]
            finally:
                self._local.ctrl = None
                ctrl["procs"].clear()
            if t.get("letGo"):  # Fenrir stopped waiting for it a while ago: how it ended now changes nothing on screen
                return
            t["status"], t["result"], t["error"] = status, result, error
            if status == "done":
                t["pct"] = 100
            if ctrl["event"].is_set():
                t["cancelled"] = True
            t["finished"] = time.time()
            self._emit(t)

        threading.Thread(target=ctx.run, args=(run,), daemon=True, name=f"task-{tid}").start()
        return tid

    # ---- cancelling (2.3)
    def attach(self, proc):
        """A program the running task started (a Popen): a cancel ends it and everything it started."""
        ctrl = getattr(self._local, "ctrl", None)
        if ctrl is not None and proc is not None:
            ctrl["procs"].append(proc)
            if ctrl["event"].is_set():
                _end_tree(proc)

    def check(self):
        """For a long loop that reports no progress: raises TaskCancelled when its task was cancelled."""
        ctrl = getattr(self._local, "ctrl", None)
        if ctrl is not None and ctrl["event"].is_set():
            raise TaskCancelled(ctrl["reason"] or "Cancelled")

    def cancel(self, tid, reason="You cancelled it"):
        """True when a running task that may be cancelled now stops at its next report (its programs end at once)."""
        with self.lock:
            t, ctrl = self.tasks.get(tid), self.ctrl.get(tid)
        if not t or not ctrl or t["status"] != "running" or not t.get("cancellable", True) or ctrl["event"].is_set():
            return False
        ctrl.update(reason=reason, at=time.time())
        ctrl["event"].set()
        for p in list(ctrl["procs"]):
            _end_tree(p)
        t["detail"], t["cancelling"] = "Stopping…", True
        self._emit(t)
        return True

    def _watch_start(self):
        with self.lock:
            if self._watching:
                return
            self._watching = True
        threading.Thread(target=self._watch, daemon=True, name="task-watch").start()

    def _watch(self, every=30):
        while True:
            time.sleep(every)
            try:
                self.watch_once()
            except Exception:  # noqa: BLE001 - the watcher never ends
                pass

    def watch_once(self, now=None):
        """A task that reported nothing for its stall time is cancelled; one cancelled a minute ago that has not ended is let
        go of. Returns what it did: [(id, "cancelled" | "let go")]."""
        now = now or time.time()
        done = []
        with self.lock:
            rows = [(tid, t, self.ctrl.get(tid)) for tid, t in self.tasks.items() if t["status"] == "running"]
        for tid, t, ctrl in rows:
            if not ctrl:
                continue
            if ctrl["event"].is_set():
                if ctrl["at"] and now - ctrl["at"] > 60 and not t.get("letGo"):
                    t.update(status="error", letGo=True, finished=now, cancelled=True,
                             error=f"{ctrl['reason']}. It did not end by itself, so Fenrir no longer waits for it.")
                    self._emit(t)
                    done.append((tid, "let go"))
            elif ctrl["stall"] and now - float(t.get("beat") or t["started"]) > ctrl["stall"]:
                if self.cancel(tid, f"It reported nothing for {ctrl['stall'] // 60} minutes, so Fenrir stopped it"):
                    done.append((tid, "cancelled"))
        return done

    def running(self, kind=None):
        with self.lock:
            return [t for t in self.tasks.values() if t["status"] == "running" and (kind is None or t["kind"] == kind)]

    def recent(self, n=20):
        with self.lock:
            return sorted(self.tasks.values(), key=lambda t: t["started"], reverse=True)[:n]


TASKS = TaskRunner()


def _end_tree(proc):
    """A program and everything it started, ended (a cancelled task's SteamCMD, its download)."""
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def content_length(headers):
    """The request body's length, 0 for a missing or malformed header (int() on it raised inside request handlers)."""
    try:
        return max(0, int(headers.get("Content-Length") or 0))
    except (TypeError, ValueError):
        return 0


class QuietHTTPServer(__import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer):
    """A threading HTTP server that writes no traceback when the client went away mid-request (a page reloading, a
    friend's app closing, a tunnel edge dropping the connection). Those filled fenrir.log with hundreds of identical
    socketserver tracebacks; every other error is still reported the usual way."""
    GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], self.GONE):
            return
        super().handle_error(request, client_address)


class BoundedHTTPServer(QuietHTTPServer):
    """At most `limit` connections handled at once (2.3): what a tunnel makes public (Fenrir Connect's listener, the phone
    remote, a folder site) could be sent thousands of slow connections, a thread each, until the host app choked. Past the
    limit a connection is closed at once; friends' apps and the page simply try again."""
    limit = 96

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(self.limit)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
