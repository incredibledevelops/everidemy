"""
Authentication blueprint — login, signup, logout.

Everything lives under the root (no prefix). Routes:
  GET  /login     → form
  POST /login     → authenticate + redirect by role
  GET  /signup    → form
  POST /signup    → create school + first admin, log them in
  GET  /logout    → clear session
"""
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, current_app,
)

from models import School, User
from utils.auth import login_user, logout_user, current_user


auth_bp = Blueprint("auth", __name__)


# =========================================================
# LOGIN
# =========================================================
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in → bounce to correct dashboard
    if current_user():
        return _redirect_by_role()

    if request.method == "POST":
        email    = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        # ---- 1. Look up user ----
        user = User.find_by_email(email)
        if not user or not User.verify_password(user, password):
            flash("Invalid email or password.", "error")
            return render_template("login.html"), 401

        # ---- 2. Load tenant (super admin has none) ----
        school = None
        if user["role"] != "super_admin":
            school = School.find_by_id(user["school_id"])
            if not school:
                flash("Account misconfigured. Please contact support.", "error")
                return render_template("login.html"), 500
            if school.get("suspended"):
                flash("Your school account is suspended. Please contact support.", "error")
                return render_template("login.html"), 403

        # ---- 3. Log them in ----
        login_user(user, school)
        User.update_last_login(user["_id"])

        if school:
            School.log(
                school["_id"], user["_id"],
                "user.login", {"role": user["role"]},
            )

        first_name = (user.get("name") or "").split(" ")[0] or "there"
        flash(f"Welcome back, {first_name}!", "success")

        # ---- 4. Redirect by role ----
        return _redirect_by_role()

    # GET
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

        # ---- Validation ----
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
        if User.find_by_email(email):
            errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", form=request.form), 400

        # ---- Create school + first admin ----
        try:
            school, user = School.create(
                name=school_name,
                owner_email=email,
                owner_name=owner_name,
                password=password,
                phone=phone,
                type=school_type,
                plan="monthly",           # single plan
                send_welcome=True,
            )
        except Exception:
            # Log the full traceback server-side, show a friendly message to the user.
            current_app.logger.exception("Signup failed")
            flash(
                "Something went wrong while creating your account. "
                "Please try again in a moment, or contact support if the issue persists.",
                "error",
            )
            return render_template("signup.html", form=request.form), 500

        # ---- Log them in ----
        login_user(user, school)
        User.update_last_login(user["_id"])
        School.log(school["_id"], user["_id"], "school.signup", {"name": school_name})

        flash(
            f"Welcome to Everidemy, {owner_name}! "
            f"Your 14-day free trial has started.",
            "success",
        )
        return _redirect_by_role()

    # GET
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
# HELPERS
# =========================================================
def _redirect_by_role():
    """
    Send the user to the right portal based on their role.
    Every endpoint is guaranteed to exist since all blueprints
    are registered in app.py. Falls back to landing on any
    unexpected role.
    """
    role = session.get("role")

    if role == "super_admin":
        return redirect(url_for("super_admin.dashboard"))

    if role == "school_admin":
        return redirect(url_for("school_admin.dashboard"))

    if role == "teacher":
        return redirect(url_for("teacher.dashboard"))

    if role == "student":
        return redirect(url_for("student.dashboard"))

    if role == "parent":
        return redirect(url_for("parent.dashboard"))

    if role == "accountant":
        # Accountants use the school admin workspace
        return redirect(url_for("school_admin.dashboard"))

    # No role / unknown → send home
    return redirect(url_for("main.landing"))


def _looks_like_email(email: str) -> bool:
    """
    Cheap but effective check — don't need RFC 5322 here.
    Anything matching `something@something.something` passes.
    """
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()