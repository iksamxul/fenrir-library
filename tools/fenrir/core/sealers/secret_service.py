"""The store's sealer on a Linux desktop (3.0, STO-2; docs/v3-design/store.md §3.3): the data key kept as an item of the
desktop's keyring (GNOME Keyring; KWallet 5.97 or later) through the Secret Service, with secret-tool when it is installed
(SteamOS does not install it: the key file then, as store.md §7 question 3 has it). The item is found again by an
attribute made for this store's seal, so two stores on one desktop never share one.

A keyring that is locked or out of reach (no desktop session) leaves the store as it is (store.SealerAbsent): a store is
never taken for one from elsewhere because the keyring could not be asked."""
from connect import sealed

from .. import store

APPLICATION = "Fenrir"


class SecretService(store.Sealer):
    kind = "secret-service"
    rank = 40
    bound = True
    absent = "It is in your desktop's keyring: start Fenrir from your desktop session, with the keyring unlocked."
    safe_from = "other users of this machine, and anyone who copies Fenrir's folder elsewhere (your login locks the keyring)"
    not_safe_from = "programs running as you"

    def __init__(self, runner=None, ready=None):
        self.runner, self.ready = runner, ready

    def _run(self, *a, **k):
        return (self.runner or sealed.run)(*a, **k)

    def available(self):
        return (self.ready or sealed.secret_tool_ready)()

    @staticmethod
    def _attrs(entropy):
        return {"application": APPLICATION, "fenrir-store": entropy.hex()}

    def seal(self, key, entropy):
        try:
            sealed.secret_store(self._attrs(entropy), key, "Fenrir's sealed store", self._run)
        except sealed.SealedError as e:
            raise store.SealError(str(e)) from None
        return {"item": entropy.hex()}

    def unseal(self, rec, entropy):
        key = sealed.secret_lookup(self._attrs(entropy), self._run)
        if key is None:
            raise store.SealerAbsent("The keyring did not give the store's key back (is it locked?).")
        return key


store.register(SecretService())
