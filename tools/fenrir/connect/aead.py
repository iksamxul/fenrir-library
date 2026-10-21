"""ChaCha20-Poly1305 (RFC 8439), standard library only.

Fenrir's sealed store (core/store.py) encrypts every secret with it, and Fenrir Connect will keep its friend's key the
same way (docs/v3-design/store.md §3.2). It lives here, in Fenrir Connect's package, for the reason connect/ed25519.py
does: both apps need it, and neither has a crypto package at run time. Pure Python is slow, which does not matter for
what it does: secrets are a few bytes and are opened rarely. The tests check it against the RFC's own vectors and
against the cryptography package (tests/test_aead.py).
"""
import hmac
import struct

MASK = 0xFFFFFFFF
P1305 = (1 << 130) - 5
CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)  # "expand 32-byte k"
ROUNDS = ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),  # the columns, then the diagonals
          (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14))
TAG = 16


class AuthError(ValueError):
    """The ciphertext, its nonce or its associated data is not what was sealed: nothing is returned."""


def _block(state):
    x = list(state)
    for _ in range(10):
        for a, b, c, d in ROUNDS:
            xa = (x[a] + x[b]) & MASK
            xd = x[d] ^ xa
            xd = ((xd << 16) & MASK) | (xd >> 16)
            xc = (x[c] + xd) & MASK
            xb = x[b] ^ xc
            xb = ((xb << 12) & MASK) | (xb >> 20)
            xa = (xa + xb) & MASK
            xd ^= xa
            xd = ((xd << 8) & MASK) | (xd >> 24)
            xc = (xc + xd) & MASK
            xb ^= xc
            xb = ((xb << 7) & MASK) | (xb >> 25)
            x[a], x[b], x[c], x[d] = xa, xb, xc, xd
    return struct.pack("<16L", *((x[i] + state[i]) & MASK for i in range(16)))


def _check(key, nonce):
    if len(key) != 32:
        raise ValueError("A ChaCha20 key is 32 bytes.")
    if len(nonce) != 12:
        raise ValueError("A ChaCha20 nonce is 12 bytes.")


def chacha20_block(key, counter, nonce):
    """One 64-byte block of key stream (RFC 8439 §2.3)."""
    _check(key, nonce)
    return _block((*SIGMA, *struct.unpack("<8L", key), counter & MASK, *struct.unpack("<3L", nonce)))


def chacha20(key, counter, nonce, data):
    """data XORed with the key stream from block `counter` on (RFC 8439 §2.4): encrypting and decrypting are the same."""
    _check(key, nonce)
    data = bytes(data)
    if counter + (len(data) + 63) // 64 > 1 << 32:
        raise ValueError("Too much data for one ChaCha20 nonce.")  # the 32-bit block counter would wrap and repeat the stream
    head = (*SIGMA, *struct.unpack("<8L", key))
    tail = struct.unpack("<3L", nonce)
    out = bytearray()
    for i in range(0, len(data), 64):
        chunk = data[i:i + 64]
        stream = _block((*head, counter + i // 64, *tail))[:len(chunk)]
        out += (int.from_bytes(chunk, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(chunk), "little")
    return bytes(out)


def poly1305(key, msg):
    """The 16-byte Poly1305 tag of msg under a one-time 32-byte key (RFC 8439 §2.5)."""
    if len(key) != 32:
        raise ValueError("A Poly1305 key is 32 bytes.")
    r = int.from_bytes(key[:16], "little") & CLAMP
    s = int.from_bytes(key[16:], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        acc = (acc + int.from_bytes(msg[i:i + 16] + b"\x01", "little")) * r % P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def poly1305_key(key, nonce):
    """The one-time Poly1305 key for one nonce: the first half of block 0 (RFC 8439 §2.6)."""
    return chacha20_block(key, 0, nonce)[:32]


def _mac_input(aad, ct):
    return aad + bytes(-len(aad) % 16) + ct + bytes(-len(ct) % 16) + struct.pack("<QQ", len(aad), len(ct))


def encrypt(key, nonce, plaintext, aad=b""):
    """The ciphertext with its 16-byte tag after it (RFC 8439 §2.8). A nonce must never be used twice with one key:
    the store draws a random one for every write."""
    aad, ct = bytes(aad), chacha20(key, 1, nonce, plaintext)
    return ct + poly1305(poly1305_key(key, nonce), _mac_input(aad, ct))


def decrypt(key, nonce, data, aad=b""):
    """encrypt()'s plaintext, or AuthError when anything was changed, including the associated data."""
    _check(key, nonce)
    data, aad = bytes(data), bytes(aad)
    if len(data) < TAG:
        raise AuthError("Too short to be sealed data.")
    ct, tag = data[:-TAG], data[-TAG:]
    if not hmac.compare_digest(poly1305(poly1305_key(key, nonce), _mac_input(aad, ct)), tag):
        raise AuthError("The sealed data does not open with this key.")
    return chacha20(key, 1, nonce, ct)
