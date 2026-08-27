from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from taskdeck_core.auth.session import (
    SessionEncryptionError,
    decrypt,
    encrypt,
    make_fernet,
)


def test_make_fernet_rejects_empty() -> None:
    with pytest.raises(SessionEncryptionError):
        make_fernet("")


def test_make_fernet_rejects_malformed() -> None:
    with pytest.raises(SessionEncryptionError):
        make_fernet("not-a-valid-fernet-key")


def test_encrypt_decrypt_roundtrip() -> None:
    f = make_fernet(Fernet.generate_key().decode())
    cipher = encrypt(f, "supersecret-token")
    assert isinstance(cipher, bytes)
    assert b"supersecret-token" not in cipher  # not plaintext
    assert decrypt(f, cipher) == "supersecret-token"


def test_decrypt_with_wrong_key_raises() -> None:
    a = make_fernet(Fernet.generate_key().decode())
    b = make_fernet(Fernet.generate_key().decode())
    cipher = encrypt(a, "x")
    with pytest.raises(SessionEncryptionError):
        decrypt(b, cipher)


def test_decrypt_garbage_raises() -> None:
    f = make_fernet(Fernet.generate_key().decode())
    with pytest.raises(SessionEncryptionError):
        decrypt(f, b"definitely-not-a-fernet-token")
