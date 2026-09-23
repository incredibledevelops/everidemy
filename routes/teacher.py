"""
Teacher blueprint — the teacher's daily workspace.

A teacher is a `user` with role="teacher" linked to a `staff` doc.
Every query is scoped by school_id AND by this teacher's classes.
"""
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, abort,
)
from bson import ObjectId

from config import Config
from extensions import (
    users, students, staff, classes, subjects,
    attendance, grades, invoices, payments,
    announcements, messages, timetable as timetable_col,
    audit_logs,
)
from models import School, format_money
from utils.auth import role_required
from utils.announcements import visible_for, unread_count, mark_all_read


teacher_bp = Blueprint(
    "teacher", __name__,
    url_prefix="/teacher",
)


# =========================================================
# LOW-LEVEL HELPERS
# =========================================================
def _to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _day(d):
    """Normalize a datetime to midnight UTC."""
    return datetime(d.year, d.month, d.day)


def _tenant(extra=None):
    """Every query in this blueprint carries the current school_id."""
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
# TEACHER HELPERS
# =========================================================
def _current_staff():
    """Return the staff doc for the current teacher (or None)."""
    return staff.find_one({
        "school_id": g.school["_id"],
        "user_id": g.user["_id"],
    })


def _my_classes():
    """
    Classes this teacher is responsible for:
    - Their own classes (via `classes.teacher_id`)
    - Plus classes they teach via `staff.classes[]`
    """
    st = _current_staff()
    teacher_class_ids = set()

    # Own classes (form teacher)
    for c in classes.find(_tenant({"teacher_id": g.user["_id"], "status": "active"})):
        teacher_class_ids.add(c["_id"])

    # Assigned via staff.classes
    if st and st.get("classes"):
        for cid in st["classes"]:
            teacher_class_ids.add(cid)

    if not teacher_class_ids:
        return []

    return list(
        classes.find(_tenant({"_id": {"$in": list(teacher_class_ids)}}))
        .sort([("level", 1), ("name", 1)])
    )


def _my_subjects():
    st = _current_staff()
    if not st:
        return []
    ids = st.get("subjects") or []
    if not ids:
        return []
    return list(
        subjects.find(_tenant({"_id": {"$in": ids}, "status": "active"}))
        .sort("name", 1)
    )


def _can_access_class(class_id):
    """True if this teacher is allowed to see this class."""
    return class_id in [c["_id"] for c in _my_classes()]


def _message_recipients():
    """Teachers can message anyone in the school (except themselves)."""
    return list(
        users.find({
            "school_id": g.school["_id"],
            "_id": {"$ne": g.user["_id"]},
        }).sort("name", 1)
    )


# =========================================================
# CONTEXT PROCESSOR
# =========================================================
@teacher_bp.context_processor
def inject_teacher_context():
    """
    Values available in every teacher-portal template.
    Computes `unread_messages` + `unread_announcements` so the sidebar
    and notification bell work everywhere without each route passing them.
    """
    unread = 0
    unread_ann = 0
    try:
        unread = messages.count_documents({
            "school_id": g.school["_id"],
            "recipient_ids": g.user["_id"],
            "read_at": None,
        })
    except Exception:
        pass

    try:
        unread_ann = unread_count(g.user, g.school["_id"], announcements)
    except Exception:
        pass

    return {
        "format_money": format_money,
        "currency_symbol": Config.CURRENCY_SYMBOL,
        "currency_code": Config.CURRENCY,
        "unread_messages": unread,
        "unread_announcements": unread_ann,
    }


# =========================================================
# GATE — every route requires teacher (or admin acting as teacher)
# =========================================================
@teacher_bp.before_request
@role_required("teacher", "school_admin")
def _gate():
    return None


