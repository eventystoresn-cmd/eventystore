"""Modèle de données relationnel EventyStore.

Conventions :
- Montants en FCFA (XOF) stockés en entiers (pas de décimales) ;
- Dates/heures techniques en UTC (naïf) ; les dates d'événement/réservation sont des `date` calendaires
  (fuseau Africa/Dakar = UTC+0, sans changement d'heure) ;
- Les champs semi-structurés (options, instantanés, attributs) utilisent JSON.
"""
from datetime import date, datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow():
    return datetime.utcnow()


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- Utilisateurs
STAFF_ROLES = {
    "super_admin": "Super-administrateur",
    "catalog": "Gestion catalogue",
    "vendor_validation": "Validation prestataires",
    "support": "Support",
    "finance": "Finance",
    "moderation": "Modération",
}

# Permissions accordées à chaque rôle d'équipe (le super_admin a tout).
ROLE_PERMISSIONS = {
    "catalog": {"catalog", "content", "offers"},
    "vendor_validation": {"vendors", "vendor_docs"},
    "support": {"support", "orders_view", "conversations"},
    "finance": {"finance", "orders_view", "payouts", "commissions", "refunds", "reports"},
    "moderation": {"moderation", "offers", "reviews"},
}


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(16), nullable=False, default="buyer", index=True)  # buyer | vendor | staff
    staff_roles = db.Column(db.JSON, default=list)
    account_type = db.Column(db.String(16), default="individual")  # individual | company
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(190), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(30), unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    email_verified_at = db.Column(db.DateTime)
    phone_verified_at = db.Column(db.DateTime)
    status = db.Column(db.String(16), default="active", nullable=False)  # active | suspended | closed
    city = db.Column(db.String(80))
    neighborhood = db.Column(db.String(80))
    company_name = db.Column(db.String(160))
    ninea = db.Column(db.String(40))
    billing_address = db.Column(db.String(255))
    marketing_consent = db.Column(db.Boolean, default=False)
    terms_accepted_at = db.Column(db.DateTime)
    notif_prefs = db.Column(db.JSON, default=dict)  # {"email": True, "sms": False}
    totp_secret = db.Column(db.String(64))
    totp_enabled = db.Column(db.Boolean, default=False)
    last_login_at = db.Column(db.DateTime)
    is_demo = db.Column(db.Boolean, default=False, index=True)

    vendor = db.relationship("VendorProfile", back_populates="user", uselist=False)

    # -- mot de passe
    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw, method="pbkdf2:sha256:600000")

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    # -- états
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name(self):
        if self.role == "vendor" and self.vendor:
            return self.vendor.business_name
        if self.account_type == "company" and self.company_name:
            return self.company_name
        return self.full_name

    @property
    def is_active(self):  # Flask-Login : un compte suspendu/clos ne peut plus se connecter
        return self.status == "active"

    @property
    def email_verified(self):
        return self.email_verified_at is not None

    @property
    def is_staff(self):
        return self.role == "staff"

    @property
    def is_buyer(self):
        return self.role == "buyer"

    @property
    def is_vendor(self):
        return self.role == "vendor"

    def has_perm(self, perm):
        if not self.is_staff:
            return False
        roles = self.staff_roles or []
        if "super_admin" in roles:
            return True
        return any(perm in ROLE_PERMISSIONS.get(r, ()) for r in roles)

    def wants(self, channel):
        prefs = self.notif_prefs or {}
        return prefs.get(channel, channel == "email")

    def __repr__(self):
        return f"<User {self.id} {self.email} {self.role}>"


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    identifier = db.Column(db.String(190), index=True)
    ip = db.Column(db.String(64), index=True)
    success = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


VERIFICATION_STATUSES = {
    "draft": ("Brouillon", "neutral"),
    "submitted": ("Soumis", "info"),
    "in_review": ("En cours de vérification", "info"),
    "complement_requested": ("Complément demandé", "warning"),
    "validated": ("Validé", "success"),
    "refused": ("Refusé", "danger"),
    "suspended": ("Suspendu", "danger"),
}


