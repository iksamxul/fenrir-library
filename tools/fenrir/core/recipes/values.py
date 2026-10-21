"""Placeholders and values (recipes.md section 3.3): templates filled from a server's settings, ports and folders; settings
read and checked by their type; conditional arguments; the ports a server takes; and the guards for the paths and local
addresses a recipe names, checked again on the filled text at run time.

Filling keeps the 2.x rule (core/games/servers.py fill): one pass, so a value that holds braces is never filled again, and
braces that are not a placeholder stay as written. A placeholder whose kind the context does not hold stays as written
too, so a text can be filled in stages: the engine fills the ports, Fenrir Connect the address."""
import re
import urllib.parse
from pathlib import Path

from . import Problem, RecipeError

PLACEHOLDER = re.compile(
    r"\{(?:(?P<kind>setting|port)\.(?P<key>[A-Za-z0-9_-]{1,40})(?:\+(?P<plus>[0-9]{1,5}))?"
    r"|secret\.(?P<secret>[A-Za-z0-9_][A-Za-z0-9_.-]{0,120})"
    r"|(?P<word>install|data|saves|log|game|profile|address|code|name|text|minutes))\}")
ROOTS = ("install", "data", "saves", "log", "game", "profile")  # the folders and files a path may start from
NUMBERS = ("number", "port", "duration")
TEXTS = ("text", "secret")
LINE_MAX = 4096  # a log line is cut here before any recipe's pattern reads it (recipes.md section 5)
ONE_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
DEVICES = re.compile(r"(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3]|conin\$|conout\$)(\..*)?", re.I)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SWITCH_WORDS = {"1": True, "0": False, "true": True, "false": False, "on": True, "off": False, "yes": True, "no": False, "": False}
_compiled = {}


class SettingError(ValueError):
    """A setting's value Fenrir will not keep, said to the host as it is."""

    def __init__(self, text, key=None):
        super().__init__(text)
        self.key = key


# ---------------------------------------------------------------------------------------------------- placeholders
def parse(template):
    """Every placeholder in a template: (kind, name, plus), kind being setting, port, secret or the word itself (install,
    address...). Braces that are not one are not listed: they stay as written."""
    out = []
    for m in PLACEHOLDER.finditer(str(template)):
        if m.group("kind"):
            out.append((m.group("kind"), m.group("key"), int(m.group("plus")) if m.group("plus") else 0))
        elif m.group("secret") is not None:
            out.append(("secret", m.group("secret"), 0))
        else:
            out.append((m.group("word"), None, 0))
    return out


def fill(template, ctx):
    """The template with its placeholders filled from ctx, which holds text: {"setting": {key: text}, "port": {id: text},
    "secret": a dict or a function name -> value, "install": "C:\\...", ...}. "+N" adds to a number, as 2.x {port+1} does
    (an empty value counts as 0). A secret is asked for only when the template names it: {secret.env.TOKEN} is the store's
    server.<id>.env.TOKEN, and the driver's function adds the server's own prefix, so a recipe never reaches another's."""
    def one(m):
        kind = m.group("kind") or ("secret" if m.group("secret") is not None else m.group("word"))
        if kind not in ctx:
            return m.group(0)
        if kind == "secret":
            src = ctx["secret"]
            v = src.get(m.group("secret")) if isinstance(src, dict) else src(m.group("secret"))
            return "" if v is None else str(v)
        if kind in ("setting", "port"):
            v = ctx[kind].get(m.group("key"), "")
            if m.group("plus"):
                try:
                    v = int(v or 0) + int(m.group("plus"))
                except (TypeError, ValueError):
                    raise RecipeError([Problem("/", f"{{{kind}.{m.group('key')}}} is not a number, so it cannot have a number added", "placeholder")]) from None
            return "" if v is None else str(v)
        return str(ctx[kind])
    return PLACEHOLDER.sub(one, str(template))


# ---------------------------------------------------------------------------------------------------- settings
def fields(doc):
    """The recipe's settings by key, in its own order."""
    return {f["key"]: f for f in doc.get("settings") or []}


def default(field):
    """What a setting holds before the host sets it: the recipe's default, or nothing of its kind."""
    if "default" in field:
        return field["default"]
    return {"switch": False, "list": []}.get(field["type"], None if field["type"] in NUMBERS else "")


