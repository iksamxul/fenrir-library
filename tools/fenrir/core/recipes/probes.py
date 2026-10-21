"""Ready and health (recipes.md section 3.6): the probes that say a server is up, each one bounded in time and aimed at
127.0.0.1 only.

    log     a line of this run matches (cut to 4 KB first, values.search)
    port    a TCP port takes a connection; a UDP port's Steam query answers
    query   the game's own query: Steam's A2S (gamekit/a2s.py), Minecraft's Server List Ping, or RCON's sign-in
    http    an address on this PC answers with an allowed status, and holds a text when the recipe asks for one;
            a redirect counts as its own status and is never followed off this PC
    alive   running this long counts as ready, for a game Fenrir cannot ask

A probe never raises: anything that goes wrong is "not yet", with what was seen, so the page can say it."""
import http.client
import json
import socket
import ssl
import urllib.parse

from gamekit import a2s

from .. import rcon
from ..games import a2splayers
from . import values

TIMEOUT = 1.5
BODY_MAX = 64 * 1024
SLP_MAX = 1024 * 1024  # a status answer larger than this is not a Minecraft server's


def tcp_open(port, timeout=TIMEOUT):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _varint(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(sock):
    n = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise OSError("the server closed the connection")
        n |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return n
    raise OSError("not a Minecraft answer")


def _read_exact(sock, n):
    data = bytearray()
    while len(data) < n:
        part = sock.recv(min(65536, n - len(data)))
        if not part:
            raise OSError("the server closed the connection")
        data += part
    return bytes(data)


def slp(port, timeout=2.0):
    """Minecraft's Server List Ping (1.7 and later): who is on and how many may be, or None when nothing answers."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            host = b"127.0.0.1"
            hello = b"\x00" + _varint(47) + _varint(len(host)) + host + int(port).to_bytes(2, "big") + _varint(1)
            s.sendall(_varint(len(hello)) + hello + _varint(1) + b"\x00")
            length = _read_varint(s)
            if not 0 < length <= SLP_MAX or _read_varint(s) != 0:
                return None
            body = _read_exact(s, _read_varint(s))
        doc = json.loads(body.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    pl = doc.get("players") if isinstance(doc.get("players"), dict) else {}
    desc = doc.get("description")
    name = desc.get("text") if isinstance(desc, dict) else desc if isinstance(desc, str) else None
    names = [p["name"] for p in pl.get("sample") or [] if isinstance(p, dict) and isinstance(p.get("name"), str)]
    try:
        return {"players": int(pl.get("online") or 0), "max": int(pl["max"]) if pl.get("max") is not None else None,
                "name": name, "names": names[:32]}
    except (TypeError, ValueError):
        return None


def rcon_signs_in(port, password, timeout=TIMEOUT):
    """RCON takes the password: the server is up and its console listens."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.sendall(rcon.packet(1, rcon.AUTH, password or ""))
            req_id, _kind, _text = rcon.read_packet(s)
            return req_id != -1
    except (OSError, ValueError):
        return False


def http_call(url, method="GET", body=None, timeout=3.0, headers=None):
    """(status, the first 64 KB of the answer) for one request to an address on this PC; (None, why) when it does not
    answer. No redirect is followed: one could lead off this PC. headers: what else the call carries (a sign-in)."""
    u = urllib.parse.urlsplit(url)
    if u.hostname != "127.0.0.1":
        return None, "not an address on this PC"
    try:
        # an https server on this PC is its own, and usually signed by itself: its certificate proves nothing here
        conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl._create_unverified_context())
                if u.scheme == "https" else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout))
        try:
            data = body.encode("utf-8") if isinstance(body, str) else None
            conn.request(method, (u.path or "/") + (f"?{u.query}" if u.query else ""), body=data,
                         headers={"User-Agent": "Fenrir", **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
            r = conn.getresponse()
            return r.status, r.read(BODY_MAX).decode("utf-8", "replace")
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as e:
        return None, str(e)[:120]


def http_get(url, timeout=3.0, headers=None):
    return http_call(url, "GET", None, timeout, headers)


class Reach:
    """What a probe or a player source may ask of one running server: its ports, this run's lines, how long it has run, its
    addresses on this PC (url fills a template and checks it is 127.0.0.1) and its RCON. The ways of asking are
    attributes, so a test puts its fakes in their place."""

    def __init__(self, ports=None, lines=None, alive=0.0, url=None, rcon_port=None, rcon_password=None, auth=None):
        self.ports = ports or {}  # id -> {"port": n, "proto": "tcp" or "udp"}
        self.lines = lines or (lambda: [])
        self.alive = alive
        self.url = url or (lambda template: template)
        self.rcon_port, self.rcon_password = rcon_port, rcon_password
        self.auth = auth or (lambda spec: {})  # a REST source's sign-in as headers, its password opened from the store
        self.tcp, self.slp, self.http_get, self.rcon_signs_in = tcp_open, slp, http_get, rcon_signs_in
        self.a2s = lambda port: a2s.query("127.0.0.1", port)
        self.a2s_names = lambda port: a2splayers.players("127.0.0.1", port)
        self.rcon_command = lambda command, port, password: rcon.command(command, password or "", port=port, timeout=3)

    def port(self, pid):
        return int(self.ports[pid]["port"])

    def rcon(self, command, port=None):
        """One RCON command's answer; raises when RCON cannot be reached or refuses the password."""
        return self.rcon_command(command, int(port or self.rcon_port), self.rcon_password)


def check(probe, reach):
    """(it answered, what was seen) for one probe. Never raises."""
    try:
        if "log" in probe:
            ok = any(values.search(probe["log"], line) for line in reach.lines())
            return ok, "" if ok else "its log has not said it yet"
        if "port" in probe:
            p = reach.ports[probe["port"]]
            ok = reach.tcp(p["port"]) if p["proto"] == "tcp" else reach.a2s(p["port"]) is not None
            return ok, "" if ok else f"port {p['port']} does not answer yet"
        if "query" in probe:
            port, kind = reach.port(probe["on"]), probe["query"]
            ok = (reach.a2s(port) is not None if kind == "a2s" else reach.slp(port) is not None if kind == "minecraft"
                  else reach.rcon_signs_in(port, reach.rcon_password))
            return ok, "" if ok else f"its {'Steam query' if kind == 'a2s' else 'Minecraft status' if kind == 'minecraft' else 'RCON'} does not answer yet"
        if "http" in probe:
            h = probe["http"]
            status, body = reach.http_get(reach.url(h["url"]))
            ok = status is not None and (status in h["status"] if h.get("status") else 200 <= status < 400) and (not h.get("text") or h["text"] in body)
            return ok, "" if ok else f"its address answers {status}" if status is not None else "its address does not answer yet"
        if "alive" in probe:
            ok = reach.alive >= probe["alive"]
            return ok, "" if ok else "it has not run long enough yet"
    except Exception as e:  # noqa: BLE001 - a probe that breaks says "not yet": the ready timeout then says it plainly
        return False, f"the check could not run ({type(e).__name__})"
    return False, "this probe asks nothing Fenrir knows"


def ready(doc, reach):
    """(ready, what was seen): any probe in ready.any, or every one in ready.all, answered."""
    r = doc.get("ready") or {}
    if r.get("all"):
        seen = [check(p, reach) for p in r["all"]]
        return all(ok for ok, _ in seen), next((why for ok, why in seen if not ok), "")
    said = ""
    for p in r.get("any") or []:
        ok, why = check(p, reach)
        if ok:
            return True, ""
        said = said or why
    return False, said
