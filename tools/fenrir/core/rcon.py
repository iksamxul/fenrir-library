"""Minecraft RCON (remote console) client: how Fenrir talks to a server it adopted rather than launched.

Wire format: <int32 length><int32 request id><int32 type><payload>\0\0, little endian. Type 3 logs in, type 2 runs a
command; the server splits long answers into 4096-byte packets, so a throwaway request follows each command and reading
stops when its answer arrives."""
import socket
import struct

AUTH, COMMAND, PROBE = 3, 2, 0


def packet(req_id, kind, payload=""):
    body = struct.pack("<ii", req_id, kind) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        buf += chunk
    return buf


def read_packet(sock):
    (n,) = struct.unpack("<i", _recv_exact(sock, 4))
    body = _recv_exact(sock, n)
    req_id, kind = struct.unpack("<ii", body[:8])
    return req_id, kind, body[8:-2].decode("utf-8", "replace")


def command(cmd, password, port=25575, host="127.0.0.1", timeout=8):
    """Run one command; returns the server's answer (may be empty, e.g. for `stop`)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(packet(1, AUTH, password))
        req_id, kind, _ = read_packet(s)
        if req_id == -1:
            raise PermissionError("RCON password rejected")
        s.sendall(packet(2, COMMAND, cmd))
        s.sendall(packet(3, PROBE, ""))
        parts = []
        for _ in range(64):  # answers longer than 4096 bytes arrive in pieces before the probe's answer
            req_id, kind, text = read_packet(s)
            if req_id == 3:
                break
            parts.append(text)
        return "".join(parts)
