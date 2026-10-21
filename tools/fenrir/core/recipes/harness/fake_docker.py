"""The fake docker command (recipes.md section 3.11.2), first on PATH for a run of an image recipe: version, pull, run,
start, stop, inspect, logs, ps, rm and volume, with its state in a JSON file. A container is the fake server, started
detached from the play file the harness names, so the Docker driver (REC-9) runs as it does with the real thing: only
docker is fake.

    FENRIR_FAKE_DOCKER=<state.json> FENRIR_FAKE_DOCKER_PLAY=<play.json> docker run -d --name kuma -p 3001:3001 louislam/uptime-kuma:1

shim(folder) writes docker.cmd (Windows) and docker (Linux and macOS) there: put the folder first on PATH, and find
docker with shutil.which, as Windows starts a .cmd only by its full name. A stop asks the container to stop through a
file it watches (the signal docker would send), waits its -t seconds, then ends it. The standard library only."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    from . import tool
except ImportError:  # run as a program (the docker shim), beside tool.py
    import tool

FAKE = Path(__file__).with_name("fake_server.py")
DETACHED = (0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0  # no console of its own, its own group, no window


def _path():
    p = os.environ.get("FENRIR_FAKE_DOCKER")
    if not p:
        sys.stderr.write("fake docker: FENRIR_FAKE_DOCKER names no state file\n")
        sys.exit(125)
    return Path(p)


def load():
    try:
        return json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {"images": [], "containers": {}, "volumes": []}


def save(state):
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), "utf-8")
    os.replace(tmp, _path())


def _alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _status(c):
    if c.get("pid") and _alive(c["pid"]):
        return "running"
    return "exited" if c.get("started") else "created"


def _launch(c, folder):
    play = json.loads(Path(c["play"]).read_text("utf-8"))
    play["stopFile"] = str(folder / f"{c['id']}.stop")
    play["report"] = str(folder / f"{c['id']}.jsonl")
    own = folder / f"{c['id']}.play.json"
    own.write_text(json.dumps(play), "utf-8")
    log = open(folder / f"{c['id']}.log", "ab")
    kw = {"creationflags": DETACHED} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(tool.command("fake-server", "--play", own, "--as", c["image"], "--", *c["cmd"]),
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={**os.environ, **c["env"]}, **kw)
    log.close()
    c.update(pid=p.pid, started=time.time())


def _end(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(int(pid), 9)
        except OSError:
            pass


def _port_map(spec):
    """-p's [ip:]host:container[/proto] as inspect writes it: "3001/tcp": [{"HostIp", "HostPort"}]."""
    spec, _, proto = spec.partition("/")
    parts = spec.split(":")
    container, host = parts[-1], parts[-2] if len(parts) > 1 else parts[-1]
    ip = parts[-3] if len(parts) > 2 else "0.0.0.0"
    return f"{container}/{proto or 'tcp'}", [{"HostIp": ip, "HostPort": host}]


def _find(state, name):
    for cid, c in state["containers"].items():
        if name in (cid, cid[:12], c["name"]):
            return c
    return None


