"""Inscription, connexion, vérification e-mail, mot de passe oublié, 2FA du personnel."""
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from ..extensions import db
from ..models import Category, EmailOutbox, User, VendorProfile, utcnow
from ..security import (client_ip, new_totp_secret, record_attempt, too_many_attempts, totp_uri, verify_totp)
from ..services import audit
from ..services.notifications import abs_url, send_email
from ..utils import REGIONS, clean_phone, is_safe_next, slugify
from ._helpers import EMAIL_RE, flag, flash_errors, password_problem, txt

bp = Blueprint("auth", __name__)


def _ser(salt):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=salt)


def send_verification(user):
    token = _ser("verify-email").dumps({"uid": user.id, "email": user.email})
    link = abs_url(url_for("auth.verify_email", token=token))
    send_email(user.email, "Confirmez votre adresse e-mail — EventyStore",
               f"Bonjour {user.first_name},\n\nBienvenue sur EventyStore. Confirmez votre adresse e-mail en cliquant sur ce lien "
               f"(valable 3 jours) :\n{link}\n\nSi vous n'êtes pas à l'origine de cette inscription, ignorez ce message.", user.id)
    # Démo sans SMTP : on valide directement l'adresse (l'e-mail reste consultable dans la boîte d'envoi).
    if current_app.config.get("AUTO_VERIFY_EMAIL") and not current_app.config.get("SMTP_HOST") and not user.email_verified_at:
        user.email_verified_at = utcnow()


def _unique_slug(name):
    base, slug, i = slugify(name), None, 1
    slug = base
    while VendorProfile.query.filter_by(slug=slug).first():
        i += 1
        slug = f"{base}-{i}"
    return slug


def _common_signup_errors(email, phone_raw, password, password2):
    errors = []
    if not EMAIL_RE.match(email) or len(email) > 190:
        errors.append("Adresse e-mail invalide.")
    elif User.query.filter(func.lower(User.email) == email.lower()).first():
        errors.append("Un compte existe déjà avec cette adresse e-mail.")
    phone = clean_phone(phone_raw)
    if not phone:
        errors.append("Numéro de téléphone invalide (ex. 77 123 45 67).")
    elif User.query.filter_by(phone=phone).first():
        errors.append("Un compte existe déjà avec ce numéro de téléphone.")
    pb = password_problem(password)
    if pb:
        errors.append(pb)
    if password != password2:
        errors.append("Les mots de passe ne correspondent pas.")
    if not flag("accept_terms"):
        errors.append("Vous devez accepter les conditions d'utilisation et la politique de confidentialité.")
    return errors, phone


@bp.route("/inscription", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("auth.after_login"))
    if request.method == "POST":
        email = txt("email", 190).lower()
        errors, phone = _common_signup_errors(email, txt("phone", 30), request.form.get("password", ""),
                                              request.form.get("password2", ""))
        first, last = txt("first_name", 80), txt("last_name", 80)
        if not first or not last:
            errors.append("Nom et prénom obligatoires.")
        acct = txt("account_type", 16)
        if acct not in ("individual", "company"):
            acct = "individual"
        if acct == "company" and not txt("company_name", 160):
            errors.append("Indiquez le nom de votre entreprise.")
        if not flash_errors(errors):
            u = User(role="buyer", first_name=first, last_name=last, email=email, phone=phone, account_type=acct,
                     company_name=txt("company_name", 160) or None, ninea=txt("ninea", 40) or None,
                     city=txt("city", 80), neighborhood=txt("neighborhood", 80),
                     marketing_consent=flag("marketing_consent"), terms_accepted_at=utcnow(),
                     notif_prefs={"email": True, "sms": False})
            u.set_password(request.form["password"])
            db.session.add(u)
            db.session.flush()
            send_verification(u)
            audit.log("user.register", u, actor=u, role="buyer")
            db.session.commit()
            login_user(u)
            flash("Compte créé ! Un e-mail de confirmation vous a été envoyé.", "success")
            return redirect(url_for("auth.verify_pending"))
    return render_template("auth/register.html", regions=REGIONS, form=request.form)


