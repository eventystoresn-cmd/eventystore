"""Calcul des prix côté serveur (le prix final n'est JAMAIS pris du navigateur)."""
from datetime import date

from ..models import CartItem, Offer, OfferOption, Promo
from ..utils import fmt_xof


def quantity_meaning(offer):
    """Ce que représente `quantité` pour l'unité tarifaire de l'offre."""
    return {"person": "Nombre de personnes", "hour": "Nombre d'heures", "day": "Quantité (par jour)",
            "night": "Quantité (par nuit)", "unit": "Quantité", "lot": "Nombre de lots",
            "event": "Quantité", "package": "Quantité"}.get(offer.price_unit, "Quantité")


def n_days(start, end):
    return (end - start).days + 1 if start and end else 1


def unit_price_for(offer, qty):
    """Tarif unitaire, avec paliers dégressifs éventuels."""
    price = offer.price_xof or 0
    for tier in sorted(offer.tiers or [], key=lambda t: int(t.get("min_qty", 0))):
        try:
            if qty >= int(tier["min_qty"]):
                price = int(tier["unit_price"])
        except (KeyError, ValueError, TypeError):
            continue
    return price


def compute(offer, start, end, qty=1, guests=None, option_ids=None, event_region=None, promo=None):
    """Retourne un dict détaillé (lignes, sous-total, frais, remise, total, acompte)."""
    qty = max(1, int(qty or 1))
    days = n_days(start, end)
    lines = []
    unit = offer.price_unit or "event"
    if unit == "person":
        qty = max(qty, offer.min_quantity or 1)
        mult = qty
        label_qty = f"{qty} pers."
    elif unit in ("day", "night"):
        mult = qty * days
        label_qty = f"{qty} × {days} {'nuit(s)' if unit == 'night' else 'jour(s)'}"
    elif unit in ("event", "package"):
        qty = 1
        mult = 1
        label_qty = "1"
    else:
        mult = qty
        label_qty = str(qty)
    unit_price = unit_price_for(offer, mult)
    base = unit_price * mult
    lines.append({"kind": "item", "label": offer.title, "quantity": mult, "unit": _unit_label(unit),
                  "unit_price_xof": unit_price, "total_xof": base, "detail": label_qty})
    option_total = 0
    chosen = []
    if option_ids:
        opts = OfferOption.query.filter(OfferOption.offer_id == offer.id, OfferOption.id.in_(option_ids)).all()
        for o in opts:
            if o.price_type == "per_person":
                q = guests or qty
            elif o.price_type == "per_unit":
                q = mult
            else:
                q = 1
            t = (o.price_xof or 0) * q
            option_total += t
            chosen.append(o.id)
            lines.append({"kind": "option", "label": f"Option : {o.name}", "quantity": q,
                          "unit": "", "unit_price_xof": o.price_xof or 0, "total_xof": t, "detail": ""})
    subtotal = base + option_total

    discount = 0
    promo_note = None
    if promo:
        if promo.percent:
            discount = round(subtotal * promo.percent / 100)
        elif promo.amount_xof:
            discount = min(promo.amount_xof, subtotal)
        promo_note = promo.code

    fees = 0
    if offer.delivery_fee_xof and offer.offer_type in ("rental", "sale"):
        fees += offer.delivery_fee_xof
        lines.append({"kind": "delivery", "label": "Frais de livraison", "quantity": 1, "unit": "",
                      "unit_price_xof": offer.delivery_fee_xof, "total_xof": offer.delivery_fee_xof, "detail": ""})
    if offer.travel_fee_xof and event_region and offer.region and event_region != offer.region:
        fees += offer.travel_fee_xof
        lines.append({"kind": "travel", "label": "Frais de déplacement", "quantity": 1, "unit": "",
                      "unit_price_xof": offer.travel_fee_xof, "total_xof": offer.travel_fee_xof, "detail": ""})
    total = max(0, subtotal - discount) + fees
    pct = offer.deposit_percent if offer.deposit_percent is not None else 30
    deposit = round(total * pct / 100) if pct else 0
    return {
        "lines": lines, "subtotal_xof": subtotal, "discount_xof": discount, "fees_xof": fees,
        "total_xof": total, "deposit_percent": pct, "deposit_xof": min(deposit, total),
        "caution_xof": offer.caution_xof or 0, "quantity": qty, "multiplier": mult, "days": days,
        "option_ids": chosen, "promo": promo_note, "unit_price_xof": unit_price,
    }


def _unit_label(unit):
    return {"person": "pers.", "hour": "h", "day": "jour", "night": "nuit", "unit": "u.", "lot": "lot",
            "event": "événement", "package": "forfait"}.get(unit, "")


def price_label(offer):
    """Texte de prix avec unité et devise (AC-02)."""
    from ..models import PRICE_UNITS
    if offer.price_model == "quote" or not offer.price_xof:
        return "Sur devis"
    unit = PRICE_UNITS.get(offer.price_unit, "")
    if offer.price_model == "range" and offer.price_max_xof:
        return f"{fmt_xof(offer.price_xof, False)} – {fmt_xof(offer.price_max_xof)} {unit}".strip()
    prefix = "À partir de " if offer.price_model == "from" else ""
    return f"{prefix}{fmt_xof(offer.price_xof)} {unit}".strip()


def find_promo(code, vendor_id, offer_id=None, today=None):
    """Retourne (promo, erreur)."""
    if not code:
        return None, None
    today = today or date.today()
    p = Promo.query.filter(Promo.vendor_id == vendor_id, Promo.code == code.strip().upper(), Promo.active.is_(True)).first()
    if not p:
        return None, "Code promotionnel inconnu pour ce prestataire."
    if p.valid_from and today < p.valid_from or p.valid_to and today > p.valid_to:
        return None, "Ce code promotionnel n'est plus valide."
    if p.max_uses is not None and (p.used or 0) >= p.max_uses:
        return None, "Ce code promotionnel a atteint sa limite d'utilisation."
    if p.offer_id and offer_id and p.offer_id != offer_id:
        return None, "Ce code ne s'applique pas à cette offre."
    return p, None
