"""Recherche, filtres, tri et suggestions (cahier §6). Filtrage et pagination côté serveur."""
from ..extensions import db
from ..models import Category, Offer, Review, User, VendorProfile
from ..utils import ALL_PLACES, PLACE_SYNONYMS, REGIONS, normalize, parse_date, parse_int, region_of
from . import availability

PER_PAGE = 12

KEYWORD_SYNONYMS = {
    "sono": "sonorisation", "son": "sonorisation", "musique": "dj", "photographe": "photo", "video": "video",
    "gateau": "patisserie", "buffet": "traiteur", "repas": "traiteur", "bache": "tente", "chapiteau": "tente",
    "chaise": "chaises", "table": "tables", "salle": "salle", "lumiere": "eclairage",
}


def refresh_search_fields(offer):
    """Recalcule les champs de recherche normalisés d'une offre (à appeler à chaque sauvegarde)."""
    cat = offer.category
    cat_names = []
    c = cat
    while c is not None:
        cat_names.append(c.name)
        c = c.parent
    v = offer.vendor
    parts = [offer.title, offer.description or "", offer.inclusions or "", v.business_name if v else "",
             (v.specialties or "") if v else "", " ".join(cat_names), offer.city or "", offer.region or "",
             " ".join(offer.service_areas or [])]
    offer.search_text = normalize(" ".join(parts))
    places = set(filter(None, [offer.city, offer.region] + list(offer.service_areas or [])))
    places |= {region_of(p) for p in list(places) if region_of(p)}
    offer.areas_text = "|" + "|".join(sorted(normalize(p) for p in places)) + "|"


def _rating_subq():
    return (db.session.query(Review.offer_id.label("oid"), db.func.avg(Review.rating).label("avg"),
                             db.func.count(Review.id).label("n"))
            .filter(Review.status == "published").group_by(Review.offer_id).subquery())


def base_query():
    return (Offer.query.join(VendorProfile, Offer.vendor_id == VendorProfile.id)
            .join(User, VendorProfile.user_id == User.id)
            .filter(Offer.status == "active", VendorProfile.verification_status == "validated", User.status == "active"))


def parse_params(args):
    return {
        "q": (args.get("q") or "").strip()[:100],
        "category": (args.get("category") or "").strip(),
        "place": (args.get("place") or "").strip()[:80],
        "start": parse_date(args.get("start")),
        "end": parse_date(args.get("end")),
        "guests": parse_int(args.get("guests"), None, 1, 100000),
        "bmin": parse_int(args.get("bmin"), None, 0),
        "bmax": parse_int(args.get("bmax"), None, 0),
        "pmodel": args.get("pmodel") if args.get("pmodel") in ("fixed", "from", "range", "quote") else "",
        "punit": args.get("punit") or "",
        "otype": args.get("otype") or "",
        "rating": parse_int(args.get("rating"), None, 1, 5),
        "verified": args.get("verified") == "1",
        "delivery": args.get("delivery") == "1",
        "travel": args.get("travel") == "1",
        "online": args.get("online") == "1",
        "sort": args.get("sort") if args.get("sort") in ("relevance", "price_asc", "price_desc", "rating", "popular", "recent") else "relevance",
        "page": parse_int(args.get("page"), 1, 1),
    }


