"""Cycle de vie des commandes/réservations (cahier §8.5, §9, §10).

États : pending_payment -> pending_confirmation -> confirmed -> in_progress -> completed ; cancelled ; dispute.
Règle de confirmation paramétrable (`confirm_rule`) : « deposit » (confirmation automatique dès l'acompte)
ou « vendor_and_deposit » (acompte payé ET confirmation du prestataire).
"""
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import (CartItem, LedgerEntry, Order, OrderItem, Promo, Payment, QuoteLine, utcnow)
from ..utils import fmt_xof, new_reference
from . import audit, availability, finance, payments, pricing, settings
from .notifications import notify


class OrderError(Exception):
    pass


def _order_url(order, role):
    return f"/espace/commandes/{order.id}" if role == "buyer" else f"/pro/commandes/{order.id}"


def _notify_both(order, kind, title, body="", critical=False, key=None):
    notify(order.buyer, kind, title, body, _order_url(order, "buyer"), critical, f"{key}:{order.id}:b" if key else None)
    notify(order.vendor.user, kind, title, body, _order_url(order, "vendor"), critical, f"{key}:{order.id}:v" if key else None)


# ------------------------------------------------------------------ création depuis le panier
def create_orders_from_cart(user, cart_items, promo_codes=None):
    """Une commande par prestataire (ventilation multi-prestataires, cahier §9). Recalcul 100 % serveur.

    Retourne la liste des commandes créées. Lève OrderError si une ligne est indisponible/invalide.
    """
    promo_codes = promo_codes or {}
    by_vendor = {}
    for ci in cart_items:
        by_vendor.setdefault(ci.offer.vendor_id, []).append(ci)
    created = []
    hold_min = settings.get_int("hold_minutes", 15)
    for vendor_id, items in by_vendor.items():
        vendor = items[0].offer.vendor
        order = Order(reference=new_reference("EV"), buyer_id=user.id, vendor_id=vendor_id, source="cart",
                      status="pending_payment", start_date=min(i.start_date for i in items),
                      end_date=max(i.end_date for i in items), event_id=items[0].event_id,
                      confirm_rule=settings.get("confirm_rule"), hold_expires_at=utcnow() + timedelta(minutes=hold_min))
        subtotal = fees = discount = caution = 0
        promo_used = None
        policies = []
        conds = []
        order_items = []
        for ci in items:
            offer = ci.offer
            if offer.status != "active" or not vendor.is_validated:
                raise OrderError(f"« {offer.title} » n'est plus disponible à la réservation.")
            if not offer.is_bookable_direct:
                raise OrderError(f"« {offer.title} » nécessite une demande de devis.")
            av = availability.check(offer, ci.start_date, ci.end_date, ci.quantity)
            if not av.ok or av.status != "available":
                raise OrderError(f"« {offer.title} » : {av.reason or av.label}")
            promo, perr = pricing.find_promo(promo_codes.get(vendor_id), vendor_id, offer.id)
            if perr and promo_codes.get(vendor_id):
                raise OrderError(perr)
            region = ci.event.region if ci.event else None
            calc = pricing.compute(offer, ci.start_date, ci.end_date, ci.quantity, ci.guests, ci.option_ids, region, promo)
            if promo and promo_used is None:
                promo_used = promo
            subtotal += calc["subtotal_xof"]
            fees += calc["fees_xof"]
            discount += calc["discount_xof"]
            caution += calc["caution_xof"]
            policies.append(offer.cancellation_policy)
            if offer.conditions:
                conds.append(f"{offer.title} : {offer.conditions}")
            for line in calc["lines"]:
                order_items.append(OrderItem(offer_id=offer.id, title=line["label"], kind=line["kind"],
                                             quantity=line["quantity"], unit=line["unit"],
                                             unit_price_xof=line["unit_price_xof"], total_xof=line["total_xof"],
                                             options={"detail": line.get("detail", ""), "qty": calc["quantity"]}))
        total = subtotal - discount + fees
        order.subtotal_xof, order.fees_xof, order.discount_xof, order.total_xof = subtotal, fees, discount, total
        order.caution_xof = caution
        order.deposit_xof = _deposit_from_items(items, total)
        order.policy = _strictest(policies)
        order.conditions_snapshot = "\n".join(conds)
        order.commission_bp = finance.commission_bp_for(vendor, items[0].offer.category_id, items[0].offer.offer_type)
        order.commission_xof = round(total * order.commission_bp / 10000)
        order.balance_due_date = order.start_date - timedelta(days=settings.get_int("balance_due_days_before", 3))
        order.snapshot = {"source": "cart", "offers": [i.offer_id for i in items], "taken_at": utcnow().isoformat()}
        if promo_used:
            order.promo_code = promo_used.code
            promo_used.used = (promo_used.used or 0) + 1
        order.items = order_items
        db.session.add(order)
        db.session.flush()
        created.append(order)
        for ci in items:
            db.session.delete(ci)
    return created


