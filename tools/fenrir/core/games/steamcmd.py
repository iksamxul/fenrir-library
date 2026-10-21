"""SteamCMD, Valve's command-line Steam, for dedicated servers: fetched once from Valve into DATA/games/steamcmd, then
`steamcmd.exe +force_install_dir <dir> +login anonymous +app_update <id> [validate] +quit` installs or updates a server.
Its progress lines ("Update state (0x61) downloading, progress: 12.34 (…)") drive the task's bar, and its result lines are
read rather than its exit code: "Success! App '<id>' fully installed." is the only success; "No subscription" means the
server needs an account that owns the game; state 0x202 means the disk is full."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..util import DATA
from gamekit.archives import unpack_zip
from gamekit.net import download

URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
HOME = DATA / "games" / "steamcmd"
PROGRESS = re.compile(r"Update state \((0x[0-9a-f]+)\) ([^,]+), progress: ([\d.]+) \((\d+) / (\d+)\)")
SUCCESS = re.compile(r"Success! App '(\d+)' fully installed")
WORKSHOP_OK = re.compile(r"Success\. Downloaded item (\d+) to \"([^\"]+)\"")


class SteamCmdError(Exception):
    """Said to the host as it is."""


def exe(home=None):
    from .. import oslayer
    return Path(home or HOME) / ("steamcmd.exe" if oslayer.system() == "windows" else "steamcmd.sh")


def ensure(home=None, progress=None):
    """SteamCMD from Valve, once: this machine's build (core/oslayer/tools.py). There is none for ARM: said plainly."""
    from ..oslayer import tools
    home = Path(home or HOME)
    if exe(home).exists():
        return exe(home)
    try:
        got = tools.tool("steamcmd")
    except tools.NoBuild as e:
        raise SteamCmdError(str(e)) from None
    tmp = Path(tempfile.mkdtemp(prefix="fenrir-steamcmd-"))
    try:
        z = tmp / got["file"]
        download(got["url"], z, progress=(lambda f: progress(f * 10, "Getting SteamCMD from Valve")) if progress else None)
        home.mkdir(parents=True, exist_ok=True)
        if got["unpack"] == "zip":
            unpack_zip(z, home)
        else:
            _unpack_tar(z, home)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not exe(home).exists():
        raise SteamCmdError("SteamCMD did not unpack.")
    return exe(home)


def _unpack_tar(archive, dest):
    """Valve's steamcmd_linux.tar.gz into dest: plain files and folders only, each inside dest, modes kept for its scripts."""
    import tarfile
    dest = Path(dest).resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not (m.isfile() or m.isdir()) or (target != dest and dest not in target.parents):
                continue  # a link, a device, or a name that would leave the folder: never unpacked
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, m.mode & 0o755)


def read(lines, app_id=None, progress=None):
    """SteamCMD's output → the verdict: {"ok", "error"}; progress(pct, text) for each progress line."""
    ok, error = False, None
    for line in lines:
        m = PROGRESS.search(line)
        if m and progress:
            state = m.group(2).strip()
            pct = float(m.group(3))
            word = {"downloading": "Downloading", "validating": "Checking", "preallocating": "Making room", "committing": "Finishing",
                    "reconfiguring": "Getting ready"}.get(state.split()[0] if state else "", "Working")
            progress(10 + pct * 0.88, f"{word} the server ({pct:.0f}%)")
        if SUCCESS.search(line) and (app_id is None or SUCCESS.search(line).group(1) == str(app_id)):
            ok = True
        low = line.lower()
        if "no subscription" in low:
            error = "This server needs a Steam account that owns the game; SteamCMD's anonymous login cannot get it."
        elif "0x202" in low or "not enough disk space" in low:
            error = "There is not enough free disk space for the server."
        elif "invalid platform" in low:
            error = "Valve has no Windows build of this server."
        elif low.startswith("error!") and not error:
            error = "SteamCMD stopped: " + line.strip()[7:160]
    return {"ok": ok and not error, "error": None if ok and not error else (error or "SteamCMD did not finish the install.")}


