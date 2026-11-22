"""Données initiales (catalogue, pages, aide) et données de DÉMONSTRATION séparables (`is_demo=True`)."""
from datetime import date, timedelta

from .extensions import db
from .icons import CATEGORY_ICONS
from .models import (Category, CommissionRule, Event, HelpArticle, Media, Offer, OfferOption, Order, Page, Promo,
                     QuoteRequest, Review, Setting, User, VendorProfile, utcnow)
from .services import settings
from .services.search import refresh_search_fields
from .utils import slugify

TAXONOMY = [
    ("Traiteur & restauration", "utensils", ["Traiteur complet", "Buffet", "Cocktail", "Repas à l'assiette", "Pâtisserie & gâteaux", "Boissons", "Cuisine locale", "Cuisine internationale", "Service serveur"]),
    ("Lieux & réception", "building", ["Salle de réception", "Hôtel", "Restaurant privatisable", "Espace extérieur", "Villa", "Salle polyvalente"]),
    ("Animation & musique", "music", ["DJ", "Orchestre", "Groupe musical", "Animateur", "MC / maître de cérémonie", "Artistes", "Jeux et animations"]),
    ("Décoration & scénographie", "flower", ["Décoration de salle", "Décoration florale", "Décoration de table", "Décoration thématique", "Mobilier décoratif", "Éclairage décoratif"]),
    ("Son, lumière & audiovisuel", "speaker", ["Sonorisation", "Micros", "Éclairage", "Écran & vidéoprojecteur", "Scène", "Régie technique", "Groupe électrogène"]),
    ("Mobilier & vaisselle", "chair", ["Chaises", "Tables", "Nappes", "Couverts & assiettes", "Verres", "Mobilier lounge"]),
    ("Tentes & structures", "tent", ["Bâches", "Chapiteaux", "Tentes", "Podiums", "Tribunes", "Barrières"]),
    ("Photo & vidéo", "camera", ["Photographe", "Vidéaste", "Photobooth", "Drone", "Impression photo"]),
    ("Autres services", "grid", ["Transport", "Sécurité", "Hôtesses", "Nettoyage", "Coiffure & maquillage", "Habillement", "Papeterie", "Cadeaux invités"]),
]

ATTRS = {
    "Traiteur & restauration": [{"key": "cuisine", "label": "Type de cuisine", "type": "text"}, {"key": "service", "label": "Service (buffet, assiette…)", "type": "text"}],
    "Lieux & réception": [{"key": "surface", "label": "Surface (m²)", "type": "number"}, {"key": "parking", "label": "Parking", "type": "text"}],
    "Son, lumière & audiovisuel": [{"key": "puissance", "label": "Puissance / caractéristiques", "type": "text"}],
    "Mobilier & vaisselle": [{"key": "matiere", "label": "Matière / coloris", "type": "text"}],
}

LEGAL_NOTICE = "> **Modèle de travail à faire valider juridiquement avant mise en production** (cahier des charges §19.3, §24).\n\n"

PAGES = {
    "conditions-generales": ("Conditions générales d'utilisation", LEGAL_NOTICE + "## 1. Objet\nEventyStore est une plateforme d'intermédiation numérique entre organisateurs d'événements et prestataires au Sénégal.\n\n## 2. Rôle de la plateforme\nEventyStore agit comme intermédiaire : le contrat de prestation est conclu entre l'acheteur et le prestataire. Les responsabilités, la facturation et les taxes applicables restent à valider.\n\n## 3. Comptes\nLes informations fournies doivent être exactes. EventyStore peut suspendre un compte en cas de manquement.\n\n## 4. Réservations et paiements\nUne demande de devis n'est pas une réservation ferme. La réservation est confirmée selon les conditions affichées avant paiement.\n\n## 5. Annulation\nVoir la politique d'annulation."),
    "confidentialite": ("Politique de confidentialité", LEGAL_NOTICE + "## Données collectées\nIdentité, coordonnées, données de réservation, documents de vérification des prestataires.\n\n## Finalités\nGestion des comptes, réservations, paiements, sécurité, support.\n\n## Vos droits\nAccès, rectification, suppression, opposition : depuis *Mon compte > Paramètres* ou via le support. Formalités auprès de la CDP (Sénégal) à confirmer."),
    "cookies": ("Politique cookies", LEGAL_NOTICE + "EventyStore n'utilise que des cookies strictement nécessaires (session, sécurité, comparateur). Aucun cookie publicitaire ou de mesure n'est déposé dans cette version."),
    "annulation": ("Politique d'annulation et de remboursement", LEGAL_NOTICE + "Chaque offre applique l'une de ces politiques, affichée avant paiement :\n\n* **Flexible** : remboursement intégral jusqu'à 7 jours avant l'événement ; 50 % entre 7 et 3 jours ; aucun ensuite.\n* **Modérée** : intégral jusqu'à 14 jours ; 50 % entre 14 et 7 jours ; aucun ensuite.\n* **Stricte** : 50 % jusqu'à 30 jours ; aucun ensuite.\n\nUne annulation par le prestataire ou avant sa confirmation donne lieu à un remboursement intégral."),
    "mentions-legales": ("Informations légales", LEGAL_NOTICE + "Raison sociale, adresse, NINEA et directeur de publication : **à compléter**."),
}