def _deposit_from_items(items, total):
    pcts = [i.offer.deposit_percent or 0 for i in items]
    pct = max(pcts) if pcts else 0
    return min(total, round(total * pct / 100))


_STRICT_ORDER = {"flexible": 0, "moderate": 1, "strict": 2}


def _strictest(policies):
    return max(policies or ["moderate"], key=lambda p: _STRICT_ORDER.get(p, 1))


# ------------------------------------------------------------------ création depuis un devis accepté
def create_order_from_quote(quote):
    req = quote.request
    if quote.status != "accepted":
        raise OrderError("Seul un devis accepté peut être transformé en commande.")
    existing = Order.query.filter_by(quote_id=quote.id).filter(Order.status != "cancelled").first()
    if existing:
        return existing
    offer = req.offer
    vendor = req.vendor
    start, end = quote.start_date or req.start_date, quote.end_date or req.end_date
    if offer is not None:
        av = availability.check(offer, start, end, quote.quantity or 1)
        if av.status == "unavailable":
            raise OrderError(f"Créneau devenu indisponible : {av.reason}")
    online = offer.online_payment if offer is not None else True
    order = Order(reference=new_reference("EV"), buyer_id=req.buyer_id, vendor_id=req.vendor_id, event_id=req.event_id,
                  quote_id=quote.id, source="quote", start_date=start, end_date=end,
                  subtotal_xof=quote.subtotal_xof, fees_xof=quote.fees_xof, total_xof=quote.total_xof,
                  deposit_xof=quote.deposit_xof,
                  policy=quote.cancellation_policy or "moderate", conditions_snapshot=quote.conditions or "",
                  confirm_rule=settings.get("confirm_rule"),
                  hold_expires_at=utcnow() + timedelta(hours=settings.get_int("quote_order_hold_hours", 48)))
    order.commission_bp = finance.commission_bp_for(vendor, offer.category_id if offer else None,
                                                    offer.offer_type if offer else None)
    order.commission_xof = round(order.total_xof * order.commission_bp / 10000)
    order.balance_due_date = start - timedelta(days=quote.balance_due_days_before or 3)
    order.caution_xof = offer.caution_xof if offer else 0
    order.snapshot = {"source": "quote", "quote_id": quote.id, "quote_ref": quote.reference, "version": quote.version,
                      "description": quote.description, "exclusions": quote.exclusions,
                      "offline": not online, "qty": quote.quantity or 1, "taken_at": utcnow().isoformat()}
    for ln in quote.lines:
        order.items.append(OrderItem(offer_id=offer.id if offer else None, title=ln.label, kind=ln.kind,
                                     quantity=ln.quantity, unit=ln.unit, unit_price_xof=ln.unit_price_xof,
                                     total_xof=ln.total_xof))
    if not online:
        order.status, order.hold_expires_at = "pending_confirmation", None
    db.session.add(order)
    db.session.flush()
    notify(vendor.user, "order_created", f"Devis accepté — réservation {order.reference}",
           "L'acheteur a accepté votre devis. Le paiement de l'acompte est en attente.", _order_url(order, "vendor"),
           dedupe_key=f"order_created:{order.id}")
    return order


# ------------------------------------------------------------------ paiement -> confirmation
def amount_due_now(order, choice="deposit"):
    """Montant à payer : acompte (si défini et non payé) ou solde/total."""
    remaining = order.remaining_xof
    if choice == "full" or not order.deposit_xof:
        return remaining
    if (order.paid_xof or 0) < order.deposit_xof:
        return min(remaining, order.deposit_xof - (order.paid_xof or 0))
    return remaining


def on_payment_failed(order, pay):
    notify(order.buyer, "payment_failed", f"Paiement échoué — {order.reference}",
           "Votre paiement n'a pas abouti. Vous pouvez réessayer tant que la réservation est bloquée.",
           _order_url(order, "buyer"), critical=True, dedupe_key=f"payfail:{pay.id}")


def on_payment_succeeded(order, pay):
    notify(order.buyer, "payment_ok", f"Paiement reçu — {fmt_xof(pay.amount_xof)}",
           f"Commande {order.reference}. Reçu disponible dans votre espace.", _order_url(order, "buyer"),
           dedupe_key=f"payok:{pay.id}:b")
    notify(order.vendor.user, "payment_ok", f"Paiement reçu — {fmt_xof(pay.amount_xof)}",
           f"Commande {order.reference}.", _order_url(order, "vendor"), dedupe_key=f"payok:{pay.id}:v")
    if order.status != "pending_payment":
        return
    deposit_ok = (order.paid_xof or 0) >= (order.deposit_xof or 0) and (order.paid_xof or 0) > 0
    if not deposit_ok:
        return
    order.hold_expires_at = None  # le créneau est désormais « ferme » via le statut
    order.status = "pending_confirmation"
    notify(order.vendor.user, "order_pending", f"Nouvelle réservation à confirmer — {order.reference}",
           f"{order.buyer.display_name} · {fmt_xof(order.total_xof)}", _order_url(order, "vendor"), critical=True,
           dedupe_key=f"pending:{order.id}")
    if order.confirm_rule == "deposit":
        try:
            confirm(order, by_system=True)
        except OrderError:
            pass  # reste en attente de confirmation manuelle