@bp.route("/inscription-prestataire", methods=["GET", "POST"])
def register_vendor():
    if current_user.is_authenticated:
        return redirect(url_for("auth.after_login"))
    cats = Category.query.filter_by(parent_id=None, active=True).order_by(Category.position).all()
    if request.method == "POST":
        email = txt("email", 190).lower()
        errors, phone = _common_signup_errors(email, txt("phone", 30), request.form.get("password", ""),
                                              request.form.get("password2", ""))
        first, last, biz = txt("first_name", 80), txt("last_name", 80), txt("business_name", 160)
        if not first or not last:
            errors.append("Nom et prénom du responsable obligatoires.")
        if not biz:
            errors.append("Le nom commercial est obligatoire.")
        legal = txt("legal_status", 40)
        if legal not in ("independant", "entreprise", "association"):
            errors.append("Choisissez votre statut juridique.")
        cat_ids = [int(c) for c in request.form.getlist("categories") if c.isdigit()]
        valid_ids = {c.id for c in Category.query.filter(Category.id.in_(cat_ids)).all()} if cat_ids else set()
        if not valid_ids:
            errors.append("Sélectionnez au moins une catégorie d'activité.")
        areas = [a for a in request.form.getlist("areas") if a in REGIONS]
        if not areas:
            errors.append("Sélectionnez au moins une région desservie.")
        if not flash_errors(errors):
            u = User(role="vendor", first_name=first, last_name=last, email=email, phone=phone,
                     city=txt("city", 80), terms_accepted_at=utcnow(), marketing_consent=flag("marketing_consent"),
                     notif_prefs={"email": True, "sms": False})
            u.set_password(request.form["password"])
            db.session.add(u)
            db.session.flush()
            city = txt("city", 80)
            vp = VendorProfile(user_id=u.id, business_name=biz, slug=_unique_slug(biz), legal_status=legal,
                               activity_type=txt("activity_type", 160), contact_phone=phone, contact_email=email,
                               city=city, region=next((r for r, pl in REGIONS.items() if city == r or city in pl), areas[0]),
                               served_areas=areas, category_ids=sorted(valid_ids), verification_status="draft")
            db.session.add(vp)
            send_verification(u)
            audit.log("user.register", u, actor=u, role="vendor")
            db.session.commit()
            login_user(u)
            flash("Compte prestataire créé. Complétez votre profil et soumettez vos justificatifs pour être validé.", "success")
            return redirect(url_for("vendor.verification"))
    return render_template("auth/register_vendor.html", categories=cats, regions=REGIONS, form=request.form,
                           selected_cats=[int(c) for c in request.form.getlist("categories") if c.isdigit()],
                           selected_areas=request.form.getlist("areas"))


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.after_login"))
    nxt = request.args.get("next") or request.form.get("next") or ""
    if request.method == "POST":
        ident = txt("identifier", 190).lower()
        pw = request.form.get("password", "")
        if too_many_attempts(ident):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "danger")
            return render_template("auth/login.html", next=nxt), 429
        user = None
        if "@" in ident:
            user = User.query.filter(func.lower(User.email) == ident).first()
        else:
            phone = clean_phone(ident)
            user = User.query.filter_by(phone=phone).first() if phone else None
        if user is None or not user.check_password(pw):
            record_attempt(ident, False)
            db.session.commit()
            flash("Identifiants incorrects.", "danger")
            return render_template("auth/login.html", next=nxt, identifier=ident), 401
        if user.status != "active":
            flash("Ce compte est suspendu ou clôturé. Contactez le support.", "danger")
            return render_template("auth/login.html", next=nxt), 403
        record_attempt(ident, True)
        user.last_login_at = utcnow()
        session.clear()
        login_user(user, remember=flag("remember"))
        if user.is_staff:
            audit.log("staff.login", user, actor=user)
        db.session.commit()
        if user.is_staff and current_app.config["ENFORCE_ADMIN_2FA"]:
            return redirect(url_for("auth.two_factor", next=nxt))
        return redirect(nxt if is_safe_next(nxt) else url_for("auth.after_login"))
    return render_template("auth/login.html", next=nxt)


@bp.route("/apres-connexion")
@login_required
def after_login():
    if current_user.is_staff:
        return redirect(url_for("admin.dashboard"))
    if current_user.is_vendor:
        return redirect(url_for("vendor.dashboard"))
    return redirect(url_for("buyer.dashboard"))


@bp.route("/deconnexion", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("public.home"))


