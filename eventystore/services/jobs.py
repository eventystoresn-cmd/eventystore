"""Tâches périodiques : expiration des blocages et devis, démarrage des prestations, rappels, avis, fonds.

Exécution : `flask run-jobs` (cron toutes les 5 min en production) ou thread interne en développement.
Toutes les tâches sont idempotentes.
"""
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import Order, utcnow
from . import orders as order_service
from . import quotes, settings
from .notifications import notify


def expire_holds():
    """Libère les créneaux dont le blocage temporaire de paiement a expiré (commande annulée par le système)."""
    n = 0
    now = utcnow()
    for o in Order.query.filter(Order.status == "pending_payment", Order.hold_expires_at.isnot(None),
                                Order.hold_expires_at < now).all():
        if (o.paid_xof or 0) > 0:
            continue
        try:
            order_service.cancel(o, by="system", reason="Blocage expiré : paiement non effectué dans le délai imparti.")
            n += 1
        except order_service.OrderError:
            pass
    return n


def advance_orders(today=None):
    today = today or date.today()
    started = 0
    for o in Order.query.filter(Order.status == "confirmed", Order.start_date <= today).all():
        o.status = "in_progress"
        started += 1
    return started


def reminders(today=None):
    today = today or date.today()
    n = 0
    # rappel de l'événement à J-3
    for o in Order.query.filter(Order.status.in_(("confirmed",)), Order.start_date == today + timedelta(days=3)).all():
        for u, url in ((o.buyer, f"/espace/commandes/{o.id}"), (o.vendor.user, f"/pro/commandes/{o.id}")):
            notify(u, "event_reminder", f"Rappel : prestation dans 3 jours — {o.reference}",
                   f"Prévue le {o.start_date:%d/%m/%Y}.", url, dedupe_key=f"remind3:{o.id}:{u.id}")
            n += 1
    # rappel d'échéance de solde
    for o in Order.query.filter(Order.status.in_(("confirmed", "in_progress", "pending_confirmation")),
                                Order.balance_due_date.isnot(None), Order.balance_due_date <= today + timedelta(days=2),
                                Order.balance_due_date >= today).all():
        if o.remaining_xof > 0:
            notify(o.buyer, "balance_due", f"Solde à régler avant le {o.balance_due_date:%d/%m/%Y} — {o.reference}",
                   f"Reste à payer : {o.remaining_xof} FCFA.", f"/espace/commandes/{o.id}", critical=True,
                   dedupe_key=f"balance:{o.id}")
            n += 1
    return n


def request_reviews():
    n = 0
    for o in Order.query.filter(Order.status == "completed", Order.review_requested.is_(False)).all():
        notify(o.buyer, "review_request", f"Donnez votre avis sur {o.vendor.business_name}",
               "Votre retour aide les autres organisateurs.", f"/espace/commandes/{o.id}", dedupe_key=f"review:{o.id}")
        o.review_requested = True
        n += 1
    return n


def release_funds():
    delay = settings.get_int("payout_delay_days", 2)
    limit = utcnow() - timedelta(days=delay)
    n = 0
    for o in Order.query.filter(Order.status == "completed", Order.funds_released.is_(False),
                                Order.completed_at <= limit).all():
        order_service.release_funds(o)
        n += 1
    return n


def run_all():
    result = {
        "holds_expired": expire_holds(),
        "orders_started": advance_orders(),
        "quotes_expired": quotes.expire_due(),
        "quote_reminders": quotes.remind_expiring(),
        "reminders": reminders(),
        "reviews_requested": request_reviews(),
        "funds_released": release_funds(),
    }
    db.session.commit()
    return result
