"""
Everidemy — Data access layer.

Thin helpers around MongoDB. Every school-scoped query MUST filter by
`school_id` to enforce multi-tenant isolation.
"""
from datetime import datetime, timedelta

from bson import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import (
    schools, users, students, staff, classes, subjects,
    attendance, grades, fees, invoices, subscriptions,
    announcements, audit_logs, tickets,
)
from config import Config


# =====================================================================
# LOW-LEVEL HELPERS
# =====================================================================
def _now():
    """Central UTC clock. Change here to change everywhere."""
    return datetime.utcnow()


def to_object_id(value):
    """Safe ObjectId conversion. Returns None on bad input."""
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def format_money(amount):
    """
    Format an integer/float as GH₵ 500 (or whatever Config says).
    Fallback so a bad value never crashes a template.
    """
    try:
        return f"{Config.CURRENCY_SYMBOL} {int(amount):,}"
    except Exception:
        return f"{Config.CURRENCY_SYMBOL} {amount}"


# =====================================================================
# SCHOOLS
# =====================================================================
class School:
    @staticmethod
    def create(name, owner_email, owner_name, password=None, phone=None,
               plan=None, address=None, type=None, send_welcome=False):
        """
        Create a school + its first admin user + a subscription row.

        The school starts in status "unpaid" and is LOCKED until the
        admin completes a Paystack checkout. There is no trial period.

        Returns (school_doc, user_doc) both including `_id`.
        """
        now = _now()
        plan = plan or Config.PLAN_KEY

        school_doc = {
            "name": name,
            "type": (type or "primary").lower(),
            "logo": None,
            "plan": plan,
            "subscription_status": "unpaid",   # locked until paid
            "trial_ends_at": None,             # no trial
            "paystack_customer_code": None,
            "phone": phone,
            "address": address or {},
            "created_at": now,
            "updated_at": now,
            "suspended": False,
        }
        school_id = schools.insert_one(school_doc).inserted_id

        # First admin user
        password = password or "ChangeMe123!"
        user_doc = {
            "school_id": school_id,
            "name": owner_name,
            "email": owner_email.lower().strip(),
            "password_hash": generate_password_hash(password),
            "role": "school_admin",
            "email_verified": False,
            "linked_children": [],
            "created_at": now,
            "last_login": None,
        }
        user_id = users.insert_one(user_doc).inserted_id

        # Subscription row — status starts as "unpaid"
        subscriptions.insert_one({
            "school_id": school_id,
            "plan": plan,
            "paystack_subscription_code": None,
            "status": "unpaid",
            "amount": Config.PLAN_PRICE,
            "currency": Config.CURRENCY,
            "next_billing_date": None,
            "created_at": now,
        })

        School.log(school_id, user_id, "school.created", {"name": name})

        # Welcome email (best-effort)
        if send_welcome:
            try:
                from utils.mailer import send_welcome_email
                send_welcome_email(name, owner_name, owner_email)
            except Exception as e:
                print(f"[warn] welcome email failed: {e}")

        return school_doc | {"_id": school_id}, user_doc | {"_id": user_id}

    @staticmethod
    def update(school_id, data: dict, actor_id=None):
        """Update only whitelisted school fields."""
        oid = to_object_id(school_id)
        if not oid:
            return False
        allowed = {"name", "type", "phone", "address", "logo"}
        clean = {k: v for k, v in data.items() if k in allowed}
        if not clean:
            return False
        clean["updated_at"] = _now()
        schools.update_one({"_id": oid}, {"$set": clean})
        School.log(oid, actor_id, "school.updated", clean)
        return True

    @staticmethod
    def delete(school_id, actor_id=None):
        """
        Delete a school and cascade-delete ALL of its tenant data.

        Every school-scoped collection is wiped, plus any audit log rows
        the school created. Platform-level audit entries (school_id=None)
        are preserved.
        """
        oid = to_object_id(school_id)
        if not oid:
            return False

        s = schools.find_one({"_id": oid})
        if not s:
            return False

        # Log first, then delete.
        audit_logs.insert_one({
            "school_id": None,
            "actor_id": actor_id,
            "action": "school.deleted",
            "meta": {"name": s.get("name")},
            "timestamp": _now(),
        })

        # Import here (not at module top) to avoid a circular import:
        # extensions.py imports Config, but routes import models → extensions.
        from extensions import (
            attendance as attendance_coll,
            grades as grades_coll,
            classes as classes_coll,
            subjects as subjects_coll,
            fee_structures as fee_structures_coll,
            payments as payments_coll,
            messages as messages_coll,
            timetable as timetable_coll,
            webhook_events as webhook_events_coll,
        )

        # Every collection that carries a `school_id`.
        tenant_collections = [
            users,
            students,
            staff,
            classes_coll,
            subjects_coll,
            attendance_coll,
            grades_coll,
            invoices,
            payments_coll,
            fee_structures_coll,
            messages_coll,
            announcements,
            timetable_coll,
            subscriptions,
            audit_logs,
            webhook_events_coll,
        ]

        for coll in tenant_collections:
            try:
                coll.delete_many({"school_id": oid})
            except Exception as e:
                # Don't let one bad collection block the whole delete.
                print(f"[warn] cascade delete failed on {coll.name}: {e}")

        schools.delete_one({"_id": oid})
        return True

    @staticmethod
    def find_by_id(school_id):
        oid = to_object_id(school_id)
        if not oid:
            return None
        return schools.find_one({"_id": oid})

    @staticmethod
    def find_by_name(name):
        return schools.find_one({"name": name.strip()})

    @staticmethod
    def all(search=None, plan=None, status=None, limit=100, skip=0):
        q = {}
        if search:
            q["name"] = {"$regex": search, "$options": "i"}
        if plan and plan != "all":
            q["plan"] = plan
        if status and status != "all":
            q["subscription_status"] = status
        cursor = schools.find(q).sort("created_at", -1).skip(skip).limit(limit)
        return list(cursor)

    @staticmethod
    def count(search=None, plan=None, status=None):
        q = {}
        if search:
            q["name"] = {"$regex": search, "$options": "i"}
        if plan and plan != "all":
            q["plan"] = plan
        if status and status != "all":
            q["subscription_status"] = status
        return schools.count_documents(q)

    @staticmethod
    def update_status(school_id, status, actor_id=None):
        oid = to_object_id(school_id)
        if not oid:
            return
        schools.update_one(
            {"_id": oid},
            {"$set": {"subscription_status": status, "updated_at": _now()}},
        )
        School.log(oid, actor_id, "school.status_changed", {"status": status})

    @staticmethod
    def toggle_suspend(school_id, actor_id=None):
        """Flip the `suspended` flag. Returns the new boolean state."""
        oid = to_object_id(school_id)
        s = schools.find_one({"_id": oid})
        if not s:
            return None
        new_val = not s.get("suspended", False)
        schools.update_one(
            {"_id": oid},
            {"$set": {"suspended": new_val, "updated_at": _now()}},
        )
        School.log(
            oid, actor_id,
            "school.suspended" if new_val else "school.reactivated",
            {},
        )
        return new_val

    @staticmethod
    def log(school_id, actor_id, action, meta=None):
        audit_logs.insert_one({
            "school_id": school_id,
            "actor_id": actor_id,
            "action": action,
            "meta": meta or {},
            "timestamp": _now(),
        })

    @staticmethod
    def student_count(school_id):
        oid = to_object_id(school_id)
        return students.count_documents({"school_id": oid}) if oid else 0

    @staticmethod
    def staff_count(school_id):
        oid = to_object_id(school_id)
        return staff.count_documents({"school_id": oid}) if oid else 0

    @staticmethod
    def get_owner(school_id):
        """Return the first school_admin user for a school (or None)."""
        oid = to_object_id(school_id)
        if not oid:
            return None
        return users.find_one({"school_id": oid, "role": "school_admin"})


