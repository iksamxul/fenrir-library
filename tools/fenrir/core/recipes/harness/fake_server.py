"""The fake server (recipes.md section 3.11.1): one program that plays any recipe's game, app or database server, so a
recipe's whole life runs on any PC in seconds, with no game, no Steam and no network beyond 127.0.0.1. The harness starts
it in place of the real program, with the real program's arguments, folder and environment, through the driver's own
popen seam, so everything around the program (the hidden console, Ctrl+C, the tree end, the log) is Fenrir's real code:

    python fake_server.py --play <play.json> --as <the real program> -- <the real program's arguments>

The play file (play.py writes it from the recipe and its tests.harness section) says everything already filled in: the
arguments to expect, the files and the keys the writers must have written, the lines to print and when, the ports to
open and what each answers, the players' schedule, how it may be stopped and what it saves. What happened goes to the
play's report, one JSON line per event, which the harness reads.

It checks its arguments and its files first (a difference is reported and it exits 2), prints its boot lines, then after
readyAfter seconds its ready lines and opens its ports. Players join and leave on schedule, on every port and in its
lines alike. It answers its console (stdin, or keys typed into its own console) and RCON, and on a stop it was told to
save on (Ctrl+C, a stop command, RCON stop, a REST call, a signal) it writes its save file and exits 0. It can misbehave on
purpose: crash, ignore every stop (slowStop, to prove the tree end), or never be ready (noReady, to prove unhealthy).

The standard library only, and nothing from the engine: it runs as a program of its own."""
import json
import os
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from . import wire
except ImportError:  # run as a program, beside wire.py
    import wire

EXIT_DIFFERS = 2  # the arguments or the files are not what the recipe's harness says


# ---------------------------------------------------------------------------------------------------- the files the writers wrote
def _text(path, encoding):
    return Path(path).read_text(encoding="utf-8-sig" if encoding == "utf-8-sig" else encoding or "utf-8", errors="replace")


def _flat(x, at=""):
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            out.update(_flat(v, f"{at}/{str(k).replace('~', '~0').replace('/', '~1')}"))
        return out
    if isinstance(x, list):
        out = {}
        for i, v in enumerate(x):
            out.update(_flat(v, f"{at}/{i}"))
        return out
    return {at or "/": x}


def read_keys(path, fmt, sep="=", encoding="utf-8"):
    """A config file's keys and values as its writer's format holds them: xml-property's <property name value>, ini's
    Section/key, properties' and keyvalue's key and value, json's pointers, lines' lines, and a hook's cfg lines
    ("set key value" or "key value", values as written)."""
    text = _text(path, encoding)
    if fmt == "xml-property":
        root = ET.fromstring(text)
        return {el.get("name"): el.get("value") for el in root.iter("property") if el.get("name") is not None}
    if fmt == "json":
        return _flat(json.loads(text or "null"))
    if fmt == "ini-tuple":  # Section/Key/Field for every field of every struct value
        out, section = {}, ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif "=(" in line and line.endswith(")"):
                key, _, value = line.partition("=")
                for part in _fields(value[1:-1]):
                    name, _, v = part.partition("=")
                    out[f"{section}/{key.strip()}/{name.strip()}"] = v.strip().strip('"')
        return out
    out, section = {}, ""
    for raw in text.splitlines():
        line = raw.strip()
        if fmt == "lines":
            if line:
                out[line] = True
            continue
        if not line or line[0] in "#;!" or line.startswith("//"):
            continue
        if fmt == "ini" and line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if fmt == "hook":
            parts = line.split(None, 1)
            if parts and parts[0].lower() in ("set", "sets", "setr") and len(parts) == 2:
                parts = parts[1].split(None, 1)
            if parts:
                out[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
            continue
        mark = sep if fmt == "keyvalue" else ("=" if "=" in line or fmt == "ini" else ":")
        if mark not in line:
            continue
        k, _, v = line.partition(mark)
        out[f"{section}/{k.strip()}" if fmt == "ini" and section else k.strip()] = v.strip()
    return out


def _fields(inner):
    """An Unreal struct's fields as written, split on the commas outside quotes and brackets."""
    out, depth, quoted, start = [], 0, False, 0
    for i, c in enumerate(inner + ","):
        if c == '"':
            quoted = not quoted
        elif not quoted and c == "(":
            depth += 1
        elif not quoted and c == ")":
            depth -= 1
        elif not quoted and depth == 0 and c == ",":
            if inner[start:i].strip():
                out.append(inner[start:i].strip())
            start = i + 1
    return out


def same(got, want):
    if got == want:
        return True
    if isinstance(got, (dict, list)) or isinstance(want, (dict, list)):
        return json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)
    as_text = (lambda v: "true" if v is True else "false" if v is False else "" if v is None else str(v))
    return as_text(got) == as_text(want)


