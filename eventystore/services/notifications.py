"""Notifications in-app (toujours), e-mail (SMTP si configuré, sinon outbox) et SMS (point d'extension).

Chaque notification est historisée et dédupliquée (`dedupe_key`). Une notification critique n'est jamais
uniquement envoyée par e-mail : elle est toujours créée in-app.
"""
import smtplib
from email.message import EmailMessage

from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import EmailOutbox, Notification, SmsOutbox, utcnow


def abs_url(path):
    if not path:
        return current_app.config["SITE_URL"]
    if path.startswith("http"):
        return path
    return current_app.config["SITE_URL"].rstrip("/") + path


def send_email(to_email, subject, body, user_id=None):
    """Enregistre l'e-mail dans l'outbox puis tente l'envoi SMTP si configuré."""
    cfg = current_app.config
    row = EmailOutbox(user_id=user_id, to_email=to_email, subject=subject, body=body, status="queued")
    db.session.add(row)
    if not cfg["SMTP_HOST"]:
        row.status = "not_sent"
        row.error = "SMTP non configuré (aperçu uniquement)"
        return row
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], to_email, subject
        msg.set_content(body)
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as s:
            if cfg["SMTP_TLS"]:
                s.starttls()
            if cfg["SMTP_USER"]:
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
        row.status, row.sent_at = "sent", utcnow()
    except Exception as exc:  # noqa: BLE001 - on ne casse jamais le parcours métier pour un e-mail
        row.status, row.error = "failed", str(exc)[:250]
        current_app.logger.warning("Envoi e-mail échoué: %s", exc)
    return row


def notify(user, kind, title, body="", url=None, critical=False, dedupe_key=None, email=True):
    """Crée la notification in-app et déclenche les canaux selon les préférences."""
    if user is None:
        return None
    if dedupe_key:
        exists = Notification.query.filter_by(user_id=user.id, dedupe_key=dedupe_key).first()
        if exists:
            return exists
    n = Notification(user_id=user.id, kind=kind, title=title, body=body, url=url, critical=critical,
                     dedupe_key=dedupe_key)
    try:
        with db.session.begin_nested():
            db.session.add(n)
    except IntegrityError:  # course sur la clé de déduplication : on ignore
        return None
    if email and user.wants("email"):
        text = f"Bonjour {user.first_name},\n\n{title}\n{body}\n"
        if url:
            text += f"\nVoir le détail : {abs_url(url)}\n"
        text += "\n— L'équipe EventyStore\nPlanifiez • Réservez • Célébrez"
        send_email(user.email, f"[EventyStore] {title}", text, user.id)
    if critical and user.wants("sms") and user.phone:
        # Point d'extension : brancher ici un fournisseur SMS (Orange SMS API, Twilio…).
        db.session.add(SmsOutbox(user_id=user.id, to_phone=user.phone, body=f"EventyStore: {title}"[:300],
                                 status="not_sent"))
    return n


def unread_count(user):
    return Notification.query.filter_by(user_id=user.id, read_at=None).count()
