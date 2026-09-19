"""
Everidemy — Paystack API client.

Thin wrapper around Paystack's REST API.
All calls are server-side. Secrets never leave this module.
"""
import hashlib
import hmac

import requests

from config import Config


# =========================================================
# LOW-LEVEL HTTP
# =========================================================
def _headers(secret_key: str | None = None):
    key = secret_key or Config.PAYSTACK_SECRET_KEY
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _url(path):
    return f"{Config.PAYSTACK_BASE_URL}{path}"


def _post(path, payload, secret_key=None):
    try:
        resp = requests.post(
            _url(path), json=payload,
            headers=_headers(secret_key), timeout=20,
        )
        return resp.status_code, resp.json()
    except Exception as e:
        return 0, {"error": str(e)}


def _get(path, secret_key=None):
    try:
        resp = requests.get(
            _url(path),
            headers=_headers(secret_key), timeout=20,
        )
        return resp.status_code, resp.json()
    except Exception as e:
        return 0, {"error": str(e)}


# =========================================================
# SIGNATURE VERIFICATION
# =========================================================
def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    Verify a Paystack webhook payload.
    `raw_body` MUST be the raw bytes (not parsed JSON).
    `signature` is the value of the x-paystack-signature header.
    """
    if not signature or not Config.PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(
        Config.PAYSTACK_SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# =========================================================
# CUSTOMER
# =========================================================
def create_customer(email: str, first_name: str = "", last_name: str = "",
                    phone: str = "", metadata: dict | None = None) -> dict | None:
    """
    Create (or fetch) a Paystack customer.
    Idempotent — Paystack returns the existing customer if the email already exists.
    """
    payload = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
    }
    if phone:
        payload["phone"] = phone
    if metadata:
        payload["metadata"] = metadata

    status, data = _post("/customer", payload)
    if status in (200, 201) and data.get("status"):
        return data.get("data")
    return None


# =========================================================
# SUBSCRIPTION
# =========================================================
def start_subscription(customer_code: str, plan_code: str,
                       authorization_code: str | None = None) -> dict | None:
    """Subscribe a customer to a Paystack plan."""
    payload = {"customer": customer_code, "plan": plan_code}
    if authorization_code:
        payload["authorization"] = authorization_code

    status, data = _post("/subscription", payload)
    if status in (200, 201) and data.get("status"):
        return data.get("data")
    return None


def disable_subscription(subscription_code: str, email_token: str) -> bool:
    """Cancel an active subscription."""
    status, data = _post("/subscription/disable", {
        "code": subscription_code,
        "token": email_token,
    })
    return status in (200, 201) and data.get("status", False)


# =========================================================
# TRANSACTION
# =========================================================
def initialize_transaction(email: str, amount_kobo: int, reference: str,
                            callback_url: str, metadata: dict | None = None,
                            plan_code: str | None = None,
                            currency: str | None = None) -> dict | None:
    """
    Initialize a transaction. Returns the Paystack payload including
    `authorization_url` and `access_code`.
    """
    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "callback_url": callback_url,
        "currency": currency or Config.PAYSTACK_PLATFORM_CURRENCY,
    }
    if plan_code:
        payload["plan"] = plan_code
    if metadata:
        payload["metadata"] = metadata

    status, data = _post("/transaction/initialize", payload)
    if status in (200, 201) and data.get("status"):
        return data.get("data")
    return None


def verify_transaction(reference: str) -> dict | None:
    """Verify a transaction by reference. Returns the transaction data."""
    status, data = _get(f"/transaction/verify/{reference}")
    if status == 200 and data.get("status") and data.get("data"):
        return data["data"]
    return None


def verify_transaction_with_secret(reference: str, secret_key: str) -> dict | None:
    """
    Verify a transaction using a specific secret key.
    Useful when different schools have their own Paystack accounts.
    """
    if not secret_key:
        return None
    status, data = _get(f"/transaction/verify/{reference}", secret_key=secret_key)
    if status == 200 and data.get("status") and data.get("data"):
        return data["data"]
    return None