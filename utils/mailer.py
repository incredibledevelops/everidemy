"""
Email helpers for Everidemy.

All sends are wrapped in try/except so a broken SMTP never crashes a request.
Every helper returns a tuple: (ok: bool, error: str | None)
"""
from flask import current_app
from flask_mail import Message

from config import Config
from extensions import mail


# =====================================================================
# BASE TEMPLATE
# =====================================================================
_BASE = """
<div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:0 auto;background:#ffffff;">
  <div style="background:#2036e0;padding:24px;text-align:center;">
    <h1 style="color:#ffffff;margin:0;font-size:22px;">Everidemy</h1>
    <p style="color:#dbe6ff;margin:4px 0 0;font-size:12px;">Modern School Management</p>
  </div>
  <div style="padding:32px 24px;color:#0b1020;line-height:1.6;">
    {body}
  </div>
  <div style="background:#f8fafc;padding:16px;text-align:center;color:#64748b;font-size:12px;">
    © 2025 Everidemy
  </div>
</div>
"""


def _wrap(body_html: str) -> str:
    """Wrap a body fragment in the standard Everidemy email shell."""
    return _BASE.format(body=body_html)


# =====================================================================
# LOW-LEVEL SEND
# =====================================================================
def _send(subject: str, recipients: list, html: str, text: str = None):
    """
    Send an email. Returns (ok, error).
    Never raises — a bad SMTP config shouldn't crash the request.
    """
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return False, "No recipients"

    try:
        msg = Message(
            subject=subject,
            recipients=recipients,
            html=html,
            body=text or html,
        )
        mail.send(msg)
        try:
            current_app.logger.info(f"[mail] Sent '{subject}' → {recipients}")
        except Exception:
            pass
        return True, None

    except Exception as e:
        try:
            current_app.logger.error(f"[mail] Failed '{subject}' → {recipients}: {e}")
        except Exception:
            pass
        return False, str(e)


