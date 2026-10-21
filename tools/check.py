#!/usr/bin/env python3
"""The library's checks on one recipe folder (docs/v3-design/library.md section 3.3, steps 2 to 6). The workflow
(.github/workflows/check.yml) runs them on every submission, and anyone can run them on their own PC:

    python -I tools/check.py schema    <folder>        jsonschema and Fenrir's own validator, which must agree
    python -I tools/check.py policy    <folder>        Fenrir's rules as the library reads a recipe, and the library's own
    python -I tools/check.py harness   <folder>        the fake-server harness's whole life, on this PC's platform
    python -I tools/check.py downloads <folder>        each download fetched and hashed: nothing is written, run or unpacked
    python -I tools/check.py inside    <folder>        "What is inside": what Fenrir's trust prompt shows before it is used
    python -I tools/check.py all       <folder> [--downloads]   schema, policy, harness and inside, one after another

<folder> is a recipe's folder: recipes/<id>/ with recipe.json and README.md in it (examples/example-valheim/ is one).
The exit code is 0 when the step passed and 1 when it did not. In the workflow each step also writes its answer to
$GITHUB_OUTPUT as one line of JSON, and a summary to $GITHUB_STEP_SUMMARY.

The checks are Fenrir's own, from the pinned copy in tools/fenrir/ (tools/MANIFEST.json says which Fenrir it is from),
with Fenrir's data kept in a new temporary folder, never a real Fenrir's. Fenrir checks everything again on its
maker's PC before anything is signed, and again on every PC before anything is used: these checks are the first
of three, not the only one."""
import hashlib
import http.client
import ipaddress
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STARTUP = set(sys.modules)  # what the interpreter loaded before any check: the trace leaves these out
TOOLS = Path(__file__).resolve().parent
LIBRARY = TOOLS.parent
FENRIR = TOOLS / "fenrir"
SCHEMA = LIBRARY / "schema" / "recipe-1.json"
README_MAX = 64 * 1024  # as the signing step (fenrir/build/sign_library.py)
ICON_MAX = 64 * 1024
DOWNLOAD_MAX = 2 << 30  # 2 GB
DOWNLOAD_DEADLINE = 600  # seconds for one file
READ_TIMEOUT = 30
STEPS = ("schema", "policy", "harness", "downloads", "inside")


class Result:
    """One step's answer: what failed, what to look at, and what it saw."""

    def __init__(self, step):
        self.step, self.fails, self.warns, self.notes, self.extra = step, [], [], [], {}

    @property
    def ok(self):
        return not self.fails

    def as_json(self):
        return {"step": self.step, "ok": self.ok, "fails": self.fails[:60], "warns": self.warns[:60], "notes": self.notes[:200], **self.extra}


# ---------------------------------------------------------------------------------------------------- the pinned copy
def fenrir():
    """Fenrir's pinned copy on the path, and its data in a new folder of its own."""
    if str(FENRIR) not in sys.path:
        sys.path.insert(0, str(FENRIR))
    if not os.environ.get("FENRIR_LIBRARY_DATA"):
        os.environ["FENRIR_LIBRARY_DATA"] = tempfile.mkdtemp(prefix="fenrir-library-data-")
    os.environ["FENRIR_DATA"] = os.environ["FENRIR_LIBRARY_DATA"]


