"""GTA V's modded multiplayer: a FiveM server for GTA V Enhanced ("Cfx Server", Rockstar's Creator Platform), hosted by
Fenrir with the host's own license key. Friends join with the FiveM app for Enhanced; the server's resources (its mods)
download to them by themselves when they connect.

    the server   cfx-server_win_x64.zip from the address FiveM's own download page carries (its __NEXT_DATA__), unpacked
                 into install/; cfx-server.exe started from server-data/ with "+exec server.cfg" (txAdmin is skipped)
    the basics   mapmanager, spawnmanager and basic-gamemode from github.com/citizenfx/cfx-server-data
    the key      free with a Cfx.re account at portal.cfx.re (Generate Key, cfxk_…): each owner uses their own, never shared
    privately    sv_master1 "" keeps the server off FiveM's public list; friends join by address
    friends      fivem://connect/<address>:30120, with the FiveM app for Enhanced (its installer from depot.cfx.re)

The Creator Platform License Agreement applies to whoever runs the server: no selling outside approved stores, and a
public page would have to name the operator and say it is not approved by Rockstar. Fenrir keeps servers private."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from gamekit.archives import unpack_zip
from gamekit.net import download, get
from .servers import GameServer, ServerError, fill, from_recipes

DOCS = "https://docs.fivem.net/docs/server-download/"
SERVER_DATA = "https://github.com/citizenfx/cfx-server-data/archive/refs/heads/master.zip"
CLIENT = "https://depot.cfx.re/public/p/cfxinstaller/u/main/c/FiveM_Installer.exe"
KEY_PAGE = "https://portal.cfx.re/servers/registration-keys"
BASICS = ("mapmanager", "spawnmanager", "basic-gamemode")

RECIPE_29 = {  # 2.9's own; since 3.0 the built-in recipe says it (fenrir/recipes/builtin/fivem.json), and this stands in when it does not load
    "name": "GTA V (FiveM)", "client": ["3240220"], "exe": "cfx-server.exe", "args": ["+exec", "server.cfg"],
    "defaults": {"name": "Fenrir", "port": 30120, "players": 8, "key": ""}, "ports": [["{port}", "tcp"], ["{port}", "udp"]],
    "stopWait": 20, "join": "Open fivem://connect/{address}:{port} (FiveM for GTA V Enhanced), or press F8 in FiveM and type connect {address}:{port}.",
}
RECIPE = from_recipes({"fivem": RECIPE_29})["fivem"]


def server_download(fetch=get):
    """The current Windows server for Enhanced: {"url", "name", "build"} from FiveM's own download page."""
    page = fetch(DOCS).decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise ServerError("FiveM's download page has changed; get the server from docs.fivem.net and point Fenrir at its folder.")
    try:
        win = json.loads(m.group(1))["props"]["pageProps"]["enhanced"]["windows"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ServerError("FiveM's download page names no Windows server for GTA V Enhanced right now.") from None
    url = str(win.get("downloadURL") or "")
    if not re.match(r"^https://downloads\.cfx-services\.net/", url):
        raise ServerError("FiveM's download page points somewhere unexpected, so Fenrir did not use it.")
    return {"url": url, "name": win.get("displayName") or "cfx-server_win_x64.zip", "build": (win.get("subtitle") or "").replace("build", "").strip()}


class FiveM(GameServer):
    def __init__(self, home=None):
        super().__init__("fivem", recipe=RECIPE, home=home)

    def data_dir(self):
        return self.dir / "server-data"

    def resources(self):
        return self.data_dir() / "resources"

    def exe(self, game_dir=None):
        return self.install_dir() / "cfx-server.exe"

    def validate(self, changes):
        super().validate(changes)
        key = str(changes.get("key") or "")
        if key and not re.fullmatch(r"cfxk_[A-Za-z0-9_]{10,}", key):
            raise ServerError(f"That is not a FiveM server key (a key starts with cfxk_). Make one, free, at {KEY_PAGE} with Generate Key.")

    def check_settings(self, s):
        super().check_settings(s)
        if not re.fullmatch(r"cfxk_[A-Za-z0-9_]{10,}", str(s.get("key") or "")):
            raise ServerError(f"FiveM needs your own server key: make one (free) at {KEY_PAGE} with Generate Key, then paste it here.")

    def install(self, progress=None, validate=False, fetch=get, fetch_file=download):
        if self.running():
            raise ServerError("Stop the FiveM server first: its files are in use while it runs.")
        info = server_download(fetch)
        tmp = Path(tempfile.mkdtemp(prefix="fenrir-fivem-"))
        try:
            z = tmp / info["name"]
            fetch_file(info["url"], z, progress=(lambda f: progress(f * 70, "Getting the FiveM server")) if progress else None)
            new = tmp / "install"
            unpack_zip(z, new)
            if not (new / "cfx-server.exe").exists():
                raise ServerError("The FiveM server download holds no cfx-server.exe; FiveM may have changed it.")
            shutil.rmtree(self.install_dir(), ignore_errors=True)
            if self.install_dir().exists():  # something holds the old files: moving the new ones in would nest them
                raise ServerError("The old FiveM server files are in use, so the new ones could not take their place. Close what uses them and try again.")
            self.install_dir().parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), str(self.install_dir()))
            if not all((self.resources() / "[basics]" / b).is_dir() for b in BASICS):
                if progress:
                    progress(80, "Getting the basic game mode")
                sz = tmp / "server-data.zip"
                fetch_file(SERVER_DATA, sz)
                sd = tmp / "server-data"
                unpack_zip(sz, sd)
                for b in BASICS:
                    src = next((p for p in sd.rglob(b) if p.is_dir() and ((p / "fxmanifest.lua").exists() or (p / "__resource.lua").exists())), None)
                    if src:
                        dst = self.resources() / "[basics]" / b
                        shutil.rmtree(dst, ignore_errors=True)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(src, dst)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        d = self.load()
        d.update(build=info["build"], installedAt=__import__("time").time())
        self.save(d)
        return {"ok": True, "message": "The FiveM server is installed"}

    # ---- resources: a folder with fxmanifest.lua, started with "ensure <name>"
    def add_resource(self, folder, name=None):
        folder = Path(folder)
        root = next((p.parent for p in [folder / "fxmanifest.lua", folder / "__resource.lua", *folder.rglob("fxmanifest.lua")] if p.exists()), None)
        if root is None:
            raise ServerError("That is not a FiveM resource: it has no fxmanifest.lua.")
        name = re.sub(r"[^A-Za-z0-9_\-]", "-", name or root.name).strip("-").lower() or "resource"
        dst = self.resources() / "[fenrir]" / name
        # 2.8 (the bug hunt): the old copy was deleted file by file under a running server, and one it held left the
        # resource half gone. A new copy is whole beside the resources (never one FiveM would load) before the old one goes
        # in one rename, and a resource the running server has is never replaced under it
        if dst.exists() and self.running():
            raise ServerError(f"The server is running with {name}: stop it, then add the new copy.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir() / f".fenrir-new-{name}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(root, tmp)
        old = None
        if dst.exists():
            old = self.data_dir() / f".fenrir-old-{name}-{os.getpid()}"
            try:
                os.replace(dst, old)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ServerError(f"A file of {name} is in use: close what holds it, then add the new copy again.") from None
        try:
            os.replace(tmp, dst)
        except OSError:  # the new copy held a moment (a scan): the old one goes back, never a resource gone (the review of 2.8)
            if old is not None:
                os.replace(old, dst)
            shutil.rmtree(tmp, ignore_errors=True)
            raise ServerError(f"The new copy of {name} is held by another program (a virus scan?): try again in a moment.") from None
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        d = self.load()
        d["resources"] = sorted(set(d.get("resources") or []) | {name})
        self.save(d)
        return name

    def remove_resource(self, name):
        shutil.rmtree(self.resources() / "[fenrir]" / name, ignore_errors=True)
        d = self.load()
        d["resources"] = [r for r in d.get("resources") or [] if r != name]
        self.save(d)

    def write_config(self, values):
        def q(v):
            return '"' + str(v).replace("\\", "").replace('"', "'").replace("\n", " ")[:120] + '"'
        lines = [f'endpoint_add_tcp "0.0.0.0:{int(values["port"])}"', f'endpoint_add_udp "0.0.0.0:{int(values["port"])}"',
                 f"sv_hostname {q(values['name'])}", f"sv_maxclients {max(1, min(48, int(values.get('players') or 8)))}",
                 f"sv_licenseKey {q(values['key'])}", 'sv_master1 ""', "", *[f"ensure {b}" for b in BASICS]]
        lines += [f"ensure {r}" for r in self.load().get("resources") or []]
        self.data_dir().mkdir(parents=True, exist_ok=True)
        (self.data_dir() / "server.cfg").write_text("\n".join(lines) + "\n", "utf-8")

    def command(self, game_dir=None):
        return [str(self.exe()), "+exec", "server.cfg"]

    def start(self, game_dir=None, popen=None):
        """From server-data (its resources and server.cfg are found from there)."""
        real = popen or __import__("subprocess").Popen

        def from_data(args, **kw):
            kw["cwd"] = str(self.data_dir())
            return real(args, **kw)
        self.data_dir().mkdir(parents=True, exist_ok=True)
        return super().start(game_dir, popen=from_data)

    def status(self, game_dir=None):
        st = super().status(game_dir)
        st["settings"].pop("key", None)
        st["keySet"] = bool(self.settings().get("key"))
        st["keyNeeded"] = True  # FiveM starts no server without the host's own key (hosting.setting_needs)
        st["resources"] = self.load().get("resources") or []
        st["join"] = fill(RECIPE["join"], {**self.settings(), "address": "{address}"})
        return st