# =========================================================
# DASHBOARD
# =========================================================
@teacher_bp.route("/")
@teacher_bp.route("/dashboard")
def dashboard():
    my_classes = _my_classes()
    my_subjects = _my_subjects()

    today = _day(datetime.utcnow())

    # Count students in my classes
    total_students = 0
    for c in my_classes:
        total_students += students.count_documents(_tenant({
            "class_id": c["_id"], "status": "active",
        }))

    # Attendance today
    att_count = 0
    att_total = 0
    for c in my_classes:
        doc = attendance.find_one(_tenant({"class_id": c["_id"], "date": today}))
        if doc:
            for rec in doc.get("records", []):
                att_total += 1
                if rec.get("status") in ("present", "late"):
                    att_count += 1
    attendance_pct = round((att_count / att_total) * 100) if att_total else 0

    # Weekly attendance chart (last 7 days)
    weekly = []
    for i in range(6, -1, -1):
        d = _day(datetime.utcnow() - timedelta(days=i))
        tot = pres = 0
        for c in my_classes:
            doc = attendance.find_one(_tenant({"class_id": c["_id"], "date": d}))
            if doc:
                for rec in doc.get("records", []):
                    tot += 1
                    if rec.get("status") in ("present", "late"):
                        pres += 1
        weekly.append({
            "date": d,
            "label": d.strftime("%a"),
            "pct": round((pres / tot) * 100) if tot else 0,
            "marked": tot > 0,
        })

    # Today's schedule from timetable
    today_name = datetime.utcnow().strftime("%A").lower()
    today_slots = list(
        timetable_col.find(_tenant({
            "teacher_id": g.user["_id"],
            "day": today_name,
        })).sort("start", 1)
    )
    slot_class_ids = {s["class_id"] for s in today_slots}
    slot_subj_ids  = {s["subject_id"] for s in today_slots if s.get("subject_id")}
    class_map = {c["_id"]: c for c in classes.find(_tenant({"_id": {"$in": list(slot_class_ids)}}))}
    subj_map  = {s["_id"]: s for s in subjects.find(_tenant({"_id": {"$in": list(slot_subj_ids)}}))}
    for s in today_slots:
        s["_class"]   = class_map.get(s["class_id"])
        s["_subject"] = subj_map.get(s["subject_id"])

    # Recent notices — respects audience + expiry
    recent_notices = visible_for(g.user, g.school["_id"], announcements, limit=4)

    return render_template(
        "teacher/dashboard.html",
        my_classes=my_classes,
        my_subjects=my_subjects,
        total_students=total_students,
        attendance_pct=attendance_pct,
        attendance_marked=att_total > 0,
        weekly_attendance=weekly,
        today_slots=today_slots,
        recent_notices=recent_notices,
    )


# =========================================================
# MY CLASSES
# =========================================================
@teacher_bp.route("/classes")
def classes_list():
    my_classes = _my_classes()

    for c in my_classes:
        c["_student_count"] = students.count_documents(_tenant({
            "class_id": c["_id"], "status": "active",
        }))

    return render_template("teacher/classes.html", classes=my_classes)


@teacher_bp.route("/classes/<class_id>")
def class_detail(class_id):
    klass = classes.find_one(_tenant({"_id": _to_oid(class_id)}))
    if not klass:
        flash("Class not found.", "error")
        return redirect(url_for("teacher.classes_list"))

    # Access check
    if not _can_access_class(klass["_id"]):
        flash("You don't have access to that class.", "error")
        return redirect(url_for("teacher.classes_list"))

    enrolled = list(
        students.find(_tenant({"class_id": klass["_id"], "status": "active"}))
        .sort([("last_name", 1), ("first_name", 1)])
    )

    # Attendance for the last 30 days
    att_docs = list(attendance.find(_tenant({"class_id": klass["_id"]}))
                    .sort("date", -1).limit(30))
    tot = pres = 0
    for d in att_docs:
        for rec in d.get("records", []):
            tot += 1
            if rec.get("status") in ("present", "late"):
                pres += 1
    class_att_pct = round((pres / tot) * 100) if tot else 0

    return render_template(
        "teacher/class_detail.html",
        klass=klass,
        students=enrolled,
        attendance_pct=class_att_pct,
    )


# =========================================================
# ANNOUNCEMENTS
# =========================================================
@teacher_bp.route("/announcements")
def announcements_page():
    rows = visible_for(g.user, g.school["_id"], announcements)
    mark_all_read(g.user, g.school["_id"], announcements, [a["_id"] for a in rows])
    return render_template("teacher/announcements.html", announcements=rows)


