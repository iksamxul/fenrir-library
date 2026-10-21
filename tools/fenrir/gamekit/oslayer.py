"""The one OS layer for the game kit and Fenrir Connect (3.0, PLT-1; docs/v3-design/platforms.md §3.1): paths, processes,
memory, directory links, case-insensitive joins, file versions, hiding and opening, each for Windows, Linux and macOS.

The implementation is chosen once, at import (IMPL). The Windows one is the code the kit and Connect used before, moved
here without a change, so Windows behaves exactly as it did in 2.9. The Linux and macOS ones read what those systems
keep: /proc, the XDG folders, ps and sysctl. Every program they start gets an argument list, never a shell. Each
implementation is a class whose roots (/proc, the home folder, the environment) a test can point at fixtures.

Standard library only, and nothing of either app, like the rest of the kit."""
import os
import posixpath
import re
import signal
import struct
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, as the kit starts games
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a junction
HIDDEN = 0x2  # FILE_ATTRIBUTE_HIDDEN
QUIET = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
# Windows' known folders a game keeps its per-user files in, and where each is inside a Proton prefix (platforms.md §3.3)
KNOWN = {"LOCALAPPDATA": "AppData/Local", "LOCALLOW": "AppData/LocalLow", "APPDATA": "AppData/Roaming", "DOCUMENTS": "Documents",
         "SAVED_GAMES": "Saved Games", "USERPROFILE": ""}
PREFIX_USER = "drive_c/users/steamuser"  # Proton's own user in every prefix


class CaseConflict(OSError):
    """Two entries in one folder differ only in case, so a name that ignores case names both: nothing is placed there."""

    def __init__(self, folder, names):
        self.folder, self.names = Path(folder), sorted(names)
        super().__init__(f"{self.folder} holds {' and '.join(self.names)}, which differ only in case")


# ---------------------------------------------------------------------------------------------------- case (every system)
def case_matters(folder):
    """Whether names in this folder differ by case: Linux's file systems tell BepInEx/Plugins from BepInEx/plugins, Windows'
    and macOS's usually do not. Checked, never assumed: a folder on a FAT or exFAT card (the Steam Deck's microSD) or on a
    case-insensitive APFS ignores case, and Windows can make a folder tell case apart. Read only: one entry of the folder
    is looked up again in the other case."""
    folder = Path(folder)
    try:
        with os.scandir(folder) as it:
            for entry in it:
                name = entry.name
                other = name.swapcase()
                if other == name:
                    continue
                try:
                    return not os.path.samefile(folder / name, folder / other)
                except FileNotFoundError:
                    return True  # the other case is not there: this folder's names differ by case
                except OSError:
                    continue  # this one cannot be looked at (in use, no access): another entry says it
    except OSError:
        pass
    return not (os.name == "nt" or sys.platform == "darwin")  # nothing to look at: what this system's own disks do


def ci_join(root, rel, sensitive=None):
    """root / rel, each part matched without case against what is there, as Windows and Wine find it (platforms.md §3.4).
    An existing entry of the same name in another case is used; a part that is not there is made as rel writes it; two
    entries differing only in case raise CaseConflict. Where the folder ignores case anyway (Windows, as in 2.9) this is
    root / rel and nothing is looked at. It never leaves root: .. is refused, and no link is followed out of it.
    sensitive: case_matters(root) when the caller already knows it."""
    root = Path(root)
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"{rel} reaches outside {root}")
    if not (case_matters(root) if sensitive is None else sensitive):
        return root / str(rel)
    top = os.path.realpath(root)
    cur = root
    for i, part in enumerate(parts):
        try:
            names = [n for n in os.listdir(cur) if n.lower() == part.lower()]
        except (FileNotFoundError, NotADirectoryError):
            return cur.joinpath(*parts[i:])  # nothing of the rest is there yet: made as written
        if len(names) > 1:
            raise CaseConflict(cur, names)
        cur = cur / (names[0] if names else part)
        if names and (os.path.islink(cur) or getattr(os.path, "isjunction", lambda _p: False)(cur)):  # a junction is a link too
            real = os.path.realpath(cur)
            if real != top and not real.startswith(top.rstrip(os.sep) + os.sep):
                raise ValueError(f"{cur} is a link out of {root}")
        if not names:
            return cur.joinpath(*parts[i + 1:])
    return cur


