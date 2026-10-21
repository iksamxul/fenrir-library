"""The trust prompt, "What is inside" (PLAN-V3 A8, recipes.md section 3.12), and the host's answers to it.

A recipe from the community library or an import is shown before it is used: what it downloads and from where (each file
pinned by its checksum or not), the program it runs with its arguments (a secret on the command line said so), the ports
friends need, the files it writes, where it keeps its world, its settings, its anti-cheat facts, whether it needs an
account, and where it runs; with its source's badge, built-in and library being signed. The answers are Yes, this once;
Always trust this recipe; and No, which is the default. They are kept in DATA/recipes/trust.json by the recipe's hash, so
a recipe that changes asks again, saying what changed since the version the host trusted.

    prompt(recipe)            the prompt: sections of the engine's messages (M), the recipe's own notes as written
    needed(recipe)            None when it may be used without asking, else the prompt
    record(recipe, answer)    once, always or no; True when the answer lets it be used now
    as_json(value)            a prompt with every message as {id, args, text}, for the API and the pages

Built-in recipes come inside Fenrir's signed update and the host's own were made here: neither asks."""
import json
import os
import threading
import time
import urllib.parse
from pathlib import Path

from ..i18n import M, Msg
from ..util import DATA
from . import policy, values

FILE = DATA / "recipes" / "trust.json"
ANSWERS = ("no", "once", "always")
ASKS = ("library", "imported")  # the sources that ask: the others are Fenrir's own or the host's
SECTIONS = ("gets", "runs", "ports", "files", "saves", "settings", "anticheat", "account", "where")
_lock = threading.Lock()
# each id written out, so tests/test_i18n_messages.py can check every one exists
TITLES = {"gets": lambda: M("recipes.trust.section.gets"), "runs": lambda: M("recipes.trust.section.runs"),
          "ports": lambda: M("recipes.trust.section.ports"), "files": lambda: M("recipes.trust.section.files"),
          "saves": lambda: M("recipes.trust.section.saves"), "settings": lambda: M("recipes.trust.section.settings"),
          "anticheat": lambda: M("recipes.trust.section.anticheat"), "account": lambda: M("recipes.trust.section.account"),
          "where": lambda: M("recipes.trust.section.where")}
CHANGED = {"gets": lambda **a: M("recipes.trust.changed.gets", **a), "runs": lambda **a: M("recipes.trust.changed.runs", **a),
           "ports": lambda **a: M("recipes.trust.changed.ports", **a), "files": lambda **a: M("recipes.trust.changed.files", **a),
           "saves": lambda **a: M("recipes.trust.changed.saves", **a), "settings": lambda **a: M("recipes.trust.changed.settings", **a),
           "anticheat": lambda **a: M("recipes.trust.changed.anticheat", **a), "account": lambda **a: M("recipes.trust.changed.account", **a),
           "where": lambda **a: M("recipes.trust.changed.where", **a)}


def _raw(text):
    """The recipe's own words, shown as written (never translated, kept apart in the page as data)."""
    return {"raw": str(text)}


# ---------------------------------------------------------------------------------------------------- the facts
def _platforms(doc):
    names = {"windows": "Windows", "linux": "Linux"}
    return ", ".join(f"{names.get(os_, os_)} ({', '.join(p.get('arch') or [])})" for os_, p in (doc.get("platforms") or {}).items())


def _host(url):
    return urllib.parse.urlsplit(str(url)).hostname or ""


def _file(url):
    return Path(urllib.parse.urlsplit(str(url)).path).name or str(url)


