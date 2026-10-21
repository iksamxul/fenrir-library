"""The recipe schema's checker, in the standard library only: Fenrir ships no third-party packages beyond pywebview and
segno, so decision 10 (docs/v3-design/README.md section 5) chose this over a vendored jsonschema.

It reads JSON Schema 2020-12, but only the keywords the recipe schema uses (KEYWORDS). A schema with any other keyword is
refused when it loads, so the schema can never lean on a keyword this checker would quietly skip. The tests hold its
answers to jsonschema's on every example and on a corpus of broken recipes (tests/test_recipes_schema.py), so the
library's GitHub Action, which runs jsonschema, and Fenrir always agree on what is valid. As in jsonschema without a
format checker, "format" is a note only: policy.py checks the dates itself."""
import json
import re
import threading
from functools import lru_cache

from ..util import RECIPE_DIR
from . import Problem

SCHEMA_FILE = RECIPE_DIR / "recipe.schema.json"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
ONE = {"items", "additionalProperties", "propertyNames", "if", "then", "else", "not"}  # each holds one schema
MANY = {"allOf", "anyOf", "oneOf"}  # each holds a list of schemas
NAMED = {"properties", "$defs"}  # each holds name -> schema
PLAIN = {"$schema", "$id", "$ref", "title", "description", "default", "format", "type", "enum", "const", "required",
         "minProperties", "maxProperties", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern",
         "minimum", "maximum"}
KEYWORDS = ONE | MANY | NAMED | PLAIN
TYPES = {"object": "an object", "array": "a list", "string": "text", "integer": "a whole number", "number": "a number",
         "boolean": "true or false", "null": "null"}
_lock = threading.Lock()
_cache = {}


class SchemaError(ValueError):
    """The schema itself asks for something this checker does not do."""


def _is(kind, x):
    """JSON Schema's types as jsonschema reads them for 2020-12: true is not a number, and 3.0 is a whole number."""
    if kind == "object":
        return isinstance(x, dict)
    if kind == "array":
        return isinstance(x, list)
    if kind == "string":
        return isinstance(x, str)
    if kind == "boolean":
        return isinstance(x, bool)
    if kind == "null":
        return x is None
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return kind == "number" or isinstance(x, int) or x.is_integer()


def equal(a, b):
    """JSON equality: 1 equals 1.0, but true never equals 1 (Python's own == says it does)."""
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(v, b[k]) for k, v in a.items())
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


def _canon(x):
    """A value written so that JSON-equal values read the same: 1.0 as 1, keys in order, true never as 1."""
    if isinstance(x, float) and x.is_integer():
        return json.dumps(int(x))
    if isinstance(x, list):
        return "[" + ",".join(_canon(v) for v in x) + "]"
    if isinstance(x, dict):
        return "{" + ",".join(json.dumps(k) + ":" + _canon(x[k]) for k in sorted(x)) + "}"
    return json.dumps(x)


def unique(items):
    """No two entries are equal, in one pass: a long list must not take the square of its length to check."""
    seen = {_canon(v) for v in items}
    return len(seen) == len(items)


@lru_cache(maxsize=512)
def _rx(pattern):
    return re.compile(pattern)


def _ptr(at, key):
    return f"{at}/{str(key).replace('~', '~0').replace('/', '~1')}"


