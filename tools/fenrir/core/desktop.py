"""What the host's desktop does for an engine that may have none (docs/v3-design/server.md §3.1, §3.2.6): opening what
Fenrir built, and Windows' administrator prompt for a firewall rule or for Fenrir Server's removal.

Per user, the engine runs in the host's session and does both itself, as 2.x did. Fenrir Server, the Windows service, runs
in session 0, where nobody sees a window or a prompt: there both go to the shell on the host's desktop (core/shellhub.py,
core/shellside.py), and only in a fixed shape that the shell checks again and builds the command from itself. So whatever
got into the service can never put a script of its own in front of the host's administrator prompt: at most a firewall
rule named as Fenrir names its rules, which the host sees asked for and may decline.

Nothing here loads the settings, since the shell imports it too."""
import base64
import os
import re
import subprocess
from pathlib import Path

from .util import ps_quote

CREATE_NO_WINDOW = 0x08000000
DECLINED = 1223  # Windows' code for an administrator prompt the host said No to
NO_SHELL = -1  # the service asked with no shell attached, or the shell never answered
BAD_REQUEST = 87  # what the shell answers a request of the wrong shape (ERROR_INVALID_PARAMETER)
NOT_THERE = 2  # one of Fenrir's own scripts is missing from the shell's tools folder (ERROR_FILE_NOT_FOUND)
PROMPT_WAIT = 660  # s the service waits for the shell's answer: the prompt itself may wait 300 s, the script after it too
NO_SHELL_TEXT = ("The firewall still blocks friends: allow it on this PC. Fenrir Server runs as a Windows service, which "
                 "cannot show Windows' administrator prompt, so open Fenrir's window on this PC and allow it there.")
NO_DESKTOP_TEXT = ("Fenrir Server runs as a Windows service, without a screen of its own: open Fenrir's window on this PC, "
                   "then try again there.")

# what may be opened on the host's desktop, besides a folder or a file that is there: the addresses Fenrir builds
OPENABLE = (
    re.compile(r"^https://[\w./?=&%#-]+$"),  # core/paths.py open_url's, a tunnel's, a site's public address
    re.compile(r"^http://(127\.0\.0\.1|localhost|10(\.\d{1,3}){3}|192\.168(\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2})"
               r"(:\d{2,5})?(/[\w./?=&%#-]*)?$"),  # this PC's own (the world map, a listener without a tunnel) and a home network's
    re.compile(r"^(steam://install/\d{1,10}|com\.epicgames\.launcher://apps/[A-Za-z0-9%._\-]{3,200}\?action=install"
               r"|goggalaxy://openGameView/\d{1,12})$"),  # a store's own install link (core/games/webapi.py store-install)
)
RULE_NAME = re.compile(r"^(Fenrir - |ATM10 Server - )[ !#-~]{1,190}$")  # Fenrir's names (core/thispc.py PREFIXES), plain ASCII, no "
PROGRAM = re.compile(r"^[A-Za-z]:\\[^\"*?<>|\r\n\t]{1,250}\.exe$", re.I)  # a program on a drive of this PC, never a share
PROFILES = ("any", "private", "public", "domain")
TOOLS = ("firewall-radmin", "firewall-public")  # the Minecraft world's own scripts in tools/, as 2.x starts them


def in_service():
    """This engine is Fenrir Server, the Windows service (app.py --service sets it before anything loads)."""
    return os.environ.get("FENRIR_SERVICE") == "1"


def openable(target):
    """An address of a shape Fenrir builds (a folder or a file is checked where it is opened, by being there)."""
    t = str(target or "")
    return any(p.match(t) for p in OPENABLE)


# ---------------------------------------------------------------------------------------------------- the firewall
def _name(name):
    n = str(name or "")
    if not RULE_NAME.fullmatch(n):
        raise ValueError(f"not a firewall rule name of Fenrir's: {n[:80]!r}")
    return n


