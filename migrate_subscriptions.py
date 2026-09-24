"""
One-off migration: convert any 'trialing' schools to 'unpaid'.

Run once after deploying the pay-to-play changes:
    py migrate_subscriptions.py
"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime

from extensions import schools, subscriptions


def main():
    print("=" * 60)
    print("Migrating schools: trialing → unpaid")
    print("=" * 60)

    # Schools
    result = schools.update_many(
        {"subscription_status": "trialing"},
        {"$set": {
            "subscription_status": "unpaid",
            "trial_ends_at": None,
            "updated_at": datetime.utcnow(),
        }},
    )
    print(f"✓ Updated {result.modified_count} school(s)")

    # Subscription rows
    result = subscriptions.update_many(
        {"status": "trialing"},
        {"$set": {
            "status": "unpaid",
            "next_billing_date": None,
            "updated_at": datetime.utcnow(),
        }},
    )
    print(f"✓ Updated {result.modified_count} subscription row(s)")

    # Report remaining counts
    print()
    print("Current state:")
    for status in ("unpaid", "active", "past_due", "canceled", "trialing"):
        n = schools.count_documents({"subscription_status": status})
        if n:
            print(f"  {status:12s}: {n}")

    print()
    print("Done. Restart the Flask app.")


if __name__ == "__main__":
    main()