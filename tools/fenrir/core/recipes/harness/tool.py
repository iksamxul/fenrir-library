"""The harness's own programs, run from source and in the built app alike (recipes.md section 3.11.4). From source each is
its Python file; in the built app sys.executable is Fenrir.exe, which runs no .py file, so each is Fenrir.exe --harness
<name>, which app.py hands here before anything else starts (as it does --ctrl-c), so the program's output stays its own.

    command("fake-server", "--play", p, ...)    the command line for one of them
    main(["fake-server", ...])                  Fenrir.exe --harness: run one, its exit code

Check before sharing and an imported draft's test run use the harness inside Fenrir, so these must work in the built app."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = {"fake-server": "fake_server.py", "fake-steamcmd": "fake_steamcmd.py", "fake-docker": "fake_docker.py", "regex-worker": "regexes.py"}


def command(name, *args):
    """The command line that runs one of the harness's programs here, from source or in the built app."""
    if name not in SCRIPTS:
        raise ValueError(f"The harness has no program called {name}.")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--harness", name, *map(str, args)]
    return [sys.executable, str(HERE / SCRIPTS[name]), *map(str, args)]


def main(argv):
    """Fenrir.exe --harness <name> <its arguments>: one of the harness's programs, and its exit code."""
    name, rest = (argv[0] if argv else ""), list(argv[1:])
    if name == "fake-server":
        from . import fake_server
        return fake_server.main(rest)
    if name == "fake-steamcmd":
        from . import fake_steamcmd
        return fake_steamcmd.main(rest)
    if name == "fake-docker":
        from . import fake_docker
        return fake_docker.main(rest)
    if name == "regex-worker":
        from . import regexes
        regexes._worker()
        return 0
    sys.stderr.write(f"Fenrir's harness has no program called {name or 'that'}.\n")
    return 2
