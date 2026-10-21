"""What the fake server plays for one run (recipes.md section 3.11.1), worked out from the recipe and its tests.harness
section with every placeholder filled from the server's own context, the one its driver fills the command line from. And
what the fake installs lay down: the program the recipe runs, the files its writers start from, and the archive a
download recipe fetches.

    play(doc, vals, ctx, report)     the play file's contents
    layout(doc)                      the files an install must leave for the recipe to run
    archive(entry, doc, folder)      a fake download for one of the recipe's files: the bytes and their sha256

A save file is under the save folder ({saves}) unless it names its root itself, as {install}/Pal/Saved/SaveGames/0/world.sav
does. Nothing here starts a process or opens a port."""
import hashlib
import io
import json
import tarfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from .. import values

PLACEHOLDER = "Fake program for Fenrir's recipe harness: the harness starts its fake server in place of this file.\n"
TEMPLATES = {  # what a writer's "from" file holds before Fenrir writes it: each key it sets is there, empty
    "xml-property": lambda keys: '<?xml version="1.0"?>\n<ServerSettings>\n'
                                 + "".join(f'  <property name="{k}" value="" />\n' for k in keys) + "</ServerSettings>\n",
    "ini": lambda keys: "".join(f"[{s}]\n" + "".join(f"{k.rsplit('/', 1)[1]}=\n" for k in keys if k.rsplit("/", 1)[0] == s) + "\n"
                                for s in dict.fromkeys(k.rsplit("/", 1)[0] for k in keys if "/" in k)),
    "properties": lambda keys: "".join(f"{k}=\n" for k in keys),
    "keyvalue": lambda keys: "".join(f"{k}=\n" for k in keys),
    "json": lambda keys: "{}\n",
    "lines": lambda keys: "",
}


def harness_of(doc):
    return dict(((doc.get("tests") or {}).get("harness")) or {})


def _port_of_url(template, ctx):
    try:
        return urllib.parse.urlsplit(values.fill(template, ctx)).port
    except ValueError:
        return None


def serve_kinds(doc, vals, ctx):
    """[{port, proto, serve}] for the ports the recipe takes with these settings: Steam's query on every UDP port, and on
    TCP whatever the recipe reaches it with (RCON, Minecraft's status, HTTP for its probes, REST and players), else a
    plain accept."""
    ports = values.ports(doc, vals)
    by_id = {p["id"]: p for p in ports}
    kind = {}
    probes = [*(doc.get("ready") or {}).get("any", []), *(doc.get("ready") or {}).get("all", [])]
    if (doc.get("health") or {}).get("probe"):
        probes.append(doc["health"]["probe"])
    for pr in probes:
        q = pr.get("query")
        if q in ("minecraft", "rcon") and pr.get("on") in by_id:
            kind[by_id[pr["on"]]["port"]] = q
        if "http" in pr:
            n = _port_of_url(pr["http"]["url"], ctx)
            if n:
                kind[n] = "http"
    for s in (doc.get("players") or {}).get("sources") or []:
        if s["via"] in ("rcon", "minecraft") and s.get("port") in by_id:
            kind[by_id[s["port"]]["port"]] = s["via"]
        if s["via"] == "rest" and s.get("url"):
            n = _port_of_url(s["url"], ctx)
            if n:
                kind[n] = "http"
    for st in (doc.get("stop") or {}).get("steps") or []:
        if st["do"] == "rest" and st.get("url"):
            n = _port_of_url(st["url"], ctx)
            if n:
                kind[n] = "http"
    rcon = doc.get("rcon") or {}  # REC-2's section: the port Fenrir reaches RCON on
    if rcon.get("port") in by_id:
        kind[by_id[rcon["port"]]["port"]] = "rcon"
    if "rcon" in by_id:
        kind.setdefault(by_id["rcon"]["port"], "rcon")
    if doc.get("kind") == "minecraft" and "game" in by_id:
        kind.setdefault(by_id["game"]["port"], "minecraft")
    out = []
    for p in ports:
        serve = "a2s" if p["proto"] == "udp" else kind.get(p["port"]) or ("http" if p.get("http") else "tcp")
        out.append({"port": p["port"], "proto": p["proto"], "serve": serve, "id": p["id"]})
    return out


