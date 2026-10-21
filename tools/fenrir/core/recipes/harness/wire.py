"""The answers a game server gives on its ports, for the fake server (fake_server.py): Steam's query (A2S_INFO and
A2S_PLAYER, as gamekit/a2s.py and core/games/a2splayers.py ask), Source RCON (core/rcon.py), Minecraft's Server List Ping
(the probes' slp), plain HTTP for REST, health and players, and a plain TCP accept. The standard library only: the fake
server runs as a program of its own, without the engine.

Every listener runs on a daemon thread, answers on 127.0.0.1 only, and reads what it is told through a Live object, so
the player count, the names and the stop that follow the schedule are the same on every port."""
import json
import os
import socket
import struct
import threading
import time

HEAD = b"\xff\xff\xff\xff"
INFO_ASK = HEAD + b"TSource Engine Query\x00"
CHALLENGE = b"\x0f\x0e\x0d\x0c"  # a real server's challenge changes; one fixed value still makes the client ask twice
RCON_AUTH, RCON_COMMAND, RCON_ANSWER = 3, 2, 0


class Live:
    """What the server says right now: who is on, its name and map, and what it does with what it hears. The schedule,
    the console and the ports all read and write it, under one lock."""

    def __init__(self, name="Fenrir", game="Fenrir", folder="fenrir", map_name="Fenrir", appid=0, max_players=10, version="1.0"):
        self.lock = threading.Lock()
        self.names = []
        self.joined = {}
        self.name, self.game, self.folder, self.map, self.appid, self.max = name, game, folder, map_name, appid, max_players
        self.version = version
        self.on_command = lambda via, line: ""  # set by the server: a console, RCON or REST command and its answer

    def join(self, name):
        with self.lock:
            if name not in self.names:
                self.names.append(name)
                self.joined[name] = time.monotonic()

    def leave(self, name):
        with self.lock:
            if name in self.names:
                self.names.remove(name)
                self.joined.pop(name, None)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return list(self.names), {n: now - t for n, t in self.joined.items()}


# ---------------------------------------------------------------------------------------------------- Steam's query
def _cstr(text):
    return str(text).encode("utf-8", "replace").replace(b"\x00", b"") + b"\x00"


def a2s_info(live):
    """An A2S_INFO answer, as a Source dedicated server sends it."""
    names, _ = live.snapshot()
    body = b"I" + b"\x11" + _cstr(live.name) + _cstr(live.map) + _cstr(live.folder) + _cstr(live.game)
    body += struct.pack("<hBBB", int(live.appid) & 0x7FFF, min(255, len(names)), min(255, int(live.max)), 0)
    body += b"d" + (b"w" if os.name == "nt" else b"l") + b"\x00\x00" + _cstr(live.version)  # dedicated, public, no VAC
    return HEAD + body


def a2s_players(live):
    """An A2S_PLAYER answer: each player's index, name, score and seconds on."""
    names, seconds = live.snapshot()
    body = b"D" + bytes([min(255, len(names))])
    for i, n in enumerate(names[:255]):
        body += bytes([i]) + _cstr(n) + struct.pack("<lf", 0, float(seconds.get(n, 0.0)))
    return HEAD + body


def a2s_answer(data, live):
    """The answer to one query packet, or None for one that is not a query. The first ask gets a challenge, as servers do
    since 2020, so the client's second round trip is played too."""
    if data.startswith(INFO_ASK):
        return a2s_info(live) if data[len(INFO_ASK):len(INFO_ASK) + 4] == CHALLENGE else HEAD + b"A" + CHALLENGE
    if data.startswith(HEAD + b"U"):
        return a2s_players(live) if data[5:9] == CHALLENGE else HEAD + b"A" + CHALLENGE
    return None


def serve_udp(port, live, stop):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(0.2)

    def loop():
        with s:
            while not stop.is_set():
                try:
                    data, addr = s.recvfrom(1400)
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        return
                    continue  # Windows says a reset for an earlier answer nobody read; the port still serves
                answer = a2s_answer(data, live)
                if answer:
                    try:
                        s.sendto(answer, addr)
                    except OSError:
                        pass
    threading.Thread(target=loop, daemon=True, name=f"udp-{port}").start()
    return s


