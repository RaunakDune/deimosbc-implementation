"""Ed25519 identities.

The paper's *Register* step assigns "a pair of cryptographic keys" to every
participant. A node's on-chain identity is the hex encoding of its raw public
key.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .encoding import canonical_json


@dataclass
class KeyPair:
    private: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        """Deterministic key from a 32-byte seed (used for reproducible simulations)."""
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def public_hex(self) -> str:
        raw = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return raw.hex()

    def sign(self, message: bytes) -> str:
        return self.private.sign(message).hex()

    def sign_obj(self, obj) -> str:
        return self.sign(canonical_json(obj))


def verify(public_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(signature_hex), message
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_obj(public_hex: str, obj, signature_hex: str) -> bool:
    return verify(public_hex, canonical_json(obj), signature_hex)
