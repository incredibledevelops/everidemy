"""
Student blueprint — the student's personal workspace.

A student is a `user` with role="student" linked to a `students` doc
via `students.user_id`. If a student has no user account yet, they
see a "contact your school admin" message.
"""
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, abort,
)
from bson import ObjectId

from config import Config
from extensions import (
    users, students, classes, subjects,
    attendance, grades, invoices, payments,
    announcements, messages,
)
from models import format_money
from utils.auth import role_required


student_bp = Blueprint(
    "student", __name__,
    url_prefix="/student",
)


# =========================================================
# LOW-LEVEL HELPERS
# =========================================================
def _to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _tenant(extra=None):
    """Merge `school_id` into any Mongo query filter."""
    f = dict(extra or {})
    f["school_id"] = g.school["_id"]
    return f


def _current_term():
    from routes.school_admin import _current_term as _t
    return _t()


def _current_academic_year():
    from routes.school_admin import _current_academic_year as _a
    return _a()


# =========================================================
# STUDENT HELPERS
# =========================================================
def _current_student():
    """Return the students doc for the current user (or None)."""
    return students.find_one({
        "school_id": g.school["_id"],
        "user_id": g.user["_id"],
    })


def _student_class(student):
    """Return the class doc for a student, or None."""
    if not student or not student.get("class_id"):
        return None
    return classes.find_one(_tenant({"_id": student["class_id"]}))


def _message_recipients():
    """Students can message staff and other students in their school."""
    return list(
        users.find({
            "school_id": g.school["_id"],
            "_id": {"$ne": g.user["_id"]},
            "role": {"$in": ["teacher", "school_admin", "accountant"]},
        }).sort("name", 1)
    )


# =========================================================
# CONTEXT PROCESSOR
# =========================================================
@student_bp.context_processor
def inject_student_context():
    """
    Values available in every student-portal template.
    Also computes `unread_messages` so the notification bell
    works on every page without each route passing it.
    """
    unread = 0
    try:
        unread = messages.count_documents({
            "school_id": g.school["_id"],
            "recipient_ids": g.user["_id"],
            "read_at": None,
        })
    except Exception:
        pass

    return {
        "format_money": format_money,
        "currency_symbol": Config.CURRENCY_SYMBOL,
        "currency_code": Config.CURRENCY,
        "unread_messages": unread,
    }


# =========================================================
# GATE
# =========================================================
@student_bp.before_request
@role_required("student", "school_admin")
def _gate():
    return None


# =========================================================
# DASHBOARD
# =========================================================
@student_bp.route("/")
@student_bp.route("/dashboard")
def dashboard():
    student = _current_student()
    if not student:
        return render_template("student/no_account.html")

    klass = _student_class(student)

    # ---- Attendance summary ----
    att_docs = list(attendance.find(_tenant({})).sort("date", -1).limit(60))
    att_counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    for doc in att_docs:
        for rec in doc.get("records", []):
            if rec.get("student_id") == student["_id"]:
                st = rec.get("status", "present")
                att_counts[st] = att_counts.get(st, 0) + 1
    att_total = sum(att_counts.values())
    att_pct = round(((att_counts["present"] + att_counts["late"]) / att_total) * 100) if att_total else 0

    # ---- Grades for the current term ----
    term = _current_term()
    ay = _current_academic_year()
    grade_rows = list(grades.find(_tenant({
        "student_id": student["_id"], "term": term, "academic_year": ay,
    })))
    subj_ids = [r["subject_id"] for r in grade_rows]
    subj_map = {s["_id"]: s for s in subjects.find(_tenant({"_id": {"$in": subj_ids}}))}

    enriched = []
    total_sum = 0
    for r in grade_rows:
        sj = subj_map.get(r["subject_id"])
        if not sj:
            continue
        enriched.append({
            "subject": sj["name"],
            "ca":      r.get("ca_score", 0),
            "exam":    r.get("exam_score", 0),
            "total":   r.get("total", 0),
            "grade":   r.get("grade", "—"),
        })
        total_sum += r.get("total", 0)
    average = round(total_sum / len(enriched)) if enriched else 0

    # ---- Fee balance ----
    invs = list(invoices.find(_tenant({"student_id": student["_id"]})))
    total_due = sum(i.get("amount_due", 0) for i in invs)
    total_paid = sum(i.get("amount_paid", 0) for i in invs)
    balance = max(total_due - total_paid, 0)

    # ---- Today's timetable slots ----
    today_name = datetime.utcnow().strftime("%A").lower()
    today_slots = []
    if klass:
        from extensions import timetable as timetable_col
        today_slots = list(
            timetable_col.find(_tenant({"class_id": klass["_id"], "day": today_name}))
            .sort("start", 1)
        )
        slot_subj_ids = {s["subject_id"] for s in today_slots if s.get("subject_id")}
        slot_subj_map = {sj["_id"]: sj for sj in subjects.find(_tenant({"_id": {"$in": list(slot_subj_ids)}}))}
        for s in today_slots:
            s["_subject"] = slot_subj_map.get(s["subject_id"])

    # ---- Recent notices ----
    recent_notices = list(
        announcements.find({
            "$or": [
                {"school_id": g.school["_id"]},
                {"school_id": None},
            ],
        }).sort("created_at", -1).limit(4)
    )

    return render_template(
        "student/dashboard.html",
        student=student,
        klass=klass,
        att_pct=att_pct,
        att_counts=att_counts,
        average=average,
        grade_rows=enriched,
        balance=balance,
        total_due=total_due,
        total_paid=total_paid,
        today_slots=today_slots,
        recent_notices=recent_notices,
        term=term,
        academic_year=ay,
    )


