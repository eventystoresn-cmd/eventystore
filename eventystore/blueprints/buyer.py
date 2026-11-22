"""Espace acheteur/organisateur : événements, panier, devis & négociation, commandes, avis."""
import csv
import io
from datetime import date, timedelta

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for)
from flask_login import current_user

from ..extensions import db
from ..models import (CANCELLATION_POLICIES, ACTIVE_ORDER_STATUSES, CartItem, Category, Event, EventDocument, Favorite,
                      Offer, Order, Payment, Quote, QuoteAttachment, QuoteRequest, Review, TICKET_CATEGORIES, Ticket, TicketMessage,
                      utcnow)
from ..security import role_required, verified_required
from ..services import audit, availability, files, finance, messaging, orders as order_service, payments, pricing, settings
from ..services import quotes as quote_service
from ..services.notifications import notify
from ..utils import (REGIONS, new_reference, parse_date, parse_int, region_of, fmt_xof)
from ._helpers import dt, flag, flash_errors, num, owned_or_404, paginate, txt

bp = Blueprint("buyer", __name__)


@bp.before_request
@role_required("buyer")
def _guard():
    pass


# ------------------------------------------------------------------ tableau de bord
@bp.route("/")
def dashboard():
    uid = current_user.id
    events = Event.query.filter_by(buyer_id=uid, status="planned").filter(Event.end_date >= date.today()) \
        .order_by(Event.start_date).limit(5).all()
    by_status = dict(db.session.query(Order.status, db.func.count(Order.id)).filter_by(buyer_id=uid).group_by(Order.status).all())
    spent = db.session.query(db.func.coalesce(db.func.sum(Order.paid_xof - Order.refunded_xof), 0)).filter(
        Order.buyer_id == uid, Order.status != "cancelled").scalar()
    remaining = sum(o.remaining_xof for o in Order.query.filter(Order.buyer_id == uid,
                    Order.status.in_(("pending_payment", "pending_confirmation", "confirmed", "in_progress"))))
    budget_total = db.session.query(db.func.coalesce(db.func.sum(Event.budget_xof), 0)).filter(
        Event.buyer_id == uid, Event.status == "planned").scalar()
    to_handle = (QuoteRequest.query.filter(QuoteRequest.buyer_id == uid, QuoteRequest.status.in_(("quoted", "negotiating")))
                 .order_by(QuoteRequest.updated_at.desc()).all())
    to_handle = [r for r in to_handle if r.latest_quote and r.latest_quote.author_role == "vendor" and r.latest_quote.status == "sent"]
    waiting = QuoteRequest.query.filter(QuoteRequest.buyer_id == uid, QuoteRequest.status.in_(("open", "negotiating"))).count()
    upcoming_pay = (Order.query.filter(Order.buyer_id == uid, Order.status.in_(("confirmed", "in_progress", "pending_confirmation")),
                                       Order.balance_due_date.isnot(None)).order_by(Order.balance_due_date).limit(5).all())
    upcoming_pay = [o for o in upcoming_pay if o.remaining_xof > 0]
    return render_template("buyer/dashboard.html", events=events, by_status=by_status, spent=spent, remaining=remaining,
                           budget_total=budget_total, to_handle=to_handle, waiting=waiting, upcoming_pay=upcoming_pay)


# ------------------------------------------------------------------ événements
def _event_form_errors(f):
    errors = []
    if not f["title"]:
        errors.append("Donnez un nom à votre événement.")
    if not f["start_date"]:
        errors.append("La date de l'événement est obligatoire.")
    elif f["start_date"] < date.today() - timedelta(days=0) and not f.get("allow_past"):
        errors.append("La date de l'événement doit être dans le futur.")
    if f["start_date"] and f["end_date"] and f["end_date"] < f["start_date"]:
        errors.append("La date de fin précède la date de début.")
    return errors


