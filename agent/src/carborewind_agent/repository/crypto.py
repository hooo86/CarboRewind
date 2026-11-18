"""Key hierarchy and authenticated encryption of repository objects.

Key hierarchy
-------------
* **KEK** (key-encryption key, 32 bytes): supplied by the operator (file readable only by the
  agent user, or a KMS later). Never stored in the repository.
* **Master key** (32 random bytes): generated at ``init``, stored in ``keys/master.json``
  wrapped (AES-256-GCM) by the KEK. Rotating the KEK only re-wraps this key.
* **Sub-keys** derived from the master key with HKDF-SHA256:
  ``enc`` (AES-256-GCM of objects) and ``id`` (HMAC-SHA256 naming of blobs).

Object format (version 1)
-------------------------
``b"CRB1" || nonce (12 bytes) || AES-256-GCM(enc, nonce, zstd(plaintext), aad)``.
The AAD binds the ciphertext to its location (blob id or manifest path), so a file swapped
with another one fails authentication. Nonces are random: with 96-bit nonces the collision
risk stays negligible below ~2^32 objects per key (NIST SP 800-38D).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

import zstandard
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"CRB1"
NONCE_SIZE = 12
KEY_SIZE = 32
_ZSTD_LEVEL = 6
_WRAP_AAD = b"carborewind/master-key/v1"


class IntegrityError(Exception):
    """Authentication failed: wrong key, corrupted or substituted object."""


def generate_key() -> bytes:
    return os.urandom(KEY_SIZE)


def encode_key(key: bytes) -> str:
    """Base64 (also used for nonces and sealed values in key files)."""
    return base64.b64encode(key).decode()


def decode_key(text: str) -> bytes:
    key = base64.b64decode(text.strip(), validate=True)
    if len(key) != KEY_SIZE:
        raise ValueError("key must be 32 bytes (base64-encoded)")
    return key


def wrap_master(kek: bytes, master: bytes) -> dict[str, str]:
    nonce = os.urandom(NONCE_SIZE)
    sealed = AESGCM(kek).encrypt(nonce, master, _WRAP_AAD)
    return {"alg": "AES-256-GCM", "nonce": encode_key(nonce), "sealed": encode_key(sealed)}


def unwrap_master(kek: bytes, wrapped: dict[str, str]) -> bytes:
    try:
        return AESGCM(kek).decrypt(
            base64.b64decode(wrapped["nonce"]), base64.b64decode(wrapped["sealed"]), _WRAP_AAD
        )
    except (InvalidTag, KeyError, ValueError) as exc:
        raise IntegrityError("cannot unwrap master key: wrong KEK or corrupted key file") from exc


def _derive(master: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_SIZE, salt=None, info=label).derive(master)


@dataclass(frozen=True)
class Keys:
    enc: bytes
    ident: bytes

    @classmethod
    def from_master(cls, master: bytes) -> Keys:
        return cls(
            enc=_derive(master, b"carborewind/enc/v1"),
            ident=_derive(master, b"carborewind/id/v1"),
        )

    def blob_id(self, plaintext: bytes) -> str:
        """Keyed content address: equal content -> equal id, without revealing a plain hash."""
        return hmac.new(self.ident, plaintext, hashlib.sha256).hexdigest()

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        compressed = zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(plaintext)
        return MAGIC + nonce + AESGCM(self.enc).encrypt(nonce, compressed, aad)

    def open(self, sealed: bytes, aad: bytes) -> bytes:
        if len(sealed) < len(MAGIC) + NONCE_SIZE + 16 or not sealed.startswith(MAGIC):
            raise IntegrityError("not a CarboRewind object (bad magic or truncated)")
        nonce = sealed[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
        try:
            compressed = AESGCM(self.enc).decrypt(nonce, sealed[len(MAGIC) + NONCE_SIZE :], aad)
        except InvalidTag as exc:
            raise IntegrityError("authentication failed") from exc
        return zstandard.ZstdDecompressor().decompress(compressed)
