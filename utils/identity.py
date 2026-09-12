import hashlib
import hmac

from django.conf import settings


def email_digest(email: str) -> str:
    normalized_email = email.strip().casefold()
    return hmac.new(
        settings.SECRET_KEY.encode(),
        normalized_email.encode(),
        hashlib.sha256,
    ).hexdigest()
