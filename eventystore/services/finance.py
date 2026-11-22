"""Commissions, barème d'annulation/remboursement, soldes prestataires.

Les règles financières exactes (taux, délais, caution) sont à valider juridiquement (cahier §10, §24) :
tous les paramètres sont modifiables depuis l'administration, et un instantané est conservé par commande.
"""
from datetime import date

from ..extensions import db
from ..models import CANCELLATION_POLICIES, CommissionRule, LedgerEntry, Payout
from . import settings


def commission_bp_for(vendor, category_id=None, offer_type=None, today=None):
    """Priorité : surcharge prestataire > règle prestataire > catégorie > type d'offre > global."""
    today = today or date.today()
    if vendor is not None and vendor.commission_override_bp is not None:
        return vendor.commission_override_bp

    def active(q):
        return [r for r in q if r.active and (not r.valid_from or r.valid_from <= today) and (not r.valid_to or r.valid_to >= today)]

    rules = active(CommissionRule.query.all())
    for scope, ref in (("vendor", str(vendor.id) if vendor else None), ("category", str(category_id) if category_id else None),
                       ("offer_type", offer_type)):
        if ref is None:
            continue
        for r in rules:
            if r.scope == scope and r.ref == ref:
                return r.rate_bp
    for r in rules:
        if r.scope == "global":
            return r.rate_bp
    return settings.get_int("commission_default_bp", 1000)


def refund_percent(policy, days_before):
    """Part remboursable (0-100) selon la politique et le délai avant l'événement."""
    if policy not in CANCELLATION_POLICIES:
        policy = "moderate"
    if policy == "flexible":
        return 100 if days_before >= 7 else 50 if days_before >= 3 else 0
    if policy == "moderate":
        return 100 if days_before >= 14 else 50 if days_before >= 7 else 0
    return 50 if days_before >= 30 else 0


def refund_quote(order, today=None, by="buyer"):
    """Calcule le remboursement dû pour une annulation. Retourne dict(percent, amount, retained, days, reason)."""
    today = today or date.today()
    days = (order.start_date - today).days
    paid = max(0, (order.paid_xof or 0) - (order.refunded_xof or 0))
    if by in ("vendor", "admin_full", "system"):
        pct, why = 100, "Annulation par le prestataire / la plateforme : remboursement intégral."
    elif order.status in ("pending_payment", "pending_confirmation") and not order.vendor_confirmed_at:
        pct, why = 100, "Le prestataire n'avait pas encore confirmé : remboursement intégral."
    else:
        pct = refund_percent(order.policy, days)
        why = f"Politique « {CANCELLATION_POLICIES.get(order.policy, ('',''))[0]} », {days} jour(s) avant l'événement."
    amount = round(paid * pct / 100)
    return {"percent": pct, "amount": amount, "retained": paid - amount, "days": days, "reason": why, "paid": paid}


def vendor_balance(vendor_id):
    """Retourne dict(available, pending, withdrawn, total_credit)."""
    q = db.session.query
    pending = q(db.func.coalesce(db.func.sum(LedgerEntry.amount_xof), 0)).filter(
        LedgerEntry.vendor_id == vendor_id, LedgerEntry.available.is_(False)).scalar()
    available = q(db.func.coalesce(db.func.sum(LedgerEntry.amount_xof), 0)).filter(
        LedgerEntry.vendor_id == vendor_id, LedgerEntry.available.is_(True)).scalar()
    withdrawn = q(db.func.coalesce(db.func.sum(Payout.amount_xof), 0)).filter(
        Payout.vendor_id == vendor_id, Payout.status == "paid").scalar()
    in_progress = q(db.func.coalesce(db.func.sum(Payout.amount_xof), 0)).filter(
        Payout.vendor_id == vendor_id, Payout.status.in_(("requested", "in_review", "approved", "transferring"))).scalar()
    return {"available": int(available), "pending": int(pending), "withdrawn": int(withdrawn),
            "in_progress": int(in_progress)}
