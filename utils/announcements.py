"""
Announcement visibility helpers — shared by every portal.
"""
from datetime import datetime


# Which `audience` values each role can see
AUDIENCE_MAP = {
    "school_admin": ["all", "staff", "school_admins"],
    "teacher":      ["all", "staff"],
    "accountant":   ["all", "staff"],
    "student":      ["all", "students"],
    "parent":       ["all", "parents"],
    "super_admin":  ["all", "students", "parents", "staff", "school_admins"],
}


def audiences_for(user: dict):
    role = (user or {}).get("role", "")
    return AUDIENCE_MAP.get(role, ["all"])


def build_visibility_filter(user: dict, school_id):
    """
    Mongo filter that returns only the announcements this user should see.
    Includes:
      - school-specific + platform-wide (school_id=None)
      - audience matching their role
      - not expired
    """
    audiences = audiences_for(user)
    now = datetime.utcnow()

    return {
        "$and": [
            {"$or": [
                {"school_id": school_id},
                {"school_id": None},
            ]},
            {"$or": [
                {"expires_at": None},
                {"expires_at": {"$gte": now}},
            ]},
            {"audience": {"$in": audiences}},
        ]
    }


def visible_for(user: dict, school_id, announcements_coll, limit: int = 200):
    """Return the list of announcements this user should see, newest first."""
    q = build_visibility_filter(user, school_id)
    return list(
        announcements_coll.find(q)
        .sort([("published_at", -1), ("created_at", -1)])
        .limit(limit)
    )


def unread_count(user: dict, school_id, announcements_coll) -> int:
    """Count of announcements the user hasn't read yet."""
    q = build_visibility_filter(user, school_id)
    q["read_by"] = {"$ne": user["_id"]}
    return announcements_coll.count_documents(q)


def mark_all_read(user: dict, school_id, announcements_coll, ids=None):
    """Mark visible announcements as read for this user."""
    q = build_visibility_filter(user, school_id)
    if ids:
        q = {"$and": [q, {"_id": {"$in": ids}}]}
    announcements_coll.update_many(
        q,
        {"$addToSet": {"read_by": user["_id"]}},
    )