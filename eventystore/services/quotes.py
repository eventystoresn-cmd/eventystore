"""Demandes de devis, devis versionnés et négociation structurée (cahier §8).

Règles :
- une demande de devis n'est pas une réservation ferme et ne bloque aucun créneau ;
- aucune modification silencieuse : chaque envoi crée une nouvelle version horodatée (AC-05) ;
- un devis expiré ne peut plus être accepté (AC-06) ;
- un devis accepté devient la version contractuelle ; les autres versions restent en historique ;
- nombre de contre-propositions acheteur limité (paramètre `max_buyer_counters`).
"""
from datetime import date, datetime, timedelta

from ..extensions import db
from ..models import Quote, QuoteLine, QuoteRequest, utcnow
from ..utils import fmt_xof, new_reference
from . import audit, settings
from .notifications import notify


class QuoteError(Exception):
    pass


OPEN_STATES = ("open", "quoted", "negotiating")


def _urls(req):
    return f"/espace/devis/{req.id}", f"/pro/devis/{req.id}"


def create_request(buyer, offer, vendor, data):
    req = QuoteRequest(reference=new_reference("DV"), buyer_id=buyer.id, vendor_id=vendor.id,
                       offer_id=offer.id if offer else None, **data)
    db.session.add(req)
    db.session.flush()
    b_url, v_url = _urls(req)
    notify(vendor.user, "quote_request", f"Nouvelle demande de devis — {req.reference}",
           f"{buyer.display_name} · {req.start_date:%d/%m/%Y}" + (f" · {offer.title}" if offer else ""), v_url,
           critical=True, dedupe_key=f"qr:{req.id}")
    return req


def _compute(lines):
    sub = sum(l["total_xof"] for l in lines if l["kind"] not in ("travel", "delivery", "install", "staff", "caution", "other"))
    fees = sum(l["total_xof"] for l in lines if l["kind"] in ("travel", "delivery", "install", "staff", "other"))
    caution = sum(l["total_xof"] for l in lines if l["kind"] == "caution")  # la caution n'entre pas dans le total dû
    return sub, fees, caution


def parse_lines(form):
    """Lit les lignes du formulaire (listes parallèles). Retourne une liste de dicts normalisés."""
    labels = form.getlist("line_label")
    qtys = form.getlist("line_qty")
    units = form.getlist("line_unit")
    prices = form.getlist("line_price")
    discounts = form.getlist("line_discount")
    kinds = form.getlist("line_kind")
    out = []
    for i, label in enumerate(labels):
        label = (label or "").strip()
        if not label:
            continue
        try:
            q = float(str(qtys[i]).replace(",", ".")) if i < len(qtys) and qtys[i] else 1.0
            p = int(float(str(prices[i]).replace(" ", "").replace(" ", ""))) if i < len(prices) and prices[i] else 0
            d = int(float(str(discounts[i]).replace(" ", ""))) if i < len(discounts) and discounts[i] else 0
        except ValueError:
            raise QuoteError(f"Valeur numérique invalide à la ligne « {label} ».")
        if q <= 0 or p < 0 or d < 0:
            raise QuoteError(f"Quantité/prix invalides à la ligne « {label} ».")
        kind = kinds[i] if i < len(kinds) and kinds[i] in ("item", "travel", "delivery", "install", "staff", "caution", "other") else "item"
        total = max(0, round(q * p) - d)
        out.append({"kind": kind, "label": label[:200], "quantity": q, "unit": (units[i] if i < len(units) else "")[:30],
                    "unit_price_xof": p, "discount_xof": d, "total_xof": total})
    if not out:
        raise QuoteError("Ajoutez au moins une ligne au devis.")
    return out


