"""
Everidemy — Application Factory
"""
import os
from datetime import datetime
from urllib.parse import urlparse

from flask import (
    Flask, render_template, g, request, jsonify,
    flash, session, redirect, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from extensions import (
    schools, users, students, staff, audit_logs,
    tickets, subscriptions, webhook_events, mail,
    ping_mongo,
)
from models import User, School, format_money
from utils.auth import current_user, current_school


# =====================================================================
# APP FACTORY
# =====================================================================
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # ------------------------------------------------------------
    # Trust reverse-proxy headers (Nginx / Cloudflare / Heroku)
    # Only enable when NOT running locally.
    # ------------------------------------------------------------
    if not app.debug:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,       # X-Forwarded-For
            x_proto=1,     # X-Forwarded-Proto → request.is_secure, url_root
            x_host=1,      # X-Forwarded-Host
            x_port=1,      # X-Forwarded-Port
        )

    # ------------------------------------------------------------
    # Init extensions
    # ------------------------------------------------------------
    mail.init_app(app)

    # ------------------------------------------------------------
    # Register blueprints
    # ------------------------------------------------------------
    from routes.main import main_bp
    from routes.auth import auth_bp
    from routes.super_admin import super_admin_bp
    from routes.school_admin import school_admin_bp
    from routes.teacher import teacher_bp
    from routes.student import student_bp
    from routes.parent import parent_bp
    from routes.billing import billing_bp
    from routes.webhooks import webhooks_bp
    from routes.account import account_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(super_admin_bp)
    app.register_blueprint(school_admin_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(parent_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(account_bp)

    # ------------------------------------------------------------
    # Before-request: load user/school into g FIRST
    # ------------------------------------------------------------
    @app.before_request
    def load_user_context():
        g.user = current_user()
        g.school = current_school() if g.user else None

    # ------------------------------------------------------------
    # Before-request: force password change (runs AFTER user loaded)
    # ------------------------------------------------------------
    @app.before_request
    def force_password_change():
        allowed_endpoints = {
            # Auth flow
            "auth.login",
            "auth.logout",
            "auth.signup",
            "auth.forgot_password",
            "auth.reset_password",
            # Account flow
            "account.change_password",
            "account.profile",
            # Public marketing + assets
            "static",
            "main.landing",
            "main.health",
            "main.pricing",
            "main.features",
            "main.about",
            "main.contact",
            "main.robots_txt",
            "main.sitemap_xml",
            # Billing callback must not be interrupted
            "billing.callback",
        }

        # Webhooks must never be redirected
        if request.path == "/webhooks" or request.path.startswith("/webhooks/"):
            return None

        if request.endpoint in allowed_endpoints:
            return None

        if not session.get("user_id"):
            return None

        user = getattr(g, "user", None)
        if user and user.get("must_reset_password"):
            # Super admin never needs the reset — it uses bootstrap creds
            if user.get("role") == "super_admin":
                return None
            flash("Please change your temporary password.", "warning")
            return redirect(url_for("account.change_password"))

        return None

    # ------------------------------------------------------------
    # Context processors
    # ------------------------------------------------------------
    @app.context_processor
    def inject_globals():
        return {
            "platform_name": Config.PLATFORM_NAME,
            "plans": Config.PLANS,
            "currency_symbol": Config.CURRENCY_SYMBOL,
            "currency_code": Config.CURRENCY,
            "format_money": format_money,
            "current_user": g.get("user"),
            "current_school": g.get("school"),
            "now": datetime.utcnow(),
        }

    # ------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------
    @app.errorhandler(400)
    def bad_request(e):
        if _wants_json():
            return jsonify(error="Bad request"), 400
        return render_template("errors/400.html"), 400

    @app.errorhandler(401)
    def unauthorized(e):
        if _wants_json():
            return jsonify(error="Unauthorized"), 401
        return render_template("errors/403.html"), 401

    @app.errorhandler(403)
    def forbidden(e):
        if _wants_json():
            return jsonify(error="Forbidden"), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify(error="Not found"), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Unhandled server error")
        if _wants_json():
            return jsonify(error="Internal server error"), 500
        return render_template("errors/500.html"), 500

    # ------------------------------------------------------------
    # Security headers
    # ------------------------------------------------------------
    @app.after_request
    def add_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS in production only (behind HTTPS)
        if not app.debug and request.is_secure:
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return resp

    # ------------------------------------------------------------
    # Bootstrap: ping Mongo → indexes → super admin
    # ------------------------------------------------------------
    _bootstrap_database(app)

    return app


# =====================================================================
# BOOTSTRAP
# =====================================================================
def _bootstrap_database(app: Flask):
    """
    Fail fast on a broken database so deploys don't ship a zombie.

    Set MONGO_PING_ON_BOOT=false in .env to skip this step temporarily
    (e.g. when you're behind a firewall and just want to boot).
    """
    ping_on_boot = (
        os.getenv("MONGO_PING_ON_BOOT", "true").strip().lower()
        not in ("0", "false", "no", "off")
    )

    with app.app_context():
        target = _describe_mongo_target(Config.MONGO_URI)

        if not ping_on_boot:
            app.logger.warning(
                "⚠ MONGO_PING_ON_BOOT is disabled — skipping DB checks at boot"
            )
            app.logger.warning(f"  Target: {target}")
            _safe_run(app, "index creation", lambda: _ensure_indexes(app))
            _safe_run(app, "super admin bootstrap",
                      lambda: _bootstrap_super_admin(app))
            return

        app.logger.info(f"Connecting to MongoDB at {target}")

        ok, err = ping_mongo()
        if not ok:
            app.logger.error(f"✗ MongoDB unreachable: {err}")
            app.logger.error("")
            app.logger.error("Possible causes:")
            app.logger.error(
                "  1. The DB server's firewall is not allowing your current IP."
            )
            app.logger.error(
                "     → Whitelist your public IP on the DB host."
            )
            app.logger.error(
                "  2. mongod is not running, or is bound to 127.0.0.1 only."
            )
            app.logger.error(
                "  3. Your network (office/VPN) blocks outbound port 27017."
            )
            app.logger.error(
                "  4. MONGO_URI is wrong (host, port, credentials)."
            )
            app.logger.error("")
            app.logger.error(
                "To bypass this check temporarily, add to .env:  "
                "MONGO_PING_ON_BOOT=false"
            )
            raise RuntimeError(
                f"MongoDB is unreachable at boot ({target}). "
                "See the log above for likely causes."
            )

        app.logger.info("✓ MongoDB connection OK")

        _safe_run(app, "index creation", lambda: _ensure_indexes(app))
        _safe_run(app, "super admin bootstrap",
                  lambda: _bootstrap_super_admin(app))


def _describe_mongo_target(uri: str) -> str:
    """
    Return a log-safe description of the Mongo target: host:port/db.
    Never includes credentials.
    """
    if not uri:
        return "(MONGO_URI is empty)"
    try:
        p = urlparse(uri)
        host = p.hostname or "unknown-host"
        port = p.port or 27017
        db = (p.path or "/").lstrip("/") or "(default)"
        return f"{host}:{port}/{db}"
    except Exception:
        return "(unparseable MONGO_URI)"


def _safe_run(app: Flask, label: str, fn):
    """Run a bootstrap step, logging failures without crashing the boot."""
    try:
        fn()
    except Exception:
        app.logger.exception(f"⚠ {label} failed — continuing anyway")


# =====================================================================
# HELPERS
# =====================================================================
def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    return (
        accept["application/json"] >= accept["text/html"]
        and accept["application/json"] > 0
    )


def _ensure_indexes(app: Flask):
    """
    Create required indexes. Idempotent — safe to run on every boot.
    """
    from pymongo.errors import OperationFailure

    # Import here to avoid circular imports
    from extensions import (
        invoices, payments, announcements, messages,
        classes, subjects, attendance, grades,
    )

    specs = [
        # Schools
        (schools,       [("subscription_status", 1)],              {}),
        (schools,       [("created_at", -1)],                      {}),

        # Users
        (users,         [("email", 1)],                            {"unique": True}),
        (users,         [("school_id", 1), ("role", 1)],           {}),
        (users,         [("school_id", 1), ("linked_children", 1)],{}),

        # Students
        (students,      [("school_id", 1), ("admission_no", 1)],   {"unique": True}),
        (students,      [("school_id", 1), ("class_id", 1)],       {}),
        (students,      [("school_id", 1), ("user_id", 1)],        {}),

        # Staff
        (staff,         [("school_id", 1), ("user_id", 1)],        {"unique": True}),

        # Subscriptions
        (subscriptions, [("school_id", 1)],                        {"unique": True}),
        (subscriptions, [("status", 1), ("next_billing_date", 1)], {}),
        (subscriptions, [("paystack_subscription_code", 1)],       {}),

        # Audit logs
        (audit_logs,    [("timestamp", -1)],                       {}),
        (audit_logs,    [("school_id", 1), ("timestamp", -1)],     {}),

        # Tickets
        (tickets,       [("status", 1), ("created_at", -1)],       {}),

        # Webhook events
        (webhook_events, [("paystack_reference", 1)],              {}),
        (webhook_events, [("event", 1), ("processed", 1)],         {}),
        (webhook_events, [("created_at", -1)],                     {}),
        # Partial unique index — enforces idempotency at the DB level.
        (webhook_events,
         [("event", 1), ("paystack_reference", 1)],
         {"unique": True,
          "partialFilterExpression": {"processed": False},
          "name": "webhook_unprocessed_unique"}),

        # Invoices + payments
        (invoices, [("paystack_reference", 1)],                    {}),
        (invoices, [("school_id", 1), ("kind", 1)],                {}),
        (invoices, [("school_id", 1), ("student_id", 1)],          {}),
        (payments, [("paystack_reference", 1)],                    {}),
        (payments, [("school_id", 1), ("invoice_id", 1)],          {}),

        # Announcements
        (announcements, [("school_id", 1), ("audience", 1)],       {}),
        (announcements, [("school_id", 1), ("published_at", -1)],  {}),
        (announcements, [("school_id", 1), ("expires_at", 1)],     {}),

        # Messages
        (messages, [("school_id", 1), ("recipient_ids", 1)],       {}),
        (messages, [("school_id", 1), ("thread_id", 1)],           {}),

        # Classes / subjects / attendance / grades
        (classes,   [("school_id", 1), ("status", 1)],             {}),
        (subjects,  [("school_id", 1), ("status", 1)],             {}),
        (attendance,[("school_id", 1), ("class_id", 1), ("date", -1)], {}),
        (grades,    [("school_id", 1), ("student_id", 1)],         {}),
        (grades,    [("school_id", 1), ("class_id", 1), ("term", 1)], {}),
    ]

    for coll, keys, kwargs in specs:
        try:
            coll.create_index(keys, **kwargs)
        except OperationFailure as e:
            # Index already exists with the same options, or Mongo refused
            # the build (e.g. duplicate keys in the collection).
            app.logger.warning(
                f"Index warning on {coll.name} {keys}: {e}"
            )
        except Exception as e:
            # Unexpected — surface it loudly.
            app.logger.error(
                f"Index create failed on {coll.name} {keys} {kwargs}: {e}"
            )


def _bootstrap_super_admin(app: Flask):
    """
    Create a default super admin on first run.
    Override with env vars: SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD

    Race-safe: if multiple workers boot simultaneously, only one insert
    succeeds; the others catch DuplicateKeyError and exit cleanly.
    """
    from werkzeug.security import generate_password_hash
    from pymongo.errors import DuplicateKeyError

    if users.find_one({"role": "super_admin"}):
        return

    email = os.getenv("SUPER_ADMIN_EMAIL", "admin@everidemy.com").lower().strip()
    password = os.getenv("SUPER_ADMIN_PASSWORD", "ChangeMe123!")

    try:
        users.insert_one({
            "school_id": None,
            "name": "Super Admin",
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "super_admin",
            "email_verified": True,
            "linked_children": [],
            "must_reset_password": False,
            "created_at": datetime.utcnow(),
            "last_login": None,
        })
    except DuplicateKeyError:
        # Another worker beat us to it — fine.
        app.logger.info("Super admin already bootstrapped by another worker.")
        return

    # Only print in dev. In production, rely on the logger.
    if app.debug:
        print("=" * 60)
        print(f"  ✓ Bootstrapped super admin")
        print(f"    Email:    {email}")
        print(f"    Password: {password}")
        print("  ⚠️  Change this password immediately in production!")
        print("=" * 60)

    app.logger.info(f"✓ Bootstrapped super admin → {email}")


# =====================================================================
# ENTRY POINT (dev only)
# =====================================================================
app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5003"))
    # FLASK_DEBUG=true/false; falls back to FLASK_ENV for older setups.
    debug_env = os.getenv("FLASK_DEBUG") or os.getenv("FLASK_ENV", "development")
    debug = debug_env.lower() not in ("0", "false", "production")

    app.run(debug=debug, host="0.0.0.0", port=port)