# =====================================================================
# PUBLIC HELPERS
# =====================================================================
def send_welcome_email(school_name: str, owner_name: str, email: str):
    """Sent when a school signs up or is created by the super admin."""
    body = f"""
      <h2 style="margin-top:0;">Welcome to Everidemy, {owner_name}! 🎉</h2>
      <p>Your school <strong>{school_name}</strong> is now live on Everidemy.</p>
      <p>You have a <strong>14-day free trial</strong>. No payment required until it ends.</p>
      <p style="color:#64748b;font-size:13px;margin-top:24px;">
        Log in with your email: <strong>{email}</strong>
      </p>
      <p style="margin-top:24px;">
        <a href="#"
           style="display:inline-block;background:#2036e0;color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600;">
          Open my dashboard
        </a>
      </p>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        Questions? Reply to this email — we're here to help.
      </p>
    """
    return _send(
        subject=f"Welcome to Everidemy, {school_name}!",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_suspended_email(school_name: str, email: str, reason: str = ""):
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">Your Everidemy account has been suspended</h2>
      <p>Hi {school_name} admin,</p>
      <p>Your school account on Everidemy has been temporarily suspended.</p>
      {f'<p><strong>Reason:</strong> {reason}</p>' if reason else ''}
      <p>If you believe this is a mistake, please contact support:</p>
      <p><a href="mailto:support@everidemy.com" style="color:#2036e0;">support@everidemy.com</a></p>
    """
    return _send(
        subject=f"Everidemy account suspended — {school_name}",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_reactivated_email(school_name: str, email: str):
    body = f"""
      <h2 style="margin-top:0;color:#16a34a;">Your Everidemy account is active again ✅</h2>
      <p>Hi {school_name} admin,</p>
      <p>Good news — your school account has been reactivated. You can now log in and continue managing your school.</p>
      <p>
        <a href="#"
           style="display:inline-block;background:#2036e0;color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600;">
          Open my dashboard
        </a>
      </p>
    """
    return _send(
        subject=f"Everidemy account reactivated — {school_name}",
        recipients=[email],
        html=_wrap(body),
    )


def send_platform_broadcast(school_name: str, email: str, message: str):
    body = f"""
      <h2 style="margin-top:0;">📢 Announcement from Everidemy</h2>
      <p>Hi {school_name} team,</p>
      <div style="background:#f1f5f9;border-left:4px solid #2036e0;padding:16px;border-radius:8px;margin:16px 0;white-space:pre-wrap;">{message}</div>
      <p style="color:#64748b;font-size:13px;">— The Everidemy Team</p>
    """
    return _send(
        subject="📢 Announcement from Everidemy",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_invite_email(school_name: str, email: str, admin_name: str):
    body = f"""
      <h2 style="margin-top:0;">You've been added to {school_name} on Everidemy</h2>
      <p>Hi {admin_name},</p>
      <p>A new school <strong>{school_name}</strong> has been created for you on Everidemy. Set your password to get started.</p>
      <p>
        <a href="#"
           style="display:inline-block;background:#2036e0;color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600;">
          Set my password
        </a>
      </p>
    """
    return _send(
        subject=f"You're invited to {school_name} on Everidemy",
        recipients=[email],
        html=_wrap(body),
    )


def send_custom_email(to_email: str, subject: str, message: str):
    """
    Generic helper for the super admin's "Email the admin" box.
    Wraps the plain-text message in the standard shell.
    """
    body = f"""
      <h2 style="margin-top:0;">{subject}</h2>
      <div style="white-space:pre-wrap;">{message}</div>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">— The Everidemy Team</p>
    """
    return _send(
        subject=subject,
        recipients=[to_email],
        html=_wrap(body),
    )


# =========================================================
# BILLING EMAILS
# =========================================================
def send_billing_confirmed_email(school_name: str, email: str, amount: float,
                                  next_billing):
    """Sent after a successful subscription charge."""
    sym = Config.CURRENCY_SYMBOL
    body = f"""
      <h2 style="margin-top:0;color:#16a34a;">✅ Payment received</h2>
      <p>Hi {school_name} admin,</p>
      <p>Your Everidemy subscription payment of
         <strong>{sym} {amount:,.2f}</strong> was successful.</p>
      <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:16px;
                  border-radius:8px;margin:16px 0;">
        <p style="margin:0;font-size:13px;color:#166534;">Next billing date</p>
        <p style="margin:4px 0 0;font-size:18px;font-weight:700;color:#166534;">
          {next_billing.strftime('%B %d, %Y')}
        </p>
      </div>
      <p>Your account is active. No action required.</p>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        You can download receipts anytime from your billing page.
      </p>
    """
    return _send(
        subject="Everidemy — Payment received ✓",
        recipients=[email],
        html=_wrap(body),
    )


def send_billing_failed_email(school_name: str, email: str, amount: float,
                               grace_days: int):
    """Sent when a renewal charge fails."""
    sym = Config.CURRENCY_SYMBOL
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">⚠️ Payment failed</h2>
      <p>Hi {school_name} admin,</p>
      <p>We couldn't process your Everidemy subscription renewal of
         <strong>{sym} {amount:,.2f}</strong>.</p>
      <p>You have <strong>{grace_days} days</strong> to update your payment method
         before your account is temporarily locked.</p>
      <p style="margin-top:24px;">
        <a href="#" style="display:inline-block;background:#dc2626;color:#fff;
                           padding:12px 24px;border-radius:10px;text-decoration:none;
                           font-weight:600;">
          Update payment method
        </a>
      </p>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        Common causes: expired card, insufficient funds, or bank declining the charge.
      </p>
    """
    return _send(
        subject="Everidemy — Action needed: payment failed",
        recipients=[email],
        html=_wrap(body),
    )


def send_billing_locked_email(school_name: str, email: str):
    """
    Sent when the school's access has been locked due to non-payment.

    Wired up by a future scheduled job (checks `past_due` schools whose
    grace period has lapsed and calls this helper once).
    """
    sym = Config.CURRENCY_SYMBOL
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">🔒 Your account is locked</h2>
      <p>Hi {school_name} admin,</p>
      <p>Your Everidemy subscription could not be renewed and your account
         has been temporarily locked.</p>
      <p>To restore access, please update your payment method:</p>
      <p style="margin-top:24px;">
        <a href="#" style="display:inline-block;background:#16a34a;color:#fff;
                           padding:12px 24px;border-radius:10px;text-decoration:none;
                           font-weight:600;">
          Restore access
        </a>
      </p>
      <p>Your data is safe and will be waiting for you.</p>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        Need help? Reply to this email — we'll get back to you within 24 hours.
      </p>
    """
    return _send(
        subject=f"Everidemy — {school_name} account locked",
        recipients=[email],
        html=_wrap(body),
    )