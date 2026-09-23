"""
Billing blueprint — the school's subscription management.

Accessible to school admins only. Handles:
- GET  /school-admin/billing            → status, plan, invoice history
- POST /school-admin/billing/checkout   → start Paystack subscription
- GET  /school-admin/billing/callback   → verify payment + activate
- POST /school-admin/billing/cancel     → cancel subscription

The webhook (routes/webhooks.py) is the authoritative source of truth;
this blueprint exists so the admin gets immediate feedback after checkout.
"""
from datetime import datetime, timedelta
import uuid

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, current_app,
)
from bson import ObjectId

from config import Config
from extensions import schools, invoices
from models import School, format_money
from utils.auth import current_user, current_school
from utils import paystack
from utils import subscription as sub_utils
from utils.mailer import send_billing_confirmed_email


billing_bp = Blueprint(
    "billing", __name__,
    url_prefix="/school-admin/billing",
)


# =========================================================
# HELPERS
# =========================================================
def _to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _billing_url():
    return url_for("billing.dashboard")


# =========================================================
# GATE — school_admin only, but /callback is exempt
# =========================================================
@billing_bp.before_request
def _gate():
    """
    Every route in this blueprint requires a logged-in school_admin,
    EXCEPT /callback (Paystack redirect) and /webhook-adjacent paths.

    We do the check manually instead of stacking @role_required so we
    can bypass the guard cleanly for the callback endpoint.
    """
    # The callback must work even if the school state changed mid-flow
    if request.endpoint == "billing.callback":
        user = current_user()
        if not user:
            # Defensive: if the session was lost, we still can't activate
            # because we don't know which school to activate.
            flash("Please log in to complete your payment.", "warning")
            return redirect(url_for("auth.login"))
        g.user = user
        g.school = current_school()
        return None

    user = current_user()
    if not user:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("auth.login"))

    if user.get("role") != "school_admin":
        from flask import abort
        abort(403)

    school = current_school()
    if not school:
        flash("No school is linked to your account.", "error")
        return redirect(url_for("auth.logout"))

    if school.get("suspended"):
        flash("Your school account is suspended. Please contact support.", "error")
        return redirect(url_for("auth.logout"))

    g.user = user
    g.school = school
    return None


# =========================================================
# DASHBOARD
# =========================================================
@billing_bp.route("/", methods=["GET"])
def dashboard():
    """Show current subscription, plan, and invoice history."""
    school = g.school
    subscription = sub_utils.get_subscription(school["_id"])

    platform_invoices = list(
        invoices.find({
            "school_id": school["_id"],
            "kind": "platform",
        })
        .sort("created_at", -1)
        .limit(24)
    )

    status = school.get("subscription_status", "trialing")
    days_left = sub_utils.days_left(school)
    expired = sub_utils.is_expired(school)

    return render_template(
        "school_admin/billing.html",
        school=school,
        subscription=subscription,
        invoices=platform_invoices,
        status=status,
        days_left=days_left,
        expired=expired,
        amount=Config.PAYSTACK_PLATFORM_AMOUNT,
        plan_code=Config.PAYSTACK_PLAN_CODE,
        public_key=Config.PAYSTACK_PUBLIC_KEY,
        currency=Config.PAYSTACK_PLATFORM_CURRENCY,
        grace_days=Config.PAYSTACK_GRACE_DAYS,
        plan_name=Config.PLAN_NAME,
    )


# =========================================================
# CHECKOUT
# =========================================================
@billing_bp.route("/checkout", methods=["POST"])
def checkout():
    """
    Initialize a Paystack subscription for this school:
      1. Ensure a Paystack customer exists.
      2. Initialize a transaction with the platform plan code.
      3. Redirect to Paystack checkout.
    """
    school = g.school
    user = g.user

    # ---- 0. Config guard ----
    if not Config.PAYSTACK_SECRET_KEY or not Config.PAYSTACK_PLAN_CODE:
        flash(
            "Online billing isn't configured. Please contact Everidemy support.",
            "error",
        )
        return redirect(_billing_url())

    # ---- 1. Ensure Paystack customer exists ----
    customer_code = school.get("paystack_customer_code")
    if not customer_code:
        parts = (user.get("name") or "").strip().split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""

        customer = paystack.create_customer(
            email=user["email"],
            first_name=first,
            last_name=last,
            phone=school.get("phone", ""),
            metadata={
                "school_id": str(school["_id"]),
                "school_name": school["name"],
            },
        )
        if not customer or not customer.get("customer_code"):
            flash("Could not reach Paystack. Please try again.", "error")
            return redirect(_billing_url())

        customer_code = customer["customer_code"]
        schools.update_one(
            {"_id": school["_id"]},
            {"$set": {
                "paystack_customer_code": customer_code,
                "updated_at": datetime.utcnow(),
            }},
        )

    # ---- 2. Initialize transaction ----
    reference = f"EVD-SUB-{school['_id']}-{uuid.uuid4().hex[:12]}"
    amount_kobo = int(Config.PAYSTACK_PLATFORM_AMOUNT) * 100

    init = paystack.initialize_transaction(
        email=user["email"],
        amount_kobo=amount_kobo,
        reference=reference,
        callback_url=url_for("billing.callback", _external=True),
        plan_code=Config.PAYSTACK_PLAN_CODE,
        metadata={
            "school_id": str(school["_id"]),
            "school_name": school["name"],
            "purpose": "everidemy_subscription",
        },
    )
    if not init or not init.get("authorization_url"):
        flash("Could not start checkout. Please try again.", "error")
        return redirect(_billing_url())

    # Store the reference so we can reconcile
    schools.update_one(
        {"_id": school["_id"]},
        {"$set": {
            "pending_checkout_reference": reference,
            "updated_at": datetime.utcnow(),
        }},
    )

    School.log(school["_id"], user["_id"], "billing.checkout_started", {
        "reference": reference,
        "amount": Config.PAYSTACK_PLATFORM_AMOUNT,
    })

    return redirect(init["authorization_url"])