class VendorProfile(TimestampMixin, db.Model):
    __tablename__ = "vendor_profiles"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    business_name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(180), unique=True, index=True)
    tagline = db.Column(db.String(200))
    description = db.Column(db.Text)
    legal_status = db.Column(db.String(40))  # independant | entreprise | association
    activity_type = db.Column(db.String(160))
    specialties = db.Column(db.String(300))
    years_experience = db.Column(db.Integer)
    contact_phone = db.Column(db.String(30))
    whatsapp = db.Column(db.String(30))
    contact_email = db.Column(db.String(190))
    address = db.Column(db.String(255))
    city = db.Column(db.String(80), index=True)
    region = db.Column(db.String(80), index=True)
    served_areas = db.Column(db.JSON, default=list)  # liste de régions/villes desservies
    category_ids = db.Column(db.JSON, default=list)  # catégories déclarées à l'inscription
    travel_fee_xof = db.Column(db.Integer, default=0)
    opening_hours = db.Column(db.JSON, default=dict)  # {"mon": "08:00-18:00", ...}
    logo = db.Column(db.String(255))
    cover = db.Column(db.String(255))
    verification_status = db.Column(db.String(24), default="draft", index=True)
    verification_note = db.Column(db.Text)  # motif visible par le prestataire
    verified_badge = db.Column(db.Boolean, default=False)
    submitted_at = db.Column(db.DateTime)
    verified_at = db.Column(db.DateTime)
    commission_override_bp = db.Column(db.Integer)  # points de base (100 = 1 %)
    payout_method = db.Column(db.String(24))  # wave | orange_money | bank
    payout_account_enc = db.Column(db.Text)  # chiffré
    payout_account_mask = db.Column(db.String(40))
    payout_holder = db.Column(db.String(120))
    payout_updated_at = db.Column(db.DateTime)
    response_hours_avg = db.Column(db.Float)
    is_demo = db.Column(db.Boolean, default=False, index=True)

    user = db.relationship("User", back_populates="vendor")
    offers = db.relationship("Offer", back_populates="vendor", lazy="dynamic")
    documents = db.relationship("VendorDocument", back_populates="vendor", cascade="all, delete-orphan")

    @property
    def is_validated(self):
        return self.verification_status == "validated"

    @property
    def status_label(self):
        return VERIFICATION_STATUSES.get(self.verification_status, (self.verification_status, "neutral"))[0]

    @property
    def rating(self):
        rows = db.session.query(db.func.avg(Review.rating), db.func.count(Review.id)).filter(
            Review.vendor_id == self.id, Review.status == "published").one()
        return (round(float(rows[0]), 1) if rows[0] else None, rows[1])

    @property
    def public_url_slug(self):
        return self.slug


class VendorDocument(db.Model):
    __tablename__ = "vendor_documents"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    doc_type = db.Column(db.String(40), nullable=False)  # identity | registry | ninea | other
    original_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255), nullable=False)  # fichier chiffré dans PRIVATE_DIR
    mime = db.Column(db.String(80))
    size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=utcnow)
    vendor = db.relationship("VendorProfile", back_populates="documents")


class VerificationEvent(db.Model):
    """Historique de vérification d'un prestataire."""
    __tablename__ = "verification_events"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    from_status = db.Column(db.String(24))
    to_status = db.Column(db.String(24))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    actor = db.relationship("User")


# --------------------------------------------------------------------------- Catalogue
class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("categories.id"), index=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(140), unique=True, nullable=False, index=True)
    icon = db.Column(db.String(40), default="sparkles")
    description = db.Column(db.Text)
    position = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)
    # Champs spécifiques à la catégorie : [{"key":"capacity","label":"Capacité","type":"number"}, ...]
    attributes = db.Column(db.JSON, default=list)

    parent = db.relationship("Category", remote_side=[id], backref=db.backref("children", order_by="Category.position"))

    @property
    def is_root(self):
        return self.parent_id is None

    def descendant_ids(self):
        ids = [self.id]
        for c in self.children:
            ids.extend(c.descendant_ids())
        return ids

    @property
    def root(self):
        return self.parent.root if self.parent else self


OFFER_TYPES = {
    "service": "Prestation de service",
    "rental": "Location d'équipement",
    "sale": "Vente de produit",
    "package": "Forfait / pack",
}
PRICE_MODELS = {"fixed": "Prix fixe", "from": "À partir de", "range": "Fourchette indicative", "quote": "Sur devis"}
PRICE_UNITS = {
    "person": "par personne", "hour": "par heure", "day": "par jour", "night": "par nuit",
    "unit": "par unité", "lot": "par lot", "event": "par événement", "package": "forfait",
}
CANCELLATION_POLICIES = {
    "flexible": ("Flexible", "Remboursement intégral jusqu'à 7 jours avant l'événement ; 50 % entre 7 et 3 jours ; aucun remboursement ensuite."),
    "moderate": ("Modérée", "Remboursement intégral jusqu'à 14 jours avant ; 50 % entre 14 et 7 jours ; aucun remboursement ensuite."),
    "strict": ("Stricte", "Remboursement de 50 % jusqu'à 30 jours avant l'événement ; aucun remboursement ensuite."),
}
OFFER_STATUSES = {
    "draft": ("Brouillon", "neutral"), "submitted": ("En modération", "info"), "active": ("Active", "success"),
    "refused": ("Refusée", "danger"), "suspended": ("Suspendue", "danger"), "archived": ("Archivée", "neutral"),
}


