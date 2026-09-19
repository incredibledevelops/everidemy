"""
School Admin blueprint — the workspace each school uses daily.

Every route in this blueprint:
1. Requires a logged-in user (via @school_required)
2. Requires that user to belong to a school
3. Scopes every Mongo query by the current school's _id

This is the multi-tenant isolation layer.
"""
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, abort, make_response,
)
from bson import ObjectId

from config import Config
from extensions import (
    schools, users, students, staff, classes, subjects,
    attendance, grades, invoices, announcements,
    audit_logs, timetable as timetable_col,
    fee_structures, payments, messages,
)
from models import School, Subscription, User, format_money
from utils.auth import school_required
from utils.mailer import send_school_invite_email
from utils.reports import (
    enrollment_summary, enrollment_trend,
    attendance_summary, attendance_by_class, attendance_trend,
    academics_summary, class_rankings, top_students,
    finance_summary, monthly_revenue, outstanding_by_class,
    staff_summary,
    parse_range, rows_to_csv,
)
from utils.settings import (
    get_settings, update_section,
    validate_grading_scale,
    DEFAULT_GRADING_SCALE,
)


# =========================================================
# BLUEPRINT
# =========================================================
school_admin_bp = Blueprint(
    "school_admin", __name__,
    url_prefix="/school-admin",
)


# =========================================================
# CONSTANTS
# =========================================================
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
WEEKDAY_LABELS = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday",
}

GRADE_SCALE = [
    (80, "A1", "Excellent"),
    (75, "B2", "Very Good"),
    (70, "B3", "Good"),
    (65, "C4", "Credit"),
    (60, "C5", "Credit"),
    (55, "C6", "Credit"),
    (50, "D7", "Pass"),
    (45, "E8", "Weak Pass"),
    (0,  "F9", "Fail"),
]

TERMS = [
    ("first",  "First Term"),
    ("second", "Second Term"),
    ("third",  "Third Term"),
]

CA_MAX = 30
EXAM_MAX = 70

PAYMENT_CHANNELS = [
    ("cash",     "Cash"),
    ("transfer", "Bank Transfer"),
    ("paystack", "Paystack"),
    ("cheque",   "Cheque"),
]

INVOICE_STATUSES = [
    ("unpaid",  "Unpaid"),
    ("partial", "Partial"),
    ("paid",    "Paid"),
    ("waived",  "Waived"),
]

ANNOUNCEMENT_AUDIENCES = [
    ("all",           "Everyone"),
    ("students",      "Students"),
    ("parents",       "Parents"),
    ("staff",         "Staff"),
    ("school_admins", "School Admins"),
]

ANNOUNCEMENT_PRIORITIES = [
    ("normal", "Normal"),
    ("high",   "High"),
    ("urgent", "Urgent"),
]

SETTINGS_SECTIONS = [
    ("profile",       "School Profile",  "General info, address, timezone"),
    ("academic",      "Academics",       "Terms, academic year, grading scale"),
    ("fees",          "Fees & Invoices", "Invoice numbering, due dates"),
    ("notifications", "Notifications",   "Email and SMS preferences"),
    ("team",          "Team & Roles",    "Staff permissions and access"),
    ("integrations",  "Integrations",    "Paystack, SMS, and email"),
    ("data",          "Data & Privacy",  "Export, import, and account data"),
]


# =========================================================
# LOW-LEVEL HELPERS
# =========================================================
def tenant_filter(extra=None) -> dict:
    """Merge `school_id` into any Mongo query filter."""
    f = dict(extra or {})
    f["school_id"] = g.school["_id"]
    return f


def _to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _day(d: datetime) -> datetime:
    """Normalize any datetime to midnight UTC."""
    return datetime(d.year, d.month, d.day)


def _parse_date(s: str, default=None):
    """Parse YYYY-MM-DD → datetime, or return default."""
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return default


def _valid_time(s: str) -> bool:
    if not s or len(s) != 5 or s[2] != ":":
        return False
    try:
        h, m = int(s[:2]), int(s[3:])
        return 0 <= h <= 23 and 0 <= m <= 59
    except ValueError:
        return False


def _find_overlap(class_id, day, start, end, exclude_id=None):
    q = {"class_id": class_id, "day": day}
    for slot in timetable_col.find(tenant_filter(q)):
        if exclude_id and slot["_id"] == exclude_id:
            continue
        if slot["start"] < end and slot["end"] > start:
            return slot
    return None


def _grade_for(total):
    if total is None:
        return None, None
    for threshold, letter, remark in GRADE_SCALE:
        if total >= threshold:
            return letter, remark
    return "F9", "Fail"


def _compute_total(ca, exam):
    ca   = max(0, min(int(ca or 0), CA_MAX))
    exam = max(0, min(int(exam or 0), EXAM_MAX))
    total = ca + exam
    letter, remark = _grade_for(total)
    return total, letter, remark


def _current_term():
    m = datetime.utcnow().month
    if m in (9, 10, 11, 12): return "first"
    if m in (1, 2, 3):       return "second"
    return "third"


def _current_academic_year():
    y = datetime.utcnow().year
    if datetime.utcnow().month >= 8:
        return f"{y}/{y + 1}"
    return f"{y - 1}/{y}"


def _range_for_template(start, end):
    return {"from": start.strftime("%Y-%m-%d"),
            "to":   end.strftime("%Y-%m-%d")}


# =========================================================
# CONTEXT PROCESSOR
# =========================================================
@school_admin_bp.context_processor
def inject_school_context():
    return {
        "format_money": format_money,
        "currency_symbol": Config.CURRENCY_SYMBOL,
        "currency_code": Config.CURRENCY,
    }


# =========================================================
# DASHBOARD
# =========================================================
@school_admin_bp.route("/")
@school_admin_bp.route("/dashboard")
@school_required
def dashboard():
    school = g.school
    sid = school["_id"]

    total_students = students.count_documents(tenant_filter({"status": "active"}))
    total_staff    = staff.count_documents(tenant_filter({"status": "active"}))

    today = _day(datetime.utcnow())
    attendance_docs = list(attendance.find(tenant_filter({"date": today})))
    total_records = 0
    present_records = 0
    for doc in attendance_docs:
        for rec in doc.get("records", []):
            total_records += 1
            if rec.get("status") in ("present", "late"):
                present_records += 1
    attendance_pct = round((present_records / total_records) * 100) if total_records else 0

    fee_pipeline = [
        {"$match": tenant_filter({})},
        {"$group": {
            "_id": None,
            "expected": {"$sum": "$amount_due"},
            "collected": {"$sum": "$amount_paid"},
        }},
    ]
    fee_result = list(invoices.aggregate(fee_pipeline))
    fees_expected  = fee_result[0]["expected"]  if fee_result else 0
    fees_collected = fee_result[0]["collected"] if fee_result else 0
    fees_pct = round((fees_collected / fees_expected) * 100) if fees_expected else 0

    recent_activity = list(audit_logs.find(tenant_filter({})).sort("timestamp", -1).limit(6))
    newest_students = list(
        students.find(tenant_filter({"status": "active"}))
        .sort("created_at", -1).limit(5)
    )
    recent_notices = list(
        announcements.find({"school_id": {"$in": [sid, None]}})
        .sort("created_at", -1).limit(4)
    )

    subscription = Subscription.find_for_school(sid)
    trial_days_left = None
    if school.get("subscription_status") == "trialing" and school.get("trial_ends_at"):
        delta = (school["trial_ends_at"] - datetime.utcnow()).days
        trial_days_left = max(delta, 0)

    weekly = []
    for i in range(6, -1, -1):
        d = _day(datetime.utcnow() - timedelta(days=i))
        docs = list(attendance.find(tenant_filter({"date": d})))
        tot = pres = 0
        for doc in docs:
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

    unread_messages = messages.count_documents({
        "school_id": sid,
        "recipient_ids": g.user["_id"],
        "read_at": None,
    })

    return render_template(
        "school_admin/dashboard.html",
        total_students=total_students,
        total_staff=total_staff,
        attendance_pct=attendance_pct,
        attendance_marked=total_records > 0,
        fees_pct=fees_pct,
        fees_expected=fees_expected,
        fees_collected=fees_collected,
        recent_activity=recent_activity,
        newest_students=newest_students,
        recent_notices=recent_notices,
        subscription=subscription,
        trial_days_left=trial_days_left,
        weekly_attendance=weekly,
        unread_messages=unread_messages,
        plans=Config.PLANS,
    )


# =========================================================
# STUDENTS
# =========================================================
@school_admin_bp.route("/students")
@school_required
def students_list():
    search  = request.args.get("q", "").strip()
    class_f = request.args.get("class_id", "").strip()
    status  = request.args.get("status", "active").strip()
    page    = max(1, int(request.args.get("page", 1)))
    per_page = 25

    q = {"status": status} if status else {}
    if search:
        q["$or"] = [
            {"first_name":   {"$regex": search, "$options": "i"}},
            {"last_name":    {"$regex": search, "$options": "i"}},
            {"middle_name":  {"$regex": search, "$options": "i"}},
            {"admission_no": {"$regex": search, "$options": "i"}},
        ]
    if class_f:
        oid = _to_oid(class_f)
        if oid:
            q["class_id"] = oid

    full_q = tenant_filter(q)
    total = students.count_documents(full_q)
    rows  = list(
        students.find(full_q)
        .sort([("last_name", 1), ("first_name", 1)])
        .skip((page - 1) * per_page).limit(per_page)
    )

    class_ids = {r["class_id"] for r in rows if r.get("class_id")}
    class_map = {}
    if class_ids:
        for c in classes.find(tenant_filter({"_id": {"$in": list(class_ids)}})):
            class_map[c["_id"]] = c
    for r in rows:
        r["_class"] = class_map.get(r.get("class_id"))

    class_list = list(classes.find(tenant_filter({})).sort("name", 1))
    stats = {
        "total":  students.count_documents(tenant_filter({})),
        "active": students.count_documents(tenant_filter({"status": "active"})),
        "male":   students.count_documents(tenant_filter({"gender": "male", "status": "active"})),
        "female": students.count_documents(tenant_filter({"gender": "female", "status": "active"})),
    }

    return render_template(
        "school_admin/students.html",
        students=rows, total=total, page=page, per_page=per_page,
        search=search, class_f=class_f, status=status,
        class_list=class_list, stats=stats,
    )


@school_admin_bp.route("/students/new", methods=["GET", "POST"])
@school_required
def student_new():
    if request.method == "POST":
        data, errors = _validate_student_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/student_form.html",
                form=request.form, mode="create",
                class_list=_classes_for_dropdown(),
                next_admission_no=request.form.get("admission_no", ""),
            ), 400

        doc = {
            **data,
            "school_id": g.school["_id"],
            "guardian_ids": [], "photo": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = students.insert_one(doc)
        student_id = result.inserted_id
        School.log(g.school["_id"], g.user["_id"], "student.created", {
            "student_id": str(student_id),
            "name": f"{data['first_name']} {data['last_name']}",
            "admission_no": data["admission_no"],
        })
        flash(f"{data['first_name']} {data['last_name']} admitted successfully.", "success")
        if request.form.get("save_and_new"):
            return redirect(url_for("school_admin.student_new"))
        return redirect(url_for("school_admin.student_detail", student_id=str(student_id)))

    return render_template(
        "school_admin/student_form.html",
        form={"admission_no": _generate_admission_no()},
        mode="create",
        class_list=_classes_for_dropdown(),
        next_admission_no=_generate_admission_no(),
    )


@school_admin_bp.route("/students/<student_id>")
@school_required
def student_detail(student_id):
    student = _load_student_or_404(student_id)
    klass = None
    if student.get("class_id"):
        klass = classes.find_one(tenant_filter({"_id": student["class_id"]}))

    attendance_pct = _student_attendance_pct(student["_id"])
    grades_avg     = _student_grade_avg(student["_id"])
    fees_summary   = _student_fees_summary(student["_id"])
    fees_detail    = _student_fees_detail(student["_id"])
    activity = list(
        audit_logs.find(tenant_filter({"meta.student_id": str(student["_id"])}))
        .sort("timestamp", -1).limit(10)
    )
    att_records   = _student_attendance_detail(student["_id"])
    grades_detail = _student_grades_detail(student["_id"])

    return render_template(
        "school_admin/student_detail.html",
        student=student, klass=klass,
        attendance_pct=attendance_pct, grades_avg=grades_avg,
        fees_summary=fees_summary, fees_detail=fees_detail,
        activity=activity,
        attendance_records=att_records,
        grades_detail=grades_detail,
        terms=TERMS,
        active_tab=request.args.get("tab", "overview"),
    )


@school_admin_bp.route("/students/<student_id>/edit", methods=["GET", "POST"])
@school_required
def student_edit(student_id):
    student = _load_student_or_404(student_id)
    if request.method == "POST":
        data, errors = _validate_student_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/student_form.html",
                form=request.form, mode="edit", student=student,
                class_list=_classes_for_dropdown(),
                next_admission_no=data.get("admission_no", ""),
            ), 400

        data["updated_at"] = datetime.utcnow()
        students.update_one(tenant_filter({"_id": student["_id"]}), {"$set": data})
        School.log(g.school["_id"], g.user["_id"], "student.updated", {
            "student_id": str(student["_id"]),
            "name": f"{data['first_name']} {data['last_name']}",
        })
        flash("Student updated successfully.", "success")
        return redirect(url_for("school_admin.student_detail", student_id=student_id))

    form = {
        "first_name":   student.get("first_name", ""),
        "middle_name":  student.get("middle_name", ""),
        "last_name":    student.get("last_name", ""),
        "gender":       student.get("gender", ""),
        "dob":          student["dob"].strftime("%Y-%m-%d") if student.get("dob") else "",
        "blood_group":  student.get("blood_group", ""),
        "admission_no": student.get("admission_no", ""),
        "class_id":     str(student["class_id"]) if student.get("class_id") else "",
        "status":       student.get("status", "active"),
        "guardian_name":  student.get("guardian_name", ""),
        "guardian_phone": student.get("guardian_phone", ""),
        "guardian_email": student.get("guardian_email", ""),
        "relationship":   student.get("relationship", ""),
        "address":        student.get("address", ""),
        "notes":          student.get("notes", ""),
    }
    return render_template(
        "school_admin/student_form.html",
        form=form, mode="edit", student=student,
        class_list=_classes_for_dropdown(),
        next_admission_no=form["admission_no"],
    )


@school_admin_bp.route("/students/<student_id>/delete", methods=["POST"])
@school_required
def student_delete(student_id):
    student = _load_student_or_404(student_id)
    name = f"{student.get('first_name','')} {student.get('last_name','')}".strip()
    students.update_one(tenant_filter({"_id": student["_id"]}), {"$set": {
        "status": "archived", "archived_at": datetime.utcnow(),
        "archived_by": g.user["_id"], "updated_at": datetime.utcnow(),
    }})
    School.log(g.school["_id"], g.user["_id"], "student.archived", {
        "student_id": str(student["_id"]), "name": name,
    })
    flash(f"{name} has been archived.", "success")
    return redirect(url_for("school_admin.students_list"))


