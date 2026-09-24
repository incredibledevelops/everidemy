"""
Billing blueprint — the school's subscription management.

Accessible to school admins only. Handles:
- GET  /school-admin/billing            → status, plan, invoice history
- POST /school-admin/billing/checkout   → start a one-time Paystack charge
- GET  /school-admin/billing/callback   → verify payment + activate for 30 days
- POST /school-admin/billing/cancel     → cancel (locks at end of paid period)

Manual monthly renewal: each month the school pays again through checkout.
There is no Paystack Plan and no auto-renewal subscription.
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
from utils.mailer import (
    send_subscription_activated_email,
    send_subscription_renewed_email,
)

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
    EXCEPT /callback (Paystack redirect).
    """
    if request.endpoint == "billing.callback":
        user = current_user()
        if not user:
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

    status = school.get("subscription_status", "unpaid")
    expired = sub_utils.is_expired(school)
    days_left = sub_utils.days_left(school)

    return render_template(
        "school_admin/billing.html",
        school=school,
        subscription=subscription,
        invoices=platform_invoices,
        status=status,
        expired=expired,
        days_left=days_left,
        amount=Config.PAYSTACK_PLATFORM_AMOUNT,
        public_key=Config.PAYSTACK_PUBLIC_KEY,
        currency=Config.PAYSTACK_PLATFORM_CURRENCY,
        plan_name=Config.PLAN_NAME,
    )


