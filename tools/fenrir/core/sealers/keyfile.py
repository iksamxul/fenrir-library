"""The sealer of last resort (3.0, STO-2; docs/v3-design/store.md §3.3): a key in a file, for Docker, a NAS and Linux
without systemd credentials or a keyring. The key wraps the store's data key (ChaCha20-Poly1305).

The key is a Docker secret at /run/secrets/fenrir_store_key when Docker gives one (preferred: it stays out of the data
volume), else store.key beside the store, made with mode 0600 so only Fenrir's own user reads it. That keeps the secrets
from other users of the machine and nothing more: anyone who can read the data volume, or a copy of it, reads them too.
Settings, Security and the Doctor say so in those words (core/sealers/__init__.py)."""
import os
from pathlib import Path

from connect import sealed

from .. import store

DOCKER_SECRET = Path("/run/secrets/fenrir_store_key")
AAD = b"fenrir-store-keyfile\0"


def key_path():
    return store.PATH.parent / "store.key"


def where():
    """The key file the store uses, as the Doctor names it."""
    return str(DOCKER_SECRET) + " (a Docker secret)" if DOCKER_SECRET.is_file() else str(key_path())


class KeyFile(store.Sealer):
    kind = "keyfile"
    rank = 10  # only where nothing better exists
    safe_from = "other users of this machine"
    not_safe_from = ("anyone who can read Fenrir's data folder or a copy of it, such as a backup of the whole volume: the key "
                     "that opens your secrets is kept in a file beside them")

    def __init__(self, docker=None, path=None, posix=None):
        self.docker, self.path, self.posix = docker, path, posix

    def _docker(self):
        return Path(self.docker) if self.docker is not None else DOCKER_SECRET

    def _file(self):
        return Path(self.path) if self.path is not None else key_path()

    def available(self):
        return os.name != "nt" if self.posix is None else self.posix  # Windows always has DPAPI, which is better

    def seal(self, key, entropy):
        if self._docker().is_file():
            source, wrapping = "docker", sealed.key_file(self._docker(), create=False)
        else:
            source, wrapping = "file", sealed.key_file(self._file(), create=True)
        if wrapping is None:
            raise store.SealError("The Docker secret fenrir_store_key holds no key (32 bytes, as hex or base64).")
        return {"source": source, **sealed.wrap(wrapping, key, AAD + entropy)}

    def unseal(self, rec, entropy):
        path = self._docker() if rec.get("source") == "docker" else self._file()
        if rec.get("source") == "docker" and not path.is_file():  # the container started without its secret: out of reach, not lost
            raise store.SealerAbsent("Give Fenrir its Docker secret fenrir_store_key again: the store was sealed with it.")
        try:
            wrapping = sealed.key_file(path, create=False)
        except sealed.SealedError as e:
            raise store.SealError(f"The store's key file could not be read: {e}") from None
        if wrapping is None:
            raise store.SealError(f"The store's key file is not there ({path}).")
        try:
            return sealed.unwrap(wrapping, rec, AAD + entropy)
        except sealed.SealedError:
            raise store.SealError("The store's key does not open with this key file.") from None


store.register(KeyFile())
