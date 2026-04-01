"""Tests for Fernet encryption/decryption."""

from src.core.crypto import decrypt, encrypt


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "s3cr3t_p@ssw0rd!"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_encrypt_produces_different_ciphertexts() -> None:
    plaintext = "same_input"
    c1 = encrypt(plaintext)
    c2 = encrypt(plaintext)
    # Fernet includes a timestamp, so same plaintext produces different ciphertext
    assert c1 != c2
    assert decrypt(c1) == decrypt(c2) == plaintext
