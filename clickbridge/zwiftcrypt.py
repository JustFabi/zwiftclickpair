"""Legacy Zwift controller crypto (ECDH + HKDF + AES-CCM).

Older Zwift controller firmware (original Zwift Play, and Click units that
have not received the 2024+ plain-mode firmware) encrypt the controller
channel with a scheme close to "BLE Security Mode 1 Level 4":

  * ECDH key exchange on NIST P-256 (secp256r1),
  * HKDF-SHA256 to derive a 36-byte key (32-byte AES key + 4-byte nonce base),
  * AES-CCM with an 8-byte nonce and 4-byte MAC,
  * frames: [counter u32 LE][ciphertext...][MIC 4 bytes]

Everything here is derived from published reverse-engineering
(makinolo.com "Connecting to Zwift Play controllers") and is only used when
you explicitly run ClickBridge with ``--legacy-ecdh``.  The default path is
the plain "RideOn" protocol that all current Zwift controllers speak.

The pre-shared ECDH secret in the Zwift scheme is the full 64-byte shared
point (X || Y), not just the X coordinate, so we perform the scalar
multiplication with our own (compact, textbook) P-256 implementation and
cross-check it against the ``cryptography`` library in the test-suite.
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# NIST P-256 domain parameters
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _inv(x: int) -> int:
    return pow(x % _P, _P - 2, _P)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1) % _P
    else:
        lam = (y2 - y1) * _inv((x2 - x1) % _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _point_mul(scalar: int, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def scalar_mult_public(scalar: int) -> tuple[int, int]:
    """Public point of the given scalar for P-256 (test helper / cross-check)."""
    pt = _point_mul(scalar % _N, (_GX, _GY))
    if pt is None:
        raise ValueError("point at infinity")
    return pt


def scalar_mult_shared(scalar: int, other: tuple[int, int]):
    """Shared point Z = scalar * other on P-256."""
    pt = _point_mul(scalar % _N, other)
    if pt is None:
        raise ValueError("point at infinity")
    return pt


HANDSHAKE_MAGIC = b"RideOn"


def decode_device_public_key(payload: bytes) -> bytes | None:
    """Extract the 64-byte device public key (X||Y, no 0x04 prefix) from a
    "RideOn" public-key handshake frame.  Returns None if not a key frame."""
    if not payload.startswith(HANDSHAKE_MAGIC):
        return None
    key = payload[len(HANDSHAKE_MAGIC) + 2:]
    # Accept an unexpected compression prefix (0x04) defensively.
    if len(key) == 65 and key[0] == 0x04:
        key = key[1:]
    if len(key) == 32:  # would be a compressed key; not supported
        return None
    if len(key) != 64:
        return None
    # sanity: point must be on the curve
    x = int.from_bytes(key[:32], "big")
    y = int.from_bytes(key[32:], "big")
    try:
        ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + key)
    except ValueError:
        return None
    return key


class ZwiftCrypto:
    """Holds one negotiated session and encrypts/decrypts frames.

    ``salt_order`` selects the HKDF salt concatenation: "device-first" is the
    app-side convention from the reverse-engineering notes (device key
    material first, ours second).  Only same-salt instances interoperate;
    the unit-test uses the mirrored order to simulate the peer side.
    """

    def __init__(self, our_private: int, our_public: bytes, device_public: bytes,
                 salt_order: str = "device-first"):
        self.our_public = our_public
        self.device_public = device_public
        dev_x = int.from_bytes(device_public[:32], "big")
        dev_y = int.from_bytes(device_public[32:], "big")
        shared = scalar_mult_shared(our_private, (dev_x, dev_y))
        secret = shared[0].to_bytes(32, "big") + shared[1].to_bytes(32, "big")
        salt = (device_public + our_public if salt_order == "device-first"
                else our_public + device_public)
        derived = HKDF(
            algorithm=hashes.SHA256(), length=36, salt=salt, info=b""
        ).derive(secret)
        self.aes_key = derived[:32]
        self.nonce_base = derived[32:36]
        self.send_counter = 1
        self.rx_failures = 0

    @classmethod
    def generate(cls) -> tuple[int, bytes]:
        """Create a fresh app-side key pair.  Returns (scalar, raw_public)."""
        priv = ec.generate_private_key(ec.SECP256R1())
        nums = priv.public_key().public_numbers()
        raw = nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
        return priv.private_numbers().private_value, raw

    def handshake_frame(self) -> bytes:
        return HANDSHAKE_MAGIC + bytes((0x01, 0x02)) + self.our_public

    # --- framing --------------------------------------------------------
    def encrypt(self, plaintext: bytes) -> bytes:
        counter = self.send_counter & 0xFFFFFFFF
        self.send_counter = (self.send_counter + 1) & 0xFFFFFFFF or 1
        nonce = self.nonce_base + counter.to_bytes(4, "little")
        body = AESCCM(self.aes_key, tag_length=4).encrypt(nonce, plaintext, None)
        return counter.to_bytes(4, "little") + body

    def decrypt(self, frame: bytes) -> bytes | None:
        if len(frame) < 9:
            return None
        counter = frame[:4]
        try:
            plain = AESCCM(self.aes_key, tag_length=4).decrypt(
                self.nonce_base + counter, frame[4:], None
            )
            self.rx_failures = 0
            return plain
        except Exception:
            self.rx_failures += 1
            return None


def random_scalar() -> int:
    import secrets
    return secrets.randbits(252) + 1