def facts(doc, platform=None):
    """What a recipe asks of this PC, as plain data by section: what the prompt says, and what changed() compares."""
    v = policy.view(doc, platform) if platform else doc
    get, run, f = v.get("get") or {}, v.get("run") or {}, {}
    gets = []
    if "steamcmd" in get:
        gets.append(f"steamcmd {get['steamcmd']['app']}" + (f" beta {get['steamcmd']['beta']}" if get["steamcmd"].get("beta") else ""))
    if "download" in get:
        dl = get["download"]
        gets += [f"download {x['platform']} {x['url']} sha256 {x['sha256']}" for x in dl.get("files") or []]
        if dl.get("resolver"):
            gets.append(f"resolver {dl['resolver']} hosts {' '.join(dl.get('hosts') or [])}")
    for k in ("fromGame", "kit", "image", "minecraft"):
        if k in get:
            gets.append(f"{k} {json.dumps(get[k], sort_keys=True)}")
    f["gets"] = gets or ["none"]
    runs = [f"program {run.get('program')}", "args " + json.dumps(run.get("args") or [])] if run else []
    runs += [f"env {k}" for k in sorted(run.get("env") or {})] + ([f"shell {run['shell']}"] if run.get("shell") else [])
    runs += [f"first {st['program']} {json.dumps(st.get('args') or [])}" for st in v.get("firstRun") or []]
    runs += [f"hook {v['hooks']}"] if v.get("hooks") else []
    dk = v.get("docker") or {}
    runs += ([f"user {dk['user']}"] if dk.get("user") else []) + [f"volume {x['name']} {x['mount']}" for x in dk.get("volumes") or []]
    f["runs"] = runs
    f["ports"] = [f"{p['id']} {p['port']}/{p['proto']}" + (" friends" if p.get("friends", True) else "") for p in v.get("ports") or []]
    f["files"] = [f"{w['file']} {w.get('when', 'start')}" for w in v.get("files") or []]
    f["saves"] = [str((v.get("saves") or {}).get("path"))] if v.get("saves") else []
    f["settings"] = [f"{s['key']} {s['type']}" + (" inArgs" if s.get("inArgs") else "") for s in v.get("settings") or []]
    ac = v.get("antiCheat") or {}
    f["anticheat"] = [ac.get("status", "unsaid")] + [json.dumps(sw, sort_keys=True) for sw in ac.get("keepOff") or []]
    f["account"] = ([f"anonymous {get['steamcmd'].get('anonymous')}"] if "steamcmd" in get else []) + [n["say"] for n in v.get("needs") or []]
    f["where"] = [_platforms(v)]
    return f


def changed(old, new):
    """What changed between two versions' facts, one message per part that differs."""
    out = []
    for s in SECTIONS:
        a, b = (old or {}).get(s) or [], (new or {}).get(s) or []
        if a != b:
            out.append(CHANGED[s](was="; ".join(a)[:400] or "-", now="; ".join(b)[:400] or "-"))
    return out


# ---------------------------------------------------------------------------------------------------- the prompt
def _gets(v):
    get, out = v.get("get") or {}, []
    if "steamcmd" in get:
        s = get["steamcmd"]
        out.append(M("recipes.trust.gets.steamcmd_beta", app=s["app"], beta=s["beta"]) if s.get("beta") else M("recipes.trust.gets.steamcmd", app=s["app"]))
    if "download" in get:
        dl = get["download"]
        for x in dl.get("files") or []:
            if x.get("size"):
                out.append(M("recipes.trust.gets.file", name=_file(x["url"]), host=_host(x["url"]), platform=x["platform"], size=round(x["size"] / 1048576, 1)))
            else:
                out.append(M("recipes.trust.gets.file_nosize", name=_file(x["url"]), host=_host(x["url"]), platform=x["platform"]))
        if dl.get("resolver"):
            out.append(M("recipes.trust.gets.resolver", hosts=", ".join(dl.get("hosts") or [])))
    if "fromGame" in get:
        out.append(M("recipes.trust.gets.from_game", file=get["fromGame"]["file"]))
    if "kit" in get:
        out.append(M("recipes.trust.gets.kit", kit=get["kit"]["kit"]))
    if "image" in get:
        im = get["image"]
        out.append(M("recipes.trust.gets.image_pinned", image=im["image"], tag=im["tag"]) if im.get("digest")
                   else M("recipes.trust.gets.image", image=im["image"], tag=im["tag"]))
    if "minecraft" in get:
        out.append(M("recipes.trust.gets.minecraft", loader=str(get["minecraft"].get("loader") or "vanilla")))
    return out or [M("recipes.trust.gets.none")]


def _runs(v):
    run, out, fl = v.get("run") or {}, [], values.fields(v)
    if run.get("program"):
        args = " ".join(a if isinstance(a, str) else "[" + " ".join(a.get("args") or []) + "]" for a in run.get("args") or [])
        out.append(M("recipes.trust.runs.program", program=run["program"], args=args) if args else M("recipes.trust.runs.program_alone", program=run["program"]))
    if run.get("shell") == "cmd":
        out.append(M("recipes.trust.runs.cmd"))
    env = sorted(run.get("env") or {})
    if env:
        out.append(M("recipes.trust.runs.env", n=len(env), names=", ".join(env)))
    for st in v.get("firstRun") or []:
        out.append(M("recipes.trust.runs.first_run", program=st["program"], args=" ".join(st.get("args") or [])))
    if v.get("hooks"):
        out.append(M("recipes.trust.runs.hook", hook=v["hooks"]))
    for s in fl.values():
        if s["type"] == "secret" and s.get("inArgs"):
            out.append(M("recipes.trust.runs.secret_arg", label=s["label"]))
    dk = v.get("docker") or {}
    if "image" in (v.get("get") or {}):
        out.append(M("recipes.trust.runs.docker_user", user=dk["user"]) if dk.get("user") else M("recipes.trust.runs.docker_root"))
        if dk.get("volumes"):
            out.append(M("recipes.trust.runs.docker_volumes", n=len(dk["volumes"]), names=", ".join(x["name"] for x in dk["volumes"])))
    return out