def firewall_script(delete=(), add=()):
    """The PowerShell script behind one administrator prompt: rules deleted by name, then rules added, each checked here
    first. add: {"name", "action": allow|block, "profile", and "program", or "protocol" and "port"}. ValueError for anything
    else, so the shell never runs a request of another shape."""
    lines = ["$code = 0"]
    for name in delete:
        lines.append(f"& netsh advfirewall firewall delete rule \"name=$({ps_quote(_name(name))})\" | Out-Null")
    for r in add:
        if not isinstance(r, dict):
            raise ValueError("a rule to add is not of Fenrir's shape")
        name = _name(r.get("name"))
        action, profile = str(r.get("action") or "allow"), str(r.get("profile") or "any")
        if action not in ("allow", "block") or profile not in PROFILES:
            raise ValueError(f"a rule's action or profile is not one Fenrir uses: {action!r}, {profile!r}")
        if r.get("program") is not None:
            prog = str(r["program"])
            if not PROGRAM.fullmatch(prog):
                raise ValueError(f"not a program on this PC: {prog[:120]!r}")
            what = f"\"program=$({ps_quote(prog)})\""
        else:
            proto, port = str(r.get("protocol") or "TCP").upper(), r.get("port")
            if proto not in ("TCP", "UDP") or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"not a port Fenrir opens: {proto!r} {port!r}")
            what = f"protocol={proto} localport={port}"
        lines.append(f"& netsh advfirewall firewall add rule \"name=$({ps_quote(name)})\" dir=in action={action} {what} enable=yes "
                     f"profile={profile} | Out-Null; if ($LASTEXITCODE) {{ $code = $LASTEXITCODE }}")
    if len(lines) == 1:
        raise ValueError("no rule to delete or add")
    return "\n".join(lines + ["exit $code"])


def prompt_command(inner):
    """One administrator prompt for a script, which travels base64-encoded so that no name in it can change what it runs.
    Its exit code is the script's, or 1223 when the host said No: what core/games/hosting.py and the rest ran in 2.x."""
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    outer = (f"try {{ $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile','-EncodedCommand','{encoded}'; "
             "exit $p.ExitCode } catch { exit 1223 }")
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", outer]


def _run(args):
    return subprocess.run(args, capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW).returncode


def prompt(inner, run=None):
    return (run or _run)(prompt_command(inner))


def tools_command(repo, name, port=None):
    """One of the Minecraft world's own firewall scripts, elevated in a window of its own, as core/network.py and
    core/publicaccess.py start them: None when it is not in that tools folder."""
    script = Path(repo) / "tools" / f"{name}.ps1"
    if name not in TOOLS or not script.exists():
        return None
    args = ps_quote(f'-NoExit -ExecutionPolicy Bypass -File "{script}"' + (f" -Port {int(port)}" if port else ""))
    return ["powershell", "-NoProfile", "-Command", f"Start-Process powershell -Verb RunAs -ArgumentList {args}"]


# ---------------------------------------------------------------------------------------------------- the shell's side
def shell_firewall(cmd, run=None):
    """A firewall request the service sent, as the shell runs it: checked, then one prompt. Its exit code."""
    try:
        inner = firewall_script(cmd.get("delete") or [], cmd.get("add") or [])
    except (TypeError, ValueError) as e:
        print(f"shell: a firewall request of a shape Fenrir never sends: {e}")
        return BAD_REQUEST
    return prompt(inner, run)


def shell_elevate(cmd, repo, popen=None):
    """The shell's other administrator prompt: one of the world's firewall scripts, in a window of its own as 2.x started it,
    not waited for. 0 once asked, else why not. Fenrir Server's removal is never asked of a shell: the host's own Fenrir
    puts the servers back first (core/servicemode.py)."""
    script, port = cmd.get("script"), cmd.get("port")
    if script is not None:
        if script not in TOOLS or (port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535)):
            return BAD_REQUEST
        args = tools_command(repo, script, port)
        if args is None:
            return NOT_THERE
        (popen or (lambda a: subprocess.Popen(a, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)))(args)
        return 0
    return BAD_REQUEST


# ---------------------------------------------------------------------------------------------------- the service's side
def ask_shell(what, wait=PROMPT_WAIT, **data):
    """In the service: the shell on the host's desktop asked to show Windows' prompt. Its exit code, or NO_SHELL when no
    shell is attached or none answered."""
    try:
        from . import shellhub
        got = shellhub.ask(what, wait, **data)
    except Exception:  # noqa: BLE001
        return NO_SHELL
    return got if isinstance(got, int) and not isinstance(got, bool) else NO_SHELL


def firewall(delete=(), add=()):
    """Service mode's firewall prompt, through the shell. The request is checked here too, so a wrong one fails where it
    was made rather than in the shell."""
    delete, add = list(delete), [dict(r) for r in add]
    firewall_script(delete, add)
    return ask_shell("firewall", delete=delete, add=add)
