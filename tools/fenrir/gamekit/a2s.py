"""Steam's server query (A2S_INFO): whether a dedicated server is up, its name, map and players, in one UDP round trip.
Valheim answers on its query port (game port + 1, only with -public 1), Source servers on 27015, Project Zomboid on 16261.

    request   FF FF FF FF 54 "Source Engine Query\\0"
    challenge FF FF FF FF 41 + 4 bytes  → the request again with those 4 bytes appended
    answer    FF FF FF FF 49 protocol, name\\0, map\\0, folder\\0, game\\0, appid (short), players, max, bots, …
"""
import socket
import struct

REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"


def _cstr(data, i):
    j = data.index(b"\x00", i)
    return data[i:j].decode("utf-8", "replace"), j + 1


def parse_info(data):
    """An A2S_INFO answer → {"name", "map", "folder", "game", "appid", "players", "max", "bots"}; None when it is not one."""
    if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x49:
        return None
    try:
        i = 6  # header + protocol byte
        name, i = _cstr(data, i)
        mapname, i = _cstr(data, i)
        folder, i = _cstr(data, i)
        game, i = _cstr(data, i)
        appid, players, maxp, bots = struct.unpack_from("<hBBB", data, i)
    except (ValueError, struct.error):
        return None
    return {"name": name, "map": mapname, "folder": folder, "game": game, "appid": appid, "players": players, "max": maxp, "bots": bots}


def query(host, port, timeout=1.5, sock_factory=None):
    """One server's A2S_INFO, or None when nothing answers in time."""
    s = (sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))()
    try:
        s.settimeout(timeout)
        s.sendto(REQUEST, (host, int(port)))
        data, _ = s.recvfrom(1400)
        if len(data) >= 9 and data[:5] == b"\xff\xff\xff\xffA":  # a challenge: ask again with it
            s.sendto(REQUEST + data[5:9], (host, int(port)))
            data, _ = s.recvfrom(1400)
        return parse_info(data)
    except OSError:
        return None
    finally:
        s.close()
