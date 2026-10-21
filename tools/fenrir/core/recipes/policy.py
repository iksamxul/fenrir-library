"""The rules a schema cannot say (recipes.md section 5), checked on every recipe before Fenrir uses it, whatever its
source. Each problem says where it is and has a short code; nothing is fixed on the recipe's behalf.

- Anti-cheat (PLAN-V3 section 7, answers 1 and 20): a game whose anti-cheat cannot be switched off gets no recipe, whatever
  the recipe says about itself (the game kit's blocked lists, gamekit/catalog.py); a game the kit knows has anti-cheat must
  say so; and a recipe that says "off" must really keep every switch off. The driver checks the final command line and
  environment again (kept_off), since a setting's value lands there too.
- Where it comes from: hooks, resolvers and hook writers are built-in only; the library pins images and names a licence.
- Placeholders by place: the text friends and the phone see never holds a secret or a folder of this PC; {secret.NAME}
  only ever reaches this server's own sealed values, and only in the environment, its files and the connection string; a
  secret setting goes on the command line only with inArgs.
- Paths start in the server's own folders and never climb out. Programs are .exe on Windows: a .bat runs through cmd, which
  reads arguments its own way, so only your own server may (with 2.9's guard). Nothing runs through a shell otherwise.
- Downloads name real hosts; probes and REST calls reach 127.0.0.1 only; console lines are one plain command.
- A pattern from outside could hang the engine: no backreferences, no lookbehind, no repeat inside a repeat (regex_problem
  walks Python's own parse of it), and lines are cut at 4 KB before any pattern reads them (values.search)."""
import datetime
import ipaddress
import re
import urllib.parse
from re import _constants as sre_c
from re import _parser as sre_parse

from . import Problem, schema
from . import values as V

SOURCES = ("builtin", "library", "local", "imported")
PLATFORM_NAMES = {"windows": "Windows", "linux": "Linux"}
HOOKS = {"fivem": "fivem", "skyrimtogether": "skyrimtogether", "minecraft": None}  # the recipe each hook belongs to (None: kind minecraft)
RESOLVERS = {"fivem-page": "fivem"}  # the hook each download resolver belongs to
URI = re.compile(r"^(steam|fivem)://")  # the links a join may open on a friend's PC
PLAIN_LINE = re.compile(r"[&|<>^%\"\x00-\x1f\x7f]")  # 2.9 custom.PLAIN_STOP: one plain command, never a second one
BAD_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")  # controls, line breaks, text turned around
BAD_NOTES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")  # the same, lines allowed
NOTES = re.compile(r"^/(research/notes|firstRun/[0-9]+/writes/text|tests/live/notes)$")  # the only texts with more than one line
OWN_ID = re.compile(r"custom-[0-9a-f]{8}")  # 2.9's own servers
INSTANCE_ID = re.compile(r".+-[0-9a-f]{8}")  # a second server of a recipe (valheim-3f9a0c1e, recipes.md section 3.9)
WINDOWS_PROGRAMS, BATCH = (".exe", ".com"), (".bat", ".cmd")
LOCAL_URL = re.compile(r"^https?://127\.0\.0\.1(?::(?:[0-9]{1,5}|\{(?:setting|port)\.[A-Za-z0-9_-]{1,40}(?:\+[0-9]{1,5})?\}))?(?:/|$)")
REMOTE_URL = re.compile(r"^https://([A-Za-z0-9.-]+)(?::[0-9]{1,5})?/")
PORT_TEMPLATE = re.compile(r"\{setting\.([A-Za-z0-9_]{1,40})(?:\+([0-9]{1,5}))?\}")
ROOT = re.compile(r"^\{(install|data|saves|log|game|profile)\}(?=$|[\\/])")
DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# where each kind of placeholder may stand
PLACES = {
    "arg": {"setting", "port", "install", "data", "saves", "log", "game"},
    "program": {"install", "data", "game"},
    "env": {"setting", "port", "secret", "install", "data", "saves", "log", "game"},
    "path": {"setting", "port", "install", "data", "saves", "log", "game", "profile"},
    "file-text": {"setting", "port", "secret", "install", "data", "saves", "log", "game"},
    "connection": {"setting", "port", "secret"},
    "port": {"setting"},
    "join": {"setting", "port", "address"},
    "join-code": {"setting", "port", "address", "code"},
    "local-url": {"setting", "port"},
    "remote-url": {"setting"},
    "moderation": {"setting", "name"},
    "chat": {"setting", "name", "text"},
    "warn": {"setting", "minutes"},
    "plain": set(),
    "body": {"setting", "port"},
    "warn-body": {"setting", "port", "minutes"},
}
PLACE_SAY = {"arg": "the command line", "program": "a program's name", "env": "the environment", "path": "a path",
             "file-text": "a file's text", "connection": "the connection string", "port": "a port", "join": "the text for friends",
             "join-code": "the text for friends", "local-url": "an address on this PC", "remote-url": "a download address",
             "moderation": "a player command", "chat": "a chat line", "warn": "the warning line", "plain": "a plain console line",
             "body": "a request's body", "warn-body": "the warning's request body"}
SECRET_OK = {"env", "file-text", "connection"}  # where a secret may stand (and the command line, for a secret setting with inArgs)
GET_KINDS = {"steamcmd": {"game", "app"}, "download": {"game", "app", "database"}, "fromGame": {"game"}, "kit": {"game"},
             "image": {"docker"}, "minecraft": {"minecraft"}, "none": {"own"}}
SECTION_KINDS = {"docker": {"docker"}, "minecraft": {"minecraft"}, "credentials": {"database", "app"}, "connection": {"database", "app"}}
TYPE_KEYS = {"as": {"switch"}, "span": {"port"}, "choices": {"choice"}, "inArgs": {"secret"}, "min": set(V.NUMBERS), "max": set(V.NUMBERS),
             "minLength": {"text", "secret", "list"}, "maxLength": {"text", "secret", "list"}, "pattern": {"text", "secret", "list"},
             "patternSay": {"text", "secret", "list"}, "noChars": {"text", "secret", "list"}, "notIn": {"text", "secret"}}
DEFAULT_TYPES = {"text": str, "secret": str, "switch": bool, "number": int, "port": int, "duration": int, "list": list}
STEP_NEEDS = {"command": {"line"}, "rcon": {"command"}, "rest": {"url"}, "signal": {"signal"}, "wait": {"seconds"}, "ctrl-c": set(), "close": set(), "end": set()}
STEP_MAY = {"rest": {"method", "body", "auth"}}
WARN_DO = ("command", "rcon", "rest")  # a warning is a console line, or one of these steps: never one that stops the server
VIA = {"a2s": ({"port"}, {"port", "names"}), "log": (set(), {"count", "join", "leave"}), "rcon": ({"command"}, {"command", "parse", "port"}),
       "rest": ({"url"}, {"url", "path", "port", "auth"}), "minecraft": ({"port"}, {"port"})}
