"""The engine's platform layer on Windows (docs/v3-design/server.md §3.3.6): 2.x's own code, moved here without a change
from where it was (each place is named), so Windows behaves exactly as it did. Ctrl+C and a line typed into a console stay
core/games/consolectl.py's helper processes, which this calls; processor and memory stay in core/sysinfo.py."""
import ctypes
import os
import platform
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


class WindowsLayer:
    name = "windows"

    # ---------------------------------------------------------------------------------------------------- identity
    def started(self, pid):  # core/games/servers.py proc_created
        """When a process started (unix seconds), from Windows itself; None when that cannot be read."""
        if not pid:
            return None
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.restype = ctypes.c_void_p
            k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, kt, u = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
                if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(u)):
                    return None
                return c.value / 1e7 - 11644473600  # FILETIME: 100 ns since 1601
            finally:
                k.CloseHandle(h)
        except (AttributeError, OSError, ValueError):
            return None

    def alive(self, pid):  # core/swapin.py alive
        from ..swapin import alive
        return alive(pid)

    def same(self, pid, born):  # core/games/servers.py GameServer.running
        if not pid or born is None or not self.alive(pid):
            return False
        now_born = self.started(pid)
        return now_born is not None and abs(now_born - float(born)) < 2

    def tree(self, pid, parents=None):  # core/games/keeper.py _tree
        """The server's process and every process it started (Palworld's launcher starts the real server), from one snapshot."""
        if not pid:
            return []
        if parents is not None:
            from ..sysinfo import tree
            return tree(pid, parents)
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return [pid]
        kids = {}
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        out, todo = [], [pid]
        while todo and len(out) < 64:
            p = todo.pop()
            if p in out:
                continue
            out.append(p)
            todo.extend(c for c in kids.get(p, []) if c != p)
        return out

    HOSTS = ("conhost.exe", "openconsole.exe")  # a console's own host runs as a child of the first program in it

    def children(self, pid):  # core/games/consolectl.py children
        """The programs pid started that still run, as lower-case image names, its console's host left out; None when Windows
        would not say. One Toolhelp snapshot, through kernel32 of its own (never the shared ctypes.windll's argtypes)."""
        if not pid:
            return None
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PE)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        out = []
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.th32ParentProcessID == pid and e.th32ProcessID != pid and e.szExeFile.lower() not in self.HOSTS:
                    out.append(e.szExeFile.lower())
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out

    # ---------------------------------------------------------------------------------------------------- stopping
    def interrupt(self, pid):
        """Ctrl+C, the way a person pressing it in the server's console would (core/games/consolectl.py's helper)."""
        from ..games import consolectl
        return consolectl.ctrl_c(pid)

    def terminate(self, pid):
        return self.interrupt(pid)  # Windows has no SIGTERM for a console program: Ctrl+C is the clean way

    def end_tree(self, pid):  # core/games/servers.py end_tree
        """The server's process and every process it started (taskkill /T): a launcher that started the real server leaves
        nothing behind holding the port and the save."""
        if not pid:
            return False
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, text=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0

    def signal(self, pid, name):
        return False  # a recipe's "signal" stop step is Linux's own (core/recipes/drivers/process.py)

    def close(self, pid):
        """The program asked to close, as taskkill without /F asks its windows: how Core Keeper's own Launch.ps1 stops its
        server, and Space Engineers' documented stop (3.0, the live checks). Its tree is left to end by itself."""
        if not pid:
            return False
        r = subprocess.run(["taskkill", "/PID", str(int(pid))], capture_output=True, text=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0

    # ---------------------------------------------------------------------------------------------------- starting
    def prepare_children(self):  # core/games/servers.py, core/games/custom.py, the process driver: before each start
        """Children inherit "Ctrl+C works": the clean stop depends on it."""
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetConsoleCtrlHandler(None, 0)

    def spawn_kwargs(self):
        """A hidden console of its own: its Ctrl+C and its typed lines reach it there (core/games/consolectl.py)."""
        return {"creationflags": CREATE_NO_WINDOW}

    pty_consoles = False  # a console of keys is the server's own hidden console, typed into by the helper

    def write_console(self, pid, text):
        """A line typed into the server's console (core/games/consolectl.py's helper)."""
        from ..games import consolectl
        return consolectl.type_line(pid, text)

    # ---------------------------------------------------------------------------------------------------- the machine
    def machine(self):
        return (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine() or "").lower()

    def arch(self):
        m = self.machine()
        return "arm64" if m in ("arm64", "aarch64") else "x86" if m == "x86" else "x64"

    def data_home(self, root=None, frozen=False):  # core/util.py: the built app's, else the source checkout itself
        if frozen:
            return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Fenrir"
        return Path(root) if root is not None else Path.cwd()

    def notify(self, *states, send=None):
        return False  # systemd's: the Windows service tells the SCM instead (core/winservice.py)

    def watchdog_every(self):
        return None
