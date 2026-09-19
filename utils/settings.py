"""
Everidemy — School settings helpers.

Central place for reading and writing the `settings` sub-document
on a school record. Everything is scoped to the school_id.
"""
from datetime import datetime

from bson import ObjectId


# =========================================================
# DEFAULTS
# =========================================================
DEFAULT_GRADING_SCALE = [
    {"min": 80, "letter": "A1", "remark": "Excellent"},
    {"min": 75, "letter": "B2", "remark": "Very Good"},
    {"min": 70, "letter": "B3", "remark": "Good"},
    {"min": 65, "letter": "C4", "remark": "Credit"},
    {"min": 60, "letter": "C5", "remark": "Credit"},
    {"min": 55, "letter": "C6", "remark": "Credit"},
    {"min": 50, "letter": "D7", "remark": "Pass"},
    {"min": 45, "letter": "E8", "remark": "Weak Pass"},
    {"min": 0,  "letter": "F9", "remark": "Fail"},
]

DEFAULT_SETTINGS = {
    # Profile
    "motto": "",
    "founded_year": None,
    "address": {"street": "", "city": "", "state": "", "country": "Ghana"},
    "timezone": "Africa/Accra",
    "currency": "GHS",

    # Academic
    "current_term": "first",
    "current_academic_year": "2024/2025",
    "term_start_dates": {"first": None, "second": None, "third": None},
    "term_end_dates":   {"first": None, "second": None, "third": None},
    "grading_scale": DEFAULT_GRADING_SCALE,
    "ca_max": 30,
    "exam_max": 70,

    # Fees
    "invoice_prefix": "INV",
    "invoice_auto_number": True,
    "next_invoice_number": 1,
    "fee_due_days": 30,

    # Notifications
    "notify_email_attendance": True,
    "notify_email_fees": True,
    "notify_email_grades": True,
    "notify_email_announcements": True,
    "notify_sms_attendance": False,
    "notify_sms_fees": False,
    "sms_sender_id": "",

    # Team permissions
    "team_permissions": {
        "school_admin": ["full"],
        "teacher": ["attendance", "grades", "assignments", "messages"],
        "accountant": ["fees", "reports", "messages"],
    },

    # Integrations
    "paystack_public_key": "",
    "paystack_secret_key": "",
    "sms_api_key": "",
}


# =========================================================
# LOW-LEVEL HELPERS
# =========================================================
def _to_oid(v):
    """Safe ObjectId conversion. Returns None on bad input."""
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, without mutating either."""
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# =========================================================
# READ
# =========================================================
def get_settings(school: dict) -> dict:
    """
    Return the school's settings dict, merged with defaults.
    Any field the school hasn't overridden yet falls back to DEFAULT.
    """
    saved = school.get("settings") or {}
    return _deep_merge(DEFAULT_SETTINGS, saved)


def get_one(school: dict, key: str, default=None):
    """Get a single setting value."""
    return get_settings(school).get(key, default)


# =========================================================
# WRITE
# =========================================================
def update_section(schools_coll, school_id, section: str, values: dict, actor_id=None):
    """
    Update one named section of the settings sub-document.

    Example:
        update_section(schools, sid, "profile", {"motto": "..."})
        → $set: {"settings.profile.motto": "..."}

    `section` can be "" for top-level keys, or a nested name like "profile".
    Returns True on success, False otherwise.
    """
    oid = _to_oid(school_id)
    if not oid:
        return False

    set_ops = {}
    for key, val in values.items():
        path = f"settings.{section}.{key}" if section else f"settings.{key}"
        set_ops[path] = val

    set_ops["settings.updated_at"] = datetime.utcnow()
    if actor_id:
        set_ops["settings.updated_by"] = _to_oid(actor_id)

    schools_coll.update_one({"_id": oid}, {"$set": set_ops})
    return True


def reset_section(schools_coll, school_id, section: str, actor_id=None):
    """Reset a settings section to its defaults."""
    oid = _to_oid(school_id)
    if not oid:
        return False

    if section in DEFAULT_SETTINGS and isinstance(DEFAULT_SETTINGS[section], dict):
        defaults = DEFAULT_SETTINGS[section]
    else:
        defaults = DEFAULT_SETTINGS.get(section)
        if defaults is None:
            return False

    update_ops = {
        f"settings.{section}": defaults,
        "settings.updated_at": datetime.utcnow(),
    }
    if actor_id:
        update_ops["settings.updated_by"] = _to_oid(actor_id)

    schools_coll.update_one({"_id": oid}, {"$set": update_ops})
    return True


# =========================================================
# VALIDATION
# =========================================================
def validate_grading_scale(scale) -> tuple:
    """
    Validate a submitted grading scale.
    Returns (clean_scale, errors).
    """
    errors = []
    clean = []

    if not isinstance(scale, list):
        return DEFAULT_GRADING_SCALE, ["Grading scale must be a list."]

    for i, entry in enumerate(scale):
        try:
            minimum = int(entry.get("min"))
            letter = (entry.get("letter") or "").strip().upper()
            remark = (entry.get("remark") or "").strip()

            if not (0 <= minimum <= 100):
                errors.append(f"Row {i+1}: minimum must be 0–100.")
            if not letter:
                errors.append(f"Row {i+1}: letter grade is required.")
            if not remark:
                errors.append(f"Row {i+1}: remark is required.")

            clean.append({"min": minimum, "letter": letter, "remark": remark})
        except (ValueError, TypeError):
            errors.append(f"Row {i+1}: invalid values.")

    # Sort descending by min so the grade lookup works
    clean.sort(key=lambda x: x["min"], reverse=True)

    # No duplicate letters
    letters = [c["letter"] for c in clean]
    if len(letters) != len(set(letters)):
        errors.append("Duplicate letter grades are not allowed.")

    # Must include a band starting at 0
    if clean and clean[-1]["min"] != 0:
        errors.append("Grading scale must include a band starting at 0.")

    return (clean if not errors else DEFAULT_GRADING_SCALE), errors