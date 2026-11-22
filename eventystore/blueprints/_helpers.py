"""Aides communes aux vues : lecture/validation de formulaires, pagination, accès aux ressources."""
import re
from types import SimpleNamespace

from flask import abort, flash, request
from flask_login import current_user

from ..utils import parse_date, parse_int

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def txt(name, maxlen=255, default=""):
    return (request.form.get(name) or default).strip()[:maxlen]


def num(name, default=None, minimum=None, maximum=None):
    return parse_int(request.form.get(name), default, minimum, maximum)


def dt(name):
    return parse_date(request.form.get(name))


def flag(name):
    return request.form.get(name) in ("1", "on", "true", "yes")


def password_problem(pw):
    if len(pw or "") < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Le mot de passe doit contenir au moins une lettre et un chiffre."
    return None


def flash_errors(errors):
    for e in errors:
        flash(e, "danger")
    return bool(errors)


def owned_or_404(obj, **checks):
    """404 (et non 403) si l'objet n'existe pas ou n'appartient pas à l'utilisateur."""
    if obj is None:
        abort(404)
    for attr, expected in checks.items():
        if getattr(obj, attr) != expected:
            abort(404)
    return obj


def paginate(query, per_page=15):
    page = parse_int(request.args.get("page"), 1, 1)
    total = query.count()
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return SimpleNamespace(items=items, page=page, pages=pages, total=total)


def pct_bp(name, default=None):
    """Lit un pourcentage saisi (« 7,5 » ou « 10 ») et le convertit en points de base (750). Borne 0–50 %."""
    raw = (request.form.get(name) or "").strip().replace(",", ".").replace("%", "")
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if val < 0 or val > 50:
        return default
    return int(round(val * 100))