# =========================================================
# CALLBACK
# =========================================================
@billing_bp.route("/callback")
def callback():
    """
    Paystack redirects here after checkout.
    Verify the transaction server-side before activating.
    The webhook is the authoritative source; this gives instant feedback.
    """
    reference = (request.args.get("reference") or "").strip()
    if not reference:
        flash("Missing payment reference.", "error")
        return redirect(_billing_url())

    school = g.school
    if not school:
        flash("Session expired. Please log in and try again.", "error")
        return redirect(url_for("auth.login"))

    # ---- Verify with Paystack ----
    tx = paystack.verify_transaction(reference)
    if not tx or tx.get("status") != "success":
        flash(
            "We couldn't confirm your payment yet. If you were charged, "
            "your subscription will activate within a few minutes.",
            "warning",
        )
        return redirect(_billing_url())

    # ---- Verify the reference belongs to THIS school ----
    meta = tx.get("metadata") or {}
    meta_school_id = str(meta.get("school_id") or "")
    if meta_school_id and meta_school_id != str(school["_id"]):
        current_app.logger.warning(
            f"[billing.callback] School mismatch — "
            f"reference={reference} meta_school={meta_school_id} "
            f"current_school={school['_id']}"
        )
        flash(
            "This payment reference belongs to a different school. "
            "Please contact support.",
            "error",
        )
        return redirect(_billing_url())

    # ---- Verify amount matches plan ----
    amount_paid = (tx.get("amount", 0) / 100.0)
    expected = float(Config.PAYSTACK_PLATFORM_AMOUNT)

    if amount_paid < expected:
        current_app.logger.warning(
            f"[billing.callback] Underpayment — reference={reference} "
            f"paid={amount_paid} expected={expected}"
        )
        School.log(school["_id"], g.user["_id"], "billing.underpayment", {
            "reference": reference,
            "paid": amount_paid,
            "expected": expected,
        })
        flash(
            "We received a payment, but the amount doesn't match the plan price. "
            "Please contact support if you believe this is an error.",
            "error",
        )
        return redirect(_billing_url())

    # ---- Extract subscription info ----
    plan_obj = tx.get("plan") or {}
    sub_code = plan_obj.get("subscription_code")
    email_token = plan_obj.get("email_token")

    # Prefer Paystack's next_payment_date if present; else 30 days
    next_billing = None
    nxt = plan_obj.get("next_payment_date") or tx.get("next_payment_date")
    if nxt:
        try:
            next_billing = datetime.fromisoformat(
                nxt.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            next_billing = None
    if not next_billing:
        next_billing = datetime.utcnow() + timedelta(days=30)

    if not sub_code:
        current_app.logger.warning(
            f"[billing.callback] Paystack returned no subscription_code "
            f"for reference={reference}"
        )

    # ---- Idempotency + activation ----
    existing = invoices.find_one({
        "school_id": school["_id"],
        "paystack_reference": reference,
    })

    if not existing:
        invoices.insert_one({
            "school_id": school["_id"],
            "kind": "platform",
            "paystack_reference": reference,
            "amount": amount_paid,
            "currency": Config.PAYSTACK_PLATFORM_CURRENCY,
            "status": "paid",
            "paid_at": datetime.utcnow(),
            "description": f"{Config.PLAN_NAME} subscription",
            "created_at": datetime.utcnow(),
        })

        sub_utils.mark_active(
            school["_id"],
            plan=Config.PLAN_KEY,
            next_billing=next_billing,
            subscription_code=sub_code,
            email_token=email_token,
        )

        School.log(school["_id"], g.user["_id"], "billing.payment_confirmed", {
            "reference": reference,
            "amount": amount_paid,
            "subscription_code": sub_code,
            "source": "callback",
        })

        # Confirmation email (best-effort)
        try:
            send_billing_confirmed_email(
                school_name=school["name"],
                email=g.user["email"],
                amount=amount_paid,
                next_billing=next_billing,
            )
        except Exception:
            current_app.logger.exception("Confirmation email failed")

    flash(
        f"Payment confirmed — {format_money(amount_paid)}. "
        f"Your subscription is now active.",
        "success",
    )
    return redirect(_billing_url())


# =========================================================
# CANCEL
# =========================================================
@billing_bp.route("/cancel", methods=["POST"])
def cancel():
    """Cancel the school's subscription via Paystack."""
    school = g.school
    sub = sub_utils.get_subscription(school["_id"])

    if not sub:
        flash("No subscription found.", "error")
        return redirect(_billing_url())

    sub_code = sub.get("paystack_subscription_code")
    email_token = sub.get("paystack_email_token")

    if sub_code and email_token:
        ok = paystack.disable_subscription(sub_code, email_token)
        if not ok:
            flash(
                "Could not cancel via Paystack. Please try again, "
                "or contact support if the issue persists.",
                "error",
            )
            return redirect(_billing_url())
    else:
        current_app.logger.warning(
            f"[billing.cancel] Missing sub_code or email_token for "
            f"school={school['_id']}"
        )

    sub_utils.mark_canceled(school["_id"], subscription_code=sub_code)

    School.log(school["_id"], g.user["_id"], "billing.canceled", {
        "subscription_code": sub_code,
    })

    flash(
        "Your subscription has been canceled. You'll keep access until the end "
        "of the current billing period.",
        "success",
    )
    return redirect(_billing_url())