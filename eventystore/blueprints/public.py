"""Site public : landing, catalogue, recherche, fiches, comparaison, pages de contenu, SEO."""
from datetime import date

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user

from ..extensions import db
from ..models import (Category, ContactMessage, Favorite, HelpArticle, Offer, Order, Page, Review, User,
                      VendorProfile)
from ..services import availability, pricing, search as search_service, settings
from ..services.notifications import send_email
from ..utils import REGIONS, parse_date, parse_int, slugify
from ._helpers import EMAIL_RE, flash_errors, txt

bp = Blueprint("public", __name__)


@bp.route("/")
def home():
    cats = Category.query.filter_by(parent_id=None, active=True).order_by(Category.position).all()
    base = search_service.base_query()
    featured = base.filter(Offer.featured.is_(True)).order_by(Offer.published_at.desc()).limit(8).all()
    if len(featured) < 4:  # complète avec les offres les mieux notées / récentes
        ids = [o.id for o in featured]
        extra = base.filter(~Offer.id.in_(ids or [0])).order_by(Offer.published_at.desc()).limit(8 - len(featured)).all()
        featured += extra
    vendors = (VendorProfile.query.filter_by(verification_status="validated").order_by(VendorProfile.verified_at.desc())
               .limit(6).all())
    reviews = (Review.query.filter_by(status="published").filter(Review.rating >= 4, Review.comment.isnot(None))
               .order_by(Review.created_at.desc()).limit(3).all())
    stats = {
        "vendors": VendorProfile.query.filter_by(verification_status="validated").count(),
        "offers": base.count(),
        "orders": Order.query.filter(Order.status.in_(("confirmed", "in_progress", "completed"))).count(),
    }
    return render_template("public/home.html", categories=cats, featured=featured, vendors=vendors, reviews=reviews,
                           stats=stats, regions=list(REGIONS), faq=_FAQ_SHORT)


_FAQ_SHORT = [
    ("Comment réserver un prestataire ?", "Recherchez une offre, vérifiez sa disponibilité, puis réservez directement si le prix est fixe ou demandez un devis personnalisé. Vous payez l'acompte en ligne et le prestataire confirme."),
    ("Puis-je négocier le prix ?", "Oui. Sur une demande de devis, vous pouvez accepter, refuser ou faire une contre-proposition. Chaque version est conservée et datée."),
    ("Mon argent est-il protégé ?", "Les paiements effectués sur EventyStore sont enregistrés et rapprochés côté serveur. Le prestataire n'est crédité qu'après la prestation, selon les conditions d'annulation affichées avant paiement."),
    ("Comment devenir prestataire ?", "Créez un compte prestataire, envoyez vos justificatifs : notre équipe les vérifie sous 24 à 48 heures ouvrées, puis vous pouvez publier vos offres."),
]


@bp.route("/categories")
def categories():
    cats = Category.query.filter_by(parent_id=None, active=True).order_by(Category.position).all()
    return render_template("public/categories.html", categories=cats)


def _render_results(category=None):
    params = search_service.parse_params(request.args)
    if category:
        params["category"] = category.slug
    offers, total, pages, notes, page = search_service.search(params)
    all_cats = Category.query.filter_by(parent_id=None, active=True).order_by(Category.position).all()
    suggestions = []
    if total == 0:
        suggestions = ["Élargissez la zone géographique ou retirez le filtre de ville", "Modifiez la date ou le budget",
                       "Envoyez une demande générale à plusieurs prestataires depuis la page d'un prestataire"]
    favs = set()
    if current_user.is_authenticated:
        favs = {f.offer_id for f in Favorite.query.filter_by(user_id=current_user.id).filter(Favorite.offer_id.isnot(None))}
    q_args = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("public/search.html", offers=offers, total=total, pages=pages, page=page, params=params,
                           category=category, all_cats=all_cats, notes=notes, empty_help=suggestions, favs=favs,
                           regions=REGIONS, q_args=q_args)


@bp.route("/recherche")
def search():
    return _render_results()


@bp.route("/c/<slug>")
def category(slug):
    cat = Category.query.filter_by(slug=slug, active=True).first_or_404()
    return _render_results(cat)


