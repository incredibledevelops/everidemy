"""
Everidemy — Reporting & analytics helpers.

Every function here is a pure, tenant-scoped aggregation.
They take the current school's `school_id` and return plain dicts
ready for templates.
"""
import csv
import io
from datetime import datetime, timedelta
from calendar import monthrange

from bson import ObjectId


# =========================================================
# UTILITIES
# =========================================================
def month_range(start: datetime, end: datetime):
    """
    Yield (year, month) tuples covering start→end inclusively.
    Used for enrollment trend charts.
    """
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield (y, m)
        m += 1
        if m > 12:
            m = 1
            y += 1


def month_label(y, m):
    return datetime(y, m, 1).strftime("%b %Y")


def rows_to_csv(headers, rows) -> str:
    """Return a CSV string for the given headers + row dicts."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# =========================================================
# ENROLLMENT
# =========================================================
def enrollment_summary(school_id, students_coll, classes_coll):
    """Snapshot of total / active / by gender / by class."""
    tf = {"school_id": school_id}

    total = students_coll.count_documents(tf)
    active = students_coll.count_documents({**tf, "status": "active"})
    archived = students_coll.count_documents({**tf, "status": "archived"})
    male = students_coll.count_documents({**tf, "gender": "male", "status": "active"})
    female = students_coll.count_documents({**tf, "gender": "female", "status": "active"})

    # By class
    pipeline = [
        {"$match": {**tf, "status": "active"}},
        {"$group": {"_id": "$class_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    by_class_raw = list(students_coll.aggregate(pipeline))
    class_ids = [r["_id"] for r in by_class_raw if r["_id"]]
    class_map = {}
    if class_ids:
        for c in classes_coll.find({"_id": {"$in": class_ids}, "school_id": school_id}):
            class_map[c["_id"]] = c
    by_class = []
    for r in by_class_raw:
        c = class_map.get(r["_id"])
        by_class.append({
            "class_id": str(r["_id"]) if r["_id"] else None,
            "class_name": c["name"] if c else "Unassigned",
            "count": r["count"],
        })

    return {
        "total": total,
        "active": active,
        "archived": archived,
        "male": male,
        "female": female,
        "by_class": by_class,
    }


def enrollment_trend(school_id, students_coll, months=12):
    """Monthly new admissions for the last N months."""
    end = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=365)

    pipeline = [
        {"$match": {
            "school_id": school_id,
            "created_at": {"$gte": start},
        }},
        {"$group": {
            "_id": {"y": {"$year": "$created_at"}, "m": {"$month": "$created_at"}},
            "count": {"$sum": 1},
        }},
    ]
    raw = {f"{r['_id']['y']}-{r['_id']['m']:02d}": r["count"]
           for r in students_coll.aggregate(pipeline)}

    out = []
    for (y, m) in month_range(start, end):
        key = f"{y}-{m:02d}"
        out.append({
            "year": y,
            "month": m,
            "label": month_label(y, m),
            "count": raw.get(key, 0),
        })
    return out


# =========================================================
# ATTENDANCE
# =========================================================
def attendance_summary(school_id, attendance_coll, start: datetime, end: datetime):
    """Totals + percentages for a date range."""
    tf = {"school_id": school_id, "date": {"$gte": start, "$lte": end}}

    docs = list(attendance_coll.find(tf))
    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    total = 0
    for d in docs:
        for rec in d.get("records", []):
            status = rec.get("status", "present")
            counts[status] = counts.get(status, 0) + 1
            total += 1

    pct = round(((counts["present"] + counts["late"]) / total) * 100) if total else 0

    return {
        "sessions": len(docs),
        "total": total,
        "counts": counts,
        "pct": pct,
    }


def attendance_by_class(school_id, attendance_coll, classes_coll, start, end):
    """Per-class attendance % for the date range."""
    tf = {"school_id": school_id, "date": {"$gte": start, "$lte": end}}
    docs = list(attendance_coll.find(tf))

    class_ids = {d["class_id"] for d in docs}
    class_map = {}
    if class_ids:
        for c in classes_coll.find({"_id": {"$in": list(class_ids)}, "school_id": school_id}):
            class_map[c["_id"]] = c

    by_class = {}
    for d in docs:
        cid = d["class_id"]
        bucket = by_class.setdefault(cid, {"present": 0, "absent": 0, "late": 0, "excused": 0, "total": 0})
        for rec in d.get("records", []):
            status = rec.get("status", "present")
            bucket[status] += 1
            bucket["total"] += 1

    out = []
    for cid, b in by_class.items():
        pct = round(((b["present"] + b["late"]) / b["total"]) * 100) if b["total"] else 0
        out.append({
            "class_id": str(cid),
            "class_name": class_map[cid]["name"] if cid in class_map else "Unknown",
            "present": b["present"],
            "absent": b["absent"],
            "late": b["late"],
            "excused": b["excused"],
            "total": b["total"],
            "pct": pct,
        })
    out.sort(key=lambda r: r["class_name"])
    return out


def attendance_trend(school_id, attendance_coll, days=30):
    """Daily attendance % for the last N days."""
    end = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days - 1)

    tf = {"school_id": school_id, "date": {"$gte": start, "$lte": end}}
    docs = list(attendance_coll.find(tf))

    by_day = {}
    for d in docs:
        key = d["date"].strftime("%Y-%m-%d")
        bucket = by_day.setdefault(key, {"present": 0, "late": 0, "total": 0})
        for rec in d.get("records", []):
            status = rec.get("status", "present")
            if status in ("present", "late"):
                bucket["present"] += 1
            bucket["total"] += 1

    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        b = by_day.get(key, {"present": 0, "total": 0})
        pct = round((b["present"] / b["total"]) * 100) if b["total"] else 0
        out.append({
            "date": d,
            "label": d.strftime("%b %d"),
            "pct": pct,
            "marked": b["total"] > 0,
        })
    return out


# =========================================================
# ACADEMICS
# =========================================================
def academics_summary(school_id, grades_coll, subjects_coll, term, academic_year):
    """Overall grade stats for a term."""
    tf = {"school_id": school_id, "term": term, "academic_year": academic_year}

    total_grades = grades_coll.count_documents(tf)

    avg_pipeline = [
        {"$match": tf},
        {"$group": {"_id": None, "avg": {"$avg": "$total"}}},
    ]
    avg_res = list(grades_coll.aggregate(avg_pipeline))
    average = round(avg_res[0]["avg"]) if avg_res and avg_res[0].get("avg") else 0

    # Grade distribution
    dist_pipeline = [
        {"$match": tf},
        {"$group": {"_id": "$grade", "count": {"$sum": 1}}},
    ]
    dist = {r["_id"]: r["count"] for r in grades_coll.aggregate(dist_pipeline)}

    # Subject averages
    subj_pipeline = [
        {"$match": tf},
        {"$group": {
            "_id": "$subject_id",
            "avg": {"$avg": "$total"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"avg": -1}},
    ]
    subj_raw = list(grades_coll.aggregate(subj_pipeline))
    subj_ids = [r["_id"] for r in subj_raw]
    subj_map = {}
    if subj_ids:
        for s in subjects_coll.find({"_id": {"$in": subj_ids}, "school_id": school_id}):
            subj_map[s["_id"]] = s

    by_subject = []
    for r in subj_raw:
        s = subj_map.get(r["_id"])
        by_subject.append({
            "subject_id": str(r["_id"]),
            "subject_name": s["name"] if s else "Unknown",
            "average": round(r["avg"]) if r.get("avg") else 0,
            "count": r["count"],
        })

    return {
        "total_grades": total_grades,
        "average": average,
        "distribution": dist,
        "by_subject": by_subject,
    }


def class_rankings(school_id, grades_coll, classes_coll, students_coll, term, academic_year):
    """Rank classes by average grade for a term."""
    tf = {"school_id": school_id, "term": term, "academic_year": academic_year}

    pipeline = [
        {"$match": tf},
        {"$group": {
            "_id": "$class_id",
            "avg": {"$avg": "$total"},
            "count": {"$sum": 1},
        }},
    ]
    raw = list(grades_coll.aggregate(pipeline))

    class_ids = [r["_id"] for r in raw if r["_id"]]
    class_map = {}
    if class_ids:
        for c in classes_coll.find({"_id": {"$in": class_ids}, "school_id": school_id}):
            class_map[c["_id"]] = c

    out = []
    for r in raw:
        c = class_map.get(r["_id"])
        student_count = students_coll.count_documents({
            "school_id": school_id,
            "class_id": r["_id"],
            "status": "active",
        }) if r["_id"] else 0
        out.append({
            "class_id": str(r["_id"]) if r["_id"] else None,
            "class_name": c["name"] if c else "Unassigned",
            "average": round(r["avg"]) if r.get("avg") else 0,
            "grades_entered": r["count"],
            "student_count": student_count,
        })
    out.sort(key=lambda x: x["average"], reverse=True)
    return out


def top_students(school_id, grades_coll, students_coll, classes_coll, term, academic_year, limit=10):
    """Top performers for a term."""
    tf = {"school_id": school_id, "term": term, "academic_year": academic_year}

    pipeline = [
        {"$match": tf},
        {"$group": {"_id": "$student_id", "avg": {"$avg": "$total"}, "subjects": {"$sum": 1}}},
        {"$sort": {"avg": -1}},
        {"$limit": limit},
    ]
    raw = list(grades_coll.aggregate(pipeline))

    student_ids = [r["_id"] for r in raw]
    student_map = {}
    if student_ids:
        for s in students_coll.find({"_id": {"$in": student_ids}, "school_id": school_id}):
            student_map[s["_id"]] = s

    # Class lookup
    class_ids = {s["class_id"] for s in student_map.values() if s.get("class_id")}
    class_map = {}
    if class_ids:
        for c in classes_coll.find({"_id": {"$in": list(class_ids)}, "school_id": school_id}):
            class_map[c["_id"]] = c

    out = []
    for r in raw:
        s = student_map.get(r["_id"])
        if not s:
            continue
        out.append({
            "student_id": str(r["_id"]),
            "name": f"{s.get('first_name','')} {s.get('last_name','')}".strip(),
            "admission_no": s.get("admission_no", ""),
            "class_name": class_map.get(s.get("class_id"), {}).get("name", "Unassigned"),
            "average": round(r["avg"]) if r.get("avg") else 0,
            "subjects": r["subjects"],
        })
    return out


# =========================================================
# FINANCE
# =========================================================
def finance_summary(school_id, invoices_coll, payments_coll):
    """Snapshot of billing state."""
    tf = {"school_id": school_id}

    invoice_pipeline = [
        {"$match": tf},
        {"$group": {
            "_id": None,
            "count": {"$sum": 1},
            "due": {"$sum": "$amount_due"},
            "paid": {"$sum": "$amount_paid"},
            "balance": {"$sum": "$balance"},
        }},
    ]
    inv_res = list(invoices_coll.aggregate(invoice_pipeline))
    inv = inv_res[0] if inv_res else {"count": 0, "due": 0, "paid": 0, "balance": 0}

    # Payment channel breakdown
    channel_pipeline = [
        {"$match": tf},
        {"$group": {"_id": "$channel", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
        {"$sort": {"total": -1}},
    ]
    channels = list(payments_coll.aggregate(channel_pipeline))

    # Invoice status distribution
    status_pipeline = [
        {"$match": tf},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    statuses = {r["_id"]: r["count"] for r in invoices_coll.aggregate(status_pipeline)}

    collection_pct = round((inv["paid"] / inv["due"]) * 100) if inv.get("due") else 0

    return {
        "invoice_count": inv["count"],
        "total_due": inv["due"] or 0,
        "total_paid": inv["paid"] or 0,
        "total_balance": inv["balance"] or 0,
        "collection_pct": collection_pct,
        "by_channel": [
            {"channel": c["_id"] or "unknown", "total": c["total"], "count": c["count"]}
            for c in channels
        ],
        "statuses": statuses,
    }


def monthly_revenue(school_id, payments_coll, months=12):
    """Monthly collections for the last N months."""
    end = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=365)

    pipeline = [
        {"$match": {
            "school_id": school_id,
            "paid_at": {"$gte": start},
        }},
        {"$group": {
            "_id": {"y": {"$year": "$paid_at"}, "m": {"$month": "$paid_at"}},
            "total": {"$sum": "$amount"},
            "count": {"$sum": 1},
        }},
    ]
    raw = {f"{r['_id']['y']}-{r['_id']['m']:02d}": r for r in payments_coll.aggregate(pipeline)}

    out = []
    for (y, m) in month_range(start, end):
        key = f"{y}-{m:02d}"
        r = raw.get(key, {"total": 0, "count": 0})
        out.append({
            "year": y,
            "month": m,
            "label": month_label(y, m),
            "total": r["total"],
            "count": r["count"],
        })
    return out


def outstanding_by_class(school_id, invoices_coll, classes_coll):
    """Sum of outstanding balance per class."""
    pipeline = [
        {"$match": {"school_id": school_id, "balance": {"$gt": 0}}},
        {"$group": {"_id": "$class_id", "balance": {"$sum": "$balance"}, "count": {"$sum": 1}}},
        {"$sort": {"balance": -1}},
    ]
    raw = list(invoices_coll.aggregate(pipeline))

    class_ids = [r["_id"] for r in raw if r["_id"]]
    class_map = {}
    if class_ids:
        for c in classes_coll.find({"_id": {"$in": class_ids}, "school_id": school_id}):
            class_map[c["_id"]] = c

    out = []
    for r in raw:
        c = class_map.get(r["_id"])
        out.append({
            "class_id": str(r["_id"]) if r["_id"] else None,
            "class_name": c["name"] if c else "Unassigned",
            "balance": r["balance"],
            "count": r["count"],
        })
    return out


# =========================================================
# STAFF
# =========================================================
def staff_summary(school_id, staff_coll, users_coll):
    """Staff snapshot."""
    tf = {"school_id": school_id}

    total = staff_coll.count_documents(tf)
    active = staff_coll.count_documents({**tf, "status": "active"})
    on_leave = staff_coll.count_documents({**tf, "status": "on_leave"})
    resigned = staff_coll.count_documents({**tf, "status": "resigned"})

    # Role distribution
    role_pipeline = [
        {"$match": tf},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "user",
        }},
        {"$unwind": {"path": "$user", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$user.role", "count": {"$sum": 1}}},
    ]
    roles = list(staff_coll.aggregate(role_pipeline))

    # Salary total (active only)
    sal_pipeline = [
        {"$match": {**tf, "status": "active"}},
        {"$group": {"_id": None, "total": {"$sum": "$salary"}}},
    ]
    sal_res = list(staff_coll.aggregate(sal_pipeline))
    total_salary = sal_res[0]["total"] if sal_res and sal_res[0].get("total") else 0

    return {
        "total": total,
        "active": active,
        "on_leave": on_leave,
        "resigned": resigned,
        "by_role": [
            {"role": r["_id"] or "unknown", "count": r["count"]}
            for r in roles
        ],
        "total_salary": total_salary,
    }


# =========================================================
# RANGE HELPERS
# =========================================================
def parse_range(args):
    """
    Return (start, end) datetimes from query args `from` and `to`.
    Defaults to last 30 days.
    """
    to_d = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    from_d = to_d - timedelta(days=30)

    f = (args.get("from") or "").strip()
    t = (args.get("to") or "").strip()

    if f:
        try:
            from_d = datetime.strptime(f, "%Y-%m-%d")
        except ValueError:
            pass
    if t:
        try:
            to_d = datetime.strptime(t, "%Y-%m-%d")
            # include the whole day
            to_d = to_d.replace(hour=23, minute=59, second=59)
        except ValueError:
            pass

    return from_d, to_d