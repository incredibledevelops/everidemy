"""
Main blueprint — public marketing site.

No prefix. Routes:
  GET  /             → landing page
  GET  /pricing      → pricing page
  GET  /features     → features page
  GET  /about        → about page
  GET  /contact      → contact page
  POST /contact      → send contact form
  GET  /healthz      → JSON health check (alias: /health)
  GET  /robots.txt   → robots directives
  GET  /sitemap.xml  → XML sitemap for SEO
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, Response, request,
    redirect, url_for, flash,
)
from config import Config


main_bp = Blueprint("main", __name__)


# =========================================================
# PUBLIC MARKETING PAGES
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


# =========================================================
# CONTACT (GET form + POST submit)
# =========================================================
@main_bp.route("/contact", methods=["GET", "POST"])
def contact():
    """
    Contact form.
      GET  → show the form
      POST → validate, send to support, send auto-reply, flash + redirect
    """
    if request.method == "POST":
        # ── Honeypot: bots fill this hidden field ──
        if (request.form.get("website") or "").strip():
            flash("Message sent. We'll be in touch shortly.", "success")
            return redirect(url_for("main.contact"))

        name    = (request.form.get("name") or "").strip()
        email   = (request.form.get("email") or "").strip().lower()
        school  = (request.form.get("school_name") or "").strip()
        message = (request.form.get("message") or "").strip()

        # ── Validation ──
        errors = []
        if not name:
            errors.append("Your name is required.")
        if not email:
            errors.append("Email is required.")
        elif not _looks_like_email(email):
            errors.append("Please enter a valid email address.")
        if len(message) < 10:
            errors.append("Please write a message of at least 10 characters.")

        if errors:
            for e in errors:
                flash(e, "error")
            # Re-render with the same inputs so the user doesn't lose text
            return render_template("contact.html",
                                   plans=Config.PLANS), 400

        # ── Send to support (best-effort) ──
        try:
            from utils.mailer import send_custom_email
            subject = f"[Contact] {name}" + (f" — {school}" if school else "")
            body = (
                f"From:  {name} <{email}>\n"
                f"School: {school or '—'}\n"
                f"─────────────────────────\n\n"
                f"{message}"
            )
            send_custom_email(
                to_email="support@everidemy.com",
                subject=subject,
                message=body,
            )
        except Exception:
            # Never block the user on a mail failure
            import logging
            logging.getLogger(__name__).exception("Contact form email failed")

        # ── Auto-reply (best-effort) ──
        try:
            from utils.mailer import send_custom_email as _send
            _send(
                to_email=email,
                subject="We received your message",
                message=(
                    f"Hi {name},\n\n"
                    "Thanks for reaching out to Everidemy. "
                    "A member of our team will get back to you within 24 hours.\n\n"
                    "If your question is urgent, reply directly to this email.\n\n"
                    "— The Everidemy Team"
                ),
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Contact auto-reply failed")

        flash("Message sent. We'll be in touch shortly.", "success")
        return redirect(url_for("main.contact"))

    # GET
    return render_template("contact.html", plans=Config.PLANS)


# =========================================================
# HEALTH CHECK
# =========================================================
@main_bp.route("/healthz")
@main_bp.route("/health")
def health():
    """Simple JSON health check for uptime monitors and load balancers."""
    return {
        "status": "ok",
        "platform": Config.PLATFORM_NAME,
        "time": datetime.utcnow().isoformat() + "Z",
    }


# =========================================================
# ROBOTS.TXT
# =========================================================
@main_bp.route("/robots.txt")
def robots_txt():
    """
    Serve a robots.txt that:
      - Blocks crawlers from authenticated portals and API surfaces
      - Allows the public marketing site
      - Points crawlers at the sitemap
    """
    base = request.url_root.rstrip("/")

    lines = [
        "User-agent: *",
        "",
        "# Public marketing site",
        "Allow: /",
        "Allow: /pricing",
        "Allow: /features",
        "Allow: /about",
        "Allow: /contact",
        "",
        "# Authenticated portals — never index these",
        "Disallow: /super-admin/",
        "Disallow: /school-admin/",
        "Disallow: /teacher/",
        "Disallow: /student/",
        "Disallow: /parent/",
        "Disallow: /account/",
        "",
        "# Auth & reset flow",
        "Disallow: /login",
        "Disallow: /signup",
        "Disallow: /logout",
        "Disallow: /forgot-password",
        "Disallow: /reset-password/",
        "",
        "# Webhooks & internal endpoints",
        "Disallow: /webhooks/",
        "Disallow: /healthz",
        "Disallow: /health",
        "",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ]

    body = "\n".join(lines)
    return Response(body, mimetype="text/plain; charset=utf-8")


# =========================================================
# SITEMAP.XML
# =========================================================
@main_bp.route("/sitemap.xml")
def sitemap_xml():
    """
    Minimal sitemap listing the public marketing pages.
    Uses <lastmod> for each URL so search engines know when to recrawl.
    """
    base = request.url_root.rstrip("/")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # (path, changefreq, priority, lastmod)
    pages = [
        ("/",          "weekly",  "1.0", today),
        ("/pricing",   "monthly", "0.9", today),
        ("/features",  "monthly", "0.8", today),
        ("/about",     "yearly",  "0.7", today),
        ("/contact",   "yearly",  "0.6", today),
    ]

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for path, changefreq, priority, lastmod in pages:
        parts.append("  <url>")
        parts.append(f"    <loc>{base}{path}</loc>")
        parts.append(f"    <lastmod>{lastmod}</lastmod>")
        parts.append(f"    <changefreq>{changefreq}</changefreq>")
        parts.append(f"    <priority>{priority}</priority>")
        parts.append("  </url>")

    parts.append("</urlset>")

    body = "\n".join(parts)
    return Response(body, mimetype="application/xml; charset=utf-8")


# =========================================================
# HELPERS
# =========================================================
def _looks_like_email(email: str) -> bool:
    """Cheap but effective — same rule as the auth blueprint."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()