@bp.route("/offres/<int:offer_id>-<slug>")
@bp.route("/offres/<int:offer_id>")
def offer(offer_id, slug=None):
    o = db.session.get(Offer, offer_id) or abort(404)
    owner = current_user.is_authenticated and (
        (current_user.is_vendor and current_user.vendor and current_user.vendor.id == o.vendor_id) or current_user.is_staff)
    if (o.status != "active" or not o.vendor.is_validated) and not owner:
        abort(404)
    if slug != o.slug and o.slug:
        return redirect(url_for("public.offer", offer_id=o.id, slug=o.slug), 301)
    seen = session.setdefault("seen", [])
    if o.id not in seen and not owner:
        o.views = (o.views or 0) + 1
        seen.append(o.id)
        session["seen"] = seen[-60:]
        db.session.commit()
    reviews = Review.query.filter_by(offer_id=o.id, status="published").order_by(Review.created_at.desc()).limit(10).all()
    similar = (search_service.base_query().filter(Offer.category_id == o.category_id, Offer.id != o.id)
               .order_by(Offer.views.desc()).limit(4).all())
    is_fav = bool(current_user.is_authenticated and Favorite.query.filter_by(user_id=current_user.id, offer_id=o.id).first())
    start, end = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    av = availability.check(o, start, end or start, parse_int(request.args.get("qty"), 1, 1)) if start else None
    events = []
    if current_user.is_authenticated and current_user.is_buyer:
        from ..models import Event
        events = Event.query.filter_by(buyer_id=current_user.id, status="planned").order_by(Event.start_date).all()
    return render_template("public/offer.html", o=o, reviews=reviews, similar=similar, is_fav=is_fav, av=av,
                           start=start, end=end, events=events, preview=o.status != "active")


@bp.route("/prestataires/<slug>")
def vendor(slug):
    v = VendorProfile.query.filter_by(slug=slug).first_or_404()
    owner = current_user.is_authenticated and (current_user.is_staff or (current_user.is_vendor and current_user.vendor and current_user.vendor.id == v.id))
    if not v.is_validated and not owner:
        abort(404)
    offers = v.offers.filter_by(status="active").order_by(Offer.published_at.desc()).all()
    reviews = Review.query.filter_by(vendor_id=v.id, status="published").order_by(Review.created_at.desc()).limit(10).all()
    from ..models import Media
    gallery = Media.query.filter_by(vendor_id=v.id).order_by(Media.position).all()
    done = Order.query.filter_by(vendor_id=v.id, status="completed").count()
    is_fav = bool(current_user.is_authenticated and Favorite.query.filter_by(user_id=current_user.id, vendor_id=v.id).first())
    return render_template("public/vendor.html", v=v, offers=offers, reviews=reviews, gallery=gallery, done=done,
                           is_fav=is_fav, preview=not v.is_validated)


# ------------------------------------------------------------------ favoris & comparaison
@bp.route("/favoris/offre/<int:offer_id>", methods=["POST"])
def toggle_favorite(offer_id):
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.referrer or url_for("public.home")))
    o = db.session.get(Offer, offer_id) or abort(404)
    fav = Favorite.query.filter_by(user_id=current_user.id, offer_id=o.id).first()
    if fav:
        db.session.delete(fav)
        state = False
    else:
        db.session.add(Favorite(user_id=current_user.id, offer_id=o.id))
        state = True
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(favorite=state)
    return redirect(request.referrer or url_for("public.offer", offer_id=o.id, slug=o.slug))


@bp.route("/favoris/prestataire/<int:vendor_id>", methods=["POST"])
def toggle_favorite_vendor(vendor_id):
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.referrer or url_for("public.home")))
    v = db.session.get(VendorProfile, vendor_id) or abort(404)
    fav = Favorite.query.filter_by(user_id=current_user.id, vendor_id=v.id).first()
    if fav:
        db.session.delete(fav)
    else:
        db.session.add(Favorite(user_id=current_user.id, vendor_id=v.id))
    db.session.commit()
    return redirect(request.referrer or url_for("public.vendor", slug=v.slug))


@bp.route("/comparer/toggle/<int:offer_id>", methods=["POST"])
def compare_toggle(offer_id):
    o = db.session.get(Offer, offer_id) or abort(404)
    ids = session.get("compare", [])
    msg = None
    if offer_id in ids:
        ids.remove(offer_id)
    else:
        if len(ids) >= 4:
            msg = "Vous pouvez comparer 4 offres maximum."
        else:
            first = db.session.get(Offer, ids[0]) if ids else None
            if first and first.category.root.id != o.category.root.id:
                msg = "Comparez des offres de la même famille (ex. deux traiteurs)."
            else:
                ids.append(offer_id)
    session["compare"] = ids
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ids=ids, message=msg)
    if msg:
        flash(msg, "warning")
    return redirect(request.referrer or url_for("public.compare"))