# =========================================================
# TIMETABLE
# =========================================================
@student_bp.route("/timetable")
def timetable():
    student = _current_student()
    if not student:
        return render_template("student/no_account.html")

    klass = _student_class(student)
    if not klass:
        return render_template(
            "student/timetable.html",
            klass=None,
            grid={},
            weekdays=[],
            selected_day=None,
        )

    from extensions import timetable as timetable_col

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    grid = {d: [] for d in weekdays}

    slots = list(
        timetable_col.find(_tenant({"class_id": klass["_id"]}))
        .sort([("day", 1), ("start", 1)])
    )
    subj_ids = {s["subject_id"] for s in slots if s.get("subject_id")}
    subj_map = {sj["_id"]: sj for sj in subjects.find(_tenant({"_id": {"$in": list(subj_ids)}}))}
    for s in slots:
        s["_subject"] = subj_map.get(s["subject_id"])
        grid[s["day"]].append(s)

    today_name = datetime.utcnow().strftime("%A").lower()
    selected_day = request.args.get("day", today_name)
    if selected_day not in weekdays:
        selected_day = today_name

    return render_template(
        "student/timetable.html",
        klass=klass,
        grid=grid,
        weekdays=weekdays,
        selected_day=selected_day,
    )


# =========================================================
# GRADES
# =========================================================
@student_bp.route("/grades")
def grades_page():
    student = _current_student()
    if not student:
        return render_template("student/no_account.html")

    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    rows = list(grades.find(_tenant({
        "student_id": student["_id"], "term": term, "academic_year": ay,
    })))
    subj_ids = [r["subject_id"] for r in rows]
    subj_map = {s["_id"]: s for s in subjects.find(_tenant({"_id": {"$in": subj_ids}}))}

    enriched = []
    total_sum = 0
    for r in rows:
        sj = subj_map.get(r["subject_id"])
        if not sj:
            continue
        enriched.append({
            "subject": sj["name"],
            "code":    sj.get("code", ""),
            "ca":      r.get("ca_score", 0),
            "exam":    r.get("exam_score", 0),
            "total":   r.get("total", 0),
            "grade":   r.get("grade", "—"),
            "remark":  r.get("remark", ""),
        })
        total_sum += r.get("total", 0)

    enriched.sort(key=lambda x: x["subject"])
    average = round(total_sum / len(enriched)) if enriched else 0

    # Rank within class
    klass = _student_class(student)
    rank = None
    size = None
    if klass:
        pipeline = [
            {"$match": _tenant({
                "class_id": klass["_id"], "term": term, "academic_year": ay,
            })},
            {"$group": {"_id": "$student_id", "avg": {"$avg": "$total"}}},
            {"$sort": {"avg": -1}},
        ]
        ranked = list(grades.aggregate(pipeline))
        size = len(ranked)
        for i, r in enumerate(ranked):
            if r["_id"] == student["_id"]:
                rank = i + 1
                break

    return render_template(
        "student/grades.html",
        student=student,
        klass=klass,
        rows=enriched,
        average=average,
        rank=rank,
        class_size=size,
        term=term,
        academic_year=ay,
    )