# ---------- Student helpers ----------
def _generate_admission_no() -> str:
    year = datetime.utcnow().year
    prefix = f"STU-{year}-"
    last = students.find_one(
        tenant_filter({"admission_no": {"$regex": f"^{prefix}"}}),
        sort=[("admission_no", -1)],
    )
    if last and last.get("admission_no"):
        try:
            n = int(last["admission_no"].rsplit("-", 1)[-1]) + 1
        except Exception:
            n = 1
    else:
        n = 1
    return f"{prefix}{n:04d}"


def _classes_for_dropdown():
    rows = list(classes.find(tenant_filter({"status": "active"})).sort("name", 1))
    return [{"id": str(c["_id"]), "name": c["name"]} for c in rows]


def _validate_student_form(form) -> tuple:
    def get(k): return (form.get(k) or "").strip()

    first_name    = get("first_name")
    middle_name   = get("middle_name")
    last_name     = get("last_name")
    gender        = get("gender").lower()
    dob_str       = get("dob")
    blood_group   = get("blood_group")
    admission_no  = get("admission_no")
    class_id      = get("class_id")
    status        = get("status") or "active"
    guardian_name = get("guardian_name")
    guardian_phone= get("guardian_phone")
    guardian_email= get("guardian_email")
    relationship  = get("relationship")
    address       = get("address")
    notes         = get("notes")

    errors = []
    if not first_name: errors.append("First name is required.")
    if not last_name:  errors.append("Last name is required.")
    if gender not in ("male", "female", ""):
        errors.append("Gender must be male or female.")
    if not admission_no:
        errors.append("Admission number is required.")
    else:
        existing = students.find_one(tenant_filter({"admission_no": admission_no}))
        current_id = form.get("_id")
        if existing and (not current_id or str(existing["_id"]) != current_id):
            errors.append(f"Admission number {admission_no} is already in use.")

    dob = None
    if dob_str:
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d")
        except ValueError:
            errors.append("Date of birth must be a valid date (YYYY-MM-DD).")

    class_obj_id = None
    if class_id:
        class_obj_id = _to_oid(class_id)
        if not class_obj_id:
            errors.append("Invalid class.")

    clean = {
        "first_name":   first_name,
        "middle_name":  middle_name,
        "last_name":    last_name,
        "gender":       gender or None,
        "dob":          dob,
        "blood_group":  blood_group or None,
        "admission_no": admission_no,
        "class_id":     class_obj_id,
        "status":       status,
        "guardian_name":  guardian_name or None,
        "guardian_phone": guardian_phone or None,
        "guardian_email": guardian_email or None,
        "relationship":   relationship or None,
        "address":        address or None,
        "notes":          notes or None,
    }
    return clean, errors


def _load_student_or_404(student_id: str):
    oid = _to_oid(student_id)
    if not oid:
        abort(404)
    student = students.find_one(tenant_filter({"_id": oid}))
    if not student:
        flash("Student not found.", "error")
        abort(404)
    return student


def _student_attendance_pct(student_id) -> int:
    docs = attendance.find(tenant_filter({}))
    total = 0
    present = 0
    for doc in docs:
        for rec in doc.get("records", []):
            if rec.get("student_id") == student_id:
                total += 1
                if rec.get("status") in ("present", "late"):
                    present += 1
    return round((present / total) * 100) if total else 0


def _student_attendance_detail(student_id) -> dict:
    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    recent = []
    docs = list(attendance.find(tenant_filter({})).sort("date", -1).limit(60))
    for doc in docs:
        for rec in doc.get("records", []):
            if rec.get("student_id") == student_id:
                status = rec.get("status", "present")
                counts[status] = counts.get(status, 0) + 1
                if len(recent) < 30:
                    recent.append({"date": doc["date"], "status": status})
    return {"counts": counts, "recent": recent}


def _student_grade_avg(student_id) -> int:
    pipeline = [
        {"$match": tenant_filter({"student_id": student_id})},
        {"$group": {"_id": None, "avg": {"$avg": "$total"}}},
    ]
    res = list(grades.aggregate(pipeline))
    return round(res[0]["avg"]) if res and res[0].get("avg") is not None else 0


def _student_fees_summary(student_id) -> dict:
    pipeline = [
        {"$match": tenant_filter({"student_id": student_id})},
        {"$group": {
            "_id": None,
            "expected": {"$sum": "$amount_due"},
            "paid":     {"$sum": "$amount_paid"},
        }},
    ]
    res = list(invoices.aggregate(pipeline))
    if res:
        expected = res[0].get("expected") or 0
        paid     = res[0].get("paid") or 0
    else:
        expected = paid = 0
    return {"expected": expected, "paid": paid, "balance": expected - paid}


def _student_grades_detail(student_id, term=None, academic_year=None) -> dict:
    term = term or _current_term()
    ay   = academic_year or _current_academic_year()

    rows = list(grades.find(tenant_filter({
        "student_id": student_id,
        "term": term,
        "academic_year": ay,
    })))

    subj_ids = [r["subject_id"] for r in rows]
    subj_map = {}
    if subj_ids:
        for s in subjects.find(tenant_filter({"_id": {"$in": subj_ids}})):
            subj_map[s["_id"]] = s

    enriched = []
    total_sum = 0
    for r in rows:
        subj = subj_map.get(r["subject_id"])
        if not subj:
            continue
        enriched.append({
            "subject": subj["name"],
            "code":    subj.get("code", ""),
            "ca":      r.get("ca_score", 0),
            "exam":    r.get("exam_score", 0),
            "total":   r.get("total", 0),
            "grade":   r.get("grade", "—"),
            "remark":  r.get("remark", ""),
        })
        total_sum += r.get("total", 0)

    enriched.sort(key=lambda x: x["subject"])
    average = round(total_sum / len(enriched)) if enriched else 0
    letter, remark = _grade_for(average) if enriched else (None, None)

    return {
        "rows": enriched,
        "average": average,
        "grade": letter,
        "remark": remark,
        "term": term,
        "academic_year": ay,
    }


# =========================================================
# CLASSES
# =========================================================
@school_admin_bp.route("/classes")
@school_required
def classes_list():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "active").strip()

    q = {"status": status} if status else {}
    if search:
        q["$or"] = [
            {"name":  {"$regex": search, "$options": "i"}},
            {"level": {"$regex": search, "$options": "i"}},
        ]

    rows = list(classes.find(tenant_filter(q)).sort([("level", 1), ("name", 1)]))

    teacher_ids = {r["teacher_id"] for r in rows if r.get("teacher_id")}
    teacher_map = {}
    if teacher_ids:
        for u in users.find({"_id": {"$in": list(teacher_ids)}}):
            teacher_map[u["_id"]] = u

    for r in rows:
        r["_teacher"] = teacher_map.get(r.get("teacher_id"))
        r["_student_count"] = students.count_documents(tenant_filter({
            "class_id": r["_id"], "status": "active"
        }))

    stats = {
        "total":     classes.count_documents(tenant_filter({})),
        "active":    classes.count_documents(tenant_filter({"status": "active"})),
        "archived":  classes.count_documents(tenant_filter({"status": "archived"})),
        "students":  students.count_documents(tenant_filter({"status": "active"})),
    }

    return render_template(
        "school_admin/classes.html",
        classes=rows, stats=stats, search=search, status=status,
    )


@school_admin_bp.route("/classes/new", methods=["GET", "POST"])
@school_required
def class_new():
    if request.method == "POST":
        data, errors = _validate_class_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/class_form.html",
                form=request.form, mode="create",
                teacher_list=_staff_for_dropdown(),
            ), 400

        doc = {
            **data,
            "school_id": g.school["_id"],
            "students": [], "subject_ids": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = classes.insert_one(doc)
        School.log(g.school["_id"], g.user["_id"], "class.created", {
            "class_id": str(result.inserted_id), "name": data["name"],
        })
        flash(f"Class “{data['name']}” created.", "success")
        return redirect(url_for("school_admin.classes_list"))

    return render_template(
        "school_admin/class_form.html",
        form={}, mode="create",
        teacher_list=_staff_for_dropdown(),
    )


@school_admin_bp.route("/classes/<class_id>/edit", methods=["GET", "POST"])
@school_required
def class_edit(class_id):
    klass = _load_class_or_404(class_id)
    if request.method == "POST":
        data, errors = _validate_class_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/class_form.html",
                form=request.form, mode="edit", klass=klass,
                teacher_list=_staff_for_dropdown(),
            ), 400

        data["updated_at"] = datetime.utcnow()
        classes.update_one(tenant_filter({"_id": klass["_id"]}), {"$set": data})
        School.log(g.school["_id"], g.user["_id"], "class.updated", {
            "class_id": str(klass["_id"]), "name": data["name"],
        })
        flash("Class updated.", "success")
        return redirect(url_for("school_admin.classes_list"))

    form = {
        "name":       klass.get("name", ""),
        "level":      klass.get("level", ""),
        "section":    klass.get("section", ""),
        "capacity":   klass.get("capacity", ""),
        "teacher_id": str(klass["teacher_id"]) if klass.get("teacher_id") else "",
        "status":     klass.get("status", "active"),
        "academic_year": klass.get("academic_year", ""),
    }
    return render_template(
        "school_admin/class_form.html",
        form=form, mode="edit", klass=klass,
        teacher_list=_staff_for_dropdown(),
    )