HELP = [
    ("buyer", "Démarrer", "Créer un événement", "Depuis votre espace, *Mes événements > Nouvel événement*. Les informations (date, lieu, invités, budget) sont réutilisées dans vos demandes de devis."),
    ("buyer", "Devis", "Demander un devis et négocier", "Ouvrez une offre puis **Demander un devis**. Le prestataire répond avec un devis chiffré. Vous pouvez accepter, refuser ou faire une **contre-proposition** ; chaque version est conservée."),
    ("buyer", "Réservation", "Réserver et payer", "Pour une offre à prix fixe, ajoutez-la au panier. Le créneau est bloqué pendant le paiement de l'acompte. La réservation est confirmée une fois le prestataire d'accord."),
    ("buyer", "Réservation", "Annuler une réservation", "Le montant remboursé dépend de la politique d'annulation de l'offre et du délai avant l'événement ; il est affiché avant de confirmer."),
    ("vendor", "Démarrer", "Devenir prestataire", "Créez un compte prestataire, déposez vos justificatifs : l'équipe vérifie sous 24 à 48 h ouvrées."),
    ("vendor", "Offres", "Créer une offre", "Renseignez titre, description, prix et unité, zone, conditions, au moins une photo. L'offre est publiée après modération."),
    ("vendor", "Agenda", "Gérer mon calendrier", "Bloquez des dates ponctuelles ou récurrentes, définissez délais de préparation et de retour."),
    ("vendor", "Revenus", "Retirer mes gains", "Le net (après commission) devient disponible après clôture de la prestation et un délai de sécurité. Demandez un retrait depuis *Revenus*."),
]

DEMO_PASSWORD = "Demo1234"


def seed_core():
    """Catalogue, commission par défaut, pages et aide. Idempotent."""
    if Category.query.count() == 0:
        for i, (name, icon, subs) in enumerate(TAXONOMY):
            root = Category(name=name, slug=slugify(name, 100), icon=icon, position=i, attributes=ATTRS.get(name, []),
                            description=f"Trouvez les meilleurs prestataires : {name.lower()}.")
            db.session.add(root)
            db.session.flush()
            for j, s in enumerate(subs):
                db.session.add(Category(name=s, slug=slugify(f"{name}-{s}", 120), icon=icon, position=j, parent_id=root.id))
    if CommissionRule.query.filter_by(scope="global").first() is None:
        db.session.add(CommissionRule(scope="global", rate_bp=1000, note="Taux par défaut — à valider (cahier §24)"))
    for slug, (title, body) in PAGES.items():
        if Page.query.filter_by(slug=slug).first() is None:
            db.session.add(Page(slug=slug, title=title, body=body))
    if HelpArticle.query.count() == 0:
        for i, (aud, sec, title, body) in enumerate(HELP):
            db.session.add(HelpArticle(audience=aud, section=sec, title=title, slug=slugify(title, 100), body=body, position=i))
    db.session.commit()


def create_staff(email, password, first="Admin", last="EventyStore", roles=("super_admin",)):
    u = User(role="staff", first_name=first, last_name=last, email=email.lower(), staff_roles=list(roles),
             email_verified_at=utcnow(), terms_accepted_at=utcnow(), notif_prefs={"email": True})
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return u