# =====================================================================
# USERS
# =====================================================================
class User:
    @staticmethod
    def find_by_email(email):
        return users.find_one({"email": email.lower().strip()})

    @staticmethod
    def find_by_id(user_id):
        oid = to_object_id(user_id)
        if not oid:
            return None
        return users.find_one({"_id": oid})

    @staticmethod
    def verify_password(user, password):
        if not user or "password_hash" not in user:
            return False
        return check_password_hash(user["password_hash"], password)

    @staticmethod
    def update_last_login(user_id):
        oid = to_object_id(user_id)
        if not oid:
            return
        users.update_one(
            {"_id": oid},
            {"$set": {"last_login": _now()}},
        )


# =====================================================================
# SUBSCRIPTIONS
# =====================================================================
class Subscription:
    @staticmethod
    def find_for_school(school_id):
        oid = to_object_id(school_id)
        if not oid:
            return None
        return subscriptions.find_one({"school_id": oid})

    @staticmethod
    def all(limit=100, skip=0):
        return list(
            subscriptions
            .find()
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )

    @staticmethod
    def count():
        return subscriptions.count_documents({})


# =====================================================================
# ANALYTICS (platform-level, super admin)
# =====================================================================
class Analytics:
    @staticmethod
    def platform_metrics():
        total_schools   = schools.count_documents({})
        active_schools  = schools.count_documents({"subscription_status": "active"})
        unpaid          = schools.count_documents({"subscription_status": "unpaid"})
        past_due        = schools.count_documents({"subscription_status": "past_due"})
        canceled        = schools.count_documents({"subscription_status": "canceled"})
        suspended       = schools.count_documents({"suspended": True})

        # Single-plan model: MRR = active schools × plan price
        mrr = active_schools * Config.PLAN_PRICE

        total_students = students.count_documents({})
        open_tickets   = tickets.count_documents({"status": "open"})

        return {
            "total_schools":   total_schools,
            "active_schools":  active_schools,
            "unpaid":          unpaid,
            "past_due":        past_due,
            "canceled":        canceled,
            "suspended":       suspended,
            "mrr":             mrr,
            "arr":             mrr * 12,
            "total_students":  total_students,
            "open_tickets":    open_tickets,
            "plan_price":      Config.PLAN_PRICE,
            "currency_symbol": Config.CURRENCY_SYMBOL,
        }

    @staticmethod
    def recent_schools(limit=5):
        return list(schools.find().sort("created_at", -1).limit(limit))

    @staticmethod
    def top_schools(limit=5):
        return list(
            schools.find({"subscription_status": "active"})
            .sort("created_at", 1)
            .limit(limit)
        )

    @staticmethod
    def recent_activity(limit=10):
        return list(audit_logs.find().sort("timestamp", -1).limit(limit))