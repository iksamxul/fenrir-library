"""Fenrir Server's sealer on a Linux node (3.0, STO-2; docs/v3-design/store.md §3.3, server.md §3.3): systemd-creds.

At install, the package makes a random 32-byte key and has systemd seal it (make_credential: systemd-creds encrypt
--with-key=auto, with the machine's TPM2 where there is one, else the host key in /var/lib/systemd/credential.secret) into
/etc/fenrir/store-key.cred. The unit loads it with LoadCredentialEncrypted=store-key:..., and systemd puts the opened key
into $CREDENTIALS_DIRECTORY, a tmpfs only the service reads. That key wraps the store's data key (ChaCha20-Poly1305).
The engine itself never needs root: it only reads what systemd opened for it.

Started by hand rather than by its unit, the engine has no $CREDENTIALS_DIRECTORY: the store is then out of reach, and
is left as it is (store.SealerAbsent), never taken for one from elsewhere."""
import os
import secrets
import sys
from pathlib import Path

from connect import sealed

from .. import store

NAME = "store-key"
CRED = "/etc/fenrir/store-key.cred"
AAD = b"fenrir-store-key\0"


class SystemdCreds(store.Sealer):
    kind = "systemd-creds"
    rank = 90
    bound = True
    absent = "Start Fenrir Server through systemd (systemctl start fenrir-server): only its unit opens the key."
    safe_from = "other users of this machine, and anyone who reads its disk elsewhere (with a TPM, even the whole disk)"
    not_safe_from = "root on this machine"

    def __init__(self, env=None, platform=None):
        self.env, self.platform = env, platform

    def _key(self):
        env = os.environ if self.env is None else self.env
        folder = env.get("CREDENTIALS_DIRECTORY")
        if not folder:
            return None
        try:
            return sealed.key_file(Path(folder) / NAME, create=False)
        except sealed.SealedError:
            return None

    def available(self):
        return (self.platform or sys.platform).startswith("linux") and self._key() is not None

    def seal(self, key, entropy):
        wrapping = self._key()
        if wrapping is None:
            raise store.SealerAbsent(self.absent)
        return sealed.wrap(wrapping, key, AAD + entropy)

    def unseal(self, rec, entropy):
        wrapping = self._key()
        if wrapping is None:
            raise store.SealerAbsent(self.absent)
        try:
            return sealed.unwrap(wrapping, rec, AAD + entropy)
        except sealed.SealedError:
            raise store.SealError("The store's key does not open with this machine's systemd credential.") from None


store.register(SystemdCreds())


def make_credential(path=CRED, runner=sealed.run):
    """The package's install step (as root): a new random key, sealed by systemd-creds into path for the unit to load. The
    key goes to systemd-creds on its standard input, never on its command line or onto a disk unsealed. One already there
    is kept: a new key would leave the store sealed by the old one, out of reach."""
    if Path(path).exists():
        return str(path)
    code, _ = runner(["systemd-creds", "encrypt", "--with-key=auto", f"--name={NAME}", "-", str(path)], stdin=secrets.token_bytes(32))
    if code != 0:
        raise store.SealError("systemd-creds could not seal the store's key (systemd 250 or later is needed).")
    return str(path)