@school_admin_bp.route("/classes/<class_id>/delete", methods=["POST"])
@school_required
def class_delete(class_id):
    klass = _load_class_or_404(class_id)
    n = students.count_documents(tenant_filter({"class_id": klass["_id"], "status": "active"}))
    if n:
        flash(f"Can't delete “{klass['name']}” — {n} student(s) are still assigned. "
              f"Move them to another class first.", "error")
        return redirect(url_for("school_admin.classes_list"))

    classes.update_one(tenant_filter({"_id": klass["_id"]}), {"$set": {
        "status": "archived", "archived_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }})
    School.log(g.school["_id"], g.user["_id"], "class.archived", {
        "class_id": str(klass["_id"]), "name": klass["name"],
    })
    flash(f"Class “{klass['name']}” archived.", "success")
    return redirect(url_for("school_admin.classes_list"))


def _staff_for_dropdown():
    rows = list(users.find({
        "school_id": g.school["_id"],
        "role": {"$in": ["teacher", "school_admin"]},
    }).sort("name", 1))
    return [{"id": str(u["_id"]), "name": u["name"]} for u in rows]


def _validate_class_form(form) -> tuple:
    def get(k): return (form.get(k) or "").strip()
    name       = get("name")
    level      = get("level")
    section    = get("section")
    capacity   = get("capacity")
    teacher_id = get("teacher_id")
    status     = get("status") or "active"
    academic_year = get("academic_year")

    errors = []
    if not name: errors.append("Class name is required.")

    cap = None
    if capacity:
        try:
            cap = int(capacity)
            if cap < 1 or cap > 500:
                errors.append("Capacity must be between 1 and 500.")
        except ValueError:
            errors.append("Capacity must be a number.")

    t_oid = None
    if teacher_id:
        t_oid = _to_oid(teacher_id)
        if not t_oid:
            errors.append("Invalid teacher.")

    clean = {
        "name":          name,
        "level":         level or None,
        "section":       section or None,
        "capacity":      cap,
        "teacher_id":    t_oid,
        "status":        status,
        "academic_year": academic_year or None,
    }
    return clean, errors


def _load_class_or_404(class_id: str):
    oid = _to_oid(class_id)
    if not oid:
        abort(404)
    klass = classes.find_one(tenant_filter({"_id": oid}))
    if not klass:
        flash("Class not found.", "error")
        abort(404)
    return klass


# =========================================================
# SUBJECTS
# =========================================================
@school_admin_bp.route("/subjects")
@school_required
def subjects_list():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "active").strip()

    q = {"status": status} if status else {}
    if search:
        q["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"code": {"$regex": search, "$options": "i"}},
        ]

    rows = list(subjects.find(tenant_filter(q)).sort("name", 1))

    all_class_ids = set()
    for r in rows:
        for cid in r.get("class_ids", []):
            all_class_ids.add(cid)
    class_map = {}
    if all_class_ids:
        for c in classes.find(tenant_filter({"_id": {"$in": list(all_class_ids)}})):
            class_map[c["_id"]] = c

    for r in rows:
        r["_classes"] = [class_map[cid] for cid in r.get("class_ids", []) if cid in class_map]

    stats = {
        "total":  subjects.count_documents(tenant_filter({})),
        "active": subjects.count_documents(tenant_filter({"status": "active"})),
        "core":   subjects.count_documents(tenant_filter({"is_core": True, "status": "active"})),
        "elective": subjects.count_documents(tenant_filter({"is_core": False, "status": "active"})),
    }

    return render_template(
        "school_admin/subjects.html",
        subjects=rows, stats=stats, search=search, status=status,
    )


@school_admin_bp.route("/subjects/new", methods=["GET", "POST"])
@school_required
def subject_new():
    if request.method == "POST":
        data, errors = _validate_subject_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/subject_form.html",
                form=request.form, mode="create",
                class_list=_classes_for_dropdown(),
            ), 400

        doc = {
            **data,
            "school_id": g.school["_id"],
            "teacher_ids": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = subjects.insert_one(doc)
        School.log(g.school["_id"], g.user["_id"], "subject.created", {
            "subject_id": str(result.inserted_id), "name": data["name"],
        })
        flash(f"Subject “{data['name']}” created.", "success")
        return redirect(url_for("school_admin.subjects_list"))

    return render_template(
        "school_admin/subject_form.html",
        form={}, mode="create",
        class_list=_classes_for_dropdown(),
    )


@school_admin_bp.route("/subjects/<subject_id>/edit", methods=["GET", "POST"])
@school_required
def subject_edit(subject_id):
    subject = _load_subject_or_404(subject_id)
    if request.method == "POST":
        data, errors = _validate_subject_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/subject_form.html",
                form=request.form, mode="edit", subject=subject,
                class_list=_classes_for_dropdown(),
            ), 400

        data["updated_at"] = datetime.utcnow()
        subjects.update_one(tenant_filter({"_id": subject["_id"]}), {"$set": data})
        School.log(g.school["_id"], g.user["_id"], "subject.updated", {
            "subject_id": str(subject["_id"]), "name": data["name"],
        })
        flash("Subject updated.", "success")
        return redirect(url_for("school_admin.subjects_list"))

    form = {
        "name":     subject.get("name", ""),
        "code":     subject.get("code", ""),
        "is_core":  subject.get("is_core", True),
        "status":   subject.get("status", "active"),
        "class_ids": [str(cid) for cid in subject.get("class_ids", [])],
    }
    return render_template(
        "school_admin/subject_form.html",
        form=form, mode="edit", subject=subject,
        class_list=_classes_for_dropdown(),
    )


@school_admin_bp.route("/subjects/<subject_id>/delete", methods=["POST"])
@school_required
def subject_delete(subject_id):
    subject = _load_subject_or_404(subject_id)
    subjects.update_one(tenant_filter({"_id": subject["_id"]}), {"$set": {
        "status": "archived", "archived_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }})
    School.log(g.school["_id"], g.user["_id"], "subject.archived", {
        "subject_id": str(subject["_id"]), "name": subject["name"],
    })
    flash(f"Subject “{subject['name']}” archived.", "success")
    return redirect(url_for("school_admin.subjects_list"))


def _validate_subject_form(form) -> tuple:
    def get(k): return (form.get(k) or "").strip()
    name = get("name")
    code = get("code").upper()
    is_core = form.get("is_core") == "on"
    status = get("status") or "active"
    class_ids_raw = form.getlist("class_ids") or []

    errors = []
    if not name: errors.append("Subject name is required.")
    existing = subjects.find_one(tenant_filter({"name": name}))
    current_id = form.get("_id")
    if existing and (not current_id or str(existing["_id"]) != current_id):
        errors.append(f"A subject named “{name}” already exists.")

    class_oids = []
    for cid in class_ids_raw:
        oid = _to_oid(cid)
        if oid:
            class_oids.append(oid)

    clean = {
        "name":      name,
        "code":      code or None,
        "is_core":   is_core,
        "status":    status,
        "class_ids": class_oids,
    }
    return clean, errors


def _load_subject_or_404(subject_id: str):
    oid = _to_oid(subject_id)
    if not oid:
        abort(404)
    subject = subjects.find_one(tenant_filter({"_id": oid}))
    if not subject:
        flash("Subject not found.", "error")
        abort(404)
    return subject


# =========================================================
# STAFF
# =========================================================
@school_admin_bp.route("/staff")
@school_required
def staff_list():
    search = request.args.get("q", "").strip()
    role_f = request.args.get("role", "").strip()
    status = request.args.get("status", "active").strip()

    q = {"status": status} if status else {}
    rows = list(staff.find(tenant_filter(q)).sort("staff_no", 1))

    user_ids = {r["user_id"] for r in rows if r.get("user_id")}
    user_map = {}
    if user_ids:
        for u in users.find({"_id": {"$in": list(user_ids)}}):
            user_map[u["_id"]] = u

    filtered = []
    for r in rows:
        u = user_map.get(r.get("user_id"))
        r["_user"] = u
        if not u: continue
        if role_f and u.get("role") != role_f: continue
        if search:
            hay = f"{u.get('name','')} {u.get('email','')} {r.get('staff_no','')}".lower()
            if search.lower() not in hay: continue
        filtered.append(r)

    stats = {
        "total":    staff.count_documents(tenant_filter({})),
        "active":   staff.count_documents(tenant_filter({"status": "active"})),
        "teachers": users.count_documents(tenant_filter({"role": "teacher"})),
        "on_leave": staff.count_documents(tenant_filter({"status": "on_leave"})),
    }

    return render_template(
        "school_admin/staff.html",
        staff=filtered, stats=stats,
        search=search, role_f=role_f, status=status,
    )


@school_admin_bp.route("/staff/new", methods=["GET", "POST"])
@school_required
def staff_new():
    if request.method == "POST":
        data, errors = _validate_staff_form(request.form, mode="create")
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/staff_form.html",
                form=request.form, mode="create",
                subjects_list=_subjects_for_dropdown(),
            ), 400

        now = datetime.utcnow()
        user_doc = {
            "school_id": g.school["_id"],
            "name": data["name"], "email": data["email"],
            "password_hash": data["password_hash"],
            "role": data["role"], "phone": data.get("phone"),
            "email_verified": False, "linked_children": [],
            "created_at": now, "last_login": None,
        }
        user_id = users.insert_one(user_doc).inserted_id

        staff_doc = {
            "school_id": g.school["_id"],
            "user_id": user_id,
            "staff_no": data["staff_no"],
            "designation": data["designation"],
            "department": data.get("department"),
            "subjects": data.get("subjects", []),
            "classes": data.get("classes", []),
            "employment_date": data.get("employment_date"),
            "salary": data.get("salary"),
            "status": "active",
            "created_at": now, "updated_at": now,
        }
        staff_id = staff.insert_one(staff_doc).inserted_id

        School.log(g.school["_id"], g.user["_id"], "staff.created", {
            "staff_id": str(staff_id), "user_id": str(user_id),
            "name": data["name"], "staff_no": data["staff_no"],
        })

        if request.form.get("send_invite") == "on":
            try:
                send_school_invite_email(g.school["name"], data["email"], data["name"])
            except Exception:
                pass

        flash(f"{data['name']} added to staff.", "success")
        return redirect(url_for("school_admin.staff_detail", staff_id=str(staff_id)))

    return render_template(
        "school_admin/staff_form.html",
        form={"staff_no": _generate_staff_no()},
        mode="create",
        subjects_list=_subjects_for_dropdown(),
    )


@school_admin_bp.route("/staff/<staff_id>")
@school_required
def staff_detail(staff_id):
    member = _load_staff_or_404(staff_id)
    u = users.find_one({"_id": member.get("user_id")}) if member.get("user_id") else None

    subject_names = []
    if member.get("subjects"):
        for s in subjects.find(tenant_filter({"_id": {"$in": member["subjects"]}})):
            subject_names.append(s.get("name"))

    class_names = []
    if member.get("classes"):
        for c in classes.find(tenant_filter({"_id": {"$in": member["classes"]}})):
            class_names.append(c.get("name"))

    timetable_slots = list(
        timetable_col.find(tenant_filter({"teacher_id": member["_id"]}))
        .sort([("day", 1), ("start", 1)])
    )

    activity = list(
        audit_logs.find(tenant_filter({"meta.staff_id": str(member["_id"])}))
        .sort("timestamp", -1).limit(15)
    )

    return render_template(
        "school_admin/staff_detail.html",
        member=member, user=u,
        subject_names=subject_names, class_names=class_names,
        timetable_slots=timetable_slots,
        activity=activity,
    )


@school_admin_bp.route("/staff/<staff_id>/edit", methods=["GET", "POST"])
@school_required
def staff_edit(staff_id):
    member = _load_staff_or_404(staff_id)
    u = users.find_one({"_id": member.get("user_id")}) if member.get("user_id") else None

    if request.method == "POST":
        data, errors = _validate_staff_form(request.form, mode="edit", member=member, user=u)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/staff_form.html",
                form=request.form, mode="edit",
                member=member, user=u,
                subjects_list=_subjects_for_dropdown(),
            ), 400

        now = datetime.utcnow()
        user_update = {
            "name": data["name"], "email": data["email"],
            "phone": data.get("phone"), "role": data["role"],
        }
        if data.get("password_hash"):
            user_update["password_hash"] = data["password_hash"]
        users.update_one({"_id": member["user_id"]}, {"$set": user_update})

        staff_update = {
            "staff_no": data["staff_no"],
            "designation": data["designation"],
            "department": data.get("department"),
            "subjects": data.get("subjects", []),
            "classes": data.get("classes", []),
            "employment_date": data.get("employment_date"),
            "salary": data.get("salary"),
            "status": data.get("status", "active"),
            "updated_at": now,
        }
        staff.update_one(tenant_filter({"_id": member["_id"]}), {"$set": staff_update})

        School.log(g.school["_id"], g.user["_id"], "staff.updated", {
            "staff_id": str(member["_id"]), "name": data["name"],
        })
        flash("Staff profile updated.", "success")
        return redirect(url_for("school_admin.staff_detail", staff_id=staff_id))

    form = {
        "name":        (u or {}).get("name", ""),
        "email":       (u or {}).get("email", ""),
        "phone":       (u or {}).get("phone", ""),
        "role":        (u or {}).get("role", "teacher"),
        "staff_no":    member.get("staff_no", ""),
        "designation": member.get("designation", ""),
        "department":  member.get("department", ""),
        "employment_date": member["employment_date"].strftime("%Y-%m-%d") if member.get("employment_date") else "",
        "salary":      member.get("salary", ""),
        "status":      member.get("status", "active"),
        "subjects":    [str(s) for s in member.get("subjects", [])],
        "classes":     [str(c) for c in member.get("classes", [])],
    }
    return render_template(
        "school_admin/staff_form.html",
        form=form, mode="edit", member=member, user=u,
        subjects_list=_subjects_for_dropdown(),
    )


@school_admin_bp.route("/staff/<staff_id>/delete", methods=["POST"])
@school_required
def staff_delete(staff_id):
    member = _load_staff_or_404(staff_id)
    u = users.find_one({"_id": member.get("user_id")}) if member.get("user_id") else None
    name = (u or {}).get("name") or member.get("staff_no") or "Staff"

    staff.update_one(tenant_filter({"_id": member["_id"]}), {"$set": {
        "status": "resigned", "archived_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }})
    if member.get("user_id"):
        users.update_one({"_id": member["user_id"]}, {"$set": {
            "deactivated_at": datetime.utcnow(),
        }})

    School.log(g.school["_id"], g.user["_id"], "staff.archived", {
        "staff_id": str(member["_id"]), "name": name,
    })
    flash(f"{name} removed from active staff.", "success")
    return redirect(url_for("school_admin.staff_list"))


def _subjects_for_dropdown():
    rows = list(subjects.find(tenant_filter({"status": "active"})).sort("name", 1))
    return [{"id": str(s["_id"]), "name": s["name"]} for s in rows]


def _generate_staff_no() -> str:
    year = datetime.utcnow().year
    prefix = f"EMP-{year}-"
    last = staff.find_one(
        tenant_filter({"staff_no": {"$regex": f"^{prefix}"}}),
        sort=[("staff_no", -1)],
    )
    if last and last.get("staff_no"):
        try:
            n = int(last["staff_no"].rsplit("-", 1)[-1]) + 1
        except Exception:
            n = 1
    else:
        n = 1
    return f"{prefix}{n:04d}"


def _validate_staff_form(form, mode="create", member=None, user=None):
    from werkzeug.security import generate_password_hash

    def get(k): return (form.get(k) or "").strip()
    name        = get("name")
    email       = get("email").lower()
    phone       = get("phone")
    role        = get("role") or "teacher"
    password    = form.get("password") or ""
    staff_no    = get("staff_no")
    designation = get("designation")
    department  = get("department")
    employment_date_str = get("employment_date")
    salary_str  = get("salary")
    status      = get("status") or "active"
    subject_ids_raw = form.getlist("subjects") or []
    class_ids_raw   = form.getlist("classes") or []

    errors = []
    if not name:  errors.append("Full name is required.")
    if not email: errors.append("Email is required.")
    if not staff_no: errors.append("Staff number is required.")
    if role not in ("school_admin", "teacher", "accountant"):
        errors.append("Invalid role.")

    if email:
        existing_user = User.find_by_email(email)
        if existing_user and (not user or str(existing_user["_id"]) != str(user["_id"])):
            errors.append(f"An account with email {email} already exists.")

    pwd_hash = None
    if mode == "create":
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        else:
            pwd_hash = generate_password_hash(password)
    elif password:
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        else:
            pwd_hash = generate_password_hash(password)

    employment_date = None
    if employment_date_str:
        try:
            employment_date = datetime.strptime(employment_date_str, "%Y-%m-%d")
        except ValueError:
            errors.append("Employment date must be YYYY-MM-DD.")

    salary = None
    if salary_str:
        try:
            salary = float(salary_str)
            if salary < 0:
                errors.append("Salary must be positive.")
        except ValueError:
            errors.append("Salary must be a number.")

    subject_ids = [_to_oid(i) for i in subject_ids_raw if _to_oid(i)]
    class_ids   = [_to_oid(i) for i in class_ids_raw if _to_oid(i)]

    clean = {
        "name":  name, "email": email,
        "phone": phone or None, "role": role,
        "password_hash": pwd_hash,
        "staff_no":    staff_no,
        "designation": designation or None,
        "department":  department or None,
        "subjects":    subject_ids,
        "classes":     class_ids,
        "employment_date": employment_date,
        "salary":      salary,
        "status":      status,
    }
    return clean, errors


def _load_staff_or_404(staff_id: str):
    oid = _to_oid(staff_id)
    if not oid:
        abort(404)
    member = staff.find_one(tenant_filter({"_id": oid}))
    if not member:
        flash("Staff member not found.", "error")
        abort(404)
    return member


# =========================================================
# ATTENDANCE
# =========================================================
@school_admin_bp.route("/attendance")
@school_required
def attendance_page():
    date_str = request.args.get("date", "").strip()
    class_id = request.args.get("class_id", "").strip()

    chosen_date = _parse_date(date_str, _day(datetime.utcnow()))
    today = _day(datetime.utcnow())

    class_list = list(
        classes.find(tenant_filter({"status": "active"}))
        .sort([("level", 1), ("name", 1)])
    )
    if not class_id and class_list:
        class_id = str(class_list[0]["_id"])

    selected_class = None
    students_rows = []
    attendance_doc = None
    summary = {"present": 0, "absent": 0, "late": 0, "excused": 0, "total": 0}

    if class_id:
        oid = _to_oid(class_id)
        if oid:
            selected_class = classes.find_one(tenant_filter({"_id": oid}))

        if selected_class:
            students_rows = list(
                students.find(tenant_filter({
                    "class_id": selected_class["_id"],
                    "status": "active",
                })).sort([("last_name", 1), ("first_name", 1)])
            )

            attendance_doc = attendance.find_one(tenant_filter({
                "class_id": selected_class["_id"],
                "date": chosen_date,
            }))

            existing_map = {}
            if attendance_doc:
                for rec in attendance_doc.get("records", []):
                    existing_map[rec["student_id"]] = rec.get("status", "present")

            for s in students_rows:
                s["_status"] = existing_map.get(s["_id"], None)

            for s in students_rows:
                st = s.get("_status")
                if st in summary:
                    summary[st] += 1
                summary["total"] += 1

    return render_template(
        "school_admin/attendance.html",
        class_list=class_list,
        selected_class=selected_class,
        selected_class_id=str(selected_class["_id"]) if selected_class else "",
        chosen_date=chosen_date,
        date_str=chosen_date.strftime("%Y-%m-%d"),
        yesterday_str=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
        today=today,
        is_today=(chosen_date == today),
        students=students_rows,
        attendance_doc=attendance_doc,
        summary=summary,
    )


@school_admin_bp.route("/attendance/save", methods=["POST"])
@school_required
def attendance_save():
    class_id = (request.form.get("class_id") or "").strip()
    date_str = (request.form.get("date") or "").strip()
    chosen_date = _parse_date(date_str, _day(datetime.utcnow()))

    klass = _load_class_or_404(class_id)

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
        return redirect(url_for(
            "school_admin.attendance_page",
            class_id=class_id, date=date_str,
        ))

    now = datetime.utcnow()
    existing = attendance.find_one(tenant_filter({
        "class_id": klass["_id"],
        "date": chosen_date,
    }))

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
    return redirect(url_for(
        "school_admin.attendance_page",
        class_id=class_id, date=date_str,
    ))


@school_admin_bp.route("/attendance/history")
@school_required
def attendance_history():
    class_id = request.args.get("class_id", "").strip()
    from_str = request.args.get("from", "").strip()
    to_str   = request.args.get("to", "").strip()

    to_d   = _parse_date(to_str, _day(datetime.utcnow()))
    from_d = _parse_date(from_str, _day(to_d - timedelta(days=30)))

    q = {"date": {"$gte": from_d, "$lte": to_d}}
    if class_id:
        oid = _to_oid(class_id)
        if oid:
            q["class_id"] = oid

    docs = list(attendance.find(tenant_filter(q)).sort("date", -1).limit(200))

    class_ids = {d["class_id"] for d in docs}
    class_map = {}
    if class_ids:
        for c in classes.find(tenant_filter({"_id": {"$in": list(class_ids)}})):
            class_map[c["_id"]] = c

    for d in docs:
        d["_class"] = class_map.get(d["class_id"])
        d["_summary"] = {
            "present": sum(1 for r in d.get("records", []) if r["status"] == "present"),
            "absent":  sum(1 for r in d.get("records", []) if r["status"] == "absent"),
            "late":    sum(1 for r in d.get("records", []) if r["status"] == "late"),
            "excused": sum(1 for r in d.get("records", []) if r["status"] == "excused"),
        }
        total = sum(d["_summary"].values())
        d["_summary"]["total"] = total
        d["_summary"]["pct"] = round(
            ((d["_summary"]["present"] + d["_summary"]["late"]) / total) * 100
        ) if total else 0

    total_records = present = absent = late = excused = 0
    for d in docs:
        for rec in d.get("records", []):
            total_records += 1
            if rec["status"] == "present": present += 1
            elif rec["status"] == "absent": absent += 1
            elif rec["status"] == "late": late += 1
            elif rec["status"] == "excused": excused += 1

    overall_pct = round(((present + late) / total_records) * 100) if total_records else 0

    class_list = list(classes.find(tenant_filter({"status": "active"})).sort("name", 1))

    return render_template(
        "school_admin/attendance_history.html",
        records=docs,
        class_list=class_list,
        class_id=class_id,
        from_date=from_d,
        to_date=to_d,
        stats={
            "sessions": len(docs),
            "total":    total_records,
            "present":  present,
            "absent":   absent,
            "late":     late,
            "excused":  excused,
            "pct":      overall_pct,
        },
    )


# =========================================================
# TIMETABLE
# =========================================================
@school_admin_bp.route("/timetable")
@school_required
def timetable():
    class_id = request.args.get("class_id", "").strip()

    class_list = list(
        classes.find(tenant_filter({"status": "active"}))
        .sort([("level", 1), ("name", 1)])
    )
    if not class_id and class_list:
        class_id = str(class_list[0]["_id"])

    selected_class = None
    slots = []
    grid = {day: [] for day in WEEKDAYS}

    if class_id:
        oid = _to_oid(class_id)
        if oid:
            selected_class = classes.find_one(tenant_filter({"_id": oid}))

        if selected_class:
            slots = list(
                timetable_col.find(tenant_filter({"class_id": selected_class["_id"]}))
                .sort([("day", 1), ("start", 1)])
            )

            subj_ids = {s["subject_id"] for s in slots if s.get("subject_id")}
            teacher_ids = {s["teacher_id"] for s in slots if s.get("teacher_id")}
            subj_map = {}
            teacher_map = {}
            if subj_ids:
                for s in subjects.find(tenant_filter({"_id": {"$in": list(subj_ids)}})):
                    subj_map[s["_id"]] = s
            if teacher_ids:
                for u in users.find({"_id": {"$in": list(teacher_ids)}}):
                    teacher_map[u["_id"]] = u

            for s in slots:
                s["_subject"] = subj_map.get(s["subject_id"])
                s["_teacher"] = teacher_map.get(s["teacher_id"])
                grid[s["day"]].append(s)

    subject_list = list(
        subjects.find(tenant_filter({"status": "active"})).sort("name", 1)
    )
    teacher_list = list(
        users.find({
            "school_id": g.school["_id"],
            "role": {"$in": ["teacher", "school_admin"]},
        }).sort("name", 1)
    )

    return render_template(
        "school_admin/timetable.html",
        class_list=class_list,
        selected_class=selected_class,
        selected_class_id=str(selected_class["_id"]) if selected_class else "",
        slots=slots,
        grid=grid,
        weekdays=WEEKDAYS,
        weekday_labels=WEEKDAY_LABELS,
        subject_list=subject_list,
        teacher_list=teacher_list,
    )


@school_admin_bp.route("/timetable/new", methods=["POST"])
@school_required
def timetable_new():
    class_id   = (request.form.get("class_id") or "").strip()
    subject_id = (request.form.get("subject_id") or "").strip()
    teacher_id = (request.form.get("teacher_id") or "").strip()
    day        = (request.form.get("day") or "").strip().lower()
    start      = (request.form.get("start") or "").strip()
    end        = (request.form.get("end") or "").strip()
    room       = (request.form.get("room") or "").strip()

    klass = _load_class_or_404(class_id)

    errors = []
    if day not in WEEKDAYS: errors.append("Invalid day.")
    if not _valid_time(start) or not _valid_time(end):
        errors.append("Start and end times must be HH:MM.")
    if start and end and start >= end:
        errors.append("Start time must be before end time.")

    subject_oid = _to_oid(subject_id)
    teacher_oid = _to_oid(teacher_id)

    if not errors:
        overlap = _find_overlap(klass["_id"], day, start, end)
        if overlap:
            errors.append(
                f"Time conflict with {overlap.get('start')}–{overlap.get('end')} "
                f"on {day.capitalize()}."
            )

    if errors:
        for e in errors: flash(e, "error")
        return redirect(url_for("school_admin.timetable", class_id=class_id))

    now = datetime.utcnow()
    timetable_col.insert_one({
        "school_id": g.school["_id"],
        "class_id": klass["_id"],
        "subject_id": subject_oid,
        "teacher_id": teacher_oid,
        "day": day,
        "start": start,
        "end": end,
        "room": room or None,
        "created_at": now,
        "updated_at": now,
    })

    School.log(g.school["_id"], g.user["_id"], "timetable.slot_added", {
        "class_id": str(klass["_id"]),
        "class_name": klass["name"],
        "day": day,
        "start": start,
        "end": end,
    })

    flash("Slot added to timetable.", "success")
    return redirect(url_for("school_admin.timetable", class_id=class_id))


@school_admin_bp.route("/timetable/<slot_id>/delete", methods=["POST"])
@school_required
def timetable_delete(slot_id):
    oid = _to_oid(slot_id)
    if not oid:
        abort(404)
    slot = timetable_col.find_one(tenant_filter({"_id": oid}))
    if not slot:
        flash("Slot not found.", "error")
        return redirect(url_for("school_admin.timetable"))

    timetable_col.delete_one({"_id": slot["_id"]})
    School.log(g.school["_id"], g.user["_id"], "timetable.slot_removed", {
        "class_id": str(slot["class_id"]),
        "day": slot.get("day"),
    })
    flash("Slot removed.", "success")
    return redirect(url_for("school_admin.timetable", class_id=str(slot["class_id"])))


# =========================================================
# EXAMS & GRADES
# =========================================================
@school_admin_bp.route("/exams")
@school_required
def exams_page():
    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    class_rows = list(
        classes.find(tenant_filter({"status": "active"}))
        .sort([("level", 1), ("name", 1)])
    )
    subject_rows = list(
        subjects.find(tenant_filter({"status": "active"}))
        .sort("name", 1)
    )

    matrix = []
    for c in class_rows:
        student_count = students.count_documents(tenant_filter({
            "class_id": c["_id"], "status": "active",
        }))

        class_subjects = [
            s for s in subject_rows
            if c["_id"] in s.get("class_ids", [])
        ] or subject_rows

        row = {"klass": c, "student_count": student_count, "cells": []}
        for s in class_subjects:
            entered = grades.count_documents(tenant_filter({
                "class_id": c["_id"],
                "subject_id": s["_id"],
                "term": term,
                "academic_year": ay,
            }))
            row["cells"].append({
                "subject": s,
                "entered": entered,
                "total":   student_count,
                "pct":     round((entered / student_count) * 100) if student_count else 0,
                "complete": entered >= student_count and student_count > 0,
            })
        matrix.append(row)

    stats = {
        "total_grades": grades.count_documents(tenant_filter({
            "term": term, "academic_year": ay,
        })),
        "classes":    len(class_rows),
        "subjects":   len(subject_rows),
    }

    return render_template(
        "school_admin/grades_overview.html",
        matrix=matrix,
        stats=stats,
        terms=TERMS,
        term=term,
        academic_year=ay,
    )


@school_admin_bp.route("/exams/enter")
@school_required
def grades_entry():
    class_id   = request.args.get("class_id", "").strip()
    subject_id = request.args.get("subject_id", "").strip()
    term       = request.args.get("term", _current_term())
    ay         = request.args.get("academic_year", _current_academic_year())

    class_list = list(
        classes.find(tenant_filter({"status": "active"}))
        .sort([("level", 1), ("name", 1)])
    )
    if not class_id and class_list:
        class_id = str(class_list[0]["_id"])

    subject_list = list(
        subjects.find(tenant_filter({"status": "active"}))
        .sort("name", 1)
    )

    selected_class = None
    selected_subject = None
    rows = []

    if class_id and subject_id:
        c_oid = _to_oid(class_id)
        s_oid = _to_oid(subject_id)
        if c_oid and s_oid:
            selected_class   = classes.find_one(tenant_filter({"_id": c_oid}))
            selected_subject = subjects.find_one(tenant_filter({"_id": s_oid}))

    if selected_class and selected_subject:
        students_rows = list(
            students.find(tenant_filter({
                "class_id": selected_class["_id"],
                "status": "active",
            })).sort([("last_name", 1), ("first_name", 1)])
        )

        existing_grades = list(grades.find(tenant_filter({
            "class_id":   selected_class["_id"],
            "subject_id": selected_subject["_id"],
            "term":       term,
            "academic_year": ay,
        })))
        grade_map = {g["student_id"]: g for g in existing_grades}

        for s in students_rows:
            g = grade_map.get(s["_id"], {})
            rows.append({
                "student": s,
                "ca":   g.get("ca_score", ""),
                "exam": g.get("exam_score", ""),
                "total": g.get("total"),
                "grade": g.get("grade"),
                "remark": g.get("remark"),
            })

    return render_template(
        "school_admin/grades.html",
        class_list=class_list,
        subject_list=subject_list,
        selected_class=selected_class,
        selected_subject=selected_subject,
        selected_class_id=class_id,
        selected_subject_id=subject_id,
        rows=rows,
        term=term,
        academic_year=ay,
        terms=TERMS,
        ca_max=CA_MAX,
        exam_max=EXAM_MAX,
    )


@school_admin_bp.route("/exams/enter", methods=["POST"])
@school_required
def grades_save():
    class_id   = (request.form.get("class_id") or "").strip()
    subject_id = (request.form.get("subject_id") or "").strip()
    term       = (request.form.get("term") or _current_term()).strip()
    ay         = (request.form.get("academic_year") or _current_academic_year()).strip()

    klass   = _load_class_or_404(class_id)
    subject = _load_subject_or_404(subject_id)

    now = datetime.utcnow()
    saved = 0
    cleared = 0

    student_rows = list(students.find(tenant_filter({
        "class_id": klass["_id"], "status": "active",
    })))

    for s in student_rows:
        sid = str(s["_id"])
        ca_raw   = request.form.get(f"ca_{sid}")
        exam_raw = request.form.get(f"exam_{sid}")

        if (ca_raw or "").strip() == "" and (exam_raw or "").strip() == "":
            r = grades.delete_one(tenant_filter({
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
            tenant_filter({
                "student_id": s["_id"],
                "subject_id": subject["_id"],
                "term": term,
                "academic_year": ay,
            }),
            {
                "$set": {
                    "class_id":    klass["_id"],
                    "ca_score":    ca,
                    "exam_score":  exam,
                    "total":       total,
                    "grade":       letter,
                    "remark":      remark,
                    "entered_by":  g.user["_id"],
                    "updated_at":  now,
                },
                "$setOnInsert": {
                    "school_id":  g.school["_id"],
                    "student_id": s["_id"],
                    "subject_id": subject["_id"],
                    "term":       term,
                    "academic_year": ay,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        saved += 1

    School.log(g.school["_id"], g.user["_id"], "grades.saved", {
        "class_id":     str(klass["_id"]),
        "class_name":   klass["name"],
        "subject_id":   str(subject["_id"]),
        "subject_name": subject["name"],
        "term":         term,
        "academic_year": ay,
        "saved":        saved,
        "cleared":      cleared,
    })

    flash(f"Grades saved for {klass['name']} · {subject['name']} · {saved} entries.", "success")
    return redirect(url_for(
        "school_admin.grades_entry",
        class_id=class_id, subject_id=subject_id,
        term=term, academic_year=ay,
    ))


@school_admin_bp.route("/students/<student_id>/report-card")
@school_required
def student_report_card(student_id):
    student = _load_student_or_404(student_id)
    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    klass = None
    if student.get("class_id"):
        klass = classes.find_one(tenant_filter({"_id": student["class_id"]}))

    rows = list(grades.find(tenant_filter({
        "student_id":   student["_id"],
        "term":         term,
        "academic_year": ay,
    })))

    subj_ids = [r["subject_id"] for r in rows]
    subj_map = {}
    if subj_ids:
        for s in subjects.find(tenant_filter({"_id": {"$in": subj_ids}})):
            subj_map[s["_id"]] = s

    report_rows = []
    total_sum = 0
    for r in rows:
        subj = subj_map.get(r["subject_id"])
        if not subj:
            continue
        report_rows.append({
            "subject": subj["name"],
            "code":    subj.get("code", ""),
            "ca":      r.get("ca_score", 0),
            "exam":    r.get("exam_score", 0),
            "total":   r.get("total", 0),
            "grade":   r.get("grade", "—"),
            "remark":  r.get("remark", ""),
        })
        total_sum += r.get("total", 0)

    report_rows.sort(key=lambda x: x["subject"])

    average = round(total_sum / len(report_rows)) if report_rows else 0
    overall_grade, overall_remark = _grade_for(average)

    class_rank = None
    class_size = None
    if klass:
        pipeline = [
            {"$match": tenant_filter({
                "class_id": klass["_id"],
                "term":     term,
                "academic_year": ay,
            })},
            {"$group": {
                "_id": "$student_id",
                "avg": {"$avg": "$total"},
            }},
            {"$sort": {"avg": -1}},
        ]
        ranked = list(grades.aggregate(pipeline))
        class_size = len(ranked)
        for i, r in enumerate(ranked):
            if r["_id"] == student["_id"]:
                class_rank = i + 1
                break

    att_counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    if klass:
        att_docs = list(attendance.find(tenant_filter({
            "class_id": klass["_id"],
        })))
        for doc in att_docs:
            for rec in doc.get("records", []):
                if rec.get("student_id") == student["_id"]:
                    att_counts[rec.get("status", "present")] = \
                        att_counts.get(rec.get("status", "present"), 0) + 1
    att_total = sum(att_counts.values())
    att_pct = round(
        ((att_counts["present"] + att_counts["late"]) / att_total) * 100
    ) if att_total else 0

    return render_template(
        "school_admin/report_card.html",
        student=student,
        klass=klass,
        rows=report_rows,
        term=term,
        term_label=dict(TERMS).get(term, term.title()),
        academic_year=ay,
        average=average,
        overall_grade=overall_grade,
        overall_remark=overall_remark,
        class_rank=class_rank,
        class_size=class_size,
        attendance=att_counts,
        attendance_pct=att_pct,
        terms=TERMS,
        is_print=request.args.get("print") == "1",
    )


@school_admin_bp.route("/students/<student_id>/grades")
@school_required
def student_grades_detail(student_id):
    return redirect(url_for(
        "school_admin.student_detail",
        student_id=student_id,
        tab="grades",
    ))


# =========================================================
# FEES & INVOICES
# =========================================================
def _generate_invoice_no() -> str:
    year = datetime.utcnow().year
    prefix = f"INV-{year}-"
    last = invoices.find_one(
        tenant_filter({"invoice_no": {"$regex": f"^{prefix}"}}),
        sort=[("invoice_no", -1)],
    )
    if last and last.get("invoice_no"):
        try:
            n = int(last["invoice_no"].rsplit("-", 1)[-1]) + 1
        except Exception:
            n = 1
    else:
        n = 1
    return f"{prefix}{n:04d}"


def _recompute_invoice(invoice_id, school_id=None) -> dict:
    """
    Recompute a single invoice's paid/balance/status.

    - Inside a request: uses `g.school["_id"]` if school_id isn't given.
    - Outside a request (e.g. a webhook): caller MUST pass school_id.
    """
    if school_id is None:
        try:
            school_id = g.school["_id"]
        except Exception:
            # No request context — caller must pass school_id.
            return {}

    oid = _to_oid(invoice_id)
    if not oid:
        return {}

    inv = invoices.find_one({"_id": oid, "school_id": school_id})
    if not inv:
        return {}

    pipeline = [
        {"$match": {"invoice_id": oid, "school_id": school_id}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    res = list(payments.aggregate(pipeline))
    paid = res[0]["total"] if res else 0

    due     = inv.get("amount_due", 0) or 0
    balance = max(due - paid, 0)

    if inv.get("status") == "waived":
        status = "waived"
        balance = 0
    elif due > 0 and paid >= due:
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
    inv["amount_paid"] = paid
    inv["balance"]     = balance
    inv["status"]      = status
    return inv

def _student_fees_detail(student_id) -> dict:
    invs = list(
        invoices.find(tenant_filter({"student_id": student_id}))
        .sort("created_at", -1)
    )

    inv_ids = [i["_id"] for i in invs]
    payments_by_invoice = {}
    if inv_ids:
        for p in payments.find(tenant_filter({"invoice_id": {"$in": inv_ids}})).sort("paid_at", -1):
            payments_by_invoice.setdefault(p["invoice_id"], []).append(p)

    for i in invs:
        i["_payments"] = payments_by_invoice.get(i["_id"], [])

    total_due     = sum(i.get("amount_due", 0) for i in invs)
    total_paid    = sum(i.get("amount_paid", 0) for i in invs)
    total_balance = sum(i.get("balance", 0) for i in invs)

    return {
        "invoices":      invs,
        "total_due":     total_due,
        "total_paid":    total_paid,
        "total_balance": total_balance,
        "payment_count": sum(len(v) for v in payments_by_invoice.values()),
    }


@school_admin_bp.route("/fees")
@school_required
def fees_page():
    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    structures = list(
        fee_structures.find(tenant_filter({
            "term": term, "academic_year": ay, "status": "active",
        })).sort("created_at", -1)
    )

    class_ids = {s["class_id"] for s in structures if s.get("class_id")}
    class_map = {}
    if class_ids:
        for c in classes.find(tenant_filter({"_id": {"$in": list(class_ids)}})):
            class_map[c["_id"]] = c

    for s in structures:
        s["_class"] = class_map.get(s.get("class_id"))
        s["_invoice_count"] = invoices.count_documents(tenant_filter({
            "fee_structure_id": s["_id"],
        }))
        s["_paid_count"] = invoices.count_documents(tenant_filter({
            "fee_structure_id": s["_id"], "status": "paid",
        }))
        s["_collected"] = sum(
            inv.get("amount_paid", 0) for inv in invoices.find(tenant_filter({
                "fee_structure_id": s["_id"],
            }))
        )

    recent_payments = list(
        payments.find(tenant_filter({})).sort("paid_at", -1).limit(8)
    )
    student_ids = {p["student_id"] for p in recent_payments}
    student_map = {}
    if student_ids:
        for s in students.find(tenant_filter({"_id": {"$in": list(student_ids)}})):
            student_map[s["_id"]] = s
    for p in recent_payments:
        p["_student"] = student_map.get(p["student_id"])

    total_expected  = sum(s.get("total", 0) * s["_invoice_count"] for s in structures)
    total_collected = sum(s["_collected"] for s in structures)

    stats = {
        "structures":       len(structures),
        "total_invoiced":   invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay,
        })),
        "total_paid":       invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay, "status": "paid",
        })),
        "total_unpaid":     invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay,
            "status": {"$in": ["unpaid", "partial"]},
        })),
        "expected":         total_expected,
        "collected":        total_collected,
        "collection_pct":   round((total_collected / total_expected) * 100) if total_expected else 0,
    }

    return render_template(
        "school_admin/fees_overview.html",
        structures=structures,
        recent_payments=recent_payments,
        stats=stats,
        term=term,
        academic_year=ay,
        terms=TERMS,
    )


@school_admin_bp.route("/fees/structures/new", methods=["GET", "POST"])
@school_required
def fee_structure_new():
    if request.method == "POST":
        data, errors = _validate_fee_structure_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/fee_structure_form.html",
                form=request.form, mode="create",
                class_list=_classes_for_dropdown(),
                terms=TERMS,
            ), 400

        now = datetime.utcnow()
        doc = {
            **data,
            "school_id": g.school["_id"],
            "status": "active",
            "currency": Config.CURRENCY,
            "created_at": now,
            "updated_at": now,
        }
        result = fee_structures.insert_one(doc)
        School.log(g.school["_id"], g.user["_id"], "fee_structure.created", {
            "fee_structure_id": str(result.inserted_id),
            "name": data["name"],
            "total": data["total"],
        })
        flash(f"Fee structure “{data['name']}” created.", "success")
        return redirect(url_for("school_admin.fee_structure_detail",
                                structure_id=str(result.inserted_id)))

    return render_template(
        "school_admin/fee_structure_form.html",
        form={"term": _current_term(), "academic_year": _current_academic_year()},
        mode="create",
        class_list=_classes_for_dropdown(),
        terms=TERMS,
    )


@school_admin_bp.route("/fees/structures/<structure_id>")
@school_required
def fee_structure_detail(structure_id):
    structure = _load_fee_structure_or_404(structure_id)

    klass = None
    if structure.get("class_id"):
        klass = classes.find_one(tenant_filter({"_id": structure["class_id"]}))

    invs = list(
        invoices.find(tenant_filter({"fee_structure_id": structure["_id"]}))
        .sort("created_at", -1).limit(100)
    )
    student_ids = {i["student_id"] for i in invs}
    student_map = {}
    if student_ids:
        for s in students.find(tenant_filter({"_id": {"$in": list(student_ids)}})):
            student_map[s["_id"]] = s
    for i in invs:
        i["_student"] = student_map.get(i["student_id"])

    total_due     = sum(i.get("amount_due", 0) for i in invs)
    total_paid    = sum(i.get("amount_paid", 0) for i in invs)
    total_balance = sum(i.get("balance", 0) for i in invs)

    stats = {
        "invoices":  len(invs),
        "paid":      sum(1 for i in invs if i["status"] == "paid"),
        "partial":   sum(1 for i in invs if i["status"] == "partial"),
        "unpaid":    sum(1 for i in invs if i["status"] == "unpaid"),
        "due":       total_due,
        "paid_amt":  total_paid,
        "balance":   total_balance,
        "pct":       round((total_paid / total_due) * 100) if total_due else 0,
    }

    if klass:
        eligible_count = students.count_documents(tenant_filter({
            "class_id": klass["_id"], "status": "active",
        }))
    else:
        eligible_count = students.count_documents(tenant_filter({
            "status": "active",
        }))

    return render_template(
        "school_admin/fee_structure_detail.html",
        structure=structure,
        klass=klass,
        invoices=invs,
        stats=stats,
        eligible_count=eligible_count,
        terms=TERMS,
    )


@school_admin_bp.route("/fees/structures/<structure_id>/edit", methods=["GET", "POST"])
@school_required
def fee_structure_edit(structure_id):
    structure = _load_fee_structure_or_404(structure_id)

    if request.method == "POST":
        data, errors = _validate_fee_structure_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/fee_structure_form.html",
                form=request.form, mode="edit",
                structure=structure,
                class_list=_classes_for_dropdown(),
                terms=TERMS,
            ), 400

        data["updated_at"] = datetime.utcnow()
        fee_structures.update_one(
            tenant_filter({"_id": structure["_id"]}),
            {"$set": data},
        )
        School.log(g.school["_id"], g.user["_id"], "fee_structure.updated", {
            "fee_structure_id": str(structure["_id"]),
            "name": data["name"],
        })
        flash("Fee structure updated.", "success")
        return redirect(url_for("school_admin.fee_structure_detail",
                                structure_id=structure_id))

    form = {
        "name":          structure.get("name", ""),
        "class_id":      str(structure["class_id"]) if structure.get("class_id") else "",
        "term":          structure.get("term", _current_term()),
        "academic_year": structure.get("academic_year", _current_academic_year()),
        "due_date":      structure["due_date"].strftime("%Y-%m-%d") if structure.get("due_date") else "",
        "items":         structure.get("items", []),
        "total":         structure.get("total", 0),
    }
    return render_template(
        "school_admin/fee_structure_form.html",
        form=form, mode="edit", structure=structure,
        class_list=_classes_for_dropdown(),
        terms=TERMS,
    )


@school_admin_bp.route("/fees/structures/<structure_id>/delete", methods=["POST"])
@school_required
def fee_structure_delete(structure_id):
    structure = _load_fee_structure_or_404(structure_id)

    paid_count = invoices.count_documents(tenant_filter({
        "fee_structure_id": structure["_id"],
        "amount_paid": {"$gt": 0},
    }))
    if paid_count:
        flash(f"Can't delete — {paid_count} invoice(s) already have payments. "
              f"Archive instead.", "error")
        return redirect(url_for("school_admin.fee_structure_detail",
                                structure_id=structure_id))

    fee_structures.update_one(
        tenant_filter({"_id": structure["_id"]}),
        {"$set": {
            "status": "archived",
            "archived_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }},
    )
    School.log(g.school["_id"], g.user["_id"], "fee_structure.archived", {
        "fee_structure_id": str(structure["_id"]),
        "name": structure["name"],
    })
    flash(f"Fee structure “{structure['name']}” archived.", "success")
    return redirect(url_for("school_admin.fees_page"))


@school_admin_bp.route("/fees/structures/<structure_id>/generate", methods=["POST"])
@school_required
def fee_structure_generate(structure_id):
    structure = _load_fee_structure_or_404(structure_id)

    q = {"status": "active"}
    if structure.get("class_id"):
        q["class_id"] = structure["class_id"]

    student_rows = list(students.find(tenant_filter(q)))
    if not student_rows:
        flash("No active students found in the target class.", "warning")
        return redirect(url_for("school_admin.fee_structure_detail",
                                structure_id=structure_id))

    now = datetime.utcnow()
    created = 0
    skipped = 0

    for s in student_rows:
        existing = invoices.find_one(tenant_filter({
            "student_id": s["_id"],
            "fee_structure_id": structure["_id"],
        }))
        if existing:
            skipped += 1
            continue

        invoice_no = _generate_invoice_no()
        invoices.insert_one({
            "school_id":        g.school["_id"],
            "student_id":       s["_id"],
            "class_id":         s.get("class_id"),
            "fee_structure_id": structure["_id"],
            "invoice_no":       invoice_no,
            "term":             structure["term"],
            "academic_year":    structure["academic_year"],
            "items":            structure.get("items", []),
            "amount_due":       structure.get("total", 0),
            "amount_paid":      0,
            "balance":          structure.get("total", 0),
            "status":           "unpaid",
            "due_date":         structure.get("due_date"),
            "created_by":       g.user["_id"],
            "created_at":       now,
            "updated_at":       now,
        })
        created += 1

    School.log(g.school["_id"], g.user["_id"], "invoices.bulk_generated", {
        "fee_structure_id": str(structure["_id"]),
        "created":          created,
        "skipped":          skipped,
    })

    if created:
        flash(f"{created} invoice(s) created. {skipped} already existed.", "success")
    else:
        flash(f"No new invoices — all {skipped} students already have one.", "info")

    return redirect(url_for("school_admin.fee_structure_detail",
                            structure_id=structure_id))


@school_admin_bp.route("/fees/invoices")
@school_required
def invoices_list():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    class_f = request.args.get("class_id", "").strip()
    term   = request.args.get("term", _current_term())
    ay     = request.args.get("academic_year", _current_academic_year())
    page   = max(1, int(request.args.get("page", 1)))
    per_page = 30

    q = {"term": term, "academic_year": ay}
    if status:
        q["status"] = status
    if class_f:
        oid = _to_oid(class_f)
        if oid:
            q["class_id"] = oid

    if search:
        q["$or"] = [{"invoice_no": {"$regex": search, "$options": "i"}}]

        student_ids_from_search = []
        for s in students.find(tenant_filter({
            "$or": [
                {"first_name":  {"$regex": search, "$options": "i"}},
                {"last_name":   {"$regex": search, "$options": "i"}},
                {"admission_no": {"$regex": search, "$options": "i"}},
            ]
        })).limit(100):
            student_ids_from_search.append(s["_id"])
        if student_ids_from_search:
            q["$or"].append({"student_id": {"$in": student_ids_from_search}})

    full_q = tenant_filter(q)
    total = invoices.count_documents(full_q)
    rows  = list(
        invoices.find(full_q)
        .sort("created_at", -1)
        .skip((page - 1) * per_page).limit(per_page)
    )

    student_ids = {r["student_id"] for r in rows}
    class_ids   = {r["class_id"] for r in rows if r.get("class_id")}
    student_map = {}
    class_map   = {}
    if student_ids:
        for s in students.find(tenant_filter({"_id": {"$in": list(student_ids)}})):
            student_map[s["_id"]] = s
    if class_ids:
        for c in classes.find(tenant_filter({"_id": {"$in": list(class_ids)}})):
            class_map[c["_id"]] = c
    for r in rows:
        r["_student"] = student_map.get(r["student_id"])
        r["_class"]   = class_map.get(r.get("class_id"))

    stats = {
        "total":    invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay,
        })),
        "paid":     invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay, "status": "paid",
        })),
        "partial":  invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay, "status": "partial",
        })),
        "unpaid":   invoices.count_documents(tenant_filter({
            "term": term, "academic_year": ay, "status": "unpaid",
        })),
    }

    class_list = list(classes.find(tenant_filter({})).sort("name", 1))

    return render_template(
        "school_admin/invoices.html",
        invoices=rows,
        total=total, page=page, per_page=per_page,
        search=search, status=status, class_f=class_f,
        term=term, academic_year=ay, terms=TERMS,
        class_list=class_list, stats=stats,
    )


