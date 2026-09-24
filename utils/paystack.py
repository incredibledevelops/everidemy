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
    """
    Returns (status_code, response_dict).

    Unlike a naive wrapper, this returns the full body even on error
    so callers can log Paystack's exact message.
    """
    try:
        resp = requests.post(
            _url(path), json=payload,
            headers=_headers(secret_key), timeout=20,
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body
    except Exception as e:
        return 0, {"error": str(e)}


def _get(path, secret_key=None):
    try:
        resp = requests.get(
            _url(path),
            headers=_headers(secret_key), timeout=20,
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body
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
    try:
        from flask import current_app
        current_app.logger.error(
            f"[paystack.create_customer] HTTP {status} — {data}"
        )
    except Exception:
        pass
    return None


# =========================================================
# TRANSACTION (one-time charge)
# =========================================================
def initialize_transaction(email: str, amount_kobo: int, reference: str,
                            callback_url: str, metadata: dict | None = None,
                            currency: str | None = None) -> dict | None:
    """
    Initialize a ONE-TIME transaction. Returns the Paystack payload including
    `authorization_url` and `access_code`.

    No `plan` is passed — schools pay month by month manually.
    """
    payload = {
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "callback_url": callback_url,
        "currency": currency or Config.PAYSTACK_PLATFORM_CURRENCY,
    }
    if metadata:
        payload["metadata"] = metadata

    status, data = _post("/transaction/initialize", payload)

    # Always log what came back so billing.checkout debug output
    # includes Paystack's own error message.
    try:
        from flask import current_app
        if status in (200, 201) and data.get("status"):
            current_app.logger.info(
                f"[paystack.initialize_transaction] OK ref={reference} "
                f"auth_url={data.get('data', {}).get('authorization_url')}"
            )
        else:
            current_app.logger.error(
                f"[paystack.initialize_transaction] HTTP {status} — "
                f"payload_sent={payload} response={data}"
            )
    except Exception:
        pass

    if status in (200, 201) and data.get("status"):
        return data.get("data")
    return None


def verify_transaction(reference: str) -> dict | None:
    """Verify a transaction by reference. Returns the transaction data."""
    status, data = _get(f"/transaction/verify/{reference}")
    if status == 200 and data.get("status") and data.get("data"):
        return data["data"]
    try:
        from flask import current_app
        current_app.logger.error(
            f"[paystack.verify_transaction] HTTP {status} ref={reference} "
            f"response={data}"
        )
    except Exception:
        pass
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