def _ports(v):
    out = []
    try:
        taken = values.ports(v, values.current(v))
    except Exception:  # noqa: BLE001 - a port that needs a setting first is still said by its template
        taken = [{"id": p["id"], "port": p["port"], "proto": p["proto"], "friends": p.get("friends", True), "label": p.get("label") or ""}
                 for p in v.get("ports") or []]
    for p in taken:
        say = {"label": p.get("label") or p["id"], "port": str(p["port"]), "proto": p["proto"].upper()}
        out.append(M("recipes.trust.ports.friends", **say) if p.get("friends", True) else M("recipes.trust.ports.local", **say))
    return out or [M("recipes.trust.ports.none")]


def _files(v):
    out = [M("recipes.trust.files.save", file=w["file"]) if w.get("when") == "save" else M("recipes.trust.files.start", file=w["file"])
           for w in v.get("files") or []]
    return out or [M("recipes.trust.files.none")]


def _settings(v):
    fl = values.fields(v)
    if not fl:
        return [M("recipes.trust.settings.none")]
    out = [M("recipes.trust.settings.list", n=len(fl), labels=", ".join(f["label"] for f in fl.values()))]
    out += [M("recipes.trust.settings.secret", label=f["label"]) for f in fl.values() if f["type"] == "secret"]
    return out


def _anticheat(v):
    ac = v.get("antiCheat")
    if not ac:
        return [M("recipes.trust.anticheat.unsaid")]
    if ac["status"] == "none":
        out = [M("recipes.trust.anticheat.none")]
    else:
        what, out = ac.get("what") or "", []
        for sw in ac.get("keepOff") or []:
            if "noArg" in sw:
                out.append(M("recipes.trust.anticheat.off_arg", what=what, flag=sw["noArg"]))
            elif "args" in sw:
                out.append(M("recipes.trust.anticheat.off_args", what=what, args=" ".join(sw["args"])))
            elif "file" in sw:
                out.append(M("recipes.trust.anticheat.off_file", what=what, key=sw["key"], value=sw["value"], file=sw["file"]))
            else:
                out.append(M("recipes.trust.anticheat.off_env", what=what, env=sw["env"], value=sw["value"]))
    if ac.get("notes"):
        out.append(_raw(ac["notes"]))
    return out


def _account(v):
    get, out = v.get("get") or {}, []
    if "steamcmd" in get:
        out.append(M("recipes.trust.account.anonymous"))
    if v.get("credentials"):
        out.append(M("recipes.trust.account.credentials", n=len(v["credentials"]) if isinstance(v["credentials"], list) else 1))
    out += [_raw(n["say"]) for n in v.get("needs") or []]
    return out or [M("recipes.trust.account.none")]


def _where(v, platform):
    from . import load
    out = [M("recipes.trust.where.list", platforms=_platforms(v))]
    os_name, arch = load.here()
    here = arch in (((v.get("platforms") or {}).get(platform or os_name) or {}).get("arch") or [])
    out.append(M("recipes.trust.where.here") if here else M("recipes.trust.where.not_here"))
    out += [_raw(p["notes"]) for p in (v.get("platforms") or {}).values() if p.get("notes")]
    return out


def badge(recipe, meta=None, record=None):
    """The source's badge: built in and library are signed (Fenrir's update, the library's index), the others are not."""
    src = getattr(recipe, "source", "local")
    if src == "builtin":
        return {"kind": src, "signed": True, "text": M("recipes.source.builtin")}
    if src == "library":
        day = (meta or {}).get("reviewed")
        return {"kind": src, "signed": True, "text": M("recipes.source.library", date=day) if day else M("recipes.source.library_undated")}
    if src == "imported":
        rec = record or {}
        text = M("recipes.source.egg", origin=rec.get("origin") or "-") if rec.get("how") == "egg" else M("recipes.source.imported", origin=rec.get("origin") or "-")
        return {"kind": src, "signed": False, "text": text}
    return {"kind": src, "signed": False, "text": M("recipes.source.local")}


