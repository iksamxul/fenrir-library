"""The sealers beside DPAPI for this Windows user (3.0, STO-2; docs/v3-design/store.md §3.3). Each module registers its
sealer with the store (core/store.py register), which picks the strongest one this process can use, or the one
FENRIR_STORE_SEALER names:

    dpapi-service   Fenrir Server, the Windows service: DPAPI for NT SERVICE\\FenrirServer (or for this PC, see the module)
    systemd-creds   a Linux node under systemd 250 or later: a key systemd decrypts for the service alone
    secret-service  a Linux desktop: the keyring, through secret-tool
    keychain        macOS: the login keychain, through security
    keyfile         Docker, a NAS, Linux without the above: a key in a file beside the data, which the Doctor says plainly

The platform calls themselves are connect/sealed.py's, which Fenrir Connect uses for its own key. findings() is what the
Doctor says about the store (core/doctor.py)."""
from . import dpapi_service, keychain, keyfile, secret_service, systemd  # noqa: F401 - each registers its sealer

KEYFILE_TITLE = "Your secrets are locked with a key kept in a file"
KEYFILE_TEXT = ("This machine has nothing better to keep the key in (no Windows account, no keyring, no systemd credentials), so "
                "the key that opens Fenrir's secrets is a file beside them: {where}. Other users of this machine cannot read it, "
                "but anyone who can read Fenrir's data folder, or a copy of it such as a backup of the whole volume, can read "
                "your secrets too. Keep that folder and its backups private. In Docker, give Fenrir the key as a Docker secret "
                "named fenrir_store_key instead.")


def findings():
    """[(id, severity, title, detail)] about the sealed store for the Doctor: a key file, said plainly; a store that could
    not be opened; one kept aside because it could not be used here."""
    from .. import store
    st = store.status()
    out = []
    if st.get("sealer") == "keyfile":
        out.append(("storekeyfile", "info", KEYFILE_TITLE, KEYFILE_TEXT.format(where=keyfile.where())))
    if not st.get("ok"):
        out.append(("storeclosed", "warn", "Fenrir's sealed store could not be opened",
                    f"{st.get('error') or 'Nothing here can seal secrets.'} Until it opens, Fenrir keeps its secrets in its settings "
                    "file as before, and moves them back into the store at a start where it opens."))
    aside = [k for k in st.get("keptAside") or [] if k.get("why") == "foreign" and not str(k.get("file")).endswith(".bak")]
    if aside:
        files = ", ".join(k["file"] for k in aside)
        out.append(("storeaside", "warn", "A sealed store from before could not be opened here",
                    f"It was sealed for another Windows account or another machine, so it is kept, untouched, as {files} beside "
                    "the new one. Secrets in it need typing again: keys for other services, passwords, and the phone's link."))
    if st.get("recovered"):
        out.append(("storerecovered", "info", "The sealed store was damaged, and its copy from before took over",
                    "Fenrir keeps the copy of the store from before its last change, and used it. A secret changed in that last "
                    "change may need typing again."))
    return out
