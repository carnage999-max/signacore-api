from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import EmailValidator
from django.db import models


def get_fernet() -> Fernet:
    key = settings.FERNET_KEY
    if not key:
        raise ImproperlyConfigured("FERNET_KEY must be configured for encrypted fields.")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("FERNET_KEY must be a valid 32-byte url-safe base64-encoded key.") from exc


def encrypt_value(value: str | None) -> str | None:
    if value in (None, ""):
        return value
    return get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(value: str | None) -> str | None:
    if value in (None, ""):
        return value
    try:
        return get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return value


class EncryptedTextField(models.TextField):
    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_value(value)

    def from_db_value(self, value, expression, connection):
        return decrypt_value(value)

    def to_python(self, value):
        value = super().to_python(value)
        return decrypt_value(value)


class EncryptedEmailField(EncryptedTextField):
    default_validators = [EmailValidator()]
