"""The skyrimtogether hook: what Skyrim Together's recipe cannot say as data, kept as 2.x's servers.SkyrimTogether does it.

    install    a copy of the kit's server folder (never links: a running server must not lock the kit, nor its next
               version change a running server's files), its config folder left as the server keeps it
    start      the copy refreshed first when the kit's server changed: client and server must be one version
    the ini    2.x's limits on STServer.ini: one line of at most 64 characters, 1 to 16 players, a name always there"""
from pathlib import Path

from ...games.servers import ServerError
from ..drivers import register
from ..drivers.process import ProcessDriver

KIT_SERVER = ("Data", "SkyrimTogetherReborn")


class SkyrimTogetherDriver(ProcessDriver):

    def _kit_server(self, inst):
        return (Path(inst.kit_dir) if inst.kit_dir else Path("-")).joinpath(*KIT_SERVER)

    def install(self, inst, progress=None, **kw):
        if not (self._kit_server(inst) / "SkyrimTogetherServer.exe").is_file():
            raise ServerError("Get Skyrim Together Reborn first (Before you start): its server comes with it.")
        if self.running(inst):
            raise ServerError("Stop the Skyrim Together server first.")
        super().install(inst, progress, **kw)
        return {"ok": True, "message": "The Skyrim Together server is ready"}

    def start(self, inst, popen=None):
        kit, here = self._kit_server(inst) / "SkyrimTogetherServer.exe", inst.install_dir() / "SkyrimTogetherServer.exe"
        try:
            stale = kit.is_file() and (not here.is_file() or kit.stat().st_size != here.stat().st_size or abs(kit.stat().st_mtime - here.stat().st_mtime) > 2)
        except OSError:
            stale = False
        if stale and not self.running(inst):
            self.install(inst)
        return super().start(inst, popen)

    def configure(self, inst, when="start", ctx=None):
        ctx = dict(ctx or self.context(inst, inst.doc(), self.settings(inst, open_secrets=True)))
        s = dict(ctx["setting"])
        clean = lambda v: str(v).replace("\n", " ").replace("\r", " ")[:64]  # noqa: E731
        s["name"] = clean(s.get("name") or "Fenrir")
        s["password"] = clean(s.get("password") or "")
        s["players"] = str(max(1, min(16, int(s.get("players") or 8))))
        s["port"] = str(int(s["port"]))
        ctx["setting"] = s
        return super().configure(inst, when, ctx)


register("skyrimtogether", SkyrimTogetherDriver)