# =========================================================
# CHECKOUT
# =========================================================
@billing_bp.route("/checkout", methods=["POST"])
def checkout():
    """
    Initialize a ONE-TIME Paystack charge for this school's monthly
    subscription. No Paystack Plan is involved — schools renew manually.
    """
    school = g.school
    user = g.user

    # ---- 0. Config guard ----
    missing = []
    if not Config.PAYSTACK_SECRET_KEY:
        missing.append("PAYSTACK_SECRET_KEY")
    if not Config.PAYSTACK_PLATFORM_AMOUNT or int(Config.PAYSTACK_PLATFORM_AMOUNT) <= 0:
        missing.append("PAYSTACK_PLATFORM_AMOUNT")
    if not Config.PAYSTACK_PLATFORM_CURRENCY:
        missing.append("PAYSTACK_PLATFORM_CURRENCY")

    if missing:
        current_app.logger.error(
            f"[billing.checkout] Missing/invalid config: {', '.join(missing)}"
        )
        flash(
            "Online billing isn't configured correctly. "
            f"Missing: {', '.join(missing)}. Please contact support.",
            "error",
        )
        return redirect(_billing_url())

    current_app.logger.info(
        f"[billing.checkout] Starting checkout for school={school['_id']} "
        f"amount={Config.PAYSTACK_PLATFORM_AMOUNT} "
        f"currency={Config.PAYSTACK_PLATFORM_CURRENCY} "
        f"sk_prefix={Config.PAYSTACK_SECRET_KEY[:8]}"
    )

    # ---- 1. Ensure Paystack customer exists ----
    customer_code = school.get("paystack_customer_code")
    if not customer_code:
        parts = (user.get("name") or "").strip().split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""

        current_app.logger.info(
            f"[billing.checkout] Creating Paystack customer for {user['email']}"
        )
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
            current_app.logger.error(
                f"[billing.checkout] create_customer failed — response={customer}"
            )
            flash(
                "Could not create your Paystack customer profile. "
                "Please try again or contact support.",
                "error",
            )
            return redirect(_billing_url())

        customer_code = customer["customer_code"]
        schools.update_one(
            {"_id": school["_id"]},
            {"$set": {
                "paystack_customer_code": customer_code,
                "updated_at": datetime.utcnow(),
            }},
        )
        current_app.logger.info(
            f"[billing.checkout] Customer created: {customer_code}"
        )

    # ---- 2. Initialize a one-time transaction ----
    reference = f"EVD-SUB-{school['_id']}-{uuid.uuid4().hex[:12]}"
    amount_kobo = int(Config.PAYSTACK_PLATFORM_AMOUNT) * 100

    current_app.logger.info(
        f"[billing.checkout] Initializing one-time transaction — "
        f"ref={reference} amount_kobo={amount_kobo} customer={customer_code}"
    )

    init = paystack.initialize_transaction(
        email=user["email"],
        amount_kobo=amount_kobo,
        reference=reference,
        callback_url=url_for("billing.callback", _external=True),
        metadata={
            "school_id": str(school["_id"]),
            "school_name": school["name"],
            "purpose": "everidemy_subscription",
            "kind": "manual_monthly",
        },
    )

    if not init or not init.get("authorization_url"):
        current_app.logger.error(
            f"[billing.checkout] initialize_transaction failed — "
            f"response={init!r}"
        )
        current_app.logger.error(
            f"[billing.checkout] Config used: "
            f"base_url={Config.PAYSTACK_BASE_URL} "
            f"amount_kobo={amount_kobo} "
            f"currency={Config.PAYSTACK_PLATFORM_CURRENCY} "
            f"secret_key_prefix={Config.PAYSTACK_SECRET_KEY[:8] if Config.PAYSTACK_SECRET_KEY else 'NONE'}"
        )
        flash(
            "Could not start checkout. Check the server logs for the "
            "Paystack error, or contact support.",
            "error",
        )
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

    current_app.logger.info(
        f"[billing.checkout] Redirecting to Paystack: "
        f"{init['authorization_url']}"
    )
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
        current_app.logger.warning(
            f"[billing.callback] Verification failed — ref={reference} "
            f"tx={tx!r}"
        )
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

    # ---- Next renewal = 30 days from now (manual) ----
    next_billing = datetime.utcnow() + timedelta(days=30)

    # ---- Idempotency + activation ----
    existing = invoices.find_one({
        "school_id": school["_id"],
        "paystack_reference": reference,
    })

    is_first = False  # default — safe even if we skip the block

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

        result = sub_utils.mark_active(
            school["_id"],
            plan=Config.PLAN_KEY,
            next_billing=next_billing,
        )
        is_first = bool(result.get("is_first_activation"))

        School.log(school["_id"], g.user["_id"], "billing.payment_confirmed", {
            "reference": reference,
            "amount": amount_paid,
            "first_activation": is_first,
            "source": "callback",
        })

        # ---- Email the school admin ----
        try:
            login_url = url_for("auth.login", _external=True)
            if is_first:
                send_subscription_activated_email(
                    school_name=school["name"],
                    email=g.user["email"],
                    amount=amount_paid,
                    next_billing=next_billing,
                    login_url=login_url,
                )
            else:
                send_subscription_renewed_email(
                    school_name=school["name"],
                    email=g.user["email"],
                    amount=amount_paid,
                    next_billing=next_billing,
                )
        except Exception:
            current_app.logger.exception("Subscription email failed")

    if is_first:
        flash(
            f"Payment confirmed — {format_money(amount_paid)}. "
            f"Your school portal is now active until "
            f"{next_billing.strftime('%b %d, %Y')}.",
            "success",
        )
    else:
        flash(
            f"Renewal confirmed — {format_money(amount_paid)}. "
            f"Your subscription is active until "
            f"{next_billing.strftime('%b %d, %Y')}.",
            "success",
        )

    return redirect(_billing_url())


# =========================================================
# CANCEL
# =========================================================
@billing_bp.route("/cancel", methods=["POST"])
def cancel():
    """
    Cancel the school's subscription.
    Because there is no auto-renewal, "cancel" simply means:
    don't renew at the end of the paid period.
    """
    school = g.school
    sub = sub_utils.get_subscription(school["_id"])

    if not sub:
        flash("No subscription found.", "error")
        return redirect(_billing_url())

    sub_utils.mark_canceled(school["_id"])

    School.log(school["_id"], g.user["_id"], "billing.canceled", {})

    flash(
        "Your subscription has been canceled. You'll keep access until the end "
        "of the current billing period, then you'll need to re-subscribe.",
        "success",
    )
    return redirect(_billing_url())