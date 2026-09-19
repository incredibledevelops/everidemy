import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---------- Flask ----------
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 days

    # ---------- MongoDB ----------
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/everidemy")
    MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "everidemy")

    # ---------- Paystack ----------
    PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
    PAYSTACK_BASE_URL = "https://api.paystack.co"

    # ---------- Platform Subscription ----------
    PAYSTACK_PLAN_CODE = os.getenv("PAYSTACK_PLAN_CODE", "")
    PAYSTACK_PLATFORM_AMOUNT = int(os.getenv("PAYSTACK_PLATFORM_AMOUNT", "500"))
    PAYSTACK_PLATFORM_CURRENCY = os.getenv("PAYSTACK_PLATFORM_CURRENCY", "GHS")
    PAYSTACK_GRACE_DAYS = int(os.getenv("PAYSTACK_GRACE_DAYS", "3"))

    # ---------- Platform ----------
    PLATFORM_NAME = os.getenv("PLATFORM_NAME", "Everidemy")
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))

    # Currency
    CURRENCY = os.getenv("PLATFORM_CURRENCY", "GHS")
    CURRENCY_SYMBOL = os.getenv("PLATFORM_CURRENCY_SYMBOL", "GH₵")

    # ---------- Single Plan ----------
    PLAN_PRICE = PAYSTACK_PLATFORM_AMOUNT
    PLAN_NAME = "Everidemy Monthly"
    PLAN_KEY = "monthly"

    PLANS = {
        "monthly": {
            "name": PLAN_NAME,
            "price": PLAN_PRICE,
            "students": None,
            "color": "brand",
            "description": "Full access to every Everidemy feature — unlimited students, staff, and modules.",
        },
    }

    # ---------- Email ----------
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() == "true"
    MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "False").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "Everidemy <noreply@everidemy.com>")
    MAIL_SUPPRESS_SEND = os.getenv("MAIL_SUPPRESS_SEND", "False").lower() == "true"