# ---------------------------------------------------------------------------------------------------- file versions
def pe_version(path):
    """The x.y.z.w file version in a Windows program's version resource (VS_FIXEDFILEINFO), read from the file itself:
    the answer version.dll gives, on any system, for a game that runs through Proton. None when there is none. Only the
    headers and the version resource are read, wherever they sit in a game's exe of a hundred megabytes or more."""
    try:
        with open(path, "rb") as f:
            return _pe_version(f)
    except (OSError, struct.error, IndexError, ValueError):
        return None


def _pe_version(f):
    def read(off, n):
        f.seek(off)
        b = f.read(n)
        if len(b) != n:
            raise ValueError("the file ends early")
        return b

    u16 = lambda off: struct.unpack("<H", read(off, 2))[0]  # noqa: E731
    u32 = lambda off: struct.unpack("<I", read(off, 4))[0]  # noqa: E731
    if read(0, 2) != b"MZ":
        return None
    pe = u32(0x3C)
    if read(pe, 4) != b"PE\0\0":
        return None
    sections, opt_size = u16(pe + 6), u16(pe + 20)
    opt = pe + 24
    magic = u16(opt)
    if magic not in (0x10B, 0x20B):
        return None
    dirs = opt + (96 if magic == 0x10B else 112)
    if u32(dirs - 4) < 3:  # NumberOfRvaAndSizes: the resource directory is the third
        return None
    res_rva = u32(dirs + 16)
    if not res_rva or sections > 96:
        return None
    spans = []
    for i in range(sections):
        s = opt + opt_size + 40 * i
        vsize, va, raw_size, raw = struct.unpack("<IIII", read(s + 8, 16))
        spans.append((va, max(vsize, raw_size), raw))

    def at(rva):
        for va, size, raw in spans:
            if va <= rva < va + size:
                return raw + rva - va
        raise ValueError("an address outside every section")

    base = at(res_rva)

    def first(off, want=None):
        named, ids = struct.unpack("<HH", read(off + 12, 4))
        for i in range(min(named + ids, 4096)):
            name, child = struct.unpack("<II", read(off + 16 + 8 * i, 8))
            if want is None or (not name & 0x80000000 and name == want):
                return child
        return None

    level = first(base, 16)  # RT_VERSION
    for _ in range(2):  # its name, then its language
        if level is None or not level & 0x80000000:
            return None
        level = first(base + (level & 0x7FFFFFFF))
    if level is None or level & 0x80000000:
        return None
    rva, size = struct.unpack("<II", read(base + level, 8))
    blob = read(at(rva), min(size, 1 << 20))
    i = blob.find(b"\xbd\x04\xef\xfe")  # VS_FIXEDFILEINFO's signature, 0xFEEF04BD
    if i < 0 or len(blob) < i + 16:
        return None
    ms, ls = struct.unpack_from("<II", blob, i + 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


# ---------------------------------------------------------------------------------------------------- the systems
class Posix:
    """What Linux and macOS share."""
    name = "posix"

    def __init__(self, home=None, env=None, run=None):
        self.home = Path(home) if home is not None else Path.home()
        self.env = env if env is not None else os.environ
        self.run = run or self._run

    @staticmethod
    def _run(argv, timeout=10):
        """What a program printed, or None when it is not there, failed or took too long. An argument list, never a shell."""
        try:
            r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def hide(self, path):
        """Nothing to do: a name that starts with a dot is hidden already (.fenrir-originals)."""

    def dir_link(self, link, target):
        os.symlink(str(target), str(link), target_is_directory=True)

    def is_dir_link(self, path):
        return os.path.islink(path)

    def remove_dir_link(self, path):
        """The link itself goes; the folder it points to stays. A symlink is unlinked: rmdir refuses one."""
        if not os.path.islink(path):
            raise OSError(f"Not a link: {path}")
        os.unlink(path)

    def link_target(self, path):
        try:
            return Path(os.readlink(path))
        except OSError:
            return None

    def end(self, pid):
        try:
            os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))  # as taskkill /F: the program cannot refuse
            return True
        except (OSError, ValueError):
            return False

    def end_named(self, name):
        """Every process of this name ended, as taskkill /IM does; False when the list could not be read."""
        procs = self.find_procs([name])
        for p in procs or []:
            self.end(p["pid"])
        return procs is not None

    def spawn_detached(self, argv, cwd=None):
        return subprocess.Popen([str(a) for a in argv], cwd=str(cwd) if cwd else None, start_new_session=True, close_fds=True, **QUIET)

    def file_version(self, exe):
        return pe_version(exe)