def build_quote(req, author, role, lines, *, valid_until, deposit_percent, balance_days, description="", exclusions="",
                conditions="", policy="moderate", note="", start=None, end=None, quantity=None, send=True):
    if req.status not in OPEN_STATES:
        raise QuoteError("Cette demande n'accepte plus de nouvelle proposition.")
    if not valid_until or valid_until < date.today():
        raise QuoteError("La date de validité doit être dans le futur.")
    sub, fees, caution = _compute(lines)
    total = sub + fees
    if total <= 0:
        raise QuoteError("Le total du devis doit être supérieur à zéro.")
    if not 0 <= deposit_percent <= 100:
        raise QuoteError("L'acompte doit être compris entre 0 et 100 %.")
    version = (req.quotes[-1].version + 1) if req.quotes else 1
    # l'ancienne version envoyée est remplacée (jamais modifiée ni supprimée)
    for q in req.quotes:
        if q.status == "sent":
            q.status = "superseded"
    quote = Quote(request_id=req.id, version=version, reference=f"{req.reference}-v{version}", author_id=author.id,
                  author_role=role, status="sent" if send else "draft", start_date=start or req.start_date,
                  end_date=end or req.end_date, quantity=quantity or req.quantity, subtotal_xof=sub, fees_xof=fees,
                  total_xof=total, deposit_percent=deposit_percent, deposit_xof=round(total * deposit_percent / 100),
                  balance_due_days_before=balance_days, valid_until=valid_until, description=description,
                  exclusions=exclusions, conditions=conditions, cancellation_policy=policy, note=note,
                  issued_at=utcnow() if send else None)
    for l in lines:
        quote.lines.append(QuoteLine(**l))
    req.quotes.append(quote)
    if send:
        _after_send(req, quote, role)
    db.session.flush()
    return quote


def _after_send(req, quote, role):
    b_url, v_url = _urls(req)
    if role == "vendor":
        req.status = "quoted" if req.counter_count == 0 else "negotiating"
        if req.first_response_at is None:
            req.first_response_at = utcnow()
        notify(req.buyer, "quote_received", f"Devis reçu — {quote.reference}",
               f"{req.vendor.business_name} · {fmt_xof(quote.total_xof)}", b_url, dedupe_key=f"qsent:{quote.id}")
    else:
        req.status = "negotiating"
        notify(req.vendor.user, "quote_counter", f"Contre-proposition reçue — {req.reference}",
               f"{req.buyer.display_name} propose {fmt_xof(quote.total_xof)}", v_url, critical=True,
               dedupe_key=f"qsent:{quote.id}")


def send_draft(quote):
    if quote.status != "draft":
        raise QuoteError("Ce devis n'est plus un brouillon.")
    req = quote.request
    for q in req.quotes:
        if q.status == "sent":
            q.status = "superseded"
    quote.status, quote.issued_at = "sent", utcnow()
    _after_send(req, quote, quote.author_role)


def buyer_counter(req, buyer, lines, **kw):
    limit = settings.get_int("max_buyer_counters", 5)
    if (req.counter_count or 0) >= limit:
        raise QuoteError(f"Limite de {limit} contre-propositions atteinte. Contactez le prestataire par message.")
    last = req.latest_quote
    if not last or last.status != "sent" or last.author_role != "vendor":
        raise QuoteError("Aucune proposition du prestataire à contrer.")
    if last.is_expired:
        raise QuoteError("Le devis a expiré. Demandez une réactualisation.")
    quote = build_quote(req, buyer, "buyer", lines, **kw)
    req.counter_count = (req.counter_count or 0) + 1
    return quote


def accept(quote, by_user):
    """Acceptation d'une version. L'acheteur accepte une proposition du prestataire ; le prestataire peut accepter
    une contre-proposition de l'acheteur."""
    req = quote.request
    if quote.status != "sent":
        raise QuoteError("Cette version n'est plus ouverte à l'acceptation.")
    if quote.is_expired:
        quote.status = "expired"
        raise QuoteError("Ce devis a expiré et ne peut plus être accepté (réactualisation à demander).")
    by_buyer = by_user.id == req.buyer_id
    if by_buyer and quote.author_role != "vendor":
        raise QuoteError("Vous ne pouvez accepter qu'une proposition du prestataire.")
    if not by_buyer and quote.author_role != "buyer":
        raise QuoteError("Vous ne pouvez accepter qu'une contre-proposition de l'acheteur.")
    quote.status = "accepted"
    req.status = "accepted"
    for q in req.quotes:
        if q.id != quote.id and q.status in ("sent", "draft"):
            q.status = "superseded" if q.status == "sent" else "withdrawn"
    b_url, v_url = _urls(req)
    other = req.vendor.user if by_buyer else req.buyer
    notify(other, "quote_accepted", f"Devis accepté — {quote.reference}",
           f"Version {quote.version} · {fmt_xof(quote.total_xof)}", v_url if by_buyer else b_url, critical=True,
           dedupe_key=f"qacc:{quote.id}")
    audit.log("quote.accept", quote, by=by_user.id)


