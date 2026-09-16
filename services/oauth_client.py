from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from django.conf import settings

from utils.base_api_client import BaseAPIClient


class OAuthExchangeError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedOAuthIdentity:
    provider: str
    subject: str
    email: str
    display_name: str


def decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(decode_base64url(encoded_header))
        payload = json.loads(decode_base64url(encoded_payload))
        signature = decode_base64url(encoded_signature)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OAuthExchangeError("The identity provider returned an invalid token.") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise OAuthExchangeError("The identity provider returned an invalid token.")
    return header, payload, f"{encoded_header}.{encoded_payload}".encode(), signature


def validate_common_claims(
    claims: dict[str, Any],
    *,
    issuer: str,
    audience: str,
    nonce: str,
) -> None:
    token_audience = claims.get("aud")
    valid_audience = token_audience == audience or (isinstance(token_audience, list) and audience in token_audience)
    if claims.get("iss") != issuer or not valid_audience:
        raise OAuthExchangeError("The identity token was not issued for SignaCore.")
    if int(claims.get("exp") or 0) <= int(time.time()):
        raise OAuthExchangeError("The identity token has expired.")
    if not nonce or claims.get("nonce") != nonce:
        raise OAuthExchangeError("The identity token could not be matched to this sign-in.")


class GoogleOAuthClient(BaseAPIClient):
    def __init__(self) -> None:
        super().__init__("https://oauth2.googleapis.com")

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        return self.post(
            "/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        ).json()

    def verify_identity_token(self, identity_token: str, *, nonce: str) -> VerifiedOAuthIdentity:
        claims = self.get("/tokeninfo", params={"id_token": identity_token}).json()
        if claims.get("aud") != settings.GOOGLE_OAUTH_CLIENT_ID:
            raise OAuthExchangeError("The Google identity token was not issued for SignaCore.")
        if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise OAuthExchangeError("The Google identity token issuer is invalid.")
        if int(claims.get("exp") or 0) <= int(time.time()) or claims.get("nonce") != nonce:
            raise OAuthExchangeError("The Google identity token is expired or unmatched.")
        email_verified = claims.get("email_verified")
        if email_verified not in {True, "true"}:
            raise OAuthExchangeError("Google did not verify this email address.")
        subject = str(claims.get("sub") or "")
        email = str(claims.get("email") or "")
        if not subject or not email:
            raise OAuthExchangeError("Google did not return the required account details.")
        return VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject=subject,
            email=email,
            display_name=str(claims.get("name") or ""),
        )


class AppleOAuthClient(BaseAPIClient):
    def __init__(self) -> None:
        super().__init__("https://appleid.apple.com/auth")

    def create_client_secret(self) -> str:
        now = int(time.time())
        header = {"alg": "ES256", "kid": settings.APPLE_OAUTH_KEY_ID, "typ": "JWT"}
        claims = {
            "iss": settings.APPLE_OAUTH_TEAM_ID,
            "iat": now,
            "exp": now + 86400,
            "aud": "https://appleid.apple.com",
            "sub": settings.APPLE_OAUTH_CLIENT_ID,
        }
        encoded_header = encode_base64url(json.dumps(header, separators=(",", ":")).encode())
        encoded_claims = encode_base64url(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{encoded_header}.{encoded_claims}".encode()
        try:
            private_key = serialization.load_pem_private_key(
                settings.APPLE_OAUTH_PRIVATE_KEY.encode(),
                password=None,
            )
            der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        except (TypeError, ValueError) as exc:
            raise OAuthExchangeError("Apple sign-in is not configured correctly.") from exc
        r_value, s_value = decode_dss_signature(der_signature)
        signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        return f"{encoded_header}.{encoded_claims}.{encode_base64url(signature)}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        return self.post(
            "/token",
            data={
                "code": code,
                "client_id": settings.APPLE_OAUTH_CLIENT_ID,
                "client_secret": self.create_client_secret(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        ).json()

    def verify_identity_token(self, identity_token: str, *, nonce: str) -> VerifiedOAuthIdentity:
        header, claims, signing_input, signature = decode_jwt_parts(identity_token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise OAuthExchangeError("The Apple identity token uses an unsupported signature.")
        key_set = self.get("/keys").json()
        matching_key = next(
            (key for key in key_set.get("keys", []) if isinstance(key, dict) and key.get("kid") == header["kid"]),
            None,
        )
        if not matching_key:
            raise OAuthExchangeError("Apple's signing key could not be found.")
        try:
            modulus = int.from_bytes(decode_base64url(str(matching_key["n"])), "big")
            exponent = int.from_bytes(decode_base64url(str(matching_key["e"])), "big")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
            raise OAuthExchangeError("The Apple identity token signature is invalid.") from exc
        validate_common_claims(
            claims,
            issuer="https://appleid.apple.com",
            audience=settings.APPLE_OAUTH_CLIENT_ID,
            nonce=nonce,
        )
        subject = str(claims.get("sub") or "")
        email = str(claims.get("email") or "")
        if not subject or not email:
            raise OAuthExchangeError("Apple did not return the required account details.")
        return VerifiedOAuthIdentity(
            provider="APPLE",
            subject=subject,
            email=email,
            display_name="",
        )


def exchange_oauth_code(
    *,
    provider: str,
    code: str,
    redirect_uri: str,
    nonce: str,
) -> VerifiedOAuthIdentity:
    if provider == "GOOGLE":
        client = GoogleOAuthClient()
    elif provider == "APPLE":
        client = AppleOAuthClient()
    else:
        raise OAuthExchangeError("Unsupported identity provider.")
    token_payload = client.exchange_code(code=code, redirect_uri=redirect_uri)
    identity_token = str(token_payload.get("id_token") or "")
    if not identity_token:
        raise OAuthExchangeError("The identity provider did not return an identity token.")
    return client.verify_identity_token(identity_token, nonce=nonce)
