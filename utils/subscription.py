"""
Everidemy — Subscription state helpers.

Subscription state lives in two places:
  - schools.subscription_status   → 'unpaid' | 'active' | 'past_due' | 'canceled'
  - subscriptions collection      → record of the last successful charge

There is NO trial period, NO grace period, and NO auto-renewal.
A school is locked the moment it is not 'active'.
Schools renew manually each month by paying again.
"""
from datetime import datetime, timedelta

from bson import ObjectId

from config import Config
from extensions import schools, subscriptions


# =========================================================
# LOW-LEVEL
# =========================================================
def _to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


# =========================================================
# READ
# =========================================================
def get_subscription(school_id) -> dict | None:
    """Return the subscription doc for a school (the last successful charge)."""
    oid = _to_oid(school_id)
    if not oid:
        return None
    return subscriptions.find_one({"school_id": oid})


def is_locked(school: dict) -> bool:
    """
    True if the school should be locked out of the platform.

    Rules (no trial, no grace):
      - suspended → locked
      - subscription_status != 'active' → locked
    """
    if not school:
        return True

    if school.get("suspended"):
        return True

    status = school.get("subscription_status") or "unpaid"
    return status != "active"


# Back-compat alias (used by templates/older code)
def is_expired(school: dict) -> bool:
    return is_locked(school)


def days_left(school: dict) -> int | None:
    """
    Days until the next manual renewal is due.
    - None if active but no next_billing_date recorded
    - otherwise max(0, next_billing_date - now).days
    - 0 if the school is locked
    """
    if not school:
        return 0

    if school.get("subscription_status") == "active":
        sub = get_subscription(school["_id"])
        if sub and sub.get("next_billing_date"):
            return max((sub["next_billing_date"] - datetime.utcnow()).days, 0)
        return None

    return 0


def is_trialing(school: dict) -> bool:
    """Always False now — kept for template compatibility."""
    return False


def is_past_due(school: dict) -> bool:
    return (school or {}).get("subscription_status") == "past_due"


def is_first_activation(school: dict) -> bool:
    """
    True if this school has never been active before
    (no subscription row, or the row has no last_payment_at).
    """
    sub = get_subscription((school or {}).get("_id"))
    if not sub:
        return True
    return not sub.get("last_payment_at")


# =========================================================
# WRITE
# =========================================================
def mark_active(school_id, plan: str = None, next_billing: datetime = None):
    """
    Called after a successful charge — unlocks the school.
    `next_billing` defaults to 30 days from now (manual renewal date).
    Returns a dict summarising what changed (useful for emails):
        { "is_first_activation": bool, "previous_status": str|None }
    """
    oid = _to_oid(school_id)
    if not oid:
        return {"is_first_activation": False, "previous_status": None}

    now = datetime.utcnow()
    if next_billing is None:
        next_billing = now + timedelta(days=30)

    # Was this the first activation ever?
    prev_sub = subscriptions.find_one({"school_id": oid})
    is_first = not (prev_sub and prev_sub.get("last_payment_at"))

    # Was the school previously active? (used to skip duplicate emails)
    prev_school = schools.find_one({"_id": oid}) or {}
    previous_status = prev_school.get("subscription_status")

    schools.update_one(
        {"_id": oid},
        {"$set": {
            "subscription_status": "active",
            "plan": plan or Config.PLAN_KEY,
            "updated_at": now,
        }},
    )

    subscriptions.update_one(
        {"school_id": oid},
        {
            "$set": {
                "status": "active",
                "next_billing_date": next_billing,
                "last_payment_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {
                "school_id": oid,
                "plan": plan or Config.PLAN_KEY,
                "created_at": now,
            },
        },
        upsert=True,
    )

    return {
        "is_first_activation": is_first,
        "previous_status": previous_status,
    }


def mark_past_due(school_id):
    """
    Called when a renewal is due but unpaid.
    With no grace period, this LOCKS the school immediately.
    """
    oid = _to_oid(school_id)
    if not oid:
        return
    now = datetime.utcnow()
    schools.update_one(
        {"_id": oid},
        {"$set": {"subscription_status": "past_due", "updated_at": now}},
    )
    subscriptions.update_one(
        {"school_id": oid},
        {"$set": {"status": "past_due", "updated_at": now}},
    )


def mark_canceled(school_id):
    """
    Called when the admin explicitly cancels.
    With no grace, this LOCKS the school immediately.
    """
    oid = _to_oid(school_id)
    if not oid:
        return
    now = datetime.utcnow()
    schools.update_one(
        {"_id": oid},
        {"$set": {"subscription_status": "canceled", "updated_at": now}},
    )
    subscriptions.update_one(
        {"school_id": oid},
        {"$set": {"status": "canceled", "canceled_at": now, "updated_at": now}},
    )


def mark_unpaid(school_id):
    """Reset a school back to 'unpaid' (e.g. after manual cleanup)."""
    oid = _to_oid(school_id)
    if not oid:
        return
    now = datetime.utcnow()
    schools.update_one(
        {"_id": oid},
        {"$set": {"subscription_status": "unpaid", "updated_at": now}},
    )
    subscriptions.update_one(
        {"school_id": oid},
        {"$set": {"status": "unpaid", "updated_at": now}},
    )