class Offer(TimestampMixin, db.Model):
    __tablename__ = "offers"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    slug = db.Column(db.String(200), index=True)
    description = db.Column(db.Text)
    offer_type = db.Column(db.String(16), default="service", nullable=False)
    price_model = db.Column(db.String(12), default="fixed", nullable=False)
    price_unit = db.Column(db.String(12), default="event")
    price_xof = db.Column(db.Integer)  # prix (ou minimum)
    price_max_xof = db.Column(db.Integer)  # pour fourchette
    min_quantity = db.Column(db.Integer, default=1)
    capacity = db.Column(db.Integer)  # convives max / capacité d'une salle
    stock_qty = db.Column(db.Integer)  # quantité totale (location / vente)
    concurrent_capacity = db.Column(db.Integer, default=1)  # nb de prestations simultanées possibles
    min_notice_days = db.Column(db.Integer, default=2)
    buffer_before_days = db.Column(db.Integer, default=0)
    buffer_after_days = db.Column(db.Integer, default=0)
    region = db.Column(db.String(80), index=True)
    city = db.Column(db.String(80), index=True)
    service_areas = db.Column(db.JSON, default=list)
    travel_fee_xof = db.Column(db.Integer, default=0)
    delivery_fee_xof = db.Column(db.Integer, default=0)
    install_fee_xof = db.Column(db.Integer, default=0)
    deposit_percent = db.Column(db.Integer, default=30)
    caution_xof = db.Column(db.Integer, default=0)
    cancellation_policy = db.Column(db.String(12), default="moderate")
    conditions = db.Column(db.Text)
    inclusions = db.Column(db.Text)
    attrs = db.Column(db.JSON, default=dict)
    tiers = db.Column(db.JSON, default=list)  # tarifs dégressifs [{"min_qty":50,"unit_price":900}]
    online_payment = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(12), default="draft", nullable=False, index=True)
    refusal_reason = db.Column(db.Text)
    featured = db.Column(db.Boolean, default=False, index=True)
    sponsored = db.Column(db.Boolean, default=False)
    views = db.Column(db.Integer, default=0)
    clicks = db.Column(db.Integer, default=0)
    published_at = db.Column(db.DateTime)
    search_text = db.Column(db.Text)  # texte normalisé (sans accents) pour la recherche
    areas_text = db.Column(db.Text)  # "|dakar|pikine|thies|" zones desservies normalisées
    is_demo = db.Column(db.Boolean, default=False, index=True)

    vendor = db.relationship("VendorProfile", back_populates="offers")
    category = db.relationship("Category")
    media = db.relationship("Media", back_populates="offer", cascade="all, delete-orphan",
                            order_by="Media.position", foreign_keys="Media.offer_id")
    options = db.relationship("OfferOption", back_populates="offer", cascade="all, delete-orphan",
                              order_by="OfferOption.id")

    @property
    def is_quote_only(self):
        return self.price_model == "quote" or not self.price_xof

    @property
    def is_bookable_direct(self):
        """Une offre à prix fixe/« à partir de » avec paiement en ligne peut aller au panier."""
        return (self.price_model in ("fixed", "from") and bool(self.price_xof)
                and self.online_payment and self.status == "active")

    @property
    def uses_stock(self):
        return self.offer_type in ("rental", "sale") and bool(self.stock_qty)

    @property
    def cover(self):
        for m in self.media:
            if m.is_cover:
                return m
        return self.media[0] if self.media else None

    @property
    def status_label(self):
        return OFFER_STATUSES.get(self.status, (self.status, "neutral"))[0]

    @property
    def rating(self):
        r = db.session.query(db.func.avg(Review.rating), db.func.count(Review.id)).filter(
            Review.offer_id == self.id, Review.status == "published").one()
        return (round(float(r[0]), 1) if r[0] else None, r[1])

    def missing_for_publication(self):
        """AC-14 : une offre active comporte titre, catégorie, description, zone, modèle de prix, conditions."""
        miss = []
        if not self.title:
            miss.append("titre")
        if not self.category_id:
            miss.append("catégorie")
        if not (self.description and len(self.description.strip()) >= 30):
            miss.append("description (30 caractères minimum)")
        if not (self.city or self.region or self.service_areas):
            miss.append("zone de service")
        if self.price_model != "quote" and not self.price_xof:
            miss.append("prix")
        if not self.price_model:
            miss.append("modèle de prix")
        if not (self.conditions and self.conditions.strip()):
            miss.append("conditions")
        return miss


