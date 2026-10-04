"""Encrypt/decrypt the stored Rivian session tokens (Fernet, key file is 0600)."""
import json
import os

from cryptography.fernet import Fernet

from config import KEY_PATH


def write_private(path, data: bytes) -> None:
    """Create/overwrite a secret file with mode 600 from the start."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)   # also fixes pre-existing files


def _key() -> bytes:
    if not KEY_PATH.exists():
        write_private(KEY_PATH, Fernet.generate_key())
    return KEY_PATH.read_bytes()


def seal(obj: dict) -> bytes:
    return Fernet(_key()).encrypt(json.dumps(obj).encode())


def unseal(blob: bytes) -> dict:
    return json.loads(Fernet(_key()).decrypt(blob).decode())
