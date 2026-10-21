"""Where each program Fenrir downloads comes from, for this machine's system and processor (docs/v3-design/server.md
§3.3.6, §3.11). Windows' addresses are 2.x's own, unchanged; Linux has builds for x64 and arm64 where the tool's maker
publishes them. A tool with no build for this machine is refused in plain words (NoBuild), never fetched for the wrong one.

    tool(name, system, arch)   {"url", "file", "unpack", "exe", "mode"}: where to get it, the file it is saved as, how it
                               unpacks (None, "zip" or "tar.gz"), the program inside, and its mode on Linux
    java(major, system, arch)  Adoptium's Temurin JRE for a Java version, as an archive"""

CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
PLAYIT = "https://github.com/playit-cloud/playit-agent/releases/download/v0.15.26/"
STEAMCMD = "https://steamcdn-a.akamaihd.net/client/installer/"
ADOPTIUM = "https://api.adoptium.net/v3/binary/latest/{major}/ga/{os}/{arch}/jre/hotspot/normal/eclipse"

TOOLS = {
    "cloudflared": {
        ("windows", "x64"): {"url": CLOUDFLARED + "cloudflared-windows-amd64.exe", "file": "cloudflared.exe"},
        ("linux", "x64"): {"url": CLOUDFLARED + "cloudflared-linux-amd64", "file": "cloudflared", "mode": 0o755},
        ("linux", "arm64"): {"url": CLOUDFLARED + "cloudflared-linux-arm64", "file": "cloudflared", "mode": 0o755},
    },
    "playit": {
        ("windows", "x64"): {"url": PLAYIT + "playit-windows-x86_64.exe", "file": "playit.exe"},
        ("linux", "x64"): {"url": PLAYIT + "playit-linux-amd64", "file": "playit", "mode": 0o755},
        ("linux", "arm64"): {"url": PLAYIT + "playit-linux-aarch64", "file": "playit", "mode": 0o755},
    },
    "steamcmd": {
        ("windows", "x64"): {"url": STEAMCMD + "steamcmd.zip", "file": "steamcmd.zip", "unpack": "zip", "exe": "steamcmd.exe"},
        ("linux", "x64"): {"url": STEAMCMD + "steamcmd_linux.tar.gz", "file": "steamcmd_linux.tar.gz", "unpack": "tar.gz", "exe": "steamcmd.sh"},
    },
}
NAMES = {"cloudflared": "cloudflared", "playit": "The playit agent", "steamcmd": "SteamCMD"}
JAVA_ARCH = {"x64": "x64", "arm64": "aarch64"}


class NoBuild(Exception):
    """A tool its maker does not build for this machine, said plainly."""


def _here():
    from . import IMPL
    return ("windows" if IMPL.name == "windows" else "linux"), IMPL.arch()


def tool(name, system=None, arch=None):
    if system is None or arch is None:
        s, a = _here()
        system, arch = system or s, arch or a
    builds = TOOLS.get(name)
    if builds is None:
        raise KeyError(name)
    got = builds.get((system, arch))
    if got is None:
        from ..i18n import M
        if (name, arch) == ("steamcmd", "arm64"):
            raise NoBuild(M("tools.steamcmd_arm"))
        raise NoBuild(M("tools.no_build", tool=NAMES.get(name, name), system="Windows" if system == "windows" else "Linux", arch=arch))
    return {"unpack": None, "exe": got["file"], "mode": None, **got}


def url(name, system=None, arch=None):
    return tool(name, system, arch)["url"]


def java(major, system=None, arch=None):
    """Adoptium's Temurin JRE for a Java version: {"url", "unpack"} (a zip on Windows, a .tar.gz on Linux)."""
    if system is None or arch is None:
        s, a = _here()
        system, arch = system or s, arch or a
    if arch not in JAVA_ARCH:
        from ..i18n import M
        raise NoBuild(M("tools.java_no_build", arch=arch))
    return {"url": ADOPTIUM.format(major=int(major), os=system, arch=JAVA_ARCH[arch]), "unpack": "zip" if system == "windows" else "tar.gz"}