REPEATS = {sre_c.MAX_REPEAT, sre_c.MIN_REPEAT, getattr(sre_c, "POSSESSIVE_REPEAT", sre_c.MAX_REPEAT)}
ATOMIC = getattr(sre_c, "ATOMIC_GROUP", None)
_noeac = None


def _esc(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def view(doc, platform):
    """The recipe as it runs on one platform: platformOverrides merged over it there. run, stop and saves take the
    override's keys over their own (Valheim's Linux program and library path); get is replaced whole, since a recipe
    gets its server exactly one way, and so are firstRun and backup, whose programs' names differ by platform."""
    over = (doc.get("platformOverrides") or {}).get(platform) or {}
    v = {k: x for k, x in doc.items() if k != "platformOverrides"}
    for part in ("run", "stop", "saves"):
        if part in over:
            v[part] = {**(doc.get(part) or {}), **over[part]}
    for part in ("get", "firstRun", "backup"):
        if part in over:
            v[part] = over[part]
    return v


# ---------------------------------------------------------------------------------------------------- the regex guard
def regex_problem(rx):
    """Why a recipe's pattern could hang the engine or is not one, or None. Walks Python's own parse of it, so what is
    judged is what will run: no backreference or conditional on a group, no lookbehind, no repeat inside a repeat."""
    if not isinstance(rx, str) or not rx:
        return "is empty"
    if len(rx) > 200:
        return "is longer than 200 characters"
    try:
        re.compile(rx)
        tree = sre_parse.parse(rx)
    except (re.error, OverflowError, RecursionError, ValueError) as e:
        return f"is not a pattern Python reads ({e})"
    return _rx_walk(tree, False)


def _rx_walk(items, repeating):
    for op, av in items:
        said = None
        if op in (sre_c.GROUPREF, sre_c.GROUPREF_EXISTS):
            return "refers back to an earlier group, which can take very long to match"
        if op in (sre_c.ASSERT, sre_c.ASSERT_NOT):
            if av[0] < 0:
                return "looks behind, which a recipe's pattern may not"
            said = _rx_walk(av[1], repeating)
        elif op in REPEATS:
            many = av[1] > 1
            if many and repeating:
                return "repeats a part that already repeats, like (a+)+, which can take forever to match"
            said = _rx_walk(av[2], repeating or many)
        elif op == sre_c.SUBPATTERN:
            said = _rx_walk(av[-1], repeating)
        elif ATOMIC is not None and op == ATOMIC:
            said = _rx_walk(av, repeating)
        elif op == sre_c.BRANCH:
            said = next((s for s in (_rx_walk(b, repeating) for b in av[1]) if s), None)
        if said:
            return said
    return None


# ---------------------------------------------------------------------------------------------------- what a recipe holds
def _templates(v):
    """(where, text, place, roots) for every template in a recipe; roots are the folders a path there may start in."""
    out = []

    def put(where, text, place, roots=()):
        if isinstance(text, str):
            out.append((where, text, place, roots))
    run = v.get("run") or {}
    put("/run/program", run.get("program"), "program")
    put("/run/cwd", run.get("cwd"), "path", ("install", "data", "saves", "game"))
    for i, a in enumerate(run.get("args") or []):
        for j, x in enumerate(a.get("args") or []) if isinstance(a, dict) else []:
            put(f"/run/args/{i}/args/{j}", x, "arg")
        put(f"/run/args/{i}", a, "arg")
    for k, x in (run.get("env") or {}).items():
        put(f"/run/env/{_esc(k)}", x, "env")
    if isinstance(run.get("output"), dict):
        put("/run/output/file", run["output"].get("file"), "path", ("data", "install"))
    for i, st in enumerate(v.get("firstRun") or []):
        put(f"/firstRun/{i}/program", st.get("program"), "program")
        for j, x in enumerate(st.get("args") or []):
            put(f"/firstRun/{i}/args/{j}", x, "arg")
        put(f"/firstRun/{i}/writes/file", (st.get("writes") or {}).get("file"), "path", ("data", "install"))
        put(f"/firstRun/{i}/writes/text", (st.get("writes") or {}).get("text"), "file-text")
    for i, p in enumerate(v.get("ports") or []):
        put(f"/ports/{i}/port", p.get("port"), "port")
    put("/saves/path", (v.get("saves") or {}).get("path"), "path", ("data", "install", "profile"))
    for i, w in enumerate(v.get("files") or []):
        put(f"/files/{i}/file", w.get("file"), "path", ("install", "data", "saves"))
        put(f"/files/{i}/from", w.get("from"), "path", ("install", "data", "game"))
        for k, x in (w.get("set") or {}).items():
            put(f"/files/{i}/set/{_esc(k)}", x, "file-text")
        for j, x in enumerate(w.get("lines") or []):
            put(f"/files/{i}/lines/{j}", x, "file-text")
    lb = (v.get("tests") or {}).get("loopback") or {}  # the live run's way to listen on 127.0.0.1: the same rules as the rest
    for i, x in enumerate(lb.get("args") or []):
        put(f"/tests/loopback/args/{i}", x, "arg")
    for i, w in enumerate(lb.get("files") or []):
        put(f"/tests/loopback/files/{i}/file", w.get("file"), "path", ("install", "data", "saves"))
        for k, x in (w.get("set") or {}).items():
            put(f"/tests/loopback/files/{i}/set/{_esc(k)}", x, "file-text")
    for part in ("any", "all"):
        for i, pr in enumerate((v.get("ready") or {}).get(part) or []):
            put(f"/ready/{part}/{i}/http/url", (pr.get("http") or {}).get("url"), "local-url")
    put("/health/probe/http/url", (((v.get("health") or {}).get("probe") or {}).get("http") or {}).get("url"), "local-url")
    pl = v.get("players") or {}
    for i, s in enumerate(pl.get("sources") or []):
        put(f"/players/sources/{i}/url", s.get("url"), "local-url")
    for k in ("kick", "ban", "unban", "mute"):
        put(f"/players/{k}", pl.get(k), "moderation")
    for k, lf in (pl.get("lists") or {}).items():
        put(f"/players/lists/{k}/file", lf.get("file"), "path", ("install", "data", "saves"))
    stop = v.get("stop") or {}
    for i, st in enumerate(stop.get("steps") or []):
        put(f"/stop/steps/{i}/url", st.get("url"), "local-url")
        put(f"/stop/steps/{i}/line", st.get("line"), "plain")
        put(f"/stop/steps/{i}/command", st.get("command"), "plain")
        put(f"/stop/steps/{i}/body", st.get("body"), "body")
    warn = stop.get("warn")
    put("/stop/warn", warn, "warn")
    if isinstance(warn, dict):  # a warning as a step: its line or command is a warning line, its address on this PC
        put("/stop/warn/line", warn.get("line"), "warn")
        put("/stop/warn/command", warn.get("command"), "warn")
        put("/stop/warn/url", warn.get("url"), "local-url")
        put("/stop/warn/body", warn.get("body"), "warn-body")
    put("/mods/into", (v.get("mods") or {}).get("into"), "path", ("install", "data", "saves"))
    j = v.get("join") or {}
    put("/join/text", j.get("text"), "join")
    put("/join/withCode/text", (j.get("withCode") or {}).get("text"), "join-code")
    for i, x in enumerate(j.get("steamArgs") or []):
        put(f"/join/steamArgs/{i}", x, "join")
    put("/join/uri", j.get("uri"), "join")
    put("/chat/send/command", ((v.get("chat") or {}).get("send") or {}).get("command"), "chat")
    for i, c in enumerate((v.get("console") or {}).get("commands") or []):
        put(f"/console/commands/{i}/line", c.get("line"), "plain")
    put("/connection/template", (v.get("connection") or {}).get("template"), "connection")
    for part in ("dump", "restore"):
        b = (v.get("backup") or {}).get(part) or {}
        put(f"/backup/{part}/program", b.get("program"), "program")
        for i, x in enumerate(b.get("args") or []):
            put(f"/backup/{part}/args/{i}", x, "arg")
    put("/backup/dump/file", ((v.get("backup") or {}).get("dump") or {}).get("file"), "path", ("data",))
    d = v.get("docker") or {}
    for k, x in (d.get("env") or {}).items():
        put(f"/docker/env/{_esc(k)}", x, "env")
    for i, x in enumerate(d.get("command") or []):
        put(f"/docker/command/{i}", x, "arg")
    inst = (v.get("minecraft") or {}).get("installer") or {}
    put("/minecraft/installer/url", inst.get("url"), "remote-url")
    for i, x in enumerate(inst.get("args") or []):
        put(f"/minecraft/installer/args/{i}", x, "arg")
    for i, sw in enumerate((v.get("antiCheat") or {}).get("keepOff") or []):
        for j, x in enumerate(sw.get("args") or []):
            put(f"/antiCheat/keepOff/{i}/args/{j}", x, "arg")
    return out


def _regexes(v):
    """(where, pattern, the groups it must have) for every pattern in a recipe."""
    out = [(f"/settings/{i}/pattern", f["pattern"], ()) for i, f in enumerate(v.get("settings") or []) if "pattern" in f]
    for part in ("any", "all"):
        out += [(f"/ready/{part}/{i}/log", p["log"], ()) for i, p in enumerate((v.get("ready") or {}).get(part) or []) if "log" in p]
    probe = (v.get("health") or {}).get("probe") or {}
    if "log" in probe:
        out.append(("/health/probe/log", probe["log"], ()))
    for i, s in enumerate((v.get("players") or {}).get("sources") or []):
        for k, need in (("count", (1,)), ("join", ("name",)), ("leave", ("name",)), ("parse", (1,))):
            if k in s:
                out.append((f"/players/sources/{i}/{k}", s[k], need))
    code = ((v.get("join") or {}).get("withCode") or {}).get("code")
    if code is not None:
        out.append(("/join/withCode/code", code, (1,)))
    rcv = ((v.get("chat") or {}).get("receive") or {}).get("log")
    if rcv is not None:
        out.append(("/chat/receive/log", rcv, ("name", "text")))
    asset = ((v.get("updates") or {}).get("feed") or {}).get("asset")
    if asset is not None:
        out.append(("/updates/feed/asset", asset, ()))
    return out


# ---------------------------------------------------------------------------------------------------- the checks
def _text(x, at, add):
    """No control character, no line break where one line belongs, and nothing that turns text around in the trust prompt
    (the bidi controls of Trojan Source). Python's $ matches before a final line break, so the schema's own one-line
    patterns let "valheim\\n" through; this does not."""
    if isinstance(x, str):
        if (BAD_NOTES if NOTES.match(at) else BAD_TEXT).search(x):
            add(at, "holds a control character, a line break where one line belongs, or a character that turns text around", "text")
    elif isinstance(x, list):
        for i, y in enumerate(x):
            _text(y, f"{at}/{i}", add)
    elif isinstance(x, dict):
        for k, y in x.items():
            if BAD_TEXT.search(str(k)):
                add(f"{at}/{_esc(k)}", "has a name with a control character in it", "text")
            _text(y, f"{at}/{_esc(k)}", add)


def _source(v, source, add):
    builtin, hook = source == "builtin", v.get("hooks")
    if hook and not builtin:
        add("/hooks", "names Python code of Fenrir's own, which only built-in recipes may do", "hook")
    elif hook and ((HOOKS[hook] and v["id"] != HOOKS[hook]) or (HOOKS[hook] is None and v["kind"] != "minecraft")):
        add("/hooks", f"belongs to the {HOOKS[hook] or 'Minecraft'} recipe", "hook")
    res = ((v.get("get") or {}).get("download") or {}).get("resolver")
    if res and not builtin:
        add("/get/download/resolver", "is Python code of Fenrir's own, which only built-in recipes may use", "hook")
    elif res and hook != RESOLVERS.get(res):
        add("/get/download/resolver", f"goes with the {RESOLVERS.get(res)} hook", "hook")
    for i, w in enumerate(v.get("files") or []):
        if w.get("format") == "hook" or "hook" in w:
            if not builtin:
                add(f"/files/{i}", "a hook writer is Python code of Fenrir's own: built-in recipes only", "hook")
            elif w.get("format") != "hook" or str(w.get("hook") or "").split(".")[0] != hook:
                add(f"/files/{i}/hook", "has to be format hook, and one of this recipe's own hook's writers", "hook")
    if OWN_ID.fullmatch(v["id"]):
        if v["kind"] != "own":
            add("/id", "custom-<8 hex> is how your own servers are named (2.9)", "id")
    elif INSTANCE_ID.fullmatch(v["id"]):
        add("/id", "ends like a second server's name (valheim-3f9a0c1e), so the two could be mixed up", "id")
    if source == "library" and not v.get("license"):
        add("/license", "the library needs the recipe's licence (CC0-1.0, MIT or Apache-2.0)", "license")


def _noeac_ids():
    """Steam ids the game kit plays with the anti-cheat off (its noeac policy): a server recipe for one says off too."""
    global _noeac
    if _noeac is None:
        from gamekit import catalog
        _noeac = {sid for r in catalog.RECIPES.values() if r.get("policy") == "noeac" for sid in r.get("steam") or []}
    return _noeac


def _anticheat(v, add):
    from gamekit import catalog
    ac, client = v.get("antiCheat") or {}, v.get("client") or {}
    for store, ids, blocked in (("steam", client.get("steam") or [], catalog.BLOCKED_STEAM), ("epic", client.get("epic") or [], catalog.BLOCKED_EPIC)):
        for i, gid in enumerate(ids):
            if gid in blocked:
                add(f"/client/{store}/{i}", f"is {blocked[gid]}, whose anti-cheat cannot be switched off: Fenrir hosts no server for it", "anticheat")
    if v["kind"] == "game" and catalog.BLOCKED_NAMES.match(catalog.squeeze(v.get("name"))):
        add("/name", "names a game whose anti-cheat cannot be switched off: Fenrir hosts no server for it", "anticheat")
    if ac.get("status") != "off" and any(gid in _noeac_ids() for gid in client.get("steam") or []):
        add("/antiCheat/status", "this game has anti-cheat (Fenrir's game kit plays its mods with it off): say off, and the switch Fenrir keeps off", "anticheat")
    if ac.get("status") != "off":
        return
    run, what = v.get("run") or {}, ac.get("what") or "its anti-cheat"
    plain = [a for a in run.get("args") or [] if isinstance(a, str)]
    every = plain + [x for a in run.get("args") or [] if isinstance(a, dict) for x in a.get("args") or []]
    env = {**(run.get("env") or {}), **((v.get("docker") or {}).get("env") or {})}
    for i, sw in enumerate(ac.get("keepOff") or []):
        at = f"/antiCheat/keepOff/{i}"
        if "noArg" in sw:
            if any(_is_flag(a, sw["noArg"]) for a in every):
                add(at, f"the command line passes {sw['noArg']}, which turns {what} on", "anticheat")
        elif "args" in sw:
            n = len(sw["args"])
            if not any(plain[k:k + n] == sw["args"] for k in range(len(plain) - n + 1)):
                add(at, f"the command line has to carry {' '.join(sw['args'])} every time, outside any condition, or {what} stays on", "anticheat")
        elif "file" in sw:
            writers = [w for w in v.get("files") or [] if w.get("file") == sw["file"]]
            sets = [(w, k, x) for w in writers for k, x in (w.get("set") or {}).items() if k.lower() == sw["key"].lower()]
            if not any(x == sw["value"] and k == sw["key"] and w.get("when", "start") == "start" for w, k, x in sets):
                add(at, f"no writer sets {sw['key']} to {sw['value']} in {sw['file']} at every start, so {what} could be on", "anticheat")
            for w, k, x in sets:
                if x != sw["value"]:
                    add(at, f"a writer sets {k} to {x} in {sw['file']}, and it has to stay {sw['value']}", "anticheat")
        elif env.get(sw["env"]) != sw["value"]:
            add(at, f"the environment has to set {sw['env']} to {sw['value']} every time, or {what} could be on", "anticheat")


def _is_flag(arg, flag):
    a, f = str(arg).lower(), flag.lower()
    return a == f or a.startswith((f + "=", f + ":"))


def kept_off(doc, argv, env=None):
    """At start, after every placeholder is filled: why this command line or environment would turn the anti-cheat on, or
    None. A setting's value can land on the command line whole (a world named -secure), which no static check sees."""
    ac = doc.get("antiCheat") or {}
    if ac.get("status") != "off":
        return None
    what = ac.get("what") or "its anti-cheat"
    rest = [str(a) for a in list(argv)[1:]]
    for sw in ac.get("keepOff") or []:
        if "noArg" in sw and any(_is_flag(a, sw["noArg"]) for a in rest):
            return f"The command line would pass {sw['noArg']}, which turns {what} on, so the server was not started."
        if "args" in sw and not V.parse(" ".join(sw["args"])):
            n = len(sw["args"])
            if not any(rest[k:k + n] == sw["args"] for k in range(len(rest) - n + 1)):
                return f"The command line is missing {' '.join(sw['args'])}, which keeps {what} off, so the server was not started."
        if "env" in sw and env is not None and env.get(sw["env"]) != sw["value"]:
            return f"{sw['env']} would not be {sw['value']}, which keeps {what} off, so the server was not started."
    return None


def _settings(v, add):
    fl = V.fields(v)
    seen = set()
    for i, f in enumerate(v.get("settings") or []):
        at, kind = f"/settings/{i}", f["type"]
        if f["key"] in seen:
            add(f"{at}/key", f"{f['key']} is there twice", "setting")
        seen.add(f["key"])
        for k, kinds in TYPE_KEYS.items():
            if k in f and kind not in kinds:
                add(f"{at}/{k}", f"means nothing for a {kind} setting", "setting")
        if kind == "choice" and not f.get("choices"):
            add(at, "a choice needs its choices", "setting")
        if f.get("min") is not None and f.get("max") is not None and f["min"] > f["max"]:
            add(at, "min is above max", "setting")
        if f.get("maxLength") is not None and f.get("minLength", 0) > f["maxLength"]:
            add(at, "minLength is above maxLength", "setting")
        for other in f.get("notIn") or []:
            if other == f["key"] or other not in fl:
                add(f"{at}/notIn", f"names {other}, which is not another of this recipe's settings", "setting")
        if kind == "secret" and f.get("default") not in (None, ""):
            add(f"{at}/default", "a secret's value is never written in a recipe", "secret")
        elif "default" in f:
            d, want = f["default"], DEFAULT_TYPES.get(kind)
            if kind == "choice":
                ok = any(schema.equal(d, c["value"]) for c in f.get("choices") or [])
            else:
                ok = isinstance(d, want) and not (want is int and isinstance(d, bool))
                ok = ok and (kind != "list" or all(isinstance(x, str) for x in d))
            said = None if not ok else V.limit_problem(f, d)
            if not ok or said:
                add(f"{at}/default", said or f"is not a {kind} setting's value", "setting")
    for i, n in enumerate(v.get("needs") or []):
        if n.get("setting") and n["setting"] not in fl:
            add(f"/needs/{i}/setting", f"names {n['setting']}, which this recipe has no setting for", "setting")
        if n.get("game") and "fromGame" not in (v.get("get") or {}):
            add(f"/needs/{i}/game", "only a server that comes with the game needs the game", "setting")
    mx = (v.get("players") or {}).get("max")
    if mx and (fl.get(mx.split(".", 1)[1]) or {}).get("type") != "number":
        add("/players/max", "has to name one of this recipe's number settings", "setting")
    for i, c in enumerate(v.get("credentials") or []):
        if (fl.get(c["setting"]) or {}).get("type") != "secret":
            add(f"/credentials/{i}/setting", "has to name one of this recipe's secret settings", "secret")
    for i, a in enumerate((v.get("run") or {}).get("args") or []):
        if isinstance(a, dict) and a["if"].split(".", 1)[1] not in fl:
            add(f"/run/args/{i}/if", f"names {a['if']}, which this recipe has no setting for", "setting")
    for k, x in (((v.get("tests") or {}).get("harness") or {}).get("settings") or {}).items():
        if k not in fl:
            add(f"/tests/harness/settings/{_esc(k)}", "names a setting this recipe does not have", "tests")
            continue
        try:
            said = V.limit_problem(fl[k], V.coerce(fl[k], x))
        except V.SettingError as e:
            said = str(e)
        if said:
            add(f"/tests/harness/settings/{_esc(k)}", said, "tests")


def _ports(v, add):
    fl, ids = V.fields(v), set()
    for i, p in enumerate(v.get("ports") or []):
        if p["id"] in ids:
            add(f"/ports/{i}/id", f"{p['id']} is there twice", "port")
        ids.add(p["id"])
        if isinstance(p["port"], str):
            m = PORT_TEMPLATE.fullmatch(p["port"])
            f = fl.get(m.group(1)) if m else None
            if not m or f is None or f["type"] not in ("port", "number"):
                add(f"/ports/{i}/port", "has to be a number, or one port setting like {setting.port+1}", "port")
            elif f["type"] == "port" and int(m.group(2) or 0) >= f.get("span", 1):
                add(f"/ports/{i}/port", f"goes past the {f.get('span', 1)} port(s) the {f['key']} setting says it takes (span)", "port")
    if (v.get("run") or {}).get("portEnv") and not ids:
        add("/run/portEnv", "carries the first port, and this recipe has none", "port")
    return ids


def _places(v, add):
    fl, port_ids = V.fields(v), {p["id"] for p in v.get("ports") or []}
    out_file = isinstance((v.get("run") or {}).get("output"), dict)
    game = bool({"fromGame", "kit"} & set(v.get("get") or {}))
    for where, text, place, _roots in _templates(v):
        for kind, name, plus in V.parse(text):
            ph = "{" + (f"{kind}.{name}" if name else kind) + (f"+{plus}" if plus else "") + "}"
            if kind not in PLACES[place]:
                add(where, f"{ph} does not belong in {PLACE_SAY[place]}", "secret" if kind == "secret" else "placeholder")
            elif kind == "setting":
                f = fl.get(name)
                if f is None:
                    add(where, f"{ph} names a setting this recipe does not have", "placeholder")
                elif plus and f["type"] not in V.NUMBERS:
                    add(where, f"{ph} adds to a setting that is not a number", "placeholder")
                elif f["type"] == "secret" and place not in SECRET_OK and not (place == "arg" and f.get("inArgs")):
                    add(where, f"{ph} is a secret: it goes on the command line only with inArgs, and never into a text friends or the phone see", "secret")
            elif kind == "port" and name not in port_ids:
                add(where, f"{ph} names a port this recipe does not have", "placeholder")
            elif kind == "log" and (not out_file or where == "/run/output/file"):
                add(where, "{log} is the file run.output names, and there is none to name here", "placeholder")
            elif kind == "saves" and (not v.get("saves") or where == "/saves/path"):
                add(where, "{saves} is the save folder saves.path names, and there is none to name here", "placeholder")
            elif kind == "game" and not game:
                add(where, "{game} is the game's own folder, which only a server that comes with the game or a kit uses", "placeholder")
            elif kind == "profile" and not (where == "/saves/path" and (v.get("saves") or {}).get("how") == "profile"):
                add(where, "{profile} may stand only in a save path the game keeps under the profile (saves.how profile)", "placeholder")
        if place == "plain" and where.startswith("/stop/steps/") and PLAIN_LINE.search(text):
            add(where, "has to be one plain command: none of & | < > ^ % or quote marks", "stop")
        if place == "warn" and PLAIN_LINE.search(text):
            add(where, "has to be one plain console line: none of & | < > ^ % or quote marks", "stop")
        if place == "local-url" and not LOCAL_URL.match(text):
            add(where, "has to be an address on this PC: http://127.0.0.1", "url")
        if place == "remote-url":
            m = REMOTE_URL.match(text)
            if not m or _bad_host(m.group(1)):
                add(where, "has to be an https address on a named host", "download")
        if where == "/join/uri" and not URI.match(text):
            add(where, "a join link may open steam:// or fivem:// only", "uri")


def _rest_problem(rest):
    """A path's text after its first folder, judged with its placeholders standing in as plain names (the guard at run
    time judges their values)."""
    return V.path_text_problem(V.PLACEHOLDER.sub("x", rest))


def _relative_problem(rel):
    if not rel or DRIVE.match(rel) or rel.startswith(("\\", "/")) or ROOT.match(rel):
        return "has to be a plain path inside the server's own folder"
    return V.path_text_problem(V.PLACEHOLDER.sub("x", rel))


def _paths(v, source, add):
    own_local = v["kind"] == "own" and source == "local"
    for where, text, place, roots in _templates(v):
        said = None
        if place == "path":
            m = ROOT.match(text)
            if m and m.group(1) not in roots:
                said = f"starts in {{{m.group(1)}}}, and here a path starts in " + ", ".join("{" + r + "}" for r in roots)
            elif m:
                said = _rest_problem(text[m.end():])
            elif own_local and where in ("/run/cwd", "/saves/path") and DRIVE.match(text):
                said = V.path_text_problem(text)
            else:
                said = "has to start in one of the server's own folders: " + ", ".join("{" + r + "}" for r in roots)
        elif place == "program":
            m = re.match(r"^\{(install|data|game)\}[\\/]", text)
            if own_local and where == "/run/program" and DRIVE.match(text):
                said = V.path_text_problem(text)
            else:
                said = _relative_problem(text[m.end():] if m else text)
        if said:
            add(where, said, "path")
    g, run, saves = v.get("get") or {}, v.get("run") or {}, v.get("saves") or {}
    rels = [("/get/fromGame/file", (g.get("fromGame") or {}).get("file")), ("/get/kit/from", (g.get("kit") or {}).get("from"))]
    rels += [(f"/get/kit/keep/{i}", x) for i, x in enumerate((g.get("kit") or {}).get("keep") or [])]
    rels += [(f"/run/realPrograms/{i}", x) for i, x in enumerate(run.get("realPrograms") or [])]
    rels += [(f"/saves/exclude/{i}", str(x).replace("*", "x")) for i, x in enumerate(saves.get("exclude") or [])]
    for where, rel in rels:
        said = _relative_problem(rel) if isinstance(rel, str) else None
        if said:
            add(where, said, "path")
    if saves.get("how") == "picked" and not own_local:
        add("/saves/how", "only your own server's save folder is picked by the host", "path")
    if "fromGame" in g:  # {install} is the game's own folder there, and Fenrir writes nothing in it
        writes = [(f"/files/{i}/file", w.get("file")) for i, w in enumerate(v.get("files") or [])]
        writes += [(f"/firstRun/{i}/writes/file", (st.get("writes") or {}).get("file")) for i, st in enumerate(v.get("firstRun") or [])]
        writes += [("/mods/into", (v.get("mods") or {}).get("into"))] + [("/saves/path", saves.get("path"))]
        for where, text in writes:
            if isinstance(text, str) and text.startswith("{install}"):
                add(where, "is in the game's own folder, which Fenrir never writes in: use {data}", "path")


def _run(v, platform, add):
    kind, run = v["kind"], v.get("run") or {}
    on = PLATFORM_NAMES[platform]
    others = [(f"/firstRun/{i}/program", st["program"]) for i, st in enumerate(v.get("firstRun") or [])]
    others += [(f"/backup/{k}/program", b["program"]) for k, b in (v.get("backup") or {}).items() if isinstance(b, dict) and b.get("program")]
    for where, prog in others:  # Windows runs a .bat through cmd, which reads the arguments its own way: only a .exe there
        low = str(prog).lower()
        if platform == "windows" and not low.endswith(WINDOWS_PROGRAMS):
            add(where, "on Windows a program here has to be a .exe", "run")
        elif platform != "windows" and low.endswith(WINDOWS_PROGRAMS + BATCH):
            add(where, f"is a Windows program, and the recipe says it runs on {on}", "run")
    if kind in ("game", "app", "database", "own") and not run.get("program"):
        add("/run/program", "names no program to run", "run")
        return
    prog = str(run.get("program") or "").lower()
    if not prog:
        return
    batch = prog.endswith(BATCH)
    if run.get("shell") == "cmd" and not (kind == "own" and batch and platform == "windows"):
        add("/run/shell", "cmd is only for your own server's .bat or .cmd file on Windows, with 2.9's guard", "run")
    if platform == "windows":
        if batch and run.get("shell") != "cmd":
            add("/run/program", "a .bat or .cmd runs through cmd, which reads the arguments its own way: only your own server may, with shell cmd", "run")
        elif not batch and not prog.endswith(WINDOWS_PROGRAMS):
            add("/run/program", "on Windows the program has to be a .exe", "run")
    elif prog.endswith(WINDOWS_PROGRAMS + BATCH):
        add("/run/program", f"is a Windows program, and the recipe says it runs on {on}: name the {on} one in platformOverrides.{platform}", "run")
    if run.get("console") == "keys" and platform != "windows":
        add("/run/console", f"key presses into its own console work on Windows only, not on {on}", "run")
    elif run.get("console") == "keys" and run.get("shell") != "cmd":
        add("/run/console", "key presses reach a server through the console cmd hosts it in: your own server, with shell cmd", "run")


def _steps(v, platform, add):
    stop = v.get("stop") or {}
    console = (v.get("run") or {}).get("console")
    for i, st in enumerate(stop.get("steps") or []):
        at, do = f"/stop/steps/{i}", st["do"]
        need = STEP_NEEDS[do]
        missing, extra = need - set(st), set(st) - need - STEP_MAY.get(do, set()) - {"do"}
        if missing:
            add(at, f"a {do} step needs {', '.join(sorted(missing))}", "stop")
        if extra:
            add(at, f"{', '.join(sorted(extra))} means nothing in a {do} step", "stop")
        if do == "signal" and platform == "windows" and v["kind"] != "docker":
            add(at, "signals are for Linux: on Windows the first step is ctrl-c", "stop")
        if do == "command" and v["kind"] not in ("minecraft", "docker") and console not in ("stdin", "keys"):
            add(at, "a console command needs a console to type it into: run.console stdin or keys", "stop")
    words = set(stop.get("stopWords") or [])
    words |= {str(s.get("line") or "").split()[0].lower() for s in stop.get("steps") or [] if s["do"] == "command" and str(s.get("line") or "").split()}
    for i, c in enumerate((v.get("console") or {}).get("commands") or []):
        first = str(c.get("line") or "").strip().lstrip("/").split()
        if first and first[0].lower() in words:
            add(f"/console/commands/{i}/line", "would end the server behind Fenrir's back: its Stop and Restart do that", "stop")


def _secret_problem(where, ref, v, add):
    """A secret a recipe names (setting.KEY or secret.NAME) has to be one it has: a secret setting, or a value it makes."""
    kind, _, name = str(ref or "").partition(".")
    if kind == "setting":
        if (V.fields(v).get(name) or {}).get("type") != "secret":
            add(where, "has to name one of this recipe's secret settings", "secret")
    elif kind == "secret":
        if name not in {s.get("name") for s in v.get("secrets") or []}:
            add(where, f"names secret.{name}, which this recipe does not make: add it to secrets", "secret")
    else:
        add(where, "has to name a secret setting (setting.KEY) or a value the recipe makes (secret.NAME)", "secret")


def _auth_and_warn(v, add):
    """A REST call's sign-in names a secret the server keeps sealed, never one written down; a recipe's made secrets have
    names of their own; a warning step is a console command, an RCON command or a REST call, and never a stop."""
    names = [s.get("name") for s in v.get("secrets") or []]
    for i, n in enumerate(names):
        if n in names[:i]:
            add(f"/secrets/{i}/name", f"{n} is there twice", "secret")
    stop = v.get("stop") or {}
    for i, st in enumerate(stop.get("steps") or []):
        if st.get("auth"):
            _secret_problem(f"/stop/steps/{i}/auth/password", st["auth"].get("password"), v, add)
    for i, s in enumerate((v.get("players") or {}).get("sources") or []):
        if s.get("auth"):
            _secret_problem(f"/players/sources/{i}/auth/password", s["auth"].get("password"), v, add)
    warn = stop.get("warn")
    if not isinstance(warn, dict):
        return
    do = warn.get("do")
    if do not in WARN_DO:
        add("/stop/warn/do", "a warning tells players: a console command, an RCON command or a REST call", "stop")
        return
    need = STEP_NEEDS[do]
    missing, extra = need - set(warn), set(warn) - need - STEP_MAY.get(do, set()) - {"do"}
    if missing:
        add("/stop/warn", f"a {do} warning needs {', '.join(sorted(missing))}", "stop")
    if extra:
        add("/stop/warn", f"{', '.join(sorted(extra))} means nothing in a {do} warning", "stop")
    if do == "command" and v["kind"] not in ("minecraft", "docker") and (v.get("run") or {}).get("console") not in ("stdin", "keys"):
        add("/stop/warn", "a console warning needs a console to type it into: run.console stdin or keys", "stop")
    if do == "rcon" and not v.get("rcon") and v["kind"] != "minecraft":
        add("/stop/warn", "speaks RCON, and the recipe does not say how: an rcon section with its port and password", "rcon")
    if warn.get("auth"):
        _secret_problem("/stop/warn/auth/password", warn["auth"].get("password"), v, add)


def _probe(at, pr, ports, add, health=False):
    asks = [k for k in ("log", "port", "query", "http", "alive") if k in pr]
    if len(asks) != 1:
        add(at, "a probe asks one thing: a log line, a port, a query, an address or a time alive", "probe")
    if ("on" in pr) != ("query" in pr):
        add(at, "a query needs on, the port to ask, and on goes with a query only", "probe")
    for k in ("port", "on"):
        if k in pr and pr[k] not in ports:
            add(f"{at}/{k}", f"names {pr[k]}, which is not one of this recipe's ports", "port")
    if health and "alive" in pr:
        add(at, "running for a while says nothing about health: ask a port, a query or an address", "probe")


def _probes(v, ports, add):
    ready = v.get("ready")
    if ready is not None and not (ready.get("any") or ready.get("all")):
        add("/ready", "needs any or all: the probes that say it is ready", "probe")
    for part in ("any", "all"):
        for i, pr in enumerate((ready or {}).get(part) or []):
            _probe(f"/ready/{part}/{i}", pr, ports, add)
    if v.get("health"):
        _probe("/health/probe", v["health"]["probe"], ports, add, health=True)
    for i, s in enumerate((v.get("players") or {}).get("sources") or []):
        need, may = VIA[s["via"]]
        missing, extra = need - set(s), set(s) - may - {"via"}
        if missing or extra:
            add(f"/players/sources/{i}", f"a {s['via']} source " + ("needs " + ", ".join(sorted(missing)) if missing else "") +
                ("; " if missing and extra else "") + (", ".join(sorted(extra)) + " means nothing in it" if extra else ""), "players")
        if s["via"] == "log" and not {"count", "join"} & set(s) or "leave" in s and "join" not in s:
            add(f"/players/sources/{i}", "a log source reads a count line, or join lines (and leave lines with them)", "players")
        if "port" in s and s["port"] not in ports:
            add(f"/players/sources/{i}/port", f"names {s['port']}, which is not one of this recipe's ports", "port")
    j, client = v.get("join") or {}, v.get("client") or {}
    if j.get("steamArgs") and not client.get("steam"):
        add("/join/steamArgs", "Steam's launch arguments need the game's Steam id in client.steam", "join")
    via, console = (((v.get("chat") or {}).get("send") or {}).get("via")), (v.get("run") or {}).get("console")
    if via in ("stdin", "keys") and v["kind"] != "minecraft" and console != via:
        add("/chat/send/via", f"sends through {via}, and run.console is {console or 'not set'}", "chat")
    rcon, uses = v.get("rcon"), []
    for part in ("any", "all"):
        uses += [f"/ready/{part}/{i}" for i, p in enumerate((ready or {}).get(part) or []) if p.get("query") == "rcon"]
    uses += ["/health/probe"] if ((v.get("health") or {}).get("probe") or {}).get("query") == "rcon" else []
    uses += [f"/players/sources/{i}" for i, s in enumerate((v.get("players") or {}).get("sources") or []) if s["via"] == "rcon"]
    uses += [f"/stop/steps/{i}" for i, s in enumerate((v.get("stop") or {}).get("steps") or []) if s["do"] == "rcon"]
    uses += ["/chat/send"] if via == "rcon" else []
    if uses and not rcon and v["kind"] != "minecraft":
        add(uses[0], "speaks RCON, and the recipe does not say how: an rcon section with its port and password setting", "rcon")
    if rcon:
        port = next((p for p in v.get("ports") or [] if p["id"] == rcon["port"]), None)
        if port is None or port["proto"] != "tcp":
            add("/rcon/port", "has to name one of this recipe's tcp ports", "port")
        _secret_problem("/rcon/password", rcon["password"], v, add)
    u, g = v.get("updates") or {}, v.get("get") or {}
    if "steam" in u and "steamcmd" not in g:
        add("/updates/steam", "Steam updates are for a server SteamCMD installs", "updates")
    if "feed" in u and "download" not in g:
        add("/updates/feed", "a release feed is for a server that is downloaded", "updates")


def _writers(v, add):
    """Each writer says what its format needs: the keys to set, or the lines; Section/key for ini; a JSON pointer for json;
    a separator only for keyvalue (REC-2's writers then never guess)."""
    lb = ((v.get("tests") or {}).get("loopback") or {}).get("files") or []
    for at, w in [(f"/files/{i}", w) for i, w in enumerate(v.get("files") or [])] + [(f"/tests/loopback/files/{i}", w) for i, w in enumerate(lb)]:
        fmt = w["format"]
        if fmt == "lines" and not w.get("lines"):
            add(at, "a lines writer needs its lines", "writer")
        elif fmt not in ("lines", "hook") and not w.get("set"):
            add(at, f"a {fmt} writer needs the keys it sets", "writer")
        if fmt != "lines" and "lines" in w:
            add(f"{at}/lines", f"means nothing for a {fmt} writer", "writer")
        if "sep" in w and fmt != "keyvalue":
            add(f"{at}/sep", "a separator is for a keyvalue writer only", "writer")
        if "from" in w and fmt in ("lines", "hook"):
            add(f"{at}/from", f"a {fmt} writer writes the whole file, so it copies nothing first", "writer")
        for k in w.get("set") or {}:
            if (fmt == "ini" and not re.fullmatch(r"[^\[\]]+/[^/=:]+", k)) or (fmt == "json" and not k.startswith("/")) \
                    or (fmt == "ini-tuple" and not re.fullmatch(r"[^\[\]]+/[^/=:(),]+/[^/=:(),\"]+", k)) \
                    or (fmt in ("properties", "keyvalue") and (not k.strip() or any(c in k for c in "=:\n") or k != k.strip())):
                add(f"{at}/set/{_esc(k)}", {"ini": "an ini key is Section/key", "ini-tuple": "an ini-tuple key is Section/Key/Field",
                                            "json": "a json key is a JSON pointer like /a/b"}.get(fmt, "is not a key this format can hold"), "writer")
        for k in w.get("keepIfSet") or []:
            if fmt != "ini-tuple":
                add(f"{at}/keepIfSet", "is for an ini-tuple writer only", "writer")
            elif k not in (w.get("set") or {}):
                add(f"{at}/keepIfSet", f"names {k}, which this writer does not set", "writer")


def _kinds(v, add):
    kind = v["kind"]
    for sec, kinds in SECTION_KINDS.items():
        if sec in v and kind not in kinds:
            add(f"/{sec}", f"is for {' and '.join(sorted(kinds))} recipes only", "kind")
    for method in v.get("get") or {}:
        if kind not in GET_KINDS[method]:
            add(f"/get/{method}", f"is not a way a {kind} recipe gets its server", "kind")


def _bad_host(host):
    """A host a download may not name: an address, this PC, a name with no dot, a local network name."""
    h = str(host or "").lower().rstrip(".")
    if "." not in h or h == "localhost" or h.endswith((".localhost", ".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[0-9.]+", h))


def _get(v, platform, source, add):
    g = v.get("get") or {}
    dl = g.get("download")
    if dl:
        seen = set()
        for i, f in enumerate(dl.get("files") or []):
            at = f"/get/download/files/{i}"
            if f["platform"] in seen:
                add(f"{at}/platform", f"{f['platform']} has two files", "download")
            seen.add(f["platform"])
            if _bad_host(urllib.parse.urlsplit(f["url"]).hostname):
                add(f"{at}/url", "has to name a host on the Internet, not an address or a name on this network", "download")
            unpack = f.get("unpack", "zip")
            if "rename" in f and unpack != "none":
                add(f"{at}/rename", "renames a file that is not unpacked (unpack none) only", "download")
            if "strip" in f and unpack == "none":
                add(f"{at}/strip", "strips folders from an archive only", "download")
        if dl.get("files"):
            for arch in (v.get("platforms") or {}).get(platform, {}).get("arch") or []:
                if f"{platform}-{arch}" not in seen and "any" not in seen:
                    add("/get/download/files", f"has no file for {PLATFORM_NAMES[platform]} on {arch}, where the recipe says it runs", "download")
        for i, h in enumerate(dl.get("hosts") or []):
            if _bad_host(h):
                add(f"/get/download/hosts/{i}", "has to be a host on the Internet, not an address or a name on this network", "download")
    img, d = g.get("image"), v.get("docker") or {}
    if img and source == "library" and not img.get("digest"):
        add("/get/image/digest", "a library image is pinned by its digest", "docker")
    if d.get("compose") and source != "builtin":
        add("/docker/compose", "a compose file can ask for anything (the host's network, the Docker socket, any folder): built-in recipes only, until Fenrir checks compose files", "docker")
    for key in ("name", "mount"):
        names = [vol[key] for vol in d.get("volumes") or []]
        if len(set(names)) != len(names):
            add("/docker/volumes", f"has the same {key} twice", "docker")


def _dates(v, add):
    for where, s in (("/research/checked", (v.get("research") or {}).get("checked")), ("/tests/live/last", ((v.get("tests") or {}).get("live") or {}).get("last"))):
        if s is None:
            continue
        try:
            ok = bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s)) and datetime.date.fromisoformat(s) is not None
        except ValueError:
            ok = False
        if not ok:
            add(where, "has to be a date like 2026-10-13", "date")


