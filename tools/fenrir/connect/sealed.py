"""Keeping a secret sealed by what the system offers (3.0, STO-2; docs/v3-design/store.md §3.3, platforms.md §3.5): Windows'
DPAPI for this user, the Secret Service through secret-tool on Linux desktops (GNOME Keyring, KWallet), the macOS Keychain
through security, and, only where none of those is there, a key file that this user alone may read, said plainly.

Fenrir's sealed store (core/sealers) uses these to seal its data key, and Fenrir Connect uses Keeper for its friend's key.
It lives in Fenrir Connect's package for the reason connect/aead.py does: both apps need it, and Connect has nothing of
Fenrir's engine. Every program it runs gets an argument list, never a shell, and a secret goes to it on standard input,
never on its command line, where other programs could read it in the process list. Standard library only."""
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from . import aead

HIDDEN = 0x08000000  # CREATE_NO_WINDOW


class SealedError(Exception):
    """A secret could not be sealed or opened here. Said as it is; it never holds the secret."""


class KeeperUnavailable(SealedError):
    """The system's keeping did not answer this time: a keyring that is locked or not in this session (a Steam Deck in Game
    Mode), a keychain that would not open, a key file that could not be read, a program that took too long. Nothing is
    known about the secret itself: it may well be there, so it is asked again later, never taken for gone."""


