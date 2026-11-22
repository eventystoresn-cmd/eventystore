"""Paramètres de plateforme modifiables depuis l'administration (table `settings`)."""
from ..extensions import db
from ..models import Setting

DEFAULTS = {
    "hero_title": "Organisez votre événement, trouvez tous vos prestataires au même endroit",
    "hero_subtitle": "Traiteurs, salles, DJ, décoration, sonorisation, mobilier, tentes… Comparez, échangez, négociez et réservez en toute confiance au Sénégal.",
    "commission_default_bp": "1000",       # 10 % — valeur de départ à valider (cahier §24)
    "hold_minutes": "15",                  # pré-réservation pendant le paiement (cahier §7.3)
    "quote_order_hold_hours": "48",        # blocage après acceptation d'un devis, le temps de payer l'acompte
    "max_buyer_counters": "5",             # anti-spam des contre-propositions
    "confirm_rule": "vendor_and_deposit",  # deposit | vendor_and_deposit
    "payout_delay_days": "2",              # délai entre clôture de commande et solde disponible
    "payout_min_xof": "5000",
    "moderate_offers": "1",                # modération préalable des annonces
    "quote_default_validity_days": "7",
    "balance_due_days_before": "3",
    "support_hours": "Lundi–samedi, 8h–19h (heure de Dakar)",
    "support_email": "support@eventystore.sn",
    "support_phone": "",
    "company_legal": "EventyStore — raison sociale, adresse et NINEA à compléter avant mise en production.",
    "launch_zone_note": "Zone de lancement : Dakar et périphérie (extensible à tout le Sénégal).",
}


def get(key, default=None):
    row = db.session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULTS.get(key, default)


def get_int(key, default=0):
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        return default


def get_bool(key, default=False):
    return str(get(key, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


def set_(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.value = str(value)