def _read_event_form(ev=None):
    start = dt("start_date")
    city = txt("city", 80)
    return {"title": txt("title", 160), "event_type": txt("event_type", 16) if txt("event_type", 16) in
            ("wedding", "birthday", "baptism", "conference", "seminar", "party", "cultural", "other") else "other",
            "description": txt("description", 3000), "start_date": start, "end_date": dt("end_date") or start,
            "start_time": txt("start_time", 5), "end_time": txt("end_time", 5), "venue": txt("venue", 160),
            "address": txt("address", 255), "city": city, "region": txt("region", 80) or region_of(city),
            "indoor": txt("indoor", 10) if txt("indoor", 10) in ("indoor", "outdoor", "both") else None,
            "guests": num("guests", None, 1, 100000), "budget_xof": num("budget_xof", None, 0),
            "budget_alert_pct": num("budget_alert_pct", 90, 10, 200)}


def event_summary(ev):
    orders = [o for o in ev.orders if o.status != "cancelled"]
    committed = sum(o.total_xof for o in orders)
    paid = sum((o.paid_xof or 0) - (o.refunded_xof or 0) for o in orders)
    per_cat = {}
    for o in orders:
        cat = None
        for it in o.items:
            if it.offer and it.kind == "item":
                cat = it.offer.category.root
                break
        key = cat.name if cat else "Autres"
        per_cat[key] = per_cat.get(key, 0) + o.total_xof
    budget = ev.budget_xof or 0
    alert = bool(budget and committed >= budget * (ev.budget_alert_pct or 90) / 100)
    return {"committed": committed, "paid": paid, "to_pay": max(0, committed - paid),
            "left_to_commit": (budget - committed) if budget else None, "per_cat": per_cat, "alert": alert,
            "over": bool(budget and committed > budget)}


@bp.route("/evenements")
def events():
    evs = Event.query.filter_by(buyer_id=current_user.id).order_by(Event.start_date.desc()).all()
    return render_template("buyer/events.html", events=evs, summaries={e.id: event_summary(e) for e in evs})


@bp.route("/evenements/nouveau", methods=["GET", "POST"])
def event_new():
    nxt = request.args.get("next") or request.form.get("next")
    if request.method == "POST":
        f = _read_event_form()
        if not flash_errors(_event_form_errors(f)):
            ev = Event(buyer_id=current_user.id, **f)
            db.session.add(ev)
            db.session.commit()
            flash("Événement créé.", "success")
            if nxt and nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("buyer.event_detail", event_id=ev.id))
        return render_template("buyer/event_form.html", ev=None, form=request.form, regions=REGIONS, next=nxt)
    pre = {"city": current_user.city or ""}
    for k in ("start", "end"):
        if parse_date(request.args.get(k)):
            pre["start_date" if k == "start" else "end_date"] = request.args.get(k)
    return render_template("buyer/event_form.html", ev=None, form=pre, regions=REGIONS, next=nxt)


def _event(event_id):
    return owned_or_404(db.session.get(Event, event_id), buyer_id=current_user.id)


@bp.route("/evenements/<int:event_id>")
def event_detail(event_id):
    ev = _event(event_id)
    summary = event_summary(ev)
    reqs = QuoteRequest.query.filter_by(event_id=ev.id, buyer_id=current_user.id).order_by(QuoteRequest.created_at.desc()).all()
    steps = []
    for o in sorted(ev.orders, key=lambda x: x.created_at):
        if o.status == "cancelled":
            continue
        label = {"confirmed": "confirmé", "in_progress": "en cours", "completed": "terminé"}.get(o.status, "en attente")
        steps.append((o, label))
    return render_template("buyer/event_detail.html", ev=ev, summary=summary, reqs=reqs, steps=steps,
                           categories=Category.query.filter_by(parent_id=None, active=True).order_by(Category.position).all())


@bp.route("/evenements/<int:event_id>/modifier", methods=["GET", "POST"])
def event_edit(event_id):
    ev = _event(event_id)
    if request.method == "POST":
        f = _read_event_form()
        f["allow_past"] = True
        errs = _event_form_errors(f)
        f.pop("allow_past")
        if not flash_errors(errs):
            for k, v in f.items():
                setattr(ev, k, v)
            db.session.commit()
            flash("Événement mis à jour.", "success")
            return redirect(url_for("buyer.event_detail", event_id=ev.id))
        return render_template("buyer/event_form.html", ev=ev, form=request.form, regions=REGIONS, next=None)
    return render_template("buyer/event_form.html", ev=ev, form=_event_to_form(ev), regions=REGIONS, next=None)


