"""
Email helpers for Everidemy using Resend.

All sends are wrapped in try/except so a broken API key never crashes a request.
Every helper returns a tuple: (ok: bool, error: str | None)
"""
from datetime import datetime

from flask import current_app

import resend

from config import Config


# =====================================================================
# BASE TEMPLATE
# =====================================================================
_BASE = """
<div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:0 auto;background:#ffffff;">
  <div style="background:#2036e0;padding:24px;text-align:center;">
    <h1 style="color:#ffffff;margin:0;font-size:22px;">{platform_name}</h1>
    <p style="color:#dbe6ff;margin:4px 0 0;font-size:12px;">Modern School Management</p>
  </div>
  <div style="padding:32px 24px;color:#0b1020;line-height:1.6;">
    {body}
  </div>
  <div style="background:#f8fafc;padding:16px;text-align:center;color:#64748b;font-size:12px;">
    © {year} {platform_name}
  </div>
</div>
"""


def _wrap(body_html: str) -> str:
    return _BASE.format(
        body=body_html,
        platform_name=Config.PLATFORM_NAME,
        year=datetime.utcnow().year,
    )


# =====================================================================
# LOW-LEVEL SEND (Resend)
# =====================================================================
def _send(subject: str, recipients: list, html: str, text: str = None):
    """
    Send an email via Resend.
    Returns (ok, error).
    """
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return False, "No recipients"

    if not Config.RESEND_API_KEY:
        return False, "RESEND_API_KEY not configured"

    params: resend.Emails.SendParams = {
        "from": Config.RESEND_FROM_EMAIL,
        "to": recipients,
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text

    try:
        email = resend.Emails.send(params)
        try:
            current_app.logger.info(f"[resend] Sent '{subject}' → {recipients} (id={email.get('id')})")
        except Exception:
            pass
        return True, None
    except Exception as e:
        try:
            current_app.logger.error(f"[resend] Failed '{subject}' → {recipients}: {e}")
        except Exception:
            pass
        return False, str(e)


# =====================================================================
# PUBLIC HELPERS (same signatures as before)
# =====================================================================
def send_welcome_email(school_name: str, owner_name: str, email: str):
    body = f"""
      <h2 style="margin-top:0;">Welcome to {Config.PLATFORM_NAME}, {owner_name}! 🎉</h2>
      <p>Your school <strong>{school_name}</strong> is now live on {Config.PLATFORM_NAME}.</p>
      <p>Subscribe to unlock your dashboard, staff, students, and parent portals.</p>
      <p style="color:#64748b;font-size:13px;margin-top:24px;">
        Log in with your email: <strong>{email}</strong>
      </p>
    """
    return _send(
        subject=f"Welcome to {Config.PLATFORM_NAME}, {school_name}!",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_suspended_email(school_name: str, email: str, reason: str = ""):
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">Your {Config.PLATFORM_NAME} account has been suspended</h2>
      <p>Hi {school_name} admin,</p>
      <p>Your school account on {Config.PLATFORM_NAME} has been temporarily suspended.</p>
      {f'<p><strong>Reason:</strong> {reason}</p>' if reason else ''}
      <p>If you believe this is a mistake, please contact support:</p>
      <p><a href="mailto:support@everidemy.com" style="color:#2036e0;">support@everidemy.com</a></p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} account suspended — {school_name}",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_reactivated_email(school_name: str, email: str):
    body = f"""
      <h2 style="margin-top:0;color:#16a34a;">Your {Config.PLATFORM_NAME} account is active again ✅</h2>
      <p>Hi {school_name} admin,</p>
      <p>Good news — your school account has been reactivated. You can now log in and continue managing your school.</p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} account reactivated — {school_name}",
        recipients=[email],
        html=_wrap(body),
    )


def send_platform_broadcast(school_name: str, email: str, message: str):
    body = f"""
      <h2 style="margin-top:0;">📢 Announcement from {Config.PLATFORM_NAME}</h2>
      <p>Hi {school_name} team,</p>
      <div style="background:#f1f5f9;border-left:4px solid #2036e0;padding:16px;border-radius:8px;margin:16px 0;white-space:pre-wrap;">{message}</div>
      <p style="color:#64748b;font-size:13px;">— The {Config.PLATFORM_NAME} Team</p>
    """
    return _send(
        subject=f"📢 Announcement from {Config.PLATFORM_NAME}",
        recipients=[email],
        html=_wrap(body),
    )


def send_school_invite_email(school_name: str, email: str, admin_name: str):
    body = f"""
      <h2 style="margin-top:0;">You've been added to {school_name} on {Config.PLATFORM_NAME}</h2>
      <p>Hi {admin_name},</p>
      <p>A new school <strong>{school_name}</strong> has been created for you on {Config.PLATFORM_NAME}. Set your password to get started.</p>
    """
    return _send(
        subject=f"You're invited to {school_name} on {Config.PLATFORM_NAME}",
        recipients=[email],
        html=_wrap(body),
    )


def send_custom_email(to_email: str, subject: str, message: str):
    body = f"""
      <h2 style="margin-top:0;">{subject}</h2>
      <div style="white-space:pre-wrap;">{message}</div>
      <p style="color:#64748b;font-size:13px;margin-top:32px;">— The {Config.PLATFORM_NAME} Team</p>
    """
    return _send(
        subject=subject,
        recipients=[to_email],
        html=_wrap(body),
    )


# =========================================================
# SUBSCRIPTION / BILLING EMAILS
# =========================================================
def send_subscription_activated_email(
    school_name: str,
    email: str,
    amount: float,
    next_billing,
    login_url: str = "",
):
    sym = Config.CURRENCY_SYMBOL
    login_block = ""
    if login_url:
        login_block = f"""
          <p style="margin:24px 0;">
            <a href="{login_url}"
               style="display:inline-block;background:#2036e0;color:#fff;
                      padding:12px 24px;border-radius:10px;text-decoration:none;
                      font-weight:600;">
              Open my dashboard
            </a>
          </p>
        """

    body = f"""
      <h2 style="margin-top:0;color:#16a34a;">🎉 Your account is now active!</h2>
      <p>Hi {school_name} admin,</p>
      <p>
        Welcome aboard. Your first {Config.PLATFORM_NAME} subscription payment of
        <strong>{sym} {amount:,.2f}</strong> was successful, and your school
        portal is now fully unlocked.
      </p>

      <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:16px;
                  border-radius:8px;margin:16px 0;">
        <p style="margin:0;font-size:13px;color:#166534;">Active until</p>
        <p style="margin:4px 0 0;font-size:18px;font-weight:700;color:#166534;">
          {next_billing.strftime('%B %d, %Y')}
        </p>
      </div>

      {login_block}

      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        Renewals are manual — you'll be prompted to pay again before
        <strong>{next_billing.strftime('%B %d, %Y')}</strong>.
      </p>
    """
    return _send(
        subject=f"🎉 {Config.PLATFORM_NAME} — Your school is now active",
        recipients=[email],
        html=_wrap(body),
    )


def send_subscription_renewed_email(
    school_name: str,
    email: str,
    amount: float,
    next_billing,
):
    sym = Config.CURRENCY_SYMBOL
    body = f"""
      <h2 style="margin-top:0;color:#16a34a;">✅ Subscription renewed</h2>
      <p>Hi {school_name} admin,</p>
      <p>
        Your {Config.PLATFORM_NAME} subscription renewal of
        <strong>{sym} {amount:,.2f}</strong> was successful.
        Your school portal remains fully active.
      </p>

      <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:16px;
                  border-radius:8px;margin:16px 0;">
        <p style="margin:0;font-size:13px;color:#166534;">Active until</p>
        <p style="margin:4px 0 0;font-size:18px;font-weight:700;color:#166534;">
          {next_billing.strftime('%B %d, %Y')}
        </p>
      </div>

      <p>No action required.</p>

      <p style="color:#64748b;font-size:13px;margin-top:32px;">
        You can view receipts anytime from your billing page.
      </p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} — Subscription renewed ✓",
        recipients=[email],
        html=_wrap(body),
    )


