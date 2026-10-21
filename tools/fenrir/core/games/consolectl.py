"""Ctrl+C for a console program Fenrir started, the way a person pressing it in that window would: game servers such as
Valheim's save their world on Ctrl+C and lose everything since the last autosave when they are simply ended.

Windows lets a process send Ctrl+C only to the console it is attached to, so a separate short-lived process does it
(`Fenrir.exe --ctrl-c <pid>`, handled in app.py before anything else, or `python app.py --ctrl-c <pid>` from source): it
attaches to the server's console, ignores the event itself, sends CTRL_C_EVENT to that console, and leaves. Fenrir's own
process never attaches to anything.

Servers must be started with a console of their own (CREATE_NO_WINDOW), not in a new process group: Windows turns Ctrl+C
off for a process started with CREATE_NEW_PROCESS_GROUP."""
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from ..util import FROZEN, ROOT

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def send(pid):
    """In this (helper) process: Ctrl+C to the console `pid` is attached to. True when Windows delivered it."""
    if os.name != "nt":
        return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.AttachConsole.argtypes = [ctypes.c_uint32]
    k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    k.FreeConsole()  # a helper started with a console of its own lets go of it first
    if not k.AttachConsole(int(pid)):
        return False
    try:
        k.SetConsoleCtrlHandler(None, 1)  # this helper ignores the Ctrl+C it is about to send
        ok = bool(k.GenerateConsoleCtrlEvent(0, 0))  # CTRL_C_EVENT to every process on that console: the server
        time.sleep(0.5)
        return ok
    finally:
        k.FreeConsole()


def ctrl_c(pid, timeout=15):
    """From Fenrir: run the helper for `pid` and wait for it. True when the event went out."""
    args = [sys.executable, "--ctrl-c", str(int(pid))] if FROZEN else [sys.executable, str(ROOT / "app.py"), "--ctrl-c", str(int(pid))]
    try:
        r = subprocess.run(args, cwd=str(ROOT), timeout=timeout, creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv):
    """app.py --ctrl-c <pid>: exit code 0 when delivered, 1 when not."""
    try:
        return 0 if send(int(argv[argv.index("--ctrl-c") + 1])) else 1
    except (ValueError, IndexError):
        return 2


# ---------------------------------------------------------------------------------------------------- a line typed in (2.9)
# A server the host runs as their own (core/games/custom.py) reads its commands from its own console, a hidden one that
# belongs to it, not to Fenrir: a line typed there reaches it after Fenrir restarted too. As with Ctrl+C, a short-lived
# helper does it (`Fenrir.exe --console-type <pid> <base64 text>`): it attaches to that console, writes the line as key
# presses and Enter into the console's input, and leaves.
def type_into(pid, text):
    """In this (helper) process: `text` and Enter typed into the console `pid` is attached to. True when all of it went in."""
    if os.name != "nt":
        return False
    from ctypes import wintypes

    class KEY(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                    ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

    class EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY), ("pad", ctypes.c_byte * 16)]

    class RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u = ctypes.WinDLL("user32", use_last_error=True)
    k.AttachConsole.argtypes = [wintypes.DWORD]
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(RECORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    u.VkKeyScanW.argtypes = [wintypes.WCHAR]
    u.VkKeyScanW.restype = ctypes.c_short
    u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    units = str(text).encode("utf-16-le", "surrogatepass")
    keys = [units[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(units), 2)] + ["\r"]
    recs = (RECORD * (2 * len(keys)))()
    for i, ch in enumerate(keys):
        scan = -1 if ch == "\r" else u.VkKeyScanW(ch)
        vk = 0x0D if ch == "\r" else (scan & 0xFF if scan != -1 else 0)
        for j, down in enumerate((True, False)):
            e = recs[2 * i + j].Event.KeyEvent
            recs[2 * i + j].EventType = 1  # KEY_EVENT
            e.bKeyDown, e.wRepeatCount, e.wVirtualKeyCode = down, 1, vk
            e.wVirtualScanCode = u.MapVirtualKeyW(vk, 0) if vk else 0
            e.UnicodeChar = ch
    k.FreeConsole()  # a helper started with a console of its own lets go of it first
    if not k.AttachConsole(int(pid)):
        return False
    try:
        h = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)  # read and write, shared, the console's own input
        if not h or h == wintypes.HANDLE(-1).value:
            return False
        try:
            n = wintypes.DWORD()
            ok = k.WriteConsoleInputW(h, recs, len(recs), ctypes.byref(n))
            return bool(ok) and n.value == len(recs)
        finally:
            k.CloseHandle(h)
    finally:
        k.FreeConsole()


def type_line(pid, text, timeout=15):
    """From Fenrir: run the helper that types `text` into the console of `pid`, and wait for it. True when it went in."""
    import base64
    data = base64.b64encode(str(text).encode("utf-8")).decode("ascii")
    args = ([sys.executable, "--console-type", str(int(pid)), data] if FROZEN
            else [sys.executable, str(ROOT / "app.py"), "--console-type", str(int(pid)), data])
    try:
        r = subprocess.run(args, cwd=str(ROOT), timeout=timeout, creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HOSTS = ("conhost.exe", "openconsole.exe")  # a console's own host runs as a child of the first program in it


def children(pid):
    """The programs pid started that still run, as lower-case image names, its console's host left out; None when the
    system would not say (core/oslayer: one Toolhelp snapshot on Windows, /proc on Linux)."""
    from .. import oslayer
    return oslayer.children(pid)


def main_type(argv):
    """app.py --console-type <pid> <base64 text>: exit code 0 when typed in, 1 when not, 2 for a wrong call."""
    import base64
    try:
        i = argv.index("--console-type")
        pid, text = int(argv[i + 1]), base64.b64decode(argv[i + 2], validate=True).decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return 2
    return 0 if type_into(pid, text) else 1