def _abs(p):
    """Whether a folder an XDG setting names is absolute (one that is not is ignored, as the XDG spec says); POSIX's rule,
    and the host's too, so the tests can give fixture folders of this PC."""
    return posixpath.isabs(p) or os.path.isabs(p)


class Linux(Posix):
    name = "linux"

    def __init__(self, home=None, env=None, run=None, proc="/proc"):
        super().__init__(home, env, run)
        self.proc = Path(proc)

    def _xdg(self, var, default):
        v = self.env.get(var)
        return Path(v) if v and _abs(v) else self.home / default

    def data_home(self, app):
        return self._xdg("XDG_DATA_HOME", ".local/share") / app

    def config_home(self, app):
        return self._xdg("XDG_CONFIG_HOME", ".config") / app

    def cache_home(self, app):
        return self._xdg("XDG_CACHE_HOME", ".cache") / app

    def _user_dir(self, which, default):
        """A folder as the desktop names it: xdg-user-dir, else ~/.config/user-dirs.dirs, else the English default."""
        out = (self.run(["xdg-user-dir", which]) or "").strip()
        if out and _abs(out) and Path(out) != self.home:
            return Path(out)
        try:
            text = (self._xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs").read_text("utf-8", "replace")
            m = re.search(rf'^XDG_{which}_DIR="([^"]*)"', text, re.M)
            if m:
                p = m.group(1).replace("$HOME", str(self.home))
                if _abs(p) and Path(p) != self.home:
                    return Path(p)
        except OSError:
            pass
        return self.home / default

    def downloads_dir(self):
        return self._user_dir("DOWNLOAD", "Downloads")

    def desktop_dir(self):
        return self._user_dir("DESKTOP", "Desktop")

    def _procs(self):
        """(pid, comm, argv) for every process /proc shows; None when /proc cannot be read."""
        try:
            pids = [p for p in os.listdir(self.proc) if p.isdigit()]
        except OSError:
            return None
        out = []
        for pid in pids:
            try:
                comm = (self.proc / pid / "comm").read_text("utf-8", "replace").strip()
                argv = [a for a in (self.proc / pid / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
            except OSError:
                continue  # it ended meanwhile, or belongs to someone else
            out.append((int(pid), comm, argv))
        return out

    @staticmethod
    def _names(comm, argv):
        """A process's names, lower case: its own, and the Windows program a Wine or Proton process runs (argv[0] ends in .exe)."""
        names = {comm.lower()} if comm else set()
        if argv:
            first = re.split(r"[\\/]", argv[0])[-1].lower()
            if first:
                names.add(first)
        return names

    def running_names(self):
        procs = self._procs()
        if procs is None:
            return None
        names = set()
        for _pid, comm, argv in procs:
            names |= self._names(comm, argv)
        return names or None

    def find_procs(self, names, contains=None, timeout=30):
        want = {n.lower() for n in names}
        procs = self._procs()
        if procs is None:
            return None
        return [{"pid": pid, "name": next(iter(sorted(self._names(comm, argv) & want))), "cmd": " ".join(argv)}
                for pid, comm, argv in procs if self._names(comm, argv) & want and (contains is None or contains in " ".join(argv))]

    def memory(self):
        try:
            text = (self.proc / "meminfo").read_text("utf-8", "replace")
        except OSError:
            return None
        kb = {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M)}
        if "MemTotal" not in kb:
            return None
        return {"total": kb["MemTotal"] * 1024, "available": kb.get("MemAvailable", kb.get("MemFree", 0)) * 1024}

    def open_uri(self, uri, strict=False):
        try:
            subprocess.Popen(["xdg-open", str(uri)], start_new_session=True, close_fds=True, **QUIET)
            return True
        except OSError:
            if strict:
                raise
            return False

    open_path = open_uri

    STEAM_HOMES = (".steam/steam", ".steam/debian-installation", ".local/share/Steam",  # native Steam, and the Steam Deck's
                   ".var/app/com.valvesoftware.Steam/data/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
                   "snap/steam/common/.local/share/Steam")  # Snap

    def steam_roots(self):
        """Every Steam on this machine, each once (~/.steam/steam is usually a link to ~/.local/share/Steam)."""
        out, seen = [], set()
        for rel in self.STEAM_HOMES:
            root = self.home / rel
            try:
                if not (root / "steamapps").is_dir():
                    continue
                real = os.path.realpath(root)
            except OSError:
                continue
            if real not in seen:
                seen.add(real)
                out.append(root)
        return out

    def user_dir(self, known, prefix=None):
        """A Windows game's known folder inside its Proton prefix; None without a prefix (Steam makes one at the first start)."""
        if prefix is None or known not in KNOWN:
            return None
        return Path(prefix) / PREFIX_USER / KNOWN[known] if KNOWN[known] else Path(prefix) / PREFIX_USER


class Mac(Posix):
    name = "mac"

    def data_home(self, app):
        return self.home / "Library" / "Application Support" / app

    def config_home(self, app):
        return self.home / "Library" / "Preferences" / app

    def cache_home(self, app):
        return self.home / "Library" / "Caches" / app

    def downloads_dir(self):
        return self.home / "Downloads"

    def desktop_dir(self):
        return self.home / "Desktop"

    def _ps(self, field):
        """{pid: the field} from ps, which puts the field last on each line so a path with spaces stays whole."""
        out = self.run(["ps", "-axww", "-o", "pid=", "-o", f"{field}="])
        if out is None:
            return None
        rows = {}
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(.*)$", line)
            if m:
                rows[int(m.group(1))] = m.group(2).strip()
        return rows

    def running_names(self):
        rows = self._ps("comm")
        if rows is None:
            return None
        return {v.rsplit("/", 1)[-1].lower() for v in rows.values() if v} or None

    def find_procs(self, names, contains=None, timeout=30):
        want = {n.lower() for n in names}
        comm, cmd = self._ps("comm"), self._ps("command") or {}
        if comm is None:
            return None
        out = []
        for pid, path in comm.items():
            name = path.rsplit("/", 1)[-1].lower()
            line = cmd.get(pid, "")
            if name in want and (contains is None or contains in line):
                out.append({"pid": pid, "name": name, "cmd": line})
        return out

    def memory(self):
        total = (self.run(["sysctl", "-n", "hw.memsize"]) or "").strip()
        if not total.isdigit():
            return None
        out = {"total": int(total), "available": 0}
        vm = self.run(["vm_stat"]) or ""
        page = re.search(r"page size of (\d+) bytes", vm)
        pages = {m.group(1): int(m.group(2)) for m in re.finditer(r"^Pages (free|inactive|speculative):\s+(\d+)\.", vm, re.M)}
        if page:
            out["available"] = sum(pages.values()) * int(page.group(1))
        return out

    def open_uri(self, uri, strict=False):
        try:
            subprocess.Popen(["open", str(uri)], start_new_session=True, close_fds=True, **QUIET)
            return True
        except OSError:
            if strict:
                raise
            return False

    open_path = open_uri

    def steam_roots(self):
        root = self.home / "Library" / "Application Support" / "Steam"
        return [root] if (root / "steamapps").is_dir() else []

    def user_dir(self, known, prefix=None):
        """Where a native macOS game keeps what a Windows one keeps in that known folder."""
        return {"LOCALAPPDATA": self.home / "Library" / "Application Support", "APPDATA": self.home / "Library" / "Application Support",
                "LOCALLOW": self.home / "Library" / "Application Support", "DOCUMENTS": self.home / "Documents",
                "SAVED_GAMES": self.home / "Documents", "USERPROFILE": self.home}.get(known)


class Windows:
    """Today's code, as the kit and Fenrir Connect had it (the places it came from are named)."""
    name = "windows"

    def data_home(self, app):  # gamekit/__init__.py home()
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / app

    config_home = cache_home = data_home

    def downloads_dir(self):  # gamekit/sources_web.py downloads_folder()
        """The person's Downloads folder, wherever they moved it (the Known Folder, not a guess)."""
        try:
            import ctypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]
            u = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
            g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            p = ctypes.c_wchar_p()
            shell = ctypes.windll.shell32
            shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
            if shell.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                return path
        except (AttributeError, OSError, ValueError):
            pass
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"

    def desktop_dir(self):  # connect/appinstall.py desktop_dir()
        """The Desktop Windows shows. OneDrive often moves it to OneDrive\\Desktop: a shortcut in the old folder was invisible."""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                p = os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0])
            if p and Path(p).is_dir():
                return Path(p)
        except (OSError, ImportError):
            pass
        return Path(os.environ.get("USERPROFILE", "")) / "Desktop"

    def running_names(self):  # gamekit/procs.py running_names()
        """Image names of every running process, lower case; None when Windows would not say."""
        try:
            import ctypes
            from ctypes import wintypes

            class PE(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
            if not snap or snap == ctypes.c_void_p(-1).value:
                return None
            try:
                e = PE()
                e.dwSize = ctypes.sizeof(PE)
                names = set()
                ok = k32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    names.add(e.szExeFile.lower())
                    ok = k32.Process32NextW(snap, ctypes.byref(e))
                return names or None
            finally:
                k32.CloseHandle(snap)
        except (AttributeError, OSError, ValueError):
            return None

    def find_procs(self, names, contains=None, timeout=30):  # connect/installer.py close_launcher(), game_running(): CIM through PowerShell
        """Processes by image name, with their command lines; None when Windows would not say in time."""
        names = [n for n in names if re.fullmatch(r"[A-Za-z0-9_.\- ]+", n or "")]  # the names go into a filter: never anything else
        if not names:
            return []
        flt = " OR ".join(f"Name='{n}'" for n in names)
        script = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
                  f"Get-CimInstance Win32_Process -Filter \"{flt}\" | ForEach-Object {{ \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)\" }}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                               creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        out = []
        for line in (r.stdout or b"").decode("utf-8", "replace").lstrip("﻿").splitlines():
            pid, _, rest = line.partition("\t")
            name, _, cmd = rest.partition("\t")
            if pid.strip().isdigit() and (contains is None or contains in cmd):
                out.append({"pid": int(pid), "name": name.strip().lower(), "cmd": cmd.strip()})
        return out

    def end(self, pid):  # connect/installer.py close_launcher(): taskkill /F
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
            return True
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    def end_named(self, name):  # connect/installer.py close_launcher(), when the processes could not be listed
        try:
            subprocess.run(["taskkill", "/IM", str(name), "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def memory(self):  # connect/installer.py total_ram_gb()
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        try:
            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return {"total": int(st.ullTotalPhys), "available": int(st.ullAvailPhys)}
        except (AttributeError, OSError):
            return None

    def dir_link(self, link, target):  # connect/installer.py make_junction(): a junction, without a shell
        """A directory junction link -> target: no admin needed, and cmd never splits a path with an & or ^ in it."""
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, AttributeError):
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, creationflags=CREATE_NO_WINDOW)
        except OSError:
            pass  # the caller checks the result and says what to do

    def is_dir_link(self, path):  # connect/installer.py is_junction()
        try:
            return bool(os.lstat(path).st_file_attributes & REPARSE)
        except (OSError, AttributeError):
            return False

    def remove_dir_link(self, path):  # connect/installer.py: a junction is removed with rmdir, which leaves its folder
        os.rmdir(path)

    def link_target(self, path):
        try:
            t = os.readlink(path)
        except (OSError, ValueError):
            return None
        return Path(t[4:] if t.startswith("\\\\?\\") else t)

    def hide(self, path):  # gamekit/deploy.py _hide()
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), HIDDEN)
        except (AttributeError, OSError):
            pass

    def file_version(self, exe):  # gamekit/kits.py file_version(): version.dll
        """The x.y.z.w version in an exe's version resource (GTA5_Enhanced.exe → 1.0.1158.13), or None."""
        try:
            import ctypes
            ver = ctypes.windll.version
            size = ver.GetFileVersionInfoSizeW(str(exe), None)
            if not size:
                return None
            buf = ctypes.create_string_buffer(size)
            if not ver.GetFileVersionInfoW(str(exe), 0, size, buf):
                return None
            p, n = ctypes.c_void_p(), ctypes.c_uint()
            if not ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n)):
                return None

            class VS(ctypes.Structure):
                _fields_ = [("sig", ctypes.c_uint32), ("strucver", ctypes.c_uint32), ("ms", ctypes.c_uint32), ("ls", ctypes.c_uint32)]
            info = ctypes.cast(p, ctypes.POINTER(VS)).contents
            return f"{info.ms >> 16}.{info.ms & 0xFFFF}.{info.ls >> 16}.{info.ls & 0xFFFF}"
        except (AttributeError, OSError):
            return None

    def open_uri(self, uri, strict=False):  # os.startfile, as Connect and the kit open pages, folders and store links
        try:
            os.startfile(str(uri))  # noqa: S606 - the system's own handler for a page, a folder or a store's link
            return True
        except OSError:
            if strict:  # the kit's Play said why a start failed: it still does
                raise
            return False

    def steam_roots(self):  # gamekit/library.py steam_root(): the registry
        """Steam's folder as Steam wrote it in the registry: [] when Steam is not installed."""
        p = None
        try:
            import winreg
            for root, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(root, key) as k:
                        p = winreg.QueryValueEx(k, value)[0]
                except OSError:
                    continue
                if p:
                    break
        except ImportError:
            pass
        return [Path(p)] if p and Path(p).is_dir() else []

    def user_dir(self, known, prefix=None):  # gamekit/hooks.py plugins_txt(): the folders a game finds in its environment
        """A known folder as a Windows game finds it (a prefix is Linux's, and ignored here)."""
        profile = Path(os.environ.get("USERPROFILE") or Path.home())
        return {"LOCALAPPDATA": lambda: Path(os.environ.get("LOCALAPPDATA") or Path.home()),
                "APPDATA": lambda: Path(os.environ.get("APPDATA") or Path.home()),
                "LOCALLOW": lambda: profile / "AppData" / "LocalLow", "DOCUMENTS": lambda: profile / "Documents",
                "SAVED_GAMES": lambda: profile / "Saved Games", "USERPROFILE": lambda: profile}.get(known, lambda: None)()

    open_path = open_uri

    def spawn_detached(self, argv, cwd=None):  # gamekit/store.py, gamekit/mashups.py: a game started on its own
        return subprocess.Popen([str(a) for a in argv], cwd=str(cwd) if cwd else None, creationflags=DETACHED, close_fds=True)


