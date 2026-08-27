from __future__ import annotations

import base64
import secrets

import pytest
from taskdeck_core.im.wecom.crypto import (
    WecomCryptoError,
    compute_signature,
    decrypt,
    encrypt,
    verify_signature,
)


# Generate a valid AES key for round-trip tests: 32 random bytes, then
# take the first 43 chars of its base64 (WeCom convention: 43-char key
# corresponds to 44 with "=" padding = 32 bytes).
def _make_key() -> str:
    raw = secrets.token_bytes(32)
    return base64.b64encode(raw).decode()[:43]


def test_signature_matches_sorted_sha1():
    sig = compute_signature("tok", "1", "n", "ABC")
    # Compute the expected manually: sorted -> ["1", "ABC", "n", "tok"]
    import hashlib
    expected = hashlib.sha1(b"1ABCntok").hexdigest()
    assert sig == expected


def test_verify_signature_good_and_bad():
    sig = compute_signature("tok", "t", "n", "enc")
    assert verify_signature("tok", "t", "n", "enc", sig)
    assert not verify_signature("tok", "t", "n", "enc", "00")
    assert not verify_signature("tok", "t", "n", "enc", sig.replace("a", "b"))


def test_encrypt_decrypt_roundtrip():
    key = _make_key()
    msg = "<xml><Content>hello 世界</Content></xml>"
    receive_id = "wxCorp123"
    encrypted = encrypt(key, msg, receive_id)
    back = decrypt(key, encrypted, receive_id)
    assert back == msg


def test_decrypt_with_wrong_receive_id_rejects():
    key = _make_key()
    encrypted = encrypt(key, "hello", "corp-A")
    with pytest.raises(WecomCryptoError):
        decrypt(key, encrypted, "corp-B")


def test_decrypt_invalid_base64():
    key = _make_key()
    with pytest.raises(WecomCryptoError):
        decrypt(key, "!!!notbase64!!!", "corp")


def test_decrypt_short_ciphertext():
    key = _make_key()
    bad = base64.b64encode(b"short").decode()
    with pytest.raises(WecomCryptoError):
        decrypt(key, bad, "corp")


def test_decrypt_wrong_key_fails():
    k1 = _make_key()
    k2 = _make_key()
    encrypted = encrypt(k1, "hello", "corp")
    with pytest.raises(WecomCryptoError):
        decrypt(k2, encrypted, "corp")


def test_derive_key_invalid_base64():
    with pytest.raises(WecomCryptoError):
        decrypt("not_a_valid_b64_key", "whatever", "corp")