# ---------------------------------------------------------------------------------------------------- the server
class Fake:
    def __init__(self, play, program, args):
        self.play, self.program, self.args = play, program, list(args)
        self.t0 = time.monotonic()
        self.live = wire.Live(**(play.get("a2s") or {}))
        self.live.on_command = self.command
        self.stop_how = None
        self.ports_stop = threading.Event()
        self.sockets = []
        self.ready = False
        self.lock = threading.Lock()

    def elapsed(self):
        return time.monotonic() - self.t0

    def report(self, event, **data):
        path = self.play.get("report")
        if not path:
            return
        line = json.dumps({"t": round(self.elapsed(), 3), "event": event, **data}, ensure_ascii=False)
        with self.lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def say(self, line):
        """A line where the real program would write it: its output, and the log files it is told to write to as well."""
        with self.lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
            for p in self.play.get("alsoWrite") or []:
                try:
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass

    # ---- what it was started with
    def check_args(self):
        want = self.play.get("expectArgs")
        name = Path(self.program.replace("\\", "/")).name
        expected = Path(str(self.play.get("program") or name).replace("\\", "/")).name
        same_program = name.lower() == expected.lower() if os.name == "nt" else name == expected
        self.report("started", program=self.program, args=self.args, cwd=os.getcwd(), sameProgram=same_program,
                    env={k: os.environ.get(k) for k in self.play.get("env") or []})
        if want is not None and self.args != want:
            self.report("args", ok=False, want=want, got=self.args)
            self.say("Fake server: the command line is not the one the recipe's harness expects")
            return False
        self.report("args", ok=True)
        return True

    def check_files(self):
        bad = []
        for f in self.play.get("expectFiles") or []:
            try:
                keys = read_keys(f["path"], f["format"], f.get("sep") or "=", f.get("encoding") or "utf-8")
            except (OSError, ValueError, ET.ParseError) as e:
                bad.append({"path": f["path"], "why": f"could not be read: {e}"})
                continue
            for k, v in (f.get("expect") or {}).items():
                if k not in keys:
                    bad.append({"path": f["path"], "key": k, "want": v, "why": "missing"})
                elif not same(keys[k], v):
                    bad.append({"path": f["path"], "key": k, "want": v, "got": keys[k], "why": "differs"})
        self.report("files", ok=not bad, bad=bad)
        if bad:
            self.say("Fake server: a config file is not what the recipe's harness expects")
        return not bad

    # ---- what it hears
    def command(self, via, line):
        """A line from its console, RCON or REST: a stop, a player command, chat from Fenrir, or something it just echoes."""
        line = str(line).strip()
        self.report("command", via=via, line=line)
        if line in (self.play.get("stopLines") or []):
            self.request_stop("rcon" if via == "rcon" else "rest" if via == "rest" else "command")
            return "Stopping the server"
        word, _, rest = line.partition(" ")
        if word.lower() == "list":
            names, _ = self.live.snapshot()
            return f"There are {len(names)} of a max of {self.live.max} players online: {', '.join(names)}"
        if word.lower() == "kick" and rest:
            self.leave(rest.split()[0], kicked=True)
            return f"Kicked {rest.split()[0]}"
        chat = self.play.get("chatSend")
        if chat:
            head, mark, tail = chat.partition("{text}")
            if mark and line.startswith(head) and line.endswith(tail) and len(line) >= len(head) + len(tail):
                text = line[len(head):len(line) - len(tail)] if tail else line[len(head):]
                self.report("chat", text=text)
                self.say(f"[Server] {text}")
                return ""
        self.say(f"> {line}")
        return f"Done: {line}"

    def request_stop(self, how):
        if self.play.get("slowStop"):
            self.report("stopIgnored", how=how)
            self.say("Fake server: told to stop, and not stopping (slow stop)")
            return
        if self.stop_how is None:
            self.stop_how = how

    def console(self):
        """Lines typed into it: its stdin, or for a console of keys its own console's input (what the helper types)."""
        how = self.play.get("console")
        if how not in ("stdin", "keys"):
            return
        try:
            stream = open("CONIN$", encoding="utf-8", errors="replace") if how == "keys" and os.name == "nt" else sys.stdin
        except OSError:
            stream = sys.stdin

        def loop():
            for line in stream:
                if line.strip():
                    self.command("keys" if how == "keys" else "stdin", line)
        threading.Thread(target=loop, daemon=True, name="console").start()

    def routes(self, method, path, body):
        """HTTP on its http ports: a REST stop, the players as JSON where the recipe reads them, health for anything else."""
        rest = self.play.get("http") or {}
        stop = rest.get("stop") or {}
        if stop and path.split("?")[0] == stop.get("path") and method == stop.get("method", "POST"):
            self.report("command", via="rest", line=f"{method} {path}")
            self.request_stop("rest")
            return 200, json.dumps({"ok": True}), "application/json"
        names, _ = self.live.snapshot()
        listing = [{"name": n} for n in names]
        where = [p for p in str((rest.get("players") or {}).get("path") or "players").replace(".", "/").split("/") if p]
        doc = listing
        for part in reversed(where):
            doc = {part: doc}
        self.report("http", method=method, path=path)
        return 200, json.dumps({"ok": True, **doc} if isinstance(doc, dict) else doc), "application/json"

    # ---- what it does
    def open_ports(self):
        for p in self.play.get("ports") or []:
            port, serve = int(p["port"]), p.get("serve", "tcp")
            try:
                if p["proto"] == "udp":
                    self.sockets.append(wire.serve_udp(port, self.live, self.ports_stop))
                elif serve == "rcon":
                    password = (self.play.get("rcon") or {}).get("password")
                    self.sockets.append(wire.serve_tcp(port, lambda c: wire.rcon_session(c, self.live, password), self.ports_stop))
                elif serve == "minecraft":
                    self.sockets.append(wire.serve_tcp(port, lambda c: wire.slp_session(c, self.live), self.ports_stop))
                elif serve == "http":
                    self.sockets.append(wire.serve_tcp(port, lambda c: wire.http_session(c, self.live, self.routes), self.ports_stop))
                else:
                    self.sockets.append(wire.serve_tcp(port, lambda c: None, self.ports_stop))
                self.report("port", port=port, proto=p["proto"], serve=serve)
            except OSError as e:
                self.report("port", port=port, proto=p["proto"], serve=serve, error=str(e))
                self.say(f"Fake server: port {port}/{p['proto']} is taken ({e})")

    def join(self, name):
        self.live.join(name)
        names, _ = self.live.snapshot()
        self.report("join", name=name, count=len(names))
        if self.play.get("joinLine"):
            self.say(self.play["joinLine"].replace("{name}", name))
        if self.play.get("countLine"):
            self.say(self.play["countLine"].replace("{n}", str(len(names))))

    def leave(self, name, kicked=False):
        names, _ = self.live.snapshot()
        if name not in names:
            return
        self.live.leave(name)
        names, _ = self.live.snapshot()
        self.report("leave", name=name, count=len(names), kicked=kicked)
        if self.play.get("leaveLine"):
            self.say(self.play["leaveLine"].replace("{name}", name))
        if self.play.get("countLine"):
            self.say(self.play["countLine"].replace("{n}", str(len(names))))

    def save(self):
        target = self.play.get("saveFile")
        if not target:
            return None
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"savedBy": "fake server", "at": time.time(), "players": self.live.snapshot()[0]}), "utf-8")
        return str(p)

    def finish(self):
        how = self.stop_how
        saved = self.save() if how in (self.play.get("saveOn") or []) else None
        self.say("Fake server: saving the world" if saved else "Fake server: stopping without saving")
        self.report("stopped", how=how, saved=saved)
        self.ports_stop.set()
        return 0

    def run(self):
        crash = self.play.get("crash") or {}
        if not self.check_args() or not self.check_files():
            return EXIT_DIFFERS
        for line in self.play.get("bootLines") or []:
            self.say(line)
        self.console()
        players = [dict(p) for p in self.play.get("players") or []]
        chatted = False
        stop_file = Path(self.play["stopFile"]) if self.play.get("stopFile") else None
        close_file = Path(self.play["closeFile"]) if self.play.get("closeFile") else None
        while True:
            now = self.elapsed()
            if stop_file is not None and stop_file.exists():  # a container's stop (fake_docker.py): the signal docker sends
                stop_file.unlink(missing_ok=True)
                self.request_stop("signal")
            if close_file is not None and close_file.exists():  # a recipe's "close" stop: asked to close (3.0)
                close_file.unlink(missing_ok=True)
                self.request_stop("close")
            if self.stop_how:
                return self.finish()
            if crash and now >= float(crash.get("after", 0)):
                self.say(crash.get("line") or "Fake server: crashed")
                self.report("crashed", code=int(crash.get("code", 1)))
                return int(crash.get("code", 1))
            if not self.ready and not self.play.get("noReady") and now >= float(self.play.get("readyAfter") or 0):
                self.ready = True
                self.open_ports()
                for line in self.play.get("readyLines") or []:
                    self.say(line.replace("{n}", str(len(self.live.snapshot()[0]))))
                self.report("ready")
            for p in players:
                if not p.get("_in") and not p.get("_out") and now >= float(p["at"]) and self.ready:
                    p["_in"] = True
                    self.join(p["name"])
                if p.get("_in") and p.get("leave") is not None and now >= float(p["leave"]):
                    p["_in"], p["_out"] = False, True
                    self.leave(p["name"])
            if not chatted and self.play.get("chatLine") and self.live.snapshot()[0]:
                chatted = True
                self.say(self.play["chatLine"].replace("{name}", self.live.snapshot()[0][0]))
            time.sleep(0.05)


