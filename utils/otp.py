from __future__ import annotations

import random
import string

from django.contrib.auth.hashers import check_password, make_password


def generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def hash_otp(otp: str) -> str:
    return make_password(otp)


def verify_otp(otp: str, otp_hash: str) -> bool:
    return check_password(otp, otp_hash)