class OfferOption(db.Model):
    __tablename__ = "offer_options"
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    price_xof = db.Column(db.Integer, default=0)
    price_type = db.Column(db.String(12), default="flat")  # flat | per_unit | per_person
    required = db.Column(db.Boolean, default=False)
    offer = db.relationship("Offer", back_populates="options")


class Media(db.Model):
    __tablename__ = "media"
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), index=True)  # portfolio
    path = db.Column(db.String(255), nullable=False)  # relatif à UPLOAD_DIR
    thumb = db.Column(db.String(255))
    caption = db.Column(db.String(200))
    alt = db.Column(db.String(200))
    position = db.Column(db.Integer, default=0)
    is_cover = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    offer = db.relationship("Offer", back_populates="media", foreign_keys=[offer_id])


# --------------------------------------------------------------------------- Disponibilités
class AvailabilityBlock(db.Model):
    """Indisponibilité saisie par le prestataire (jours entiers, bornes incluses)."""
    __tablename__ = "availability_blocks"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)  # null = toutes les offres
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(200))  # motif interne
    weekly = db.Column(db.Boolean, default=False)  # blocage récurrent chaque semaine (jour de start_date)
    created_at = db.Column(db.DateTime, default=utcnow)


# --------------------------------------------------------------------------- Événements acheteur
EVENT_TYPES = {
    "wedding": "Mariage", "birthday": "Anniversaire", "baptism": "Baptême", "conference": "Conférence",
    "seminar": "Séminaire", "party": "Soirée", "cultural": "Événement culturel", "other": "Autre",
}


class Event(TimestampMixin, db.Model):
    __tablename__ = "events"
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    event_type = db.Column(db.String(16), default="other")
    description = db.Column(db.Text)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(5))
    end_time = db.Column(db.String(5))
    venue = db.Column(db.String(160))
    address = db.Column(db.String(255))
    city = db.Column(db.String(80))
    region = db.Column(db.String(80))
    indoor = db.Column(db.String(10))  # indoor | outdoor | both
    guests = db.Column(db.Integer)
    budget_xof = db.Column(db.Integer)
    budget_alert_pct = db.Column(db.Integer, default=90)
    category_budgets = db.Column(db.JSON, default=dict)  # {category_id: montant}
    status = db.Column(db.String(12), default="planned")  # planned | archived
    is_demo = db.Column(db.Boolean, default=False)

    buyer = db.relationship("User")
    documents = db.relationship("EventDocument", cascade="all, delete-orphan", backref="event")


class EventDocument(db.Model):
    __tablename__ = "event_documents"
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False, index=True)
    label = db.Column(db.String(160))
    original_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(80))
    size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)


class Favorite(db.Model):
    __tablename__ = "favorites"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    offer = db.relationship("Offer")
    vendor = db.relationship("VendorProfile")
    __table_args__ = (db.UniqueConstraint("user_id", "offer_id", name="uq_fav_offer"),
                      db.UniqueConstraint("user_id", "vendor_id", name="uq_fav_vendor"))


# --------------------------------------------------------------------------- Devis & négociation
QUOTE_REQUEST_STATUSES = {
    "open": ("Demande envoyée", "info"), "quoted": ("Devis reçu", "warning"),
    "negotiating": ("En négociation", "warning"), "accepted": ("Devis accepté", "success"),
    "declined": ("Refusée", "danger"), "expired": ("Expirée", "neutral"), "cancelled": ("Annulée", "neutral"),
}


class QuoteRequest(TimestampMixin, db.Model):
    __tablename__ = "quote_requests"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), index=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(5))
    end_time = db.Column(db.String(5))
    location = db.Column(db.String(255))
    indoor = db.Column(db.String(10))
    quantity = db.Column(db.Integer, default=1)
    guests = db.Column(db.Integer)
    need = db.Column(db.Text, nullable=False)
    budget_xof = db.Column(db.Integer)
    respond_by = db.Column(db.Date)
    contact_consent = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(12), default="open", nullable=False, index=True)
    counter_count = db.Column(db.Integer, default=0)
    first_response_at = db.Column(db.DateTime)
    is_demo = db.Column(db.Boolean, default=False)

    buyer = db.relationship("User")
    vendor = db.relationship("VendorProfile")
    offer = db.relationship("Offer")
    event = db.relationship("Event")
    quotes = db.relationship("Quote", back_populates="request", order_by="Quote.version", cascade="all, delete-orphan")
    attachments = db.relationship("QuoteAttachment", cascade="all, delete-orphan")

    @property
    def latest_quote(self):
        sent = [q for q in self.quotes if q.status != "draft"]
        return sent[-1] if sent else None

    @property
    def accepted_quote(self):
        for q in self.quotes:
            if q.status == "accepted":
                return q
        return None

    @property
    def status_label(self):
        return QUOTE_REQUEST_STATUSES.get(self.status, (self.status, "neutral"))[0]


