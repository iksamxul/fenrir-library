"""Steam's player query (A2S_PLAYER, 2.5): the names of who is on a dedicated server that answers it, for the Servers
pages' Players. The host's alone: gamekit/a2s.py (Fenrir Connect's too) says how many are on, this says who, with one
more UDP round trip, and only while someone is.

    request   FF FF FF FF 55 + challenge (FF FF FF FF the first time)
    challenge FF FF FF FF 41 + 4 bytes  → the request again with those 4 bytes
    answer    FF FF FF FF 44 count, then for each player: index, name\\0, score (int32), seconds on (float32)
"""
import math
import socket
import struct
import threading
import time

HEAD = b"\xff\xff\xff\xff"
FRESH, SILENT = 10, 60  # s: who is on is asked again after FRESH; a server that does not answer this query, after SILENT
_seen, _lock = {}, threading.Lock()


def parse_players(data):
    """An A2S_PLAYER answer → [{"name", "score", "seconds"}], players without a name left out (some servers send them
    blank); None when it is not such an answer. A cut answer gives what was read before the cut."""
    if len(data) < 6 or data[:4] != HEAD or data[4] != 0x44:
        return None
    out, i = [], 6
    for _ in range(data[5]):
        try:
            j = data.index(b"\x00", i + 1)  # past the index byte
            score, secs = struct.unpack_from("<lf", data, j + 1)
        except (ValueError, struct.error):
            break
        name = data[i + 1:j].decode("utf-8", "replace").strip()
        i = j + 9
        if name:  # a time on that is not a number (NaN, or infinite: 00 00 80 7F) is 0, never a crash
            out.append({"name": name[:40], "score": score, "seconds": int(secs) if math.isfinite(secs) and secs > 0 else 0})
    return out


def players(host, port, timeout=1.5, sock_factory=None):
    """Who is on one server, or None when nothing answers in time. Remembered: the Servers pages and the phone ask every
    few seconds, and a server that answers how many are on but not who would otherwise add its wait to every one of them
    (2.5 review). A test's own socket is never remembered."""
    key = (host, int(port))
    if sock_factory is None:
        with _lock:
            hit = _seen.get(key)
        if hit and time.time() - hit[0] < (FRESH if hit[1] is not None else SILENT):
            return hit[1]
    got = _ask(host, port, timeout, sock_factory)
    if sock_factory is None:
        with _lock:
            _seen[key] = (time.time(), got)
    return got


def _ask(host, port, timeout, sock_factory):
    s = (sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))()
    try:
        s.settimeout(timeout)
        s.sendto(HEAD + b"\x55" + HEAD, (host, int(port)))
        data, _ = s.recvfrom(1400)
        if len(data) >= 9 and data[:5] == HEAD + b"A":  # a challenge: ask again with it
            s.sendto(HEAD + b"\x55" + data[5:9], (host, int(port)))
            data, _ = s.recvfrom(1400)
        return parse_players(data)
    except OSError:
        return None
    finally:
        s.close()