def _event_to_form(ev):
    return {c: (getattr(ev, c).isoformat() if isinstance(getattr(ev, c), date) else getattr(ev, c))
            for c in ("title", "event_type", "description", "start_date", "end_date", "start_time", "end_time", "venue",
                      "address", "city", "region", "indoor", "guests", "budget_xof", "budget_alert_pct")}


@bp.route("/evenements/<int:event_id>/archiver", methods=["POST"])
def event_archive(event_id):
    ev = _event(event_id)
    ev.status = "archived" if ev.status == "planned" else "planned"
    db.session.commit()
    flash("Événement archivé." if ev.status == "archived" else "Événement réactivé.", "success")
    return redirect(url_for("buyer.events"))


@bp.route("/evenements/<int:event_id>/documents", methods=["POST"])
def event_doc_add(event_id):
    ev = _event(event_id)
    try:
        info = files.save_private(request.files.get("file"), files.ATTACH_EXT, subdir="events")
    except files.UploadError as e:
        flash(str(e), "danger")
        return redirect(url_for("buyer.event_detail", event_id=ev.id))
    db.session.add(EventDocument(event_id=ev.id, label=txt("label", 160) or info["original_name"], **info))
    db.session.commit()
    flash("Document ajouté.", "success")
    return redirect(url_for("buyer.event_detail", event_id=ev.id))


@bp.route("/evenements/documents/<int:doc_id>")
def event_doc_get(doc_id):
    doc = db.session.get(EventDocument, doc_id) or abort(404)
    owned_or_404(doc.event, buyer_id=current_user.id)
    return send_file(io.BytesIO(files.read_private(doc.stored_name)), mimetype=doc.mime, as_attachment=True,
                     download_name=doc.original_name)


@bp.route("/evenements/documents/<int:doc_id>/supprimer", methods=["POST"])
def event_doc_delete(doc_id):
    doc = db.session.get(EventDocument, doc_id) or abort(404)
    owned_or_404(doc.event, buyer_id=current_user.id)
    eid = doc.event_id
    db.session.delete(doc)
    db.session.commit()
    return redirect(url_for("buyer.event_detail", event_id=eid))


# ------------------------------------------------------------------ favoris
@bp.route("/favoris")
def favorites():
    favs = Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.created_at.desc()).all()
    return render_template("buyer/favorites.html", favs=favs)


# ------------------------------------------------------------------ panier
def _cart_items():
    return CartItem.query.filter_by(user_id=current_user.id).order_by(CartItem.id).all()


def _cart_view(items, promos=None):
    promos = promos or {}
    rows, by_vendor = [], {}
    for ci in items:
        o = ci.offer
        promo, perr = pricing.find_promo(promos.get(o.vendor_id), o.vendor_id, o.id)
        calc = pricing.compute(o, ci.start_date, ci.end_date, ci.quantity, ci.guests, ci.option_ids,
                               ci.event.region if ci.event else None, promo)
        av = availability.check(o, ci.start_date, ci.end_date, ci.quantity)
        row = {"item": ci, "calc": calc, "av": av, "promo_error": perr if promos.get(o.vendor_id) else None}
        rows.append(row)
        by_vendor.setdefault(o.vendor, []).append(row)
    total = sum(r["calc"]["total_xof"] for r in rows)
    deposit = sum(r["calc"]["deposit_xof"] for r in rows)
    return rows, by_vendor, total, deposit


@bp.route("/panier")
def cart():
    items = _cart_items()
    rows, by_vendor, total, deposit = _cart_view(items)
    blocked = any(not r["av"].ok or r["av"].status != "available" for r in rows)
    return render_template("buyer/cart.html", rows=rows, by_vendor=by_vendor, total=total, deposit=deposit, blocked=blocked)