@school_admin_bp.route("/fees/invoices/<invoice_id>")
@school_required
def invoice_detail(invoice_id):
    inv = _load_invoice_or_404(invoice_id)

    student = students.find_one(tenant_filter({"_id": inv["student_id"]}))
    klass   = None
    if inv.get("class_id"):
        klass = classes.find_one(tenant_filter({"_id": inv["class_id"]}))

    payment_rows = list(
        payments.find(tenant_filter({"invoice_id": inv["_id"]})).sort("paid_at", -1)
    )

    recorder_ids = {p["recorded_by"] for p in payment_rows if p.get("recorded_by")}
    recorder_map = {}
    if recorder_ids:
        for u in users.find({"_id": {"$in": list(recorder_ids)}}):
            recorder_map[u["_id"]] = u

    return render_template(
        "school_admin/invoice_detail.html",
        invoice=inv,
        student=student,
        klass=klass,
        payments=payment_rows,
        recorder_map=recorder_map,
        channels=PAYMENT_CHANNELS,
        terms=TERMS,
        format_money=format_money,
    )


@school_admin_bp.route("/fees/invoices/<invoice_id>/payment", methods=["POST"])
@school_required
def invoice_record_payment(invoice_id):
    inv = _load_invoice_or_404(invoice_id)

    if inv["status"] == "waived":
        flash("Cannot add payment to a waived invoice.", "error")
        return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))

    amount_str = (request.form.get("amount") or "").strip()
    channel    = (request.form.get("channel") or "cash").strip()
    reference  = (request.form.get("reference") or "").strip()
    note       = (request.form.get("note") or "").strip()

    errors = []
    try:
        amount = float(amount_str)
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
    except (ValueError, TypeError):
        errors.append("Amount must be a valid number.")

    if channel not in dict(PAYMENT_CHANNELS):
        errors.append("Invalid payment channel.")

    if errors:
        for e in errors: flash(e, "error")
        return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))

    remaining = inv.get("balance", 0)
    if amount > remaining:
        amount = remaining

    now = datetime.utcnow()
    payments.insert_one({
        "school_id":  g.school["_id"],
        "invoice_id": inv["_id"],
        "student_id": inv["student_id"],
        "amount":     amount,
        "currency":   Config.CURRENCY,
        "channel":    channel,
        "reference":  reference or None,
        "note":       note or None,
        "paid_at":    now,
        "recorded_by": g.user["_id"],
        "created_at": now,
    })

    updated_inv = _recompute_invoice(inv["_id"])

    School.log(g.school["_id"], g.user["_id"], "payment.recorded", {
        "invoice_id": str(inv["_id"]),
        "invoice_no": inv["invoice_no"],
        "amount":     amount,
        "channel":    channel,
        "new_status": updated_inv.get("status"),
    })

    flash(f"Payment of {format_money(amount)} recorded.", "success")
    return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))


