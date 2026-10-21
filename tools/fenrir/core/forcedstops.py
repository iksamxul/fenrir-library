"""Servers Fenrir Server had to end by force (3.0, docs/v3-design/server.md §3.2.2): asked to stop by its own command or
by Ctrl+C, a server under the service was still running when its grace time ran out, so its whole process tree was ended.
Its last minutes may not have been saved, and the Doctor says so plainly.

Under the service a stop prefers the server's own way (its console's stdin, RCON, its web API: core/recipes/drivers/
process.py service_steps), since whether Ctrl+C reaches a console in session 0 was never checked on a real PC. A forced end
is how that would show, so each one is kept: DATA/forced-stops.json, the newest KEEP, in the service only."""
import json
import threading
import time

from .util import DATA, write_text_whole

FILE = DATA / "forced-stops.json"
KEEP = 20
SHOWN_FOR = 7 * 86400  # s the Doctor keeps saying it
_lock = threading.Lock()


def _read():
    try:
        rows = json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("t"), (int, float))] if isinstance(rows, list) else []


def note(sid, name, grace=None, clock=time.time):
    """A server under the service ended by force after `grace` s: kept, and said on the timeline. Outside the service it is
    the stop's own message, as in 2.x, and nothing is kept."""
    from .desktop import in_service
    if not in_service():
        return False
    row = {"id": str(sid), "name": str(name or sid), "t": clock(), "grace": grace}
    with _lock:
        rows = (_read() + [row])[-KEEP:]
        try:
            write_text_whole(FILE, json.dumps(rows, indent=1))
        except OSError:
            pass
    try:
        from .activity import log
        log("server", f"{row['name']} did not stop by itself in time, so Fenrir Server ended it: its last minutes may not be saved", "warn")
    except Exception:  # noqa: BLE001 - the timeline never stands in the way of the stop
        pass
    return True


def recent(now=None):
    """The forced ends of the last SHOWN_FOR seconds, newest first."""
    now = time.time() if now is None else now
    return sorted((r for r in _read() if now - r["t"] < SHOWN_FOR), key=lambda r: r["t"], reverse=True)


def findings(now=None):
    """The Doctor's words for them: one finding a server, its newest forced end."""
    out, seen = [], set()
    for r in recent(now):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        when = time.strftime("%b %d %H:%M", time.localtime(r["t"]))
        wait = f" within {int(r['grace'])} s" if isinstance(r.get("grace"), (int, float)) else ""
        out.append((f"forcedstop-{r['id']}", "warn", f"Fenrir Server had to end {r['name']} by force",
                    f"On {when} {r['name']} was asked to stop and did not stop by itself{wait}, so Fenrir Server ended it. Whatever happened "
                    "in its last minutes may not be saved. If it keeps happening, give it more time to stop in its settings, or tell "
                    "Fenrir's makers which server it was."))
    return out
