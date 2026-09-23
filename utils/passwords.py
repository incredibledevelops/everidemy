"""
Password generation + reset-token helpers.

Uses itsdangerous (bundled with Flask) for signed, time-limited tokens.
"""
import random
import string

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import current_app


RESET_TOKEN_MAX_AGE = 60 * 60 * 24 * 3   # 3 days
INVITE_TOKEN_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


# ---------------------------------------------------------
# Password generation
# ---------------------------------------------------------
def generate_password(length: int = 10) -> str:
    """
    Human-friendly random password.
    Avoids 0/O/1/l/I to prevent transcription mistakes.
    Always contains at least one letter and one digit.
    """
    letters = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
    digits  = "23456789"
    symbols = "!@#$%"

    # Guarantee at least one of each
    pwd = [
        random.choice(letters),
        random.choice(letters),
        random.choice(digits),
        random.choice(digits),
        random.choice(symbols),
    ]
    # Pad to length from mixed pool
    pool = letters + digits
    while len(pwd) < length:
        pwd.append(random.choice(pool))

    random.shuffle(pwd)
    return "".join(pwd)


# ---------------------------------------------------------
# Reset tokens
# ---------------------------------------------------------
def _serializer(purpose: str):
    return URLSafeTimedSerializer(
        secret_key=current_app.config["SECRET_KEY"],
        salt=f"everidemy-{purpose}",
    )


def generate_reset_token(email: str) -> str:
    """Signed token for a password reset link."""
    return _serializer("reset-password").dumps(email.lower().strip())


def verify_reset_token(token: str, max_age: int = RESET_TOKEN_MAX_AGE):
    """Return the email or None if invalid/expired."""
    try:
        return _serializer("reset-password").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def generate_invite_token(email: str) -> str:
    """Signed token for a first-login invite link (longer expiry)."""
    return _serializer("invite").dumps(email.lower().strip())


def verify_invite_token(token: str, max_age: int = INVITE_TOKEN_MAX_AGE):
    try:
        return _serializer("invite").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None