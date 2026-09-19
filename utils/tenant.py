from bson import ObjectId
from flask import g


def scoped_filter(base_filter=None):
    """
    Merge the current user's school_id into a Mongo query filter.
    Always use this in school-level queries.
    """
    school = getattr(g, "school", None)
    if not school:
        raise RuntimeError("No school context — user may not be logged in.")
    f = dict(base_filter or {})
    f["school_id"] = school["_id"]
    return f