# =========================================================
# ATTENDANCE
# =========================================================
@student_bp.route("/attendance")
def attendance_page():
    student = _current_student()
    if not student:
        return render_template("student/no_account.html")

    docs = list(attendance.find(_tenant({})).sort("date", -1).limit(90))

    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    recent = []
    for doc in docs:
        for rec in doc.get("records", []):
            if rec.get("student_id") == student["_id"]:
                st = rec.get("status", "present")
                counts[st] = counts.get(st, 0) + 1
                if len(recent) < 60:
                    recent.append({
                        "date": doc["date"],
                        "status": st,
                        "note": rec.get("note"),
                    })

    total = sum(counts.values())
    pct = round(((counts["present"] + counts["late"]) / total) * 100) if total else 0

    return render_template(
        "student/attendance.html",
        student=student,
        counts=counts,
        recent=recent,
        pct=pct,
    )


# =========================================================
# FEES
# =========================================================
@student_bp.route("/fees")
def fees_page():
    student = _current_student()
    if not student:
        return render_template("student/no_account.html")

    invs = list(invoices.find(_tenant({"student_id": student["_id"]})).sort("created_at", -1))

    inv_ids = [i["_id"] for i in invs]
    payment_map = {}
    if inv_ids:
        for p in payments.find(_tenant({"invoice_id": {"$in": inv_ids}})).sort("paid_at", -1):
            payment_map.setdefault(p["invoice_id"], []).append(p)

    for i in invs:
        i["_payments"] = payment_map.get(i["_id"], [])

    total_due  = sum(i.get("amount_due", 0) for i in invs)
    total_paid = sum(i.get("amount_paid", 0) for i in invs)
    balance    = max(total_due - total_paid, 0)

    return render_template(
        "student/fees.html",
        student=student,
        invoices=invs,
        total_due=total_due,
        total_paid=total_paid,
        balance=balance,
    )


# =========================================================
# MESSAGES
# =========================================================
def _threads_for(user_id):
    """Return a list of thread summaries involving the given user."""
    msgs = list(messages.find({
        "school_id": g.school["_id"],
        "$or": [{"sender_id": user_id}, {"recipient_ids": user_id}],
    }).sort("created_at", -1))

    by_thread = {}
    for m in msgs:
        tid = m.get("thread_id")
        if tid is None:
            continue
        by_thread.setdefault(tid, []).append(m)

    out = []
    for tid, arr in by_thread.items():
        last = arr[0]
        unread = sum(
            1 for m in arr
            if m.get("read_at") is None and user_id in m.get("recipient_ids", [])
        )
        partner_ids = set()
        for m in arr:
            for pid in m.get("recipient_ids", []):
                if pid != user_id:
                    partner_ids.add(pid)
            if m["sender_id"] != user_id:
                partner_ids.add(m["sender_id"])
        partners = list(users.find({"_id": {"$in": list(partner_ids)}}))
        out.append({
            "thread_id":    tid,
            "subject":      last.get("subject") or "(no subject)",
            "last_message": last,
            "partners":     partners,
            "unread":       unread,
            "updated_at":   last.get("created_at"),
        })
    out.sort(key=lambda s: s["updated_at"] or datetime.min, reverse=True)
    return out


@student_bp.route("/messages")
def messages_page():
    threads = _threads_for(g.user["_id"])
    tab = request.args.get("tab", "inbox")
    if tab == "unread":
        threads = [t for t in threads if t["unread"] > 0]

    search = request.args.get("q", "").strip()
    if search:
        s = search.lower()
        threads = [
            t for t in threads
            if s in (t["subject"] or "").lower()
            or any(s in (p.get("name") or "").lower() for p in t["partners"])
        ]

    return render_template(
        "student/messages.html",
        threads=threads,
        tab=tab,
        search=search,
        total_unread=sum(t["unread"] for t in threads),
        recipients=_message_recipients()[:8],
    )


