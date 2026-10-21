"""The fivem hook: what FiveM's recipe cannot say as data, kept as 2.x's core/games/fivem.py does it.

    install      FiveM's own download page names the current server (fivem.server_download); its zip takes install/'s
                 place whole, and mapmanager, spawnmanager and basic-gamemode come from citizenfx/cfx-server-data
    server.cfg   both endpoints on the port, the name, 1 to 48 players, the host's own key, off FiveM's public list
                 (sv_master1 ""), the basics and the host's resources: the bytes 2.x's FiveM.write_config writes"""
import json
import os
import time
from pathlib import Path

from ...games import fivem as fivem2x
from ...games.servers import GameServer, ServerError
from .. import writers
from ..drivers import register
from ..drivers.process import RESOLVERS, ProcessDriver


def _q(v):
    """A value in server.cfg's quotes, as 2.x wrote it: no backslash, a double quote as a single one, one line, 120 at most."""
    return '"' + str(v).replace("\\", "").replace('"', "'").replace("\n", " ")[:120] + '"'


def server_cfg(path, text, sets, ctx):
    """server.cfg as a whole (writers' format hook): what 2.x wrote, in Windows' own line endings as its text mode did."""
    s = ctx["setting"]
    try:
        resources = json.loads((Path(ctx["data"]) / "server.json").read_text("utf-8")).get("resources") or []
    except (OSError, ValueError, KeyError):
        resources = []
    port = int(s["port"])
    lines = [f'endpoint_add_tcp "0.0.0.0:{port}"', f'endpoint_add_udp "0.0.0.0:{port}"', f"sv_hostname {_q(s.get('name', ''))}",
             f"sv_maxclients {max(1, min(48, int(s.get('players') or 8)))}", f"sv_licenseKey {_q(s.get('key', ''))}", 'sv_master1 ""', "",
             *[f"ensure {b}" for b in fivem2x.BASICS], *[f"ensure {r}" for r in resources]]
    return ("\n".join(lines) + "\n").replace("\n", os.linesep)


def download_page(doc, platform, page=None):
    """The download the recipe's resolver names: today's Windows server from FiveM's own page (page reads it, as
    gamekit.net.get does)."""
    info = fivem2x.server_download(page) if page else fivem2x.server_download()
    return [{"platform": "windows-x64", "url": info["url"], "unpack": "zip"}]


def _server_2x(inst):
    """2.x's FiveM object for this server's own folder: Fenrir's fivem (what FiveM(home) is), or a harness run's
    fivem-<8 hex>, so a run never installs into a folder that is not its own."""
    fm = fivem2x.FiveM.__new__(fivem2x.FiveM)
    GameServer.__init__(fm, inst.id, recipe=fivem2x.RECIPE, home=inst.dir.parent)
    return fm


class FiveMDriver(ProcessDriver):
    """FiveM's server, run from its recipe; its install is 2.x's own (the zip in place of install/, then the basics)."""

    def install(self, inst, progress=None, validate=False, fetch=None, page=None, **kw):
        """2.x's install, into this server's own folder. The seams every driver's install has: fetch downloads a file (as
        gamekit.net.download's), page reads a resolver's page (as gamekit.net.get's); 2.x calls them fetch_file and fetch."""
        with inst.lock:
            if self.running(inst):
                raise ServerError("Stop the FiveM server first: its files are in use while it runs.")
            d = inst.load()
            d["installing"] = time.time()
            inst.save(d)
            try:
                seams = {k: v for k, v in (("fetch", page), ("fetch_file", fetch)) if v}
                return _server_2x(inst).install(progress=progress, validate=validate, **seams)
            finally:
                d = inst.load()
                d.pop("installing", None)
                inst.save(d)


register("fivem", FiveMDriver)
writers.register("fivem.server_cfg", server_cfg)
RESOLVERS["fivem-page"] = download_page
