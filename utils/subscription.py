"""
Everidemy — Subscription state helpers.

Subscription state lives in two places:
  - schools.subscription_status   → 'trialing' | 'active' | 'past_due' | 'canceled'
  - subscriptions collection      → full record of the Paystack subscription
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
    """Return the subscription doc for a school."""
    oid = _to_oid(school_id)
    if not oid:
        return None
    return subscriptions.find_one({"school_id": oid})


def is_expired(school: dict) -> bool:
    """
    True if the school's access has fully lapsed.
    A school is expired if:
      - suspended
      - OR past_due/canceled AND grace period has passed
      - OR trialing AND trial + grace has passed
    """
    if not school:
        return True

    if school.get("suspended"):
        return True

    status = school.get("subscription_status", "trialing")
    now = datetime.utcnow()
    grace = timedelta(days=Config.PAYSTACK_GRACE_DAYS)

    if status == "active":
        return False

    if status == "trialing":
        trial_end = school.get("trial_ends_at")
        if not trial_end:
            return False
        return now > (trial_end + grace)

    if status in ("past_due", "canceled"):
        sub = get_subscription(school.get("_id"))
        if sub and sub.get("next_billing_date"):
            return now > (sub["next_billing_date"] + grace)
        return True

    return True


def days_left(school: dict) -> int | None:
    """Number of days left on trial / billing cycle / grace period."""
    if not school:
        return 0

    status = school.get("subscription_status", "trialing")
    now = datetime.utcnow()
    grace = timedelta(days=Config.PAYSTACK_GRACE_DAYS)

    if status == "trialing":
        trial_end = school.get("trial_ends_at")
        if not trial_end:
            return None
        return max((trial_end - now).days, 0)

    if status == "active":
        sub = get_subscription(school.get("_id"))
        if sub and sub.get("next_billing_date"):
            return max((sub["next_billing_date"] - now).days, 0)
        return None

    if status in ("past_due", "canceled"):
        sub = get_subscription(school.get("_id"))
        if sub and sub.get("next_billing_date"):
            grace_end = sub["next_billing_date"] + grace
            return max((grace_end - now).days, 0)
        return 0

    return 0


def is_trialing(school: dict) -> bool:
    return (school or {}).get("subscription_status") == "trialing"


def is_past_due(school: dict) -> bool:
    return (school or {}).get("subscription_status") == "past_due"


# =========================================================
# WRITE
# =========================================================
def mark_active(school_id, plan: str = None, next_billing: datetime = None,
                subscription_code: str = None, email_token: str = None):
    """
    Called after a successful charge.
    Preserves existing subscription_code / email_token if the new ones are None.
    """
    oid = _to_oid(school_id)
    if not oid:
        return

    now = datetime.utcnow()
    next_billing = next_billing or (now + timedelta(days=30))

    schools.update_one(
        {"_id": oid},
        {"$set": {
            "subscription_status": "active",
            "plan": plan or Config.PLAN_KEY,
            "updated_at": now,
        }},
    )

    set_ops = {
        "status": "active",
        "next_billing_date": next_billing,
        "updated_at": now,
    }
    if subscription_code:
        set_ops["paystack_subscription_code"] = subscription_code
    if email_token:
        set_ops["paystack_email_token"] = email_token

    subscriptions.update_one(
        {"school_id": oid},
        {
            "$set": set_ops,
            "$setOnInsert": {
                "school_id": oid,
                "plan": plan or Config.PLAN_KEY,
                "created_at": now,
            },
        },
        upsert=True,
    )


def mark_past_due(school_id, subscription_code: str = None):
    """Called when a renewal charge fails."""
    oid = _to_oid(school_id)
    if not oid:
        return
    now = datetime.utcnow()
    schools.update_one(
        {"_id": oid},
        {"$set": {"subscription_status": "past_due", "updated_at": now}},
    )
    set_ops = {"status": "past_due", "updated_at": now}
    if subscription_code:
        set_ops["paystack_subscription_code"] = subscription_code
    subscriptions.update_one({"school_id": oid}, {"$set": set_ops})


def mark_canceled(school_id, subscription_code: str = None):
    """
    Called when a subscription is canceled / not renewing.
    Preserves `next_billing_date` so access continues until period end.
    """
    oid = _to_oid(school_id)
    if not oid:
        return
    now = datetime.utcnow()
    schools.update_one(
        {"_id": oid},
        {"$set": {"subscription_status": "canceled", "updated_at": now}},
    )
    set_ops = {"status": "canceled", "canceled_at": now, "updated_at": now}
    if subscription_code:
        set_ops["paystack_subscription_code"] = subscription_code
    subscriptions.update_one({"school_id": oid}, {"$set": set_ops})