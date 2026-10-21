"""The engine's platform layer (docs/v3-design/server.md §3.3.6): what running servers needs that Windows and Linux do
differently, in one place, chosen once at import (IMPL). The engine, the game servers, your own servers and the recipes'
process driver ask it rather than checking the system themselves.

    windows.py   2.x's own code, moved here unchanged: Windows behaves exactly as before
    linux.py     /proc, process groups and signals, ptys, the fw helper, sd_notify, XDG and /var/lib
    tools.py     where each downloaded program's build for this system and processor comes from, or why there is none

    started(pid)            when a process started (unix seconds)        same(pid, born)   it is still the one Fenrir started
    alive(pid)              it runs                                      tree(pid)         it and what it started
    interrupt(pid)          a clean stop's Ctrl+C                        end_tree(pid)     it and what it started, ended now
    signal(pid, name)       a recipe's own signal (Linux)                children(pid)     what it started, by name
    close(pid)              the program asked to close (taskkill without /F; SIGTERM on Linux)
    prepare_children()      before a start (Windows: Ctrl+C on)          spawn_kwargs()    Popen's own arguments for a server
    pty_consoles            a console of keys is a pty (Linux)           write_console()   a line typed into it
    arch()                  "x64" or "arm64"                             data_home(root)   where DATA goes by default
    notify(*states)         sd_notify, under systemd (Linux)             watchdog_every()  systemd's watchdog, half its time

The game kit keeps its own layer (gamekit/oslayer.py), shared with Fenrir Connect: games, libraries and their mods."""
import os

if os.name == "nt":
    from .windows import WindowsLayer as _Layer
else:
    from .linux import LinuxLayer as _Layer

IMPL = _Layer()


def started(pid):
    return IMPL.started(pid)


def alive(pid):
    return IMPL.alive(pid)


def same(pid, born):
    return IMPL.same(pid, born)


def tree(pid, parents=None):
    return IMPL.tree(pid, parents)


def children(pid):
    return IMPL.children(pid)


def interrupt(pid):
    return IMPL.interrupt(pid)


def terminate(pid):
    return IMPL.terminate(pid)


def end_tree(pid):
    return IMPL.end_tree(pid)


def signal(pid, name):
    return IMPL.signal(pid, name)


def close(pid):
    return IMPL.close(pid)


def prepare_children():
    return IMPL.prepare_children()


def spawn_kwargs():
    return IMPL.spawn_kwargs()


def pty_consoles():
    return bool(IMPL.pty_consoles)


def spawn_console(argv, cwd, env, log, popen=None):
    return IMPL.spawn_console(argv, cwd, env, log, popen=popen)


def write_console(pid, text):
    return IMPL.write_console(pid, text)


def arch():
    return IMPL.arch()


def system():
    """"windows" or "linux", as recipes name systems."""
    return "windows" if IMPL.name == "windows" else "linux"


def data_home(root=None, frozen=False):
    return IMPL.data_home(root, frozen=frozen)


def notify(*states, send=None):
    return IMPL.notify(*states, send=send)


def watchdog_every():
    return IMPL.watchdog_every()
