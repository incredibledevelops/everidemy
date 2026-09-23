"""
Account blueprint — lets any logged-in user view/update their own profile
and change their password. Works for every role.
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, g,
)
from werkzeug.security import generate_password_hash

from extensions import users
from utils.auth import current_user, current_school


account_bp = Blueprint("account", __name__, url_prefix="/account")


# =========================================================
# GATE
# =========================================================
@account_bp.before_request
def _gate():
    """
    Every route in this blueprint requires a logged-in user.
    We read via `current_user()` (which caches on g) so this works
    even if the app-level before_request hasn't populated g.user yet.
    """
    user = current_user()
    if not user:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("auth.login"))

    g.user = user
    # Preserve school context if the app already set it
    if not getattr(g, "school", None):
        g.school = current_school()
    return None


# =========================================================
# PROFILE
# =========================================================
@account_bp.route("/profile", methods=["GET", "POST"])
def profile():
    user = g.user

    if request.method == "POST":
        name  = (request.form.get("name") or "").strip()
        phone = (request.form.get("phone") or "").strip()

        errors = []
        if not name:
            errors.append("Full name is required.")
        if len(name) > 120:
            errors.append("Name must be under 120 characters.")
        if len(phone) > 30:
            errors.append("Phone must be under 30 characters.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("account/profile.html", user=user), 400

        users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "name":       name,
                "phone":      phone or None,
                "updated_at": datetime.utcnow(),
            }},
        )

        # Refresh in-memory user so the header/sidebar shows the new name
        user["name"] = name
        user["phone"] = phone or None
        g.user = user

        flash("Profile updated.", "success")
        return redirect(url_for("account.profile"))

    return render_template("account/profile.html", user=user)


# =========================================================
# CHANGE PASSWORD
# =========================================================
@account_bp.route("/password", methods=["GET", "POST"])
def change_password():
    user = g.user
    is_temp = bool(user.get("must_reset_password"))

    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new     = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        from models import User  # local import avoids circular deps

        errors = []

        # Only require the current password if this isn't a temp-password flow
        if not is_temp:
            if not current:
                errors.append("Current password is required.")
            elif not User.verify_password(user, current):
                errors.append("Current password is incorrect.")

        if len(new) < 8:
            errors.append("New password must be at least 8 characters.")
        if new != confirm:
            errors.append("New passwords do not match.")
        if not is_temp and current and new and current == new:
            errors.append("New password must be different from current password.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "account/change_password.html",
                user=user,
                is_temp=is_temp,
            ), 400

        now = datetime.utcnow()
        users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "password_hash":       generate_password_hash(new),
                "must_reset_password": False,
                "password_reset_at":   now,
                "updated_at":          now,
            }},
        )

        # Refresh g.user so the next request doesn't re-trigger the guard
        user["must_reset_password"] = False
        g.user = user

        flash("Password changed successfully.", "success")
        return _redirect_after_password_change()

    return render_template(
        "account/change_password.html",
        user=user,
        is_temp=is_temp,
    )


# =========================================================
# HELPERS
# =========================================================
def _redirect_after_password_change():
    role = (g.user or {}).get("role")
    if role == "school_admin": return redirect(url_for("school_admin.dashboard"))
    if role == "teacher":      return redirect(url_for("teacher.dashboard"))
    if role == "student":      return redirect(url_for("student.dashboard"))
    if role == "parent":       return redirect(url_for("parent.dashboard"))
    if role == "super_admin":  return redirect(url_for("super_admin.dashboard"))
    if role == "accountant":   return redirect(url_for("school_admin.dashboard"))
    return redirect(url_for("main.landing"))