@school_admin_bp.route("/fees/invoices/<invoice_id>/waive", methods=["POST"])
@school_required
def invoice_waive(invoice_id):
    inv = _load_invoice_or_404(invoice_id)
    invoices.update_one(
        tenant_filter({"_id": inv["_id"]}),
        {"$set": {
            "status": "waived",
            "balance": 0,
            "waived_at": datetime.utcnow(),
            "waived_by": g.user["_id"],
            "updated_at": datetime.utcnow(),
        }},
    )
    School.log(g.school["_id"], g.user["_id"], "invoice.waived", {
        "invoice_id": str(inv["_id"]),
        "invoice_no": inv["invoice_no"],
    })
    flash("Invoice marked as waived.", "success")
    return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))


@school_admin_bp.route("/fees/invoices/<invoice_id>/delete-payment/<payment_id>", methods=["POST"])
@school_required
def payment_delete(invoice_id, payment_id):
    inv = _load_invoice_or_404(invoice_id)
    p_oid = _to_oid(payment_id)
    if not p_oid:
        abort(404)

    p = payments.find_one(tenant_filter({"_id": p_oid, "invoice_id": inv["_id"]}))
    if not p:
        flash("Payment not found.", "error")
        return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))

    payments.delete_one({"_id": p_oid})
    _recompute_invoice(inv["_id"])

    School.log(g.school["_id"], g.user["_id"], "payment.deleted", {
        "invoice_id": str(inv["_id"]),
        "payment_id": str(p_oid),
        "amount":     p.get("amount"),
    })
    flash("Payment reversed.", "success")
    return redirect(url_for("school_admin.invoice_detail", invoice_id=invoice_id))


