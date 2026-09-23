# utils/navigation.py
from flask import redirect, url_for, session

def dashboard_for(role: str | None):
    """Return a Redirect response to the correct dashboard for this role."""
    if role == "super_admin":  return redirect(url_for("super_admin.dashboard"))
    if role == "school_admin": return redirect(url_for("school_admin.dashboard"))
    if role == "accountant":   return redirect(url_for("school_admin.dashboard"))
    if role == "teacher":      return redirect(url_for("teacher.dashboard"))
    if role == "student":      return redirect(url_for("student.dashboard"))
    if role == "parent":       return redirect(url_for("parent.dashboard"))
    return redirect(url_for("main.landing"))