def _label(field):
    return field.get("label") or field.get("key") or "That setting"


def _number(field, v):
    if isinstance(v, bool):
        raise SettingError(f"{_label(field)} has to be a number.", field.get("key"))
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    s = str(v if v is not None else "").strip().lower()
    unit = 1
    if field["type"] == "duration" and s[-1:] in UNITS:
        s, unit = s[:-1].strip(), UNITS[s[-1]]
    if not re.fullmatch(r"-?[0-9]{1,12}", s, re.ASCII) or (s.startswith("-") and field["type"] != "number"):
        raise SettingError(f"{_label(field)} has to be a number." if field["type"] != "duration"
                           else f"{_label(field)} has to be a number of seconds, or minutes and hours like 10m or 2h.", field.get("key"))
    return int(s) * unit


def read(field, value):
    """A stored value as its kind, forgiving what older files hold: 2.x kept switches as 0 and 1, which read as off and on,
    and a value that no longer fits its kind reads as the default (a start then says what is missing). server.json keeps
    {"secret": true} for a secret setting: its value is in the sealed store, so the marker reads as not set here."""
    if value is None or isinstance(value, dict) or (isinstance(value, list) and field["type"] != "list"):
        return default(field)
    try:
        return coerce(field, value, legacy=True)
    except SettingError:
        return default(field)


def coerce(field, value, legacy=False):
    """The value to keep for one setting, as its kind: whole numbers, true or false for switches, one line of text, one of
    the choices. legacy=True keeps 2.x's text rule for the migrated servers (a line break becomes a space, the ends are
    trimmed, 120 characters at most: core/games/servers.py set); new text with a control character in it is refused."""
    kind = field["type"]
    if kind in NUMBERS:
        return _number(field, value)
    if kind == "switch":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        word = str(value if value is not None else "").strip().lower()
        if word in SWITCH_WORDS:
            return SWITCH_WORDS[word]
        raise SettingError(f"{_label(field)} is a switch: on or off.", field.get("key"))
    if kind == "choice":
        for c in field.get("choices") or []:
            if str(c["value"]) == str(value if value is not None else "").strip():
                return c["value"]
        raise SettingError("Pick one of the choices in the list.", field.get("key"))
    if kind == "list":
        items = value if isinstance(value, list) else str(value or "").replace("\r", "").split("\n")
        out = [str(x).strip() for x in items if str(x if x is not None else "").strip()]
        for x in out:
            if ONE_LINE.search(x):
                raise SettingError(f"Each entry of {_label(field)} has to be one line.", field.get("key"))
        return out
    text = str(value if value is not None else "")
    if legacy:
        return text.replace("\n", " ").strip()[:120]
    if ONE_LINE.search(text):
        raise SettingError(f"{_label(field)} has to be one line, with no tabs or line breaks.", field.get("key"))
    return text.strip() if kind == "text" else text  # a password is kept exactly as typed


def limit_problem(field, value):
    """What breaks the setting's own limits, or None. An empty value is not judged here: required is a start's question,
    so a name can be saved before there is a password (2.x validate)."""
    kind, label = field["type"], _label(field)
    if value in (None, "", []):
        return None
    if kind in NUMBERS:
        lo, hi = field.get("min"), field.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            if kind == "port" and lo is not None and hi is not None:
                return f"Pick a port between {lo} and {hi}."
            if lo is not None and hi is not None:
                return f"{label} has to be between {lo:g} and {hi:g}."
            return f"{label} has to be at least {lo:g}." if lo is not None and value < lo else f"{label} can be at most {hi:g}."
        return None
    items = value if kind == "list" else [value] if kind in TEXTS else []
    for v in items:
        v = str(v)
        if len(v) < field.get("minLength", 0):
            return (f"The server's password needs at least {field['minLength']} characters." if kind == "secret"
                    else f"{label} needs at least {field['minLength']} characters.")
        if "maxLength" in field and len(v) > field["maxLength"]:
            return f"{label} can be at most {field['maxLength']} characters long."
        if field.get("noChars") and any(c in v for c in field["noChars"]):
            return f"{label} may not hold any of these: {' '.join(field['noChars'])}"
        if field.get("pattern") and not search(field["pattern"], v):
            return field.get("patternSay") or f"{label} is not written in a form the server takes."
    return None