@bp.route("/comparer")
def compare():
    ids = session.get("compare", [])
    offers = [o for o in (db.session.get(Offer, i) for i in ids) if o and o.status == "active"]
    return render_template("public/compare.html", offers=offers)


@bp.route("/comparer/vider", methods=["POST"])
def compare_clear():
    session["compare"] = []
    return redirect(url_for("public.compare"))


# ------------------------------------------------------------------ pages de contenu
@bp.route("/comment-ca-marche")
def how_it_works():
    return render_template("public/how.html")


@bp.route("/devenir-prestataire")
def become_vendor():
    return render_template("public/become_vendor.html", cats=Category.query.filter_by(parent_id=None, active=True)
                           .order_by(Category.position).all())


@bp.route("/a-propos")
def about():
    return render_template("public/about.html")


@bp.route("/faq")
def faq():
    articles = HelpArticle.query.filter_by(published=True).order_by(HelpArticle.position).all()
    return render_template("public/faq.html", articles=articles, faq=_FAQ_SHORT)


@bp.route("/aide")
def help_center():
    q = txt("q", 80) if request.method == "POST" else (request.args.get("q") or "").strip()[:80]
    audience = request.args.get("pour") if request.args.get("pour") in ("buyer", "vendor") else None
    query = HelpArticle.query.filter_by(published=True)
    if audience:
        query = query.filter(HelpArticle.audience.in_((audience, "all")))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(HelpArticle.title.ilike(like), HelpArticle.body.ilike(like)))
    articles = query.order_by(HelpArticle.section, HelpArticle.position).all()
    return render_template("public/help.html", articles=articles, q=q, audience=audience)


@bp.route("/aide/<slug>")
def help_article(slug):
    a = HelpArticle.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("public/help_article.html", a=a)


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name, email, subject, body = txt("name", 120), txt("email", 190), txt("subject", 200), txt("body", 4000)
        errors = []
        if not name:
            errors.append("Indiquez votre nom.")
        if not EMAIL_RE.match(email):
            errors.append("Adresse e-mail invalide.")
        if len(body) < 10:
            errors.append("Votre message est trop court.")
        if txt("website"):  # champ piège anti-spam (honeypot)
            return redirect(url_for("public.contact"))
        if not flash_errors(errors):
            db.session.add(ContactMessage(name=name, email=email.lower(), subject=subject, body=body))
            db.session.commit()
            flash("Merci ! Votre message a bien été enregistré, nous vous répondrons rapidement.", "success")
            return redirect(url_for("public.contact"))
    return render_template("public/contact.html", form=request.form)


@bp.route("/p/<slug>")
def page(slug):
    p = Page.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("public/page.html", p=p)


# ------------------------------------------------------------------ SEO
@bp.route("/robots.txt")
def robots():
    base = settings_url()
    body = f"User-agent: *\nDisallow: /espace/\nDisallow: /pro/\nDisallow: /admin/\nDisallow: /api/\nDisallow: /messages/\nSitemap: {base}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


def settings_url():
    from flask import current_app
    return current_app.config["SITE_URL"].rstrip("/")


@bp.route("/sitemap.xml")
def sitemap():
    base = settings_url()
    urls = [f"{base}/", f"{base}/categories", f"{base}/comment-ca-marche", f"{base}/devenir-prestataire", f"{base}/faq",
            f"{base}/contact", f"{base}/a-propos"]
    urls += [f"{base}/c/{c.slug}" for c in Category.query.filter_by(active=True).all()]
    urls += [f"{base}/prestataires/{v.slug}" for v in VendorProfile.query.filter_by(verification_status="validated").all()]
    urls += [f"{base}/offres/{o.id}-{o.slug}" for o in search_service.base_query().all()]
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")
    return Response(xml, mimetype="application/xml")


@bp.route("/dev/outbox")
def dev_outbox():
    from flask import current_app
    from ..models import EmailOutbox
    if current_app.config["ENV"] == "production":
        abort(404)
    rows = EmailOutbox.query.order_by(EmailOutbox.id.desc()).limit(40).all()
    return render_template("public/dev_outbox.html", rows=rows)
