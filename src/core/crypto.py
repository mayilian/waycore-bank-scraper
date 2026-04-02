"""Fernet symmetric encryption for credentials at rest.

Supports key rotation via MultiFernet: encrypts with the current key,
decrypts by trying current then previous. To rotate:
  1. Set ENCRYPTION_KEY=new, ENCRYPTION_KEY_PREVIOUS=old
  2. Deploy. Old ciphertexts still decrypt.
  3. Optionally re-encrypt all stored credentials.
  4. Remove ENCRYPTION_KEY_PREVIOUS.
"""

from cryptography.fernet import Fernet, MultiFernet

from src.core.config import settings


def _build_fernet() -> MultiFernet:
    keys = [Fernet(settings.encryption_key.encode())]
    if settings.encryption_key_previous:
        keys.append(Fernet(settings.encryption_key_previous.encode()))
    return MultiFernet(keys)


_fernet = _build_fernet()


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