# ------------------------------------------------------------------ DÉMO
DEMO_VENDORS = [
    ("Teranga Délices", "Traiteur complet", "Traiteur & restauration", "Dakar", "Dakar", "person", 6500, "service", 100,
     "Traiteur sénégalais et international pour mariages, baptêmes et séminaires. Buffets, service serveurs inclus."),
    ("Sunu Sono", "Sonorisation & DJ", "Son, lumière & audiovisuel", "Dakar", "Dakar", "event", 150000, "service", None,
     "Sonorisation complète, régie et DJ pour vos soirées et cérémonies."),
    ("Chaises & Tentes Kaolack", "Location mobilier et tentes", "Mobilier & vaisselle", "Kaolack", "Kaolack", "unit", 300, "rental", None,
     "Location de chaises, tables et bâches avec livraison et installation."),
    ("Studio Lumière", "Photographe événementiel", "Photo & vidéo", "Thiès", "Dakar", "event", 120000, "service", None,
     "Reportage photo et vidéo de vos événements, livraison d'un album numérique sous 10 jours."),
    ("Salle Baobab", "Salle de réception", "Lieux & réception", "Saly", "Thiès", "event", 450000, "service", 300,
     "Salle climatisée de 300 places avec jardin, parking et cuisine équipée."),
]


def seed_demo():
    """Jeu de données FICTIF, balisé is_demo. Les visuels sont des illustrations de substitution, pas de vraies photos."""
    seed_core()
    if User.query.filter_by(is_demo=True).first():
        return 0
    n = 0
    admin = User(role="staff", first_name="Démo", last_name="Admin", email="admin@demo.eventystore.sn", staff_roles=["super_admin"],
                 email_verified_at=utcnow(), terms_accepted_at=utcnow(), is_demo=True, notif_prefs={"email": True})
    admin.set_password(DEMO_PASSWORD)
    buyer = User(role="buyer", first_name="Awa", last_name="Diop", email="acheteur@demo.eventystore.sn", phone="+221770000001",
                 city="Dakar", email_verified_at=utcnow(), terms_accepted_at=utcnow(), is_demo=True, notif_prefs={"email": True})
    buyer.set_password(DEMO_PASSWORD)
    db.session.add_all([admin, buyer])
    db.session.flush()
    offers = []
    for i, (biz, act, catname, city, region, unit, price, otype, cap, desc) in enumerate(DEMO_VENDORS):
        u = User(role="vendor", first_name="Resp", last_name=biz.split()[0], email=f"prestataire{i + 1}@demo.eventystore.sn",
                 phone=f"+22177000010{i}", city=city, email_verified_at=utcnow(), terms_accepted_at=utcnow(), is_demo=True,
                 notif_prefs={"email": True})
        u.set_password(DEMO_PASSWORD)
        db.session.add(u)
        db.session.flush()
        root = Category.query.filter_by(name=catname, parent_id=None).first()
        v = VendorProfile(user_id=u.id, business_name=biz, slug=slugify(biz), tagline=act, description=desc + " (profil de démonstration)",
                          legal_status="entreprise", activity_type=act, city=city, region=region, served_areas=[region, "Dakar"],
                          category_ids=[root.id], contact_phone=u.phone, contact_email=u.email, verification_status="validated",
                          verified_badge=True, verified_at=utcnow(), years_experience=5 + i, is_demo=True)
        db.session.add(v)
        db.session.flush()
        sub = Category.query.filter_by(parent_id=root.id).order_by(Category.position).first()
        o = Offer(vendor_id=v.id, category_id=(sub or root).id, title=f"{act} — {biz}", slug=slugify(f"{act}-{biz}", 70),
                  description=desc + " Offre de démonstration : toutes les données sont fictives.", offer_type=otype,
                  price_model="fixed", price_unit=unit, price_xof=price, capacity=cap, stock_qty=500 if otype == "rental" else None,
                  city=city, region=region, service_areas=[region, "Dakar"], deposit_percent=30, cancellation_policy="moderate",
                  conditions="Acompte de 30 % à la réservation, solde 3 jours avant l'événement.", inclusions="Détails à préciser avec le prestataire.",
                  status="active", published_at=utcnow(), featured=i < 3, delivery_fee_xof=15000 if otype == "rental" else 0,
                  min_notice_days=2, is_demo=True)
        if otype == "service" and unit == "person":
            o.min_quantity = 30
            o.tiers = [{"min_qty": 100, "unit_price": price - 500}]
        db.session.add(o)
        db.session.flush()
        o.options.append(OfferOption(name="Service serveurs" if i == 0 else "Option supplémentaire", price_xof=25000, price_type="flat"))
        refresh_search_fields(o)
        offers.append(o)
        n += 3
    # une offre sur devis
    v0 = VendorProfile.query.filter_by(is_demo=True).first()
    q = Offer(vendor_id=v0.id, category_id=Category.query.filter_by(name="Décoration & scénographie").first().id, title="Décoration de mariage sur mesure",
              slug="decoration-mariage-sur-mesure", description="Décoration complète de salle selon vos thèmes et couleurs. Sur devis personnalisé (démo).",
              offer_type="service", price_model="quote", city="Dakar", region="Dakar", service_areas=["Dakar"],
              conditions="Devis gratuit, acompte à la commande.", status="active", published_at=utcnow(), is_demo=True)
    db.session.add(q)
    db.session.flush()
    refresh_search_fields(q)
    ev = Event(buyer_id=buyer.id, title="Mariage de Awa (démo)", event_type="wedding", start_date=date.today() + timedelta(days=60),
               end_date=date.today() + timedelta(days=60), city="Dakar", region="Dakar", guests=150, budget_xof=3000000, is_demo=True)
    db.session.add(ev)
    Setting.query.filter_by(key="demo_mode").delete()
    db.session.add(Setting(key="demo_mode", value="1"))
    db.session.commit()
    return n + 3


