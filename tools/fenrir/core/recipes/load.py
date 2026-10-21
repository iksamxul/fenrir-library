"""Recipe files read strictly, hashed and checked (recipes.md sections 3.9 and 3.12): the one way a recipe reaches the rest of
Fenrir. A file is at most 256 KB of UTF-8 JSON, one object, no key twice, no NaN or Infinity, nested at most 32 deep. Then
come the schema (schema.py) and Fenrir's own rules for where it came from (policy.py). Its sha256 is of the bytes as read,
so the trust record and server.json's recipe reference name exactly what was checked.

    fenrir/recipes/recipe.schema.json      the schema, shipped beside static/ (core/util.py RECIPE_DIR)
    fenrir/recipes/builtin/<id>.json       the built-in recipes, trusted: they ship inside the signed update"""
import codecs
import hashlib
import json
import platform
import threading
from pathlib import Path

from ..util import RECIPE_DIR
from . import Problem, RecipeError, policy, schema, values

MAX_BYTES = 256 * 1024  # the API's cap for a recipe body too (docs/v3-design/api3.md section 3.7)
MAX_DEPTH = 32
BUILTIN = RECIPE_DIR / "builtin"
_lock = threading.Lock()
_builtin = {"key": None, "recipes": {}, "broken": {}}


def here():
    """This machine as a recipe names platforms: (windows or linux, x64 or arm64). On Linux the engine's layer says
    (core/oslayer): a processor Fenrir has no builds for (32-bit ARM, x86) keeps its own name, so no download fits it."""
    from .. import oslayer
    if oslayer.system() != "windows":
        return "linux", oslayer.arch()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return "windows", arch


class Recipe:
    """One checked recipe: its document, where it came from (builtin, library, local, imported), the sha256 of what was
    read, and the file it came from (None for one kept inside a server's own server.json)."""
    __slots__ = ("doc", "source", "sha256", "path", "raw")

    def __init__(self, doc, source, sha256, path=None, raw=None):
        self.doc, self.source, self.sha256, self.path, self.raw = doc, source, sha256, path, raw  # raw: the bytes the sha256 is of

    id = property(lambda self: self.doc["id"])
    version = property(lambda self: self.doc["version"])
    kind = property(lambda self: self.doc["kind"])
    name = property(lambda self: self.doc["name"])

    def ref(self):
        """What server.json keeps of the recipe it was made from (recipes.md section 3.9)."""
        return {"id": self.id, "version": self.version, "source": self.source, "sha256": self.sha256}

    def view(self, where=None):
        """The recipe as it runs on one platform (this machine's when none is given), overrides merged."""
        return policy.view(self.doc, where or here()[0])

    def runs_here(self):
        os_name, arch = here()
        return arch in ((self.doc.get("platforms") or {}).get(os_name) or {}).get("arch", [])

    def setting(self, key):
        return values.fields(self.doc).get(key)

    def port(self, pid):
        return next((p for p in self.doc.get("ports") or [] if p["id"] == pid), None)

    def __repr__(self):
        return f"<Recipe {self.id} {self.version} from {self.source}>"


class _Twice(Exception):
    pass


def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:  # JSON lets the last one win quietly: a checked key could then hide a different one later in the file
            raise _Twice(k)
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"{name} is not a JSON number")


def _depth(x, n=1):
    if n > MAX_DEPTH:
        return n
    kids = x.values() if isinstance(x, dict) else x if isinstance(x, list) else ()
    return max([n] + [_depth(k, n + 1) for k in kids])


def _fail(say, code="file"):
    return RecipeError([Problem("/", say, code)])


def parse(raw):
    """The recipe's document from its bytes, read strictly; RecipeError says why not."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise _fail(f"The file is larger than {MAX_BYTES // 1024} KB")
    if raw.startswith(codecs.BOM_UTF8):  # Notepad's byte-order mark: the text after it is what counts
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("The file is not UTF-8 text") from None
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant)
    except _Twice as e:
        raise _fail(f"The file says {e.args[0]} twice in one place") from None
    except RecursionError:
        raise _fail("The file is nested too deep") from None
    except json.JSONDecodeError as e:
        raise _fail(f"The file is not JSON ({e.msg}, line {e.lineno})") from None
    except ValueError as e:
        raise _fail(f"The file is not JSON Fenrir reads ({e})") from None
    if not isinstance(doc, dict):
        raise _fail("The file is not one JSON object")
    if _depth(doc) > MAX_DEPTH:
        raise _fail("The file is nested too deep")
    return doc


def check(doc, source="builtin"):
    """Every problem with a recipe's document for its source: the schema's, else Fenrir's own rules; [] when none."""
    return schema.problems(doc) or policy.problems(doc, source)


def read(raw, source="builtin", path=None):
    """A Recipe from a file's bytes, or RecipeError with every problem found."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    doc = parse(raw)
    said = check(doc, source)
    if said:
        rid = doc.get("id")
        named = isinstance(rid, str) and schema.load().valid(rid, {"$ref": "#/$defs/id"})
        raise RecipeError(said, f"The recipe {rid} cannot be used" if named else "This recipe cannot be used")
    return Recipe(doc, source, hashlib.sha256(raw).hexdigest(), path, raw)


def read_file(path, source="builtin"):
    """A Recipe from a file on disk; one too large is refused before it is read."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise _fail(f"The file is larger than {MAX_BYTES // 1024} KB")
        raw = path.read_bytes()
    except OSError as e:
        raise _fail(f"The file could not be read ({e.strerror or e})") from None
    return read(raw, source, path)


def from_doc(doc, source="local"):
    """A Recipe from a document already parsed, such as your own server's kept in its server.json. Its sha256 is of the
    document written one fixed way (keys sorted, no spaces), so the same recipe always hashes the same."""
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return read(raw, source)


def builtin(folder=None):
    """The built-in recipes by id, read again only when a file there changed. A broken one is left out and said in
    broken(): one bad file never takes the others with it."""
    folder = Path(folder) if folder else BUILTIN
    try:
        files = sorted(p for p in folder.glob("*.json") if p.is_file())
        key = (str(folder), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files))
    except OSError:
        files, key = [], (str(folder), ())
    with _lock:
        if _builtin["key"] != key:
            recipes, broken = {}, {}
            for p in files:
                try:
                    r = read_file(p, "builtin")
                    if p.stem != r.id:
                        raise _fail(f"The file is named {p.name}, and its recipe's id is {r.id}", "id")
                    if r.id in recipes:
                        raise _fail(f"A second recipe says it is {r.id}", "id")
                    recipes[r.id] = r
                except RecipeError as e:
                    broken[p.name] = e.problems
            _builtin.update(key=key, recipes=recipes, broken=broken)
        return dict(_builtin["recipes"])


def broken(folder=None):
    """The built-in recipe files that were left out, with their problems (the tests keep this empty)."""
    builtin(folder)
    with _lock:
        return dict(_builtin["broken"])