# =========================================================
# ATTENDANCE
# =========================================================
@teacher_bp.route("/attendance")
def attendance_page():
    """Mark attendance for a specific class + date."""
    my_classes = _my_classes()
    if not my_classes:
        flash("You don't have any classes assigned yet.", "warning")
        return redirect(url_for("teacher.dashboard"))

    class_id = request.args.get("class_id", "").strip()
    date_str = request.args.get("date", "").strip()

    chosen_date = _day(datetime.utcnow())
    if date_str:
        try:
            chosen_date = _day(datetime.strptime(date_str, "%Y-%m-%d"))
        except ValueError:
            pass

    if not class_id:
        class_id = str(my_classes[0]["_id"])

    klass = classes.find_one(_tenant({"_id": _to_oid(class_id)}))
    if not klass or not _can_access_class(klass["_id"]):
        flash("Class not found or not assigned to you.", "error")
        return redirect(url_for("teacher.classes_list"))

    students_rows = list(
        students.find(_tenant({"class_id": klass["_id"], "status": "active"}))
        .sort([("last_name", 1), ("first_name", 1)])
    )

    attendance_doc = attendance.find_one(_tenant({
        "class_id": klass["_id"], "date": chosen_date,
    }))

    existing_map = {}
    if attendance_doc:
        for rec in attendance_doc.get("records", []):
            existing_map[rec["student_id"]] = rec.get("status", "present")

    for s in students_rows:
        s["_status"] = existing_map.get(s["_id"])

    summary = {"present": 0, "absent": 0, "late": 0, "excused": 0, "total": 0}
    for s in students_rows:
        st = s.get("_status")
        if st in summary:
            summary[st] += 1
        summary["total"] += 1

    return render_template(
        "teacher/attendance.html",
        class_list=my_classes,
        selected_class=klass,
        selected_class_id=str(klass["_id"]),
        chosen_date=chosen_date,
        date_str=chosen_date.strftime("%Y-%m-%d"),
        today=_day(datetime.utcnow()),
        is_today=chosen_date == _day(datetime.utcnow()),
        students=students_rows,
        attendance_doc=attendance_doc,
        summary=summary,
    )


@teacher_bp.route("/attendance/save", methods=["POST"])
def attendance_save():
    class_id = (request.form.get("class_id") or "").strip()
    date_str = (request.form.get("date") or "").strip()

    klass = classes.find_one(_tenant({"_id": _to_oid(class_id)}))
    if not klass or not _can_access_class(klass["_id"]):
        flash("Class not found or not assigned to you.", "error")
        return redirect(url_for("teacher.classes_list"))

    chosen_date = _day(datetime.utcnow())
    if date_str:
        try:
            chosen_date = _day(datetime.strptime(date_str, "%Y-%m-%d"))
        except ValueError:
            pass

    records = []
    for key, value in request.form.items():
        if key.startswith("status_"):
            sid = key.replace("status_", "")
            oid = _to_oid(sid)
            if not oid:
                continue
            records.append({
                "student_id": oid,
                "status": value,
                "note": (request.form.get(f"note_{sid}") or "").strip() or None,
            })

    if not records:
        flash("No attendance recorded.", "warning")
        return redirect(url_for("teacher.attendance_page", class_id=class_id, date=date_str))

    now = datetime.utcnow()
    existing = attendance.find_one(_tenant({"class_id": klass["_id"], "date": chosen_date}))

    if existing:
        attendance.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "records": records,
                "updated_at": now,
                "taken_by": g.user["_id"],
            }},
        )
        action = "attendance.updated"
    else:
        attendance.insert_one({
            "school_id": g.school["_id"],
            "class_id": klass["_id"],
            "date": chosen_date,
            "records": records,
            "taken_by": g.user["_id"],
            "created_at": now,
            "updated_at": now,
        })
        action = "attendance.marked"

    School.log(g.school["_id"], g.user["_id"], action, {
        "class_id": str(klass["_id"]),
        "class_name": klass["name"],
        "date": date_str,
        "count": len(records),
    })

    flash(f"Attendance saved for {klass['name']} · {chosen_date.strftime('%b %d, %Y')}.", "success")
    return redirect(url_for("teacher.attendance_page", class_id=class_id, date=date_str))


# =========================================================
# GRADEBOOK
# =========================================================
@teacher_bp.route("/gradebook")
def gradebook():
    """Pick a class + subject + term and enter grades."""
    my_classes = _my_classes()
    my_subjects = _my_subjects()

    if not my_classes or not my_subjects:
        return render_template(
            "teacher/gradebook.html",
            class_list=my_classes,
            subject_list=my_subjects,
            selected_class=None,
            selected_subject=None,
            selected_class_id="",
            selected_subject_id="",
            rows=[],
            term=_current_term(),
            academic_year=_current_academic_year(),
            ca_max=30,
            exam_max=70,
        )

    class_id   = request.args.get("class_id", "").strip()
    subject_id = request.args.get("subject_id", "").strip()
    term       = request.args.get("term", _current_term())
    ay         = request.args.get("academic_year", _current_academic_year())

    if not class_id:
        class_id = str(my_classes[0]["_id"])
    if not subject_id and my_subjects:
        subject_id = str(my_subjects[0]["_id"])

    klass   = classes.find_one(_tenant({"_id": _to_oid(class_id)}))
    subject = subjects.find_one(_tenant({"_id": _to_oid(subject_id)}))

    rows = []
    if klass and subject:
        student_rows = list(
            students.find(_tenant({"class_id": klass["_id"], "status": "active"}))
            .sort([("last_name", 1), ("first_name", 1)])
        )
        existing = list(grades.find(_tenant({
            "class_id": klass["_id"],
            "subject_id": subject["_id"],
            "term": term,
            "academic_year": ay,
        })))
        gmap = {g["student_id"]: g for g in existing}
        for s in student_rows:
            gg = gmap.get(s["_id"], {})
            rows.append({
                "student": s,
                "ca":   gg.get("ca_score", ""),
                "exam": gg.get("exam_score", ""),
                "total": gg.get("total"),
                "grade": gg.get("grade"),
                "remark": gg.get("remark"),
            })

    return render_template(
        "teacher/gradebook.html",
        class_list=my_classes,
        subject_list=my_subjects,
        selected_class=klass,
        selected_subject=subject,
        selected_class_id=str(klass["_id"]) if klass else "",
        selected_subject_id=str(subject["_id"]) if subject else "",
        rows=rows,
        term=term,
        academic_year=ay,
        ca_max=30,
        exam_max=70,
    )