class QuoteAttachment(db.Model):
    __tablename__ = "quote_attachments"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("quote_requests.id"), nullable=False, index=True)
    original_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(80))
    size = db.Column(db.Integer)


QUOTE_STATUSES = {
    "draft": ("Brouillon", "neutral"), "sent": ("Envoyé", "info"), "superseded": ("Remplacé", "neutral"),
    "accepted": ("Accepté", "success"), "declined": ("Refusé", "danger"), "expired": ("Expiré", "neutral"),
    "withdrawn": ("Retiré", "neutral"),
}


class Quote(db.Model):
    """Une version de devis. Aucune modification silencieuse : toute modification crée une version."""
    __tablename__ = "quotes"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("quote_requests.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    reference = db.Column(db.String(24), index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    author_role = db.Column(db.String(8), nullable=False)  # vendor | buyer
    status = db.Column(db.String(12), default="draft", nullable=False, index=True)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    quantity = db.Column(db.Integer, default=1)
    subtotal_xof = db.Column(db.Integer, default=0)
    fees_xof = db.Column(db.Integer, default=0)
    total_xof = db.Column(db.Integer, default=0)
    deposit_percent = db.Column(db.Integer, default=30)
    deposit_xof = db.Column(db.Integer, default=0)
    balance_due_days_before = db.Column(db.Integer, default=3)
    valid_until = db.Column(db.Date)
    description = db.Column(db.Text)
    exclusions = db.Column(db.Text)
    conditions = db.Column(db.Text)
    cancellation_policy = db.Column(db.String(12), default="moderate")
    note = db.Column(db.Text)  # message accompagnant la version
    issued_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)

    request = db.relationship("QuoteRequest", back_populates="quotes")
    author = db.relationship("User")
    lines = db.relationship("QuoteLine", back_populates="quote", cascade="all, delete-orphan", order_by="QuoteLine.id")

    @property
    def balance_xof(self):
        return max(0, (self.total_xof or 0) - (self.deposit_xof or 0))

    @property
    def is_expired(self):
        return bool(self.valid_until and self.valid_until < date.today())

    @property
    def status_label(self):
        return QUOTE_STATUSES.get(self.status, (self.status, "neutral"))[0]

    @property
    def kind_label(self):
        return "Contre-proposition" if self.author_role == "buyer" else (
            "Devis" if self.version == 1 or not any(q.author_role == "vendor" for q in self.request.quotes if q.version < self.version) else "Devis révisé")


class QuoteLine(db.Model):
    __tablename__ = "quote_lines"
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False, index=True)
    kind = db.Column(db.String(16), default="item")  # item | travel | delivery | install | staff | caution | other
    label = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Float, default=1)
    unit = db.Column(db.String(30))
    unit_price_xof = db.Column(db.Integer, default=0)
    discount_xof = db.Column(db.Integer, default=0)
    total_xof = db.Column(db.Integer, default=0)
    quote = db.relationship("Quote", back_populates="lines")


# --------------------------------------------------------------------------- Panier, commandes, paiements
class CartItem(TimestampMixin, db.Model):
    __tablename__ = "cart_items"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"))
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    guests = db.Column(db.Integer)
    option_ids = db.Column(db.JSON, default=list)
    offer = db.relationship("Offer")
    event = db.relationship("Event")


ORDER_STATUSES = {
    "pending_payment": ("En attente de paiement", "warning"),
    "pending_confirmation": ("En attente de confirmation", "info"),
    "confirmed": ("Confirmée", "success"),
    "in_progress": ("En cours", "info"),
    "completed": ("Terminée", "success"),
    "cancelled": ("Annulée", "danger"),
    "dispute": ("Litige", "danger"),
}
ACTIVE_ORDER_STATUSES = ("pending_confirmation", "confirmed", "in_progress", "dispute")