def confirm(order, by_system=False):
    """Confirmation finale (prestataire ou règle auto). Revérifie la disponibilité (conflit => refus)."""
    if order.status not in ("pending_confirmation", "pending_payment"):
        raise OrderError("Cette réservation ne peut plus être confirmée.")
    snapshot_ok = (order.paid_xof or 0) >= (order.deposit_xof or 0) and ((order.paid_xof or 0) > 0 or (order.snapshot or {}).get("offline"))
    if not snapshot_ok:
        raise OrderError("L'acompte n'a pas encore été encaissé.")
    for item in order.items:
        if item.offer_id and item.kind == "item":
            offer = item.offer
            qty = int((item.options or {}).get("qty") or (order.snapshot or {}).get("qty") or 1) if offer.uses_stock else 1
            av = availability.check(offer, order.start_date, order.end_date, qty,
                                    exclude_order_id=order.id, enforce_notice=False)
            if av.status == "unavailable":
                raise OrderError(f"Conflit de disponibilité : {av.reason} Proposez une nouvelle date ou annulez.")
    order.status = "confirmed"
    order.confirmed_at = utcnow()
    if not by_system:
        order.vendor_confirmed_at = utcnow()
    _notify_both(order, "order_confirmed", f"Réservation confirmée — {order.reference}",
                 f"Du {order.start_date:%d/%m/%Y} au {order.end_date:%d/%m/%Y}.", critical=True, key="confirmed")


def vendor_refuse(order, reason):
    if order.status not in ("pending_confirmation", "pending_payment"):
        raise OrderError("Cette réservation ne peut plus être refusée.")
    cancel(order, by="vendor", reason=reason or "Refusée par le prestataire")


def start_if_due(order):
    if order.status == "confirmed" and order.start_date <= date.today():
        order.status = "in_progress"


def complete(order):
    if order.status not in ("confirmed", "in_progress"):
        raise OrderError("Seule une prestation confirmée peut être clôturée.")
    order.status = "completed"
    order.completed_at = utcnow()
    _notify_both(order, "order_completed", f"Prestation clôturée — {order.reference}",
                 "Vous pouvez désormais déposer un avis." , key="completed")


def cancel(order, by, reason="", admin_full_refund=False, manual_amount=None, actor=None):
    """Annule la commande et rembourse selon le barème. `by` : buyer | vendor | admin | system."""
    if order.status in ("cancelled", "completed"):
        raise OrderError("Cette commande ne peut plus être annulée.")
    pol_by = "vendor" if by == "vendor" else ("admin_full" if admin_full_refund else ("system" if by == "system" else "buyer"))
    quote = finance.refund_quote(order, by=pol_by)
    amount = quote["amount"] if manual_amount is None else max(0, min(int(manual_amount), quote["paid"]))
    refunded = 0
    if amount > 0:
        refunded = payments.refund_payment(order, amount, reason or quote["reason"], actor=actor, days_before=quote["days"])
    order.status = "cancelled"
    order.cancelled_at = utcnow()
    order.cancelled_by = by
    order.cancel_reason = reason
    order.hold_expires_at = None
    order.commission_xof = round(max(0, (order.paid_xof or 0) - (order.refunded_xof or 0)) * (order.commission_bp or 0) / 10000)
    _notify_both(order, "order_cancelled", f"Réservation annulée — {order.reference}",
                 (f"Remboursement : {fmt_xof(refunded)}. " if refunded else "Aucun remboursement. ") + (reason or ""),
                 critical=True, key="cancelled")
    audit.log("order.cancel", order, by=by, refunded=refunded, reason=reason, actor=actor)
    return {"refunded": refunded, **quote}


def mark_offline_payment(order, amount):
    """Paiement convenu hors plateforme (non protégé par EventyStore) déclaré par le prestataire."""
    pay = Payment(order_id=order.id, kind="deposit" if (order.paid_xof or 0) < (order.deposit_xof or 0) else "balance",
                  amount_xof=int(amount), method="offline", provider="offline",
                  external_ref="OFF-" + new_reference("P"), status="succeeded", paid_at=utcnow(),
                  net_vendor_xof=int(amount), commission_xof=0)
    db.session.add(pay)
    order.paid_xof = (order.paid_xof or 0) + int(amount)
    return pay


# ------------------------------------------------------------------ libération des fonds
def release_funds(order):
    """À la clôture + délai : le net du prestataire devient « disponible » pour retrait."""
    entries = LedgerEntry.query.filter_by(order_id=order.id, available=False).all()
    for e in entries:
        e.available = True
    order.funds_released = True
    return len(entries)
