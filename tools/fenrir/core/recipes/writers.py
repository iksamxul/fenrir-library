"""The writers (recipes.md section 3.4): a recipe's settings put into the server's own files, one format each.

    xml-property  <property name="K" value="..."/> replaced, a missing one added before </ServerSettings>: 2.x
                  GameServer.write_config (core/games/servers.py 319-333), with the source file's own line endings
    ini           Section/key, case kept, key=value with no spaces: 2.x SkyrimTogether.write_config, but line by line, so
                  comments and the host's own lines stay (configparser dropped them)
    ini-tuple     Section/Key/Field: one field inside a key whose value is an Unreal struct, Key=(A=1,B="x"), every other
                  field kept as it was (Palworld's OptionSettings); a missing file gets the section with those fields
    properties    Java properties: only the listed keys change, values escaped by Java's own rules
    json          a JSON pointer set, the file's own indentation kept; a value keeps the type the file gave it
    keyvalue      key<sep>value lines (The Forest's "enableVAC off" with sep " ")
    lines         the whole file from templates, one per line
    hook          a built-in writer of Fenrir's own (FiveM's server.cfg), registered by its recipe's hook (REC-3)

For every writer: only the listed keys change and the rest of the file stays as the server or the host left it; the
file is written whole or not at all, with its encoding, byte-order mark and line endings kept byte for byte (bytes no
decoder knows pass through untouched); the file as it was before Fenrir's first write is kept once as
<file>.before-fenrir; and the path is resolved and checked at every write (values.real_path), so a junction made since
the recipe was checked is caught too. A file whose bytes would not change is not written, so a server watching it does
not reload for nothing."""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ..games.servers import ServerError
from . import values

ENCODINGS = {"utf-8": "utf-8", "utf-8-sig": "utf-8", "cp1252": "cp1252"}
BOM = b"\xef\xbb\xbf"
HOOKS = {}  # "fivem.server_cfg" -> fn(path, text, values, ctx) returning the file's new text; the built-in hooks register here
WRITE_ROOTS, READ_ROOTS = ("install", "data", "saves"), ("install", "data", "game")


def register(name, fn):
    """A built-in recipe's own writer (format hook): only Fenrir's code calls this, and policy keeps hooks built-in."""
    HOOKS[name] = fn


def _decode(raw, encoding):
    """(text, had a byte-order mark): bytes the encoding does not know come back as they were when written."""
    codec = ENCODINGS.get(encoding, "utf-8")
    bom = codec == "utf-8" and raw.startswith(BOM)
    return (raw[len(BOM):] if bom else raw).decode(codec, "surrogateescape"), bom


def _encode(text, encoding, bom):
    try:
        data = text.encode(ENCODINGS.get(encoding, "utf-8"), "surrogateescape")
    except UnicodeEncodeError:
        raise ServerError(f"A setting has a character the server's file cannot hold ({encoding}).") from None
    return BOM + data if bom or encoding == "utf-8-sig" else data


def newline(text):
    """The file's own line ending, or this system's for a file that has none yet."""
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    return "\r\n" if os.name == "nt" else "\n"


def _lines(text):
    """The text cut after each \\n, every piece keeping its own ending: other separators Python knows (\\x0c, \\x1c...)
    are text here, as they are to the server."""
    return [x for x in re.split(r"(?<=\n)", text) if x]


