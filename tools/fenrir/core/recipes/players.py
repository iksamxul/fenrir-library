"""Players (recipes.md section 3.7): who is on a recipe's server, from its sources in order, the first that answers.

    a2s        Steam's A2S_INFO for the count, and A2S_PLAYER for names while someone is on (2.x, 2.5)
    log        a count line (the last one of this run), or join and leave lines with a "name" group
    rcon       an RCON command and a pattern whose group 1 lists the names
    rest       a JSON API on this PC, and a JSON pointer to the list (or the count)
    minecraft  the Server List Ping

None means "cannot tell": a stop from the phone or an AI then asks as if people were on (2.x core/remote.py). Player
commands (kick, ban, unban, mute) are the recipe's templates with {name} filled, once the name passed a check that keeps
a second command out of the line."""
import json
import re

from ..games.servers import ServerError
from . import values

NAME_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f;\"'`\\|&<>^%$]")  # what could end the command or start another
NAMES_MAX = 32


def _a2s(s, reach):
    port = reach.port(s["port"])
    info = reach.a2s(port)
    if not info:
        return None
    out = {"players": info["players"], "max": info["max"], "name": info["name"]}
    if s.get("names") and info["players"]:  # one more round trip, only while someone is on (2.5)
        who = reach.a2s_names(port)
        if who:
            out["names"] = [p["name"] for p in who][:NAMES_MAX]
    return out


def _log(s, reach):
    lines = reach.lines()
    if s.get("count"):
        for line in reversed(lines):  # the latest count this run said (2.x playersLog)
            m = values.search(s["count"], line)
            if m:
                return {"players": int(m.group(1)), "max": None, "name": None}
        return None
    on = {}
    for line in lines:
        m = values.search(s["join"], line)
        if m and m.group("name"):
            on[m.group("name")] = True
        elif s.get("leave"):
            m = values.search(s["leave"], line)
            if m and m.group("name"):
                on.pop(m.group("name"), None)
    return {"players": len(on), "max": None, "name": None, "names": list(on)[:NAMES_MAX]}


def _rcon(s, reach):
    if not s.get("parse"):
        return None
    m = values.search(s["parse"], reach.rcon(s["command"], reach.port(s["port"]) if s.get("port") else None))
    if not m:
        return None
    names = [x.strip() for x in (m.group(1) or "").split(",") if x.strip()]
    return {"players": len(names), "max": None, "name": None, "names": names[:NAMES_MAX]}


def _pointer(doc, path):
    for part in [p.replace("~1", "/").replace("~0", "~") for p in str(path or "").split("/")[1:]]:
        if isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        elif isinstance(doc, dict) and part in doc:
            doc = doc[part]
        else:
            return None
    return doc


def _rest(s, reach):
    signed = reach.auth(s.get("auth")) if s.get("auth") else None
    status, body = reach.http_get(reach.url(s["url"]), headers=signed) if signed else reach.http_get(reach.url(s["url"]))
    if status is None or not 200 <= status < 300:
        return None
    found = _pointer(json.loads(body), s.get("path"))
    if isinstance(found, bool):
        return None
    if isinstance(found, int):
        return {"players": found, "max": None, "name": None}
    if isinstance(found, list):
        names = [x if isinstance(x, str) else x.get("name") for x in found if isinstance(x, str) or isinstance(x, dict)]
        return {"players": len(found), "max": None, "name": None, "names": [n for n in names if isinstance(n, str)][:NAMES_MAX]}
    return None


def _minecraft(s, reach):
    return reach.slp(reach.port(s["port"]))


VIA = {"a2s": _a2s, "log": _log, "rcon": _rcon, "rest": _rest, "minecraft": _minecraft}


def count(doc, reach, settings=None):
    """{"players", "max", "name", "names"?} from the first source that answers, or None when none can tell. The max
    players come from the recipe's players.max setting when the source does not say."""
    for s in (doc.get("players") or {}).get("sources") or []:
        try:
            got = VIA[s["via"]](s, reach)
        except Exception:  # noqa: BLE001 - a source that breaks has not answered: the next one is asked
            got = None
        if got is not None:
            mx = (doc.get("players") or {}).get("max")
            if got.get("max") is None and mx and settings:
                got["max"] = settings.get(mx.split(".", 1)[1])
            return got
    return None


def can_count(doc):
    """It has a source that can count: stop when empty is offered only then (2.x servers.can_count)."""
    return bool((doc.get("players") or {}).get("sources"))


def command(doc, what, name):
    """A player command's line (kick, ban, unban, mute) with the name filled in, or ServerError: the game has no such
    command, or the name holds something that could end the command or start another."""
    tmpl = (doc.get("players") or {}).get(what)
    if not tmpl:
        raise ServerError(f"{doc.get('name') or 'This server'} has no {what} command Fenrir knows.")
    name = str(name or "")
    if not name.strip() or name != name.strip() or len(name) > 64 or NAME_BAD.search(name):
        raise ServerError("That is not a player's name Fenrir can put in a command.")
    return values.fill(tmpl, {"name": name})