def _parse_run(args):
    opts = {"ports": [], "env": {}, "volumes": [], "name": None, "detach": False, "restart": "no"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        val = args[i + 1] if i + 1 < len(args) else ""
        if a in ("-d", "--detach"):
            opts["detach"], i = True, i + 1
            continue
        if a == "--rm":
            i += 1
            continue
        if a in ("-p", "--publish"):
            opts["ports"].append(val)
        elif a in ("-e", "--env"):
            k, _, v = val.partition("=")
            opts["env"][k] = v
        elif a in ("-v", "--volume"):
            opts["volumes"].append(val)
        elif a == "--name":
            opts["name"] = val
        elif a == "--restart":
            opts["restart"] = val
        i += 2
    return opts, args[i] if i < len(args) else "", args[i + 1:]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: docker COMMAND (Fenrir's fake docker)")
        return 0
    cmd, args = argv[0], argv[1:]
    folder = _path().parent
    state = load()
    if cmd == "version":
        print(json.dumps({"Client": {"Version": "27.0.0-fake"}, "Server": {"Version": "27.0.0-fake"}}) if "--format" in args else "Docker version 27.0.0-fake")
        return 0
    if cmd == "pull":
        image = args[-1] if args else ""
        if image and image not in state["images"]:
            state["images"].append(image)
        save(state)
        print(f"Status: Downloaded newer image for {image}")
        return 0
    if cmd == "run":
        opts, image, rest = _parse_run(args)
        play = os.environ.get("FENRIR_FAKE_DOCKER_PLAY")
        if not play:
            sys.stderr.write("fake docker: FENRIR_FAKE_DOCKER_PLAY names no play file\n")
            return 125
        if opts["name"] and _find(state, opts["name"]):
            sys.stderr.write(f'docker: Error response from daemon: Conflict. The container name "/{opts["name"]}" is already in use.\n')
            return 125
        cid = uuid.uuid4().hex + uuid.uuid4().hex
        c = {"id": cid, "name": opts["name"] or f"fake_{cid[:6]}", "image": image, "cmd": rest, "ports": opts["ports"], "env": opts["env"],
             "volumes": opts["volumes"], "restart": opts["restart"], "play": play, "created": time.time()}
        for v in opts["volumes"]:
            name = v.split(":", 1)[0]
            if name and "/" not in name and "\\" not in name and name not in state["volumes"]:
                state["volumes"].append(name)
        _launch(c, folder)
        state["containers"][cid] = c
        save(state)
        print(cid)
        return 0
    if cmd in ("start", "stop", "rm", "inspect", "logs"):
        names, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-t", "--time", "--tail", "-n", "--format", "-f" if cmd == "inspect" else ""):
                skip = True
            elif not a.startswith("-"):
                names.append(a)
        found = [(_find(state, n), n) for n in names]
        missing = [n for c, n in found if c is None]
        if missing:
            sys.stderr.write(f"Error response from daemon: No such container: {missing[0]}\n")
            return 1
        out = []
        for c, _ in found:
            if cmd == "start" and _status(c) != "running":
                _launch(c, folder)
            elif cmd == "stop" and _status(c) == "running":
                wait = int(args[args.index("-t") + 1]) if "-t" in args else 10
                (folder / f"{c['id']}.stop").write_text("stop", "utf-8")
                end = time.time() + wait
                while time.time() < end and _alive(c["pid"]):
                    time.sleep(0.1)
                if _alive(c["pid"]):
                    _end(c["pid"])
                    c["killed"] = True
            elif cmd == "rm":
                if _status(c) == "running":
                    if "-f" not in args and "--force" not in args:
                        sys.stderr.write("Error response from daemon: You cannot remove a running container. Stop it first.\n")
                        return 1
                    _end(c["pid"])
                state["containers"].pop(c["id"], None)
            elif cmd == "inspect":
                status = _status(c)
                out.append({"Id": c["id"], "Name": "/" + c["name"], "Created": c["created"],
                            "State": {"Status": status, "Running": status == "running", "Pid": c.get("pid") if status == "running" else 0,
                                      "ExitCode": 0 if status != "exited" or not c.get("killed") else 137},
                            "Config": {"Image": c["image"], "Env": [f"{k}={v}" for k, v in c["env"].items()]},
                            "HostConfig": {"RestartPolicy": {"Name": c["restart"]}},
                            "NetworkSettings": {"Ports": dict(_port_map(p) for p in c["ports"])},
                            "Mounts": [{"Name": v.split(":", 1)[0], "Destination": v.split(":", 1)[-1]} for v in c["volumes"]]})
            elif cmd == "logs":
                try:
                    text = (folder / f"{c['id']}.log").read_text("utf-8", errors="replace")
                except OSError:
                    text = ""
                lines = text.splitlines()
                if "--tail" in args:
                    lines = lines[-int(args[args.index("--tail") + 1]):]
                out.append("\n".join(lines))
            if cmd in ("start", "stop", "rm"):
                print(c["name"])
        save(state)
        if cmd == "inspect":
            print(json.dumps(out, indent=1))
        elif cmd == "logs":
            print("\n".join(out))
        return 0
    if cmd == "ps":
        everyone = "-a" in args or "--all" in args
        rows = [{"ID": cid[:12], "Names": c["name"], "Image": c["image"], "State": _status(c), "Ports": ", ".join(c["ports"])}
                for cid, c in state["containers"].items() if everyone or _status(c) == "running"]
        if "--format" in args:
            print("\n".join(json.dumps(r) for r in rows))
        else:
            print("CONTAINER ID   IMAGE   NAMES   STATE")
            for r in rows:
                print(f"{r['ID']}   {r['Image']}   {r['Names']}   {r['State']}")
        return 0
    if cmd == "volume":
        sub, rest = (args[0] if args else ""), args[1:]
        if sub == "create" and rest:
            if rest[-1] not in state["volumes"]:
                state["volumes"].append(rest[-1])
            print(rest[-1])
        elif sub == "ls":
            print("\n".join(state["volumes"]) if "-q" in rest else "DRIVER    VOLUME NAME\n" + "\n".join(f"local     {v}" for v in state["volumes"]))
        elif sub == "rm":
            for v in rest:
                if v in state["volumes"]:
                    state["volumes"].remove(v)
                    print(v)
        elif sub == "inspect":
            print(json.dumps([{"Name": v, "Driver": "local"} for v in rest if v in state["volumes"]]))
        save(state)
        return 0
    sys.stderr.write(f"docker: '{cmd}' is not a docker command this fake plays.\n")
    return 1


def shim(folder):
    """A docker command in folder that runs this fake: put folder first on PATH."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    line = " ".join(f'"{x}"' if not x.startswith("--") else x for x in tool.command("fake-docker"))
    (folder / "docker.cmd").write_text(f"@{line} %*\r\n", "utf-8")
    sh = folder / "docker"
    sh.write_text(f'#!/bin/sh\nexec {line} "$@"\n', "utf-8")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    return folder


if __name__ == "__main__":
    sys.exit(main())
