from __future__ import annotations

import hmac

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_SIGNER = TimestampSigner(salt="signacore.email-verification")


def make_email_verification_token(user_id: int) -> str:
    return _SIGNER.sign(str(user_id))


def check_email_verification_token(token: str, user_id: str) -> bool:
    try:
        signed_user_id = _SIGNER.unsign(token, max_age=settings.PASSWORD_RESET_TIMEOUT)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(signed_user_id, str(user_id))
