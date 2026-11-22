"""Parcours de paiement et webhooks fournisseur.

Le navigateur n'est jamais une preuve de paiement : seule `payments.process_event` (appelée par le sandbox
interne ou par un webhook à signature vérifiée) finalise un paiement.
"""
import json
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for)
from flask_login import current_user, login_required

from ..extensions import csrf, db
from ..models import Order, Payment
from ..security import role_required
from ..services import orders as order_service
from ..services import payments as pay_service
from ..services import settings

bp = Blueprint("payments", __name__)


def _buyer_order(order_id):
    o = db.session.get(Order, order_id)
    if o is None or o.buyer_id != current_user.id:
        abort(404)
    return o


@bp.route("/paiement/<int:order_id>", methods=["GET", "POST"])
@login_required
@role_required("buyer")
def pay(order_id):
    o = _buyer_order(order_id)
    if o.status == "cancelled":
        flash("Cette réservation est annulée.", "warning")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    if o.remaining_xof <= 0:
        flash("Cette commande est entièrement réglée.", "success")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    if (o.snapshot or {}).get("offline"):
        flash("Cette réservation se règle directement auprès du prestataire.", "info")
        return redirect(url_for("buyer.order_detail", order_id=o.id))
    methods = pay_service.available_methods()
    deposit_due = max(0, (o.deposit_xof or 0) - (o.paid_xof or 0))
    holds = o.status == "pending_payment" and o.hold_expires_at
    if request.method == "POST":
        choice = request.form.get("choice", "deposit")
        method = request.form.get("method")
        if method not in dict(methods):
            flash("Choisissez un moyen de paiement.", "danger")
            return redirect(url_for("payments.pay", order_id=o.id))
        if o.status == "pending_payment" and o.hold_expires_at and o.hold_expires_at < datetime.utcnow():
            flash("Le blocage du créneau a expiré. Recommencez votre réservation.", "danger")
            return redirect(url_for("buyer.order_detail", order_id=o.id))
        amount = order_service.amount_due_now(o, choice)
        kind = "full" if amount >= o.remaining_xof and (o.paid_xof or 0) == 0 else ("balance" if (o.paid_xof or 0) >= (o.deposit_xof or 0) and o.paid_xof else "deposit")
        if amount <= 0:
            flash("Aucun montant à régler.", "info")
            return redirect(url_for("buyer.order_detail", order_id=o.id))
        prov = pay_service.active_provider()
        try:
            p = pay_service.create_payment(o, amount, kind, method)
            url = prov.create_checkout(p, o)
        except pay_service.ProviderNotConfigured as e:
            db.session.rollback()
            flash(f"Paiement indisponible : {e}", "danger")
            return redirect(url_for("payments.pay", order_id=o.id))
        db.session.commit()
        return redirect(url)
    return render_template("payments/pay.html", o=o, methods=methods, deposit_due=deposit_due, holds=holds,
                           sandbox=pay_service.active_provider().code == "sandbox",
                           hold_minutes=settings.get_int("hold_minutes", 15))


@bp.route("/paiement/sandbox/<ref>", methods=["GET", "POST"])
@login_required
def sandbox_checkout(ref):
    """Page de paiement SIMULÉE (aucun argent réel). Disponible uniquement avec PAYMENT_PROVIDER=sandbox."""
    if pay_service.active_provider().code != "sandbox":
        abort(404)
    p = Payment.query.filter_by(external_ref=ref, provider="sandbox").first_or_404()
    if p.order.buyer_id != current_user.id:
        abort(404)
    if request.method == "POST":
        success = request.form.get("result") == "success"
        ev, ok = pay_service.sandbox_event(p, success)
        pay_service.process_event("sandbox", ev, ok)
        return redirect(url_for("payments.result", ref=ref))
    return render_template("payments/sandbox.html", p=p, o=p.order)


@bp.route("/paiement/resultat/<ref>")
@login_required
def result(ref):
    """Page de retour : lit l'état enregistré par le serveur, ne le modifie jamais."""
    p = Payment.query.filter_by(external_ref=ref).first_or_404()
    if p.order.buyer_id != current_user.id:
        abort(404)
    return render_template("payments/result.html", p=p, o=p.order)


@bp.route("/webhooks/payments/<provider>", methods=["POST"])
@csrf.exempt
def webhook(provider):
    """Webhook fournisseur : signature HMAC vérifiée avant tout traitement ; idempotent."""
    prov = pay_service.PROVIDERS.get(provider)
    if prov is None or provider == "sandbox" and current_app.config["ENV"] == "production":
        abort(404)
    raw = request.get_data()
    ok = prov.verify_signature(raw, request.headers)
    try:
        event = json.loads(raw.decode() or "{}")
    except ValueError:
        return jsonify(error="invalid json"), 400
    if hasattr(prov, "normalize_event"):
        event = prov.normalize_event(event)
        if event["type"] == "ignored":
            return jsonify(ok=True, message="événement ignoré"), 200
        if ok and event["type"] == "payment.succeeded":
            try:
                if not prov.confirm_server_side(event):
                    return jsonify(ok=False, message="confirmation serveur échouée"), 422
            except pay_service.ProviderNotConfigured:
                return jsonify(ok=False, message="confirmation indisponible, réessayer"), 503
    done, msg = pay_service.process_event(provider, event, ok)
    status = 200 if done else (401 if not ok else 422)
    return jsonify(ok=done, message=msg), status