# ------------------------------------------------------------------ vérification e-mail
@bp.route("/verifier-email/<token>")
def verify_email(token):
    try:
        data = _ser("verify-email").loads(token, max_age=3 * 86400)
    except SignatureExpired:
        flash("Ce lien a expiré. Demandez un nouvel e-mail de confirmation.", "warning")
        return redirect(url_for("auth.verify_pending") if current_user.is_authenticated else url_for("auth.login"))
    except BadSignature:
        abort(404)
    user = db.session.get(User, data["uid"])
    if user is None or user.email != data["email"]:
        abort(404)
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
        db.session.commit()
    flash("Adresse e-mail confirmée. Merci !", "success")
    return redirect(url_for("auth.after_login") if current_user.is_authenticated else url_for("auth.login"))


@bp.route("/verification-en-attente")
@login_required
def verify_pending():
    if current_user.email_verified:
        return redirect(url_for("auth.after_login"))
    return render_template("auth/verify_pending.html")


@bp.route("/renvoyer-verification", methods=["POST"])
@login_required
def resend_verification():
    if not current_user.email_verified:
        recent = EmailOutbox.query.filter(EmailOutbox.user_id == current_user.id,
                                          EmailOutbox.subject.like("Confirmez%")).order_by(EmailOutbox.id.desc()).first()
        if recent and (utcnow() - recent.created_at).total_seconds() < 60:
            flash("Un e-mail vient d'être envoyé, patientez une minute.", "warning")
        else:
            send_verification(current_user)
            db.session.commit()
            flash("E-mail de confirmation renvoyé.", "success")
    return redirect(request.referrer if is_safe_next(request.referrer or "") else url_for("auth.verify_pending"))


# ------------------------------------------------------------------ mot de passe oublié
@bp.route("/mot-de-passe-oublie", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = txt("email", 190).lower()
        user = User.query.filter(func.lower(User.email) == email).first()
        if user and user.status == "active":
            token = _ser("reset-password").dumps({"uid": user.id, "h": user.password_hash[-12:]})
            link = abs_url(url_for("auth.reset_password", token=token))
            send_email(user.email, "Réinitialisation de votre mot de passe — EventyStore",
                       f"Bonjour {user.first_name},\n\nPour choisir un nouveau mot de passe (lien valable 1 heure) :\n{link}\n\n"
                       "Si vous n'avez pas fait cette demande, ignorez ce message : votre mot de passe reste inchangé.", user.id)
            db.session.commit()
        # réponse identique dans tous les cas (pas d'énumération de comptes)
        flash("Si un compte existe pour cette adresse, un e-mail de réinitialisation vient d'être envoyé.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reinitialiser/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        data = _ser("reset-password").loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        flash("Lien invalide ou expiré. Refaites une demande.", "danger")
        return redirect(url_for("auth.forgot_password"))
    user = db.session.get(User, data["uid"])
    if user is None or user.password_hash[-12:] != data["h"]:  # lien à usage unique
        flash("Lien déjà utilisé ou invalide.", "danger")
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        problem = password_problem(pw) or ("Les mots de passe ne correspondent pas." if pw != request.form.get("password2") else None)
        if problem:
            flash(problem, "danger")
        else:
            user.set_password(pw)
            audit.log("user.password_reset", user, actor=user)
            db.session.commit()
            flash("Mot de passe mis à jour. Connectez-vous.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html")


# ------------------------------------------------------------------ 2FA (TOTP) du personnel
@bp.route("/2fa", methods=["GET", "POST"])
@login_required
def two_factor():
    if not current_user.is_staff:
        abort(404)
    nxt = request.args.get("next") or ""
    setup = not current_user.totp_enabled
    if setup and not current_user.totp_secret:
        current_user.totp_secret = new_totp_secret()
        db.session.commit()
    if request.method == "POST":
        if too_many_attempts(f"2fa:{current_user.id}"):
            flash("Trop de tentatives, patientez quelques minutes.", "danger")
        elif verify_totp(current_user.totp_secret, request.form.get("code")):
            if setup:
                current_user.totp_enabled = True
                audit.log("staff.2fa_enabled", current_user)
            session["2fa_ok"] = True
            record_attempt(f"2fa:{current_user.id}", True)
            db.session.commit()
            return redirect(nxt if is_safe_next(nxt) else url_for("admin.dashboard"))
        else:
            record_attempt(f"2fa:{current_user.id}", False)
            db.session.commit()
            flash("Code invalide.", "danger")
    return render_template("auth/two_factor.html", setup=setup, secret=current_user.totp_secret,
                           uri=totp_uri(current_user.totp_secret, current_user.email) if setup else None, next=nxt)
