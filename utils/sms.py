"""
SMS helpers for Everidemy using Arkesel.

All sends are wrapped in try/except so a broken API key never crashes a request.
Every helper returns a tuple: (ok: bool, error: str | None)
"""
from flask import current_app

from config import Config
from extensions import sms


def send_sms(recipients: list[str], message: str, sender: str | None = None):
    """
    Send an SMS via Arkesel.
    Returns (ok, error).
    """
    if not recipients:
        return False, "No recipients"

    if not Config.ARKESEL_API_KEY:
        return False, "ARKESEL_API_KEY not configured"

    ok, body = sms.send_sms(recipients, message, sender)
    if ok:
        try:
            current_app.logger.info(f"[arkesel] Sent SMS to {len(recipients)} recipient(s)")
        except Exception:
            pass
        return True, None

    try:
        current_app.logger.error(f"[arkesel] SMS failed: {body}")
    except Exception:
        pass
    return False, str(body)


# Convenience wrappers for common SMS use cases
def send_fee_reminder_sms(phone: str, school_name: str, student_name: str, balance: float):
    sym = Config.CURRENCY_SYMBOL
    message = (
        f"Dear parent, {student_name}'s fees at {school_name} have an outstanding "
        f"balance of {sym} {balance:,.2f}. Please pay soon to avoid disruption."
    )
    return send_sms([phone], message)


def send_attendance_alert_sms(phone: str, school_name: str, student_name: str, status: str):
    message = (
        f"Dear parent, {student_name} was marked {status} at {school_name} today. "
        f"Please contact the school if this is unexpected."
    )
    return send_sms([phone], message)