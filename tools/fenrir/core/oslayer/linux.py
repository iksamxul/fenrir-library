"""The engine's platform layer on Linux (docs/v3-design/server.md §3.3.6), for Fenrir Server as a systemd service, in a
container, on a NAS or a free cloud machine:

    a process's identity   its pid and its start time: the boot's start plus its starttime (/proc/<pid>/stat). The boot's
                           start is read once per boot (its boot_id) and kept in DATA, because the kernel's btime moves
                           with the clock, so a pid the system gave another program after a crash or a reboot never
                           passes for the server, and a clock set by NTP (a Raspberry Pi has no clock of its own) never
                           makes the server look like another program
    a clean stop           SIGINT to the server's process group, as Ctrl+C is on Windows; servers start in a session of
                           their own (start_new_session), so the group is the server and what it started
    the whole tree         SIGKILL to the group
    a console of keys      the server runs on a pty (pty.openpty); a typed line is written to it; what it prints is read
                           from it into its log by a thread of this engine's
    how hard it works      /proc/<pid>/stat and statm, /proc/meminfo, /proc/stat, /proc/net/dev, /proc/<pid>/io
    the firewall           the fw helper (/usr/lib/fenrir-server/fw, tools/linux/fw in the source), the one thing the engine may
                           have run as root: open or close a port, or list Fenrir's own rules. Through its socket-activated
                           root service when there is one (/run/fenrir-fw.sock), else through one sudoers line
    systemd                sd_notify: READY=1 once the doors are open, WATCHDOG=1 while it lives, STOPPING=1 at its end
    paths                  /var/lib/fenrir for the system service (systemd's StateDirectory), $XDG_DATA_HOME/fenrir per user

Standard library only, and nothing of Fenrir's at load: core/util.py asks it where DATA is. Every root it reads (/proc,
the helper, the notify socket, the environment) is given to the object, so the tests point it at a folder of their own."""
import json
import os
import platform
import re
import signal
import socket
import subprocess
import threading
from pathlib import Path

FW = Path("/usr/lib/fenrir-server/fw")  # the package's helper; the sudoers line names exactly this path
FW_SOCKET = Path("/run/fenrir-fw.sock")  # its root service's socket (fenrir-fw.socket), for the fenrir group alone
SUDO = ("sudo", "-n")  # never a password prompt: the sudoers line lets the fenrir user run the helper, and nothing else
PROTOS = ("tcp", "udp")
NOT_ALLOWED = 126  # the helper could not be had as root (sudo said no, or the unit forbids new privileges)
MISSING = 127  # there is no helper here (a per-user run, a container)
_ptys = {}  # pid -> the pty's master side, while this engine holds it (a typed line goes there)
_lock = threading.Lock()