def search(params):
    """Retourne (offres_de_la_page, total, pages, notes) — `notes` explique les écarts (AC-01)."""
    q = base_query()
    notes = []
    rs = _rating_subq()
    q = q.outerjoin(rs, rs.c.oid == Offer.id)

    cat = Category.query.filter_by(slug=params["category"], active=True).first() if params["category"] else None
    if cat:
        q = q.filter(Offer.category_id.in_(cat.descendant_ids()))

    text = normalize(params["q"])
    if text:
        for tok in text.split():
            tok = KEYWORD_SYNONYMS.get(tok, tok)
            q = q.filter(Offer.search_text.like(f"%{tok}%"))

    place = params["place"]
    if place:
        key = normalize(PLACE_SYNONYMS.get(normalize(place), place))
        q = q.filter(Offer.areas_text.like(f"%|{key}|%"))

    if params["bmin"] is not None or params["bmax"] is not None:
        q = q.filter(Offer.price_xof.isnot(None))
        if params["bmin"] is not None:
            q = q.filter(Offer.price_xof >= params["bmin"])
        if params["bmax"] is not None:
            q = q.filter(Offer.price_xof <= params["bmax"])
        notes.append("Le filtre budget compare des prix unitaires : vérifiez l'unité (par personne, par jour, forfait…).")
    if params["pmodel"]:
        q = q.filter(Offer.price_model == params["pmodel"])
    if params["punit"]:
        q = q.filter(Offer.price_unit == params["punit"])
    if params["otype"]:
        q = q.filter(Offer.offer_type == params["otype"])
    if params["guests"]:
        q = q.filter(db.or_(Offer.capacity.is_(None), Offer.capacity >= params["guests"]))
    if params["rating"]:
        q = q.filter(rs.c.avg >= params["rating"])
    if params["delivery"]:
        q = q.filter(Offer.delivery_fee_xof.isnot(None), Offer.offer_type.in_(("rental", "sale")))
    if params["travel"]:
        q = q.filter(Offer.travel_fee_xof.isnot(None))
    if params["online"]:
        q = q.filter(Offer.online_payment.is_(True), Offer.price_model != "quote")

    sort = params["sort"]
    avg = db.func.coalesce(rs.c.avg, 0)
    if sort == "price_asc":
        q = q.order_by(Offer.price_xof.is_(None), Offer.price_xof.asc())
    elif sort == "price_desc":
        q = q.order_by(Offer.price_xof.is_(None), Offer.price_xof.desc())
    elif sort == "rating":
        q = q.order_by(avg.desc(), db.func.coalesce(rs.c.n, 0).desc())
    elif sort == "popular":
        q = q.order_by((Offer.views + Offer.clicks * 3).desc())
    elif sort == "recent":
        q = q.order_by(Offer.published_at.desc())
    else:  # pertinence : aucun classement payant (les offres sponsorisées sont seulement étiquetées)
        q = q.order_by(avg.desc(), Offer.clicks.desc(), Offer.id.desc())

    start, end = params["start"], params["end"]
    if start:
        end = end or start
        offers = q.all()
        bad = availability.unavailable_offer_ids(offers, start, end)
        kept = [o for o in offers if o.id not in bad]
        if len(kept) < len(offers):
            notes.append(f"{len(offers) - len(kept)} offre(s) masquée(s) car indisponibles à cette date.")
        total = len(kept)
        pages = max(1, -(-total // PER_PAGE))
        page = min(params["page"], pages)
        return kept[(page - 1) * PER_PAGE: page * PER_PAGE], total, pages, notes, page
    total = q.order_by(None).count() if False else q.count()
    pages = max(1, -(-total // PER_PAGE))
    page = min(params["page"], pages)
    return q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all(), total, pages, notes, page


def suggestions(term, limit=8):
    term = normalize(term)
    if len(term) < 2:
        return []
    out = []
    for c in Category.query.filter(Category.active.is_(True)).order_by(Category.position).all():
        if term in normalize(c.name):
            out.append({"type": "category", "label": c.name, "url": f"/c/{c.slug}"})
    for p in ALL_PLACES:
        if normalize(p).startswith(term):
            out.append({"type": "place", "label": p, "value": p})
    vendors = (VendorProfile.query.filter(VendorProfile.verification_status == "validated")
               .filter(VendorProfile.business_name.ilike(f"%{term}%")).limit(4).all())
    for v in vendors:
        out.append({"type": "vendor", "label": v.business_name, "url": f"/prestataires/{v.slug}"})
    offers = (base_query().filter(Offer.search_text.like(f"%{term}%")).limit(4).all())
    for o in offers:
        out.append({"type": "offer", "label": o.title, "url": f"/offres/{o.id}-{o.slug}"})
    return out[:limit]
