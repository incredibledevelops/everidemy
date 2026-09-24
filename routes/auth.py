"""
Authentication blueprint — login, signup, logout, password reset.

Routes:
  GET  /login                    → form
  POST /login                    → authenticate + redirect by role
  GET  /signup                   → form
  POST /signup                   → create school + first admin, redirect to billing
  GET  /logout                   → clear session
  GET  /forgot-password          → request reset email
  POST /forgot-password          → send reset email
  GET  /reset-password/<token>   → set new password form
  POST /reset-password/<token>   → apply new password
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, current_app,
)
from werkzeug.security import generate_password_hash

from config import Config
from models import School, User
from extensions import users, audit_logs
from utils.auth import login_user, logout_user, current_user
from utils.passwords import (
    generate_reset_token, verify_reset_token,
    verify_invite_token,
)
from utils.mailer import send_password_reset_email


auth_bp = Blueprint("auth", __name__)


# =========================================================
# CONSTANTS
# =========================================================
VALID_SCHOOL_TYPES = {"primary", "secondary", "high", "mixed", "nursery", "vocational"}


# =========================================================
# LOGIN
# =========================================================
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return _redirect_by_role()

    if request.method == "POST":
        email    = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.find_by_email(email)
        if not user or not User.verify_password(user, password):
            flash("Invalid email or password.", "error")
            return render_template("login.html"), 401

        school = None
        if user["role"] != "super_admin":
            school = School.find_by_id(user["school_id"])
            if not school:
                flash("Account misconfigured. Please contact support.", "error")
                return render_template("login.html"), 500
            if school.get("suspended"):
                flash("Your school account is suspended. Please contact support.", "error")
                return render_template("login.html"), 403

        login_user(user, school)
        User.update_last_login(user["_id"])

        if school:
            School.log(
                school["_id"], user["_id"],
                "user.login", {"role": user["role"]},
            )

        first_name = (user.get("name") or "").split(" ")[0] or "there"
        flash(f"Welcome back, {first_name}!", "success")

        # Temp password → force change on first login
        if user.get("must_reset_password") and user["role"] != "super_admin":
            flash("Please set a new password to continue.", "warning")
            return redirect(url_for("account.change_password"))

        return _redirect_by_role()

    return render_template("login.html")


# =========================================================
# SIGNUP
# =========================================================
@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        owner_name  = (request.form.get("owner_name") or "").strip()
        email       = (request.form.get("email") or "").strip().lower()
        phone       = (request.form.get("phone") or "").strip()
        password    = request.form.get("password") or ""
        confirm     = request.form.get("confirm_password") or ""
        school_name = (request.form.get("school_name") or "").strip()
        school_type = (request.form.get("school_type") or "primary").strip().lower()

        errors = []
        if not owner_name:
            errors.append("Full name is required.")
        if not email:
            errors.append("Email is required.")
        elif not _looks_like_email(email):
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not school_name:
            errors.append("School name is required.")
        if school_type not in VALID_SCHOOL_TYPES:
            errors.append("Please choose a valid school type.")
        if User.find_by_email(email):
            errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", form=request.form), 400

        try:
            school, user = School.create(
                name=school_name,
                owner_email=email,
                owner_name=owner_name,
                password=password,
                phone=phone,
                type=school_type,
                plan="monthly",
                send_welcome=True,
            )
        except Exception:
            current_app.logger.exception("Signup failed")
            flash(
                "Something went wrong while creating your account. "
                "Please try again in a moment, or contact support if the issue persists.",
                "error",
            )
            return render_template("signup.html", form=request.form), 500

        login_user(user, school)
        User.update_last_login(user["_id"])
        School.log(school["_id"], user["_id"], "school.signup", {"name": school_name})

        flash(
            f"Welcome to {Config.PLATFORM_NAME}, {owner_name}! "
            f"Complete your subscription to unlock your dashboard.",
            "success",
        )

        # ── No trial → go straight to billing ──
        return redirect(url_for("billing.dashboard"))

    return render_template("signup.html", form={})


# =========================================================
# LOGOUT
# =========================================================
@auth_bp.route("/logout")
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


# =========================================================
# FORGOT PASSWORD
# =========================================================
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.find_by_email(email) if email else None

        # Always show the same success message — avoids account enumeration.
        if user:
            try:
                token = generate_reset_token(user["email"])
                reset_url = url_for("auth.reset_password", token=token, _external=True)
                send_password_reset_email(
                    recipient_name=user.get("name", ""),
                    email=user["email"],
                    reset_url=reset_url,
                )
            except Exception:
                current_app.logger.exception("Failed to send reset email")

        flash(
            "If that email is registered, we've sent reset instructions. "
            "Please check your inbox (and spam folder).",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html")


# =========================================================
# RESET PASSWORD (also handles invite tokens)
# =========================================================
@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    # Try the reset-token salt first, then the invite-token salt
    email = verify_reset_token(token)
    token_kind = "reset"
    if not email:
        email = verify_invite_token(token)
        token_kind = "invite"

    if not email:
        flash(
            "That reset link is invalid or has expired. Please request a new one.",
            "error",
        )
        return redirect(url_for("auth.forgot_password"))

    user = User.find_by_email(email)
    if not user:
        flash("Account not found. Please contact support.", "error")
        return redirect(url_for("auth.login"))

    # ── Replay guard: reject tokens issued BEFORE the last password change ──
    #    `itsdangerous` tokens stay valid until they expire (3 days).
    #    Once the password is reset, we refuse to accept older tokens.
    last_reset = user.get("password_reset_at")
    if last_reset and isinstance(last_reset, datetime):
        try:
            from itsdangerous import URLSafeTimedSerializer
            from flask import current_app as _app
            serializer = URLSafeTimedSerializer(
                secret_key=_app.config["SECRET_KEY"],
                salt=f"everidemy-{'reset-password' if token_kind == 'reset' else 'invite'}",
            )
            # `loads` with `return_timestamp=True` gives us the issue time
            _, issued_at = serializer.loads(
                token,
                max_age=60 * 60 * 24 * 14,
                return_timestamp=True,
            )
            if issued_at.replace(tzinfo=None) < last_reset:
                flash(
                    "That reset link is no longer valid. "
                    "Please request a new one.",
                    "error",
                )
                return redirect(url_for("auth.forgot_password"))
        except Exception:
            # If we can't decode the timestamp, fall through to normal handling
            pass

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm  = request.form.get("confirm_password") or ""

        errors = []
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/reset_password.html", token=token), 400

        now = datetime.utcnow()
        users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "password_hash":       generate_password_hash(password),
                "must_reset_password": False,
                "password_reset_at":   now,
                "updated_at":          now,
            }},
        )

        # Audit trail (school_id may be None for super_admin)
        try:
            audit_logs.insert_one({
                "school_id": user.get("school_id"),
                "actor_id":  user["_id"],
                "action":    "user.password_reset",
                "meta":      {"email": user["email"], "token_kind": token_kind},
                "timestamp": now,
            })
        except Exception:
            current_app.logger.exception("Failed to write password reset audit log")

        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


# =========================================================
# HELPERS
# =========================================================
def _redirect_by_role():
    role = session.get("role")
    if role == "super_admin":   return redirect(url_for("super_admin.dashboard"))
    if role == "school_admin":  return redirect(url_for("school_admin.dashboard"))
    if role == "teacher":       return redirect(url_for("teacher.dashboard"))
    if role == "student":       return redirect(url_for("student.dashboard"))
    if role == "parent":        return redirect(url_for("parent.dashboard"))
    if role == "accountant":    return redirect(url_for("school_admin.dashboard"))
    return redirect(url_for("main.landing"))


def _looks_like_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()