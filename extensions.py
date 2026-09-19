from pymongo import MongoClient
from flask_mail import Mail
from config import Config

# ---------- MongoDB ----------
client = MongoClient(Config.MONGO_URI)
db = client[Config.MONGO_DB_NAME]

schools          = db.schools
users            = db.users
students         = db.students
staff            = db.staff
classes          = db.classes
subjects         = db.subjects
attendance       = db.attendance
grades           = db.grades
fees             = db.fees
fee_structures   = db.fee_structures
invoices         = db.invoices
payments         = db.payments
subscriptions    = db.subscriptions
announcements    = db.announcements
messages         = db.messages
audit_logs       = db.audit_logs
tickets          = db.tickets
timetable        = db.timetable
webhook_events   = db.webhook_events     # ← NEW

# ---------- Mail ----------
mail = Mail()