def decline(req, by_user, reason=""):
    if req.status not in OPEN_STATES:
        raise QuoteError("Cette demande est déjà clôturée.")
    for q in req.quotes:
        if q.status in ("sent", "draft"):
            q.status = "declined" if q.status == "sent" else "withdrawn"
    req.status = "declined"
    b_url, v_url = _urls(req)
    by_buyer = by_user.id == req.buyer_id
    notify(req.vendor.user if by_buyer else req.buyer, "quote_declined", f"Demande refusée — {req.reference}",
           reason or "", v_url if by_buyer else b_url, dedupe_key=f"qdec:{req.id}")


def cancel_request(req):
    if req.status not in OPEN_STATES:
        raise QuoteError("Cette demande ne peut plus être annulée.")
    for q in req.quotes:
        if q.status in ("sent", "draft"):
            q.status = "withdrawn"
    req.status = "cancelled"
    notify(req.vendor.user, "quote_cancelled", f"Demande annulée — {req.reference}", "", _urls(req)[1],
           dedupe_key=f"qcan:{req.id}")


def withdraw(quote):
    """Le prestataire retire un devis envoyé avant acceptation."""
    if quote.status not in ("sent", "draft"):
        raise QuoteError("Seul un devis non accepté peut être retiré.")
    quote.status = "withdrawn"


def expire_due(today=None):
    """Job : marque les devis échus et les demandes sans réponse acceptable. Retourne le nombre de devis expirés."""
    today = today or date.today()
    n = 0
    for q in Quote.query.filter(Quote.status == "sent", Quote.valid_until < today).all():
        q.status = "expired"
        req = q.request
        if req.status in OPEN_STATES and not any(x.status == "sent" for x in req.quotes if x.id != q.id):
            req.status = "expired"
        b_url, v_url = _urls(req)
        notify(req.buyer, "quote_expired", f"Devis expiré — {q.reference}", "Vous pouvez demander une réactualisation.",
               b_url, dedupe_key=f"qexp:{q.id}:b")
        notify(req.vendor.user, "quote_expired", f"Devis expiré — {q.reference}", "", v_url, dedupe_key=f"qexp:{q.id}:v")
        n += 1
    return n


def remind_expiring(today=None):
    """Job : alerte les deux parties 2 jours avant l'expiration d'un devis."""
    today = today or date.today()
    n = 0
    for q in Quote.query.filter(Quote.status == "sent", Quote.valid_until == today + timedelta(days=2)).all():
        b_url, v_url = _urls(q.request)
        notify(q.request.buyer, "quote_expiring", f"Devis bientôt expiré — {q.reference}", f"Valable jusqu'au {q.valid_until:%d/%m/%Y}.",
               b_url, dedupe_key=f"qexp2:{q.id}:b")
        notify(q.request.vendor.user, "quote_expiring", f"Devis bientôt expiré — {q.reference}", "", v_url,
               dedupe_key=f"qexp2:{q.id}:v")
        n += 1
    return n


def diff_with_previous(quote):
    """Comparaison ligne à ligne avec la version précédente (pour la timeline)."""
    prev = [q for q in quote.request.quotes if q.version < quote.version]
    if not prev:
        return []
    old = {l.label: l for l in prev[-1].lines}
    new = {l.label: l for l in quote.lines}
    out = []
    for label, l in new.items():
        o = old.get(label)
        if o is None:
            out.append(("added", label, None, l.total_xof))
        elif o.total_xof != l.total_xof or o.quantity != l.quantity:
            out.append(("changed", label, o.total_xof, l.total_xof))
    for label, o in old.items():
        if label not in new:
            out.append(("removed", label, o.total_xof, None))
    return out
