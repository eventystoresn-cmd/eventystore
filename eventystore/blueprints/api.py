"""API JSON interne v1 (même origine, cookies de session + en-tête X-CSRFToken pour les écritures).

Documentée dans docs/API.md. Utilisée par le front (calendrier, estimation de prix, suggestions, notifications).
"""
from flask import Blueprint, jsonify, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import Category, Event, Notification, Offer, OfferOption, utcnow
from ..services import availability, messaging, notifications, pricing, search as search_service
from ..utils import fmt_xof, parse_date, parse_int

bp = Blueprint("api", __name__)


def offer_json(o, full=False):
    cover = o.cover
    d = {
        "id": o.id, "title": o.title, "url": url_for("public.offer", offer_id=o.id, slug=o.slug),
        "category": o.category.name, "vendor": {"name": o.vendor.business_name, "slug": o.vendor.slug, "verified": o.vendor.verified_badge},
        "city": o.city, "region": o.region, "price": {"model": o.price_model, "unit": o.price_unit, "amount_xof": o.price_xof,
                                                       "max_xof": o.price_max_xof, "currency": "XOF", "label": pricing.price_label(o)},
        "rating": o.rating[0], "reviews": o.rating[1], "bookable_direct": o.is_bookable_direct,
        "image": url_for("media.public_file", rel=(cover.thumb or cover.path)) if cover else url_for("media.placeholder", icon=o.category.root.icon),
    }
    if full:
        d.update(description=o.description, conditions=o.conditions, capacity=o.capacity, stock_qty=o.stock_qty,
                 deposit_percent=o.deposit_percent, caution_xof=o.caution_xof, cancellation_policy=o.cancellation_policy,
                 options=[{"id": x.id, "name": x.name, "price_xof": x.price_xof, "type": x.price_type, "required": x.required} for x in o.options])
    return d


@bp.route("/offers")
def offers():
    params = search_service.parse_params(request.args)
    items, total, pages, notes, page = search_service.search(params)
    return jsonify(items=[offer_json(o) for o in items], total=total, pages=pages, page=page, notes=notes)


@bp.route("/offers/<int:offer_id>")
def offer(offer_id):
    o = search_service.base_query().filter(Offer.id == offer_id).first()
    if o is None:
        return jsonify(error="not_found"), 404
    return jsonify(offer_json(o, full=True))


def _public_offer(offer_id):
    return search_service.base_query().filter(Offer.id == offer_id).first()


@bp.route("/offers/<int:offer_id>/availability")
def offer_availability(offer_id):
    o = _public_offer(offer_id)
    if o is None:
        return jsonify(error="not_found"), 404
    start, end = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    av = availability.check(o, start, end or start, parse_int(request.args.get("qty"), 1, 1))
    return jsonify(status=av.status, label=av.label, reason=av.reason, remaining=av.remaining)


@bp.route("/offers/<int:offer_id>/calendar")
def offer_calendar(offer_id):
    o = _public_offer(offer_id)
    if o is None:
        return jsonify(error="not_found"), 404
    from datetime import date
    today = date.today()
    y, m = parse_int(request.args.get("year"), today.year, 2020, 2100), parse_int(request.args.get("month"), today.month, 1, 12)
    return jsonify(year=y, month=m, days=availability.month_map(o, y, m))


@bp.route("/offers/<int:offer_id>/price", methods=["POST", "GET"])
def offer_price(offer_id):
    o = _public_offer(offer_id)
    if o is None:
        return jsonify(error="not_found"), 404
    src = request.values
    start, end = parse_date(src.get("start")), parse_date(src.get("end")) or parse_date(src.get("start"))
    if not start:
        return jsonify(error="start_required"), 400
    opt_ids = [int(i) for i in src.getlist("options") if i.isdigit()]
    region = None
    if current_user.is_authenticated and src.get("event_id"):
        ev = db.session.get(Event, parse_int(src.get("event_id"), 0))
        if ev and ev.buyer_id == current_user.id:
            region = ev.region
    promo, perr = pricing.find_promo(src.get("promo"), o.vendor_id, o.id)
    calc = pricing.compute(o, start, end, parse_int(src.get("quantity"), 1, 1), parse_int(src.get("guests"), None, 1), opt_ids, region, promo)
    av = availability.check(o, start, end, calc["quantity"])
    alternatives = []
    if av.status == "unavailable":
        alternatives = _alternative_dates(o, start)
    return jsonify(alternatives=alternatives, lines=calc["lines"], subtotal_xof=calc["subtotal_xof"], discount_xof=calc["discount_xof"], fees_xof=calc["fees_xof"],
                   total_xof=calc["total_xof"], deposit_xof=calc["deposit_xof"], deposit_percent=calc["deposit_percent"],
                   caution_xof=calc["caution_xof"], promo_error=perr, availability={"status": av.status, "label": av.label, "reason": av.reason},
                   total_label=fmt_xof(calc["total_xof"]), deposit_label=fmt_xof(calc["deposit_xof"]), currency="XOF")


def _alternative_dates(offer, start, limit=4):
    """Prochaines dates libres (≤ 90 jours après la date demandée) pour proposer une alternative."""
    from datetime import date, timedelta
    found, y, m = [], start.year, start.month
    for _ in range(4):
        for key, state in sorted(availability.month_map(offer, y, m).items()):
            if state == "free" and key > start.isoformat() and key <= (start + timedelta(days=90)).isoformat():
                found.append(key)
                if len(found) >= limit:
                    return found
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return found


@bp.route("/categories")
def categories():
    def node(c):
        return {"id": c.id, "name": c.name, "slug": c.slug, "icon": c.icon, "children": [node(x) for x in c.children if x.active]}
    return jsonify([node(c) for c in Category.query.filter_by(parent_id=None, active=True).order_by(Category.position)])


@bp.route("/suggest")
def suggest():
    return jsonify(suggestions=search_service.suggestions(request.args.get("q", "")))


@bp.route("/me/unread")
def unread():
    if not current_user.is_authenticated:
        return jsonify(notifications=0, messages=0)
    return jsonify(notifications=notifications.unread_count(current_user), messages=messaging.unread_total(current_user))


@bp.route("/notifications")
def notifications_list():
    if not current_user.is_authenticated:
        return jsonify(error="unauthorized"), 401
    rows = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(8).all()
    return jsonify(items=[{"id": n.id, "title": n.title, "body": n.body, "url": url_for("account.notification_open", nid=n.id),
                           "read": bool(n.read_at), "critical": n.critical, "at": n.created_at.isoformat() + "Z"} for n in rows],
                   unread=notifications.unread_count(current_user))
