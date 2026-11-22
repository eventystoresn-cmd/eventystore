"""Espace prestataire : profil, vérification, offres, calendrier, devis, commandes, revenus, promos, avis."""
import calendar as pycal
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for)
from flask_login import current_user

from ..extensions import db
from ..models import (AvailabilityBlock, CANCELLATION_POLICIES, Category, LedgerEntry, Media, Offer, OfferOption,
                      Order, Payment, Payout, Promo, Quote, QuoteRequest, Review, Ticket, TicketMessage, User,
                      VendorDocument, VendorProfile, VerificationEvent, utcnow)
from ..security import role_required, verified_required
from ..services import audit, availability, files, finance, messaging, orders as order_service, pricing, settings
from ..services import quotes as quote_service
from ..services.notifications import notify
from ..services.search import refresh_search_fields
from ..utils import (REGIONS, daterange, fmt_xof, new_reference, parse_date, parse_int, region_of, slugify, MONTHS_FR)
from ._helpers import dt, flag, flash_errors, num, owned_or_404, paginate, txt

bp = Blueprint("vendor", __name__)


@bp.before_request
@role_required("vendor")
def _guard():
    if current_user.vendor is None:
        abort(403)


def V():
    return current_user.vendor


def _unique_offer_slug(title):
    return slugify(title, 70)


# ------------------------------------------------------------------ tableau de bord
@bp.route("/")
def dashboard():
    v = V()
    by_status = dict(db.session.query(Order.status, db.func.count(Order.id)).filter_by(vendor_id=v.id).group_by(Order.status).all())
    to_confirm = Order.query.filter_by(vendor_id=v.id, status="pending_confirmation").order_by(Order.created_at).all()
    open_quotes = QuoteRequest.query.filter(QuoteRequest.vendor_id == v.id, QuoteRequest.status.in_(("open", "negotiating"))).all()
    to_quote = [r for r in open_quotes if r.status == "open" or (r.latest_quote and r.latest_quote.author_role == "buyer" and r.latest_quote.status == "sent")]
    gross = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.gross_xof), 0)).filter(
        LedgerEntry.vendor_id == v.id, LedgerEntry.kind.in_(("credit", "refund_debit"))).scalar()
    net = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount_xof), 0)).filter(
        LedgerEntry.vendor_id == v.id, LedgerEntry.kind.in_(("credit", "refund_debit"))).scalar()
    balance = finance.vendor_balance(v.id)
    upcoming = (Order.query.filter(Order.vendor_id == v.id, Order.status.in_(("confirmed", "in_progress")),
                                   Order.end_date >= date.today()).order_by(Order.start_date).limit(6).all())
    reviews = Review.query.filter_by(vendor_id=v.id).order_by(Review.created_at.desc()).limit(3).all()
    views = db.session.query(db.func.coalesce(db.func.sum(Offer.views), 0), db.func.coalesce(db.func.sum(Offer.clicks), 0)).filter(Offer.vendor_id == v.id).one()
    all_reqs = QuoteRequest.query.filter_by(vendor_id=v.id).all()
    answered = [r for r in all_reqs if r.first_response_at]
    rate = round(100 * len(answered) / len(all_reqs)) if all_reqs else None
    avg_h = round(sum((r.first_response_at - r.created_at).total_seconds() for r in answered) / len(answered) / 3600, 1) if answered else None
    done = by_status.get("completed", 0)
    months = _revenue_series(v.id)
    checklist = [
        ("Compléter mon profil public", bool(v.description and len(v.description) >= 30 and v.contact_phone), "vendor.profile"),
        ("Déposer mes justificatifs et soumettre mon dossier", v.verification_status not in ("draft", "complement_requested", "refused"), "vendor.verification"),
        ("Créer ma première offre avec photo", Offer.query.filter_by(vendor_id=v.id).filter(Offer.status != "archived").count() > 0, "vendor.offer_edit"),
        ("Publier une offre", Offer.query.filter(Offer.vendor_id == v.id, Offer.status.in_(("active", "submitted"))).count() > 0, "vendor.offers"),
        ("Renseigner mes coordonnées de retrait", bool(v.payout_method), "vendor.revenue"),
    ]
    return render_template("vendor/dashboard.html", checklist=checklist, v=v, by_status=by_status, to_confirm=to_confirm, to_quote=to_quote,
                           gross=gross, net=net, balance=balance, upcoming=upcoming, reviews=reviews, views=views,
                           rate=rate, avg_h=avg_h, done=done, months=months, blocks=_next_blocks(v.id))


def _revenue_series(vendor_id):
    out = []
    today = date.today().replace(day=1)
    for i in range(5, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        start = datetime(y, m, 1)
        end = datetime(y + (m == 12), (m % 12) + 1, 1)
        val = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount_xof), 0)).filter(
            LedgerEntry.vendor_id == vendor_id, LedgerEntry.kind.in_(("credit", "refund_debit")),
            LedgerEntry.created_at >= start, LedgerEntry.created_at < end).scalar()
        out.append((f"{MONTHS_FR[m - 1][:4]}.", int(val)))
    return out


def _next_blocks(vendor_id):
    return AvailabilityBlock.query.filter(AvailabilityBlock.vendor_id == vendor_id,
                                          AvailabilityBlock.end_date >= date.today()).order_by(AvailabilityBlock.start_date).limit(5).all()


