"""
Everidemy — Authentication helpers.

Functions here fall into three groups:

1. SESSION HELPERS — login_user, logout_user, current_user, current_school
2. DECORATORS     — login_required, role_required, school_required
3. CONVENIENCE    — is_logged_in, has_role
"""
from functools import wraps

from flask import session, redirect, url_for, flash, abort, g

from models import User, School


# =====================================================================
# SESSION HELPERS
# =====================================================================
def current_user():
    """
    Return the logged-in user document, or None.

    Caches the result on `g` so we only hit Mongo once per request,
    even if called from before_request + decorators + templates.
    """
    if hasattr(g, "_current_user"):
        return g._current_user

    uid = session.get("user_id")
    if not uid:
        g._current_user = None
        return None

    g._current_user = User.find_by_id(uid)
    return g._current_user


def current_school():
    """
    Return the logged-in user's school, or None.
    Same request-level caching as current_user().
    """
    if hasattr(g, "_current_school"):
        return g._current_school

    sid = session.get("school_id")
    if not sid:
        g._current_school = None
        return None

    g._current_school = School.find_by_id(sid)
    return g._current_school


def login_user(user, school):
    """
    Establish a user session.
    `school` may be None (super admin has no school).
    """
    session.clear()
    session["user_id"] = str(user["_id"])
    session["school_id"] = str(school["_id"]) if school else None
    session["role"] = user["role"]
    session.permanent = True


def logout_user():
    """Clear the current session."""
    session.clear()


def is_logged_in() -> bool:
    """True if a user session exists and the user still exists in Mongo."""
    return current_user() is not None


def has_role(*roles) -> bool:
    """True if the current user's role is in the given set."""
    user = current_user()
    return bool(user and user.get("role") in roles)


# =====================================================================
# DECORATORS
# =====================================================================
def login_required(view):
    """
    Require a logged-in user. Populates g.user / g.school.
    Redirects to /login if the session is missing or invalid.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        g.user = user
        g.school = current_school()
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    """
    Require the user to have one of the given roles.
    Blocks suspended schools (unless they are a super admin).
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("auth.login"))

            if user["role"] not in roles:
                abort(403)

            # Suspended schools can't access anything (except super admins)
            if user["role"] != "super_admin":
                school = current_school()
                if school and school.get("suspended"):
                    flash("Your school account is suspended. Please contact support.", "error")
                    return redirect(url_for("auth.logout"))

            g.user = user
            g.school = current_school()
            return view(*args, **kwargs)

        return wrapped

    return decorator


def school_required(view):
    """
    Require a logged-in user belonging to a non-suspended school.
    Blocks super admins (they have no school).

    Used by every route inside the `school_admin` blueprint.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        # Super admins must use /super-admin/*
        if user["role"] == "super_admin":
            return redirect(url_for("super_admin.dashboard"))

        school = current_school()
        if not school:
            flash("No school is linked to your account.", "error")
            return redirect(url_for("auth.logout"))

        if school.get("suspended"):
            flash("Your school account is suspended. Please contact support.", "error")
            return redirect(url_for("auth.logout"))

        g.user = user
        g.school = school
        return view(*args, **kwargs)

    return wrapped