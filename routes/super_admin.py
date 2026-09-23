"""
Super Admin blueprint — platform owner's workspace.

Handles:
- Platform dashboard (KPIs, MRR, activity)
- Schools CRUD (create, read, update, delete, suspend, email)
- Subscriptions list
- Revenue report
- Support tickets
- Broadcast announcements (with real emails)
- Audit logs
- Settings
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, current_app,
)

from config import Config
from extensions import (
    schools as schools_col,
    announcements,
    audit_logs,
    tickets as tickets_col,
)
from models import (
    School, Analytics, Subscription, User,
    format_money,
)
from utils.auth import role_required
from utils.mailer import (
    send_platform_broadcast,
    send_school_suspended_email,
    send_school_reactivated_email,
    send_custom_email,
)


super_admin_bp = Blueprint("super_admin", __name__, url_prefix="/super-admin")


# =========================================================
# AUTH GATE
# =========================================================
@super_admin_bp.before_request
@role_required("super_admin")
def _gate():
    """Every route in this blueprint requires a super_admin session."""
    return None


# =========================================================
# CONTEXT
# =========================================================
@super_admin_bp.context_processor
def inject_helpers():
    """Helpers available only inside super_admin templates."""
    return {
        "format_money": format_money,
        "currency_symbol": Config.CURRENCY_SYMBOL,
        "currency_code": Config.CURRENCY,
    }


# =========================================================
# DASHBOARD
# =========================================================
@super_admin_bp.route("/")
@super_admin_bp.route("/dashboard")
def dashboard():
    metrics = Analytics.platform_metrics()
    recent = Analytics.recent_schools(5)
    activity = Analytics.recent_activity(8)
    return render_template(
        "super_admin/dashboard.html",
        metrics=metrics,
        recent_schools=recent,
        activity=activity,
        plans=Config.PLANS,
    )


# =========================================================
# SCHOOLS — LIST
# =========================================================
@super_admin_bp.route("/schools")
def schools_list():
    search = request.args.get("q", "").strip()
    plan   = request.args.get("plan", "all")
    status = request.args.get("status", "all")
    page   = max(1, int(request.args.get("page", 1)))
    per_page = 20

    schools = School.all(
        search=search, plan=plan, status=status,
        limit=per_page, skip=(page - 1) * per_page,
    )

    for s in schools:
        s["_student_count"] = School.student_count(s["_id"])

    total = School.count(search=search, plan=plan, status=status)

    return render_template(
        "super_admin/schools.html",
        schools=schools,
        total=total,
        page=page,
        per_page=per_page,
        search=search,
        plan=plan,
        status=status,
        plans=Config.PLANS,
    )


# =========================================================
# SCHOOLS — CREATE
# =========================================================
@super_admin_bp.route("/schools/new", methods=["GET", "POST"])
def school_new():
    if request.method == "POST":
        name        = (request.form.get("name") or "").strip()
        school_type = (request.form.get("type") or "primary").strip().lower()
        owner_name  = (request.form.get("owner_name") or "").strip()
        owner_email = (request.form.get("owner_email") or "").strip().lower()
        phone       = (request.form.get("phone") or "").strip()
        password    = request.form.get("password") or ""
        send_mail   = request.form.get("send_welcome") == "on"

        errors = []
        if not name: errors.append("School name is required.")
        if not owner_name: errors.append("Admin name is required.")
        if not owner_email: errors.append("Admin email is required.")
        if User.find_by_email(owner_email):
            errors.append("That email is already registered.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "super_admin/school_form.html",
                form=request.form, mode="create", plans=Config.PLANS,
            ), 400

        try:
            school, user = School.create(
                name=name,
                owner_email=owner_email,
                owner_name=owner_name,
                password=password or None,
                phone=phone,
                plan=Config.PLAN_KEY,
                type=school_type,
                send_welcome=send_mail,
            )
            flash(f"School “{name}” created successfully.", "success")
            return redirect(url_for("super_admin.school_detail", school_id=school["_id"]))

        except Exception:
            current_app.logger.exception("Failed to create school")
            flash(
                "Could not create the school. Please try again, "
                "or contact engineering if the issue persists.",
                "error",
            )
            return render_template(
                "super_admin/school_form.html",
                form=request.form, mode="create", plans=Config.PLANS,
            ), 500

    # GET
    return render_template(
        "super_admin/school_form.html",
        form={}, mode="create", plans=Config.PLANS,
    )


# =========================================================
# SCHOOLS — DETAIL
# =========================================================
@super_admin_bp.route("/schools/<school_id>")
def school_detail(school_id):
    school = School.find_by_id(school_id)
    if not school:
        flash("School not found.", "error")
        return redirect(url_for("super_admin.schools_list"))

    subscription = Subscription.find_for_school(school_id)
    owner = School.get_owner(school_id)

    return render_template(
        "super_admin/school_detail.html",
        school=school,
        subscription=subscription,
        owner=owner,
        plans=Config.PLANS,
        student_count=School.student_count(school_id),
        staff_count=School.staff_count(school_id),
    )


# =========================================================
# SCHOOLS — EDIT
# =========================================================
@super_admin_bp.route("/schools/<school_id>/edit", methods=["GET", "POST"])
def school_edit(school_id):
    school = School.find_by_id(school_id)
    if not school:
        flash("School not found.", "error")
        return redirect(url_for("super_admin.schools_list"))

    if request.method == "POST":
        name        = (request.form.get("name") or "").strip()
        school_type = (request.form.get("type") or "primary").strip().lower()
        phone       = (request.form.get("phone") or "").strip()

        errors = []
        if not name:
            errors.append("School name is required.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "super_admin/school_form.html",
                form=request.form, mode="edit",
                school=school, plans=Config.PLANS,
            ), 400

        School.update(
            school_id,
            {"name": name, "phone": phone, "type": school_type},
            actor_id=g.user["_id"],
        )
        flash("School updated.", "success")
        return redirect(url_for("super_admin.school_detail", school_id=school_id))

    # GET → prefill
    form = {
        "name": school.get("name", ""),
        "phone": school.get("phone", ""),
        "type": school.get("type", "primary"),
    }
    return render_template(
        "super_admin/school_form.html",
        form=form, mode="edit",
        school=school, plans=Config.PLANS,
    )


# =========================================================
# SCHOOLS — DELETE
# =========================================================
@super_admin_bp.route("/schools/<school_id>/delete", methods=["POST"])
def school_delete(school_id):
    school = School.find_by_id(school_id)
    if not school:
        flash("School not found.", "error")
        return redirect(url_for("super_admin.schools_list"))

    name = school.get("name", "School")
    ok = School.delete(school_id, actor_id=g.user["_id"])
    if ok:
        flash(f"“{name}” and all its data have been deleted.", "success")
    else:
        flash("Could not delete school.", "error")
    return redirect(url_for("super_admin.schools_list"))


# =========================================================
# SCHOOLS — SUSPEND / REACTIVATE
# =========================================================
@super_admin_bp.route("/schools/<school_id>/suspend", methods=["POST"])
def suspend_school(school_id):
    school = School.find_by_id(school_id)
    if not school:
        flash("School not found.", "error")
        return redirect(url_for("super_admin.schools_list"))

    new_state = School.toggle_suspend(school_id, actor_id=g.user["_id"])

    # Notify owner (best-effort — never blocks the request)
    owner = School.get_owner(school_id)
    if owner:
        try:
            if new_state:
                send_school_suspended_email(school["name"], owner["email"])
            else:
                send_school_reactivated_email(school["name"], owner["email"])
        except Exception as e:
            current_app.logger.warning(f"Suspend email failed: {e}")

    state_word = "suspended" if new_state else "reactivated"
    flash(f"“{school['name']}” has been {state_word}.", "success")
    return redirect(
        request.referrer
        or url_for("super_admin.school_detail", school_id=school_id)
    )


# =========================================================
# SCHOOLS — EMAIL OWNER
# =========================================================
@super_admin_bp.route("/schools/<school_id>/email", methods=["POST"])
def school_email(school_id):
    school = School.find_by_id(school_id)
    if not school:
        flash("School not found.", "error")
        return redirect(url_for("super_admin.schools_list"))

    owner = School.get_owner(school_id)
    if not owner:
        flash("No admin user found for this school.", "error")
        return redirect(url_for("super_admin.school_detail", school_id=school_id))

    subject = (request.form.get("subject") or "").strip() or "A message from Everidemy"
    body    = (request.form.get("body") or "").strip()

    if not body:
        flash("Message body cannot be empty.", "error")
        return redirect(url_for("super_admin.school_detail", school_id=school_id))

    ok, err = send_custom_email(owner["email"], subject, body)
    if ok:
        flash(f"Email sent to {owner['email']}.", "success")
    else:
        flash(f"Could not send email: {err}", "error")

    return redirect(url_for("super_admin.school_detail", school_id=school_id))


# =========================================================
# SUBSCRIPTIONS
# =========================================================
@super_admin_bp.route("/subscriptions")
def subscriptions_page():
    subs = Subscription.all(limit=200)

    # Join schools in one pass
    school_ids = [s["school_id"] for s in subs if s.get("school_id")]
    schools_by_id = {
        s["_id"]: s for s in schools_col.find({"_id": {"$in": school_ids}})
    }

    rows = [
        {"sub": s, "school": schools_by_id.get(s["school_id"])}
        for s in subs
    ]
    return render_template(
        "super_admin/subscriptions.html",
        rows=rows, plans=Config.PLANS,
    )


# =========================================================
# REVENUE
# =========================================================
@super_admin_bp.route("/revenue")
def revenue():
    metrics = Analytics.platform_metrics()
    return render_template(
        "super_admin/revenue.html",
        metrics=metrics, plans=Config.PLANS,
    )


# =========================================================
# TICKETS
# =========================================================
@super_admin_bp.route("/tickets")
def tickets_page():
    all_tickets = list(
        tickets_col.find().sort("created_at", -1).limit(100)
    )
    return render_template("super_admin/tickets.html", tickets=all_tickets)


# =========================================================
# BROADCAST (with real emails)
# =========================================================
@super_admin_bp.route("/broadcast", methods=["GET", "POST"])
def broadcast():
    if request.method == "POST":
        message    = (request.form.get("message") or "").strip()
        # `audience` here means which schools get the email.
        # It is NOT the announcement audience — every portal should see it.
        target     = request.form.get("audience", "all")
        also_email = request.form.get("also_email") == "on"

        if not message:
            flash("Message cannot be empty.", "error")
            return redirect(url_for("super_admin.broadcast"))

        now = datetime.utcnow()

        # 1. Save the announcement (audience="all" so every portal sees it)
        announcements.insert_one({
            "school_id": None,
            "title": "Platform broadcast",
            "body": message,
            "audience": "all",              # <-- FIXED
            "priority": "normal",           # <-- FIXED: needed by other views
            "attachments": [],
            "published_at": now,            # <-- FIXED: needed by sort + read_by
            "created_at": now,
            "created_by": g.user["_id"],
            "read_by": [],
            "target_status": target,        # informational — which schools got emailed
        })

        # 2. Determine target schools for the email send
        q = {}
        if target == "active":
            q["subscription_status"] = "active"
        elif target == "trialing":
            q["subscription_status"] = "trialing"
        # "all" → every school (including suspended)

        target_schools = list(schools_col.find(q)) if q else School.all(limit=1000)

        # 3. Send emails
        sent = failed = 0
        if also_email:
            for s in target_schools:
                owner = School.get_owner(s["_id"])
                if not owner:
                    continue
                ok, _ = send_platform_broadcast(s["name"], owner["email"], message)
                if ok:
                    sent += 1
                else:
                    failed += 1

        # 4. Flash result
        if also_email:
            flash(
                f"Broadcast saved · {sent} emails sent · {failed} failed.",
                "success" if failed == 0 else "warning",
            )
        else:
            flash("Broadcast saved (emails not sent).", "success")

        return redirect(url_for("super_admin.broadcast"))

    # GET → recent broadcasts
    recent = list(
        announcements
        .find({"school_id": None})
        .sort("created_at", -1)
        .limit(20)
    )
    return render_template("super_admin/broadcast.html", recent=recent)


# =========================================================
# AUDIT LOGS
# =========================================================
@super_admin_bp.route("/audit-logs")
def audit_logs_page():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50
    action   = request.args.get("action", "").strip()

    q = {}
    if action:
        q["action"] = {"$regex": action, "$options": "i"}

    logs = list(
        audit_logs
        .find(q)
        .sort("timestamp", -1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )
    total = audit_logs.count_documents(q)

    return render_template(
        "super_admin/audit_logs.html",
        logs=logs, page=page, per_page=per_page, total=total,
        action=action,
    )


# =========================================================
# SETTINGS
# =========================================================
@super_admin_bp.route("/settings")
def settings():
    return render_template("super_admin/settings.html", plans=Config.PLANS)