class LinuxLayer:
    name = "linux"

    def __init__(self, proc="/proc", env=None, machine=None, fw=None, run=None, uid=None, fw_socket=None, connect=None, anchor=None):
        self.proc = Path(proc)
        self.anchor = Path(anchor) if anchor else None  # where the boot's start is kept: DATA/boot-anchor.json unless given
        self._kept = {}
        self.env = os.environ if env is None else env
        self._machine = machine
        self.fw = Path(fw) if fw else FW
        self.fw_socket = Path(fw_socket) if fw_socket else FW_SOCKET
        self.run = run or (lambda args: subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=60, stdin=subprocess.DEVNULL))
        self.connect = connect or _ask_socket
        self._uid = uid

    # ---------------------------------------------------------------------------------------------------- identity
    def _stat(self, pid):
        """/proc/<pid>/stat's fields after the command's name (which may hold spaces and parentheses): field 3 is [0]."""
        try:
            text = (self.proc / str(int(pid)) / "stat").read_text("utf-8", "replace")
        except (OSError, ValueError):
            return None
        return text.rsplit(")", 1)[1].split() if ")" in text else None

    def ticks(self):
        try:
            return os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            return 100  # Linux's USER_HZ on every architecture Fenrir runs on

    def btime_now(self):
        """When this boot began as the kernel says now (/proc/stat's btime): it moves when the clock is set."""
        try:
            for line in (self.proc / "stat").read_text("utf-8").splitlines():
                if line.startswith("btime "):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return None

    def _anchor_file(self):
        if self.anchor is None:
            from ..util import DATA
            self.anchor = DATA / "boot-anchor.json"
        return self.anchor

    def btime(self):
        """When this boot began, unix seconds, as it was first read in this boot: the same figure for every engine that
        runs until the next boot, whatever the clock does meanwhile. Kept in DATA by the boot's id."""
        boot, now = self.boot_id(), self.btime_now()
        if now is None or not boot:
            return now
        if self._kept.get("boot") == boot:
            return self._kept["btime"]
        try:
            kept = json.loads(self._anchor_file().read_text("utf-8"))
        except (OSError, ValueError):
            kept = {}
        if not (isinstance(kept, dict) and kept.get("boot") == boot and isinstance(kept.get("btime"), int)):
            kept = {"boot": boot, "btime": now}
            try:
                tmp = self._anchor_file().with_name(f"boot-anchor.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(kept), "utf-8")
                os.replace(tmp, self._anchor_file())
            except OSError:
                pass  # a folder that cannot be written: this engine keeps it for as long as it runs
        self._kept = kept
        return kept["btime"]

    def boot_id(self):
        try:
            return (self.proc / "sys" / "kernel" / "random" / "boot_id").read_text("utf-8").strip()
        except OSError:
            return None

    def started(self, pid, wall=False):
        """When the process started, unix seconds, for telling processes apart (the boot's kept start); wall: by the clock
        as it is now, to compare with files' times. None when that cannot be read (it is gone)."""
        f, bt = self._stat(pid), self.btime_now() if wall else self.btime()
        if not f or bt is None:
            return None
        try:
            return bt + int(f[19]) / self.ticks()  # field 22, starttime: clock ticks after the boot
        except (IndexError, ValueError):
            return None

    def alive(self, pid):
        """It runs: there is such a process and it is no zombie (one that ended, its exit not yet read by its parent)."""
        if not pid:
            return False
        f = self._stat(pid)
        return bool(f) and f[0] not in ("Z", "X")

    def same(self, pid, born):
        now = self.started(pid)
        return born is not None and now is not None and self.alive(pid) and abs(now - float(born)) < 2

    def parents(self):
        """{pid: parent pid} of every process, from /proc."""
        out = {}
        try:
            names = [n for n in os.listdir(self.proc) if n.isdigit()]
        except OSError:
            return out
        for n in names:
            f = self._stat(n)
            if f:
                try:
                    out[int(n)] = int(f[1])
                except (IndexError, ValueError):
                    continue
        return out

    def tree(self, pid, parents=None):
        """pid and every process it started, and theirs: at most 64."""
        if not pid:
            return []
        kids = {}
        for p, pp in (parents if parents is not None else self.parents()).items():
            if p != pp:
                kids.setdefault(pp, []).append(p)
        out, todo = [], [int(pid)]
        while todo and len(out) < 64:
            p = todo.pop()
            if p in out:
                continue
            out.append(p)
            todo.extend(kids.get(p, []))
        return out

    def comm(self, pid):
        try:
            return (self.proc / str(int(pid)) / "comm").read_text("utf-8", "replace").strip().lower()
        except (OSError, ValueError):
            return None

    def children(self, pid):
        """The programs pid started that still run, by name (lower case); None when /proc would not say."""
        if not pid:
            return None
        ps = self.parents()
        if not ps:
            return None
        return [self.comm(p) or "" for p, pp in ps.items() if pp == int(pid) and p != int(pid) and self.alive(p)]

    def _sockets(self):
        """{inode: (port, state)} of the TCP sockets in /proc/net/tcp and tcp6 (state 0A: listening)."""
        out = {}
        for name in ("tcp", "tcp6"):
            try:
                lines = (self.proc / "net" / name).read_text("utf-8").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                f = line.split()
                try:
                    out[int(f[9])] = (int(f[1].rsplit(":", 1)[1], 16), f[3])
                except (ValueError, IndexError):
                    continue
        return out

    def listening_ports(self):
        """Every TCP port something listens on, or None when /proc would not say."""
        ports = {port for port, state in self._sockets().values() if state == "0A"}
        return ports or None

    def port_pid(self, port):
        """The process listening on a TCP port: its socket's inode, found among the processes' open files (this account's)."""
        inodes = {i for i, (p, state) in self._sockets().items() if p == int(port) and state == "0A"}
        if not inodes:
            return None
        for pid in sorted(self.parents()):
            try:
                fds = os.listdir(self.proc / str(pid) / "fd")
            except OSError:
                continue
            for fd in fds:
                try:
                    link = os.readlink(self.proc / str(pid) / "fd" / fd)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1].isdigit() and int(link[8:-1]) in inodes:
                    return pid
        return None

    def process_info(self, pid):
        """name, executable, command line, parent and start time of one process, as core/procs.py has them on Windows."""
        base = self.proc / str(int(pid))
        try:
            cmd = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmd = ""
        try:
            exe = os.readlink(base / "exe")
        except OSError:
            exe = ""
        f = self._stat(pid)
        return {"pid": int(pid), "ppid": int(f[1]) if f else 0, "name": self.comm(pid) or "", "exe": exe,
                "created": self.started(pid, wall=True) or 0.0, "cmd": cmd}  # compared with its log's time (core/serverproc.py)

    def find_processes(self, image, contains=None):
        """Processes of this program (2.x names it as Windows does, playit.exe: the .exe is left out here) whose command line
        holds `contains`, oldest first."""
        want = str(image).lower()
        base = want[:-4] if want.endswith(".exe") else want
        rows = []
        for pid in self.parents():
            info = self.process_info(pid)
            exe = os.path.basename(info["exe"]).lower()
            if info["name"] not in (base, base[:15]) and exe not in (base, want):  # the kernel keeps 15 characters of a name
                continue
            if contains and contains.lower() not in info["cmd"].lower():
                continue
            rows.append(info)
        return sorted(rows, key=lambda r: r["created"])

    def kill(self, pid):
        """That one process, never its group."""
        try:
            os.kill(int(pid), signal.SIGKILL)
            return True
        except (OSError, ValueError):
            return False

    def pids_named(self, names):
        names = {str(n).lower() for n in names}
        out = []
        for p in self.parents():
            c = self.comm(p)
            if c and (c in names or c + ".exe" in names):
                out.append(p)
        return out

    def lock(self, path):
        """One engine per data folder: this file locked with flock, which the system lets go the moment the process ends,
        however it ends. The open file while this process holds it; None while another does; False when no lock can be
        had here (the folder cannot be written)."""
        import fcntl
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            os.close(fd)
            return None

    # ---------------------------------------------------------------------------------------------------- signals
    def _group(self, pid, sig):
        """sig to the server's process group; to the process alone when it shares this engine's own group (it was not
        started in a session of its own: a tunnel, a site's cloudflared), so the engine never signals itself."""
        try:
            pid = int(pid)
            pgid = os.getpgid(pid)
            if pgid == os.getpgrp():
                os.kill(pid, sig)
            else:
                os.killpg(pgid, sig)
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def interrupt(self, pid):
        """A clean stop's Ctrl+C: SIGINT to the server's process group (it started in a session of its own)."""
        return self._group(pid, signal.SIGINT)

    def terminate(self, pid):
        return self._group(pid, signal.SIGTERM)

    def end_tree(self, pid):
        """The server and everything it started: SIGKILL to its group. One in this engine's own group goes with each process
        under it instead (/proc), never with the engine."""
        if not pid:
            return False
        try:
            shared = os.getpgid(int(pid)) == os.getpgrp()
        except (OSError, ValueError, AttributeError):
            return False
        if not shared:
            return self._group(pid, signal.SIGKILL)
        for p in self.tree(pid):
            if p != os.getpid():
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
        return True

    def signal(self, pid, name):
        sig = {"SIGINT": signal.SIGINT, "SIGTERM": signal.SIGTERM, "SIGHUP": getattr(signal, "SIGHUP", signal.SIGTERM)}.get(name)
        return self._group(pid, sig) if sig is not None else False

    def close(self, pid):
        """A recipe's "close" stop: on Linux, SIGTERM to the server's group (a Unity server saves and quits on it)."""
        return self._group(pid, signal.SIGTERM)

    # ---------------------------------------------------------------------------------------------------- starting
    pty_consoles = True  # a console of keys is a pty this engine holds

    def prepare_children(self):
        """Nothing to do: on Windows a console's Ctrl+C has to be turned on for the children to inherit."""

    def spawn_kwargs(self):
        """A session of its own: the server's process group is the server and what it starts, for its signals."""
        return {"start_new_session": True}

    def spawn_console(self, argv, cwd, env, log, popen=None, reader=None, openpty=None):
        """A server whose console takes key presses, on a pty: its input, output and errors are the pty's other side, so it
        sees a terminal; what it prints is copied into its log by a thread of this engine's. The pty stays this engine's
        for typed lines (write_console) until the server ends."""
        if openpty is None:
            import pty
            openpty = pty.openpty
        master, slave = openpty()
        try:
            p = (popen or subprocess.Popen)(argv, cwd=str(cwd), env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                            close_fds=True)
        except Exception:  # noqa: BLE001 - the pty goes with the start that failed, and the error is the caller's
            os.close(master)
            raise
        finally:
            os.close(slave)
        with _lock:
            _ptys[p.pid] = master
        (reader or _start_reader)(p, master, log)
        return p

    def write_console(self, pid, text):
        """A line typed into the server's pty, as key presses and Enter. False when this engine holds no pty of it (it
        started again since the server did)."""
        with _lock:
            fd = _ptys.get(int(pid or 0))
        if fd is None:
            return False
        try:
            os.write(fd, (str(text) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    # ---------------------------------------------------------------------------------------------------- how hard it works
    def memory(self):
        """{"totalGB", "freeGB", "loadPct"} from /proc/meminfo (MemAvailable is what can be had without swapping)."""
        info = {}
        try:
            for line in (self.proc / "meminfo").read_text("utf-8").splitlines():
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {"totalGB": None, "freeGB": None, "loadPct": None}
        total, free = info.get("MemTotal"), info.get("MemAvailable", info.get("MemFree"))
        if not total:
            return {"totalGB": None, "freeGB": None, "loadPct": None}
        return {"totalGB": round(total / 1024 ** 3, 1), "freeGB": round((free or 0) / 1024 ** 3, 1),
                "loadPct": int(round(100 * (1 - (free or 0) / total)))}

    def cpu_times(self):
        """(busy, all) processor seconds of the machine so far, from /proc/stat's cpu line."""
        try:
            first = (self.proc / "stat").read_text("utf-8").splitlines()[0].split()
            vals = [int(x) for x in first[1:]]
        except (OSError, ValueError, IndexError):
            return None
        if len(vals) < 4:
            return None
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle and iowait
        total = sum(vals[:8])  # user nice system idle iowait irq softirq steal (guest is inside user already)
        t = self.ticks()
        return (total - idle) / t, total / t

    def proc_times(self, pid):
        """(processor seconds, resident bytes) of one process, or None."""
        f = self._stat(pid)
        if not f:
            return None
        try:
            cpu = (int(f[11]) + int(f[12])) / self.ticks()  # utime and stime
            pages = int((self.proc / str(int(pid)) / "statm").read_text("utf-8").split()[1])
            return cpu, pages * _page_size()
        except (OSError, ValueError, IndexError):
            return None

    def io_bytes(self, pid):
        """(bytes read, bytes written) by one process so far (/proc/<pid>/io's rchar and wchar, the network's with the
        disk's, as Windows counts them); None when it is another user's."""
        try:
            vals = dict(line.split(":", 1) for line in (self.proc / str(int(pid)) / "io").read_text("utf-8").splitlines() if ":" in line)
            return int(vals["rchar"]), int(vals["wchar"])
        except (OSError, ValueError, KeyError):
            return None

    def net_bytes(self):
        """(bytes in, bytes out) so far over the network interfaces, the loopback and virtual bridges left out."""
        try:
            lines = (self.proc / "net" / "dev").read_text("utf-8").splitlines()[2:]
        except OSError:
            return None
        got = sent = 0
        for line in lines:
            name, _, rest = line.partition(":")
            name = name.strip()
            if not rest or name == "lo" or name.startswith(("docker", "veth", "br-", "virbr", "tailscale", "wg", "tun")):
                continue
            f = rest.split()
            try:
                got += int(f[0])
                sent += int(f[8])
            except (IndexError, ValueError):
                continue
        return got, sent

    # ---------------------------------------------------------------------------------------------------- the machine
    def machine(self):
        return (self._machine or platform.machine() or "").lower()

    def arch(self):
        """"x64" or "arm64", as recipes name processors; "x86" or "arm" for the 32-bit ones Fenrir does not serve."""
        m = self.machine()
        if m in ("x86_64", "amd64"):
            return "x64"
        if m in ("aarch64", "arm64"):
            return "arm64"
        if re.fullmatch(r"i[3-6]86|x86", m):
            return "x86"
        return "arm" if m.startswith("arm") else m or "unknown"

    def data_home(self, root=None, frozen=False):
        """Where DATA goes when FENRIR_DATA does not say: systemd's StateDirectory for the system service (/var/lib/fenrir),
        else, for a package's code that cannot be written, the user's own ($XDG_DATA_HOME/fenrir), else the source
        checkout itself, as on Windows from source."""
        state = self.env.get("STATE_DIRECTORY")
        if state:
            return Path(state.split(":")[0])
        if root is not None and os.access(str(root), os.W_OK):
            return Path(root)
        xdg = self.env.get("XDG_DATA_HOME") or str(Path(self.env.get("HOME") or Path.home()) / ".local" / "share")
        return Path(xdg) / "fenrir"

    # ---------------------------------------------------------------------------------------------------- systemd
    def notify(self, *states, send=None):
        """sd_notify: each state ("READY=1", "STOPPING=1", "WATCHDOG=1", "STATUS=...") to systemd's socket, as one datagram.
        Nothing when systemd did not start this engine (no NOTIFY_SOCKET). True when it was sent."""
        addr = self.env.get("NOTIFY_SOCKET")
        if not addr or not states:
            return False
        if addr.startswith("@"):
            addr = "\0" + addr[1:]  # an abstract socket
        data = "\n".join(states).encode("utf-8")
        if send is not None:
            send(addr, data)
            return True
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(addr)
                s.sendall(data)
            return True
        except (OSError, AttributeError):
            return False

    def watchdog_every(self):
        """How often systemd wants WATCHDOG=1 (half its WatchdogSec), in seconds; None without one."""
        usec = self.env.get("WATCHDOG_USEC")
        try:
            return int(usec) / 2e6 if usec and int(usec) > 0 else None
        except ValueError:
            return None

    # ---------------------------------------------------------------------------------------------------- the firewall
    def firewall_ready(self):
        return self.fw_socket.exists() or self.fw.is_file()

    def no_new_privileges(self):
        """systemd's NoNewPrivileges is on for this process: sudo cannot become root, so only the helper's socket can."""
        try:
            for line in (self.proc / "self" / "status").read_text("utf-8").splitlines():
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
        except OSError:
            pass
        return False

    def firewall(self, do, port=None, proto=None):
        """The fw helper: ("open" | "close", port, "tcp" | "udp"), or ("list",). (code, its output): the helper's own code
        (0 done, 2 refused, 3 no firewall is active), NOT_ALLOWED or MISSING. Its arguments are checked here as the helper
        checks them again, so nothing else ever reaches root. Its root service's socket first: the engine's unit keeps
        NoNewPrivileges and a read-only /etc then, which sudo and ufw could not work under."""
        if do not in ("open", "close", "list"):
            raise ValueError("The firewall helper opens, closes or lists, and nothing else.")
        words = [do]
        if do != "list":
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or proto not in PROTOS:
                raise ValueError(f"Not a port the firewall helper takes: {port!r} {proto!r}")
            words += [str(port), proto]
        if self.fw_socket.exists():
            try:
                code, _, out = self.connect(str(self.fw_socket), (" ".join(words) + "\n").encode("ascii")).decode("utf-8", "replace").partition("\n")
                return int(code), out
            except (OSError, ValueError):
                return NOT_ALLOWED, "The firewall helper's service did not answer."
        if not self.fw.is_file():
            return MISSING, "Fenrir's firewall helper is not installed here."
        if self.no_new_privileges():
            return NOT_ALLOWED, "This service may not gain new privileges (NoNewPrivileges), so sudo cannot run the helper."
        r = self.run([*SUDO, str(self.fw), *words])
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 1 and re.search(r"^sudo: ", out, re.M):  # no sudoers line for it, or a password asked for
            return NOT_ALLOWED, out
        return r.returncode, out


def _ask_socket(path, line, timeout=90):
    """One request to the helper's root service: the line out, its whole answer back (the service ends the connection)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(line)
        s.shutdown(socket.SHUT_WR)
        got = b""
        while len(got) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
        return got


def _page_size():
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 4096


def _start_reader(p, master, log):
    """What the server prints, from its pty into its log, until it ends; then the pty goes."""
    def copy():
        try:
            with open(log, "ab", buffering=0) as out:
                while True:
                    try:
                        data = os.read(master, 65536)
                    except OSError:  # EIO: the server's side closed (it ended)
                        break
                    if not data:
                        break
                    out.write(data)
        finally:
            with _lock:
                if _ptys.get(p.pid) == master:
                    _ptys.pop(p.pid, None)
            try:
                os.close(master)
            except OSError:
                pass
    threading.Thread(target=copy, name=f"pty {p.pid}", daemon=True).start()
