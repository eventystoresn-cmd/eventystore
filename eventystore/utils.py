"""Utilitaires : formatage FCFA/dates, slugs, rendu Markdown sécurisé, constantes géographiques."""
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta

import markdown as _md
import nh3
from markupsafe import Markup

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
             "novembre", "décembre"]
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Régions du Sénégal (14) et villes/localités principales, pour la recherche géographique.
REGIONS = {
    "Dakar": ["Dakar", "Pikine", "Guédiawaye", "Rufisque", "Bargny", "Diamniadio", "Keur Massar", "Sangalkam",
              "Almadies", "Plateau", "Mermoz", "Ouakam", "Yoff", "Ngor", "Parcelles Assainies", "Médina", "Sacré-Cœur"],
    "Thiès": ["Thiès", "Mbour", "Saly", "Tivaouane", "Joal-Fadiouth", "Somone", "Popenguine", "Pout"],
    "Diourbel": ["Diourbel", "Touba", "Mbacké", "Bambey"],
    "Saint-Louis": ["Saint-Louis", "Richard-Toll", "Dagana", "Podor"],
    "Louga": ["Louga", "Linguère", "Kébémer"],
    "Fatick": ["Fatick", "Kaolack-Gossas", "Foundiougne", "Sokone"],
    "Kaolack": ["Kaolack", "Nioro du Rip", "Guinguinéo"],
    "Kaffrine": ["Kaffrine", "Koungheul", "Malem Hodar"],
    "Kolda": ["Kolda", "Vélingara", "Médina Yoro Foulah"],
    "Ziguinchor": ["Ziguinchor", "Oussouye", "Bignona", "Cap Skirring"],
    "Sédhiou": ["Sédhiou", "Goudomp", "Bounkiling"],
    "Tambacounda": ["Tambacounda", "Bakel", "Kidira", "Goudiry"],
    "Kédougou": ["Kédougou", "Salémata", "Saraya"],
    "Matam": ["Matam", "Kanel", "Ranérou"],
}
ALL_PLACES = sorted({p for v in REGIONS.values() for p in v} | set(REGIONS))
# Synonymes de localités (recherche tolérante)
PLACE_SYNONYMS = {"saly portudal": "Saly", "saly-portudal": "Saly", "st louis": "Saint-Louis", "saint louis": "Saint-Louis",
                  "guediawaye": "Guédiawaye", "thies": "Thiès", "mbacke": "Mbacké", "ziguinchor": "Ziguinchor",
                  "dakar plateau": "Plateau", "pa": "Parcelles Assainies", "medina": "Médina"}


def region_of(place):
    for r, places in REGIONS.items():
        if place == r or place in places:
            return r
    return None


def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen] or "item"


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def fmt_xof(value, with_unit=True):
    if value is None:
        return "—"
    s = f"{int(value):,}".replace(",", " ")
    return f"{s} FCFA" if with_unit else s


def fmt_date(d, long=True):
    if not d:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    if long:
        return f"{d.day}{'er' if d.day == 1 else ''} {MONTHS_FR[d.month - 1]} {d.year}"
    return d.strftime("%d/%m/%Y")


def fmt_date_range(a, b):
    if not a:
        return "—"
    if not b or a == b:
        return fmt_date(a)
    if a.year == b.year and a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS_FR[a.month - 1]} {a.year}"
    return f"{fmt_date(a)} → {fmt_date(b)}"


def fmt_dt(dt):
    """Les dates techniques sont en UTC ; Africa/Dakar = UTC+0 toute l'année."""
    if not dt:
        return "—"
    return f"{fmt_date(dt)} à {dt.strftime('%H:%M')}"


def time_ago(dt):
    if not dt:
        return ""
    s = (datetime.utcnow() - dt).total_seconds()
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {int(s // 60)} min"
    if s < 86400:
        return f"il y a {int(s // 3600)} h"
    if s < 86400 * 7:
        return f"il y a {int(s // 86400)} j"
    return fmt_date(dt, long=False)


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def parse_int(value, default=None, minimum=None, maximum=None):
    try:
        n = int(str(value).replace(" ", "").replace(" ", "").replace(" ", ""))
    except (ValueError, TypeError):
        return default
    if minimum is not None and n < minimum:
        n = minimum
    if maximum is not None and n > maximum:
        n = maximum
    return n


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def new_reference(prefix):
    return f"{prefix}-{datetime.utcnow():%y%m}-{secrets.token_hex(3).upper()}"


_ALLOWED_TAGS = {"p", "br", "strong", "em", "ul", "ol", "li", "h2", "h3", "h4", "a", "blockquote", "code", "hr", "table",
                 "thead", "tbody", "tr", "th", "td"}


def render_markdown(text):
    html = _md.markdown(text or "", extensions=["extra", "sane_lists"])
    return Markup(nh3.clean(html, tags=_ALLOWED_TAGS, attributes={"a": {"href", "title"}},
                            url_schemes={"http", "https", "mailto", "tel"}, link_rel="noopener noreferrer nofollow"))


def nl2p(text):
    """Texte brut utilisateur -> paragraphes HTML échappés."""
    from markupsafe import escape
    parts = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return Markup("".join(f"<p>{escape(p).replace(chr(10), Markup('<br>'))}</p>" for p in parts))


def clean_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        d = digits.lstrip("0")
        digits = "+221" + d if len(d) == 9 else "+" + d
    return digits if 9 <= len(re.sub(r"\D", "", digits)) <= 15 else None


def is_safe_next(url):
    return bool(url) and url.startswith("/") and not url.startswith("//") and "\\" not in url
