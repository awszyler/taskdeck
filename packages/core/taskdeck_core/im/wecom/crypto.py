from __future__ import annotations

import base64
import hashlib
import secrets
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WecomCryptoError(ValueError):
    pass


def compute_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """Spec: sha1 of sorted([token, timestamp, nonce, encrypted]) concatenated."""
    parts = sorted([token, timestamp, nonce, encrypted])
    return hashlib.sha1("".join(parts).encode()).hexdigest()


def verify_signature(
    token: str, timestamp: str, nonce: str, encrypted: str, signature: str
) -> bool:
    expected = compute_signature(token, timestamp, nonce, encrypted)
    return secrets.compare_digest(expected, signature)


def _derive_key(encoding_aes_key: str) -> bytes:
    """43-char base64 → 32-byte AES key. Pad to 44 chars for b64 decoding."""
    try:
        key = base64.b64decode(encoding_aes_key + "=")
    except Exception as e:
        raise WecomCryptoError(f"invalid encoding_aes_key: {e}") from e
    if len(key) != 32:
        raise WecomCryptoError(f"aes key must decode to 32 bytes (got {len(key)})")
    return key


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WecomCryptoError("empty padding")
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise WecomCryptoError(f"invalid pad length {pad}")
    return data[:-pad]


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    pad = block_size - (len(data) % block_size)
    if pad == 0:
        pad = block_size
    return data + bytes([pad]) * pad


def decrypt(encoding_aes_key: str, encrypted_b64: str, expected_receive_id: str) -> str:
    """Decrypt a base64-encoded WeCom ciphertext and return the inner message string.

    Layout of plaintext after AES decrypt + unpad:
        random_16 | msg_len (4 bytes, network byte order) | msg | receive_id
    """
    key = _derive_key(encoding_aes_key)
    iv = key[:16]
    try:
        ct = base64.b64decode(encrypted_b64)
    except Exception as e:
        raise WecomCryptoError(f"invalid base64: {e}") from e
    if len(ct) % 32 != 0:
        raise WecomCryptoError("ciphertext length not a multiple of 32")

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    plain = _pkcs7_unpad(padded)

    if len(plain) < 20:
        raise WecomCryptoError("plaintext too short")
    # Skip 16-byte random prefix.
    rest = plain[16:]
    (msg_len,) = struct.unpack(">I", rest[:4])
    rest = rest[4:]
    if msg_len < 0 or msg_len > len(rest):
        raise WecomCryptoError(f"bad msg_len {msg_len} vs rest={len(rest)}")
    msg = rest[:msg_len]
    receive_id = rest[msg_len:]
    if receive_id.decode(errors="replace") != expected_receive_id:
        raise WecomCryptoError("receive_id mismatch")
    return msg.decode("utf-8")


def encrypt(encoding_aes_key: str, msg: str, receive_id: str) -> str:
    """Encrypt a plain message for sending back to WeCom. Not used for M4.1's
    GET-only handshake but tested alongside for round-trip correctness."""
    key = _derive_key(encoding_aes_key)
    iv = key[:16]
    random_prefix = secrets.token_bytes(16)
    msg_bytes = msg.encode("utf-8")
    receive_bytes = receive_id.encode("utf-8")
    length = struct.pack(">I", len(msg_bytes))
    payload = random_prefix + length + msg_bytes + receive_bytes
    padded = _pkcs7_pad(payload)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ct).decode()