# ---------------------------------------------------------------------------------------------------- running programs
def run(argv, stdin=None, timeout=20):
    """(exit code, what it printed) of a program, or (None, "") when it is not there or takes too long."""
    try:
        r = subprocess.run([str(a) for a in argv], input=stdin, capture_output=True, timeout=timeout,
                           **({"creationflags": HIDDEN} if os.name == "nt" else {}))
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return r.returncode, r.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------------------------------- Windows: DPAPI
def dpapi(data, entropy, protect, flags=0x1):
    """Windows' own encryption for the account this process runs as (CryptProtectData), with an entropy the caller keeps,
    so a program that calls DPAPI as the same account without that entropy cannot open the blob. 0x1 is
    CRYPTPROTECT_UI_FORBIDDEN (never a dialog); 0x4, CRYPTPROTECT_LOCAL_MACHINE, seals for every account of this PC."""
    import ctypes
    import ctypes.wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf  # the buffer is kept alive with it

    src, _a = blob(data)
    ent, _b = blob(entropy)
    out = Blob()
    try:
        crypt = ctypes.windll.crypt32
    except AttributeError:
        raise SealedError("DPAPI is Windows' own.") from None
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out)):
        raise SealedError(f"Windows could not {'seal' if protect else 'open'} it (error {ctypes.GetLastError()}).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------------------------------------------- Linux: the Secret Service
def secret_tool_ready(env=None):
    """secret-tool is installed and a desktop session's D-Bus is there to reach the keyring (SteamOS has no secret-tool)."""
    env = os.environ if env is None else env
    return sys.platform.startswith("linux") and bool(shutil.which("secret-tool")) and bool(env.get("DBUS_SESSION_BUS_ADDRESS"))


def _attrs(attrs):
    out = []
    for k, v in attrs.items():
        out += [str(k), str(v)]
    return out


def secret_store(attrs, value, label, runner=run):
    """value (bytes) into the keyring under these attributes, replacing what was there."""
    code, _ = runner(["secret-tool", "store", f"--label={label}", *_attrs(attrs)], stdin=base64.b64encode(value))
    if code != 0:
        raise SealedError("The keyring did not take it (is it locked?).")


def secret_lookup(attrs, runner=run):
    code, out = runner(["secret-tool", "lookup", *_attrs(attrs)])
    if code != 0 or not out.strip():
        return None
    try:
        return base64.b64decode(out.strip(), validate=True)
    except ValueError:
        return None


def secret_clear(attrs, runner=run):
    runner(["secret-tool", "clear", *_attrs(attrs)])


# ---------------------------------------------------------------------------------------------------- macOS: the Keychain
def keychain_ready():
    return sys.platform == "darwin" and os.path.exists("/usr/bin/security")


def keychain_store(service, account, value, runner=run):
    """value (bytes) as a generic password, written through security's own prompt on standard input (security -i), so the
    secret is never on a command line."""
    for s in (service, account):
        if not s or any(c in str(s) for c in "\"\\\n\r"):
            raise SealedError("A keychain name must be plain text.")
    line = f'add-generic-password -U -s "{service}" -a "{account}" -w "{base64.b64encode(value).decode("ascii")}"\n'
    code, _ = runner(["/usr/bin/security", "-i"], stdin=line.encode("utf-8"))
    if code != 0:
        raise SealedError("The keychain did not take it.")


def keychain_find(service, account, runner=run):
    code, out = runner(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"])
    if code != 0 or not out.strip():
        return None
    try:
        return base64.b64decode(out.strip(), validate=True)
    except ValueError:
        return None


def keychain_delete(service, account, runner=run):
    runner(["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account])


# ---------------------------------------------------------------------------------------------------- a key file
def key_file(path, create=True):
    """The 32-byte key in path, made the first time with a mode only this user may read (0600). A file that others can read
    too is closed to them again. None when there is none and create is False."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:  # another start made it at the same moment: that one is the key
            return key_file(path, create=False)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    except OSError as e:  # it is there but could not be read now: never the same as no key at all
        raise KeeperUnavailable(f"The key file could not be read ({e.strerror or e}).") from None
    key = _key_bytes(data)
    if key is None:
        raise SealedError("The key file does not hold a key.")
    if os.name != "nt":
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
    return key


def _key_bytes(data):
    """32 bytes as the file holds them: raw, or as 64 hex digits or base64 text (a Docker secret is usually text)."""
    if len(data) == 32:
        return bytes(data)
    text = data.strip()
    try:
        if len(text) == 64:
            return bytes.fromhex(text.decode("ascii"))
        raw = base64.b64decode(text, validate=True)
        return raw if len(raw) == 32 else None
    except ValueError:
        return None


def wrap(key, value, aad=b""):
    """value sealed with a 32-byte key (ChaCha20-Poly1305), as {"n", "c"} in base64."""
    n = secrets.token_bytes(12)
    return {"n": base64.b64encode(n).decode("ascii"), "c": base64.b64encode(aead.encrypt(key, n, value, aad)).decode("ascii")}


def unwrap(key, rec, aad=b""):
    try:
        return aead.decrypt(key, base64.b64decode(rec["n"]), base64.b64decode(rec["c"]), aad)
    except (aead.AuthError, KeyError, TypeError, ValueError):
        raise SealedError("It does not open with this key.") from None


# ---------------------------------------------------------------------------------------------------- Fenrir Connect's own
class Keeper:
    """One app's few secrets (Fenrir Connect's key to its host), each sealed by the best this system offers: DPAPI on
    Windows, the Keychain on macOS, the Secret Service on a Linux desktop, else a key file (said by how(), plainly).
    Files live in folder: <name>.sealed, and key for the key file."""

    def __init__(self, folder, app="Fenrir Connect", runner=run, kind=None):
        self.folder, self.app, self.runner = Path(folder), app, runner
        self.kind = kind or ("dpapi" if os.name == "nt" else "keychain" if keychain_ready() else "secret-service" if secret_tool_ready() else "keyfile")

    def how(self):
        """What keeps the secrets, in plain words, for the app to show."""
        return {"dpapi": "Windows keeps it for your account.", "keychain": "Your Mac's keychain keeps it.",
                "secret-service": "Your desktop's keyring keeps it.",
                "keyfile": ("It is locked with a key kept in a file only your account can read. Anyone who can read your "
                            "files, or a copy of them, can read it too.")}[self.kind]

    def _file(self, name):
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise SealedError("A secret's name is letters, digits and - _ . only.")
        return self.folder / f"{name}.sealed"

    def keep(self, name, value):
        """Seal value under name; it is read back before the file names it, so a keyring that only seemed to take it never
        replaces the copy that works. Returns what keeps it."""
        value = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        path, entropy = self._file(name), secrets.token_bytes(16)
        rec = {"kind": self.kind, "entropy": base64.b64encode(entropy).decode("ascii")}
        if self.kind == "dpapi":
            rec["blob"] = base64.b64encode(dpapi(value, entropy, True)).decode("ascii")
        elif self.kind == "keychain":
            keychain_store(self.app, f"{name}-{entropy.hex()}", value, self.runner)
        elif self.kind == "secret-service":
            secret_store({"application": self.app, "secret": f"{name}-{entropy.hex()}"}, value, f"{self.app}: {name}", self.runner)
        else:
            rec.update(wrap(key_file(self.folder / "key"), value, entropy))
        if self._open(name, rec) != value:
            raise SealedError("It was sealed but did not read back, so the copy from before stays.")
        old = self._read(path)
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rec), "utf-8")
        os.replace(tmp, path)
        if old:
            self._drop(name, old)  # the item kept for the value before, in the keyring or the keychain
        return self.kind

    def take(self, name):
        """The value, or None when none is kept or this account cannot open it (DPAPI of another Windows account, a key file
        that was replaced). Raises KeeperUnavailable when the keeping did not answer this time (a locked or missing keyring,
        a keychain that would not open, a file that could not be read): the value may well be there, so a caller must
        never treat that as no value at all."""
        path = self._file(name)
        try:
            text = path.read_text("utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise KeeperUnavailable(f"The sealed file could not be read ({e.strerror or e}).") from None
        try:
            rec = json.loads(text)
        except ValueError:
            return None
        return self._open(name, rec, strict=True) if isinstance(rec, dict) else None

    def _open(self, name, rec, strict=False):
        """The value a record opens to. strict (take): a keyring or keychain that holds nothing for a record that says it
        does, or a key file that could not be read, did not answer this time: KeeperUnavailable, never None."""
        try:
            entropy = base64.b64decode(rec["entropy"])
            kind = rec.get("kind")
            if kind == "dpapi":
                return dpapi(base64.b64decode(rec["blob"]), entropy, False)
            if kind in ("keychain", "secret-service"):
                got = (keychain_find(self.app, f"{name}-{entropy.hex()}", self.runner) if kind == "keychain" else
                       secret_lookup({"application": self.app, "secret": f"{name}-{entropy.hex()}"}, self.runner))
                if got is None and strict:
                    raise KeeperUnavailable("The keyring did not give it back this time (is it locked, or not in this session?).")
                return got
            if kind == "keyfile":
                key = key_file(self.folder / "key", create=False)
                return unwrap(key, rec, entropy) if key else None
        except KeeperUnavailable:
            if strict:
                raise
            return None
        except (SealedError, KeyError, ValueError, TypeError):
            return None
        return None

    def forget(self, name):
        path = self._file(name)
        rec = self._read(path)
        if rec:
            self._drop(name, rec)
        path.unlink(missing_ok=True)

    def _drop(self, name, rec):
        try:
            entropy = base64.b64decode(rec["entropy"]).hex()
        except (KeyError, ValueError, TypeError):
            return
        if rec.get("kind") == "keychain":
            keychain_delete(self.app, f"{name}-{entropy}", self.runner)
        elif rec.get("kind") == "secret-service":
            secret_clear({"application": self.app, "secret": f"{name}-{entropy}"}, self.runner)

    @staticmethod
    def _read(path):
        try:
            d = json.loads(Path(path).read_text("utf-8"))
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None