@bp.route("/panier/ajouter", methods=["POST"])
@verified_required
def cart_add():
    offer = db.session.get(Offer, num("offer_id", 0)) or abort(404)
    back = url_for("public.offer", offer_id=offer.id, slug=offer.slug)
    if offer.status != "active" or not offer.is_bookable_direct:
        flash("Cette offre se réserve sur devis.", "warning")
        return redirect(back)
    start, end = dt("start"), dt("end") or dt("start")
    qty = num("quantity", offer.min_quantity or 1, 1, 100000)
    ev = db.session.get(Event, num("event_id", 0)) if num("event_id", 0) else None
    if ev is None or ev.buyer_id != current_user.id:
        flash("Choisissez l'événement concerné (ou créez-en un).", "warning")
        return redirect(back)
    errors = []
    if not start:
        errors.append("Choisissez la date de la prestation.")
    elif end < start:
        errors.append("La date de fin précède la date de début.")
    if start:
        av = availability.check(offer, start, end, qty)
        if av.status != "available":
            errors.append(av.reason or "Ces dates ne sont pas disponibles.")
    if flash_errors(errors):
        return redirect(back)
    opt_ids = [int(i) for i in request.form.getlist("options") if i.isdigit()]
    required = [o.id for o in offer.options if o.required]
    opt_ids = sorted(set(opt_ids) | set(required))
    existing = CartItem.query.filter_by(user_id=current_user.id, offer_id=offer.id, start_date=start, end_date=end, event_id=ev.id).first()
    if existing:
        existing.quantity, existing.option_ids, existing.guests = qty, opt_ids, num("guests", ev.guests)
    else:
        db.session.add(CartItem(user_id=current_user.id, offer_id=offer.id, event_id=ev.id, start_date=start,
                                end_date=end, quantity=qty, guests=num("guests", ev.guests), option_ids=opt_ids))
    db.session.commit()
    flash("Ajouté au panier. Le blocage du créneau n'intervient qu'à la validation de la commande.", "success")
    return redirect(url_for("buyer.cart"))


@bp.route("/panier/<int:item_id>/modifier", methods=["POST"])
def cart_update(item_id):
    ci = owned_or_404(db.session.get(CartItem, item_id), user_id=current_user.id)
    ci.quantity = num("quantity", ci.quantity, 1, 100000)
    start, end = dt("start") or ci.start_date, dt("end") or dt("start") or ci.end_date
    if end < start:
        flash("Dates invalides.", "danger")
    else:
        ci.start_date, ci.end_date = start, end
        opt = [int(i) for i in request.form.getlist("options") if i.isdigit()]
        ci.option_ids = sorted(set(opt) | {o.id for o in ci.offer.options if o.required})
        db.session.commit()
    return redirect(url_for("buyer.cart"))


@bp.route("/panier/<int:item_id>/supprimer", methods=["POST"])
def cart_remove(item_id):
    ci = owned_or_404(db.session.get(CartItem, item_id), user_id=current_user.id)
    db.session.delete(ci)
    db.session.commit()
    return redirect(url_for("buyer.cart"))


@bp.route("/panier/valider", methods=["POST"])
@verified_required
def cart_checkout():
    items = _cart_items()
    if not items:
        flash("Votre panier est vide.", "warning")
        return redirect(url_for("buyer.cart"))
    if not flag("accept_conditions"):
        flash("Acceptez les conditions de réservation et d'annulation pour continuer.", "warning")
        return redirect(url_for("buyer.cart"))
    promos = {}
    for key, val in request.form.items():
        if key.startswith("promo_") and val.strip():
            try:
                promos[int(key[6:])] = val.strip().upper()
            except ValueError:
                pass
    try:
        orders = order_service.create_orders_from_cart(current_user, items, promos)
        db.session.commit()
    except order_service.OrderError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("buyer.cart"))
    if len(orders) == 1:
        flash("Réservation créée : le créneau est bloqué pendant la durée du paiement.", "success")
        return redirect(url_for("payments.pay", order_id=orders[0].id))
    flash(f"{len(orders)} réservations créées (une par prestataire). Réglez chacune pour la confirmer.", "success")
    return redirect(url_for("buyer.orders"))


