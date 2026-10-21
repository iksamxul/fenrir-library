"""The store's sealer on macOS (3.0, STO-2; docs/v3-design/store.md §3.3): the data key as a generic password in the login
keychain, through /usr/bin/security. It is written through security's own prompt on standard input, never on a command
line, and found again by an account name made for this store's seal.

A keychain that is locked or will not answer leaves the store as it is (store.SealerAbsent), never taken for one from
elsewhere."""
from connect import sealed

from .. import store

SERVICE = "Fenrir"


class Keychain(store.Sealer):
    kind = "keychain"
    rank = 40
    bound = True
    absent = "It is in your login keychain: start Fenrir while you are signed in, with the keychain unlocked."
    safe_from = "other users of this Mac, and anyone who copies Fenrir's folder elsewhere"
    not_safe_from = "programs running as you, once you let them"

    def __init__(self, runner=None, ready=None):
        self.runner, self.ready = runner, ready

    def _run(self, *a, **k):
        return (self.runner or sealed.run)(*a, **k)

    def available(self):
        return (self.ready or sealed.keychain_ready)()

    def seal(self, key, entropy):
        try:
            sealed.keychain_store(SERVICE, f"store-{entropy.hex()}", key, self._run)
        except sealed.SealedError as e:
            raise store.SealError(str(e)) from None
        return {"account": f"store-{entropy.hex()}"}

    def unseal(self, rec, entropy):
        key = sealed.keychain_find(SERVICE, f"store-{entropy.hex()}", self._run)
        if key is None:
            raise store.SealerAbsent("The keychain did not give the store's key back (is it locked?).")
        return key


store.register(Keychain())
