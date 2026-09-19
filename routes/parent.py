"""
Parent blueprint — the parent's portal.

Parents have `role="parent"` and `linked_children: [student_id, ...]`.
They can see each child's grades, attendance, and fees, and pay online.
"""
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, g, abort, current_app,
)
from bson import ObjectId

from config import Config
from extensions import (
    users, students, classes, subjects,
    attendance, grades, invoices, payments,
    announcements, messages,
)
from models import School, format_money
from utils.auth import role_required
from utils import paystack


parent_bp = Blueprint(
    "parent", __name__,
    url_prefix="/parent",
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
# CHILD HELPERS
# =========================================================
def _my_children():
    """Return student docs linked to this parent user."""
    child_ids = g.user.get("linked_children") or []
    if not child_ids:
        # Fallback: match by guardian_email
        return list(students.find(_tenant({
            "guardian_email": g.user.get("email", ""),
            "status": "active",
        })).sort("first_name", 1))

    return list(
        students.find(_tenant({
            "_id": {"$in": child_ids},
            "status": "active",
        })).sort("first_name", 1)
    )


def _load_child_or_404(student_id):
    """Fetch a child, verifying it belongs to this parent."""
    oid = _to_oid(student_id)
    if not oid:
        abort(404)
    child = students.find_one(_tenant({"_id": oid}))
    if not child:
        flash("Child not found.", "error")
        abort(404)

    # Access check
    my_child_ids = [c["_id"] for c in _my_children()]
    if child["_id"] not in my_child_ids and child.get("guardian_email") != g.user.get("email"):
        flash("You don't have access to that student.", "error")
        abort(403)

    return child


def _message_recipients():
    """Parents can only message staff (not other parents or students)."""
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
@parent_bp.context_processor
def inject_parent_context():
    """
    Values available in every parent-portal template.
    Also computes `children` and `unread_messages` so the sidebar
    and the notification bell work without each route passing them.
    """
    unread = 0
    children = []
    try:
        unread = messages.count_documents({
            "school_id": g.school["_id"],
            "recipient_ids": g.user["_id"],
            "read_at": None,
        })

        # Enrich each child with class + balance for the sidebar
        for c in _my_children():
            if c.get("class_id"):
                c["_class"] = classes.find_one(_tenant({"_id": c["class_id"]}))
            else:
                c["_class"] = None

            invs = list(invoices.find(_tenant({"student_id": c["_id"]})))
            due = sum(i.get("amount_due", 0) for i in invs)
            paid = sum(i.get("amount_paid", 0) for i in invs)
            c["_balance"] = max(due - paid, 0)

            children.append(c)
    except Exception:
        pass

    return {
        "format_money": format_money,
        "currency_symbol": Config.CURRENCY_SYMBOL,
        "currency_code": Config.CURRENCY,
        "children": children,
        "unread_messages": unread,
    }


# =========================================================
# GATE
# =========================================================
@parent_bp.before_request
@role_required("parent", "school_admin")
def _gate():
    return None


# =========================================================
# DASHBOARD
# =========================================================
@parent_bp.route("/")
@parent_bp.route("/dashboard")
def dashboard():
    children = _my_children()

    # Enrich each child with quick stats for the main cards
    for c in children:
        klass = None
        if c.get("class_id"):
            klass = classes.find_one(_tenant({"_id": c["class_id"]}))
        c["_class"] = klass

        # Attendance %
        att_docs = list(attendance.find(_tenant({})).sort("date", -1).limit(60))
        tot = pres = 0
        for doc in att_docs:
            for rec in doc.get("records", []):
                if rec.get("student_id") == c["_id"]:
                    tot += 1
                    if rec.get("status") in ("present", "late"):
                        pres += 1
        c["_attendance_pct"] = round((pres / tot) * 100) if tot else 0

        # Fee balance
        invs = list(invoices.find(_tenant({"student_id": c["_id"]})))
        due = sum(i.get("amount_due", 0) for i in invs)
        paid = sum(i.get("amount_paid", 0) for i in invs)
        c["_balance"] = max(due - paid, 0)

        # Average grade (current term)
        term = _current_term()
        ay = _current_academic_year()
        g_pipeline = [
            {"$match": _tenant({
                "student_id": c["_id"], "term": term, "academic_year": ay,
            })},
            {"$group": {"_id": None, "avg": {"$avg": "$total"}}},
        ]
        g_res = list(grades.aggregate(g_pipeline))
        c["_avg_grade"] = round(g_res[0]["avg"]) if g_res and g_res[0].get("avg") else 0

    # Recent notices
    recent_notices = list(
        announcements.find({
            "$or": [
                {"school_id": g.school["_id"]},
                {"school_id": None},
            ],
        }).sort("created_at", -1).limit(4)
    )

    return render_template(
        "parent/dashboard.html",
        children=children,
        recent_notices=recent_notices,
    )


# =========================================================
# CHILD DETAIL
# =========================================================
@parent_bp.route("/child/<student_id>")
def child_detail(student_id):
    child = _load_child_or_404(student_id)
    klass = classes.find_one(_tenant({"_id": child["class_id"]})) if child.get("class_id") else None
    tab = request.args.get("tab", "overview")

    # Attendance summary + recent
    docs = list(attendance.find(_tenant({})).sort("date", -1).limit(60))
    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    recent_att = []
    for doc in docs:
        for rec in doc.get("records", []):
            if rec.get("student_id") == child["_id"]:
                st = rec.get("status", "present")
                counts[st] = counts.get(st, 0) + 1
                if len(recent_att) < 30:
                    recent_att.append({"date": doc["date"], "status": st})
    att_total = sum(counts.values())
    att_pct = round(((counts["present"] + counts["late"]) / att_total) * 100) if att_total else 0

    # Grades for current term
    term = _current_term()
    ay = _current_academic_year()
    grade_rows = list(grades.find(_tenant({
        "student_id": child["_id"], "term": term, "academic_year": ay,
    })))
    subj_ids = [r["subject_id"] for r in grade_rows]
    subj_map = {sj["_id"]: sj for sj in subjects.find(_tenant({"_id": {"$in": subj_ids}}))}

    enriched = []
    total_sum = 0
    for r in grade_rows:
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
    average = round(total_sum / len(enriched)) if enriched else 0

    # Invoices
    invs = list(invoices.find(_tenant({"student_id": child["_id"]})).sort("created_at", -1))
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
        "parent/child_detail.html",
        child=child,
        klass=klass,
        active_tab=tab,
        att_counts=counts,
        att_pct=att_pct,
        recent_att=recent_att,
        grades=enriched,
        average=average,
        invoices=invs,
        total_due=total_due,
        total_paid=total_paid,
        balance=balance,
        term=term,
        academic_year=ay,
    )


