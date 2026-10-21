"""The sealed store (3.0, docs/v3-design/store.md): every secret Fenrir keeps, in one file, DATA/store/store.json.

Only a 32-byte data key is sealed by the operating system, through a sealer: DPAPI for this Windows user here. The
others (the service's DPAPI, systemd-creds, the desktop keyrings, a key file) plug in through register(), and
FENRIR_STORE_SEALER names the one a process must use (the Windows service will). Changing sealer re-seals that one key,
never the entries. Each entry is encrypted with the data key by ChaCha20-Poly1305 (connect/aead.py) and bound to the
store, its name, its scope and its version, so an entry copied under another name, or from another store, does not open.
One previous version of each entry stays 30 days, so a change can be undone.

Values never leave through here: names() lists what is kept without a value. get() is for the engine modules listed in
tests/test_store_imports.py; no route and no page ever calls it.

    {"fenrirStore": 1, "id": "s_...", "dk": 1, "sealer": {"kind": "dpapi-user", "blob": b64, "entropy": b64},
     "entries": {name: {"kind", "scope", "label", "v", "dk", "n", "c", "created", "changed", "usedBy",
                        "previous": {"v", "dk", "n", "c", "until"}}}}"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time

from connect import aead

from .util import DATA, write_text_whole

PATH = DATA / "store" / "store.json"
KEEP_PREVIOUS = 30 * 86400  # how long a changed secret's previous version stays, for Undo (store.md §7 question 1)
KINDS = ("api-key", "password", "webhook", "config", "capability", "key", "env")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}")  # never a line break: the associated data is lines
FORMAT = 1


class SealError(Exception):
    """The store cannot be used here: no sealer, or the data key does not open. Said as it is; never holds a value."""


class SealerAbsent(SealError):
    """The sealer that sealed this store is this machine's, but this process cannot reach it: Fenrir Server started by hand
    and not by its service or unit, a keyring locked or out of reach. Nothing is set aside, and the next start tries again."""


# ---------------------------------------------------------------------------------------------------------- the sealers
class Sealer:
    """What seals the data key. kind names it in the file; rank orders the choice when no sealer is asked for (higher is
    stronger); safe_from and not_safe_from say in plain words what it keeps the secrets from (Settings, Security)."""
    kind = ""
    rank = 0
    safe_from = ""
    not_safe_from = ""
    bound = False  # reachable only from one kind of process on this machine (the service, a desktop session): when it is
    # out of reach, a store it sealed is not taken for one from elsewhere

    def available(self):
        """True only where this sealer is right for this process (the service's sealer, only inside the service)."""
        return False

    def seal(self, key, entropy):
        """What the file keeps to open the key again, as a dict of text; kind and entropy are added by the store."""
        raise NotImplementedError

    def unseal(self, rec, entropy):
        """The data key from what seal() returned, or SealError."""
        raise NotImplementedError


def _dpapi(data, entropy, protect, flags=0x1):
    """Windows' own encryption for the account this process runs as (CryptProtectData), with the store's entropy, so a
    program that calls DPAPI as the host without the store file in hand cannot open the blob. 0x1 is
    CRYPTPROTECT_UI_FORBIDDEN: never a dialog. The call itself is connect/sealed.py's, which Fenrir Connect shares."""
    from connect import sealed
    try:
        return sealed.dpapi(data, entropy, protect, flags)
    except sealed.SealedError as e:
        raise SealError(f"Windows could not {'seal' if protect else 'open'} the store's key: {e}") from None


class DpapiUser(Sealer):
    kind = "dpapi-user"
    rank = 50
    safe_from = "other Windows accounts on this PC, and anyone who copies Fenrir's folder to another PC"
    not_safe_from = "programs running as you, as before Fenrir 3"

    def available(self):
        return os.name == "nt"

    def seal(self, key, entropy):
        return {"blob": _b64(_dpapi(key, entropy, True))}

    def unseal(self, rec, entropy):
        try:
            return _dpapi(_unb64(rec["blob"]), entropy, False)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise SealError("The store's key is damaged.") from None


SEALERS = {}


def register(sealer):
    """A sealer for platforms this file does not cover (STO-2: systemd-creds, the keyrings, a key file; SRV-2a: the
    Windows service's DPAPI). The store picks among the registered ones."""
    SEALERS[sealer.kind] = sealer


register(DpapiUser())


def _choose():
    want = os.environ.get("FENRIR_STORE_SEALER")
    if want:
        s = SEALERS.get(want)
        if not s or not s.available():
            raise SealError(f"The sealer {want} was asked for and is not available here.")
        return s
    for s in sorted(SEALERS.values(), key=lambda s: -s.rank):
        if s.available():
            return s
    raise SealError("Nothing on this machine can seal secrets.")


# ------------------------------------------------------------------------------------------------------- the file itself
_lock = threading.RLock()
_doc = None  # the file as last read or written
_dk = None  # the data key, opened once a run: every entry needs it
_text = None  # the file's text as last read or written: what the next write keeps as .bak
_pepper = None
_status = {}
_watchers = []


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(str(s).encode("ascii"), validate=True)


def _bak():
    return PATH.with_name(PATH.name + ".bak")


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _read(path):
    """The store document at path, or None when it is missing or is not a store this Fenrir can read."""
    try:
        text = path.read_text("utf-8")
        doc = json.loads(text)
    except (OSError, ValueError):
        return None, None
    ok = (isinstance(doc, dict) and doc.get("fenrirStore") == FORMAT and isinstance(doc.get("id"), str)
          and isinstance(doc.get("sealer"), dict) and isinstance(doc.get("entries"), dict) and isinstance(doc.get("dk"), int))
    return (doc, text) if ok else (None, None)


def _unseal(doc):
    rec = doc["sealer"]
    s = SEALERS.get(rec.get("kind"))
    if s and s.bound and not s.available():
        raise SealerAbsent(f"This store was sealed with {s.kind}, which this process cannot reach. {getattr(s, 'absent', '')}".strip())
    if not s or not s.available():
        raise SealError(f"This store was sealed with {rec.get('kind') or 'an unknown sealer'}, which is not here.")
    try:
        entropy = _unb64(rec.get("entropy") or "")
    except (ValueError, binascii.Error):
        raise SealError("The store's key is damaged.") from None
    key = s.unseal(rec, entropy)
    if len(key) != 32:
        raise SealError("The store's key is damaged.")
    return key


def _sealer_rec(sealer, key):
    entropy = secrets.token_bytes(16)
    rec = {**sealer.seal(key, entropy), "kind": sealer.kind, "entropy": _b64(entropy)}
    if sealer.unseal(rec, entropy) != key:  # a sealer that seals what it cannot open would lose everything later
        raise SealError(f"The sealer {sealer.kind} did not give the key back.")
    return rec


def _keep_aside(path, why):
    """A store that cannot be used here is never overwritten: kept beside the new one, and said so."""
    if not path.exists():
        return None
    stamp, tail = _stamp(), ".bak" if path.name.endswith(".bak") else ""
    aside = next(q for q in (path.with_name(f"store.json.{why}-{stamp}{sfx}{tail}") for sfx in ("", "-2", "-3", "-4", "-5", "-6")) if not q.exists())
    try:
        shutil.move(str(path), str(aside))
    except OSError:
        raise SealError(f"The store could not be kept aside as {aside.name}; nothing was changed.") from None
    _status.setdefault("keptAside", []).append({"file": aside.name, "why": why, "t": time.time()})
    return aside.name


def _write(doc):
    """Whole or not at all (core/util.py), with the file as it was before kept as store.json.bak."""
    global _text
    text = json.dumps(doc, indent=1)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    if _text is not None:
        write_text_whole(_bak(), _text)
    write_text_whole(PATH, text)
    _text = text


def _open():
    """The store, read (or made) once a run, with its data key opened."""
    global _doc, _dk, _text
    if _doc is not None and _dk is not None:
        return _doc
    doc, text = _read(PATH)
    if doc is None:
        bdoc, btext = _read(_bak())
        if bdoc is not None:  # the last write was damaged or lost: the copy before it takes over
            if PATH.exists():
                _keep_aside(PATH, "broken")
            doc, text = bdoc, btext
            _status["recovered"] = time.time()
        elif PATH.exists():
            _keep_aside(PATH, "broken")
    dk = None
    if doc is not None:
        try:
            dk = _unseal(doc)
        except SealerAbsent:
            raise  # this machine's own sealer, out of this process's reach: the store stays as it is, for the next start
        except SealError as e:
            try:
                time.sleep(0.2)  # once more before deciding: a busy profile service has failed DPAPI for a moment
                dk = _unseal(doc)
            except SealerAbsent:
                raise
            except SealError:
                _status["foreign"] = str(e)
                _keep_aside(PATH, "foreign")
                _keep_aside(_bak(), "foreign")
                doc, text = None, None
    if doc is None:
        sealer = _choose()
        dk = secrets.token_bytes(32)
        doc = {"fenrirStore": FORMAT, "id": "s_" + secrets.token_hex(5), "dk": 1, "sealer": _sealer_rec(sealer, dk), "entries": {}}
        if _bak().exists():
            _keep_aside(_bak(), "old")  # the copy of a store that is no longer this one is never taken for it
        _text = None
        _doc, _dk = doc, dk
        _write(doc)
    else:
        _doc, _dk, _text = doc, dk, text
        if _status.get("recovered"):
            _write(doc)
        _resealed_if_asked()
    _sweep()
    return _doc


def _resealed_if_asked():
    """A store opened with one sealer while another is the one for this process (the service asks for its own, a better
    one arrived): the data key is sealed again with it, and the entries stay as they are."""
    try:
        want = _choose()
    except SealError:
        return
    if _doc["sealer"].get("kind") != want.kind and (os.environ.get("FENRIR_STORE_SEALER") or want.rank > SEALERS[_doc["sealer"]["kind"]].rank):
        _doc["sealer"] = _sealer_rec(want, _dk)
        _write(_doc)


def _sweep(now=None):
    """Previous versions older than their 30 days are wiped."""
    now = now or time.time()
    gone = False
    for e in _doc["entries"].values():
        p = e.get("previous")
        if isinstance(p, dict) and float(p.get("until") or 0) <= now:
            e.pop("previous", None)
            gone = True
    if gone:
        _write(_doc)


def scope_of(name):
    """The scope a name says: server.<id>.* is server:<id>, project.<id>.* is project:<id>, the rest is app."""
    parts = str(name).split(".")
    return f"{parts[0]}:{parts[1]}" if parts[0] in ("server", "project") and len(parts) > 2 else "app"


def _ad(doc, name, v):
    return f"{doc['id']}\n{name}\n{scope_of(name)}\n{int(v)}".encode("utf-8")


def _sealed(doc, dk, name, v, value):
    n = secrets.token_bytes(12)  # a random nonce for every write: never twice under one key
    return {"v": v, "dk": doc["dk"], "n": _b64(n), "c": _b64(aead.encrypt(dk, n, str(value).encode("utf-8"), _ad(doc, name, v)))}


def _opened(doc, dk, name, rec):
    """An entry's (or a previous version's) value, or None when it does not open here: another data key, a swapped or
    damaged entry."""
    try:
        if rec.get("dk") != doc["dk"]:
            return None
        return aead.decrypt(dk, _unb64(rec["n"]), _unb64(rec["c"]), _ad(doc, name, rec["v"])).decode("utf-8")
    except (aead.AuthError, KeyError, TypeError, ValueError, AttributeError, binascii.Error, UnicodeDecodeError):
        return None


def _check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("That is not a name the store keeps.")


def _changed(name):
    for fn in list(_watchers):
        try:
            fn(name)
        except Exception:  # noqa: BLE001 - a watcher never stands in the way of the write it hears about
            pass


# ------------------------------------------------------------------------------------------------------------ the store
def put(name, value, kind=None, label=None, used_by=None):
    """Keep value under name, as a new version (the one before stays 30 days). The same value again writes nothing.
    Returns the version."""
    with _lock:
        v, wrote = _put(name, value, kind, label, used_by)
    if wrote:
        _changed(name)  # outside the store's lock: a watcher may take the settings' lock, which can be waiting on this one
    return v


def _put(name, value, kind=None, label=None, used_by=None):
    """put() under the store's lock, without telling the watchers. Returns the version and whether anything was written."""
    global _pepper
    _check_name(name)
    value = str(value)
    if kind is not None and kind not in KINDS:
        raise ValueError(f"The store keeps no kind called {kind}.")
    doc = _open()
    e = doc["entries"].get(name)
    now = int(time.time())
    if e is not None:
        same = _opened(doc, _dk, name, e) == value
        meta_same = (kind is None or kind == e.get("kind")) and (label is None or label == e.get("label")) and (used_by is None or list(used_by) == e.get("usedBy"))
        if same and meta_same:
            return int(e["v"]), False
        v = int(e.get("v") or 0) + (0 if same else 1)  # a new label or user is no new version of the secret
        rec = dict(e)
        if not same:
            rec.update(_sealed(doc, _dk, name, v, value), changed=now)
            rec["previous"] = {k: e[k] for k in ("v", "dk", "n", "c") if k in e} | {"until": now + KEEP_PREVIOUS}
    else:
        v = 1
        rec = {"kind": kind or "password", "scope": scope_of(name), "label": label or name, **_sealed(doc, _dk, name, v, value),
               "created": now, "changed": now, "usedBy": []}
    if kind is not None:
        rec["kind"] = kind
    if label is not None:
        rec["label"] = label
    if used_by is not None:
        rec["usedBy"] = [str(u) for u in used_by]
    doc["entries"][name] = rec
    _write(doc)
    if name == "api.pepper":
        _pepper = None  # before any watcher hears of it: every hash made from now on uses the new one
    return v, True


def get(name):
    """The value kept under name, or None when there is none or it cannot be opened here. Engine modules only (the
    list in tests/test_store_imports.py): no route returns a value, ever."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    with _lock:
        doc = _open()
        e = doc["entries"].get(name)
        return None if e is None else _opened(doc, _dk, name, e)


def previous(name):
    """The value before the last change, while its 30 days last (Undo), else None."""
    with _lock:
        doc = _open()
        p = (doc["entries"].get(name) or {}).get("previous")
        if not isinstance(p, dict) or float(p.get("until") or 0) <= time.time():
            return None
        return _opened(doc, _dk, name, p)


def has(name):
    with _lock:
        return name in _open()["entries"]


def delete(name):
    """Remove an entry and its previous version at once. Returns whether there was one."""
    global _pepper
    with _lock:
        doc = _open()
        if doc["entries"].pop(name, None) is None:
            return False
        _write(doc)
        if name == "api.pepper":
            _pepper = None
    _changed(name)
    return True


def rotate(name, value=None):
    """A new version of a secret: the value given (a third party's new token), or a new random one for the keys Fenrir
    makes itself. The old one stays 30 days, as for any change. A new api.pepper is heard by the settings, which make
    every friend's hash again (store.md §3.8). Returns the version."""
    return put(name, value if value is not None else secrets.token_hex(32))


def names(prefix=""):
    """What the store keeps, for Settings and the routes: never a value."""
    with _lock:
        doc = _open()
        out = []
        for n, e in sorted(doc["entries"].items()):
            if n.startswith(prefix):
                out.append({"name": n, "kind": e.get("kind"), "scope": e.get("scope") or scope_of(n), "label": e.get("label") or n,
                            "v": e.get("v"), "created": e.get("created"), "changed": e.get("changed"), "usedBy": list(e.get("usedBy") or []),
                            "previous": isinstance(e.get("previous"), dict)})
        return out


def pepper():
    """The API pepper (api.pepper): 32 random bytes made the first time anything hashes a token or a friend key, kept in
    memory while Fenrir runs since every request needs it (store.md §3.2)."""
    global _pepper
    with _lock:
        if _pepper is None:
            v = get("api.pepper")
            if not v or not re.fullmatch(r"[0-9a-f]{64}", v):
                v = secrets.token_hex(32)  # made, not changed: no watcher hears of it, since no hash was made before it
                _put("api.pepper", v, kind="key", label="The key that hashes devices' tokens and friends' keys")
            _pepper = bytes.fromhex(v)
        return _pepper


def keyed_hash(text):
    """hs1: and HMAC-SHA256 of text under the pepper, in hex (api3.md §3.4): what is kept to look a token or a friend key
    up, without keeping the key."""
    return "hs1:" + hmac.new(pepper(), str(text).encode("utf-8"), hashlib.sha256).hexdigest()


def reseal(kind=None, new_key=False):
    """Seal the data key again, with the sealer named or the one this process would choose (Settings, Security: Re-seal
    now). new_key also makes a new data key and encrypts every entry again under it."""
    global _doc, _dk
    with _lock:
        doc = _open()
        sealer = SEALERS.get(kind) if kind else _choose()
        if not sealer or not sealer.available():
            raise SealError(f"The sealer {kind} is not available here.")
        dk = _dk
        if new_key:
            dk = secrets.token_bytes(32)
            fresh = dict(doc, dk=doc["dk"] + 1)
            entries = {}
            for n, e in doc["entries"].items():
                value = _opened(doc, _dk, n, e)
                if value is None:
                    entries[n] = e  # one that never opened here stays as it was, rather than being lost
                    continue
                rec = {**e, **_sealed(fresh, dk, n, e["v"], value)}
                p = e.get("previous")
                old = _opened(doc, _dk, n, p) if isinstance(p, dict) else None
                rec.pop("previous", None)
                if old is not None:
                    rec["previous"] = {**_sealed(fresh, dk, n, p["v"], old), "until": p["until"]}
                entries[n] = rec
            doc = dict(fresh, entries=entries)
        doc["sealer"] = _sealer_rec(sealer, dk)
        _write(doc)
        _doc, _dk = doc, dk
        return {"ok": True, "sealer": sealer.kind}


def verify(values):
    """The names whose values do not read back from the file on disk, opened afresh: what the migration checks before
    a secret leaves config.json, so nothing is lost on the way."""
    doc, _ = _read(PATH)
    if doc is None:
        return sorted(values)
    try:
        dk = _unseal(doc)
    except SealError:
        return sorted(values)
    return sorted(n for n, v in values.items() if _opened(doc, dk, n, doc["entries"].get(n) or {}) != v)


def watch(fn):
    """fn(name) after an entry is written, rotated or removed: the settings keep their copies of the store-backed
    settings current when something else changes one."""
    _watchers.append(fn)


def available():
    """Whether secrets can be kept sealed here. When not, the settings keep them as 2.x did, and say why (status)."""
    try:
        with _lock:
            _open()
        return True
    except (SealError, OSError) as e:
        _status["error"] = str(e)
        return False


def status():
    """What Settings, Security and the Doctor say: which sealer, what it keeps the secrets from, and anything kept aside."""
    with _lock:
        ok = available()
        s = SEALERS.get((_doc or {}).get("sealer", {}).get("kind")) if ok else None
        return {"ok": ok, "sealer": s.kind if s else None, "safeFrom": s.safe_from if s else "", "notSafeFrom": s.not_safe_from if s else "",
                "entries": len(_doc["entries"]) if ok else 0, "keptAside": list(_status.get("keptAside") or []),
                "recovered": _status.get("recovered"), "foreign": _status.get("foreign"), "error": None if ok else _status.get("error")}


def reload():
    """Forget what is in memory, so the next call reads the file again (tests, and a data folder put back)."""
    global _doc, _dk, _text, _pepper
    with _lock:
        _doc = _dk = _text = _pepper = None
        _status.clear()


from . import sealers as _sealers  # noqa: E402,F401 - the other sealers register themselves (STO-2): the service's, systemd-creds, the keyrings, a key file