class Order(TimestampMixin, db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), index=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"))
    source = db.Column(db.String(8), default="cart")  # cart | quote
    status = db.Column(db.String(24), default="pending_payment", nullable=False, index=True)
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False)
    subtotal_xof = db.Column(db.Integer, default=0)
    fees_xof = db.Column(db.Integer, default=0)
    discount_xof = db.Column(db.Integer, default=0)
    total_xof = db.Column(db.Integer, default=0)
    deposit_xof = db.Column(db.Integer, default=0)
    caution_xof = db.Column(db.Integer, default=0)
    paid_xof = db.Column(db.Integer, default=0)
    refunded_xof = db.Column(db.Integer, default=0)
    balance_due_date = db.Column(db.Date)
    promo_code = db.Column(db.String(30))
    commission_bp = db.Column(db.Integer, default=0)  # instantané du taux appliqué
    commission_xof = db.Column(db.Integer, default=0)
    policy = db.Column(db.String(12), default="moderate")
    conditions_snapshot = db.Column(db.Text)
    snapshot = db.Column(db.JSON, default=dict)  # instantané : prix, options, conditions au moment de l'acceptation
    confirm_rule = db.Column(db.String(16), default="vendor_and_deposit")  # vendor | deposit | vendor_and_deposit
    vendor_confirmed_at = db.Column(db.DateTime)
    hold_expires_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancelled_by = db.Column(db.String(10))  # buyer | vendor | admin | system
    cancel_reason = db.Column(db.Text)
    reminder_sent = db.Column(db.Boolean, default=False)
    review_requested = db.Column(db.Boolean, default=False)
    funds_released = db.Column(db.Boolean, default=False)
    is_demo = db.Column(db.Boolean, default=False)

    buyer = db.relationship("User")
    vendor = db.relationship("VendorProfile")
    event = db.relationship("Event", backref="orders")
    quote = db.relationship("Quote")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="order", order_by="Payment.id")
    review = db.relationship("Review", back_populates="order", uselist=False)

    @property
    def status_label(self):
        return ORDER_STATUSES.get(self.status, (self.status, "neutral"))[0]

    @property
    def remaining_xof(self):
        return max(0, (self.total_xof or 0) - (self.paid_xof or 0) + (self.refunded_xof or 0))

    @property
    def fully_paid(self):
        return self.total_xof > 0 and self.remaining_xof == 0

    @property
    def net_vendor_xof(self):
        return max(0, (self.total_xof or 0) - (self.commission_xof or 0))


class OrderItem(db.Model):
    __tablename__ = "order_items"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)
    title = db.Column(db.String(200), nullable=False)  # instantané
    kind = db.Column(db.String(16), default="item")
    quantity = db.Column(db.Float, default=1)
    unit = db.Column(db.String(30))
    unit_price_xof = db.Column(db.Integer, default=0)
    total_xof = db.Column(db.Integer, default=0)
    options = db.Column(db.JSON, default=list)
    order = db.relationship("Order", back_populates="items")
    offer = db.relationship("Offer")


PAYMENT_STATUSES = {
    "pending": ("En attente", "warning"), "succeeded": ("Réussi", "success"), "failed": ("Échoué", "danger"),
    "refunded": ("Remboursé", "neutral"), "partially_refunded": ("Partiellement remboursé", "neutral"),
    "disputed": ("Contesté", "danger"),
}
PAYMENT_METHODS = {
    "wave": "Wave", "orange_money": "Orange Money", "card": "Carte bancaire", "bank_transfer": "Virement bancaire",
    "sandbox": "Test (sandbox)", "offline": "Hors plateforme",
}


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), default="full")  # deposit | balance | full
    amount_xof = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), default="XOF")
    method = db.Column(db.String(16), default="sandbox")
    provider = db.Column(db.String(16), default="sandbox")
    external_ref = db.Column(db.String(80), unique=True, index=True)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    provider_fee_xof = db.Column(db.Integer, default=0)
    commission_xof = db.Column(db.Integer, default=0)
    net_vendor_xof = db.Column(db.Integer, default=0)
    refunded_xof = db.Column(db.Integer, default=0)
    raw = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=utcnow)
    paid_at = db.Column(db.DateTime)
    order = db.relationship("Order", back_populates="payments")

    @property
    def status_label(self):
        return PAYMENT_STATUSES.get(self.status, (self.status, "neutral"))[0]


class WebhookEvent(db.Model):
    """Journal des webhooks reçus (idempotence + rapprochement)."""
    __tablename__ = "webhook_events"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(16))
    event_id = db.Column(db.String(80), unique=True)
    payload = db.Column(db.JSON)
    signature_ok = db.Column(db.Boolean, default=False)
    processed = db.Column(db.Boolean, default=False)
    error = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)