# =========================================================
# PAY FEES
# =========================================================
@parent_bp.route("/child/<student_id>/pay/<invoice_id>")
def pay_fee(student_id, invoice_id):
    """
    Show a payment page for a specific invoice.

    Uses the platform's Paystack PUBLIC key (Config.PAYSTACK_PUBLIC_KEY).
    This matches `pay_fee_callback`, which verifies with the platform's
    SECRET key, and matches the webhook which verifies with the platform
    secret too. One Paystack account, one webhook URL.
    """
    child = _load_child_or_404(student_id)

    inv = invoices.find_one(_tenant({
        "_id": _to_oid(invoice_id),
        "student_id": child["_id"],
    }))
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))

    if inv["status"] == "paid":
        flash("This invoice is already fully paid.", "info")
        return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))

    return render_template(
        "parent/pay_fee.html",
        child=child,
        invoice=inv,
        paystack_pub=Config.PAYSTACK_PUBLIC_KEY or "",
    )


@parent_bp.route("/child/<student_id>/pay/<invoice_id>/callback")
def pay_fee_callback(student_id, invoice_id):
    """
    Called after Paystack checkout closes.
    Verifies the transaction server-side and records the payment.
    The webhook (arriving a few seconds later) is idempotent.
    """
    child = _load_child_or_404(student_id)
    inv = invoices.find_one(_tenant({
        "_id": _to_oid(invoice_id),
        "student_id": child["_id"],
    }))
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))

    reference = (request.args.get("reference") or "").strip()
    if not reference:
        flash("No payment reference provided.", "error")
        return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))

    # Verify with the PLATFORM secret — same account that received
    # the charge (Config.PAYSTACK_PUBLIC_KEY is what the JS used).
    tx = paystack.verify_transaction(reference)

    if not tx or tx.get("status") != "success":
        flash(
            "We couldn't confirm your payment yet. If you were charged, "
            "it will appear on this invoice within a few minutes.",
            "warning",
        )
        return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))

    amount = (tx.get("amount", 0) / 100.0)
    channel = tx.get("channel", "paystack")

    # Idempotency — don't double-record if the webhook already handled it
    existing = payments.find_one(_tenant({"paystack_reference": reference}))
    if not existing:
        payments.insert_one({
            "school_id":          g.school["_id"],
            "invoice_id":         inv["_id"],
            "student_id":         child["_id"],
            "amount":             amount,
            "currency":           Config.CURRENCY,
            "channel":            channel,
            "reference":          reference,
            "paystack_reference": reference,
            "note":               "Online payment via Paystack",
            "paid_at":            datetime.utcnow(),
            "recorded_by":        g.user["_id"],
            "created_at":         datetime.utcnow(),
        })

        from routes.school_admin import _recompute_invoice
        _recompute_invoice(inv["_id"], school_id=g.school["_id"])

        School.log(g.school["_id"], g.user["_id"], "payment.recorded", {
            "invoice_id": str(inv["_id"]),
            "invoice_no": inv["invoice_no"],
            "amount":     amount,
            "channel":    channel,
            "reference":  reference,
            "source":     "parent_portal",
        })

    flash(f"Payment of {format_money(amount)} received. Thank you!", "success")
    return redirect(url_for("parent.child_detail", student_id=student_id, tab="fees"))