@school_admin_bp.route("/fees/payments")
@school_required
def payments_list():
    search = request.args.get("q", "").strip()
    channel = request.args.get("channel", "").strip()
    page   = max(1, int(request.args.get("page", 1)))
    per_page = 50

    q = {}
    if channel:
        q["channel"] = channel

    if search:
        inv_ids = [inv["_id"] for inv in invoices.find(tenant_filter({
            "invoice_no": {"$regex": search, "$options": "i"}
        })).limit(100)]
        student_ids = [s["_id"] for s in students.find(tenant_filter({
            "$or": [
                {"first_name":  {"$regex": search, "$options": "i"}},
                {"last_name":   {"$regex": search, "$options": "i"}},
                {"admission_no": {"$regex": search, "$options": "i"}},
            ]
        })).limit(100)]
        q["$or"] = [
            {"invoice_id": {"$in": inv_ids}},
            {"student_id": {"$in": student_ids}},
        ]

    full_q = tenant_filter(q)
    total = payments.count_documents(full_q)
    rows  = list(
        payments.find(full_q)
        .sort("paid_at", -1)
        .skip((page - 1) * per_page).limit(per_page)
    )

    student_ids = {r["student_id"] for r in rows}
    invoice_ids = {r["invoice_id"] for r in rows}
    student_map = {}
    invoice_map = {}
    if student_ids:
        for s in students.find(tenant_filter({"_id": {"$in": list(student_ids)}})):
            student_map[s["_id"]] = s
    if invoice_ids:
        for inv in invoices.find(tenant_filter({"_id": {"$in": list(invoice_ids)}})):
            invoice_map[inv["_id"]] = inv
    for r in rows:
        r["_student"] = student_map.get(r["student_id"])
        r["_invoice"] = invoice_map.get(r["invoice_id"])

    today = _day(datetime.utcnow())
    todays_total = sum(
        p.get("amount", 0) for p in payments.find(tenant_filter({
            "paid_at": {"$gte": today},
        }))
    )

    stats = {
        "total_payments": payments.count_documents(tenant_filter({})),
        "today_count":    payments.count_documents(tenant_filter({
            "paid_at": {"$gte": today},
        })),
        "today_total":    todays_total,
    }

    return render_template(
        "school_admin/payments.html",
        payments=rows,
        total=total, page=page, per_page=per_page,
        search=search, channel=channel,
        channels=PAYMENT_CHANNELS,
        stats=stats,
    )


def _validate_fee_structure_form(form) -> tuple:
    def get(k): return (form.get(k) or "").strip()

    name          = get("name")
    class_id      = get("class_id")
    term          = get("term")
    academic_year = get("academic_year")
    due_date_str  = get("due_date")

    item_names   = form.getlist("item_name") or []
    item_amounts = form.getlist("item_amount") or []

    errors = []
    if not name: errors.append("Fee structure name is required.")
    if term not in dict(TERMS): errors.append("Invalid term.")
    if not academic_year: errors.append("Academic year is required.")

    items = []
    total = 0
    for n, a in zip(item_names, item_amounts):
        n = (n or "").strip()
        a = (a or "").strip()
        if not n and not a:
            continue
        if not n:
            errors.append("Every item needs a name.")
            continue
        try:
            amt = float(a)
            if amt < 0:
                errors.append(f"Amount for “{n}” must be positive.")
                continue
        except ValueError:
            errors.append(f"Amount for “{n}” must be a number.")
            continue
        items.append({"name": n, "amount": amt})
        total += amt

    if not items:
        errors.append("Add at least one fee item.")

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            errors.append("Due date must be YYYY-MM-DD.")

    class_obj_id = None
    if class_id:
        class_obj_id = _to_oid(class_id)
        if not class_obj_id:
            errors.append("Invalid class.")

    clean = {
        "name":          name,
        "class_id":      class_obj_id,
        "term":          term,
        "academic_year": academic_year,
        "due_date":      due_date,
        "items":         items,
        "total":         total,
    }
    return clean, errors


def _load_fee_structure_or_404(structure_id: str):
    oid = _to_oid(structure_id)
    if not oid:
        abort(404)
    s = fee_structures.find_one(tenant_filter({"_id": oid}))
    if not s:
        flash("Fee structure not found.", "error")
        abort(404)
    return s


def _load_invoice_or_404(invoice_id: str):
    oid = _to_oid(invoice_id)
    if not oid:
        abort(404)
    inv = invoices.find_one(tenant_filter({"_id": oid}))
    if not inv:
        flash("Invoice not found.", "error")
        abort(404)
    return inv


# =========================================================
# ANNOUNCEMENTS
# =========================================================
def _validate_announcement_form(form) -> tuple:
    def get(k): return (form.get(k) or "").strip()

    title    = get("title")
    body     = get("body")
    audience = get("audience") or "all"
    priority = get("priority") or "normal"
    expires_str = get("expires_at")

    errors = []
    if not title: errors.append("Title is required.")
    if not body:  errors.append("Message body is required.")
    if audience not in dict(ANNOUNCEMENT_AUDIENCES):
        errors.append("Invalid audience.")
    if priority not in dict(ANNOUNCEMENT_PRIORITIES):
        errors.append("Invalid priority.")

    expires_at = None
    if expires_str:
        try:
            expires_at = datetime.strptime(expires_str, "%Y-%m-%d")
        except ValueError:
            errors.append("Expiry date must be YYYY-MM-DD.")

    clean = {
        "title":      title,
        "body":       body,
        "audience":   audience,
        "priority":   priority,
        "expires_at": expires_at,
    }
    return clean, errors


def _load_announcement_or_404(announcement_id: str):
    oid = _to_oid(announcement_id)
    if not oid:
        abort(404)
    a = announcements.find_one({
        "_id": oid,
        "$or": [
            {"school_id": g.school["_id"]},
            {"school_id": None},
        ],
    })
    if not a:
        flash("Announcement not found.", "error")
        abort(404)
    return a


@school_admin_bp.route("/announcements")
@school_required
def announcements_page():
    search   = request.args.get("q", "").strip()
    audience = request.args.get("audience", "").strip()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 20

    base_or = [
        {"school_id": g.school["_id"]},
        {"school_id": None},
    ]

    q = {"$or": base_or}
    if audience:
        q["audience"] = audience
    if search:
        q = {
            "$and": [
                {"$or": base_or},
                {"$or": [
                    {"title": {"$regex": search, "$options": "i"}},
                    {"body":  {"$regex": search, "$options": "i"}},
                ]},
            ]
        }
        if audience:
            q["audience"] = audience

    total = announcements.count_documents(q)
    rows  = list(
        announcements.find(q)
        .sort([("published_at", -1), ("created_at", -1)])
        .skip((page - 1) * per_page).limit(per_page)
    )

    author_ids = {a["created_by"] for a in rows if a.get("created_by")}
    author_map = {}
    if author_ids:
        for u in users.find({"_id": {"$in": list(author_ids)}}):
            author_map[u["_id"]] = u
    for a in rows:
        a["_author"] = author_map.get(a.get("created_by"))
        a["_is_platform"] = a.get("school_id") is None

    stats = {
        "total": announcements.count_documents({"$or": base_or}),
        "urgent": announcements.count_documents({
            "school_id": g.school["_id"], "priority": "urgent",
        }),
        "this_month": announcements.count_documents({
            "school_id": g.school["_id"],
            "created_at": {"$gte": datetime.utcnow().replace(day=1)},
        }),
        "platform": announcements.count_documents({"school_id": None}),
    }

    return render_template(
        "school_admin/announcements.html",
        announcements=rows,
        total=total, page=page, per_page=per_page,
        search=search, audience=audience,
        audiences=ANNOUNCEMENT_AUDIENCES,
        priorities=ANNOUNCEMENT_PRIORITIES,
        stats=stats,
    )


@school_admin_bp.route("/announcements/new", methods=["GET", "POST"])
@school_required
def announcement_new():
    if request.method == "POST":
        data, errors = _validate_announcement_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/announcement_form.html",
                form=request.form, mode="create",
                audiences=ANNOUNCEMENT_AUDIENCES,
                priorities=ANNOUNCEMENT_PRIORITIES,
            ), 400

        now = datetime.utcnow()
        doc = {
            **data,
            "school_id":   g.school["_id"],
            "attachments": [],
            "published_at": now,
            "created_by":  g.user["_id"],
            "created_at":  now,
            "read_by":     [],
        }
        result = announcements.insert_one(doc)

        School.log(g.school["_id"], g.user["_id"], "announcement.created", {
            "announcement_id": str(result.inserted_id),
            "title": data["title"],
            "audience": data["audience"],
        })

        flash("Announcement published.", "success")
        return redirect(url_for("school_admin.announcement_detail",
                                announcement_id=str(result.inserted_id)))

    return render_template(
        "school_admin/announcement_form.html",
        form={}, mode="create",
        audiences=ANNOUNCEMENT_AUDIENCES,
        priorities=ANNOUNCEMENT_PRIORITIES,
    )