@student_bp.route("/messages/thread/<thread_id>")
def message_thread(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    msgs = list(messages.find({
        "school_id": g.school["_id"],
        "thread_id": tid,
        "$or": [{"sender_id": g.user["_id"]}, {"recipient_ids": g.user["_id"]}],
    }).sort("created_at", 1))

    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("student.messages_page"))

    now = datetime.utcnow()
    messages.update_many(
        {"school_id": g.school["_id"], "thread_id": tid,
         "recipient_ids": g.user["_id"], "read_at": None},
        {"$set": {"read_at": now}},
    )

    sender_ids = {m["sender_id"] for m in msgs}
    sender_map = {u["_id"]: u for u in users.find({"_id": {"$in": list(sender_ids)}})}
    for m in msgs:
        m["_sender"] = sender_map.get(m["sender_id"])
        m["_is_mine"] = (m["sender_id"] == g.user["_id"])

    participant_ids = set()
    for m in msgs:
        participant_ids.add(m["sender_id"])
        for pid in m.get("recipient_ids", []):
            participant_ids.add(pid)
    participant_ids.discard(g.user["_id"])

    participants = list(users.find({"_id": {"$in": list(participant_ids)}}))

    return render_template(
        "student/message_thread.html",
        thread_id=str(tid),
        messages=msgs,
        participants=participants,
        subject=msgs[0].get("subject") or "(no subject)",
    )


@student_bp.route("/messages/thread/<thread_id>/reply", methods=["POST"])
def message_reply(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message body is required.", "error")
        return redirect(url_for("student.message_thread", thread_id=thread_id))

    msgs = list(messages.find({"school_id": g.school["_id"], "thread_id": tid}).limit(500))
    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("student.messages_page"))

    participant_ids = set()
    for m in msgs:
        participant_ids.add(m["sender_id"])
        for pid in m.get("recipient_ids", []):
            participant_ids.add(pid)
    participant_ids.discard(g.user["_id"])

    messages.insert_one({
        "school_id":     g.school["_id"],
        "thread_id":     tid,
        "sender_id":     g.user["_id"],
        "recipient_ids": list(participant_ids),
        "subject":       msgs[0].get("subject"),
        "body":          body,
        "attachments":   [],
        "read_at":       None,
        "reply_to":      msgs[-1]["_id"],
        "created_at":    datetime.utcnow(),
    })
    return redirect(url_for("student.message_thread", thread_id=thread_id))


@student_bp.route("/messages/new", methods=["GET", "POST"])
def message_new():
    """Compose a brand-new message (starts a new thread)."""
    if request.method == "POST":
        recipient_ids_raw = request.form.getlist("recipient_ids") or []
        subject = (request.form.get("subject") or "").strip()
        body    = (request.form.get("body") or "").strip()

        recipient_ids = [_to_oid(rid) for rid in recipient_ids_raw if _to_oid(rid)]
        recipient_ids = [r for r in recipient_ids if r]

        if not recipient_ids:
            flash("Select at least one recipient.", "error")
            return redirect(url_for("student.message_new"))
        if not body:
            flash("Message body is required.", "error")
            return redirect(url_for("student.message_new"))

        thread_id = ObjectId()
        messages.insert_one({
            "school_id":     g.school["_id"],
            "thread_id":     thread_id,
            "sender_id":     g.user["_id"],
            "recipient_ids": recipient_ids,
            "subject":       subject or None,
            "body":          body,
            "attachments":   [],
            "read_at":       None,
            "reply_to":      None,
            "created_at":    datetime.utcnow(),
        })
        flash("Message sent.", "success")
        return redirect(url_for("student.message_thread", thread_id=str(thread_id)))

    preselect = request.args.get("to", "").strip()
    return render_template(
        "student/message_compose.html",
        form={"recipient_ids": [preselect] if preselect else []},
        recipients=_message_recipients(),
    )