# ------------------------------------------------------------------ devis
@bp.route("/devis")
def quotes():
    status = request.args.get("statut", "")
    q = QuoteRequest.query.filter_by(buyer_id=current_user.id)
    if status:
        q = q.filter_by(status=status)
    page = paginate(q.order_by(QuoteRequest.updated_at.desc()))
    return render_template("buyer/quotes.html", page=page, status=status)


@bp.route("/devis/nouveau", methods=["GET", "POST"])
@verified_required
def quote_new():
    offer = db.session.get(Offer, parse_int(request.values.get("offer_id"), 0)) if request.values.get("offer_id") else None
    vendor = offer.vendor if offer else None
    if vendor is None and request.values.get("vendor_id"):
        from ..models import VendorProfile
        vendor = db.session.get(VendorProfile, parse_int(request.values.get("vendor_id"), 0))
    if vendor is None or not vendor.is_validated or (offer and offer.status != "active"):
        abort(404)
    evs = Event.query.filter_by(buyer_id=current_user.id, status="planned").order_by(Event.start_date).all()
    if request.method == "POST":
        ev = db.session.get(Event, num("event_id", 0)) if num("event_id", 0) else None
        if ev is not None and ev.buyer_id != current_user.id:
            abort(404)
        start = dt("start_date") or (ev.start_date if ev else None)
        end = dt("end_date") or start
        need = txt("need", 4000)
        errors = []
        if not start:
            errors.append("Indiquez la date de la prestation.")
        elif start < date.today():
            errors.append("La date doit être dans le futur.")
        if start and end and end < start:
            errors.append("La date de fin précède le début.")
        if len(need) < 15:
            errors.append("Décrivez votre besoin (15 caractères minimum).")
        respond = dt("respond_by")
        if respond and respond < date.today():
            errors.append("La date limite de réponse est passée.")
        if not flag("contact_consent"):
            errors.append("Vous devez accepter d'être contacté par le prestataire.")
        att = request.files.getlist("attachments")
        saved = []
        if not errors:
            try:
                for f in att[:5]:
                    if f and f.filename:
                        saved.append(files.save_private(f, files.ATTACH_EXT, subdir="quotes"))
            except files.UploadError as e:
                errors.append(str(e))
        if not flash_errors(errors):
            req = quote_service.create_request(current_user, offer, vendor, dict(
                event_id=ev.id if ev else None, start_date=start, end_date=end or start, start_time=txt("start_time", 5),
                end_time=txt("end_time", 5), location=txt("location", 255) or (ev.address if ev else ""),
                indoor=txt("indoor", 10) or None, quantity=num("quantity", 1, 1, 100000), guests=num("guests", None, 1, 100000),
                need=need, budget_xof=num("budget_xof", None, 0), respond_by=respond, contact_consent=True))
            for info in saved:
                req.attachments.append(QuoteAttachment(**info))
            messaging.for_quote(req)
            db.session.commit()
            flash("Demande envoyée. Le prestataire vous répondra avec un devis détaillé.", "success")
            return redirect(url_for("buyer.quote_detail", req_id=req.id))
    # pré-remplissage depuis l'événement (AC-04) et la recherche
    form = request.form if request.method == "POST" else {
        "start_date": request.args.get("start", ""), "end_date": request.args.get("end", ""),
        "guests": request.args.get("guests", ""), "event_id": request.args.get("event_id", ""),
        "quantity": request.args.get("qty", 1), "location": "",
    }
    return render_template("buyer/quote_new.html", offer=offer, vendor=vendor, events=evs, form=form)


def _req(req_id):
    return owned_or_404(db.session.get(QuoteRequest, req_id), buyer_id=current_user.id)


