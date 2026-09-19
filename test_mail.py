from app import app
from extensions import mail
from flask_mail import Message

with app.app_context():
    try:
        mail.send(Message(
            subject="Everidemy SMTP test",
            recipients=["your-personal-email@gmail.com"],
            body="If you got this, SMTP works! 🎉",
        ))
        print("✓ Email sent")
    except Exception as e:
        print(f"✗ Failed: {e}")