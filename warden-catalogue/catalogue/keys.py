"""The catalogue's own signing keys.

OpenMetadata signs its sign-in and bot tokens with an RSA key. The image ships
one in ./conf, the same in every install, so anyone could mint an admin token
for a catalogue left on it. This writes a fresh pair once, into a volume only
the catalogue server mounts; later runs keep it, so issued tokens stay valid."""
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PRIVATE, PUBLIC = "private_key.der", "public_key.der"


def ensure(directory: str) -> bool:
    """Creates the key pair if it isn't there. Returns True when it made a new one."""
    folder = Path(directory)
    private, public = folder / PRIVATE, folder / PUBLIC
    if private.exists() and public.exists():
        return False
    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private.write_bytes(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.DER,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo))
    # Readable by the server's unprivileged user. Only the catalogue server mounts this volume.
    os.chmod(private, 0o644)
    os.chmod(public, 0o644)
    return True
