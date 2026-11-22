"""Fonctions communes à tous les comptes connectés : notifications, paramètres, support (tickets)."""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, logout_user

from ..extensions import db
from ..models import (AccountRequest, Notification, Order, TICKET_CATEGORIES, Ticket, TicketMessage, utcnow)
from ..security import perm_required
from ..services import audit, files
from ..services.notifications import notify
from ..utils import clean_phone, new_reference
from ._helpers import flag, flash_errors, paginate, password_problem, txt

bp = Blueprint("account", __name__)


@bp.before_request
@login_required
def _guard():
    pass


# ------------------------------------------------------------------ notifications
@bp.route("/notifications")
def notifications():
    page = paginate(Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()), 20)
    return render_template("account/notifications.html", page=page)


@bp.route("/notifications/<int:nid>/lire")
def notification_open(nid):
    n = db.session.get(Notification, nid)
    if n is None or n.user_id != current_user.id:
        abort(404)
    n.read_at = n.read_at or utcnow()
    db.session.commit()
    return redirect(n.url if n.url and n.url.startswith("/") else url_for("account.notifications"))


@bp.route("/notifications/tout-lire", methods=["POST"])
def notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, read_at=None).update({"read_at": utcnow()})
    db.session.commit()
    return redirect(request.referrer or url_for("account.notifications"))


# ------------------------------------------------------------------ paramètres
@bp.route("/parametres", methods=["GET", "POST"])
def settings():
    u = current_user
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            errors = []
            first, last = txt("first_name", 80), txt("last_name", 80)
            phone = clean_phone(txt("phone", 30))
            if not first or not last:
                errors.append("Nom et prénom obligatoires.")
            if not phone:
                errors.append("Numéro de téléphone invalide.")
            elif phone != u.phone:
                from ..models import User
                if User.query.filter(User.phone == phone, User.id != u.id).first():
                    errors.append("Ce numéro est déjà utilisé.")
            if not flash_errors(errors):
                u.first_name, u.last_name, u.phone = first, last, phone
                u.city, u.neighborhood = txt("city", 80), txt("neighborhood", 80)
                if u.is_buyer and u.account_type == "company":
                    u.company_name, u.ninea = txt("company_name", 160) or None, txt("ninea", 40) or None
                    u.billing_address = txt("billing_address", 255) or None
                db.session.commit()
                flash("Profil mis à jour.", "success")
        elif action == "password":
            if not u.check_password(request.form.get("current", "")):
                flash("Mot de passe actuel incorrect.", "danger")
            else:
                new = request.form.get("password", "")
                problem = password_problem(new) or ("Les mots de passe ne correspondent pas." if new != request.form.get("password2") else None)
                if problem:
                    flash(problem, "danger")
                else:
                    u.set_password(new)
                    audit.log("user.password_change", u)
                    db.session.commit()
                    flash("Mot de passe modifié.", "success")
        elif action == "notifications":
            u.notif_prefs = {"email": flag("pref_email"), "sms": flag("pref_sms")}
            u.marketing_consent = flag("marketing_consent")
            db.session.commit()
            flash("Préférences enregistrées. Les notifications dans l'application restent toujours actives.", "success")
        elif action == "data_request":
            kind = request.form.get("kind")
            if kind in ("export", "delete", "close"):
                db.session.add(AccountRequest(user_id=u.id, kind=kind))
                audit.log("account.request", u, kind=kind)
                db.session.commit()
                flash("Votre demande a été transmise au support. Certaines données doivent être conservées pour des raisons légales (commandes, paiements).", "success")
        return redirect(url_for("account.settings"))
    pending = AccountRequest.query.filter_by(user_id=u.id, status="open").all()
    return render_template("account/settings.html", pending=pending)


# ------------------------------------------------------------------ support / tickets
@bp.route("/support")
def tickets():
    if current_user.is_staff:
        return redirect(url_for("admin.tickets"))
    page = paginate(Ticket.query.filter_by(author_id=current_user.id).order_by(Ticket.updated_at.desc()))
    return render_template("account/tickets.html", page=page)


@bp.route("/support/nouveau", methods=["GET", "POST"])
def ticket_new():
    if current_user.is_staff:
        abort(404)
    order_id = request.values.get("order_id", type=int)
    order = db.session.get(Order, order_id) if order_id else None
    if order and current_user.id not in (order.buyer_id, order.vendor.user_id):
        abort(404)
    if request.method == "POST":
        subject, desc, cat = txt("subject", 200), txt("description", 4000), txt("category", 16)
        errors = []
        if len(subject) < 4:
            errors.append("Indiquez un objet.")
        if len(desc) < 15:
            errors.append("Décrivez votre demande (15 caractères minimum).")
        if not flash_errors(errors):
            t = Ticket(reference=new_reference("TK"), author_id=current_user.id, order_id=order.id if order else None,
                       category=cat if cat in TICKET_CATEGORIES else "other", subject=subject, description=desc)
            t.messages.append(TicketMessage(author_id=current_user.id, body=desc))
            db.session.add(t)
            db.session.commit()
            flash("Ticket créé. Nous vous répondrons sous " + "24 h ouvrées.", "success")
            return redirect(url_for("account.ticket_detail", ticket_id=t.id))
    return render_template("account/ticket_new.html", order=order, form=request.form)


@bp.route("/support/<int:ticket_id>", methods=["GET", "POST"])
def ticket_detail(ticket_id):
    t = db.session.get(Ticket, ticket_id) or abort(404)
    mine = t.author_id == current_user.id or (t.order and current_user.is_vendor and current_user.vendor and t.order.vendor_id == current_user.vendor.id)
    if not mine:
        abort(404)
    if request.method == "POST":
        body = txt("body", 4000)
        if body and t.status != "closed":
            m = TicketMessage(ticket_id=t.id, author_id=current_user.id, body=body)
            f = request.files.get("attachment")
            if f and f.filename:
                try:
                    info = files.save_private(f, files.ATTACH_EXT, subdir="tickets")
                    m.attachment_name, m.attachment_stored = info["original_name"], info["stored_name"]
                except files.UploadError as e:
                    flash(str(e), "danger")
                    return redirect(url_for("account.ticket_detail", ticket_id=t.id))
            t.messages.append(m)
            if t.status == "awaiting_user":
                t.status = "in_progress"
            t.updated_at = utcnow()
            db.session.commit()
        return redirect(url_for("account.ticket_detail", ticket_id=t.id))
    msgs = [m for m in t.messages if not m.internal]
    return render_template("account/ticket_detail.html", t=t, msgs=msgs)


@bp.route("/support/<int:ticket_id>/accepter-solution", methods=["POST"])
def ticket_accept(ticket_id):
    t = db.session.get(Ticket, ticket_id) or abort(404)
    if t.author_id != current_user.id:
        abort(404)
    if t.status == "solution_proposed":
        t.status = "resolved"
        db.session.commit()
        flash("Merci, le ticket est marqué comme résolu.", "success")
    return redirect(url_for("account.ticket_detail", ticket_id=t.id))
