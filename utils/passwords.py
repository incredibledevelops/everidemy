import secrets
import string

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import current_app


RESET_TOKEN_MAX_AGE = 60 * 60 * 24 * 3   # 3 days
INVITE_TOKEN_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def generate_password(length: int = 10) -> str:
    """
    Cryptographically secure random password.
    Avoids 0/O/1/l/I to prevent transcription mistakes.
    """
    letters = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
    digits  = "23456789"
    symbols = "!@#$%"

    pwd = [
        secrets.choice(letters),
        secrets.choice(letters),
        secrets.choice(digits),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pool = letters + digits
    while len(pwd) < length:
        pwd.append(secrets.choice(pool))

    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


def _serializer(purpose: str):
    return URLSafeTimedSerializer(
        secret_key=current_app.config["SECRET_KEY"],
        salt=f"everidemy-{purpose}",
    )


def generate_reset_token(email: str) -> str:
    return _serializer("reset-password").dumps(email.lower().strip())


def verify_reset_token(token: str, max_age: int = RESET_TOKEN_MAX_AGE):
    try:
        return _serializer("reset-password").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def generate_invite_token(email: str) -> str:
    return _serializer("invite").dumps(email.lower().strip())


def verify_invite_token(token: str, max_age: int = INVITE_TOKEN_MAX_AGE):
    try:
        return _serializer("invite").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None