def system():
    """The implementation for the system this runs on."""
    if os.name == "nt":
        return Windows()
    return Mac() if sys.platform == "darwin" else Linux()


IMPL = system()


def data_home(app):
    """Where an app keeps its data: %LOCALAPPDATA%\\<app>, $XDG_DATA_HOME/<app>, ~/Library/Application Support/<app>."""
    return IMPL.data_home(app)


def config_home(app):
    return IMPL.config_home(app)


def cache_home(app):
    return IMPL.cache_home(app)


def downloads_dir():
    return IMPL.downloads_dir()


def desktop_dir():
    return IMPL.desktop_dir()


def running_names():
    """Image names of every running process, lower case (a Proton game's .exe among them); None when the system would not
    say, which a caller must never read as "the game has closed"."""
    return IMPL.running_names()


def find_procs(names, contains=None, timeout=30):
    """[{"pid", "name", "cmd"}] of the processes with one of these image names (and the text in their command line); None
    when the system would not list them (in time), which a caller must never read as "none is running"."""
    return IMPL.find_procs(names, contains, timeout)


def end(pid):
    return IMPL.end(pid)


def end_named(name):
    """Every process of one image name ended (taskkill /IM on Windows); False when that could not be done."""
    return IMPL.end_named(name)


def memory():
    """{"total", "available"} in bytes, or None when the system would not say."""
    return IMPL.memory()


