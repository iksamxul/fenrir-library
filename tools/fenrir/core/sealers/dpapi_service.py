"""Fenrir Server's sealer on Windows (3.0, STO-2; docs/v3-design/store.md §3.3, server.md §3.2.2): the store's key sealed by
DPAPI for the service's own virtual account, NT SERVICE\\FenrirServer, so neither the host's own account nor any other
user opens it. Only an administrator, who can become any account, can.

Where DPAPI for the virtual account does not keep its keys across the service's restarts (SRV-2a's live check, LIVE_CHECK
below), the fallback is DPAPI for this PC (CRYPTPROTECT_LOCAL_MACHINE): then any program on this PC that can read the
store's file can open it, so the data folder's permissions (server.md §3.2.3: the service's account and administrators
only) are what keep it. FENRIR_STORE_DPAPI=machine asks for that; the scope is written beside each seal, so a store sealed
one way still opens after the setting changes, and the store seals again with the new one (Re-seal now)."""
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from connect import sealed

from .. import store

ACCOUNT = "NT SERVICE\\FenrirServer"
UI_FORBIDDEN, LOCAL_MACHINE = 0x1, 0x4

LIVE_CHECK = """What SRV-2a's live check on this PC must confirm before the service's sealer is trusted (once, with the owner's Yes,
and everything put back as it was found):

1. Inside the installed service, as NT SERVICE\\FenrirServer: `Fenrir.exe --service-probe seal <data>` (probe() here) seals a
   random canary with DPAPI for the account and with DPAPI for this PC, and writes dpapi-probe.json with the account and
   the profile folder it ran with (USERPROFILE, normally C:\\Windows\\ServiceProfiles\\FenrirServer).
2. `--service-probe open <data>` opens both:
   a. in the same run;
   b. after the service is stopped and started again;
   c. after a restart of Windows, with nobody signed in;
   d. after Windows Update has restarted the PC, if one is due during the check (or as the next scheduled one);
   e. as the host's own account and as SYSTEM (psexec -s): the user-scope blob must NOT open for either, the machine-scope
      one opens for both, which is why the machine scope leans on the folder's permissions.
3. The profile: whether C:\\Windows\\ServiceProfiles\\FenrirServer exists after each step, whether USERPROFILE stays the same,
   and whether Windows ever gave the account a temporary profile (Event Viewer, User Profile Service, events 1511 and 1515).
   DPAPI's master key for the account lives in that profile (AppData\\Roaming\\Microsoft\\Protect\\<the service SID>): a
   temporary profile, or one removed and made again, loses it, and every user-scope blob with it.
4. Uninstall and install again with the data kept (`--service uninstall`, then `install`): the account's SID is the same
   (it is derived from the service's name), but say whether the profile and its master key survived.

If (b) through (d) open the user-scope blob, the service uses DPAPI for its account (the default). If any of them fails,
SRV-2a sets FENRIR_STORE_DPAPI=machine for the service and checks the data folder's permissions at every start, as
server.md §3.2.3 has it. Either way the result is written into store.md §3.3."""


def whoami():
    """DOMAIN\\name of the account this process runs as (GetUserNameExW, NameSamCompatible), or "" when Windows would not say."""
    try:
        import ctypes
        size = ctypes.c_ulong(256)
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
            return buf.value
    except (AttributeError, OSError):
        pass
    return ""


class DpapiService(store.Sealer):
    kind = "dpapi-service"
    rank = 95  # inside the service it is the one to use; anywhere else it is not available
    bound = True
    absent = "It is Fenrir Server's own, and only the service can open it: start Fenrir Server as the service."

    def __init__(self, who=whoami, dpapi=None, env=None):
        self.who, self._dpapi, self.env = who, dpapi, env

    def _env(self):
        return os.environ if self.env is None else self.env

    def scope(self):
        return "machine" if self._env().get("FENRIR_STORE_DPAPI") == "machine" else "user"

    @property
    def safe_from(self):
        return ("everyone who cannot read Fenrir Server's data folder: your own account and every other one" if self.scope() == "machine"
                else "your own Windows account and every other one: only the service's account opens them")

    @property
    def not_safe_from(self):
        return ("administrators of this PC, and any program that can read Fenrir Server's data folder" if self.scope() == "machine"
                else "administrators of this PC")

    def available(self):
        return os.name == "nt" and str(self.who() or "").lower() == ACCOUNT.lower()

    def _call(self, data, entropy, protect, flags):
        if self._dpapi is not None:
            return self._dpapi(data, entropy, protect, flags)
        try:
            return sealed.dpapi(data, entropy, protect, flags)
        except sealed.SealedError as e:
            raise store.SealError(f"Windows could not {'seal' if protect else 'open'} the store's key for Fenrir Server: {e}") from None

    def seal(self, key, entropy):
        scope = self.scope()
        blob = self._call(key, entropy, True, UI_FORBIDDEN | (LOCAL_MACHINE if scope == "machine" else 0))
        return {"blob": base64.b64encode(blob).decode("ascii"), "scope": scope}

    def unseal(self, rec, entropy):
        if not self.available():
            raise store.SealerAbsent(self.absent)
        try:
            blob = base64.b64decode(rec["blob"])
        except (KeyError, TypeError, ValueError):
            raise store.SealError("The store's key is damaged.") from None
        return self._call(blob, entropy, False, UI_FORBIDDEN | (LOCAL_MACHINE if rec.get("scope") == "machine" else 0))


SEALER = DpapiService()
store.register(SEALER)


def probe(folder, step, who=whoami, dpapi=None):
    """SRV-2a's live check (LIVE_CHECK): step "seal" seals a canary both ways and writes dpapi-probe.json into folder; step
    "open" opens both and says what it found, with the account and the profile then and now. Never holds a secret of
    Fenrir's: the canary is made for the check."""
    path = Path(folder) / "dpapi-probe.json"
    call = dpapi or sealed.dpapi
    if step == "seal":
        canary, entropy = secrets.token_bytes(32), secrets.token_bytes(16)
        out = {"t": time.time(), "account": who(), "profile": os.environ.get("USERPROFILE", ""), "entropy": base64.b64encode(entropy).decode(),
               "canary": hashlib.sha256(canary).hexdigest()}
        for scope, flags in (("user", UI_FORBIDDEN), ("machine", UI_FORBIDDEN | LOCAL_MACHINE)):
            try:
                out[scope] = base64.b64encode(call(canary, entropy, True, flags)).decode()
            except Exception as e:  # noqa: BLE001 - what failed is the finding
                out[scope] = None
                out[f"{scope}Error"] = str(e)[:200]
        path.write_text(json.dumps(out, indent=1), "utf-8")
        return out
    d = json.loads(path.read_text("utf-8"))
    entropy = base64.b64decode(d["entropy"])
    found = {"account": who(), "accountThen": d.get("account"), "profile": os.environ.get("USERPROFILE", ""), "profileThen": d.get("profile"),
             "profileExists": bool(d.get("profile")) and os.path.isdir(d.get("profile") or "")}
    for scope, flags in (("user", UI_FORBIDDEN), ("machine", UI_FORBIDDEN | LOCAL_MACHINE)):
        try:
            got = call(base64.b64decode(d[scope]), entropy, False, flags) if d.get(scope) else None
            found[scope] = got is not None and hashlib.sha256(got).hexdigest() == d["canary"]
        except Exception as e:  # noqa: BLE001
            found[scope] = False
            found[f"{scope}Error"] = str(e)[:200]
    return found
