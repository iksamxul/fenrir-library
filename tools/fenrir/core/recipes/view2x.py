"""The 2.x shape of a built-in recipe (recipes.md section 4.1, item 3). servers.RECIPES, fivem.RECIPE and
SkyrimTogether.RECIPE are built from fenrir/recipes/builtin/*.json, so the JSON is the one place their facts live, and every
2.x path reads the same ones the recipe driver does. tests/test_recipes_golden.py holds the view to the frozen 2.9 dicts;
the one difference is the owner's ruling, 7 Days to Die's EACEnabled false.

2.x's words: {setting.port+1} is {port+1}, a port's {port.game} is its own template, and 7 Days to Die's
{install}\\fenrir-serverconfig.xml is {config}. A condition on a switch is a 2.x flag; one on a text (Terraria's
-password pair) is written inline, since 2.x dropped an empty -password pair by itself."""
import re

from . import load, values

SETTING = re.compile(r"\{setting\.([A-Za-z0-9_]+)(\+[0-9]+)?\}")


def _words(text, doc):
    """A template in 2.x's words."""
    ports = {p["id"]: p["port"] for p in doc.get("ports") or []}

    def port(m):
        t = ports.get(m.group(1))
        if isinstance(t, int):
            return str(t)
        return SETTING.sub(lambda s: "{" + s.group(1) + (s.group(2) or "") + "}", t or m.group(0))
    out = re.sub(r"\{port\.([a-z0-9-]+)\}", port, str(text))
    for w in doc.get("files") or []:
        if w["format"] == "xml-property" and str(w["file"]).startswith("{install}/"):
            name = w["file"][len("{install}/"):]
            out = out.replace("{install}\\" + name, "{config}").replace("{install}/" + name, "{config}")
    return SETTING.sub(lambda m: "{" + m.group(1) + (m.group(2) or "") + "}", out)


def _default(f):
    d = values.default(f)
    if f["type"] == "switch":
        return int(bool(d))
    return "" if d is None and f["type"] in ("text", "secret") else d


def view(recipe, platform="windows"):
    """One built-in recipe as 2.x's code holds it."""
    doc = recipe.view(platform)
    run, get, fl = doc.get("run") or {}, doc.get("get") or {}, values.fields(doc)
    out = {"name": doc["name"], "client": list((doc.get("client") or {}).get("steam") or [])}
    if "steamcmd" in get:
        out["app"] = get["steamcmd"]["app"]
    if "fromGame" in get:
        out["fromGame"] = get["fromGame"]["file"]
    else:
        out["exe"] = run["program"]
    if run.get("realPrograms"):
        out["realExes"] = list(run["realPrograms"])
    if run.get("env"):
        out["env"] = {k: _words(v, doc) for k, v in run["env"].items()}
    args, flags = [], {}
    for a in run.get("args") or []:
        if isinstance(a, str):
            args.append(_words(a, doc))
            continue
        key = a["if"].split(".", 1)[1]
        if (fl.get(key) or {}).get("type") == "switch":
            flags[key] = [_words(x, doc) for x in a["args"]]
        else:
            args += [_words(x, doc) for x in a["args"]]
    out["args"] = args
    if flags:
        out["flags"] = flags
    for w in doc.get("files") or []:
        if w["format"] == "xml-property" and w.get("from"):
            out["config"] = {"file": w["file"].split("/", 1)[1], "from": w["from"].split("/", 1)[1],
                             "set": {k: _words(v, doc) for k, v in (w.get("set") or {}).items()}}
    out["defaults"] = {k: _default(f) for k, f in fl.items()}
    # 2.x's ports are the ones friends use (router, firewall): one only Fenrir reaches on this PC (Palworld's API) is 3.0's
    out["ports"] = [[_words(p["port"], doc) if isinstance(p["port"], str) else str(p["port"]), p["proto"]] for p in doc.get("ports") or []
                    if p.get("friends", True)]
    sources = (doc.get("players") or {}).get("sources") or []
    a2s = next((s for s in sources if s["via"] == "a2s"), None)
    if a2s:
        out["query"] = _words("{port." + a2s["port"] + "}", doc)
    pw = fl.get("password")
    if pw and pw.get("minLength"):
        out["password"] = {"min": pw["minLength"]}
    if (doc.get("mods") or {}).get("source") not in (None, "none"):
        out["mods"] = doc["mods"]["source"]
    out["stopWait"] = (doc.get("stop") or {}).get("grace", 45)
    join = doc.get("join") or {}
    if join.get("withCode"):
        out["joinCode"] = join["withCode"]["code"]
    if join.get("steamArgs"):
        out["connectArgs"] = [_words(x, doc) for x in join["steamArgs"]]
    count = next((s for s in sources if s["via"] == "log" and s.get("count")), None)
    if count:
        out["playersLog"] = count["count"]
    if "kit" in get:
        out["kit"] = get["kit"]["kit"]
    out["join"] = _words(join.get("text") or "", doc)
    if join.get("withCode"):
        out["joinWithCode"] = _words(join["withCode"]["text"], doc)
    return out


def recipes(ids):
    """{id: the 2.x dict} for those built-in recipes that load; one that does not is left out (2.x's own dict stands in)."""
    found = load.builtin()
    return {i: view(found[i]) for i in ids if i in found}