# ------------------------------------------------------------------ profil
@bp.route("/profil", methods=["GET", "POST"])
def profile():
    v = V()
    if request.method == "POST":
        errors = []
        name = txt("business_name", 160)
        if not name:
            errors.append("Le nom commercial est obligatoire.")
        city = txt("city", 80)
        areas = [a for a in request.form.getlist("areas") if a in REGIONS]
        if not areas:
            errors.append("Sélectionnez au moins une région desservie.")
        if not flash_errors(errors):
            v.business_name, v.tagline = name, txt("tagline", 200)
            v.description, v.specialties = txt("description", 5000), txt("specialties", 300)
            v.years_experience = num("years_experience", None, 0, 80)
            v.contact_phone, v.whatsapp, v.contact_email = txt("contact_phone", 30), txt("whatsapp", 30), txt("contact_email", 190)
            v.address, v.city = txt("address", 255), city
            v.region = region_of(city) or v.region or areas[0]
            v.served_areas = areas
            v.travel_fee_xof = num("travel_fee_xof", 0, 0)
            v.opening_hours = {d: txt(f"hours_{d}", 20) for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") if txt(f"hours_{d}", 20)}
            try:
                for field, sub in (("logo", "logos"), ("cover", "covers")):
                    f = request.files.get(field)
                    if f and f.filename:
                        path, _ = files.save_image(f, sub, max_side=1400 if field == "cover" else 600)
                        files.delete_public(getattr(v, field))
                        setattr(v, field, path)
            except files.UploadError as e:
                flash(str(e), "danger")
            for off in v.offers:
                refresh_search_fields(off)
            db.session.commit()
            flash("Profil enregistré.", "success")
            return redirect(url_for("vendor.profile"))
    gallery = Media.query.filter_by(vendor_id=v.id).order_by(Media.position).all()
    return render_template("vendor/profile.html", v=v, regions=REGIONS, gallery=gallery,
                           days=[("mon", "Lundi"), ("tue", "Mardi"), ("wed", "Mercredi"), ("thu", "Jeudi"), ("fri", "Vendredi"),
                                 ("sat", "Samedi"), ("sun", "Dimanche")])


@bp.route("/profil/galerie", methods=["POST"])
def gallery_add():
    v = V()
    n = 0
    try:
        for f in request.files.getlist("photos")[:10]:
            if f and f.filename:
                path, thumb = files.save_image(f, "portfolio")
                db.session.add(Media(vendor_id=v.id, path=path, thumb=thumb, caption=txt("caption", 200),
                                     alt=f"Réalisation de {v.business_name}", position=Media.query.filter_by(vendor_id=v.id).count()))
                n += 1
    except files.UploadError as e:
        flash(str(e), "danger")
    db.session.commit()
    if n:
        flash(f"{n} photo(s) ajoutée(s).", "success")
    return redirect(url_for("vendor.profile"))


@bp.route("/profil/galerie/<int:media_id>/supprimer", methods=["POST"])
def gallery_delete(media_id):
    m = owned_or_404(db.session.get(Media, media_id), vendor_id=V().id)
    files.delete_public(m.path)
    files.delete_public(m.thumb)
    db.session.delete(m)
    db.session.commit()
    return redirect(url_for("vendor.profile"))


# ------------------------------------------------------------------ vérification
DOC_TYPES = {"identity": "Pièce d'identité ou passeport", "registry": "Registre de commerce (RCCM) ou équivalent",
             "ninea": "NINEA", "professional": "Justificatif professionnel / carte", "other": "Autre document"}


def required_doc_types(v):
    return ["identity"] if v.legal_status == "independant" else ["identity", "registry", "ninea"]


def _missing_for_submission(v):
    miss = []
    have = {d.doc_type for d in v.documents}
    for t in required_doc_types(v):
        if t not in have:
            miss.append(DOC_TYPES[t])
    if not v.description or len(v.description) < 30:
        miss.append("description de l'activité (profil)")
    if not (v.contact_phone):
        miss.append("téléphone de contact")
    if not v.served_areas:
        miss.append("zones desservies")
    return miss


@bp.route("/verification", methods=["GET", "POST"])
def verification():
    v = V()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "upload":
            if v.verification_status in ("submitted", "in_review", "validated"):
                flash("Votre dossier est en cours de traitement ou validé : le dépôt de documents est verrouillé.", "warning")
            else:
                dtype = request.form.get("doc_type")
                if dtype not in DOC_TYPES:
                    flash("Type de document invalide.", "danger")
                else:
                    try:
                        info = files.save_private(request.files.get("file"), files.DOC_EXT, subdir="vendor-docs")
                        db.session.add(VendorDocument(vendor_id=v.id, doc_type=dtype, **info))
                        db.session.commit()
                        flash("Document déposé de façon sécurisée (chiffré).", "success")
                    except files.UploadError as e:
                        flash(str(e), "danger")
        elif action == "submit":
            if v.verification_status not in ("draft", "complement_requested", "refused"):
                flash("Votre dossier a déjà été soumis.", "warning")
            else:
                miss = _missing_for_submission(v)
                if miss:  # AC-11
                    flash("Dossier incomplet. Il manque : " + " ; ".join(miss) + ".", "danger")
                else:
                    old = v.verification_status
                    v.verification_status, v.submitted_at = "submitted", utcnow()
                    db.session.add(VerificationEvent(vendor_id=v.id, actor_id=current_user.id, from_status=old,
                                                     to_status="submitted", note="Dossier soumis par le prestataire"))
                    for s in User.query.filter_by(role="staff").all():
                        if s.has_perm("vendors"):
                            notify(s, "vendor_submitted", f"Nouveau dossier prestataire — {v.business_name}", "", "/admin/prestataires",
                                   dedupe_key=f"vsub:{v.id}:{int(utcnow().timestamp())}", email=False)
                    db.session.commit()
                    flash("Dossier soumis ! Notre équipe le vérifie sous 24 à 48 heures ouvrées.", "success")
        return redirect(url_for("vendor.verification"))
    return render_template("vendor/verification.html", v=v, doc_types=DOC_TYPES, required=required_doc_types(v),
                           missing=_missing_for_submission(v), events=VerificationEvent.query.filter_by(vendor_id=v.id)
                           .order_by(VerificationEvent.id.desc()).all())


@bp.route("/verification/documents/<int:doc_id>/supprimer", methods=["POST"])
def doc_delete(doc_id):
    d = owned_or_404(db.session.get(VendorDocument, doc_id), vendor_id=V().id)
    if V().verification_status in ("submitted", "in_review", "validated"):
        flash("Dossier verrouillé.", "warning")
    else:
        db.session.delete(d)
        db.session.commit()
    return redirect(url_for("vendor.verification"))


# ------------------------------------------------------------------ offres
def _category_options():
    return Category.query.filter_by(active=True).order_by(Category.position, Category.name).all()


def _read_offer(o, cat):
    errors = []
    title = txt("title", 180)
    if len(title) < 4:
        errors.append("Le titre est obligatoire (4 caractères minimum).")
    o.title = title
    o.description = txt("description", 6000)
    o.inclusions = txt("inclusions", 3000)
    o.conditions = txt("conditions", 3000)
    otype = txt("offer_type", 16)
    o.offer_type = otype if otype in ("service", "rental", "sale", "package") else "service"
    pm = txt("price_model", 12)
    o.price_model = pm if pm in ("fixed", "from", "range", "quote") else "fixed"
    pu = txt("price_unit", 12)
    o.price_unit = pu if pu in ("person", "hour", "day", "night", "unit", "lot", "event", "package") else "event"
    o.price_xof = num("price_xof", None, 0) if o.price_model != "quote" else None
    o.price_max_xof = num("price_max_xof", None, 0) if o.price_model == "range" else None
    if o.price_model != "quote" and not o.price_xof:
        errors.append("Indiquez le prix (ou choisissez « Sur devis »).")
    if o.price_model == "range" and o.price_max_xof and o.price_xof and o.price_max_xof < o.price_xof:
        errors.append("Le prix maximum doit être supérieur au prix minimum.")
    o.min_quantity = num("min_quantity", 1, 1)
    o.capacity = num("capacity", None, 1)
    o.stock_qty = num("stock_qty", None, 0) if o.offer_type in ("rental", "sale") else None
    o.concurrent_capacity = num("concurrent_capacity", 1, 1, 50)
    o.min_notice_days = num("min_notice_days", 2, 0, 365)
    o.buffer_before_days = num("buffer_before_days", 0, 0, 14)
    o.buffer_after_days = num("buffer_after_days", 0, 0, 14)
    o.city = txt("city", 80) or V().city
    o.region = region_of(o.city) or txt("region", 80) or V().region
    o.service_areas = [a for a in request.form.getlist("service_areas") if a in REGIONS] or V().served_areas
    o.travel_fee_xof = num("travel_fee_xof", 0, 0)
    o.delivery_fee_xof = num("delivery_fee_xof", 0, 0)
    o.install_fee_xof = num("install_fee_xof", 0, 0)
    o.deposit_percent = num("deposit_percent", 30, 0, 100)
    o.caution_xof = num("caution_xof", 0, 0)
    cp = txt("cancellation_policy", 12)
    o.cancellation_policy = cp if cp in CANCELLATION_POLICIES else "moderate"
    o.online_payment = flag("online_payment")
    # paliers dégressifs : "50:900;100:800"
    tiers = []
    for chunk in txt("tiers", 400).replace("\n", ";").split(";"):
        if ":" in chunk:
            a, b = chunk.split(":", 1)
            qa, pb = parse_int(a), parse_int(b)
            if qa and pb:
                tiers.append({"min_qty": qa, "unit_price": pb})
    o.tiers = tiers
    o.attrs = {a["key"]: txt(f"attr_{a['key']}", 200) for a in (cat.attributes or []) if txt(f"attr_{a['key']}", 200)} if cat else {}
    return errors


def _sync_options(o):
    names = request.form.getlist("opt_name")
    prices = request.form.getlist("opt_price")
    types = request.form.getlist("opt_type")
    reqs = request.form.getlist("opt_required")
    for old in list(o.options):
        db.session.delete(old)
    o.options = []
    for i, n in enumerate(names):
        n = n.strip()
        if n:
            o.options.append(OfferOption(name=n[:160], price_xof=parse_int(prices[i] if i < len(prices) else 0, 0, 0),
                                         price_type=types[i] if i < len(types) and types[i] in ("flat", "per_unit", "per_person") else "flat",
                                         required=str(i) in reqs))


@bp.route("/offres")
def offers():
    status = request.args.get("statut", "")
    q = Offer.query.filter_by(vendor_id=V().id)
    if status:
        q = q.filter_by(status=status)
    else:
        q = q.filter(Offer.status != "archived")
    page = paginate(q.order_by(Offer.updated_at.desc()), 12)
    return render_template("vendor/offers.html", page=page, status=status)


@bp.route("/offres/nouvelle", methods=["GET", "POST"])
@bp.route("/offres/<int:offer_id>/modifier", methods=["GET", "POST"])
def offer_edit(offer_id=None):
    v = V()
    o = owned_or_404(db.session.get(Offer, offer_id), vendor_id=v.id) if offer_id else None
    cats = _category_options()
    cat_id = num("category_id", o.category_id if o else (v.category_ids[0] if v.category_ids else None))
    cat = db.session.get(Category, cat_id) if cat_id else None
    if request.method == "POST":
        if cat is None:
            flash("Choisissez une catégorie.", "danger")
        else:
            creating = o is None
            if creating:
                o = Offer(vendor_id=v.id, category_id=cat.id)
            o.category_id = cat.id
            errors = _read_offer(o, cat)
            if flash_errors(errors):
                db.session.rollback()
                return render_template("vendor/offer_form.html", o=o if not creating else None, form=request.form, categories=cats,
                                       cat=cat, regions=REGIONS, policies=CANCELLATION_POLICIES, draft=o)
            o.slug = _unique_offer_slug(o.title)
            if creating:
                db.session.add(o)
            _sync_options(o)
            db.session.flush()
            # photos
            added = _add_photos(o)
            refresh_search_fields(o)
            audit.log("offer.save", o, status=o.status)
            db.session.commit()
            flash("Offre enregistrée" + (f" ({added} photo(s) ajoutée(s))" if added else "") + ".", "success")
            return redirect(url_for("vendor.offer_edit", offer_id=o.id))
    form = request.form if request.method == "POST" else _offer_to_form(o)
    return render_template("vendor/offer_form.html", o=o, form=form, categories=cats, cat=cat, regions=REGIONS,
                           policies=CANCELLATION_POLICIES, draft=o)


def _offer_to_form(o):
    if o is None:
        return {"offer_type": "service", "price_model": "fixed", "price_unit": "event", "deposit_percent": 30,
                "min_notice_days": 2, "min_quantity": 1, "concurrent_capacity": 1, "cancellation_policy": "moderate",
                "online_payment": "1", "city": V().city or ""}
    f = {c: getattr(o, c) for c in ("title", "description", "inclusions", "conditions", "offer_type", "price_model", "price_unit",
                                    "price_xof", "price_max_xof", "min_quantity", "capacity", "stock_qty", "concurrent_capacity",
                                    "min_notice_days", "buffer_before_days", "buffer_after_days", "city", "region", "travel_fee_xof",
                                    "delivery_fee_xof", "install_fee_xof", "deposit_percent", "caution_xof", "cancellation_policy")}
    f["online_payment"] = "1" if o.online_payment else ""
    f["tiers"] = ";".join(f"{t['min_qty']}:{t['unit_price']}" for t in (o.tiers or []))
    for k, val in (o.attrs or {}).items():
        f[f"attr_{k}"] = val
    return f


def _add_photos(o):
    n = 0
    try:
        for f in request.files.getlist("photos")[:10]:
            if f and f.filename:
                path, thumb = files.save_image(f, "offers")
                db.session.add(Media(offer_id=o.id, path=path, thumb=thumb, alt=o.title, caption=txt("photo_caption", 200),
                                     position=len(o.media) + n, is_cover=(not o.media and n == 0)))
                n += 1
    except files.UploadError as e:
        flash(str(e), "danger")
    return n


@bp.route("/offres/<int:offer_id>/photos/<int:media_id>/<action>", methods=["POST"])
def offer_photo(offer_id, media_id, action):
    o = owned_or_404(db.session.get(Offer, offer_id), vendor_id=V().id)
    m = owned_or_404(db.session.get(Media, media_id), offer_id=o.id)
    if action == "cover":
        for x in o.media:
            x.is_cover = x.id == m.id
    elif action == "delete":
        was_cover = m.is_cover
        files.delete_public(m.path)
        files.delete_public(m.thumb)
        db.session.delete(m)
        db.session.flush()
        if was_cover:
            rest = Media.query.filter_by(offer_id=o.id).order_by(Media.position).first()
            if rest:
                rest.is_cover = True
    db.session.commit()
    return redirect(url_for("vendor.offer_edit", offer_id=o.id))


@bp.route("/offres/<int:offer_id>/action/<action>", methods=["POST"])
def offer_action(offer_id, action):
    v = V()
    o = owned_or_404(db.session.get(Offer, offer_id), vendor_id=v.id)
    if action == "publish":
        miss = o.missing_for_publication()
        if not v.is_validated:
            flash("Votre compte doit être validé par EventyStore avant de publier une offre.", "warning")
        elif miss:  # AC-14
            flash("Offre incomplète. Il manque : " + " ; ".join(miss) + ".", "danger")
        elif not o.media:
            flash("Ajoutez au moins une photo avant de publier.", "danger")
        elif settings.get_bool("moderate_offers", True):
            o.status, o.refusal_reason = "submitted", None
            flash("Offre envoyée en modération. Elle sera publiée après validation.", "success")
        else:
            o.status, o.published_at, o.refusal_reason = "active", utcnow(), None
            flash("Offre publiée.", "success")
    elif action == "deactivate" and o.status in ("active", "submitted"):
        o.status = "draft"
        flash("Offre désactivée (les commandes passées restent consultables).", "info")
    elif action == "archive":
        o.status = "archived"
        flash("Offre archivée.", "info")
    elif action == "restore" and o.status == "archived":
        o.status = "draft"
    elif action == "duplicate":
        c = Offer(**{k: getattr(o, k) for k in ("vendor_id", "category_id", "description", "offer_type", "price_model", "price_unit",
                    "price_xof", "price_max_xof", "min_quantity", "capacity", "stock_qty", "concurrent_capacity", "min_notice_days",
                    "buffer_before_days", "buffer_after_days", "region", "city", "service_areas", "travel_fee_xof", "delivery_fee_xof",
                    "install_fee_xof", "deposit_percent", "caution_xof", "cancellation_policy", "conditions", "inclusions", "attrs",
                    "tiers", "online_payment")}, title=f"{o.title} (copie)", status="draft")
        c.slug = _unique_offer_slug(c.title)
        for op in o.options:
            c.options.append(OfferOption(name=op.name, price_xof=op.price_xof, price_type=op.price_type, required=op.required))
        db.session.add(c)
        db.session.flush()
        refresh_search_fields(c)
        db.session.commit()
        flash("Offre dupliquée en brouillon.", "success")
        return redirect(url_for("vendor.offer_edit", offer_id=c.id))
    audit.log(f"offer.{action}", o)
    db.session.commit()
    return redirect(request.referrer if request.referrer and "/pro/" in request.referrer else url_for("vendor.offers"))


@bp.route("/offres/<int:offer_id>/statistiques")
def offer_stats(offer_id):
    o = owned_or_404(db.session.get(Offer, offer_id), vendor_id=V().id)
    reqs = QuoteRequest.query.filter_by(offer_id=o.id).count()
    booked = (db.session.query(db.func.count(db.distinct(Order.id))).join(Order.items).filter(Order.vendor_id == V().id)
              .filter(Order.status.in_(("confirmed", "in_progress", "completed"))).filter_by(offer_id=o.id).scalar())
    return render_template("vendor/offer_stats.html", o=o, reqs=reqs, booked=booked)


# ------------------------------------------------------------------ calendrier
@bp.route("/calendrier")
def calendar():
    v = V()
    today = date.today()
    y = parse_int(request.args.get("annee"), today.year, 2020, 2100)
    m = parse_int(request.args.get("mois"), today.month, 1, 12)
    first = date(y, m, 1)
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    days = {d: {"orders": [], "blocks": [], "requests": []} for d in daterange(first, last)}
    orders = Order.query.filter(Order.vendor_id == v.id, Order.start_date <= last, Order.end_date >= first,
                                Order.status.in_(("pending_payment", "pending_confirmation", "confirmed", "in_progress", "dispute"))).all()
    for o in orders:
        if o.status == "pending_payment" and not (o.hold_expires_at and o.hold_expires_at > utcnow()):
            continue
        for d in daterange(max(o.start_date, first), min(o.end_date, last)):
            days[d]["orders"].append(o)
    blocks = AvailabilityBlock.query.filter(AvailabilityBlock.vendor_id == v.id,
                                            db.or_(AvailabilityBlock.weekly.is_(True),
                                                   db.and_(AvailabilityBlock.start_date <= last, AvailabilityBlock.end_date >= first))).all()
    for b in blocks:
        for d in daterange(first, last):
            if (b.weekly and d >= b.start_date and d.weekday() == b.start_date.weekday()) or (not b.weekly and b.start_date <= d <= b.end_date):
                days[d]["blocks"].append(b)
    reqs = QuoteRequest.query.filter(QuoteRequest.vendor_id == v.id, QuoteRequest.status.in_(("open", "quoted", "negotiating")),
                                     QuoteRequest.start_date <= last, QuoteRequest.end_date >= first).all()
    for r in reqs:
        for d in daterange(max(r.start_date, first), min(r.end_date, last)):
            days[d]["requests"].append(r)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(y, m)
    prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
    next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
    my_offers = Offer.query.filter(Offer.vendor_id == v.id, Offer.status != "archived").all()
    upcoming_blocks = AvailabilityBlock.query.filter(AvailabilityBlock.vendor_id == v.id,
                                                     db.or_(AvailabilityBlock.weekly.is_(True), AvailabilityBlock.end_date >= today)).order_by(AvailabilityBlock.start_date).all()
    return render_template("vendor/calendar.html", weeks=weeks, days=days, y=y, m=m, month_name=MONTHS_FR[m - 1], today_date=today,
                           prev=(prev_y, prev_m), next=(next_y, next_m), offers=my_offers, blocks=upcoming_blocks)


@bp.route("/calendrier/bloquer", methods=["POST"])
def block_add():
    v = V()
    start, end = dt("start_date"), dt("end_date") or dt("start_date")
    if not start or end < start:
        flash("Dates invalides.", "danger")
    else:
        offer_id = num("offer_id", None)
        if offer_id:
            owned_or_404(db.session.get(Offer, offer_id), vendor_id=v.id)
        db.session.add(AvailabilityBlock(vendor_id=v.id, offer_id=offer_id, start_date=start, end_date=end,
                                         reason=txt("reason", 200), weekly=flag("weekly")))
        db.session.commit()
        flash("Période bloquée.", "success")
    return redirect(url_for("vendor.calendar", annee=(start or date.today()).year, mois=(start or date.today()).month))


@bp.route("/calendrier/bloc/<int:block_id>/supprimer", methods=["POST"])
def block_delete(block_id):
    b = owned_or_404(db.session.get(AvailabilityBlock, block_id), vendor_id=V().id)
    db.session.delete(b)
    db.session.commit()
    flash("Blocage supprimé.", "info")
    return redirect(request.referrer or url_for("vendor.calendar"))


# ------------------------------------------------------------------ devis
def _req(req_id):
    return owned_or_404(db.session.get(QuoteRequest, req_id), vendor_id=V().id)


@bp.route("/devis")
def quotes():
    status = request.args.get("statut", "")
    q = QuoteRequest.query.filter_by(vendor_id=V().id)
    if status:
        q = q.filter_by(status=status)
    if request.args.get("categorie"):
        q = q.join(Offer, QuoteRequest.offer_id == Offer.id).filter(Offer.category_id == parse_int(request.args.get("categorie"), 0))
    page = paginate(q.order_by(QuoteRequest.updated_at.desc()))
    cats = Category.query.filter_by(parent_id=None).all()
    return render_template("vendor/quotes.html", page=page, status=status, cats=cats)


@bp.route("/devis/<int:req_id>")
def quote_detail(req_id):
    req = _req(req_id)
    conv = messaging.for_quote(req)
    db.session.commit()
    messaging.mark_read(conv, current_user)
    db.session.commit()
    last = req.latest_quote
    prefill = _prefill_lines(req, last)
    diffs = {q.id: quote_service.diff_with_previous(q) for q in req.quotes}
    av = availability.check(req.offer, req.start_date, req.end_date, req.quantity or 1) if req.offer else None
    return render_template("vendor/quote_detail.html", req=req, last=last, conv=conv, prefill=prefill, diffs=diffs, av=av,
                           templates=messaging.TEMPLATES["vendor"], policies=CANCELLATION_POLICIES,
                           default_valid=date.today() + timedelta(days=settings.get_int("quote_default_validity_days", 7)),
                           order=Order.query.filter_by(quote_id=req.accepted_quote.id).first() if req.accepted_quote else None)


def _prefill_lines(req, last):
    if last:
        return [{"kind": l.kind, "label": l.label, "quantity": l.quantity, "unit": l.unit, "unit_price_xof": l.unit_price_xof,
                 "discount_xof": l.discount_xof} for l in last.lines]
    o = req.offer
    if o and o.price_xof:
        calc = pricing.compute(o, req.start_date, req.end_date, req.guests or req.quantity or 1, req.guests, [],
                               req.event.region if req.event else None)
        return [{"kind": l["kind"] if l["kind"] in ("item", "travel", "delivery") else "item", "label": l["label"],
                 "quantity": l["quantity"], "unit": l["unit"], "unit_price_xof": l["unit_price_xof"], "discount_xof": 0}
                for l in calc["lines"]]
    return [{"kind": "item", "label": o.title if o else "Prestation", "quantity": 1, "unit": "forfait", "unit_price_xof": 0, "discount_xof": 0}]


@bp.route("/devis/<int:req_id>/envoyer", methods=["POST"])
def quote_send(req_id):
    req = _req(req_id)
    try:
        lines = quote_service.parse_lines(request.form)
        quote_service.build_quote(
            req, current_user, "vendor", lines, valid_until=dt("valid_until"), deposit_percent=num("deposit_percent", 30, 0, 100),
            balance_days=num("balance_days", 3, 0, 90), description=txt("description", 3000), exclusions=txt("exclusions", 2000),
            conditions=txt("conditions", 3000), policy=txt("policy", 12) if txt("policy", 12) in CANCELLATION_POLICIES else "moderate",
            note=txt("note", 2000), start=dt("start_date") or req.start_date, end=dt("end_date") or req.end_date,
            quantity=num("quantity", req.quantity, 1), send=request.form.get("send") != "draft")
        db.session.commit()
        flash("Devis enregistré comme brouillon." if request.form.get("send") == "draft" else "Devis envoyé à l'acheteur.", "success")
    except quote_service.QuoteError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("vendor.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/versions/<int:quote_id>/<action>", methods=["POST"])
def quote_action(req_id, quote_id, action):
    req = _req(req_id)
    q = owned_or_404(db.session.get(Quote, quote_id), request_id=req.id)
    try:
        if action == "accept":
            quote_service.accept(q, current_user)
            flash("Contre-proposition acceptée. L'acheteur peut maintenant réserver et payer l'acompte.", "success")
        elif action == "withdraw":
            quote_service.withdraw(q)
            flash("Devis retiré.", "info")
        elif action == "send-draft":
            quote_service.send_draft(q)
            flash("Devis envoyé.", "success")
        db.session.commit()
    except quote_service.QuoteError as e:
        db.session.commit()
        flash(str(e), "danger")
    return redirect(url_for("vendor.quote_detail", req_id=req.id))


@bp.route("/devis/<int:req_id>/refuser", methods=["POST"])
def quote_decline(req_id):
    req = _req(req_id)
    try:
        quote_service.decline(req, current_user, txt("reason", 500))
        db.session.commit()
        flash("Demande refusée.", "info")
    except quote_service.QuoteError as e:
        flash(str(e), "danger")
    return redirect(url_for("vendor.quote_detail", req_id=req.id))


# ------------------------------------------------------------------ commandes
def _order(order_id):
    return owned_or_404(db.session.get(Order, order_id), vendor_id=V().id)


@bp.route("/commandes")
def orders():
    status = request.args.get("statut", "")
    q = Order.query.filter_by(vendor_id=V().id)
    if status:
        q = q.filter_by(status=status)
    if parse_date(request.args.get("du")):
        q = q.filter(Order.start_date >= parse_date(request.args.get("du")))
    if parse_date(request.args.get("au")):
        q = q.filter(Order.start_date <= parse_date(request.args.get("au")))
    page = paginate(q.order_by(Order.start_date.desc()))
    return render_template("vendor/orders.html", page=page, status=status)


@bp.route("/commandes/<int:order_id>")
def order_detail(order_id):
    o = _order(order_id)
    conv = messaging.for_order(o)
    db.session.commit()
    messaging.mark_read(conv, current_user)
    db.session.commit()
    return render_template("vendor/order_detail.html", o=o, conv=conv, templates=messaging.TEMPLATES["vendor"],
                           offline=(o.snapshot or {}).get("offline"))


@bp.route("/commandes/<int:order_id>/<action>", methods=["POST"])
def order_action(order_id, action):
    o = _order(order_id)
    try:
        if action == "confirm":
            order_service.confirm(o)
            flash("Réservation confirmée. Elle est ajoutée à votre calendrier.", "success")
        elif action == "refuse":
            order_service.vendor_refuse(o, txt("reason", 500))
            flash("Réservation refusée ; l'acheteur est remboursé intégralement.", "info")
        elif action == "cancel":
            order_service.cancel(o, by="vendor", reason=txt("reason", 500) or "Annulation par le prestataire", actor=current_user)
            flash("Réservation annulée ; l'acheteur est remboursé intégralement.", "info")
        elif action == "complete":
            order_service.complete(o)
            flash("Prestation clôturée. Vos fonds seront disponibles après le délai de sécurité.", "success")
        elif action == "offline-payment":
            amount = num("amount", 0, 1)
            if not (o.snapshot or {}).get("offline") or not amount or amount > o.remaining_xof:
                flash("Montant invalide.", "danger")
            else:
                order_service.mark_offline_payment(o, amount)
                flash("Paiement hors plateforme enregistré (non protégé par EventyStore).", "success")
        db.session.commit()
    except order_service.OrderError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("vendor.order_detail", order_id=o.id))