def install(app_id, dest, validate=False, beta=None, home=None, progress=None, runner=None):
    """Install or update a dedicated server into `dest` (an absolute folder). Returns read()'s verdict."""
    steam = ensure(home, progress)
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    update = f"{int(app_id)}" + (f" -beta {re.sub(r'[^A-Za-z0-9_.-]', '', beta)}" if beta else "") + (" validate" if validate else "")
    args = [str(steam), "+force_install_dir", str(dest), "+login", "anonymous", "+app_update", update, "+quit"]
    verdict = read((runner or _run)(args, Path(steam).parent), app_id, progress)
    if not verdict["ok"] and "missing configuration" in (verdict["error"] or "").lower():
        # 3.0, the first live check: a SteamCMD fresh from Valve has not read the app's configuration on its first run,
        # and the same command run again goes on
        verdict = read((runner or _run)(args, Path(steam).parent), app_id, progress)
    if not verdict["ok"]:
        raise SteamCmdError(verdict["error"])
    return verdict


def vdf(text):
    """Valve's KeyValues text ("key" "value" and "key" { ... }) as nested dicts; what cannot be read is left out."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    root, stack, key = {}, [], None
    node = root
    for word, brace in tokens:
        if brace == "{":
            child = {}
            if key is not None:
                node[key] = child
            stack.append(node)
            node, key = child, None
        elif brace == "}":
            node, key = (stack.pop() if stack else root), None
        elif key is None:
            key = word
        else:
            node[key], key = word, None
    return root


def build_id(app_id, branch="public", home=None, runner=None):
    """The build id Steam has now for one branch of an app, from SteamCMD's anonymous +app_info_print (safe updates check
    this, core/recipes/updates.py), or None when Steam did not say. SteamCMD is never fetched for it: a server installed
    through SteamCMD has it already."""
    steam = exe(home)
    if not steam.exists():
        return None
    args = [str(steam), "+login", "anonymous", "+app_info_update", "1", "+app_info_print", str(int(app_id)), "+quit"]
    text = "\n".join((runner or _run)(args, steam.parent))
    cut = text.find(f'"{int(app_id)}"')  # the app's own block, after SteamCMD's lines about itself
    info = vdf(text[cut:]).get(str(int(app_id))) if cut >= 0 else None
    found = ((((info or {}).get("depots") or {}).get("branches") or {}).get(branch) or {}).get("buildid")
    return str(found) if str(found or "").isdigit() else None


def installed_build(dest, app_id):
    """The build id SteamCMD installed into dest (steamapps/appmanifest_<id>.acf), or None."""
    try:
        text = (Path(dest) / "steamapps" / f"appmanifest_{int(app_id)}.acf").read_text("utf-8", errors="replace")
    except OSError:
        return None
    found = (vdf(text).get("AppState") or {}).get("buildid")
    return str(found) if str(found or "").isdigit() else None


def workshop(app_id, items, dest, home=None, progress=None, runner=None):
    """Workshop items for a server (only games that allow anonymous Workshop downloads: tModLoader, Synergy…).
    Returns {item id: folder}."""
    steam = ensure(home, progress)
    dest = Path(dest).resolve()
    args = [str(steam), "+force_install_dir", str(dest), "+login", "anonymous"]
    for i in items:
        args += ["+workshop_download_item", str(int(app_id)), str(int(i))]
    args.append("+quit")
    lines = (runner or _run)(args, Path(steam).parent)
    out = {}
    for line in lines:
        m = WORKSHOP_OK.search(line)
        if m:
            out[m.group(1)] = m.group(2)
    missing = [str(i) for i in items if str(i) not in out]
    if missing:
        raise SteamCmdError(f"SteamCMD could not download Workshop item{'s' if len(missing) > 1 else ''} {', '.join(missing)} "
                            "(the game may not allow it without an account).")
    return out


def _run(args, cwd):
    """SteamCMD's lines as they come (it rewrites its first run's self-update in place, so lines end with \\r or \\n)."""
    p = subprocess.Popen(args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    from ..util import TASKS
    TASKS.attach(p)  # Cancel (and a SteamCMD that hangs: the task's stall) ends it (2.3)
    buf = b""
    while True:
        chunk = p.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        while True:
            cut = min([i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0], default=-1)
            if cut < 0:
                break
            line, buf = buf[:cut], buf[cut + 1:]
            if line.strip():
                yield line.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")
    p.wait()