def save_file(doc, ctx):
    """Where the fake writes its save: under {saves}, or under the root the harness names."""
    name = harness_of(doc).get("saveFile")
    if not name:
        return None
    if name.startswith("{"):
        return str(Path(values.fill(name, ctx)))
    if not ctx.get("saves"):
        return None
    return str(Path(ctx["saves"]) / PurePosixPath(name))


def _writer_for(doc, path, ctx):
    for w in doc.get("files") or []:
        try:
            if Path(values.fill(w["file"], ctx)) == Path(path):
                return w
        except Exception:  # noqa: BLE001 - a writer whose path cannot be filled is not this one
            continue
    return None


def _setting_value(doc, vals, *names):
    fl = values.fields(doc)
    for n in names:
        if n in fl and vals.get(n) not in (None, ""):
            return vals[n]
    return None


def _rcon_password(doc, vals):
    ref = (doc.get("rcon") or {}).get("password")
    if isinstance(ref, str) and ref.startswith("setting."):
        return vals.get(ref.split(".", 1)[1]) or ""
    for k, f in values.fields(doc).items():
        if f["type"] == "secret" and "rcon" in k.lower():
            return vals.get(k) or ""
    return None  # none named: the fake takes any password


def play(doc, vals, ctx, report, crash=None, slow_stop=False, no_ready=False):
    """The play file for one run of this recipe with these settings (vals) and folders (ctx, as the driver fills them)."""
    h = harness_of(doc)
    run, chat = doc.get("run") or {}, doc.get("chat") or {}
    expect_files = []
    for template, keys in (h.get("expectFiles") or {}).items():
        path = values.fill(template, ctx)
        w = _writer_for(doc, path, ctx) or {}
        expect_files.append({"path": str(Path(path)), "format": w.get("format", "properties"), "sep": w.get("sep") or "=",
                             "encoding": w.get("encoding") or "utf-8", "expect": keys})
    stop_lines, rest_stop = [], None
    for st in (doc.get("stop") or {}).get("steps") or []:
        if st["do"] == "command":
            stop_lines.append(st["line"])
        elif st["do"] == "rcon":
            stop_lines.append(st["command"])
        elif st["do"] == "rest" and st.get("url"):
            u = urllib.parse.urlsplit(values.fill(st["url"], ctx))
            rest_stop = {"method": st.get("method", "POST"), "path": u.path or "/"}
    rest_players = next((s for s in (doc.get("players") or {}).get("sources") or [] if s["via"] == "rest"), None)
    env = run.get("env") or {}
    steam_app = env.get("SteamAppId") or next(iter((doc.get("client") or {}).get("steam") or []), None)
    out = run.get("output")
    return {
        "program": run.get("program") or "",
        "expectArgs": [values.fill(a, ctx) for a in h["expectArgs"]] if "expectArgs" in h else None,
        "expectFiles": expect_files,
        "env": sorted(env),
        "bootLines": list(h.get("bootLines") or []),
        "readyAfter": h.get("readyAfter", 1),
        "readyLines": [x for x in (h.get("readyLine"), h.get("codeLine"), h.get("countLine")) if x],
        "countLine": h.get("countLine"), "joinLine": h.get("joinLine"), "leaveLine": h.get("leaveLine"), "chatLine": h.get("chatLine"),
        "players": list(h.get("players") or []),
        "ports": serve_kinds(doc, vals, ctx),
        "a2s": {"name": str(_setting_value(doc, vals, "name", "motd") or doc.get("name") or "Fenrir"),
                "map_name": str(_setting_value(doc, vals, "world", "map") or "Fenrir"), "game": doc.get("name") or "Fenrir",
                "folder": doc.get("id") or "fenrir", "appid": int(steam_app) if str(steam_app or "").isdigit() else 0,
                "max_players": int(_setting_value(doc, vals, "players", "maxPlayers") or 10)},
        "rcon": {"password": _rcon_password(doc, vals)},
        "http": {"stop": rest_stop, "players": {"path": rest_players.get("path")} if rest_players else None},
        "console": run.get("console", "none"),
        "stopLines": stop_lines,
        "chatSend": (chat.get("send") or {}).get("command"),
        "saveOn": list(h.get("saveOn") or []),
        "saveFile": save_file(doc, ctx),
        "alsoWrite": [str(ctx["log"])] if isinstance(out, dict) and ctx.get("log") else [],
        "report": str(report),
        "crash": crash, "slowStop": bool(slow_stop), "noReady": bool(no_ready),
    }