def _version_tuple(s):
    return tuple(int(x) for x in (str(s).split(".") + ["0", "0"])[:3])


def _versions(v, add):
    if not v.get("minFenrir"):
        return
    try:
        from connect import VERSION
    except ImportError:
        return
    if _version_tuple(v["minFenrir"]) > _version_tuple(VERSION):
        add("/minFenrir", f"needs Fenrir {v['minFenrir']} or newer", "version")


def _checks(v, platform, source, add):
    ports = _ports(v, add)
    _anticheat(v, add)
    _settings(v, add)
    _places(v, add)
    _paths(v, source, add)
    _run(v, platform, add)
    _steps(v, platform, add)
    _auth_and_warn(v, add)
    _probes(v, ports, add)
    _writers(v, add)
    _kinds(v, add)
    _get(v, platform, source, add)
    for where, rx, need in _regexes(v):
        said = regex_problem(rx)
        if said:
            add(where, said, "regex")
            continue
        c = re.compile(rx)
        for group in need:
            if (group == 1 and c.groups < 1) or (isinstance(group, str) and group not in c.groupindex):
                add(where, f"needs a group {'(...)' if group == 1 else '(?P<' + group + '>...)'} for what it reads", "regex")
    _dates(v, add)
    _versions(v, add)


def problems(doc, source="builtin"):
    """Everything wrong with a recipe that already fits the schema, for one source (builtin, library, local, imported):
    a list of Problems, [] when there is nothing. Each platform the recipe lists is checked as it runs there."""
    out, seen = [], set()

    def add(where, say, code):
        if (where, say, code) not in seen:
            seen.add((where, say, code))
            out.append(Problem(where, say, code))
    if source not in SOURCES:
        add("/", f"comes from {source}, which is not a place Fenrir takes recipes from", "source")
        return out
    _text(doc, "", add)
    _source(doc, source, add)
    for p in doc.get("platformOverrides") or {}:
        if p not in doc.get("platforms") or {}:
            add(f"/platformOverrides/{p}", "is for a platform the recipe does not list", "platform")
    for platform in sorted(doc.get("platforms") or {}):
        v = view(doc, platform)
        if (doc.get("platformOverrides") or {}).get(platform):
            said = schema.problems(v)
            for pr in said:
                add(pr.where, f"{pr.say} (on {PLATFORM_NAMES[platform]})", "schema")
            if said:
                continue
        _checks(v, platform, source, add)
    return out