# =========================================================
# MESSAGES
# =========================================================
def _threads_for(user_id):
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


@parent_bp.route("/messages")
def messages_page():
    threads = _threads_for(g.user["_id"])
    tab = request.args.get("tab", "inbox")
    if tab == "unread":
        threads = [t for t in threads if t["unread"] > 0]

    return render_template(
        "parent/messages.html",
        threads=threads,
        tab=tab,
        total_unread=sum(t["unread"] for t in threads),
    )


@parent_bp.route("/messages/thread/<thread_id>")
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
        return redirect(url_for("parent.messages_page"))

    messages.update_many(
        {"school_id": g.school["_id"], "thread_id": tid,
         "recipient_ids": g.user["_id"], "read_at": None},
        {"$set": {"read_at": datetime.utcnow()}},
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

    return render_template(
        "parent/message_thread.html",
        thread_id=str(tid),
        messages=msgs,
        participants=participants,
        subject=msgs[0].get("subject") or "(no subject)",
    )


@parent_bp.route("/messages/thread/<thread_id>/reply", methods=["POST"])
def message_reply(thread_id):
    tid = _to_oid(thread_id)
    if not tid:
        abort(404)

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message body is required.", "error")
        return redirect(url_for("parent.message_thread", thread_id=thread_id))

    msgs = list(messages.find({"school_id": g.school["_id"], "thread_id": tid}).limit(500))
    if not msgs:
        flash("Conversation not found.", "error")
        return redirect(url_for("parent.messages_page"))

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
    return redirect(url_for("parent.message_thread", thread_id=thread_id))


@parent_bp.route("/messages/new", methods=["GET", "POST"])
def message_new():
    if request.method == "POST":
        recipient_ids_raw = request.form.getlist("recipient_ids") or []
        subject = (request.form.get("subject") or "").strip()
        body    = (request.form.get("body") or "").strip()

        recipient_ids = [_to_oid(rid) for rid in recipient_ids_raw if _to_oid(rid)]
        recipient_ids = [r for r in recipient_ids if r]

        if not recipient_ids:
            flash("Select at least one recipient.", "error")
            return redirect(url_for("parent.message_new"))
        if not body:
            flash("Message body is required.", "error")
            return redirect(url_for("parent.message_new"))

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
        return redirect(url_for("parent.message_thread", thread_id=str(thread_id)))

    preselect = request.args.get("to", "").strip()
    return render_template(
        "parent/message_compose.html",
        form={"recipient_ids": [preselect] if preselect else []},
        recipients=_message_recipients(),
    )