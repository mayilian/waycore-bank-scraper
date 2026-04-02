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

_fernet: MultiFernet | None = None


def _get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is None:
        try:
            keys = [Fernet(settings.encryption_key.encode())]
        except Exception:
            raise ValueError(
                "ENCRYPTION_KEY is not a valid Fernet key. "
                'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        if settings.encryption_key_previous:
            keys.append(Fernet(settings.encryption_key_previous.encode()))
        _fernet = MultiFernet(keys)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
