from __future__ import annotations

import hashlib

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner


SIGNER_SESSION_SALT = "signacore.signer.session"


def _session_value(signing_request_id: str, otp_hash: str) -> str:
    otp_fingerprint = hashlib.sha256(otp_hash.encode("utf-8")).hexdigest()
    return f"{signing_request_id}:{otp_fingerprint}"


def build_signer_session_token(signing_request_id: str, otp_hash: str) -> str:
    signer = TimestampSigner(salt=SIGNER_SESSION_SALT)
    return signer.sign(_session_value(signing_request_id, otp_hash))


def verify_signer_session_token(
    token: str,
    signing_request_id: str,
    otp_hash: str,
    max_age_seconds: int = 3600,
) -> bool:
    if not token or not otp_hash:
        return False
    signer = TimestampSigner(salt=SIGNER_SESSION_SALT)
    try:
        value = signer.unsign(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return value == _session_value(signing_request_id, otp_hash)
