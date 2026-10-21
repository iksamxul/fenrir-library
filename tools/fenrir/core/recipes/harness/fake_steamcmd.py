"""The fake SteamCMD (recipes.md section 3.11.2): what Valve's steamcmd.exe says and leaves behind, for the harness. Fenrir's
own SteamCMD code (core/games/steamcmd.py) runs as it always does; only the program it starts is this one, through its
runner seam:

    steamcmd.install(app, dest, home=<a folder with a stand-in steamcmd.exe>, runner=runner(<state.json>))

    python fake_steamcmd.py --state <state.json> +force_install_dir <dir> +login anonymous +app_update "<id> validate" +quit
    python fake_steamcmd.py --state <state.json> +login anonymous +app_info_print <id> +quit

+app_update writes the app's files (the recipe's program and the files its writers start from) and its build id into
steamapps/appmanifest_<id>.acf, with progress lines in SteamCMD's own format; +app_info_print says the latest build id.
The state file holds, per app: its files, its buildIds and which one is the latest, and whether the next update fails
(disk full, as SteamCMD says it). The harness moves the latest build on for its update step."""
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

try:
    from . import tool
except ImportError:  # run as a program, beside tool.py
    import tool


def _state(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {"apps": {}}


def _update(state, app, dest, out):
    a = (state.get("apps") or {}).get(str(app))
    if a is None:
        out(f"ERROR! Failed to install app '{app}' (No subscription)")
        return 1
    if a.get("fail"):
        out(f"Error! App '{app}' state is 0x202 after update job.")
        return 1
    builds = a.get("buildIds") or ["1"]
    build = builds[min(int(a.get("latest") or 0), len(builds) - 1)]
    for pct in (12.5, 50.0, 100.0):
        out(f" Update state (0x61) downloading, progress: {pct:.2f} ({int(pct * 10)} / 1000)")
    root = Path(dest)
    for rel, text in (a.get("files") or {}).items():
        p = root / PurePosixPath(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")
    manifest = root / "steamapps" / f"appmanifest_{app}.acf"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{app}"\n\t"buildid"\t\t"{build}"\n\t"installdir"\t\t"{root.name}"\n}}\n', "utf-8")
    out(f"Success! App '{app}' fully installed.")
    return 0


def _info(state, app, out):
    a = (state.get("apps") or {}).get(str(app)) or {}
    builds = a.get("buildIds") or ["1"]
    build = builds[min(int(a.get("latest") or 0), len(builds) - 1)]
    out(f'"{app}"\n{{\n\t"common"\n\t{{\n\t\t"name"\t\t"{a.get("name") or app}"\n\t}}\n\t"depots"\n\t{{\n\t\t"branches"\n\t\t{{\n'
        f'\t\t\t"public"\n\t\t\t{{\n\t\t\t\t"buildid"\t\t"{build}"\n\t\t\t}}\n\t\t}}\n\t}}\n}}')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    state_path = argv[argv.index("--state") + 1] if "--state" in argv else os.environ.get("FENRIR_FAKE_STEAMCMD", "")
    state = _state(state_path)
    out = (lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))
    out("Redirecting stderr to 'logs\\stderr.txt'")
    out("Loading Steam API...OK")
    dest, anonymous, code, i = None, False, 0, 0
    calls = state.setdefault("calls", [])
    while i < len(argv):
        a = argv[i]
        if a == "+force_install_dir" and i + 1 < len(argv):
            dest, i = argv[i + 1], i + 2
            continue
        if a == "+login" and i + 1 < len(argv):
            anonymous = argv[i + 1] == "anonymous"
            out("Connecting anonymously to Steam Public...OK" if anonymous else "FAILED login with result code Invalid Password")
            i += 2
            continue
        if a == "+app_update" and i + 1 < len(argv):
            words = argv[i + 1].split()
            calls.append({"do": "app_update", "app": words[0], "validate": "validate" in words, "dir": dest})
            if not anonymous:
                code = 5
            elif not dest:
                out("ERROR! Failed to install app (no install folder)")
                code = 1
            else:
                code = _update(state, words[0], dest, out) or code
            i += 2
            continue
        if a == "+app_info_print" and i + 1 < len(argv):
            calls.append({"do": "app_info_print", "app": argv[i + 1]})
            code = _info(state, argv[i + 1], out) or code
            i += 2
            continue
        i += 1
    if state_path:
        try:
            Path(state_path).write_text(json.dumps(state, indent=1), "utf-8")
        except OSError:
            pass
    return code


def runner(state_path):
    """The runner steamcmd.install and workshop take: this program in place of steamcmd.exe, its lines as SteamCMD's."""
    def run(args, cwd=None):
        p = subprocess.run(tool.command("fake-steamcmd", "--state", state_path, *args[1:]),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return (p.stdout or "").splitlines()
    return run


def home(folder):
    """A SteamCMD folder with a stand-in steamcmd.exe in it, so Fenrir's ensure() finds SteamCMD and fetches nothing."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "steamcmd.exe").write_text("Fake SteamCMD for Fenrir's recipe harness.\n", "utf-8")
    return folder


if __name__ == "__main__":
    sys.exit(main())
