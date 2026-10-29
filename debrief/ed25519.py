"""Ed25519 signature check in pure Python (RFC 8032, section 5.1.7), used by the in-app updater.

    verify(public_key: bytes, message: bytes, signature: bytes) -> bool

The installed app needs no extra packages for this (the build leaves out `cryptography`). This module
only CHECKS signatures; it never holds a secret. It is a straight port of the RFC's reference code with
Python ints and SHA-512 from hashlib, and it refuses:
  * a public key that isn't 32 bytes or a signature that isn't 64 bytes,
  * a non-canonical point encoding (y >= p, or x = 0 with the sign bit set) or a point not on the curve,
  * S >= L (a non-canonical S; this stops signature malleability).
One check takes about 10-50 ms in CPython.

Nothing here runs on import except building the curve constants. No network, no files.
"""
from __future__ import annotations

import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _recover_x(y: int, sign: int):
    """RFC 8032 5.1.3 step 2-4: x from y and the sign bit, or None if there is no such point."""
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def point_add(a, b):
    """Extended twisted Edwards coordinates (X, Y, Z, T), RFC 8032 5.1.4."""
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % P
    B = (y1 + x1) * (y2 + x2) % P
    C = 2 * t1 * t2 * D % P
    DD = 2 * z1 * z2 % P
    E, F, G, H = B - A, DD - C, DD + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def point_mul(k: int, pt):
    q = (0, 1, 1, 0)
    while k:
        if k & 1:
            q = point_add(q, pt)
        pt = point_add(pt, pt)
        k >>= 1
    return q


def point_equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def point_encode(pt) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def point_decode(s: bytes):
    """RFC 8032 5.1.3. None for a wrong length, a non-canonical encoding or a point off the curve."""
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid Ed25519 signature of message by public_key (RFC 8032 5.1.7)."""
    try:
        public_key, signature, message = bytes(public_key), bytes(signature), bytes(message)
    except TypeError:
        return False
    if len(public_key) != 32 or len(signature) != 64:
        return False
    A = point_decode(public_key)
    R = point_decode(signature[:32])
    if A is None or R is None:
        return False
    S = int.from_bytes(signature[32:], "little")
    if S >= L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % L
    return point_equal(point_mul(S, G), point_add(R, point_mul(k, A)))