@teacher_bp.route("/gradebook/save", methods=["POST"])
def gradebook_save():
    from routes.school_admin import _compute_total

    class_id   = (request.form.get("class_id") or "").strip()
    subject_id = (request.form.get("subject_id") or "").strip()
    term       = (request.form.get("term") or _current_term()).strip()
    ay         = (request.form.get("academic_year") or _current_academic_year()).strip()

    klass   = classes.find_one(_tenant({"_id": _to_oid(class_id)}))
    subject = subjects.find_one(_tenant({"_id": _to_oid(subject_id)}))

    if not klass or not subject:
        flash("Class or subject not found.", "error")
        return redirect(url_for("teacher.gradebook"))

    if not _can_access_class(klass["_id"]):
        flash("You don't have access to this class.", "error")
        return redirect(url_for("teacher.gradebook"))

    student_rows = list(students.find(_tenant({"class_id": klass["_id"], "status": "active"})))
    now = datetime.utcnow()
    saved = cleared = 0

    for s in student_rows:
        sid = str(s["_id"])
        ca_raw   = request.form.get(f"ca_{sid}")
        exam_raw = request.form.get(f"exam_{sid}")

        if (ca_raw or "").strip() == "" and (exam_raw or "").strip() == "":
            r = grades.delete_one(_tenant({
                "student_id": s["_id"],
                "subject_id": subject["_id"],
                "term": term,
                "academic_year": ay,
            }))
            if r.deleted_count:
                cleared += 1
            continue

        try:
            ca   = int((ca_raw or "0").strip())
            exam = int((exam_raw or "0").strip())
        except ValueError:
            ca, exam = 0, 0

        total, letter, remark = _compute_total(ca, exam)

        grades.update_one(
            _tenant({
                "student_id": s["_id"],
                "subject_id": subject["_id"],
                "term": term,
                "academic_year": ay,
            }),
            {
                "$set": {
                    "class_id":   klass["_id"],
                    "ca_score":   ca,
                    "exam_score": exam,
                    "total":      total,
                    "grade":      letter,
                    "remark":     remark,
                    "entered_by": g.user["_id"],
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "school_id":     g.school["_id"],
                    "student_id":    s["_id"],
                    "subject_id":    subject["_id"],
                    "term":          term,
                    "academic_year": ay,
                    "created_at":    now,
                },
            },
            upsert=True,
        )
        saved += 1

    School.log(g.school["_id"], g.user["_id"], "grades.saved", {
        "class_id":   str(klass["_id"]),
        "subject_id": str(subject["_id"]),
        "term":       term,
        "saved":      saved,
        "cleared":    cleared,
        "by":         "teacher",
    })

    flash(f"Grades saved · {saved} entries.", "success")
    return redirect(url_for(
        "teacher.gradebook",
        class_id=class_id, subject_id=subject_id,
        term=term, academic_year=ay,
    ))


# =========================================================
# MESSAGES
# =========================================================
def _build_threads(user_id):
    """Return thread summaries for the given user."""
    msgs = list(messages.find({
        "school_id": g.school["_id"],
        "$or": [
            {"sender_id": user_id},
            {"recipient_ids": user_id},
        ],
    }).sort("created_at", -1))

    by_thread = {}
    for m in msgs:
        tid = m.get("thread_id")
        if tid is None:
            continue
        by_thread.setdefault(tid, []).append(m)

    summaries = []
    for tid, arr in by_thread.items():
        last = arr[0]
        unread = sum(
            1 for m in arr
            if (m.get("read_at") is None) and (user_id in m.get("recipient_ids", []))
        )

        partner_ids = set()
        for m in arr:
            for pid in m.get("recipient_ids", []):
                if pid != user_id:
                    partner_ids.add(pid)
            if m["sender_id"] != user_id:
                partner_ids.add(m["sender_id"])

        partners = list(users.find({"_id": {"$in": list(partner_ids)}}))

        summaries.append({
            "thread_id":    tid,
            "subject":      last.get("subject") or "(no subject)",
            "last_message": last,
            "partners":     partners,
            "unread":       unread,
            "updated_at":   last.get("created_at"),
        })

    summaries.sort(key=lambda s: s["updated_at"] or datetime.min, reverse=True)
    return summaries