@bp.route("/commandes/<int:order_id>/incident", methods=["POST"])
def order_incident(order_id):
    o = _order(order_id)
    desc = txt("description", 4000)
    if len(desc) < 15:
        flash("Décrivez l'incident (15 caractères minimum).", "danger")
    else:
        t = Ticket(reference=new_reference("TK"), author_id=current_user.id, order_id=o.id, category="other",
                   subject=f"Incident prestataire — commande {o.reference}", description=desc)
        t.messages.append(TicketMessage(author_id=current_user.id, body=desc))
        db.session.add(t)
        db.session.commit()
        flash("Incident signalé au support.", "success")
        return redirect(url_for("account.ticket_detail", ticket_id=t.id))
    return redirect(url_for("vendor.order_detail", order_id=o.id))


@bp.route("/commandes/<int:order_id>/recapitulatif")
def order_summary(order_id):
    return render_template("shared/order_print.html", o=_order(order_id), viewer="vendor")


# ------------------------------------------------------------------ revenus
@bp.route("/revenus", methods=["GET", "POST"])
def revenue():
    v = V()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "payout_settings":
            if not current_user.check_password(request.form.get("password", "")):  # vérification renforcée
                flash("Mot de passe incorrect : modification refusée.", "danger")
            else:
                method, acct, holder = txt("method", 24), txt("account", 60), txt("holder", 120)
                if method not in ("wave", "orange_money", "bank") or len(acct) < 6 or not holder:
                    flash("Renseignez le moyen, le numéro de compte/portefeuille et le titulaire.", "danger")
                else:
                    v.payout_method, v.payout_holder = method, holder
                    v.payout_account_enc = files.encrypt_text(acct)
                    v.payout_account_mask = "•••• " + acct[-4:]
                    v.payout_updated_at = utcnow()
                    audit.log("vendor.payout_settings", v, method=method)
                    notify(current_user, "security", "Coordonnées de reversement modifiées",
                           "Si vous n'êtes pas à l'origine de ce changement, contactez le support immédiatement.", critical=True)
                    db.session.commit()
                    flash("Coordonnées de reversement enregistrées.", "success")
        elif action == "payout_request":
            bal = finance.vendor_balance(v.id)
            amount = num("amount", 0, 0)
            minimum = settings.get_int("payout_min_xof", 5000)
            if not v.payout_method:
                flash("Renseignez d'abord vos coordonnées de reversement.", "warning")
            elif amount < minimum or amount > bal["available"]:
                flash(f"Montant invalide : minimum {fmt_xof(minimum)}, maximum {fmt_xof(bal['available'])}.", "danger")
            else:
                p = Payout(reference=new_reference("RV"), vendor_id=v.id, amount_xof=amount, method=v.payout_method,
                           destination_mask=v.payout_account_mask)
                db.session.add(p)
                db.session.flush()
                db.session.add(LedgerEntry(vendor_id=v.id, payout_id=p.id, kind="payout_debit", amount_xof=-amount, available=True,
                                           note=f"Demande de retrait {p.reference}"))
                audit.log("payout.request", p, amount=amount)
                db.session.commit()
                flash("Demande de retrait envoyée. Elle sera vérifiée par l'équipe finance.", "success")
        return redirect(url_for("vendor.revenue"))
    bal = finance.vendor_balance(v.id)
    pays = (Payment.query.join(Order).filter(Order.vendor_id == v.id, Payment.status.in_(("succeeded", "refunded", "partially_refunded")))
            .order_by(Payment.id.desc()).limit(50).all())
    payouts = Payout.query.filter_by(vendor_id=v.id).order_by(Payout.id.desc()).all()
    return render_template("vendor/revenue.html", v=v, bal=bal, pays=pays, payouts=payouts,
                           minimum=settings.get_int("payout_min_xof", 5000), delay=settings.get_int("payout_delay_days", 2))