def purge_demo():
    """Supprime toutes les données balisées `is_demo` et celles qui en dépendent."""
    from .models import (AuditLog, CartItem, Conversation, Favorite, LedgerEntry, Message, Notification, OrderItem, Payment,
                         Payout, Quote, QuoteLine, Refund, Ticket, AvailabilityBlock, EmailOutbox)
    count = 0
    users = [u.id for u in User.query.filter_by(is_demo=True)]
    vendors = [v.id for v in VendorProfile.query.filter_by(is_demo=True)]
    orders = [o.id for o in Order.query.filter(db.or_(Order.buyer_id.in_(users or [0]), Order.vendor_id.in_(vendors or [0])))]
    reqs = [r.id for r in QuoteRequest.query.filter(db.or_(QuoteRequest.buyer_id.in_(users or [0]), QuoteRequest.vendor_id.in_(vendors or [0])))]
    offers = [o.id for o in Offer.query.filter(Offer.vendor_id.in_(vendors or [0]))]
    def d(model, *crit):
        nonlocal count
        count += model.query.filter(*crit).delete(synchronize_session=False)
    qids = [q.id for q in Quote.query.filter(Quote.request_id.in_(reqs or [0]))]
    d(QuoteLine, QuoteLine.quote_id.in_(qids or [0]))
    d(Message, Message.conversation_id.in_([c.id for c in Conversation.query.filter(Conversation.buyer_id.in_(users or [0]))] or [0]))
    d(Conversation, Conversation.buyer_id.in_(users or [0]))
    d(Review, Review.order_id.in_(orders or [0]))
    d(Refund, Refund.order_id.in_(orders or [0]))
    d(LedgerEntry, LedgerEntry.vendor_id.in_(vendors or [0]))
    d(Payment, Payment.order_id.in_(orders or [0]))
    d(OrderItem, OrderItem.order_id.in_(orders or [0]))
    d(Ticket, Ticket.author_id.in_(users or [0]))
    d(Order, Order.id.in_(orders or [0]))
    d(Quote, Quote.id.in_(qids or [0]))
    d(QuoteRequest, QuoteRequest.id.in_(reqs or [0]))
    d(CartItem, CartItem.user_id.in_(users or [0]))
    d(Favorite, Favorite.user_id.in_(users or [0]))
    d(Payout, Payout.vendor_id.in_(vendors or [0]))
    d(AvailabilityBlock, AvailabilityBlock.vendor_id.in_(vendors or [0]))
    d(Promo, Promo.vendor_id.in_(vendors or [0]))
    d(Media, Media.offer_id.in_(offers or [0]))
    d(OfferOption, OfferOption.offer_id.in_(offers or [0]))
    d(Event, Event.buyer_id.in_(users or [0]))
    d(Offer, Offer.id.in_(offers or [0]))
    d(VendorProfile, VendorProfile.id.in_(vendors or [0]))
    d(Notification, Notification.user_id.in_(users or [0]))
    d(AuditLog, AuditLog.actor_id.in_(users or [0]))
    d(EmailOutbox, EmailOutbox.user_id.in_(users or [0]))
    d(User, User.id.in_(users or [0]))
    Setting.query.filter_by(key="demo_mode").delete()
    db.session.commit()
    return count