def _split(line):
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def whole(path, data):
    """Whole or not at all: a temp file beside it, moved into place (tried again while a scanner holds the file)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for attempt in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.03 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    p.write_bytes(data)


# ---------------------------------------------------------------------------------------------------- the formats
def xml_property(text, sets, nl):
    for key, v in sets.items():
        v = v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")  # as 2.x escaped them
        text, n = re.subn(rf'(<property\s+name="{re.escape(key)}"\s+value=")[^"]*(")', lambda m, v=v: m.group(1) + v + m.group(2), text)
        if not n:
            text = text.replace("</ServerSettings>", f'\t<property name="{key}" value="{v}"/>{nl}</ServerSettings>')
    return text


def ini(text, sets, nl):
    want = {}
    for k, v in sets.items():
        section, key = k.rsplit("/", 1)  # the key after the last slash: an Unreal section is a path ([/Script/Module.Class])
        want.setdefault(section, {})[key] = v
    out, section, done, last = [], None, set(), {}

    def add_missing(sec):
        """A section's keys the file lacks go after its last line that says something (configparser's blank line stays)."""
        missing = [f"{k}={v}{nl}" for k, v in want.get(sec, {}).items() if (sec, k) not in done]
        at = last.get(sec, len(out) - 1) + 1
        if missing and at > 0 and not out[at - 1].endswith("\n"):  # the file's last line, with no line break of its own
            out[at - 1] += nl
        out[at:at] = missing
        done.update((sec, k) for k in want.get(sec, {}))
    for line in _lines(text):
        body, end = _split(line)
        s = body.strip()
        head = re.fullmatch(r"\[([^\]]+)\]", s)
        if head:
            if section is not None:
                add_missing(section)
            section = head.group(1)
            out.append(line if end else line + nl)
            last[section] = len(out) - 1
            continue
        if section in want and s and not s.startswith((";", "#")) and re.search(r"[=:]", s):
            key = re.split(r"[=:]", s, maxsplit=1)[0].strip()
            if key in want[section] and (section, key) not in done:
                out.append(f"{key}={want[section][key]}{end or nl}")
                done.add((section, key))
                last[section] = len(out) - 1
                continue
        out.append(line)
        if s and section is not None:
            last[section] = len(out) - 1
    if section is not None:
        add_missing(section)
    if out and not out[-1].endswith("\n"):
        out[-1] += nl
    for sec, keys in want.items():
        if any((sec, k) not in done for k in keys):
            out.append(f"[{sec}]{nl}")
            out += [f"{k}={v}{nl}" for k, v in keys.items() if (sec, k) not in done]
            out.append(nl)  # configparser's blank line after each section, so a new file reads as 2.x wrote it
            done.update((sec, k) for k in keys)
    return "".join(out)


def _tuple_fields(inner):
    """An Unreal struct's fields, "A=1,B=\"x,y\",C=(1,2)", as [(name, value as written)]: commas inside quotes or
    brackets belong to their value."""
    out, depth, quoted, start = [], 0, False, 0
    for i, c in enumerate(inner + ","):
        if c == '"':
            quoted = not quoted
        elif not quoted and c == "(":
            depth += 1
        elif not quoted and c == ")":
            depth -= 1
        elif not quoted and depth == 0 and c == ",":
            part = inner[start:i].strip()
            if part:
                name, eq, value = part.partition("=")
                out.append((name.strip(), value.strip() if eq else None))
            start = i + 1
    return out


def _tuple_value(old, v):
    """A field's new value written the way the struct writes it: quoted when the old one was, or when it is text."""
    if '"' in v:
        raise ServerError("A setting's value has a quote mark the server's settings line cannot hold.")
    if old is not None:
        return f'"{v}"' if old.startswith('"') else v
    return v if re.fullmatch(r"-?[0-9]+(\.[0-9]+)?|True|False|None", v) else f'"{v}"'


def ini_tuple(text, sets, nl, keep=()):
    """Fields inside Unreal struct values (Section/Key/Field), line by line: the struct's other fields, the file's other
    lines and its comments stay. A field in keep is left as the file has it when it already holds a value."""
    want = {}
    for k, v in sets.items():
        rest, field = k.rsplit("/", 1)
        section, key = rest.rsplit("/", 1)
        want.setdefault((section, key), {})[field] = v
    keep = set(keep or ())
    out, section, done = [], None, set()

    def struct(old_inner, fields, sec, key):
        parts = _tuple_fields(old_inner) if old_inner is not None else []
        names = [n for n, _ in parts]
        for f, v in fields.items():
            at = names.index(f) if f in names else None
            old = parts[at][1] if at is not None else None
            if f"{sec}/{key}/{f}" in keep and old not in (None, "", '""'):
                continue
            if at is None:
                parts.append((f, _tuple_value(None, v)))
                names.append(f)
            else:
                parts[at] = (f, _tuple_value(old, v))
        return "(" + ",".join(n if v is None else f"{n}={v}" for n, v in parts) + ")"

    for line in _lines(text):
        body, end = _split(line)
        s = body.strip()
        head = re.fullmatch(r"\[([^\]]+)\]", s)
        if head:
            section = head.group(1)
            out.append(line if end else line + nl)
            continue
        key = re.split(r"=", s, maxsplit=1)[0].strip() if "=" in s else None
        if key and (section, key) in want and (section, key) not in done and not s.startswith((";", "#")):
            value = s.split("=", 1)[1].strip()
            inner = value[1:-1] if value.startswith("(") and value.endswith(")") else None
            out.append(f"{key}={struct(inner, want[(section, key)], section, key)}{end or nl}")
            done.add((section, key))
            continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += nl
    for (sec, key), fields in want.items():
        if (sec, key) in done:
            continue
        line = f"{key}={struct(None, fields, sec, key)}{nl}"
        at = next((i for i, x in enumerate(out) if x.strip() == f"[{sec}]"), None)
        if at is None:
            out += [f"[{sec}]{nl}", line]
        else:
            out.insert(at + 1, line)
        done.add((sec, key))
    return "".join(out)


