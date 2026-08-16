"""
Shared Gmail SMTP sending logic for both main.py (weekly digest) and
recap_email.py (manual archive resend). Both scripts send email the exact
same way — this file exists so that fact only needs to be true in one
place instead of two staying in sync by hand.
"""

import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", "vernonlee3701@gmail.com")  # must match the Gmail account the app password belongs to

_raw_email_to = os.getenv("EMAIL_TO", "vernonlee37@gmail.com")
EMAIL_TO = [email.strip() for email in _raw_email_to.split(",") if email.strip()]


def send_email(subject, html_content):
    """
    Send an HTML email via Gmail SMTP. Returns True on success, False on
    failure — callers are expected to check this and act accordingly
    (e.g. not marking articles as archived if the send failed).
    """
    # TEMPORARY DEBUG — remove once the 535 error is resolved.
    # GitHub masks the actual value since it's a secret, so compare instead
    # of printing it directly — True/False and a number won't get redacted.
    logging.info(f"DEBUG: EMAIL_FROM exactly matches 'vernonlee3701@gmail.com': {EMAIL_FROM == 'vernonlee3701@gmail.com'} (length: {len(EMAIL_FROM) if EMAIL_FROM else 0}, expected length: {len('vernonlee3701@gmail.com')})")
    logging.info(f"DEBUG: app password length: {len(GMAIL_APP_PASSWORD) if GMAIL_APP_PASSWORD else 0} (expected: 16)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logging.info(f"Email sent successfully to {EMAIL_TO}")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False
