import pytest

from app.security import CredentialCipher, CredentialError, safe_error_message


def test_cipher_round_trip(valid_key):
    cipher = CredentialCipher(valid_key)
    token = cipher.encrypt("S3cret!")
    assert token != "S3cret!"
    assert cipher.decrypt(token) == "S3cret!"


def test_cipher_rejects_invalid_key():
    with pytest.raises(CredentialError):
        CredentialCipher("not-a-fernet-key")


def test_safe_error_redacts_password():
    assert "S3cret!" not in safe_error_message("login S3cret! failed", ("S3cret!",))