@school_admin_bp.route("/announcements/<announcement_id>")
@school_required
def announcement_detail(announcement_id):
    a = _load_announcement_or_404(announcement_id)

    if g.user["_id"] not in a.get("read_by", []):
        announcements.update_one(
            {"_id": a["_id"]},
            {"$addToSet": {"read_by": g.user["_id"]}},
        )
        a["read_by"] = a.get("read_by", []) + [g.user["_id"]]

    author = None
    if a.get("created_by"):
        author = users.find_one({"_id": a["created_by"]})

    audience_count = 0
    if a.get("school_id") == g.school["_id"]:
        aud = a.get("audience", "all")
        if aud == "students":
            audience_count = students.count_documents(tenant_filter({"status": "active"}))
        elif aud == "parents":
            audience_count = users.count_documents(tenant_filter({"role": "parent"}))
        elif aud == "staff":
            audience_count = users.count_documents(tenant_filter({
                "role": {"$in": ["teacher", "school_admin", "accountant"]},
            }))
        elif aud == "school_admins":
            audience_count = users.count_documents(tenant_filter({"role": "school_admin"}))
        else:
            audience_count = users.count_documents(tenant_filter({}))

    read_count = len(a.get("read_by", []))

    return render_template(
        "school_admin/announcement_detail.html",
        announcement=a,
        author=author,
        audience_count=audience_count,
        read_count=read_count,
        is_platform=(a.get("school_id") is None),
    )


@school_admin_bp.route("/announcements/<announcement_id>/edit", methods=["GET", "POST"])
@school_required
def announcement_edit(announcement_id):
    a = _load_announcement_or_404(announcement_id)

    if a.get("school_id") is None:
        flash("Platform announcements can't be edited from here.", "error")
        return redirect(url_for("school_admin.announcement_detail",
                                announcement_id=announcement_id))

    if request.method == "POST":
        data, errors = _validate_announcement_form(request.form)
        if errors:
            for e in errors: flash(e, "error")
            return render_template(
                "school_admin/announcement_form.html",
                form=request.form, mode="edit", announcement=a,
                audiences=ANNOUNCEMENT_AUDIENCES,
                priorities=ANNOUNCEMENT_PRIORITIES,
            ), 400

        data["updated_at"] = datetime.utcnow()
        announcements.update_one({"_id": a["_id"]}, {"$set": data})

        School.log(g.school["_id"], g.user["_id"], "announcement.updated", {
            "announcement_id": str(a["_id"]),
            "title": data["title"],
        })
        flash("Announcement updated.", "success")
        return redirect(url_for("school_admin.announcement_detail",
                                announcement_id=announcement_id))

    form = {
        "title":      a.get("title", ""),
        "body":       a.get("body", ""),
        "audience":   a.get("audience", "all"),
        "priority":   a.get("priority", "normal"),
        "expires_at": a["expires_at"].strftime("%Y-%m-%d") if a.get("expires_at") else "",
    }
    return render_template(
        "school_admin/announcement_form.html",
        form=form, mode="edit", announcement=a,
        audiences=ANNOUNCEMENT_AUDIENCES,
        priorities=ANNOUNCEMENT_PRIORITIES,
    )


@school_admin_bp.route("/announcements/<announcement_id>/delete", methods=["POST"])
@school_required
def announcement_delete(announcement_id):
    a = _load_announcement_or_404(announcement_id)

    if a.get("school_id") is None:
        flash("Platform announcements can't be deleted from here.", "error")
        return redirect(url_for("school_admin.announcements_page"))

    announcements.delete_one({"_id": a["_id"]})
    School.log(g.school["_id"], g.user["_id"], "announcement.deleted", {
        "announcement_id": str(a["_id"]),
        "title": a.get("title"),
    })
    flash("Announcement deleted.", "success")
    return redirect(url_for("school_admin.announcements_page"))


# =========================================================
# MESSAGES
# =========================================================
def _thread_query_for_user(user_id):
    return {
        "school_id": g.school["_id"],
        "$or": [
            {"sender_id": user_id},
            {"recipient_ids": user_id},
        ],
    }


def _build_threads_for_user(user_id):
    msgs = list(
        messages.find(_thread_query_for_user(user_id))
        .sort("created_at", -1)
    )

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

        partners = []
        if partner_ids:
            for u in users.find({"_id": {"$in": list(partner_ids)}}):
                partners.append(u)

        summaries.append({
            "thread_id":    tid,
            "subject":      last.get("subject") or "(no subject)",
            "last_message": last,
            "partners":     partners,
            "unread":       unread,
            "updated_at":   last.get("created_at"),
            "count":        len(arr),
        })

    summaries.sort(key=lambda s: s["updated_at"] or datetime.min, reverse=True)
    return summaries


def _message_recipients():
    return list(
        users.find({
            "school_id": g.school["_id"],
            "_id": {"$ne": g.user["_id"]},
            "role": {"$in": ["teacher", "school_admin", "accountant", "parent"]},
        }).sort("name", 1)
    )


@school_admin_bp.route("/messages")
@school_required
def messages_page():
    search = request.args.get("q", "").strip()
    tab    = request.args.get("tab", "inbox")

    threads = _build_threads_for_user(g.user["_id"])

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
        "school_admin/messages.html",
        threads=threads,
        tab=tab,
        search=search,
        total_unread=total_unread,
        recipients=_message_recipients()[:8],
    )


@school_admin_bp.route("/messages/new", methods=["GET", "POST"])
@school_required
def message_new():
    if request.method == "POST":
        recipient_ids_raw = request.form.getlist("recipient_ids") or []
        subject = (request.form.get("subject") or "").strip()
        body    = (request.form.get("body") or "").strip()

        errors = []
        recipient_ids = [_to_oid(rid) for rid in recipient_ids_raw if _to_oid(rid)]
        recipient_ids = [r for r in recipient_ids if r]
        if not recipient_ids:
            errors.append("Select at least one recipient.")
        if not body:
            errors.append("Message body is required.")

        if not errors:
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
            School.log(g.school["_id"], g.user["_id"], "message.sent", {
                "thread_id": str(thread_id),
                "recipients": len(recipient_ids),
            })
            flash("Message sent.", "success")
            return redirect(url_for("school_admin.message_thread",
                                    thread_id=str(thread_id)))

        for e in errors: flash(e, "error")
        return render_template(
            "school_admin/message_compose.html",
            form=request.form,
            recipients=_message_recipients(),
        ), 400

    preselect = request.args.get("to", "").strip()
    return render_template(
        "school_admin/message_compose.html",
        form={"recipient_ids": [preselect] if preselect else []},
        recipients=_message_recipients(),
    )


@school_admin_bp.route("/messages/thread/<thread_id>")
@school_required
def message_thread(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    msgs = list(
        messages.find({
            "school_id": g.school["_id"],
            "thread_id": tid,
            "$or": [
                {"sender_id": g.user["_id"]},
                {"recipient_ids": g.user["_id"]},
            ],
        }).sort("created_at", 1)
    )
    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("school_admin.messages_page"))

    now = datetime.utcnow()
    messages.update_many(
        {
            "school_id": g.school["_id"],
            "thread_id": tid,
            "recipient_ids": g.user["_id"],
            "read_at": None,
        },
        {"$set": {"read_at": now}},
    )

    sender_ids = {m["sender_id"] for m in msgs}
    sender_map = {}
    if sender_ids:
        for u in users.find({"_id": {"$in": list(sender_ids)}}):
            sender_map[u["_id"]] = u
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
    subject = msgs[0].get("subject") or "(no subject)"

    return render_template(
        "school_admin/message_thread.html",
        thread_id=str(tid),
        messages=msgs,
        participants=participants,
        subject=subject,
    )


@school_admin_bp.route("/messages/thread/<thread_id>/reply", methods=["POST"])
@school_required
def message_reply(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message body is required.", "error")
        return redirect(url_for("school_admin.message_thread", thread_id=thread_id))

    msgs = list(messages.find({
        "school_id": g.school["_id"],
        "thread_id": tid,
    }).limit(500))
    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("school_admin.messages_page"))

    participant_ids = set()
    for m in msgs:
        participant_ids.add(m["sender_id"])
        for pid in m.get("recipient_ids", []):
            participant_ids.add(pid)
    participant_ids.discard(g.user["_id"])

    if not participant_ids:
        flash("No other participants in this thread.", "error")
        return redirect(url_for("school_admin.message_thread", thread_id=thread_id))

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
        "reply_to":      msgs[-1]["_id"] if msgs else None,
        "created_at":    now,
    })

    School.log(g.school["_id"], g.user["_id"], "message.replied", {
        "thread_id": str(tid),
    })
    return redirect(url_for("school_admin.message_thread", thread_id=thread_id))


@school_admin_bp.route("/messages/compose-to/<user_id>")
@school_required
def message_compose_to(user_id):
    return redirect(url_for("school_admin.message_new", to=user_id))


# =========================================================
# REPORTS & ANALYTICS
# =========================================================
@school_admin_bp.route("/reports")
@school_required
def reports_page():
    snapshot = {
        "students":  students.count_documents(tenant_filter({"status": "active"})),
        "classes":   classes.count_documents(tenant_filter({"status": "active"})),
        "staff":     staff.count_documents(tenant_filter({"status": "active"})),
        "grades":    grades.count_documents(tenant_filter({
            "term": _current_term(),
            "academic_year": _current_academic_year(),
        })),
    }

    return render_template(
        "school_admin/reports.html",
        snapshot=snapshot,
        term=_current_term(),
        academic_year=_current_academic_year(),
        terms=TERMS,
    )


@school_admin_bp.route("/reports/enrollment")
@school_required
def report_enrollment():
    sid = g.school["_id"]
    data  = enrollment_summary(sid, students, classes)
    trend = enrollment_trend(sid, students, months=12)

    return render_template(
        "school_admin/report_enrollment.html",
        data=data,
        trend=trend,
    )


@school_admin_bp.route("/reports/enrollment.csv")
@school_required
def report_enrollment_csv():
    sid = g.school["_id"]
    data = enrollment_summary(sid, students, classes)

    csv_str = rows_to_csv(["class_name", "count"], data["by_class"])
    resp = make_response(csv_str)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="enrollment-{datetime.utcnow().strftime("%Y%m%d")}.csv"'
    )
    return resp


@school_admin_bp.route("/reports/attendance")
@school_required
def report_attendance():
    sid = g.school["_id"]
    start, end = parse_range(request.args)

    summary  = attendance_summary(sid, attendance, start, end)
    by_class = attendance_by_class(sid, attendance, classes, start, end)
    trend    = attendance_trend(sid, attendance, days=30)

    return render_template(
        "school_admin/report_attendance.html",
        summary=summary,
        by_class=by_class,
        trend=trend,
        date_range=_range_for_template(start, end),
    )


@school_admin_bp.route("/reports/attendance.csv")
@school_required
def report_attendance_csv():
    sid = g.school["_id"]
    start, end = parse_range(request.args)
    by_class = attendance_by_class(sid, attendance, classes, start, end)

    csv_str = rows_to_csv(
        ["class_name", "present", "absent", "late", "excused", "total", "pct"],
        by_class,
    )
    resp = make_response(csv_str)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="attendance-{datetime.utcnow().strftime("%Y%m%d")}.csv"'
    )
    return resp


@school_admin_bp.route("/reports/academics")
@school_required
def report_academics():
    sid  = g.school["_id"]
    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    summary  = academics_summary(sid, grades, subjects, term, ay)
    rankings = class_rankings(sid, grades, classes, students, term, ay)
    top      = top_students(sid, grades, students, classes, term, ay, limit=10)

    return render_template(
        "school_admin/report_academics.html",
        summary=summary,
        rankings=rankings,
        top_students=top,
        term=term,
        academic_year=ay,
        terms=TERMS,
    )


@school_admin_bp.route("/reports/academics.csv")
@school_required
def report_academics_csv():
    sid  = g.school["_id"]
    term = request.args.get("term", _current_term())
    ay   = request.args.get("academic_year", _current_academic_year())

    rankings = class_rankings(sid, grades, classes, students, term, ay)
    csv_str = rows_to_csv(
        ["class_name", "average", "grades_entered", "student_count"],
        rankings,
    )
    resp = make_response(csv_str)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="academics-{term}-{ay.replace("/", "-")}.csv"'
    )
    return resp


@school_admin_bp.route("/reports/finance")
@school_required
def report_finance():
    sid = g.school["_id"]

    summary     = finance_summary(sid, invoices, payments)
    trend       = monthly_revenue(sid, payments, months=12)
    outstanding = outstanding_by_class(sid, invoices, classes)

    return render_template(
        "school_admin/report_finance.html",
        summary=summary,
        trend=trend,
        outstanding=outstanding,
    )


@school_admin_bp.route("/reports/finance.csv")
@school_required
def report_finance_csv():
    sid = g.school["_id"]
    outstanding = outstanding_by_class(sid, invoices, classes)

    csv_str = rows_to_csv(["class_name", "balance", "count"], outstanding)
    resp = make_response(csv_str)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="outstanding-{datetime.utcnow().strftime("%Y%m%d")}.csv"'
    )
    return resp


@school_admin_bp.route("/reports/staff")
@school_required
def report_staff():
    sid = g.school["_id"]
    summary = staff_summary(sid, staff, users)

    return render_template(
        "school_admin/report_staff.html",
        summary=summary,
    )


@school_admin_bp.route("/reports/staff.csv")
@school_required
def report_staff_csv():
    sid = g.school["_id"]
    summary = staff_summary(sid, staff, users)

    csv_str = rows_to_csv(["role", "count"], summary["by_role"])
    resp = make_response(csv_str)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="staff-{datetime.utcnow().strftime("%Y%m%d")}.csv"'
    )
    return resp


# =========================================================
# SETTINGS
# =========================================================
def _require_admin():
    """Only school_admin can change settings."""
    if g.user.get("role") != "school_admin":
        flash("Only school admins can modify settings.", "error")
        return False
    return True


def _load_settings_context(school):
    """Common context for every settings page."""
    return {
        "settings": get_settings(school),
        "sections": SETTINGS_SECTIONS,
    }


@school_admin_bp.route("/settings")
@school_required
def settings_page():
    school = g.school
    settings = get_settings(school)

    checks = {
        "profile_complete": bool(settings.get("motto") or settings.get("address", {}).get("city")),
        "academic_set": bool(settings.get("current_academic_year")),
        "grading_scale_set": bool(settings.get("grading_scale")),
        "paystack_configured": bool(settings.get("paystack_public_key")),
        "notifications_on": any(
            settings.get(f"notify_{channel}_{kind}")
            for channel in ("email", "sms")
            for kind in ("attendance", "fees", "grades", "announcements")
            if f"notify_{channel}_{kind}" in settings
        ),
    }

    return render_template(
        "school_admin/settings.html",
        school=school,
        settings=settings,
        sections=SETTINGS_SECTIONS,
        checks=checks,
    )