def offline():
    """From here on this process reaches nothing beyond this PC: a connection, a datagram or a name lookup for any
    address but the loopback one is refused. Python's audit hooks cannot be taken away again once added. The harness
    needs no network (its downloads come from its own server on 127.0.0.1): this holds it to that."""
    def local(host):
        host = str(host or "").strip("[]").split("%")[0]
        if host in ("", "localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def hook(event, args):
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            addr = args[1] if len(args) > 1 else None
            if isinstance(addr, tuple) and addr and not local(addr[0]):
                raise PermissionError(f"The library's harness reaches nothing beyond this PC (it tried {addr[0]}).")
        elif event == "socket.getaddrinfo":
            if args and args[0] is not None and not local(args[0].decode() if isinstance(args[0], bytes) else args[0]):
                raise PermissionError(f"The library's harness looks up no name on the network (it tried {args[0]!r}).")
        elif event in ("socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"):
            if args and not local(args[0]):
                raise PermissionError(f"The library's harness looks up no name on the network (it tried {args[0]!r}).")

    sys.addaudithook(hook)


ENTRY_FILES = {"recipe.json": 256 * 1024, "README.md": README_MAX, "icon.png": ICON_MAX, "icon.webp": ICON_MAX}


def folder_problems(folder):
    """What is wrong with the folder itself, before any file in it is read: only an entry's own files, each a plain file
    (never a link to somewhere else) and within its size."""
    folder, said = Path(folder), []
    if folder.is_symlink() or not folder.is_dir():
        return [f"{folder.name} is not a plain folder."]
    for p in sorted(folder.iterdir()):
        if p.name not in ENTRY_FILES:
            said.append(f"{p.name}: an entry holds only recipe.json, README.md and an icon (icon.png or icon.webp).")
        elif p.is_symlink() or not p.is_file():
            said.append(f"{p.name} is not a plain file.")
        elif p.stat().st_size > ENTRY_FILES[p.name]:
            said.append(f"{p.name} is larger than {ENTRY_FILES[p.name] // 1024} KB.")
    if not (folder / "recipe.json").is_file():
        said.append("There is no recipe.json.")
    return said


def _raw(folder):
    return (Path(folder) / "recipe.json").read_bytes()


def _read(folder, r):
    """The recipe as the library reads it (Fenrir's load.read, source library), or None with its problems said."""
    from core.recipes import RecipeError, load
    try:
        return load.read(_raw(folder), "library", Path(folder) / "recipe.json")
    except OSError as e:
        r.fails.append(f"recipe.json could not be read ({e.strerror or e}).")
    except RecipeError as e:
        r.fails.extend(f"{p.where}: {p.say} [{p.code}]" if p.where not in ("", "/") else f"{p.say} [{p.code}]" for p in e.problems)
    return None


def _text(value):
    """A message of Fenrir's, or the recipe's own words, as English text."""
    if isinstance(value, dict) and "raw" in value:
        return str(value["raw"])
    return str(value)


def _missing_message(value):
    """True when a message of Fenrir's has no English in the tools' copy (it shows as its own id)."""
    ref = getattr(value, "ref", None)
    return callable(ref) and str(value) == (ref() or {}).get("id")


# ---------------------------------------------------------------------------------------------------- step 2: schema
def step_schema(folder, r):
    fenrir()
    from core.recipes import RecipeError, load, schema
    try:
        doc = load.parse(_raw(folder))
    except OSError as e:
        r.fails.append(f"recipe.json could not be read ({e.strerror or e}).")
        return
    except RecipeError as e:
        r.fails.append(f"recipe.json: {e}")
        return
    ours = schema.problems(doc)
    try:
        import jsonschema
    except ImportError:
        r.fails.append("jsonschema is not installed here: python -m pip install -r tools/requirements.txt")
        return
    if SCHEMA.read_bytes() != (FENRIR / "recipes" / "recipe.schema.json").read_bytes():
        r.fails.append("schema/recipe-1.json is not the schema of the Fenrir in tools/: the tools need refreshing (tools/README.md).")
    spec = json.loads(SCHEMA.read_text("utf-8"))
    theirs = sorted(jsonschema.Draft202012Validator(spec).iter_errors(doc), key=lambda e: [str(x) for x in e.absolute_path])
    r.fails.extend(f"Fenrir's validator: {p}" for p in ours[:25])
    r.fails.extend(f"jsonschema: /{'/'.join(str(x) for x in e.absolute_path)}: {e.message[:300]}" for e in theirs[:25])
    if bool(ours) != bool(theirs):
        r.fails.append("The two validators disagree about this recipe: one takes it and the other does not. That is a fault in one "
                       "of them, and Fenrir's maker looks into it.")
    if r.ok:
        from importlib.metadata import version
        r.notes.append(f"Valid against schema/recipe-1.json, by jsonschema {version('jsonschema')} and by Fenrir's own validator")


# ---------------------------------------------------------------------------------------------------- step 3: policy
def _version(v):
    return tuple(int(x) for x in str(v).split("."))


def _withdrawn(rid, version, sha):
    try:
        listed = json.loads((LIBRARY / "revoked.json").read_text("utf-8"))
    except (OSError, ValueError):
        return "revoked.json could not be read"
    for w in listed if isinstance(listed, list) else []:
        if not isinstance(w, dict):
            continue
        if w.get("sha256") == sha or (w.get("id") == rid and w.get("version") == version):
            return str(w.get("reason") or "no reason given")
    return None


def step_policy(folder, r):
    fenrir()
    from core.recipes import policy, share
    from core.recipes.harness import regexes
    folder = Path(folder)
    rec = _read(folder, r)
    if rec is None:
        return
    doc, raw = rec.doc, _raw(folder)
    sha = hashlib.sha256(raw).hexdigest()
    r.extra["platforms"] = sorted(doc.get("platforms") or {})
    r.extra["id"], r.extra["version"] = rec.id, rec.version
    if folder.name != rec.id:
        r.fails.append(f"The folder is recipes/{folder.name}/, and the recipe's id is {rec.id}: the folder is named by the id.")
    reserved = set(json.loads((TOOLS / "reserved-ids.json").read_text("utf-8")).get("ids") or [])
    if rec.id in reserved:
        r.fails.append(f"{rec.id} is the id of one of Fenrir's own recipes. Built-in ids are reserved: choose another.")
    said = _withdrawn(rec.id, rec.version, sha)
    if said:
        r.fails.append(f"{rec.id} {rec.version} was withdrawn from the library ({said}). Send a newer version.")
    kept = LIBRARY / "recipes" / rec.id / "recipe.json"
    if kept.is_file() and kept.resolve() != (folder / "recipe.json").resolve():
        try:
            before = json.loads(kept.read_text("utf-8")).get("version")
            if kept.read_bytes() == raw:
                r.notes.append(f"recipe.json is the same as the library's {rec.id} {before}.")
            elif _version(rec.version) <= _version(before):
                r.fails.append(f"The library has {rec.id} {before} already. A changed recipe needs a higher version than {before}.")
            else:
                r.notes.append(f"An update of the library's {rec.id} {before} to {rec.version}.")
        except (OSError, ValueError, AttributeError):
            r.warns.append(f"The library's own recipes/{rec.id}/recipe.json could not be read to compare versions.")
    try:
        readme = (folder / "README.md").read_bytes()
        if not readme.strip():
            r.fails.append("README.md is empty: it holds the research notes (sources, the live run, quirks).")
        elif len(readme) > README_MAX:
            r.fails.append(f"README.md is larger than {README_MAX // 1024} KB.")
    except OSError:
        r.fails.append("There is no README.md beside recipe.json: it holds the research notes (sources, the live run, quirks).")
    icon_name = doc.get("icon") or next((n for n in ("icon.png", "icon.webp") if (folder / n).is_file()), None)
    if icon_name:
        try:
            data = (folder / icon_name).read_bytes()
            if len(data) > ICON_MAX:
                r.fails.append(f"The icon is larger than {ICON_MAX // 1024} KB.")
            elif not (data.startswith(b"\x89PNG\r\n\x1a\n") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
                r.fails.append("The icon is neither a PNG nor a WebP picture.")
        except OSError:
            r.fails.append(f"The recipe names its icon {icon_name}, and it is not beside recipe.json.")
    fails, warns = share.scan(doc, names={"pc": [], "account": []})
    r.fails.extend(f"Share check: {f}" for f in fails)
    r.warns.extend(f"Share check: {w}" for w in warns)
    pats = list(dict.fromkeys((w, p) for plat in ("windows", "linux") for w, p, _ in policy._regexes(policy.view(doc, plat))))
    for s in regexes.slow(pats):
        took = "did not finish in time" if s.get("ms") is None else f"took {round(s['ms'])} ms"
        r.fails.append(f"{s['where']}: the pattern {s['pattern']!r} {took} on one 4 KB line made to be slow.")
    if r.ok:
        r.notes.append(f"{rec.id} {rec.version}: Fenrir's rules for a library recipe hold (anti-cheat, sources, paths, programs, "
                       f"downloads, secrets); the id is free; {len(pats)} patterns timed; the share check found nothing")


# ---------------------------------------------------------------------------------------------------- step 4: harness
def step_harness(folder, r):
    fenrir()
    offline()
    from core.recipes.harness import run
    report = run.run(str(Path(folder) / "recipe.json"), origin="library")
    r.notes.extend(run.text(report).splitlines())
    r.extra["platform"], r.extra["status"] = report.get("platform"), report.get("status")
    if report.get("status") != "passed":
        bad = next((s for s in report.get("steps") or [] if s.get("ok") is False), None)
        why = f" at {bad['step']}: {str(bad.get('said'))[:400]}" if bad else ""
        r.fails.append(f"The harness's life on {report.get('platform')} is {report.get('status')}{why}")


# ---------------------------------------------------------------------------------------------------- step 5: downloads
class _Refused(Exception):
    pass


def _public(host):
    """A host that is a name on the Internet, never an address of this network or this machine."""
    host = (host or "").strip("[]").lower()
    try:
        ipaddress.ip_address(host)
        raise _Refused(f"{host} is an address, not a name")
    except ValueError:
        pass
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")) or "." not in host:
        raise _Refused(f"{host or 'it'} is not a name on the Internet")
    return host


class _Connection(http.client.HTTPSConnection):
    """HTTPS that answers only from an address on the Internet (no private, loopback or link-local one, whatever a name
    resolves to)."""

    def connect(self):
        super().connect()
        peer = self.sock.getpeername()[0]
        if not ipaddress.ip_address(str(peer).split("%")[0]).is_global:
            self.sock.close()
            raise _Refused(f"{self.host} answered from {peer}, which is not an address on the Internet")


class _HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_Connection, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).startswith("https://"):
            raise _Refused(f"it sends Fenrir on to {str(newurl)[:120]}, which is not https")
        _public(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPS(), _Redirects())


def hash_download(url, limit=DOWNLOAD_MAX, deadline=DOWNLOAD_DEADLINE, opener=None):
    """(sha256, size, said) of one download: a HEAD request, then a GET read in a stream and hashed. Nothing is written
    to the disk, run or unpacked."""
    if not str(url).startswith("https://"):
        raise _Refused("it is not an https address")
    _public(urllib.parse.urlsplit(url).hostname)
    opener = opener or _opener()
    headers = {"User-Agent": "fenrir-library check (download hash)", "Accept-Encoding": "identity"}
    said = []
    try:
        with opener.open(urllib.request.Request(url, method="HEAD", headers=headers), timeout=READ_TIMEOUT) as h:
            length = h.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise _Refused(f"it says it is {int(length):,} bytes, more than the {limit:,} checked here")
    except urllib.error.HTTPError as e:  # some hosts take no HEAD: the GET still tells
        said.append(f"HEAD answered {e.code}")
    h, n, t0 = hashlib.sha256(), 0, time.monotonic()
    with opener.open(urllib.request.Request(url, headers=headers), timeout=READ_TIMEOUT) as resp:
        while chunk := resp.read(1 << 20):
            n += len(chunk)
            if n > limit:
                raise _Refused(f"it is larger than the {limit:,} bytes checked here")
            if time.monotonic() - t0 > deadline:
                raise _Refused(f"it took longer than {deadline // 60} minutes")
            h.update(chunk)
    return h.hexdigest(), n, said


def step_downloads(folder, r, hasher=None):
    fenrir()
    rec = _read(folder, r)
    if rec is None:
        return
    files = {}
    for plat in sorted(rec.doc.get("platforms") or {}):
        for x in ((rec.view(plat).get("get") or {}).get("download") or {}).get("files") or []:
            files.setdefault(x["url"], x)
    if not files:
        r.notes.append("Nothing to fetch: this recipe gets its server another way (SteamCMD's app ids are not downloaded here).")
        return
    for url, x in files.items():
        try:
            got, size, said = (hasher or hash_download)(url)
        except (_Refused, OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as e:
            r.fails.append(f"{url}: could not be checked ({str(getattr(e, 'reason', None) or e)[:300]})")
            continue
        if got != x.get("sha256"):
            r.fails.append(f"{url}: its SHA-256 is {got}, and the recipe says {x.get('sha256')}")
        elif isinstance(x.get("size"), int) and x["size"] != size:
            r.fails.append(f"{url}: it is {size:,} bytes, and the recipe says {x['size']:,}")
        else:
            r.notes.append(f"{url}: {size:,} bytes, SHA-256 as the recipe says" + (f" ({'; '.join(said)})" if said else ""))


# ---------------------------------------------------------------------------------------------------- step 6: inside
PLATFORM_NAMES = {"windows": "Windows", "linux": "Linux"}


def inside_text(rec, platform):
    """The trust prompt's "What is inside" for one platform, as plain English lines."""
    from core.recipes import trust
    p = trust.prompt(rec, platform=platform)
    lines, missing = [f"{_text(p['title'])} (on {PLATFORM_NAMES.get(platform, platform)})"], 0
    for s in p.get("sections") or []:
        lines.append("")
        lines.append(_text(s["title"]))
        for x in s.get("lines") or []:
            missing += _missing_message(x)
            lines.extend("  " + t for t in _text(x).splitlines() or [""])
    missing += _missing_message(p["title"]) + sum(_missing_message(s["title"]) for s in p.get("sections") or [])
    return lines, missing


def step_inside(folder, r):
    fenrir()
    rec = _read(folder, r)
    if rec is None:
        return
    missing = 0
    for plat in sorted(rec.doc.get("platforms") or {}):
        lines, n = inside_text(rec, plat)
        missing += n
        if r.notes:
            r.notes.append("")
        r.notes.extend(lines)
    if missing:
        r.warns.append(f"{missing} of Fenrir's messages have no English in the tools' copy and show as their ids: the tools need refreshing.")


# ---------------------------------------------------------------------------------------------------- answers
def _fence(lines):
    text = "\n".join(lines)
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}text\n{text}\n{ticks}"


def finish(r):
    """Say the answer: on the screen, to the workflow's outputs (one line of JSON) and its summary."""
    head = f"{r.step}: {'passed' if r.ok else 'FAILED'}"
    print(head)
    for title, items in (("Failed", r.fails), ("Look at", r.warns), ("", r.notes)):
        for x in items:
            print(f"  {title + ': ' if title else ''}{x}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("result=" + json.dumps(r.as_json(), ensure_ascii=True, separators=(",", ":")) + "\n")
            for k in ("platforms",):
                if k in r.extra:
                    f.write(f"{k}=" + json.dumps(r.extra[k], separators=(",", ":")) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### {head}\n\n{_fence([*(f'Failed: {x}' for x in r.fails), *(f'Look at: {x}' for x in r.warns), *r.notes]) if (r.fails or r.warns or r.notes) else ''}\n\n")
    return 0 if r.ok else 1


def trace():
    """FENRIR_LIBRARY_TRACE: the files of tools/fenrir this run imported, and any module from outside it and the
    standard library (fenrir/build/library_tools.py --check holds the copy to these)."""
    mine, outside = set(), set()
    for name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None)
        if name in STARTUP or not f:
            continue
        p = Path(f).resolve()
        if p.is_relative_to(FENRIR):
            mine.add(p.relative_to(LIBRARY).as_posix())
        elif not p.is_relative_to(TOOLS) and name.split(".")[0] not in sys.stdlib_module_names:
            outside.add(name)
    return {"fenrir": sorted(mine), "outside": sorted(outside)}


RUN = {"schema": step_schema, "policy": step_policy, "harness": step_harness, "downloads": step_downloads, "inside": step_inside}


def main(argv):
    if len(argv) < 2 or argv[0] not in (*RUN, "all"):
        sys.stderr.write(__doc__)
        return 2
    folder = Path(argv[1])
    if not folder.is_dir():
        sys.stderr.write(f"{folder} is not a folder: give a recipe's folder, such as recipes/<id> or examples/example-valheim.\n")
        return 2
    steps = [argv[0]] if argv[0] != "all" else [s for s in STEPS if s != "downloads" or "--downloads" in argv]
    wrong = folder_problems(folder)
    code = 0
    for step in steps:
        r = Result(step)
        try:
            if wrong:
                r.fails.extend(wrong)
            else:
                RUN[step](folder, r)
        except Exception as e:  # noqa: BLE001 - a check that breaks fails its step, and says how
            r.fails.append(f"The check itself broke ({type(e).__name__}: {str(e)[:300]}). Fenrir's maker looks into it.")
        code = max(code, finish(r))
        if step == "policy" and not r.ok and argv[0] == "all":
            print("The harness and the rest wait until the policy passes.")
            break
    if os.environ.get("FENRIR_LIBRARY_TRACE"):
        print("TRACE " + json.dumps(trace(), separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
