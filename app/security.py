import logging
import re

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(ValueError):
    pass


class CredentialCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise CredentialError("APP_SECRET_KEY 不是有效的 Fernet 密钥") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialError("无法使用当前 APP_SECRET_KEY 解密设备凭据") from exc


class SecretRedactingFilter(logging.Filter):
    _patterns = (
        re.compile(r"(?i)(password|passwd|authorization)(\s*[=:]\s*)([^\s,;]+)"),
        re.compile(r"(?i)(encrypted_password)(\s*[=:]\s*)([^\s,;]+)"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in self._patterns:
            message = pattern.sub(r"\1\2[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def safe_error_message(message: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = message
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned[:500]