@bp.route("/devis/<int:req_id>")
def quote_detail(req_id):
    req = _req(req_id)
    last = req.latest_quote
    if last and last.status == "sent" and last.is_expired:
        last.status = "expired"
        if req.status in quote_service.OPEN_STATES:
            req.status = "expired"
        db.session.commit()
    conv = messaging.for_quote(req)
    db.session.commit()
    messaging.mark_read(conv, current_user)
    db.session.commit()
    order = Order.query.filter_by(quote_id=req.accepted_quote.id).filter(Order.status != "cancelled").first() if req.accepted_quote else None
    diffs = {q.id: quote_service.diff_with_previous(q) for q in req.quotes}
    limit = settings.get_int("max_buyer_counters", 5)
    return render_template("buyer/quote_detail.html", req=req, last=last, conv=conv, order=order, diffs=diffs,
                           counters_left=max(0, limit - (req.counter_count or 0)), templates=[])


@bp.route("/devis/<int:req_id>/contre-proposition", methods=["POST"])
def quote_counter(req_id):
    req = _req(req_id)
    last = req.latest_quote
    try:
        lines = quote_service.parse_lines(request.form)
        quote_service.buyer_counter(
            req, current_user, lines, valid_until=date.today() + timedelta(days=5),
            deposit_percent=last.deposit_percent if last else 30, balance_days=last.balance_due_days_before if last else 3,
            description=last.description if last else "", exclusions=last.exclusions if last else "",
            conditions=last.conditions if last else "", policy=last.cancellation_policy if last else "moderate",
            note=txt("note", 2000), start=dt("start_date") or (last.start_date if last else None),
            end=dt("end_date") or (last.end_date if last else None), quantity=num("quantity", last.quantity if last else 1, 1))
        db.session.commit()
        flash("Contre-proposition envoyée.", "success")
    except quote_service.QuoteError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("buyer.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/versions/<int:quote_id>/accepter", methods=["POST"])