def java_escape(value):
    """A properties value as java.util.Properties.store writes it: backslashes, tabs and breaks, = : # ! and a leading
    space escaped, anything outside printable ASCII as \\uXXXX (read back the same by any Java server)."""
    out = []
    for i, c in enumerate(str(value)):
        if c == "\\":
            out.append("\\\\")
        elif c in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[c])
        elif c in "=:#!" or (c == " " and i == 0):
            out.append("\\" + c)
        elif ord(c) < 0x20 or ord(c) > 0x7e:  # Java's chars are UTF-16 units: a character past U+FFFF is two of them
            b = c.encode("utf-16-be")
            out += [f"\\u{int.from_bytes(b[j:j + 2], 'big'):04X}" for j in range(0, len(b), 2)]
        else:
            out.append(c)
    return "".join(out)


def _prop_key(body):
    """A properties line's key: up to the first = : or blank that no backslash escapes."""
    s, key, i = body.lstrip(" \t\f"), [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            key.append(s[i + 1])
            i += 2
            continue
        if c in "=: \t\f":
            break
        key.append(c)
        i += 1
    return "".join(key)


def properties(text, sets, nl):
    out, done, cont, dropping = [], set(), False, False
    for line in _lines(text):
        body, end = _split(line)
        odd = (len(body) - len(body.rstrip("\\"))) % 2 == 1  # an odd run of backslashes carries the value on
        if cont:  # the rest of the value on the line before: kept, or dropped with the value Fenrir replaced
            cont = odd
            if not dropping:
                out.append(line)
            continue
        s = body.lstrip(" \t\f")
        comment = not s or s.startswith(("#", "!"))  # Java never carries a comment or a blank line on
        cont, dropping = odd and not comment, False
        if not comment:
            key = _prop_key(body)
            if key in sets and key not in done:
                out.append(f"{key}={java_escape(sets[key])}{end or nl}")
                done.add(key)
                dropping = True
                continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += nl
    out += [f"{k}={java_escape(v)}{nl}" for k, v in sets.items() if k not in done]
    return "".join(out)


def _typed(old, v):
    """A setting's text as the JSON value it replaces: true and false stay booleans, numbers stay numbers. A new key takes
    true, false or a whole number as such, anything else as text."""
    if isinstance(old, bool) or (old is None and v in ("true", "false")):
        if v.lower() not in ("true", "false", "1", "0", "on", "off"):
            raise ServerError(f"{v[:40]} is not true or false, which the server's file needs there.")
        return v.lower() in ("true", "1", "on")
    if isinstance(old, int) or (old is None and re.fullmatch(r"-?[0-9]{1,15}", v)):
        try:
            return int(v)
        except ValueError:
            raise ServerError(f"{v[:40]} is not a whole number, which the server's file needs there.") from None
    if isinstance(old, float):
        try:
            return float(v)
        except ValueError:
            raise ServerError(f"{v[:40]} is not a number, which the server's file needs there.") from None
    return v


def _indent(text):
    for line in text.splitlines()[1:]:
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        if lead:
            return "\t" if lead.startswith("\t") else len(lead)
    return None


def json_file(text, sets, nl):
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError:
        raise ServerError("The server's settings file is not JSON any more, so Fenrir left it as it is.") from None
    for pointer, v in sets.items():
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        node = doc
        for p in parts[:-1]:
            if isinstance(node, list) and p.isdigit() and int(p) < len(node):
                node = node[int(p)]
            elif isinstance(node, dict):
                node = node.setdefault(p, {})
            else:
                raise ServerError(f"The server's settings file has no place for {pointer}.")
        last = parts[-1]
        if isinstance(node, list) and last.isdigit() and int(last) < len(node):
            node[int(last)] = _typed(node[int(last)], v)
        elif isinstance(node, dict):
            node[last] = _typed(node.get(last), v)
        else:
            raise ServerError(f"The server's settings file has no place for {pointer}.")
    ind = _indent(text)
    if ind is None and "\n" in text.strip():  # one entry a line, none indented
        ind = 0
    out = json.dumps(doc, indent=ind, ensure_ascii=False)  # json never writes a raw line break inside a string
    return out.replace("\n", nl) + (nl if text.endswith(("\n", "\r")) or not text else "")


def keyvalue(text, sets, nl, sep="="):
    out, done = [], set()
    for line in _lines(text):
        body, end = _split(line)
        s = body.strip()
        if s and not s.startswith(("#", ";", "//")) and sep in body:
            key = body.split(sep, 1)[0].strip()
            if key in sets and key not in done:
                out.append(f"{key}{sep}{sets[key]}{end or nl}")
                done.add(key)
                continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += nl
    out += [f"{k}{sep}{v}{nl}" for k, v in sets.items() if k not in done]
    return "".join(out)


# ---------------------------------------------------------------------------------------------------- writing
def write(w, ctx):
    """One writer of a recipe, with its templates filled from ctx (values.context). Returns the path written, or the one
    that was already as it should be."""
    path = values.real_path(w["file"], ctx, WRITE_ROOTS)
    enc, fmt = w.get("encoding", "utf-8"), w["format"]
    src = values.real_path(w["from"], ctx, READ_ROOTS) if w.get("from") else path
    try:
        raw = src.read_bytes()
    except FileNotFoundError:
        if w.get("from"):
            raise ServerError(f"The server's own {Path(src).name} is missing; install the server again.") from None
        raw = b""
    except OSError as e:
        raise ServerError(f"Fenrir could not read {Path(src).name} ({e.strerror or e}).") from None
    text, bom = _decode(raw, enc)
    nl = newline(text)
    sets = {k: values.fill(t, ctx) for k, t in (w.get("set") or {}).items()}
    for k, v in sets.items():
        if values.ONE_LINE.search(v):
            raise ServerError(f"The value for {k} has to be one line.")
    if fmt == "xml-property":
        new = xml_property(text, sets, nl)
    elif fmt == "ini":
        new = ini(text, sets, nl)
    elif fmt == "ini-tuple":
        new = ini_tuple(text, sets, nl, w.get("keepIfSet"))
    elif fmt == "properties":
        new = properties(text, sets, nl)
    elif fmt == "json":
        new = json_file(text, sets, nl)
    elif fmt == "keyvalue":
        new = keyvalue(text, sets, nl, w.get("sep", "="))
    elif fmt == "lines":
        new = nl.join(values.fill(t, ctx) for t in w.get("lines") or []) + nl
    elif fmt == "hook" and w.get("hook") in HOOKS:
        new = HOOKS[w["hook"]](path, text, sets, ctx)
    else:
        raise ServerError("This recipe names a writer Fenrir does not have.")
    data = _encode(new, enc, bom)
    try:
        if path.read_bytes() == data:
            return path
    except OSError:
        pass
    before = path.with_name(path.name + ".before-fenrir")
    if path.exists() and not before.exists():  # the server's (or the host's) own file, kept once as it was
        shutil.copy2(path, before)
    whole(path, data)
    return path


def targets(doc):
    """setting key -> [(file template, key)] it writes: what the settings form says beside each field (P1.4)."""
    out = {}
    for w in doc.get("files") or []:
        for k, t in (w.get("set") or {}).items():
            for kind, name, _plus in values.parse(t):
                if kind == "setting":
                    out.setdefault(name, []).append((w["file"], k))
    return out