# ---------------------------------------------------------------------------------------------------- TCP servers
def _exact(conn, n):
    data = b""
    while len(data) < n:
        part = conn.recv(n - len(data))
        if not part:
            raise OSError("closed")
        data += part
    return data


def rcon_packet(req_id, kind, text=""):
    body = struct.pack("<ii", req_id, kind) + str(text).encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def rcon_session(conn, live, password):
    """Source RCON: sign-in with the password (a wrong one answers -1; None takes any), then commands, each answered under
    its own id. An empty answer-type packet (core/rcon.py's end marker) is answered empty, so the client knows the answer
    is whole."""
    signed = False
    while True:
        (n,) = struct.unpack("<i", _exact(conn, 4))
        if not 10 <= n <= 4110:
            return
        body = _exact(conn, n)
        req_id, kind = struct.unpack("<ii", body[:8])
        text = body[8:-2].decode("utf-8", "replace")
        if kind == RCON_AUTH:
            signed = password is None or text == password
            conn.sendall(rcon_packet(req_id if signed else -1, RCON_COMMAND, ""))
        elif not signed:
            conn.sendall(rcon_packet(-1, RCON_COMMAND, ""))
        elif kind == RCON_COMMAND:
            conn.sendall(rcon_packet(req_id, RCON_ANSWER, live.on_command("rcon", text) or ""))
        else:
            conn.sendall(rcon_packet(req_id, RCON_ANSWER, ""))


def _varint(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(conn):
    n = 0
    for i in range(5):
        b = _exact(conn, 1)[0]
        n |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return n
    raise OSError("not a varint")


def slp_session(conn, live):
    """Minecraft's Server List Ping: the handshake, then the status as JSON, then the ping's echo."""
    for _ in range(3):
        length = _read_varint(conn)
        if not 0 < length <= 1024:
            return
        packet = _exact(conn, length)
        pid = packet[0]
        if pid == 0 and len(packet) > 1:
            continue  # the handshake: its next state is status
        if pid == 0:
            names, _ = live.snapshot()
            doc = {"version": {"name": live.version, "protocol": 767}, "description": {"text": live.name},
                   "players": {"max": int(live.max), "online": len(names),
                               "sample": [{"name": n, "id": f"00000000-0000-0000-0000-{i:012d}"} for i, n in enumerate(names)]}}
            body = json.dumps(doc).encode("utf-8")
            data = b"\x00" + _varint(len(body)) + body
            conn.sendall(_varint(len(data)) + data)
        elif pid == 1:
            conn.sendall(_varint(len(packet)) + packet)
            return


def http_session(conn, live, routes):
    """One HTTP/1.1 request, answered and closed: GET for health and players (as JSON), POST for a REST command."""
    conn.settimeout(3)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        part = conn.recv(4096)
        if not part:
            return
        data += part
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    try:
        method, path, _ = lines[0].split(" ", 2)
    except ValueError:
        return
    length = 0
    for line in lines[1:]:
        k, _, v = line.partition(":")
        if k.strip().lower() == "content-length":
            length = int(v.strip() or 0)
    while len(rest) < length:
        part = conn.recv(4096)
        if not part:
            break
        rest += part
    status, body, kind = routes(method.upper(), path, rest[:length].decode("utf-8", "replace"))
    out = body.encode("utf-8")
    conn.sendall(f"HTTP/1.1 {status} {'OK' if status < 400 else 'No'}\r\nContent-Type: {kind}\r\nContent-Length: {len(out)}\r\n"
                 "Connection: close\r\n\r\n".encode("latin-1") + out)


def serve_tcp(port, handler, stop):
    """A listener on 127.0.0.1:port that gives each connection to handler(conn) on a thread of its own."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(16)
    s.settimeout(0.2)

    def one(conn):
        with conn:
            try:
                conn.settimeout(5)
                handler(conn)
            except (OSError, ValueError, struct.error):
                pass

    def loop():
        with s:
            while not stop.is_set():
                try:
                    conn, _ = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        return
                    continue
                threading.Thread(target=one, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True, name=f"tcp-{port}").start()
    return s