class Refund(db.Model):
    __tablename__ = "refunds"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"))
    amount_xof = db.Column(db.Integer, nullable=False)
    retained_xof = db.Column(db.Integer, default=0)
    reason = db.Column(db.Text)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    days_before_event = db.Column(db.Integer)
    status = db.Column(db.String(12), default="processed")
    created_at = db.Column(db.DateTime, default=utcnow)
    order = db.relationship("Order", backref="refunds")
    actor = db.relationship("User")


class LedgerEntry(db.Model):
    """Livre des soldes prestataire : crédit net à la réussite d'un paiement, « disponible » après clôture."""
    __tablename__ = "ledger_entries"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"))
    payout_id = db.Column(db.Integer, db.ForeignKey("payouts.id"))
    kind = db.Column(db.String(16), nullable=False)  # credit | refund_debit | payout_debit | adjustment
    amount_xof = db.Column(db.Integer, nullable=False)  # signé
    gross_xof = db.Column(db.Integer, default=0)
    commission_xof = db.Column(db.Integer, default=0)
    available = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)


PAYOUT_STATUSES = {
    "requested": ("Demandé", "info"), "in_review": ("En vérification", "info"), "approved": ("Approuvé", "success"),
    "transferring": ("En cours de transfert", "warning"), "paid": ("Payé", "success"), "rejected": ("Rejeté", "danger"),
}


class Payout(db.Model):
    __tablename__ = "payouts"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    amount_xof = db.Column(db.Integer, nullable=False)
    fee_xof = db.Column(db.Integer, default=0)
    method = db.Column(db.String(24))
    destination_mask = db.Column(db.String(40))
    status = db.Column(db.String(14), default="requested", nullable=False, index=True)
    external_ref = db.Column(db.String(80))
    note = db.Column(db.Text)
    requested_at = db.Column(db.DateTime, default=utcnow)
    processed_at = db.Column(db.DateTime)
    processed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    vendor = db.relationship("VendorProfile")

    @property
    def status_label(self):
        return PAYOUT_STATUSES.get(self.status, (self.status, "neutral"))[0]


class CommissionRule(db.Model):
    __tablename__ = "commission_rules"
    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(10), nullable=False)  # global | category | vendor | offer_type
    ref = db.Column(db.String(40))  # id de catégorie / de prestataire / type d'offre
    rate_bp = db.Column(db.Integer, nullable=False)  # 1000 = 10 %
    active = db.Column(db.Boolean, default=True)
    valid_from = db.Column(db.Date)
    valid_to = db.Column(db.Date)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)


class Promo(db.Model):
    __tablename__ = "promos"
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    code = db.Column(db.String(30), nullable=False, index=True)
    percent = db.Column(db.Integer)
    amount_xof = db.Column(db.Integer)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"))
    valid_from = db.Column(db.Date)
    valid_to = db.Column(db.Date)
    max_uses = db.Column(db.Integer)
    used = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)
    __table_args__ = (db.UniqueConstraint("vendor_id", "code", name="uq_promo_vendor_code"),)


# --------------------------------------------------------------------------- Messagerie, notifications
class Conversation(db.Model):
    """Toute conversation est rattachée à un contexte (demande de devis ou commande)."""
    __tablename__ = "conversations"
    id = db.Column(db.Integer, primary_key=True)
    context_type = db.Column(db.String(12), nullable=False)  # quote | order | offer
    context_id = db.Column(db.Integer, nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    vendor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subject = db.Column(db.String(200))
    last_message_at = db.Column(db.DateTime, default=utcnow, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    buyer = db.relationship("User", foreign_keys=[buyer_id])
    vendor_user = db.relationship("User", foreign_keys=[vendor_user_id])
    messages = db.relationship("Message", back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan")
    __table_args__ = (db.UniqueConstraint("context_type", "context_id", name="uq_conv_context"),)

    def other_party(self, user):
        return self.vendor_user if user.id == self.buyer_id else self.buyer


class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text)
    attachment_name = db.Column(db.String(255))
    attachment_stored = db.Column(db.String(255))
    attachment_mime = db.Column(db.String(80))
    is_system = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    read_at = db.Column(db.DateTime)
    conversation = db.relationship("Conversation", back_populates="messages")
    sender = db.relationship("User")


class ConversationAccessLog(db.Model):
    """Traçabilité des accès du support à une conversation (litige)."""
    __tablename__ = "conversation_access_logs"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), index=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    url = db.Column(db.String(255))
    critical = db.Column(db.Boolean, default=False)
    dedupe_key = db.Column(db.String(120))
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    __table_args__ = (db.UniqueConstraint("user_id", "dedupe_key", name="uq_notif_dedupe"),)


class EmailOutbox(db.Model):
    __tablename__ = "email_outbox"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    to_email = db.Column(db.String(190), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(12), default="queued", index=True)  # queued | sent | failed | not_sent (pas de SMTP)
    error = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)
    sent_at = db.Column(db.DateTime)