@bp.route("/revenus/export.csv")
def revenue_export():
    import csv
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Date", "Commande", "Type", "Montant brut FCFA", "Commission FCFA", "Net FCFA", "Disponible"])
    for e in LedgerEntry.query.filter_by(vendor_id=V().id).order_by(LedgerEntry.id):
        order = db.session.get(Order, e.order_id) if e.order_id else None
        w.writerow([e.created_at.strftime("%Y-%m-%d %H:%M"), order.reference if order else "", e.kind, e.gross_xof, e.commission_xof, e.amount_xof, "oui" if e.available else "non"])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=eventystore-revenus.csv"})


# ------------------------------------------------------------------ promotions
@bp.route("/promotions", methods=["GET", "POST"])
def promos():
    v = V()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            code = txt("code", 30).upper().replace(" ", "")
            pct, amt = num("percent", None, 1, 90), num("amount_xof", None, 1)
            errors = []
            if len(code) < 3:
                errors.append("Le code doit contenir au moins 3 caractères.")
            if not pct and not amt:
                errors.append("Définissez une remise en % ou en FCFA.")
            if pct and pct > 50:
                errors.append("La remise maximale autorisée est de 50 %.")
            if Promo.query.filter_by(vendor_id=v.id, code=code).first():
                errors.append("Ce code existe déjà.")
            if not flash_errors(errors):
                db.session.add(Promo(vendor_id=v.id, code=code, percent=pct, amount_xof=None if pct else amt,
                                     offer_id=num("offer_id", None), valid_from=dt("valid_from"), valid_to=dt("valid_to"),
                                     max_uses=num("max_uses", None, 1)))
                db.session.commit()
                flash("Code promotionnel créé.", "success")
        elif action in ("toggle", "delete"):
            p = owned_or_404(db.session.get(Promo, num("promo_id", 0)), vendor_id=v.id)
            if action == "toggle":
                p.active = not p.active
            else:
                db.session.delete(p)
            db.session.commit()
        return redirect(url_for("vendor.promos"))
    return render_template("vendor/promos.html", promos=Promo.query.filter_by(vendor_id=v.id).order_by(Promo.id.desc()).all(),
                           offers=Offer.query.filter_by(vendor_id=v.id, status="active").all())


# ------------------------------------------------------------------ avis
@bp.route("/avis", methods=["GET", "POST"])
def reviews():
    v = V()
    if request.method == "POST":
        rv = owned_or_404(db.session.get(Review, num("review_id", 0)), vendor_id=v.id)
        action = request.form.get("action")
        if action == "reply":
            reply = txt("reply", 2000)
            if reply:
                rv.vendor_reply, rv.replied_at = reply, utcnow()
                db.session.commit()
                flash("Réponse publiée (l'avis d'origine n'est pas modifié).", "success")
        elif action == "report":
            rv.reported, rv.report_reason = True, txt("reason", 250) or "Signalé par le prestataire"
            db.session.commit()
            flash("Avis signalé à la modération.", "info")
        return redirect(url_for("vendor.reviews"))
    page = paginate(Review.query.filter_by(vendor_id=v.id).order_by(Review.created_at.desc()), 10)
    return render_template("vendor/reviews.html", page=page, v=v)