def quote_accept(req_id, quote_id):
    req = _req(req_id)
    q = owned_or_404(db.session.get(Quote, quote_id), request_id=req.id)
    try:
        quote_service.accept(q, current_user)
        db.session.commit()
        flash("Devis accepté : cette version devient la référence commerciale. Vous pouvez maintenant réserver et payer l'acompte.", "success")
    except quote_service.QuoteError as e:
        db.session.commit()
        flash(str(e), "danger")
    return redirect(url_for("buyer.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/refuser", methods=["POST"])
def quote_decline(req_id):
    req = _req(req_id)
    try:
        quote_service.decline(req, current_user, txt("reason", 500))
        db.session.commit()
        flash("Demande clôturée.", "info")
    except quote_service.QuoteError as e:
        flash(str(e), "danger")
    return redirect(url_for("buyer.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/annuler", methods=["POST"])
def quote_cancel(req_id):
    req = _req(req_id)
    try:
        quote_service.cancel_request(req)
        db.session.commit()
        flash("Demande annulée.", "info")
    except quote_service.QuoteError as e:
        flash(str(e), "danger")
    return redirect(url_for("buyer.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/actualiser", methods=["POST"])
def quote_refresh(req_id):
    """Demande de réactualisation d'un devis expiré : message au prestataire."""
    req = _req(req_id)
    conv = messaging.for_quote(req)
    messaging.post(conv, current_user, "Bonjour, mon devis a expiré. Pouvez-vous le réactualiser s'il vous plaît ?")
    if req.status == "expired":
        req.status = "open"
    db.session.commit()
    flash("Demande de réactualisation envoyée.", "success")
    return redirect(url_for("buyer.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/reserver", methods=["POST"])
@verified_required
def quote_book(req_id):
    req = _req(req_id)
    q = req.accepted_quote
    if not q:
        flash("Aucun devis accepté.", "warning")
        return redirect(url_for("buyer.quote_detail", req_id=req.id))
    if not flag("accept_conditions"):
        flash("Acceptez les conditions et la politique d'annulation pour réserver.", "warning")
        return redirect(url_for("buyer.quote_detail", req_id=req.id))
    try:
        order = order_service.create_order_from_quote(q)
        db.session.commit()
    except order_service.OrderError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("buyer.quote_detail", req_id=req.id))
    if (order.snapshot or {}).get("offline"):
        flash("Réservation enregistrée. Le paiement est convenu directement avec le prestataire (hors plateforme, non protégé par EventyStore).", "warning")
        return redirect(url_for("buyer.order_detail", order_id=order.id))
    return redirect(url_for("payments.pay", order_id=order.id))


@bp.route("/devis/comparer")
def quote_compare():
    ids = [parse_int(i) for i in request.args.getlist("ids") if i.isdigit()]
    reqs = [r for r in QuoteRequest.query.filter(QuoteRequest.id.in_(ids or [0]), QuoteRequest.buyer_id == current_user.id)]
    rows = [(r, r.latest_quote) for r in reqs if r.latest_quote]
    mine = QuoteRequest.query.filter(QuoteRequest.buyer_id == current_user.id,
                                     QuoteRequest.status.in_(("quoted", "negotiating", "accepted"))).order_by(QuoteRequest.created_at.desc()).all()
    return render_template("buyer/quote_compare.html", rows=rows, mine=mine, ids=ids)


@bp.route("/devis/pieces/<int:att_id>")
def quote_attachment(att_id):
    a = db.session.get(QuoteAttachment, att_id) or abort(404)
    owned_or_404(db.session.get(QuoteRequest, a.request_id), buyer_id=current_user.id)
    return send_file(io.BytesIO(files.read_private(a.stored_name)), mimetype=a.mime, as_attachment=True, download_name=a.original_name)


# ------------------------------------------------------------------ commandes
def _order(order_id):
    return owned_or_404(db.session.get(Order, order_id), buyer_id=current_user.id)


@bp.route("/commandes")
def orders():
    status = request.args.get("statut", "")
    q = Order.query.filter_by(buyer_id=current_user.id)
    if status:
        q = q.filter_by(status=status)
    if request.args.get("evenement"):
        q = q.filter_by(event_id=parse_int(request.args.get("evenement"), 0))
    page = paginate(q.order_by(Order.created_at.desc()))
    evs = Event.query.filter_by(buyer_id=current_user.id).all()
    return render_template("buyer/orders.html", page=page, status=status, events=evs)


@bp.route("/commandes/<int:order_id>")
def order_detail(order_id):
    o = _order(order_id)
    conv = messaging.for_order(o)
    db.session.commit()
    messaging.mark_read(conv, current_user)
    db.session.commit()
    cancel_info = finance.refund_quote(o) if o.status not in ("cancelled", "completed") else None
    timeline = _timeline(o)
    can_review = o.status == "completed" and o.review is None
    return render_template("buyer/order_detail.html", o=o, conv=conv, cancel_info=cancel_info, timeline=timeline,
                           can_review=can_review, policy=CANCELLATION_POLICIES.get(o.policy))


def _timeline(o):
    ev = [(o.created_at, "Commande créée", "info")]
    for p in o.payments:
        if p.status == "succeeded":
            ev.append((p.paid_at, f"Paiement reçu : {fmt_xof(p.amount_xof)}", "success"))
    if o.vendor_confirmed_at:
        ev.append((o.vendor_confirmed_at, "Confirmée par le prestataire", "success"))
    elif o.confirmed_at:
        ev.append((o.confirmed_at, "Réservation confirmée", "success"))
    if o.completed_at:
        ev.append((o.completed_at, "Prestation clôturée", "success"))
    if o.cancelled_at:
        ev.append((o.cancelled_at, f"Annulée ({o.cancelled_by})", "danger"))
    for r in o.refunds:
        ev.append((r.created_at, f"Remboursement : {fmt_xof(r.amount_xof)}", "info"))
    return sorted([e for e in ev if e[0]], key=lambda x: x[0])


@bp.route("/commandes/<int:order_id>/annuler", methods=["POST"])
def order_cancel(order_id):
    o = _order(order_id)
    if not flag("confirm"):
        flash("Confirmez l'annulation.", "warning")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    try:
        res = order_service.cancel(o, by="buyer", reason=txt("reason", 500) or "Annulation par l'acheteur", actor=current_user)
        db.session.commit()
        flash(f"Réservation annulée. Remboursement : {fmt_xof(res['refunded'])}.", "success")
    except order_service.OrderError as e:
        flash(str(e), "danger")
    return redirect(url_for("buyer.order_detail", order_id=o.id))


@bp.route("/commandes/<int:order_id>/litige", methods=["POST"])
def order_dispute(order_id):
    o = _order(order_id)
    cat = txt("category", 16)
    desc = txt("description", 4000)
    if len(desc) < 15:
        flash("Décrivez le problème (15 caractères minimum).", "danger")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    t = Ticket(reference=new_reference("TK"), author_id=current_user.id, order_id=o.id,
               category=cat if cat in TICKET_CATEGORIES else "other",
               subject=f"Réclamation — commande {o.reference}", description=desc)
    t.messages.append(TicketMessage(author_id=current_user.id, body=desc))
    db.session.add(t)
    if o.status in ("confirmed", "in_progress", "completed"):
        if o.status != "completed":
            o.status = "dispute"
    audit.log("ticket.open", t, order=o.reference)
    notify(o.vendor.user, "dispute", f"Réclamation ouverte — {o.reference}", "Le support EventyStore a été saisi.",
           f"/pro/commandes/{o.id}", critical=True, dedupe_key=f"dispute:{o.id}")
    db.session.commit()
    flash("Votre réclamation a été transmise au support.", "success")
    return redirect(url_for("account.ticket_detail", ticket_id=t.id))


@bp.route("/commandes/<int:order_id>/avis", methods=["POST"])
def order_review(order_id):
    o = _order(order_id)
    if o.status != "completed":
        flash("Vous pourrez donner votre avis une fois la prestation terminée.", "warning")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    if o.review is not None:
        flash("Vous avez déjà déposé un avis pour cette commande.", "warning")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    rating = num("rating", 0, 0, 5)
    if rating < 1:
        flash("Choisissez une note de 1 à 5 étoiles.", "danger")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    main_offer = next((i.offer_id for i in o.items if i.offer_id), None)
    rv = Review(order_id=o.id, buyer_id=current_user.id, vendor_id=o.vendor_id, offer_id=main_offer, rating=rating,
                quality=num("quality", None, 1, 5), punctuality=num("punctuality", None, 1, 5),
                communication=num("communication", None, 1, 5), conformity=num("conformity", None, 1, 5),
                comment=txt("comment", 3000))
    db.session.add(rv)
    notify(o.vendor.user, "review", f"Nouvel avis ({rating}/5) — {o.reference}", rv.comment[:120] if rv.comment else "",
           "/pro/avis", dedupe_key=f"review_new:{o.id}")
    db.session.commit()
    flash("Merci pour votre avis !", "success")
    return redirect(url_for("buyer.order_detail", order_id=o.id))


@bp.route("/commandes/<int:order_id>/recapitulatif")
def order_summary(order_id):
    o = _order(order_id)
    return render_template("shared/order_print.html", o=o, viewer="buyer")


@bp.route("/commandes/<int:order_id>/recu/<int:pay_id>")
def receipt(order_id, pay_id):
    o = _order(order_id)
    p = owned_or_404(db.session.get(Payment, pay_id), order_id=o.id)
    if p.status not in ("succeeded", "refunded", "partially_refunded"):
        abort(404)
    return render_template("shared/receipt.html", o=o, p=p)


# ------------------------------------------------------------------ paiements & exports
@bp.route("/paiements")
def payments_list():
    pays = (Payment.query.join(Order).filter(Order.buyer_id == current_user.id).order_by(Payment.id.desc()).all())
    return render_template("buyer/payments.html", pays=pays)


@bp.route("/export/commandes.csv")
def export_orders():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Référence", "Prestataire", "Événement", "Début", "Fin", "Statut", "Total FCFA", "Payé FCFA", "Remboursé FCFA", "Reste FCFA"])
    for o in Order.query.filter_by(buyer_id=current_user.id).order_by(Order.created_at.desc()):
        w.writerow([o.reference, o.vendor.business_name, o.event.title if o.event else "", o.start_date, o.end_date,
                    o.status_label, o.total_xof, o.paid_xof, o.refunded_xof, o.remaining_xof])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=eventystore-commandes.csv"})