def send_billing_confirmed_email(school_name: str, email: str, amount: float,
                                  next_billing):
    return send_subscription_renewed_email(
        school_name=school_name,
        email=email,
        amount=amount,
        next_billing=next_billing,
    )


def send_billing_failed_email(school_name: str, email: str, amount: float,
                               grace_days: int):
    sym = Config.CURRENCY_SYMBOL
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">⚠️ Payment failed</h2>
      <p>Hi {school_name} admin,</p>
      <p>We couldn't process your {Config.PLATFORM_NAME} subscription payment of
         <strong>{sym} {amount:,.2f}</strong>.</p>
      <p>Please try again from your billing page to keep your school portal active.</p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} — Action needed: payment failed",
        recipients=[email],
        html=_wrap(body),
    )


def send_billing_locked_email(school_name: str, email: str):
    body = f"""
      <h2 style="margin-top:0;color:#dc2626;">🔒 Your school portal is locked</h2>
      <p>Hi {school_name} admin,</p>
      <p>Your {Config.PLATFORM_NAME} subscription has expired, and access to
         your school portal is now paused.</p>
      <p>Your data is safe and will be waiting for you.</p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} — {school_name} account locked",
        recipients=[email],
        html=_wrap(body),
    )


# =====================================================================
# PORTAL CREDENTIALS
# =====================================================================
def send_portal_credentials_email(
    *,
    recipient_name: str,
    email: str,
    password: str,
    role_label: str,
    school_name: str,
    login_url: str,
    student_names: str = "",
):
    student_line = (
        f"<p>Linked student(s): <strong>{student_names}</strong></p>"
        if student_names else ""
    )
    body = f"""
      <h2 style="margin-top:0;">Welcome to {school_name} on {Config.PLATFORM_NAME} 🎉</h2>
      <p>Hi {recipient_name},</p>
      <p>An {Config.PLATFORM_NAME} <strong>{role_label}</strong> account has been created for you.</p>
      {student_line}

      <div style="background:#f1f5f9;border-left:4px solid #2036e0;padding:16px;
                  border-radius:8px;margin:20px 0;">
        <p style="margin:0 0 8px;font-size:12px;text-transform:uppercase;
                  letter-spacing:.05em;color:#475569;font-weight:700;">
          Your login details
        </p>
        <p style="margin:6px 0;font-family:Menlo,Consolas,monospace;font-size:14px;">
          <strong>Email:</strong> {email}
        </p>
        <p style="margin:6px 0;font-family:Menlo,Consolas,monospace;font-size:14px;">
          <strong>Password:</strong> {password}
        </p>
      </div>

      <p style="margin:24px 0;">
        <a href="{login_url}"
           style="display:inline-block;background:#2036e0;color:#fff;
                  padding:12px 24px;border-radius:10px;text-decoration:none;
                  font-weight:600;">
          Open my dashboard
        </a>
      </p>

      <p style="color:#64748b;font-size:13px;">
        For your security, please change this password after your first login.
      </p>
    """
    return _send(
        subject=f"Your {school_name} portal login details",
        recipients=[email],
        html=_wrap(body),
    )


def send_ward_portal_credentials_email(
    *,
    guardian_name: str,
    guardian_email: str,
    guardian_password: str | None,
    student_name: str,
    student_email: str | None,
    student_password: str | None,
    school_name: str,
    login_url: str,
):
    guardian_block = ""
    if guardian_password:
        guardian_block = f"""
          <div style="background:#eef2ff;border-left:4px solid #2036e0;
                      padding:14px;border-radius:8px;margin:12px 0;">
            <p style="margin:0 0 8px;font-size:11px;text-transform:uppercase;
                      letter-spacing:.05em;color:#3730a3;font-weight:700;">
              Your parent login
            </p>
            <p style="margin:4px 0;font-family:Menlo,Consolas,monospace;font-size:13px;">
              <strong>Email:</strong> {guardian_email}
            </p>
            <p style="margin:4px 0;font-family:Menlo,Consolas,monospace;font-size:13px;">
              <strong>Password:</strong> {guardian_password}
            </p>
          </div>
        """

    student_block = ""
    if student_password and student_email:
        student_block = f"""
          <div style="background:#f0fdf4;border-left:4px solid #16a34a;
                      padding:14px;border-radius:8px;margin:12px 0;">
            <p style="margin:0 0 8px;font-size:11px;text-transform:uppercase;
                      letter-spacing:.05em;color:#166534;font-weight:700;">
              {student_name}'s student login
            </p>
            <p style="margin:4px 0;font-family:Menlo,Consolas,monospace;font-size:13px;">
              <strong>Email:</strong> {student_email}
            </p>
            <p style="margin:4px 0;font-family:Menlo,Consolas,monospace;font-size:13px;">
              <strong>Password:</strong> {student_password}
            </p>
          </div>
        """

    body = f"""
      <h2 style="margin-top:0;">Welcome to {school_name} on {Config.PLATFORM_NAME} 🎉</h2>
      <p>Hi {guardian_name},</p>
      <p>
        A portal account has been created for you and for your ward,
        <strong>{student_name}</strong>. You can log in to see their grades,
        attendance, and fees — and pay fees online.
      </p>

      {guardian_block}
      {student_block}

      <p style="margin:24px 0;">
        <a href="{login_url}"
           style="display:inline-block;background:#2036e0;color:#fff;
                  padding:12px 24px;border-radius:10px;text-decoration:none;
                  font-weight:600;">
          Open the portal
        </a>
      </p>

      <p style="color:#64748b;font-size:13px;">
        For security, you'll be asked to change the password on first login.
      </p>
    """

    return _send(
        subject=f"Portal logins for {student_name} — {school_name}",
        recipients=[guardian_email],
        html=_wrap(body),
    )


# =====================================================================
# PASSWORD RESET
# =====================================================================
def send_password_reset_email(
    *, recipient_name: str, email: str, reset_url: str, expires_hours: int = 72,
):
    body = f"""
      <h2 style="margin-top:0;">Reset your {Config.PLATFORM_NAME} password</h2>
      <p>Hi {recipient_name or 'there'},</p>
      <p>We received a request to reset your password. Click below to choose a new one.</p>
      <p style="margin:24px 0;">
        <a href="{reset_url}"
           style="display:inline-block;background:#2036e0;color:#fff;
                  padding:12px 24px;border-radius:10px;text-decoration:none;
                  font-weight:600;">
          Reset my password
        </a>
      </p>
      <p style="color:#64748b;font-size:13px;">
        This link expires in {expires_hours} hours.
      </p>
      <p style="color:#64748b;font-size:13px;">
        If you didn't request this, you can safely ignore this email.
      </p>
    """
    return _send(
        subject=f"{Config.PLATFORM_NAME} — Reset your password",
        recipients=[email],
        html=_wrap(body),
    )