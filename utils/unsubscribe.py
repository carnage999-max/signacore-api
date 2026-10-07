"""Leaving the follow-up list without having an account to log in to.

The link has to work for somebody who has only ever been a recipient, so it carries the address
itself rather than an identifier that would need a row to exist beforehand. Signed, so the link
can only unsubscribe the address it was issued for, and long-lived, because an old email is
exactly where somebody finds it.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_SIGNER = TimestampSigner(salt="signacore.unsubscribe")

# Two years. The whole point is an email found long after it arrived.
UNSUBSCRIBE_LINK_MAX_AGE_SECONDS = 60 * 60 * 24 * 730


def build_unsubscribe_token(email: str) -> str:
    return _SIGNER.sign(email.strip().lower())


def read_unsubscribe_token(token: str) -> str | None:
    """The address the token was issued for, or None if it was not issued by us."""
    try:
        return _SIGNER.unsign(token, max_age=UNSUBSCRIBE_LINK_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