def _not_in(fl, values, keys):
    """The first notIn rule broken, judged when either side of it is among keys: a password may not be part of the name."""
    for f in fl.values():
        mine = str(values.get(f["key"]) or "")
        for other in f.get("notIn") or []:
            if (f["key"] in keys or other in keys) and mine and other in fl and mine.lower() in str(values.get(other) or "").lower():
                return SettingError(f"{_label(f)} may not be part of {_label(fl[other]).lower()}.", f["key"])
    return None


def check(doc, changes, current=None):
    """The values a Save would keep, each as its kind, or SettingError for the first that is wrong, said before anything is
    kept (2.x validate). Only the changed settings are judged; keys the recipe does not have are left out."""
    fl = fields(doc)
    kept = {k: coerce(fl[k], v) for k, v in changes.items() if k in fl}
    for k, v in kept.items():
        said = limit_problem(fl[k], v)
        if said:
            raise SettingError(said, k)
    err = _not_in(fl, {**(current or {}), **kept}, set(kept))
    if err:
        raise err
    return kept


def start_problem(doc, values):
    """Why the server cannot start with these settings, or None: a required one empty, a limit broken, a notIn rule broken
    (2.x check_settings). The recipe's own needs text is said when it has one for that setting."""
    fl = fields(doc)
    needs = {n["setting"]: n["say"] for n in doc.get("needs") or [] if n.get("setting")}
    for k, f in fl.items():
        v = values.get(k, default(f))
        if f.get("required") and v in (None, "", []):
            return needs.get(k) or f"Set {_label(f).lower()} first."
        said = limit_problem(f, v)
        if said:
            return needs.get(k) or said
    err = _not_in(fl, values, set(fl))
    return str(err) if err else None


def current(doc, stored=None):
    """Every setting's value for one server: the stored ones read as their kind, the defaults for the rest."""
    stored = stored or {}
    return {k: read(f, stored[k]) if k in stored else default(f) for k, f in fields(doc).items()}


def as_text(field, value):
    """How a value is written into a template: a switch through its "as" words (Valheim's -public 1 or 0) or true/false,
    numbers as whole numbers, a list as one comma-separated line."""
    kind = field["type"] if field else "text"
    if kind == "switch":
        on = bool(value)
        return (field["as"]["on"] if on else field["as"]["off"]) if field.get("as") else ("true" if on else "false")
    if value in (None, ""):
        return ""
    if kind in NUMBERS:
        return str(int(value))
    if kind == "list":
        return ",".join(str(v) for v in value or [])
    return str(value)


def is_on(field, value):
    """A conditional argument's test: a switch that is on, a text or a list that is not empty, a number that is not 0."""
    if field and field["type"] == "switch":
        return bool(value)
    if field and field["type"] in NUMBERS:
        return value not in (None, "", 0)
    return bool(value)


def args(items, ctx, doc, values):
    """An argument list with its conditions decided and its placeholders filled (2.x flags, and the dropped empty
    -password pair, both say this now). No shell ever reads the result: it is an argv."""
    fl, out = fields(doc), []
    for a in items or []:
        if isinstance(a, dict):
            key = a["if"].split(".", 1)[1]
            on = is_on(fl.get(key), values.get(key))
            if on != bool(a.get("unless")):
                out += [fill(x, ctx) for x in a["args"]]
        else:
            out.append(fill(a, ctx))
    return out


# ---------------------------------------------------------------------------------------------------- ports and context
def ports(doc, values):
    """The ports a server takes with these settings: id, number, protocol, whether friends need it, label, http."""
    fl = fields(doc)
    ctx = {"setting": {k: as_text(fl[k], v) for k, v in values.items() if k in fl}}
    out = []
    for i, p in enumerate(doc.get("ports") or []):
        n = p["port"] if isinstance(p["port"], int) else fill(p["port"], ctx)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= 65535:
            raise RecipeError([Problem(f"/ports/{i}/port", "does not come to a port between 1 and 65535 with these settings", "port")])
        out.append({"id": p["id"], "port": n, "proto": p["proto"], "friends": p.get("friends", True), "label": p.get("label") or "",
                    "http": bool(p.get("http"))})
    return out