def _ctrl_c_on():
    """Ctrl+C reaches this program even when it was started with it ignored: a console program normally gets it."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, 0)
        except (OSError, AttributeError):
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cut = argv.index("--") if "--" in argv else len(argv)
    own, rest = argv[:cut], argv[cut + 1:]
    opts = {own[i]: own[i + 1] for i in range(0, len(own) - 1, 2) if own[i].startswith("--")}
    if "--play" not in opts:
        sys.stderr.write("fake_server.py needs --play <play.json>\n")
        return EXIT_DIFFERS
    play = json.loads(Path(opts["--play"]).read_text("utf-8"))
    fake = Fake(play, opts.get("--as") or str(play.get("program") or ""), rest)
    if opts.get("--first-run"):  # a first-run step's program (initdb, an EULA tool): it ran, with these arguments
        fake.report("firstRun", program=fake.program, args=rest)
        fake.say(f"Fake first run: {Path(fake.program).name} {' '.join(rest)}")
        return 0
    _ctrl_c_on()
    signal.signal(signal.SIGINT, lambda *_: fake.request_stop("ctrl-c"))
    if hasattr(signal, "SIGTERM") and os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: fake.request_stop("signal"))
    try:
        return fake.run()
    except KeyboardInterrupt:  # a Ctrl+C that came before the handler was in place
        fake.request_stop("ctrl-c")
        return fake.finish() if fake.stop_how else 0


if __name__ == "__main__":
    sys.exit(main())
