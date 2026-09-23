"""
Webhook blueprint — receives Paystack events.

Rules:
- Use the raw request body to verify x-paystack-signature.
- Return 200 in a few seconds.
- Idempotent — Paystack retries for up to 72 hours.
"""
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from config import Config
from extensions import (
    schools, users, invoices, payments,
    audit_logs, webhook_events, subscriptions,
)
from utils import paystack
from utils import subscription as sub_utils
from utils.mailer import (
    send_billing_confirmed_email,
    send_billing_failed_email,
)


webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


# =========================================================
# HELPERS
# =========================================================
def _school_id_from_metadata(metadata: dict):
    if not metadata:
        return None
    sid = metadata.get("school_id")
    try:
        return ObjectId(str(sid))
    except Exception:
        return None


def _owner_email(school_id):
    """Best-effort — the first school_admin for a school."""
    user = users.find_one({"school_id": school_id, "role": "school_admin"})
    return user["email"] if user else None


def _recompute_invoice_webhook(invoice_id, school_id):
    """
    Webhook-safe invoice recompute.
    Does NOT depend on `g.school` / `tenant_filter()` — takes school_id explicitly.
    """
    try:
        oid = ObjectId(str(invoice_id))
    except Exception:
        return None

    inv = invoices.find_one({"_id": oid, "school_id": school_id})
    if not inv:
        return None

    pipeline = [
        {"$match": {"invoice_id": oid, "school_id": school_id}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    res = list(payments.aggregate(pipeline))
    paid = res[0]["total"] if res else 0

    due = inv.get("amount_due", 0) or 0
    balance = max(due - paid, 0)

    # ── FIXED: handle waived / zero-amount / paid-state edge cases ──
    if inv.get("status") == "waived":
        status = "waived"
        balance = 0
    elif due <= 0:
        # Zero-value invoice: paid if anything was paid, else unpaid
        status = "paid" if paid > 0 else "unpaid"
        balance = 0
    elif paid >= due:
        status = "paid"
    elif paid > 0:
        status = "partial"
    else:
        status = "unpaid"

    invoices.update_one(
        {"_id": oid, "school_id": school_id},
        {"$set": {
            "amount_paid": paid,
            "balance":     balance,
            "status":      status,
            "updated_at":  datetime.utcnow(),
        }},
    )
    return {"amount_paid": paid, "balance": balance, "status": status}


# =========================================================
# WEBHOOK ENDPOINT
# =========================================================
@webhooks_bp.route("/paystack", methods=["POST"])
def paystack_webhook():
    # ---- 1. Verify signature ----
    raw_body = request.get_data(cache=False, as_text=False)
    signature = request.headers.get("x-paystack-signature", "")

    if not paystack.verify_webhook_signature(raw_body, signature):
        current_app.logger.warning("Paystack webhook: invalid signature")
        return jsonify(status="error", message="Invalid signature"), 401

    # ---- 2. Parse payload ----
    payload = request.get_json(force=True, silent=True) or {}
    event = payload.get("event", "unknown")
    data = payload.get("data", {}) or {}
    reference = data.get("reference")

    # ---- 3. Record raw event (dedupe-friendly) ----
    event_id = None
    try:
        res = webhook_events.update_one(
            {
                "event": event,
                "paystack_reference": reference,
                "processed": False,
            },
            {
                "$setOnInsert": {
                    "event": event,
                    "paystack_reference": reference,
                    "payload": payload,
                    "verified": True,
                    "processed": False,
                    "created_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )
        event_id = res.upserted_id
        if event_id is None:
            # Existing pending event — find it to mark later
            existing = webhook_events.find_one({
                "event": event,
                "paystack_reference": reference,
                "processed": False,
            })
            if existing:
                event_id = existing["_id"]
    except DuplicateKeyError:
        # Unique index (if configured) caught it — fetch the row
        existing = webhook_events.find_one({
            "event": event,
            "paystack_reference": reference,
        })
        if existing:
            event_id = existing["_id"]

    # ---- 4. Route by event type ----
    try:
        if event == "charge.success":
            _handle_charge_success(data)
        elif event == "subscription.create":
            _handle_subscription_create(data)
        elif event in ("subscription.disable", "subscription.not_renew"):
            _handle_subscription_disable(data)
        elif event == "invoice.payment_failed":
            _handle_invoice_payment_failed(data)
        else:
            current_app.logger.info(f"Paystack webhook: unhandled event '{event}'")
    except Exception:
        current_app.logger.exception(f"Webhook failed to process '{event}'")

    # ---- 5. Mark THIS event row processed (by _id, not by reference) ----
    if event_id:
        webhook_events.update_one(
            {"_id": event_id},
            {"$set": {"processed": True, "processed_at": datetime.utcnow()}},
        )
    else:
        # Fallback — mark any pending rows for this event+reference
        webhook_events.update_many(
            {"event": event, "paystack_reference": reference, "processed": False},
            {"$set": {"processed": True, "processed_at": datetime.utcnow()}},
        )

    return jsonify(status="ok"), 200


# =========================================================
# EVENT HANDLERS
# =========================================================
def _handle_charge_success(data: dict):
    """
    A successful charge.

    Case A: Everidemy platform subscription (purpose=everidemy_subscription)
    Case B: A parent paying a fee invoice   (purpose=fee_payment)
    """
    metadata = data.get("metadata") or {}
    reference = data.get("reference")
    amount_paid = (data.get("amount", 0) / 100.0)
    purpose = metadata.get("purpose")

    # ---------- Case A: platform subscription ----------
    if purpose == "everidemy_subscription":
        school_id = _school_id_from_metadata(metadata)
        if not school_id:
            current_app.logger.warning("charge.success: missing school_id")
            return

        # ── FIXED: amount check before activating ──
        expected = float(Config.PAYSTACK_PLATFORM_AMOUNT)
        if amount_paid + 0.01 < expected:   # allow tiny float rounding
            current_app.logger.warning(
                f"[webhook/charge.success] Underpayment — "
                f"ref={reference} paid={amount_paid} expected={expected}"
            )
            audit_logs.insert_one({
                "school_id": school_id,
                "actor_id": None,
                "action": "billing.underpayment",
                "meta": {
                    "reference": reference,
                    "paid": amount_paid,
                    "expected": expected,
                    "source": "webhook",
                },
                "timestamp": datetime.utcnow(),
            })
            return

        plan_obj = data.get("plan") or {}
        sub_code = plan_obj.get("subscription_code")
        email_token = plan_obj.get("email_token")

        # Fallback: some Paystack flows send the tokens under data.subscription
        if not email_token:
            email_token = (data.get("subscription") or {}).get("email_token")
        if not sub_code:
            sub_code = (data.get("subscription") or {}).get("subscription_code")

        next_billing = datetime.utcnow() + timedelta(days=30)

        # ── FIXED: race-safe idempotency using upsert ──
        result = invoices.update_one(
            {
                "paystack_reference": reference,
                "school_id": school_id,
            },
            {
                "$setOnInsert": {
                    "school_id": school_id,
                    "kind": "platform",
                    "paystack_reference": reference,
                    "amount": amount_paid,
                    "currency": Config.PAYSTACK_PLATFORM_CURRENCY,
                    "status": "paid",
                    "paid_at": datetime.utcnow(),
                    "description": f"{Config.PLAN_NAME} subscription",
                    "created_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )

        if result.upserted_id is None:
            # Already processed by the callback or a previous webhook
            current_app.logger.info(
                f"charge.success: already processed ref={reference}"
            )
            return

        sub_utils.mark_active(
            school_id,
            plan=Config.PLAN_KEY,
            next_billing=next_billing,
            subscription_code=sub_code,
            email_token=email_token,
        )

        audit_logs.insert_one({
            "school_id": school_id,
            "actor_id": None,
            "action": "billing.payment_confirmed",
            "meta": {
                "reference": reference,
                "amount": amount_paid,
                "subscription_code": sub_code,
                "source": "webhook",
            },
            "timestamp": datetime.utcnow(),
        })

        try:
            school = schools.find_one({"_id": school_id})
            owner_email = _owner_email(school_id)
            if school and owner_email:
                send_billing_confirmed_email(
                    school_name=school["name"],
                    email=owner_email,
                    amount=amount_paid,
                    next_billing=next_billing,
                )
        except Exception:
            current_app.logger.exception("Confirmation email failed")

        return

    # ---------- Case B: fee invoice payment ----------
    if purpose == "fee_payment":
        invoice_id = metadata.get("invoice_id")
        school_id = _school_id_from_metadata(metadata)

        if not invoice_id or not school_id:
            current_app.logger.warning(
                f"charge.success/fee_payment: missing invoice_id or school_id "
                f"(ref={reference})"
            )
            return

        try:
            inv_oid = ObjectId(str(invoice_id))
        except Exception:
            current_app.logger.warning("charge.success: invalid invoice_id")
            return

        inv = invoices.find_one({"_id": inv_oid, "school_id": school_id})
        if not inv:
            current_app.logger.warning(
                f"charge.success: invoice {invoice_id} not found"
            )
            return

        # ── FIXED: race-safe idempotency using upsert ──
        result = payments.update_one(
            {
                "school_id": school_id,
                "paystack_reference": reference,
            },
            {
                "$setOnInsert": {
                    "school_id":          school_id,
                    "invoice_id":         inv_oid,
                    "student_id":         inv.get("student_id"),
                    "amount":             amount_paid,
                    "currency":           Config.CURRENCY,
                    "channel":            data.get("channel", "paystack"),
                    "reference":          reference,
                    "paystack_reference": reference,
                    "note":               "Online payment via Paystack (webhook)",
                    "paid_at":            datetime.utcnow(),
                    "recorded_by":        None,
                    "created_at":         datetime.utcnow(),
                },
            },
            upsert=True,
        )

        if result.upserted_id is None:
            current_app.logger.info(
                f"charge.success/fee_payment: already processed ref={reference}"
            )
            return

        _recompute_invoice_webhook(inv_oid, school_id)

        audit_logs.insert_one({
            "school_id": school_id,
            "actor_id": None,
            "action": "payment.recorded",
            "meta": {
                "invoice_id": str(inv_oid),
                "invoice_no": inv.get("invoice_no"),
                "amount":     amount_paid,
                "channel":    data.get("channel", "paystack"),
                "reference":  reference,
                "source":     "webhook",
            },
            "timestamp": datetime.utcnow(),
        })
        return

    # ---------- Anything else: log and move on ----------
    current_app.logger.info(
        f"charge.success: unhandled (reference={reference}, purpose={purpose})"
    )


def _handle_subscription_create(data: dict):
    """
    Paystack created a subscription.
    Persist subscription_code + email_token on the school's subscription row.
    """
    metadata = data.get("metadata") or {}
    school_id = _school_id_from_metadata(metadata)

    # Fallback: look up by customer email — restrict to school_admins
    if not school_id:
        customer = data.get("customer") or {}
        email = (customer.get("email") or "").lower().strip()
        if email:
            user = users.find_one({"email": email, "role": "school_admin"})
            if user and user.get("school_id"):
                school_id = user["school_id"]

    if not school_id:
        current_app.logger.warning("subscription.create: could not resolve school")
        return

    sub_code = data.get("subscription_code")
    email_token = data.get("email_token")
    next_billing = data.get("next_payment_date")

    # ── FIXED: only $set fields we actually have, to avoid clobbering
    #           existing values with None on partial payloads ──
    set_ops = {
        "status": "active",
        "updated_at": datetime.utcnow(),
    }
    if sub_code:
        set_ops["paystack_subscription_code"] = sub_code
    if email_token:
        set_ops["paystack_email_token"] = email_token
    if next_billing:
        try:
            set_ops["next_billing_date"] = datetime.fromisoformat(
                next_billing.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            pass

    subscriptions.update_one(
        {"school_id": school_id},
        {
            "$set": set_ops,
            "$setOnInsert": {
                "school_id": school_id,
                "plan": Config.PLAN_KEY,
                "created_at": datetime.utcnow(),
            },
        },
        upsert=True,
    )

    # ── FIXED: don't activate a suspended school ──
    school = schools.find_one({"_id": school_id})
    if school and not school.get("suspended"):
        schools.update_one(
            {"_id": school_id},
            {"$set": {
                "subscription_status": "active",
                "updated_at": datetime.utcnow(),
            }},
        )

    audit_logs.insert_one({
        "school_id": school_id,
        "actor_id": None,
        "action": "billing.subscription_created",
        "meta": {"subscription_code": sub_code, "source": "webhook"},
        "timestamp": datetime.utcnow(),
    })


def _handle_subscription_disable(data: dict):
    """
    Subscription canceled / not renewing.
    Mark the school canceled but keep access until next_billing_date.
    """
    sub_code = data.get("subscription_code")
    if not sub_code:
        current_app.logger.warning("subscription.disable: no subscription_code")
        return

    sub = subscriptions.find_one({"paystack_subscription_code": sub_code})
    if not sub:
        current_app.logger.warning(f"subscription.disable: unknown code {sub_code}")
        return

    school_id = sub["school_id"]
    sub_utils.mark_canceled(school_id, subscription_code=sub_code)

    audit_logs.insert_one({
        "school_id": school_id,
        "actor_id": None,
        "action": "billing.subscription_canceled",
        "meta": {"subscription_code": sub_code, "source": "webhook"},
        "timestamp": datetime.utcnow(),
    })


def _handle_invoice_payment_failed(data: dict):
    """
    A renewal charge failed.
    Mark the school past_due and email the owner.
    """
    metadata = data.get("metadata") or {}
    school_id = _school_id_from_metadata(metadata)

    sub_code = (data.get("subscription") or {}).get("subscription_code")
    if not school_id and sub_code:
        sub = subscriptions.find_one({"paystack_subscription_code": sub_code})
        if sub:
            school_id = sub["school_id"]

    if not school_id:
        current_app.logger.warning("invoice.payment_failed: could not resolve school")
        return

    sub_utils.mark_past_due(school_id, subscription_code=sub_code)

    amount = (data.get("amount", 0) / 100.0)
    school = schools.find_one({"_id": school_id})
    owner_email = _owner_email(school_id)

    try:
        if school and owner_email:
            send_billing_failed_email(
                school_name=school["name"],
                email=owner_email,
                amount=amount,
                grace_days=Config.PAYSTACK_GRACE_DAYS,
            )
    except Exception:
        current_app.logger.exception("Billing failed email failed")

    audit_logs.insert_one({
        "school_id": school_id,
        "actor_id": None,
        "action": "billing.payment_failed",
        "meta": {
            "subscription_code": sub_code,
            "amount": amount,
            "source": "webhook",
        },
        "timestamp": datetime.utcnow(),
    })