# ---------------------------------------------------------------------------------------------------- what an install leaves
def layout(doc):
    """{relative path: text} an install must leave in the program folder: the program (and first-run programs), and the
    files the writers start from ("from"), each key there and empty. Paths that start elsewhere ({data}, {game}) are not
    the install's to make."""
    run, out = doc.get("run") or {}, {}
    progs = [run.get("program") or ""] + [st["program"] for st in doc.get("firstRun") or []]
    for prog in progs:
        if prog and not prog.startswith("{"):
            out[PurePosixPath(prog.replace("\\", "/")).as_posix()] = PLACEHOLDER
    for w in doc.get("files") or []:
        src = w.get("from") or ""
        if src.startswith("{install}/"):
            rel = src[len("{install}/"):]
            out[rel] = TEMPLATES.get(w["format"], lambda k: "")(list((w.get("set") or {}).keys()))
    return out


def minecraft_layout(loader, loader_version=None, mc_version=None):
    """{relative path: text} a Minecraft loader's installer leaves in a server folder, as core/loaders.py's detect_server
    reads one: the files it finds the loader, its versions and the launch line by (the jar names the New world wizard
    gives a plain server), and the memory file every launch line starts with (REC-4)."""
    mc = mc_version or "1.21.1"
    out = {"user_jvm_args.txt": "-Xms1G\n-Xmx2G\n", "eula.txt": "eula=true\n"}
    if loader == "neoforge":
        out[f"libraries/net/neoforged/neoforge/{loader_version}/win_args.txt"] = PLACEHOLDER
        out[f"libraries/net/minecraft/server/{mc}/server-{mc}.jar"] = PLACEHOLDER
    elif loader == "forge":
        out[f"libraries/net/minecraftforge/forge/{mc}-{loader_version}/win_args.txt"] = PLACEHOLDER
    elif loader in ("fabric", "quilt"):
        out[f"{loader}-server-launch.jar"] = PLACEHOLDER
        out["server.jar"] = PLACEHOLDER
    elif loader == "vanilla":
        out[f"minecraft_server.{mc}.jar"] = PLACEHOLDER
    elif loader in ("paper", "purpur"):
        out[f"{loader}-{mc}-1.jar"] = PLACEHOLDER
    return out


def lay_down(folder, files):
    for rel, text in files.items():
        p = Path(folder) / PurePosixPath(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")


def archive(entry, doc):
    """(bytes, sha256) of a fake download for one of the recipe's download files: the install's layout packed the way
    the entry says (zip, tar.gz, tar.xz, or the program alone for none), under as many top folders as it strips."""
    files = layout(doc)
    unpack, strip = entry.get("unpack", "zip"), int(entry.get("strip") or 0)
    top = "/".join(f"top{i}" for i in range(strip))
    buf = io.BytesIO()
    if unpack == "none":
        buf.write(PLACEHOLDER.encode("utf-8"))
    elif unpack == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, text in sorted(files.items()):
                z.writestr(f"{top}/{rel}" if top else rel, text)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz" if unpack == "tar.gz" else "w:xz") as t:
            for rel, text in sorted(files.items()):
                data = text.encode("utf-8")
                info = tarfile.TarInfo(f"{top}/{rel}" if top else rel)
                info.size, info.mode = len(data), 0o755
                t.addfile(info, io.BytesIO(data))
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
    return Path(path)
