"""
Everidemy — Application Factory
"""
import os
from datetime import datetime

from flask import Flask, render_template, g, request, jsonify

from config import Config
from extensions import (
    schools, users, students, staff, audit_logs,
    tickets, subscriptions, webhook_events, mail,
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

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(super_admin_bp)
    app.register_blueprint(school_admin_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(parent_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(webhooks_bp)

    # ------------------------------------------------------------
    # Before-request: load user/school into g
    # ------------------------------------------------------------
    @app.before_request
    def load_user_context():
        g.user = current_user()
        g.school = current_school() if g.user else None

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
        return resp

    # ------------------------------------------------------------
    # Bootstrap: indexes + super admin
    # ------------------------------------------------------------
    with app.app_context():
        _ensure_indexes(app)
        _bootstrap_super_admin(app)

    return app


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
    specs = [
        # (collection, keys, kwargs)
        (schools,       [("subscription_status", 1)],              {}),
        (schools,       [("created_at", -1)],                      {}),

        (users,         [("email", 1)],                            {"unique": True}),
        (users,         [("school_id", 1), ("role", 1)],           {}),

        (students,      [("school_id", 1), ("admission_no", 1)],   {"unique": True}),
        (students,      [("school_id", 1), ("class_id", 1)],       {}),

        (staff,         [("school_id", 1), ("user_id", 1)],        {"unique": True}),

        (subscriptions, [("school_id", 1)],                        {"unique": True}),
        (subscriptions, [("status", 1), ("next_billing_date", 1)], {}),
        (subscriptions, [("paystack_subscription_code", 1)],       {}),

        (audit_logs,    [("timestamp", -1)],                       {}),
        (audit_logs,    [("school_id", 1), ("timestamp", -1)],     {}),

        (tickets,       [("status", 1), ("created_at", -1)],       {}),

        # Webhook idempotency + fast lookups
        (webhook_events, [("paystack_reference", 1)],              {}),
        (webhook_events, [("event", 1), ("processed", 1)],         {}),
        (webhook_events, [("created_at", -1)],                     {}),

        # Invoice/payment lookups for webhook processing
        # (these come from extensions, so import locally to avoid
        #  circular imports at module load time)
    ]

    # Add invoice + payment indexes
    from extensions import invoices, payments
    specs += [
        (invoices, [("paystack_reference", 1)],            {}),
        (invoices, [("school_id", 1), ("kind", 1)],        {}),
        (invoices, [("school_id", 1), ("student_id", 1)],  {}),
        (payments, [("paystack_reference", 1)],            {}),
        (payments, [("school_id", 1), ("invoice_id", 1)],  {}),
    ]

    for coll, keys, kwargs in specs:
        try:
            coll.create_index(keys, **kwargs)
        except Exception as e:
            app.logger.warning(f"Index create failed on {coll.name} {keys}: {e}")


def _bootstrap_super_admin(app: Flask):
    """
    Create a default super admin on first run.
    Override with env vars: SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD
    """
    from werkzeug.security import generate_password_hash

    if users.find_one({"role": "super_admin"}):
        return

    email = os.getenv("SUPER_ADMIN_EMAIL", "admin@everidemy.com").lower().strip()
    password = os.getenv("SUPER_ADMIN_PASSWORD", "ChangeMe123!")

    users.insert_one({
        "school_id": None,
        "name": "Super Admin",
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": "super_admin",
        "email_verified": True,
        "linked_children": [],
        "created_at": datetime.utcnow(),
        "last_login": None,
    })

    app.logger.info(f"✓ Bootstrapped super admin → {email}")
    print("=" * 60)
    print(f"  ✓ Bootstrapped super admin")
    print(f"    Email:    {email}")
    print(f"    Password: {password}")
    print("  ⚠️  Change this password immediately in production!")
    print("=" * 60)


# =====================================================================
# ENTRY POINT
# =====================================================================
app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5003"))
    debug = os.getenv("FLASK_ENV", "development") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)