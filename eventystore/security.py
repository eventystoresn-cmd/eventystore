"""Contrôle d'accès (rôles/permissions), limitation des tentatives de connexion, TOTP (2FA admin)."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from functools import wraps

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user

from .extensions import db
from .models import LoginAttempt, utcnow


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            if current_user.role not in roles:
                abort(403)
            if current_user.is_staff and current_app.config["ENFORCE_ADMIN_2FA"] and not _2fa_ok():
                return redirect(url_for("auth.two_factor", next=request.path))
            return fn(*a, **kw)
        return wrapper
    return deco


def _2fa_ok():
    from flask import session
    return bool(session.get("2fa_ok")) and current_user.totp_enabled


def perm_required(perm):
    """Accès back-office : rôle staff + permission (le super_admin les a toutes)."""
    def deco(fn):
        @wraps(fn)
        @role_required("staff")
        def wrapper(*a, **kw):
            if perm and not current_user.has_perm(perm):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def verified_required(fn):
    """Fonctions sensibles réservées aux comptes dont l'e-mail est vérifié."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if current_user.is_authenticated and not current_user.email_verified:
            flash("Vérifiez votre adresse e-mail pour utiliser cette fonction.", "warning")
            return redirect(url_for("auth.verify_pending"))
        return fn(*a, **kw)
    return wrapper


# ------------------------------------------------------------------ anti brute-force
def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()[:64]


def too_many_attempts(identifier):
    cfg = current_app.config
    since = utcnow() - timedelta(minutes=cfg["LOGIN_WINDOW_MINUTES"])
    q = LoginAttempt.query.filter(LoginAttempt.created_at >= since, LoginAttempt.success.is_(False))
    by_id = q.filter(LoginAttempt.identifier == identifier).count()
    by_ip = q.filter(LoginAttempt.ip == client_ip()).count()
    return by_id >= cfg["LOGIN_MAX_ATTEMPTS"] or by_ip >= cfg["LOGIN_MAX_ATTEMPTS"] * 3


def record_attempt(identifier, success):
    db.session.add(LoginAttempt(identifier=(identifier or "")[:190], ip=client_ip(), success=success))
    if success:
        LoginAttempt.query.filter_by(identifier=identifier, success=False).delete()


# ------------------------------------------------------------------ TOTP (RFC 6238) sans dépendance externe
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def verify_totp(secret, code, window=1):
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit():
        return False
    now = int(time.time() // 30)
    return any(hmac.compare_digest(_hotp(secret, now + d), code) for d in range(-window, window + 1))


def totp_uri(secret, email):
    return f"otpauth://totp/EventyStore:{email}?secret={secret}&issuer=EventyStore"