def audit(schema, at="#"):
    """Every keyword is one this checker does, every $ref stays inside the schema, every pattern compiles."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{at} is not a schema")
    for k, v in schema.items():
        if k not in KEYWORDS:
            raise SchemaError(f"{at} uses {k}, which Fenrir's checker does not do")
        if k in ONE:
            audit(v, f"{at}/{k}")
        elif k in MANY:
            if not isinstance(v, list) or not v:
                raise SchemaError(f"{at}/{k} is not a list of schemas")
            for i, sub in enumerate(v):
                audit(sub, f"{at}/{k}/{i}")
        elif k in NAMED:
            for name, sub in v.items():
                audit(sub, f"{at}/{k}/{name}")
        elif k == "$ref" and not str(v).startswith("#"):
            raise SchemaError(f"{at} refers outside the schema ({v})")
        elif k == "type" and any(t not in TYPES for t in (v if isinstance(v, list) else [v])):
            raise SchemaError(f"{at} names a type JSON Schema does not have")
        elif k == "pattern":
            try:
                _rx(v)
            except re.error as e:
                raise SchemaError(f"{at} has a pattern Python cannot read: {e}") from None


class Checker:
    """One schema, ready to check documents against: problems(doc) lists what is wrong, valid(doc) says whether anything is."""

    def __init__(self, schema):
        if not isinstance(schema, dict) or schema.get("$schema") != DIALECT:
            raise SchemaError("Fenrir's checker reads JSON Schema 2020-12 only")
        audit(schema)
        self.root = schema

    def problems(self, doc, limit=50):
        out = []
        for where, say in self._walk(self.root, doc, ""):
            out.append(Problem(where or "/", say))
            if len(out) >= limit:
                break
        return out

    def valid(self, doc, schema=None):
        return next(self._walk(self.root if schema is None else schema, doc, ""), None) is None

    def _ref(self, ref):
        node = self.root
        for part in ref[1:].split("/")[1:] if ref != "#" else []:
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"the schema has no {ref}")
            node = node[part]
        return node

    def _branch(self, schemas, x):
        """Of the forms a value could take, the one it was plainly meant as: its type fits, and an object has the form's
        required keys. None when no single one stands out."""
        typed = []
        for s in schemas:
            s = self._ref(s["$ref"]) if isinstance(s, dict) and set(s) == {"$ref"} else s
            t = s.get("type") if isinstance(s, dict) else None
            if isinstance(s, dict) and (t is None or any(_is(k, x) for k in (t if isinstance(t, list) else [t]))):
                typed.append(s)
        keyed = [s for s in typed if isinstance(x, dict) and all(k in x for k in s.get("required", []))]
        for fits in (keyed, typed):
            if len(fits) == 1:
                return fits[0]
        return None

    def _walk(self, s, x, at):  # noqa: C901 - one keyword after another, as the standard lists them
        if s is True:
            return
        if s is False:
            yield at, "is not allowed here"
            return
        if "$ref" in s:
            yield from self._walk(self._ref(s["$ref"]), x, at)
        t = s.get("type")
        if t is not None:
            kinds = t if isinstance(t, list) else [t]
            if not any(_is(k, x) for k in kinds):
                yield at, "should be " + " or ".join(TYPES[k] for k in kinds)
                return
        if "enum" in s and not any(equal(x, v) for v in s["enum"]):
            yield at, "should be one of " + ", ".join(json.dumps(v) for v in s["enum"][:12])
        if "const" in s and not equal(x, s["const"]):
            yield at, f"should be {json.dumps(s['const'])}"
        if isinstance(x, str):
            if len(x) < s.get("minLength", 0):
                yield at, f"should be at least {s['minLength']} characters long"
            if "maxLength" in s and len(x) > s["maxLength"]:
                yield at, f"should be at most {s['maxLength']} characters long"
            if "pattern" in s and not _rx(s["pattern"]).search(x):
                yield at, "is not written the way this place takes"
        if _is("number", x):
            if "minimum" in s and x < s["minimum"]:
                yield at, f"should be at least {s['minimum']}"
            if "maximum" in s and x > s["maximum"]:
                yield at, f"should be at most {s['maximum']}"
        if isinstance(x, list):
            if len(x) < s.get("minItems", 0):
                yield at, f"should have at least {s['minItems']} entries"
            if "maxItems" in s and len(x) > s["maxItems"]:
                yield at, f"should have at most {s['maxItems']} entries"
            if s.get("uniqueItems") and not unique(x):
                yield at, "has the same entry twice"
            if "items" in s:
                for i, item in enumerate(x):
                    yield from self._walk(s["items"], item, _ptr(at, i))
        if isinstance(x, dict):
            for k in s.get("required", []):
                if k not in x:
                    yield at, f"needs {k}"
            if len(x) < s.get("minProperties", 0):
                yield at, f"should have at least {s['minProperties']} entries"
            if "maxProperties" in s and len(x) > s["maxProperties"]:
                yield at, f"should have at most {s['maxProperties']} entries"
            props = s.get("properties", {})
            for k, v in x.items():
                if k in props:
                    yield from self._walk(props[k], v, _ptr(at, k))
                elif "additionalProperties" in s:
                    if s["additionalProperties"] is False:
                        yield _ptr(at, k), "is not something a recipe can say here"
                    else:
                        yield from self._walk(s["additionalProperties"], v, _ptr(at, k))
                if "propertyNames" in s and not self.valid(k, s["propertyNames"]):
                    yield _ptr(at, k), "is not a name this place takes"
        for sub in s.get("allOf", []):
            yield from self._walk(sub, x, at)
        if "anyOf" in s and not any(self.valid(x, sub) for sub in s["anyOf"]):
            meant = self._branch(s["anyOf"], x)
            yield from self._walk(meant, x, at) if meant is not None else iter([(at, "is not in any form this place takes")])
        if "oneOf" in s:
            n = sum(1 for sub in s["oneOf"] if self.valid(x, sub))
            if n == 0:
                meant = self._branch(s["oneOf"], x)
                yield from self._walk(meant, x, at) if meant is not None else iter([(at, "is not in any form this place takes")])
            elif n > 1:
                yield at, "fits more than one form, so it is unclear which is meant"
        if "not" in s and self.valid(x, s["not"]):
            yield at, "is in a form this place refuses"
        if "if" in s:
            then = s.get("then") if self.valid(x, s["if"]) else s.get("else")
            if then is not None:
                yield from self._walk(then, x, at)


def load(path=None):
    """The app's own schema as a Checker, read once (again only when the file changed)."""
    path = path or SCHEMA_FILE
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if key not in _cache:
            _cache.clear()
            _cache[key] = Checker(json.loads(path.read_text("utf-8")))
        return _cache[key]


def problems(doc, limit=50):
    """What is wrong with a recipe by the schema alone, as Problems (code "schema"); [] when nothing is."""
    return load().problems(doc, limit)