def prompt(recipe, platform=None, meta=None, record=None):
    """What is inside a recipe, for the host to answer before it is used. meta: the library's entry (the day it was
    reviewed); record: an import's (where it came from, a draft's notes)."""
    from . import load
    platform = platform or load.here()[0]
    v = recipe.view(platform) if hasattr(recipe, "view") else policy.view(recipe, platform)
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    parts = {"gets": _gets(v), "runs": _runs(v), "ports": _ports(v), "files": _files(v), "saves":
             [M("recipes.trust.saves.path", path=v["saves"]["path"])] if v.get("saves") else [M("recipes.trust.saves.none")],
             "settings": _settings(v), "anticheat": _anticheat(v), "account": _account(v), "where": _where(v, platform)}
    out = {"recipe": {"id": doc.get("id"), "version": doc.get("version"), "name": doc.get("name"), "source": getattr(recipe, "source", None),
                      "sha256": getattr(recipe, "sha256", None)},
           "title": M("recipes.trust.title", name=doc.get("name") or doc.get("id")),
           "source": badge(recipe, meta, record),
           "sections": [{"id": s, "title": TITLES[s](), "lines": parts[s]} for s in SECTIONS],
           "notes": [_raw(n) for n in (record or {}).get("notes") or []],
           "changed": [], "default": "no",
           "answers": [{"id": "no", "label": M("recipes.trust.answer.no"), "default": True},
                       {"id": "once", "label": M("recipes.trust.answer.once")},
                       {"id": "always", "label": M("recipes.trust.answer.always")}]}
    before = _load()["trusted"].get(_key(recipe))
    if before and before.get("sha256") != getattr(recipe, "sha256", None):
        out["changed"] = [M("recipes.trust.changed.title", name=doc.get("name") or doc.get("id")), *changed(before.get("facts"), facts(doc, platform))]
    return out


# ---------------------------------------------------------------------------------------------------- the answers
def _load():
    try:
        d = json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    d.setdefault("v", 1)
    for k in ("answers", "trusted"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def _save(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(f"trust.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), "utf-8")
    os.replace(tmp, FILE)


def _key(recipe):
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    return f"{getattr(recipe, 'source', 'local')}:{doc.get('id')}"


def answer_of(recipe):
    """The host's last answer for this recipe at this hash: always, once, no, or None when never asked."""
    return (_load()["answers"].get(getattr(recipe, "sha256", "") or "") or {}).get("answer")


def needed(recipe, platform=None, meta=None, record=None):
    """None when the recipe may be used without asking (built in, made here, or always trusted as it is now), else the
    prompt. A recipe trusted once and changed since asks again, with what changed."""
    if getattr(recipe, "source", None) not in ASKS or answer_of(recipe) == "always":
        return None
    return prompt(recipe, platform, meta, record)


def record(recipe, choice, platform=None):
    """The host's answer, kept by the recipe's hash: True when it lets the recipe be used now (once or always). Anything
    but the three answers is No."""
    choice = choice if choice in ANSWERS else "no"
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    sha = getattr(recipe, "sha256", None)
    if not sha:
        return False
    with _lock:
        d = _load()
        d["answers"][sha] = {"id": doc.get("id"), "version": doc.get("version"), "source": getattr(recipe, "source", None), "answer": choice,
                             "at": time.time()}
        if choice != "no":
            d["trusted"][_key(recipe)] = {"sha256": sha, "facts": facts(doc, platform), "at": time.time()}
        _save(d)
    return choice != "no"


def use_problem(recipe):
    """Why the host has to answer the prompt before this recipe is used, as a message, or None. The caller shows the
    prompt (needed) and calls record(); "once" lets this one use through, so it asks the next time."""
    if getattr(recipe, "source", None) not in ASKS or answer_of(recipe) == "always":
        return None
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    if answer_of(recipe) == "no":
        return M("recipes.trust.refused", name=doc.get("name") or doc.get("id"))
    return M("recipes.trust.ask", name=doc.get("name") or doc.get("id"))


# ---------------------------------------------------------------------------------------------------- for the API
def as_json(value):
    """The prompt as JSON carries it: each message as {id, args, text}, a page showing t("msg." + id, args), and text
    for an app that has no table; the recipe's own words as {raw}."""
    if isinstance(value, Msg):
        return {**value.ref(), "text": str(value)}
    if isinstance(value, dict):
        return {k: as_json(x) for k, x in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_json(x) for x in value]
    return value
