"""The scrubber (3.0, STO-3; docs/v3-design/store.md §3.7): no secret value leaves the engine inside a line of text.

Before text goes out (a timeline line, a console line sent to a client, an event, an alert, a Discord post, a support
bundle, a run's log handed out), every secret value of 8 or more characters in it becomes <secret>. The values are the
store's (core/store.py): the third parties' keys and webhooks Fenrir calls, the remote console's password, the servers'
secret settings and environment, and any a caller says it handed to a process (add()). They are read once and again
after any change to the store.

Friends' keys and the phone remote's old key are not among them: those are links the host shares on purpose, shown in
the host's own window. Known formats are a second net, for a value Fenrir never kept: API 3's tokens, FiveM keys,
Discord webhooks, and ntfy topics of Fenrir's own shape. A game server's own log file is the server's; what Fenrir reads
out of it for anyone is scrubbed."""
import re
import threading

from . import store

MASK = "<secret>"
MIN = 8  # a shorter value would turn ordinary words into <secret>
SHARED = ("friend.", "remote.legacyKey", "connect.legacySecret")  # links the host shares on purpose (above)
FORMATS = [  # (what it is, its shape): after the token's own prefix where one shows what it was
    ("an API 3 token", re.compile(r"fnr3\.[wtdcmnh]\.[a-z2-7]{10}\.[A-Za-z0-9_-]{43}")),
    ("a FiveM server key", re.compile(r"cfxk_[A-Za-z0-9_-]{8,}")),
    ("a Discord webhook", re.compile(r"(https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]{20,}")),
    ("a phone alerts or address relay topic", re.compile(r"(?<![A-Za-z0-9-])fenrir-[A-Za-z0-9]{24}(?![A-Za-z0-9])")),
]
_lock = threading.Lock()
_kept = None  # [(value, label)] of the store's secrets, longest first; None until read, and again after a change
_added = {}  # value -> label: values a caller said are in use, beside the store's


def _changed(_name=None):
    global _kept
    with _lock:
        _kept = None


store.watch(_changed)


def _values():
    """[(value, label)] of every secret value in use, the longest first, so a value inside another never leaves a piece."""
    global _kept
    with _lock:
        if _kept is None:
            found = {}
            try:
                for e in store.names():
                    name = e["name"]
                    if name.startswith(SHARED):
                        continue
                    v = store.get(name)
                    if isinstance(v, str) and len(v) >= MIN:
                        found[v] = e.get("label") or name
                        if v.startswith(("http://", "https://")):  # a webhook's token is its secret, also seen alone
                            tail = v.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                            if len(tail) >= MIN and "." not in tail:
                                found[tail] = e.get("label") or name
            except Exception:  # noqa: BLE001 - a store that cannot be opened here: the known formats still catch
                found = {}
            _kept = sorted(found.items(), key=lambda kv: -len(kv[0]))
        extra = sorted(_added.items(), key=lambda kv: -len(kv[0]))
    return extra + _kept if extra else _kept


def add(value, label="a secret in use"):
    """A value in use that the store does not hold (one a process was handed, a token being called)."""
    if isinstance(value, str) and len(value) >= MIN:
        with _lock:
            _added[value] = label


def drop(value):
    with _lock:
        _added.pop(value, None)


def _mask(m):
    return (m.group(1) if m.re.groups else "") + MASK


def text(s):
    """s with every secret value in use, and every known format, made <secret>. Anything but text is given back as it is."""
    if not isinstance(s, str) or not s:
        return s
    for value, _label in _values():
        if value in s:
            s = s.replace(value, MASK)
    for _what, rx in FORMATS:
        s = rx.sub(_mask, s)
    return s


def data(obj):
    """A JSON-shaped value (an event's data, a bundle's facts) with every string in it scrubbed."""
    if isinstance(obj, str):
        return text(obj)
    if isinstance(obj, dict):
        return {k: data(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [data(v) for v in obj]
    return obj


def raw(b):
    """Bytes (a whole log handed out) scrubbed as text, every other byte as it was."""
    if not b:
        return b
    t = b.decode("utf-8", "surrogateescape")
    out = text(t)
    return b if out == t else out.encode("utf-8", "surrogateescape")


def find(s):
    """What secrets a text holds, never their values: [("kept", the store's label)] for a value Fenrir keeps, [("format",
    what it is)] for a known shape. The share check's canary (core/games/sharecheck.py)."""
    if isinstance(s, (bytes, bytearray)):
        s = bytes(s).decode("utf-8", "surrogateescape")
    if not isinstance(s, str) or not s:
        return []
    out = [("kept", label) for value, label in _values() if value in s]
    out += [("format", what) for what, rx in FORMATS if rx.search(s)]
    return list(dict.fromkeys(out))
