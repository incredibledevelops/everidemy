"""
Main blueprint — public marketing site.

No prefix. Routes:
  GET  /          → landing page
  GET  /pricing   → pricing page
  GET  /features  → features page
  GET  /about     → about page
  GET  /contact   → contact page
  GET  /healthz   → JSON health check
"""
from flask import Blueprint, render_template, redirect, url_for
from config import Config


main_bp = Blueprint("main", __name__)


# =========================================================
# PUBLIC PAGES
# =========================================================
@main_bp.route("/")
def landing():
    """Marketing landing page with hero + features + CTA."""
    return render_template("landing.html", plans=Config.PLANS)


@main_bp.route("/pricing")
def pricing():
    """Pricing tiers with monthly/yearly toggle."""
    return render_template("pricing.html", plans=Config.PLANS)


@main_bp.route("/features")
def features():
    """Deep dive on every module Everidemy offers."""
    return render_template("features.html", plans=Config.PLANS)


@main_bp.route("/about")
def about():
    """Company story, mission, team."""
    return render_template("about.html", plans=Config.PLANS)


@main_bp.route("/contact")
def contact():
    """Contact form + support details."""
    return render_template("contact.html", plans=Config.PLANS)


# =========================================================
# HEALTH CHECK
# =========================================================
@main_bp.route("/healthz")
def health():
    """Simple JSON health check for uptime monitors and load balancers."""
    return {
        "status": "ok",
        "platform": Config.PLATFORM_NAME,
    }