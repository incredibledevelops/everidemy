"""
Everidemy — Shared extensions.

Single place to instantiate:
- The MongoDB client + every collection handle used across the app
- Resend (email)
- Arkesel (SMS)
"""
import resend
import requests
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from config import Config


# =========================================================
# MONGODB
# =========================================================
client = MongoClient(
    Config.MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=20000,
    retryWrites=True,
    appname="everidemy",
)

db = client[Config.MONGO_DB_NAME]


# ---------------------------------------------------------
# Collections
# ---------------------------------------------------------
schools          = db.schools
users            = db.users

students         = db.students
staff            = db.staff

classes          = db.classes
subjects         = db.subjects
attendance       = db.attendance
grades           = db.grades
timetable        = db.timetable

fees             = db.fees
fee_structures   = db.fee_structures
invoices         = db.invoices
payments         = db.payments
subscriptions    = db.subscriptions

announcements    = db.announcements
messages         = db.messages

audit_logs       = db.audit_logs
tickets          = db.tickets
webhook_events   = db.webhook_events


def ping_mongo() -> tuple[bool, str | None]:
    try:
        client.admin.command("ping")
        return True, None
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


# =========================================================
# RESEND (Email)
# =========================================================
if Config.RESEND_API_KEY:
    resend.api_key = Config.RESEND_API_KEY


# =========================================================
# ARKESEL (SMS)
# =========================================================
class ArkeselClient:
    """Thin wrapper around Arkesel SMS API v2."""

    def __init__(self, api_key: str, base_url: str, sender_id: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.sender_id = sender_id

    def send_sms(self, recipients: list[str], message: str, sender: str | None = None) -> tuple[bool, dict | None]:
        """
        Send an SMS via Arkesel.
        Returns (ok, response_dict).
        """
        if not self.api_key:
            return False, {"error": "ARKESEL_API_KEY not configured"}

        payload = {
            "sender": sender or self.sender_id,
            "message": message,
            "recipients": recipients,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/sms/send",
                json=payload,
                headers={
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}

            if resp.status_code in (200, 201) and body.get("status") == "success":
                return True, body
            return False, body
        except Exception as e:
            return False, {"error": str(e)}


sms = ArkeselClient(
    api_key=Config.ARKESEL_API_KEY,
    base_url=Config.ARKESEL_BASE_URL,
    sender_id=Config.ARKESEL_SENDER_ID,
)