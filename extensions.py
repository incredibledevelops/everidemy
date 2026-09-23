"""
Everidemy — Shared extensions.

Single place to instantiate:
- The MongoDB client + every collection handle used across the app
- The Flask-Mail instance

Importing this module does NOT require a Flask app context.
`mail.init_app(app)` is called inside `create_app()`.
"""
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from flask_mail import Mail

from config import Config


# =========================================================
# MONGODB
# =========================================================
client = MongoClient(
    Config.MONGO_URI,
    # Fail fast when Mongo is unreachable (default is 30s)
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=20000,
    # Auto-retry transient network errors on writes
    retryWrites=True,
    # Appears in `db.currentOp()` and server logs — helps when debugging
    appname="everidemy",
)

db = client[Config.MONGO_DB_NAME]


# ---------------------------------------------------------
# Collections
# ---------------------------------------------------------
# Core tenancy
schools          = db.schools
users            = db.users

# People
students         = db.students
staff            = db.staff

# Academics
classes          = db.classes
subjects         = db.subjects
attendance       = db.attendance
grades           = db.grades
timetable        = db.timetable

# Fees & billing
fees             = db.fees             # ← legacy / reserved
fee_structures   = db.fee_structures
invoices         = db.invoices
payments         = db.payments
subscriptions    = db.subscriptions

# Communication
announcements    = db.announcements
messages         = db.messages

# Platform / ops
audit_logs       = db.audit_logs
tickets          = db.tickets
webhook_events   = db.webhook_events


# ---------------------------------------------------------
# Boot-time connectivity check
# ---------------------------------------------------------
def ping_mongo() -> tuple[bool, str | None]:
    """
    Return (ok, error_message).
    Uses a fast ping to fail loudly if the DB is unreachable.
    """
    try:
        client.admin.command("ping")
        return True, None
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


# =========================================================
# FLASK-MAIL
# =========================================================
mail = Mail()