"""Ports for a harness run that nothing on this PC holds, so a run never answers in place of, or is refused by, a real
server here, and recipes can run side by side. Windows keeps ranges of ports for Hyper-V and the like inside its
dynamic range, often for TCP and not UDP, so the search is below it, and skips the ports real servers and Fenrir use."""
import random
import socket

LOW, HIGH = 20000, 40000
KEEP_CLEAR = {25565, 25575, 26900, 26901, 26902, 26903, 27015, 27016, 30120, 10578, 2456, 2457, 2458, 7777, 8211}


def _free(n):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        s = socket.socket(socket.AF_INET, kind)
        try:
            s.bind(("127.0.0.1", n))
        except OSError:
            return False
        finally:
            s.close()
    return True


def free_base(span=1, tries=400, rng=random, low=LOW, high=HIGH):
    """The first of span ports in a row, each free for TCP and for UDP on 127.0.0.1, between low and high (a setting's
    own limits narrow the search)."""
    lo, hi = max(LOW, int(low)), min(HIGH, int(high))
    if hi - span < lo:  # a setting allowed only outside the usual search: its own limits, still above the well-known ports
        lo, hi = max(1024, int(low)), int(high)
    low, high = lo, hi
    for _ in range(tries):
        base = rng.randint(low, max(low, high - span))
        run = range(base, base + span)
        if any(n in KEEP_CLEAR or 8720 <= n <= 8799 for n in run):
            continue
        if all(_free(n) for n in run):
            return base
    raise OSError("No free ports were found on this PC for the harness.")