@teacher_bp.route("/messages")
def messages_page():
    search = request.args.get("q", "").strip()
    tab = request.args.get("tab", "inbox")
    threads = _build_threads(g.user["_id"])

    if tab == "unread":
        threads = [t for t in threads if t["unread"] > 0]

    if search:
        s = search.lower()
        threads = [
            t for t in threads
            if s in (t["subject"] or "").lower()
            or any(s in (p.get("name") or "").lower() for p in t["partners"])
        ]

    total_unread = sum(t["unread"] for t in threads)

    return render_template(
        "teacher/messages.html",
        threads=threads,
        tab=tab,
        search=search,
        total_unread=total_unread,
        recipients=_message_recipients()[:8],
    )


@teacher_bp.route("/messages/thread/<thread_id>")
def message_thread(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    msgs = list(messages.find({
        "school_id": g.school["_id"],
        "thread_id": tid,
        "$or": [
            {"sender_id": g.user["_id"]},
            {"recipient_ids": g.user["_id"]},
        ],
    }).sort("created_at", 1))

    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("teacher.messages_page"))

    now = datetime.utcnow()
    messages.update_many(
        {"school_id": g.school["_id"], "thread_id": tid,
         "recipient_ids": g.user["_id"], "read_at": None},
        {"$set": {"read_at": now}},
    )

    sender_ids = {m["sender_id"] for m in msgs}
    sender_map = {u["_id"]: u for u in users.find({"_id": {"$in": list(sender_ids)}})}
    for m in msgs:
        m["_sender"]  = sender_map.get(m["sender_id"])
        m["_is_mine"] = (m["sender_id"] == g.user["_id"])

    participant_ids = set()
    for m in msgs:
        participant_ids.add(m["sender_id"])
        for pid in m.get("recipient_ids", []):
            participant_ids.add(pid)
    participant_ids.discard(g.user["_id"])

    participants = list(users.find({"_id": {"$in": list(participant_ids)}}))
    subject = msgs[0].get("subject") or "(no subject)"

    return render_template(
        "teacher/message_thread.html",
        thread_id=str(tid),
        messages=msgs,
        participants=participants,
        subject=subject,
    )


@teacher_bp.route("/messages/thread/<thread_id>/reply", methods=["POST"])
def message_reply(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message body is required.", "error")
        return redirect(url_for("teacher.message_thread", thread_id=thread_id))

    msgs = list(messages.find({"school_id": g.school["_id"], "thread_id": tid}).limit(500))
    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("teacher.messages_page"))

    participant_ids = set()
    for m in msgs:
        participant_ids.add(m["sender_id"])
        for pid in m.get("recipient_ids", []):
            participant_ids.add(pid)
    participant_ids.discard(g.user["_id"])

    now = datetime.utcnow()
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
        "created_at":    now,
    })

    return redirect(url_for("teacher.message_thread", thread_id=thread_id))


@teacher_bp.route("/messages/new", methods=["GET", "POST"])
def message_new():
    """Compose a brand-new message (starts a new thread)."""
    if request.method == "POST":
        recipient_ids_raw = request.form.getlist("recipient_ids") or []
        subject = (request.form.get("subject") or "").strip()
        body    = (request.form.get("body") or "").strip()

        recipient_ids = [_to_oid(rid) for rid in recipient_ids_raw if _to_oid(rid)]
        recipient_ids = [r for r in recipient_ids if r]

        errors = []
        if not recipient_ids: errors.append("Select at least one recipient.")
        if not body:          errors.append("Message body is required.")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(url_for("teacher.message_new"))

        now = datetime.utcnow()
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
            "created_at":    now,
        })
        flash("Message sent.", "success")
        return redirect(url_for("teacher.message_thread", thread_id=str(thread_id)))

    # GET — support ?to=<user_id> preselect
    preselect = request.args.get("to", "").strip()
    return render_template(
        "teacher/message_compose.html",
        form={"recipient_ids": [preselect] if preselect else []},
        recipients=_message_recipients(),
    )