@school_admin_bp.route("/settings/profile", methods=["GET", "POST"])
@school_required
def settings_profile():
    school = g.school

    if request.method == "POST":
        if not _require_admin():
            return redirect(url_for("school_admin.settings_profile"))

        motto = (request.form.get("motto") or "").strip()
        founded_raw = (request.form.get("founded_year") or "").strip()
        timezone = (request.form.get("timezone") or "Africa/Accra").strip()

        street  = (request.form.get("street") or "").strip()
        city    = (request.form.get("city") or "").strip()
        state_  = (request.form.get("state") or "").strip()
        country = (request.form.get("country") or "").strip()

        errors = []
        founded_year = None
        if founded_raw:
            try:
                founded_year = int(founded_raw)
                if not (1800 <= founded_year <= datetime.utcnow().year):
                    errors.append("Founded year must be reasonable.")
            except ValueError:
                errors.append("Founded year must be a number.")

        if errors:
            for e in errors: flash(e, "error")
            return redirect(url_for("school_admin.settings_profile"))

        update_section(
            schools, school["_id"], "profile",
            {
                "motto":        motto,
                "founded_year": founded_year,
                "timezone":     timezone,
                "address": {
                    "street":  street,
                    "city":    city,
                    "state":   state_,
                    "country": country,
                },
            },
            actor_id=g.user["_id"],
        )
        School.log(school["_id"], g.user["_id"], "settings.profile_updated", {})
        flash("School profile updated.", "success")
        return redirect(url_for("school_admin.settings_profile"))

    return render_template(
        "school_admin/settings_profile.html",
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/academic", methods=["GET", "POST"])
@school_required
def settings_academic():
    school = g.school

    if request.method == "POST":
        if not _require_admin():
            return redirect(url_for("school_admin.settings_academic"))

        current_term = (request.form.get("current_term") or "first").strip()
        academic_year = (request.form.get("current_academic_year") or "").strip()
        ca_max_raw   = (request.form.get("ca_max") or "30").strip()
        exam_max_raw = (request.form.get("exam_max") or "70").strip()

        errors = []
        if current_term not in dict(TERMS):
            errors.append("Invalid term.")
        if not academic_year:
            errors.append("Academic year is required.")

        try:
            ca_max = int(ca_max_raw)
            exam_max = int(exam_max_raw)
            if ca_max < 0 or exam_max < 0 or ca_max + exam_max != 100:
                errors.append("CA max + Exam max must equal 100.")
        except ValueError:
            errors.append("CA/exam max must be numbers.")
            ca_max = 30
            exam_max = 70

        term_starts = {}
        term_ends = {}
        for key, _ in TERMS:
            s = (request.form.get(f"start_{key}") or "").strip()
            e = (request.form.get(f"end_{key}") or "").strip()
            term_starts[key] = _parse_date(s)
            term_ends[key]   = _parse_date(e)

        raw_scale = []
        mins    = request.form.getlist("grade_min")
        letters = request.form.getlist("grade_letter")
        remarks = request.form.getlist("grade_remark")
        for mn, lt, rm in zip(mins, letters, remarks):
            raw_scale.append({"min": mn, "letter": lt, "remark": rm})
        clean_scale, scale_errors = validate_grading_scale(raw_scale)
        errors.extend(scale_errors)

        if errors:
            for err in errors: flash(err, "error")
            return redirect(url_for("school_admin.settings_academic"))

        update_section(
            schools, school["_id"], "academic",
            {
                "current_term":          current_term,
                "current_academic_year": academic_year,
                "term_start_dates":      term_starts,
                "term_end_dates":        term_ends,
                "grading_scale":         clean_scale,
                "ca_max":                ca_max,
                "exam_max":              exam_max,
            },
            actor_id=g.user["_id"],
        )
        School.log(school["_id"], g.user["_id"], "settings.academic_updated", {})
        flash("Academic settings updated.", "success")
        return redirect(url_for("school_admin.settings_academic"))

    settings = get_settings(school)
    term_starts = {}
    term_ends = {}
    for key, _ in TERMS:
        s = settings.get("term_start_dates", {}).get(key)
        e = settings.get("term_end_dates", {}).get(key)
        term_starts[key] = s.strftime("%Y-%m-%d") if isinstance(s, datetime) else ""
        term_ends[key]   = e.strftime("%Y-%m-%d") if isinstance(e, datetime) else ""

    return render_template(
        "school_admin/settings_academic.html",
        **_load_settings_context(school),
        term_starts=term_starts,
        term_ends=term_ends,
        terms=TERMS,
    )


@school_admin_bp.route("/settings/academic/reset-scale", methods=["POST"])
@school_required
def settings_reset_grading_scale():
    if not _require_admin():
        return redirect(url_for("school_admin.settings_academic"))

    update_section(
        schools, g.school["_id"], "academic",
        {"grading_scale": DEFAULT_GRADING_SCALE},
        actor_id=g.user["_id"],
    )
    School.log(g.school["_id"], g.user["_id"], "settings.grading_scale_reset", {})
    flash("Grading scale reset to default.", "success")
    return redirect(url_for("school_admin.settings_academic"))


@school_admin_bp.route("/settings/fees", methods=["GET", "POST"])
@school_required
def settings_fees():
    school = g.school

    if request.method == "POST":
        if not _require_admin():
            return redirect(url_for("school_admin.settings_fees"))

        prefix        = (request.form.get("invoice_prefix") or "INV").strip().upper()
        auto_number   = request.form.get("invoice_auto_number") == "on"
        due_days_raw  = (request.form.get("fee_due_days") or "30").strip()

        errors = []
        if not prefix:
            errors.append("Invoice prefix is required.")
        if len(prefix) > 8:
            errors.append("Invoice prefix must be ≤ 8 characters.")

        try:
            fee_due_days = int(due_days_raw)
            if not (0 <= fee_due_days <= 365):
                errors.append("Due days must be 0–365.")
        except ValueError:
            errors.append("Due days must be a number.")
            fee_due_days = 30

        if errors:
            for e in errors: flash(e, "error")
            return redirect(url_for("school_admin.settings_fees"))

        update_section(
            schools, school["_id"], "fees",
            {
                "invoice_prefix":      prefix,
                "invoice_auto_number": auto_number,
                "fee_due_days":        fee_due_days,
            },
            actor_id=g.user["_id"],
        )
        School.log(school["_id"], g.user["_id"], "settings.fees_updated", {})
        flash("Fee settings updated.", "success")
        return redirect(url_for("school_admin.settings_fees"))

    return render_template(
        "school_admin/settings_fees.html",
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/notifications", methods=["GET", "POST"])
@school_required
def settings_notifications():
    school = g.school

    if request.method == "POST":
        if not _require_admin():
            return redirect(url_for("school_admin.settings_notifications"))

        updates = {
            "notify_email_attendance":    request.form.get("notify_email_attendance") == "on",
            "notify_email_fees":          request.form.get("notify_email_fees") == "on",
            "notify_email_grades":        request.form.get("notify_email_grades") == "on",
            "notify_email_announcements": request.form.get("notify_email_announcements") == "on",
            "notify_sms_attendance":      request.form.get("notify_sms_attendance") == "on",
            "notify_sms_fees":            request.form.get("notify_sms_fees") == "on",
            "sms_sender_id":              (request.form.get("sms_sender_id") or "").strip(),
        }

        if updates["sms_sender_id"] and len(updates["sms_sender_id"]) > 11:
            flash("SMS sender ID must be ≤ 11 characters.", "error")
            return redirect(url_for("school_admin.settings_notifications"))

        update_section(
            schools, school["_id"], "notifications",
            updates,
            actor_id=g.user["_id"],
        )
        School.log(school["_id"], g.user["_id"], "settings.notifications_updated", {})
        flash("Notification preferences updated.", "success")
        return redirect(url_for("school_admin.settings_notifications"))

    return render_template(
        "school_admin/settings_notifications.html",
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/team")
@school_required
def settings_team():
    school = g.school

    team = list(
        users.find({
            "school_id": school["_id"],
            "role": {"$in": ["school_admin", "teacher", "accountant"]},
        }).sort("name", 1)
    )

    staff_map = {}
    for s in staff.find(tenant_filter({})):
        staff_map[s["user_id"]] = s

    for u in team:
        u["_staff"] = staff_map.get(u["_id"])

    return render_template(
        "school_admin/settings_team.html",
        team=team,
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/team/<user_id>/role", methods=["POST"])
@school_required
def settings_team_update_role(user_id):
    if not _require_admin():
        return redirect(url_for("school_admin.settings_team"))

    target = users.find_one(tenant_filter({"_id": _to_oid(user_id)}))
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("school_admin.settings_team"))

    if target["role"] == "school_admin":
        flash("Can't change the role of a school admin.", "error")
        return redirect(url_for("school_admin.settings_team"))

    new_role = (request.form.get("role") or "").strip()
    if new_role not in ("teacher", "accountant", "school_admin"):
        flash("Invalid role.", "error")
        return redirect(url_for("school_admin.settings_team"))

    users.update_one(
        {"_id": target["_id"]},
        {"$set": {"role": new_role}},
    )
    School.log(g.school["_id"], g.user["_id"], "settings.team_role_changed", {
        "user_id": str(target["_id"]),
        "new_role": new_role,
    })
    flash(f"{target['name']}'s role updated to {new_role.replace('_', ' ')}.", "success")
    return redirect(url_for("school_admin.settings_team"))


@school_admin_bp.route("/settings/integrations", methods=["GET", "POST"])
@school_required
def settings_integrations():
    school = g.school

    if request.method == "POST":
        if not _require_admin():
            return redirect(url_for("school_admin.settings_integrations"))

        paystack_pub = (request.form.get("paystack_public_key") or "").strip()
        paystack_sec = (request.form.get("paystack_secret_key") or "").strip()
        sms_api_key  = (request.form.get("sms_api_key") or "").strip()

        updates = {}
        if not paystack_pub.startswith("•••"):
            updates["paystack_public_key"] = paystack_pub
        if paystack_sec and not paystack_sec.startswith("•••"):
            updates["paystack_secret_key"] = paystack_sec
        if not sms_api_key.startswith("•••"):
            updates["sms_api_key"] = sms_api_key

        if updates:
            update_section(
                schools, school["_id"], "integrations",
                updates,
                actor_id=g.user["_id"],
            )
            School.log(school["_id"], g.user["_id"], "settings.integrations_updated", {
                "keys": list(updates.keys()),
            })
            flash("Integration settings updated.", "success")

        return redirect(url_for("school_admin.settings_integrations"))

    return render_template(
        "school_admin/settings_integrations.html",
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/data")
@school_required
def settings_data():
    school = g.school

    counts = {
        "students":   students.count_documents(tenant_filter({})),
        "staff":      staff.count_documents(tenant_filter({})),
        "classes":    classes.count_documents(tenant_filter({})),
        "subjects":   subjects.count_documents(tenant_filter({})),
        "attendance": attendance.count_documents(tenant_filter({})),
        "grades":     grades.count_documents(tenant_filter({})),
        "invoices":   invoices.count_documents(tenant_filter({})),
        "payments":   payments.count_documents(tenant_filter({})),
        "messages":   messages.count_documents(tenant_filter({})),
        "announcements": announcements.count_documents(tenant_filter({})),
    }

    return render_template(
        "school_admin/settings_data.html",
        counts=counts,
        **_load_settings_context(school),
    )


@school_admin_bp.route("/settings/data/export")
@school_required
def settings_data_export():
    import csv
    import io
    import zipfile

    school = g.school
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        rows = list(students.find(tenant_filter({})))
        csv_io = io.StringIO()
        writer = csv.writer(csv_io)
        writer.writerow(["admission_no", "first_name", "middle_name", "last_name",
                         "gender", "dob", "class_id", "status"])
        for r in rows:
            writer.writerow([
                r.get("admission_no", ""),
                r.get("first_name", ""),
                r.get("middle_name", ""),
                r.get("last_name", ""),
                r.get("gender", ""),
                r.get("dob").strftime("%Y-%m-%d") if r.get("dob") else "",
                str(r.get("class_id", "")),
                r.get("status", ""),
            ])
        zf.writestr("students.csv", csv_io.getvalue())

        rows = list(classes.find(tenant_filter({})))
        csv_io = io.StringIO()
        writer = csv.writer(csv_io)
        writer.writerow(["name", "level", "section", "capacity", "status"])
        for r in rows:
            writer.writerow([
                r.get("name", ""),
                r.get("level", ""),
                r.get("section", ""),
                r.get("capacity", ""),
                r.get("status", ""),
            ])
        zf.writestr("classes.csv", csv_io.getvalue())

        rows = list(subjects.find(tenant_filter({})))
        csv_io = io.StringIO()
        writer = csv.writer(csv_io)
        writer.writerow(["name", "code", "is_core", "status"])
        for r in rows:
            writer.writerow([
                r.get("name", ""),
                r.get("code", ""),
                r.get("is_core", ""),
                r.get("status", ""),
            ])
        zf.writestr("subjects.csv", csv_io.getvalue())

        rows = list(grades.find(tenant_filter({})))
        csv_io = io.StringIO()
        writer = csv.writer(csv_io)
        writer.writerow(["student_id", "subject_id", "term", "academic_year",
                         "ca_score", "exam_score", "total", "grade"])
        for r in rows:
            writer.writerow([
                str(r.get("student_id", "")),
                str(r.get("subject_id", "")),
                r.get("term", ""),
                r.get("academic_year", ""),
                r.get("ca_score", ""),
                r.get("exam_score", ""),
                r.get("total", ""),
                r.get("grade", ""),
            ])
        zf.writestr("grades.csv", csv_io.getvalue())

        rows = list(invoices.find(tenant_filter({})))
        csv_io = io.StringIO()
        writer = csv.writer(csv_io)
        writer.writerow(["invoice_no", "student_id", "term", "academic_year",
                         "amount_due", "amount_paid", "balance", "status"])
        for r in rows:
            writer.writerow([
                r.get("invoice_no", ""),
                str(r.get("student_id", "")),
                r.get("term", ""),
                r.get("academic_year", ""),
                r.get("amount_due", ""),
                r.get("amount_paid", ""),
                r.get("balance", ""),
                r.get("status", ""),
            ])
        zf.writestr("invoices.csv", csv_io.getvalue())

    buf.seek(0)
    School.log(school["_id"], g.user["_id"], "settings.data_exported", {})

    filename = f"{school['name'].replace(' ', '_')}_export_{datetime.utcnow().strftime('%Y%m%d')}.zip"
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@school_admin_bp.route("/settings/data/archive-students", methods=["POST"])
@school_required
def settings_archive_students():
    if not _require_admin():
        return redirect(url_for("school_admin.settings_data"))

    confirm = (request.form.get("confirm") or "").strip()
    if confirm != "ARCHIVE":
        flash('Type "ARCHIVE" to confirm.', "error")
        return redirect(url_for("school_admin.settings_data"))

    cutoff_raw = (request.form.get("cutoff_date") or "").strip()
    cutoff = _parse_date(cutoff_raw)
    if not cutoff:
        flash("Pick a valid cutoff date.", "error")
        return redirect(url_for("school_admin.settings_data"))

    r = students.update_many(
        tenant_filter({"status": "active", "created_at": {"$lt": cutoff}}),
        {"$set": {"status": "archived", "archived_at": datetime.utcnow()}},
    )
    School.log(g.school["_id"], g.user["_id"], "settings.bulk_archived", {
        "count": r.modified_count,
        "cutoff": cutoff_raw,
    })
    flash(f"{r.modified_count} students archived.", "success")
    return redirect(url_for("school_admin.settings_data"))
