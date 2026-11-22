"""Disponibilités : calendrier prestataire, stock loué, tampons de préparation/retour, blocages temporaires.

Granularité V1 : le jour calendaire (voir docs/DECISIONS.md). Un engagement occupe [début, fin] bornes
incluses ; les tampons avant/après de l'offre étendent la plage bloquée.

Règles (cahier §7) :
- une demande de devis ne bloque rien ;
- une commande en attente de paiement bloque le créneau tant que `hold_expires_at` n'est pas dépassé ;
- une commande en attente de confirmation / confirmée / en cours / en litige bloque ressource ou quantité ;
- pas de sur-réservation : quantité demandée + quantité déjà réservée <= stock (ou capacité simultanée).
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import ACTIVE_ORDER_STATUSES, AvailabilityBlock, Order, OrderItem
from ..utils import daterange


@dataclass
class Availability:
    status: str  # available | unavailable | to_confirm
    label: str
    reason: str = ""
    remaining: int = None

    @property
    def ok(self):
        return self.status in ("available", "to_confirm")


LABELS = {"available": "Disponible", "unavailable": "Indisponible", "to_confirm": "Disponibilité à confirmer"}


def _live_order_filter(now=None):
    now = now or datetime.utcnow()
    return db.or_(
        Order.status.in_(ACTIVE_ORDER_STATUSES),
        db.and_(Order.status == "pending_payment", Order.hold_expires_at.isnot(None), Order.hold_expires_at > now),
    )


def _blocked_days(vendor_id, offer_id, start, end):
    """Ensemble des jours bloqués manuellement par le prestataire dans [start, end]."""
    rows = AvailabilityBlock.query.filter(
        AvailabilityBlock.vendor_id == vendor_id,
        db.or_(AvailabilityBlock.offer_id.is_(None), AvailabilityBlock.offer_id == offer_id),
        db.or_(AvailabilityBlock.weekly.is_(True),
               db.and_(AvailabilityBlock.start_date <= end, AvailabilityBlock.end_date >= start)),
    ).all()
    days = set()
    for b in rows:
        if b.weekly:
            for d in daterange(start, end):
                if d >= b.start_date and d.weekday() == b.start_date.weekday():
                    days.add(d)
        else:
            for d in daterange(max(b.start_date, start), min(b.end_date, end)):
                days.add(d)
    return days


def _usage_by_day(offer, start, end, exclude_order_id=None):
    """Quantité réservée par jour (stock) ou nombre de prestations (services) dans [start, end]."""
    lo, hi = start - timedelta(days=offer.buffer_after_days or 0), end + timedelta(days=offer.buffer_before_days or 0)
    q = (db.session.query(Order, OrderItem)
         .join(OrderItem, OrderItem.order_id == Order.id)
         .filter(OrderItem.offer_id == offer.id, OrderItem.kind == "item", Order.start_date <= hi,
                 Order.end_date >= lo, _live_order_filter()))
    if exclude_order_id:
        q = q.filter(Order.id != exclude_order_id)
    usage = defaultdict(int)
    seen = set()
    for order, item in q.all():
        if order.id in seen:  # une commande ne compte qu'une fois par offre
            continue
        seen.add(order.id)
        qty = _item_qty(order, item) if offer.uses_stock else 1
        # un engagement occupe aussi ses tampons
        o_start = order.start_date - timedelta(days=offer.buffer_before_days or 0)
        o_end = order.end_date + timedelta(days=offer.buffer_after_days or 0)
        for d in daterange(max(o_start, start), min(o_end, end)):
            usage[d] += qty
    return usage


def _item_qty(order, item):
    return int((item.options or {}).get("qty") or (order.snapshot or {}).get("qty") or 1)


def capacity_of(offer):
    return (offer.stock_qty or 0) if offer.uses_stock else max(1, offer.concurrent_capacity or 1)


def check(offer, start, end, qty=1, exclude_order_id=None, today=None, enforce_notice=True):
    today = today or date.today()
    if not start or not end or end < start:
        return Availability("to_confirm", LABELS["to_confirm"], "Choisissez des dates valides.")
    if enforce_notice and start < today:
        return Availability("unavailable", LABELS["unavailable"], "Cette date est passée.")
    notice = offer.min_notice_days or 0
    if enforce_notice and start < today + timedelta(days=notice):
        return Availability("unavailable", LABELS["unavailable"],
                            f"Délai minimum de réservation : {notice} jour(s) avant la prestation.")
    # fenêtre à contrôler = plage demandée + tampons
    lo, hi = start - timedelta(days=offer.buffer_before_days or 0), end + timedelta(days=offer.buffer_after_days or 0)
    if _blocked_days(offer.vendor_id, offer.id, start, end):
        return Availability("unavailable", LABELS["unavailable"], "Le prestataire est indisponible sur tout ou partie de cette période.")
    cap = capacity_of(offer)
    need = int(qty or 1) if offer.uses_stock else 1
    if offer.uses_stock and need > cap:
        return Availability("unavailable", LABELS["unavailable"], f"Quantité demandée supérieure au stock total ({cap}).", 0)
    usage = _usage_by_day(offer, lo, hi, exclude_order_id)
    peak = max(usage.values()) if usage else 0
    remaining = cap - peak
    if remaining < need:
        why = "Stock insuffisant sur cette période" if offer.uses_stock else "Déjà réservé sur cette période"
        if offer.uses_stock:
            why += f" ({max(remaining, 0)} disponible(s))."
        else:
            why += "."
        return Availability("unavailable", LABELS["unavailable"], why, max(remaining, 0))
    if offer.price_model == "quote":
        return Availability("to_confirm", LABELS["to_confirm"],
                            "Offre sur devis : le prestataire doit confirmer explicitement.", remaining)
    return Availability("available", LABELS["available"], "", remaining)


def month_map(offer, year, month, qty=1):
    """Etat de chaque jour du mois : free | busy | blocked | partial | past."""
    first = date(year, month, 1)
    last = (date(year + (month == 12), (month % 12) + 1, 1)) - timedelta(days=1)
    blocked = _blocked_days(offer.vendor_id, offer.id, first, last)
    usage = _usage_by_day(offer, first - timedelta(days=offer.buffer_before_days or 0),
                          last + timedelta(days=offer.buffer_after_days or 0))
    cap = capacity_of(offer)
    today = date.today()
    notice_end = today + timedelta(days=offer.min_notice_days or 0)
    out = {}
    for d in daterange(first, last):
        if d < today or d < notice_end:
            out[d.isoformat()] = "past"
        elif d in blocked:
            out[d.isoformat()] = "blocked"
        else:
            u = usage.get(d, 0)
            if u >= cap:
                out[d.isoformat()] = "busy"
            elif u > 0 and offer.uses_stock:
                out[d.isoformat()] = "partial"
            else:
                out[d.isoformat()] = "free"
    return out


def unavailable_offer_ids(offers, start, end):
    """Filtrage de recherche par date : renvoie les ids d'offres incompatibles (2 requêtes groupées)."""
    if not offers:
        return set()
    today = date.today()
    ids = [o.id for o in offers]
    vendor_ids = {o.vendor_id for o in offers}
    bad = set()
    blocks = AvailabilityBlock.query.filter(
        AvailabilityBlock.vendor_id.in_(vendor_ids),
        db.or_(AvailabilityBlock.weekly.is_(True),
               db.and_(AvailabilityBlock.start_date <= end, AvailabilityBlock.end_date >= start))).all()
    per_vendor = defaultdict(list)
    for b in blocks:
        per_vendor[b.vendor_id].append(b)
    days = list(daterange(start, end))
    for o in offers:
        if start < today + timedelta(days=o.min_notice_days or 0):
            bad.add(o.id)
            continue
        for b in per_vendor.get(o.vendor_id, []):
            if b.offer_id not in (None, o.id):
                continue
            if b.weekly:
                if any(d >= b.start_date and d.weekday() == b.start_date.weekday() for d in days):
                    bad.add(o.id)
            elif b.start_date <= end and b.end_date >= start:
                bad.add(o.id)
    maxbuf = 31
    rows = (db.session.query(Order, OrderItem).join(OrderItem, OrderItem.order_id == Order.id)
            .filter(OrderItem.offer_id.in_(ids), OrderItem.kind == "item", Order.start_date <= end + timedelta(days=maxbuf),
                    Order.end_date >= start - timedelta(days=maxbuf), _live_order_filter()).all())
    by_offer = defaultdict(list)
    for order, item in rows:
        by_offer[item.offer_id].append((order, item))
    for o in offers:
        if o.id in bad:
            continue
        lo, hi = start - timedelta(days=o.buffer_before_days or 0), end + timedelta(days=o.buffer_after_days or 0)
        usage = defaultdict(int)
        seen = set()
        for order, item in by_offer.get(o.id, []):
            if order.id in seen:
                continue
            seen.add(order.id)
            qty = _item_qty(order, item) if o.uses_stock else 1
            s = order.start_date - timedelta(days=o.buffer_before_days or 0)
            e = order.end_date + timedelta(days=o.buffer_after_days or 0)
            for d in daterange(max(s, lo), min(e, hi)):
                usage[d] += qty
        if usage and capacity_of(o) - max(usage.values()) < 1:
            bad.add(o.id)
    return bad
