"""RSA keypair helpers for ActivityPub actor signing keys.

Every local actor exposed by the bridge (one per linked Matrix "Profile Room")
needs its own RSA keypair: the public half is published on the Actor object,
the private half signs outgoing activities (see ``bridge.activitypub.signatures``).
"""

from __future__ import annotations

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

KEY_SIZE_BITS = 2048


def generate_keypair() -> tuple[str, str]:
    """Generate a new RSA keypair, returned as ``(private_pem, public_pem)`` strings."""
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=KEY_SIZE_BITS, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def load_private_key(pem: str) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(
        pem.encode("utf-8"), password=None, backend=default_backend()
    )
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("PEM does not contain an RSA private key")
    return key


def load_public_key(pem: str) -> RSAPublicKey:
    key = serialization.load_pem_public_key(pem.encode("utf-8"), backend=default_backend())
    if not isinstance(key, RSAPublicKey):
        raise ValueError("PEM does not contain an RSA public key")
    return key