class SmsOutbox(db.Model):
    __tablename__ = "sms_outbox"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    to_phone = db.Column(db.String(30), nullable=False)
    body = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(12), default="not_sent")  # aucun fournisseur SMS configuré par défaut
    created_at = db.Column(db.DateTime, default=utcnow)


# --------------------------------------------------------------------------- Avis, support
class Review(db.Model):
    __tablename__ = "reviews"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), unique=True, nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendor_profiles.id"), nullable=False, index=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offers.id"), index=True)
    rating = db.Column(db.Integer, nullable=False)
    quality = db.Column(db.Integer)
    punctuality = db.Column(db.Integer)
    communication = db.Column(db.Integer)
    conformity = db.Column(db.Integer)
    comment = db.Column(db.Text)
    status = db.Column(db.String(12), default="published", index=True)  # published | hidden
    hidden_reason = db.Column(db.String(255))
    reported = db.Column(db.Boolean, default=False)
    report_reason = db.Column(db.String(255))
    vendor_reply = db.Column(db.Text)
    replied_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    is_demo = db.Column(db.Boolean, default=False)
    order = db.relationship("Order", back_populates="review")
    buyer = db.relationship("User")
    vendor = db.relationship("VendorProfile")
    offer = db.relationship("Offer")


TICKET_STATUSES = {
    "open": ("Ouvert", "info"), "in_progress": ("En cours", "warning"), "awaiting_user": ("En attente de l'utilisateur", "warning"),
    "solution_proposed": ("Solution proposée", "info"), "resolved": ("Résolu", "success"), "closed": ("Clôturé", "neutral"),
}
TICKET_CATEGORIES = {
    "non_conform": "Prestation non conforme", "no_show": "Absence", "late": "Retard", "payment": "Paiement",
    "refund": "Remboursement", "damaged": "Matériel endommagé", "behavior": "Comportement", "other": "Autre",
}


class Ticket(TimestampMixin, db.Model):
    __tablename__ = "tickets"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), index=True)
    category = db.Column(db.String(16), default="other")
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(18), default="open", index=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id"))
    resolution = db.Column(db.Text)
    author = db.relationship("User", foreign_keys=[author_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to])
    order = db.relationship("Order")
    messages = db.relationship("TicketMessage", back_populates="ticket", order_by="TicketMessage.id", cascade="all, delete-orphan")

    @property
    def status_label(self):
        return TICKET_STATUSES.get(self.status, (self.status, "neutral"))[0]


class TicketMessage(db.Model):
    __tablename__ = "ticket_messages"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    internal = db.Column(db.Boolean, default=False)  # note interne support
    attachment_name = db.Column(db.String(255))
    attachment_stored = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)
    ticket = db.relationship("Ticket", back_populates="messages")
    author = db.relationship("User")


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False)
    subject = db.Column(db.String(200))
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), default="new")  # new | handled
    created_at = db.Column(db.DateTime, default=utcnow)


# --------------------------------------------------------------------------- Contenus, paramètres, audit
class Page(db.Model):
    __tablename__ = "pages"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)  # Markdown
    published = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class HelpArticle(db.Model):
    __tablename__ = "help_articles"
    id = db.Column(db.Integer, primary_key=True)
    audience = db.Column(db.String(10), default="all")  # buyer | vendor | all
    section = db.Column(db.String(80))
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True)
    body = db.Column(db.Text, nullable=False)
    position = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)


class Banner(db.Model):
    __tablename__ = "banners"
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255))
    link_label = db.Column(db.String(60))
    active = db.Column(db.Boolean, default=True)
    position = db.Column(db.Integer, default=0)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    target_type = db.Column(db.String(30))
    target_id = db.Column(db.Integer)
    meta = db.Column(db.JSON)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    actor = db.relationship("User")


class AccountRequest(db.Model):
    """Demandes RGPD / clôture : export, rectification, suppression (traitées par le support)."""
    __tablename__ = "account_requests"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    kind = db.Column(db.String(12))  # export | delete | close
    status = db.Column(db.String(10), default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User")
