from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner


SIGNER_SESSION_SALT = "signacore.signer.session"


def build_signer_session_token(signing_request_id: str) -> str:
    signer = TimestampSigner(salt=SIGNER_SESSION_SALT)
    return signer.sign(signing_request_id)


def verify_signer_session_token(token: str, signing_request_id: str, max_age_seconds: int = 3600) -> bool:
    signer = TimestampSigner(salt=SIGNER_SESSION_SALT)
    try:
        value = signer.unsign(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return value == signing_request_id