def dir_link(link, target):
    """A directory link at link pointing to target: a junction on Windows, a symlink elsewhere."""
    return IMPL.dir_link(link, target)


def is_dir_link(path):
    return IMPL.is_dir_link(path)


def remove_dir_link(path):
    """A directory link removed, never what it points to: rmdir for a junction, unlink for a symlink. Raises OSError."""
    return IMPL.remove_dir_link(path)


def link_target(path):
    return IMPL.link_target(path)


def hide(path):
    return IMPL.hide(path)


def file_version(exe):
    return IMPL.file_version(exe)


def open_uri(uri, strict=False):
    """A page, a folder or a store's link opened by the system's own handler. strict: an OSError is raised, not answered False."""
    return IMPL.open_uri(uri, strict)


def open_path(path):
    return IMPL.open_path(path)


def spawn_detached(argv, cwd=None):
    return IMPL.spawn_detached(argv, cwd)


def steam_roots():
    """Every Steam installed for this user: the registry's on Windows; native, Flatpak and Snap Steam on Linux (the Steam
    Deck's is the native one); ~/Library/Application Support/Steam on macOS. Only folders that hold a steamapps."""
    return IMPL.steam_roots()


def user_dir(known, prefix=None):
    """A Windows known folder (LOCALAPPDATA, LOCALLOW, APPDATA, DOCUMENTS, SAVED_GAMES, USERPROFILE) as a game finds it:
    the real one on Windows, the one inside the game's Proton prefix on Linux (None without one), the nearest macOS folder."""
    return IMPL.user_dir(known, prefix)