def context(doc, values, secret=None, **parts):
    """What fill() reads for one server: its settings as text, its ports, and the folders and words given (install, data,
    saves, log, game, profile, address, code, name, text, minutes). secret opens a sealed value by its name within the
    server's own scope (the driver's function adds server.<id>.)."""
    fl = fields(doc)
    ctx = {"setting": {k: as_text(fl[k], v) for k, v in values.items() if k in fl}}
    ctx["port"] = {p["id"]: str(p["port"]) for p in ports(doc, values)}
    for k, v in parts.items():
        if v is not None:
            ctx[k] = str(v)
    if secret is not None:
        ctx["secret"] = secret
    return ctx


# ---------------------------------------------------------------------------------------------------- guards at run time
def path_text_problem(text):
    """Why a filled path reads as one that could leave its folder, or None: a '..' part, a share or a device path, a colon
    past the drive (a file stream), a name Windows keeps for a device, or a part ending in a dot or a space (Windows
    trims those, so the name is not the one checked)."""
    s = str(text)
    drive = re.match(r"^[A-Za-z]:", s)
    if ONE_LINE.search(s) or any(c in s for c in '<>"|?*'):
        return "has a character no file name may have"
    if re.match(r"^[\\/]{2}", s):
        return "names a network share or a device"
    if ":" in (s[2:] if drive else s):
        return "has a colon where a file name cannot"
    for part in re.split(r"[\\/]", s[2:] if drive else s):
        if part == "..":
            return "climbs out of its folder with .."
        if DEVICES.fullmatch(part):
            return "has a name Windows keeps for a device"
        if part not in ("", ".") and part != part.rstrip(". "):
            return "has a name ending in a dot or a space, which Windows trims"
    return None


def real_path(template, ctx, roots):
    """The real place a path template names, filled from ctx and checked once links, junctions and short names are
    resolved: inside one of the server's own folders named in roots (install, data, saves; game to read only). Checked at
    every write, so a junction made after the recipe was checked is caught too."""
    raw = fill(template, ctx)
    said = path_text_problem(raw)
    if said:
        raise RecipeError([Problem("/", f"The path {raw[:120]} {said}", "path")])
    p = Path(raw)
    if not p.is_absolute():
        raise RecipeError([Problem("/", f"The path {raw[:120]} does not start in one of the server's own folders", "path")])
    real = p.resolve()
    for r in roots:
        if ctx.get(r):
            base = Path(ctx[r]).resolve()
            if real == base or base in real.parents:
                return real
    raise RecipeError([Problem("/", f"The path {raw[:120]} leads outside the server's own folders", "path")])


def saves_problem(saves, data, home):
    """Why a server's save folder is one Fenrir must not use, or None: inside another server's folder (its backups would
    carry, and a restore overwrite, the other server's world), or holding every server's folder."""
    s, d, h = (Path(x).resolve() for x in (saves, data, home))
    if s == h or s in h.parents:
        return "The save folder holds every server's own folder."
    if h in s.parents and not (s == d or d in s.parents):
        return "The save folder is inside another server's own folder."
    return None


def local_url(template, ctx):
    """A probe's or a REST call's address, filled and checked: http or https to 127.0.0.1 only (recipes.md section 5)."""
    url = fill(template, ctx)
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or u.hostname != "127.0.0.1" or u.username or u.password or "@" in u.netloc:
        raise RecipeError([Problem("/", f"{url[:120]} is not an address on this PC (127.0.0.1)", "url")])
    try:
        u.port
    except ValueError:
        raise RecipeError([Problem("/", f"{url[:120]} has a port that is not a number", "url")]) from None
    return url


def search(pattern, line):
    """A recipe's pattern on one line, cut to 4 KB first. The pattern passed policy.regex_problem when the recipe loaded."""
    rx = _compiled.get(pattern)
    if rx is None:
        rx = _compiled[pattern] = re.compile(pattern)
        if len(_compiled) > 2000:
            _compiled.clear()
    return rx.search(str(line)[:LINE_MAX])
