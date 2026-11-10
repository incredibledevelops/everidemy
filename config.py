import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int = 0) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _env_str(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return val if val is not None else default


class Config:
    # ---------- Flask ----------
    SECRET_KEY = _env_str("SECRET_KEY")

    SESSION_COOKIE_SECURE   = _env_bool("SESSION_COOKIE_SECURE")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)

    PREFERRED_URL_SCHEME = _env_str("PREFERRED_URL_SCHEME")
    JSON_SORT_KEYS = False

    # ---------- MongoDB ----------
    MONGO_URI     = _env_str("MONGO_URI")
    MONGO_DB_NAME = _env_str("MONGO_DB_NAME")

    # ---------- Paystack ----------
    PAYSTACK_SECRET_KEY = _env_str("PAYSTACK_SECRET_KEY")
    PAYSTACK_PUBLIC_KEY = _env_str("PAYSTACK_PUBLIC_KEY")
    PAYSTACK_BASE_URL   = _env_str("PAYSTACK_BASE_URL", "https://api.paystack.co")

    # ---------- Platform Subscription ----------
    PAYSTACK_PLATFORM_AMOUNT   = _env_int("PAYSTACK_PLATFORM_AMOUNT", 500)
    PAYSTACK_PLATFORM_CURRENCY = _env_str("PAYSTACK_PLATFORM_CURRENCY", "GHS")

    TRIAL_DAYS          = _env_int("TRIAL_DAYS", 0)
    PAYSTACK_GRACE_DAYS = _env_int("PAYSTACK_GRACE_DAYS", 0)

    # ---------- Platform ----------
    PLATFORM_NAME = _env_str("PLATFORM_NAME", "Everidemy")

    CURRENCY        = _env_str("PLATFORM_CURRENCY", "GHS")
    CURRENCY_SYMBOL = _env_str("PLATFORM_CURRENCY_SYMBOL", "GH₵")

    # ---------- Single Plan ----------
    PLAN_PRICE = PAYSTACK_PLATFORM_AMOUNT
    PLAN_NAME  = "Everidemy Monthly"
    PLAN_KEY   = "monthly"

    PLANS = {
        "monthly": {
            "name":        PLAN_NAME,
            "price":       PLAN_PRICE,
            "students":    None,
            "color":       "brand",
            "description": (
                "Full access to every Everidemy feature — "
                "unlimited students, staff, and modules."
            ),
        },
    }

    # ---------- Resend (Email) ----------
    RESEND_API_KEY     = _env_str("RESEND_API_KEY")
    RESEND_FROM_EMAIL  = _env_str("RESEND_FROM_EMAIL", "Everidemy <noreply@everidemy.com>")
    RESEND_FROM_NAME   = _env_str("RESEND_FROM_NAME", "Everidemy")

    # ---------- Arkesel (SMS) ----------
    ARKESEL_API_KEY    = _env_str("ARKESEL_API_KEY")
    ARKESEL_SENDER_ID  = _env_str("ARKESEL_SENDER_ID", "Everidemy")
    ARKESEL_BASE_URL   = _env_str("ARKESEL_BASE_URL", "https://sms.arkesel.com/api/v2")

    # ---------- Legacy Email (SMTP — kept for backward compat, not used) ----------
    MAIL_SERVER         = _env_str("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT           = _env_int("MAIL_PORT", 587)
    MAIL_USE_TLS        = _env_bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL        = _env_bool("MAIL_USE_SSL", False)
    MAIL_USERNAME       = _env_str("MAIL_USERNAME", "")
    MAIL_PASSWORD       = _env_str("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = _env_str("MAIL_DEFAULT_SENDER", "Everidemy <noreply@everidemy.com>")
    MAIL_SUPPRESS_SEND  = _env_bool("MAIL_SUPPRESS_SEND", False)
    MAIL_TIMEOUT        = _env_int("MAIL_TIMEOUT", 20)