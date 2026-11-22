"""Back-office EventyStore : comptes, vérification prestataires, catalogue, commandes, finance, support, contenus, audit."""
import csv
import io
import json
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for)
from flask_login import current_user

from ..extensions import db
from ..icons import ICONS
from ..models import (AccountRequest, AuditLog, Banner, Category, CommissionRule, ContactMessage, Conversation,
                      ConversationAccessLog, EmailOutbox, HelpArticle, LedgerEntry, Offer, Order, Page, Payment, Payout,
                      Quote, QuoteRequest, Refund, Review, SmsOutbox, STAFF_ROLES, Ticket, TicketMessage, User,
                      VendorDocument, VendorProfile, VerificationEvent, WebhookEvent, utcnow)
from ..security import new_totp_secret, perm_required, role_required
from ..services import audit, files, finance, orders as order_service, settings
from ..services.notifications import notify
from ..services.search import refresh_search_fields
from ..utils import fmt_xof, slugify, clean_phone
from ._helpers import EMAIL_RE, dt, flag, flash_errors, num, owned_or_404, paginate, password_problem, pct_bp, txt

bp = Blueprint("admin", __name__)


@bp.before_request
@role_required("staff")
def _guard():
    pass


# ------------------------------------------------------------------ tableau de bord
@bp.route("/")
def dashboard():
    c = db.session.query
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    kpi = {
        "buyers": User.query.filter_by(role="buyer").count(),
        "vendors": VendorProfile.query.count(),
        "vendors_verified": VendorProfile.query.filter_by(verification_status="validated").count(),
        "suspended": User.query.filter_by(status="suspended").count(),
        "offers_active": Offer.query.filter_by(status="active").count(),
        "quotes": QuoteRequest.query.count(),
        "quotes_answered": QuoteRequest.query.filter(QuoteRequest.first_response_at.isnot(None)).count(),
        "orders": Order.query.filter(Order.status != "pending_payment").count(),
        "cancelled": Order.query.filter_by(status="cancelled").count(),
        "disputes": Ticket.query.filter(Ticket.status.in_(("open", "in_progress", "awaiting_user"))).count(),
        "gross": int(c(db.func.coalesce(db.func.sum(Payment.amount_xof), 0)).filter(Payment.status.in_(("succeeded", "partially_refunded", "refunded"))).scalar()),
        "commissions": int(c(db.func.coalesce(db.func.sum(Payment.commission_xof), 0)).filter(Payment.status.in_(("succeeded", "partially_refunded"))).scalar()),
        "refunds": int(c(db.func.coalesce(db.func.sum(Refund.amount_xof), 0)).scalar()),
        "payouts_paid": int(c(db.func.coalesce(db.func.sum(Payout.amount_xof), 0)).filter(Payout.status == "paid").scalar()),
    }
    alerts = {
        "payments_failed": Payment.query.filter_by(status="failed").filter(Payment.created_at >= datetime.utcnow() - timedelta(days=7)).count(),
        "payouts_pending": Payout.query.filter(Payout.status.in_(("requested", "in_review"))).count(),
        "vendors_pending": VendorProfile.query.filter(VendorProfile.verification_status.in_(("submitted", "in_review"))).count(),
        "offers_pending": Offer.query.filter_by(status="submitted").count(),
        "reviews_reported": Review.query.filter_by(reported=True, status="published").count(),
        "quotes_unanswered": QuoteRequest.query.filter(QuoteRequest.status == "open",
                                                      QuoteRequest.created_at < datetime.utcnow() - timedelta(days=2)).count(),
        "refunds_manual": Refund.query.filter_by(status="manual_required").count(),
        "tickets_open": Ticket.query.filter_by(status="open").count(),
        "messages_new": ContactMessage.query.filter_by(status="new").count(),
        "requests": AccountRequest.query.filter_by(status="open").count(),
    }
    per_cat = (db.session.query(Category.name, db.func.count(Offer.id)).join(Offer, Offer.category_id == Category.id)
               .filter(Offer.status == "active").group_by(Category.name).order_by(db.func.count(Offer.id).desc()).limit(8).all())
    per_region = (db.session.query(Offer.region, db.func.count(Offer.id)).filter(Offer.status == "active")
                  .group_by(Offer.region).order_by(db.func.count(Offer.id).desc()).limit(8).all())
    return render_template("admin/dashboard.html", kpi=kpi, alerts=alerts, per_cat=per_cat, per_region=per_region)


# ------------------------------------------------------------------ utilisateurs
@bp.route("/utilisateurs")
@perm_required("support")
def users():
    q = User.query
    role, status, term = request.args.get("role", ""), request.args.get("statut", ""), (request.args.get("q") or "").strip()
    if role:
        q = q.filter_by(role=role)
    if status:
        q = q.filter_by(status=status)
    if term:
        like = f"%{term}%"
        q = q.filter(db.or_(User.email.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like), User.phone.ilike(like)))
    page = paginate(q.order_by(User.created_at.desc()), 20)
    return render_template("admin/users.html", page=page, role=role, status=status, term=term)


@bp.route("/utilisateurs/<int:uid>", methods=["GET", "POST"])
@perm_required("support")
def user_detail(uid):
    u = db.session.get(User, uid) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        reason = txt("reason", 250)
        if u.id == current_user.id and action in ("suspend", "close"):
            flash("Vous ne pouvez pas suspendre votre propre compte.", "danger")
        elif action == "suspend":
            u.status = "suspended"
            audit.log("user.suspend", u, reason=reason)
            if u.vendor:
                u.vendor.verification_status = "suspended" if u.vendor.verification_status == "validated" else u.vendor.verification_status
            flash("Compte suspendu.", "success")
        elif action == "reactivate":
            u.status = "active"
            if u.vendor and u.vendor.verification_status == "suspended":
                u.vendor.verification_status = "validated"
            audit.log("user.reactivate", u, reason=reason)
            flash("Compte réactivé.", "success")
        elif action == "close":
            u.status = "closed"
            audit.log("user.close", u, reason=reason)
            flash("Compte clôturé.", "success")
        elif action == "verify_email":
            u.email_verified_at = u.email_verified_at or utcnow()
            audit.log("user.verify_email_manual", u)
            flash("E-mail marqué comme vérifié.", "success")
        db.session.commit()
        return redirect(url_for("admin.user_detail", uid=u.id))
    orders = Order.query.filter(db.or_(Order.buyer_id == u.id, Order.vendor_id == (u.vendor.id if u.vendor else -1))).order_by(Order.created_at.desc()).limit(10).all()
    logs = AuditLog.query.filter(db.or_(AuditLog.actor_id == u.id, db.and_(AuditLog.target_type == "User", AuditLog.target_id == u.id))).order_by(AuditLog.id.desc()).limit(15).all() if current_user.has_perm("audit") else []
    return render_template("admin/user_detail.html", u=u, orders=orders, logs=logs)


@bp.route("/equipe", methods=["GET", "POST"])
@perm_required("audit")  # réservé au super-administrateur
def staff():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            email, first, last = txt("email", 190).lower(), txt("first_name", 80), txt("last_name", 80)
            roles = [r for r in request.form.getlist("roles") if r in STAFF_ROLES]
            pw = request.form.get("password", "")
            errors = []
            if not EMAIL_RE.match(email) or User.query.filter(db.func.lower(User.email) == email).first():
                errors.append("E-mail invalide ou déjà utilisé.")
            if not first or not last or not roles:
                errors.append("Nom, prénom et au moins un rôle sont requis.")
            if password_problem(pw):
                errors.append(password_problem(pw))
            if not flash_errors(errors):
                u = User(role="staff", first_name=first, last_name=last, email=email, staff_roles=roles,
                         email_verified_at=utcnow(), terms_accepted_at=utcnow(), notif_prefs={"email": True})
                u.set_password(pw)
                db.session.add(u)
                audit.log("staff.create", u, roles=roles)
                db.session.commit()
                flash("Compte équipe créé. Il devra activer la double authentification à sa première connexion.", "success")
        elif action == "roles":
            u = db.session.get(User, num("uid", 0)) or abort(404)
            if u.is_staff:
                roles = [r for r in request.form.getlist("roles") if r in STAFF_ROLES]
                if u.id == current_user.id and "super_admin" not in roles:
                    flash("Vous ne pouvez pas retirer votre propre rôle de super-administrateur.", "danger")
                else:
                    u.staff_roles = roles
                    audit.log("staff.roles", u, roles=roles)
                    db.session.commit()
                    flash("Rôles mis à jour.", "success")
        return redirect(url_for("admin.staff"))
    return render_template("admin/staff.html", staff=User.query.filter_by(role="staff").order_by(User.id).all())


# ------------------------------------------------------------------ prestataires
@bp.route("/prestataires")
@perm_required("vendors")
def vendors():
    status = request.args.get("statut", "")
    q = VendorProfile.query
    if status:
        q = q.filter_by(verification_status=status)
    elif request.args.get("tous") != "1":
        q = q.filter(VendorProfile.verification_status.in_(("submitted", "in_review", "complement_requested")))
    term = (request.args.get("q") or "").strip()
    if term:
        q = q.filter(VendorProfile.business_name.ilike(f"%{term}%"))
    page = paginate(q.order_by(VendorProfile.submitted_at.asc().nullslast(), VendorProfile.id.desc()), 20)
    return render_template("admin/vendors.html", page=page, status=status, term=term, all_=request.args.get("tous") == "1")


@bp.route("/prestataires/<int:vid>", methods=["GET", "POST"])
@perm_required("vendors")
def vendor_detail(vid):
    v = db.session.get(VendorProfile, vid) or abort(404)
    if request.method == "POST":
        action, note = request.form.get("action"), txt("note", 1500)
        old = v.verification_status
        if action == "start_review":
            v.verification_status = "in_review"
        elif action == "validate":
            v.verification_status, v.verified_badge, v.verified_at, v.verification_note = "validated", True, utcnow(), note or None
        elif action in ("refuse", "complement"):
            if not note:
                flash("Un motif est obligatoire (il sera visible par le prestataire).", "danger")
                return redirect(url_for("admin.vendor_detail", vid=v.id))
            v.verification_status = "refused" if action == "refuse" else "complement_requested"
            v.verification_note, v.verified_badge = note, False
        elif action == "suspend":
            if not note:
                flash("Indiquez un motif de suspension.", "danger")
                return redirect(url_for("admin.vendor_detail", vid=v.id))
            v.verification_status, v.verification_note, v.verified_badge = "suspended", note, False
            for o in v.offers.filter_by(status="active"):
                o.status = "suspended"
        elif action == "reinstate":
            v.verification_status, v.verified_badge = "validated", True
        elif action == "badge":
            v.verified_badge = not v.verified_badge
            audit.log("vendor.badge", v, value=v.verified_badge, reason=note)
        elif action == "commission":
            bp_ = pct_bp("commission_pct")
            v.commission_override_bp = bp_
            audit.log("commission.vendor_override", v, bp=bp_)
        if action in ("start_review", "validate", "refuse", "complement", "suspend", "reinstate"):
            db.session.add(VerificationEvent(vendor_id=v.id, actor_id=current_user.id, from_status=old,
                                             to_status=v.verification_status, note=note))
            audit.log(f"vendor.{action}", v, note=note)
            titles = {"validate": "Votre compte prestataire est validé ✔", "refuse": "Votre dossier prestataire a été refusé",
                      "complement": "Complément demandé sur votre dossier", "suspend": "Votre compte prestataire est suspendu",
                      "reinstate": "Votre compte prestataire est réactivé", "start_review": "Votre dossier est en cours de vérification"}
            notify(v.user, "vendor_status", titles[action], note, "/pro/verification", critical=action != "start_review",
                   dedupe_key=f"vstatus:{v.id}:{int(utcnow().timestamp())}")
            if v.verification_status == "validated":
                for o in v.offers:
                    refresh_search_fields(o)
        db.session.commit()
        flash("Dossier mis à jour.", "success")
        return redirect(url_for("admin.vendor_detail", vid=v.id))
    events = VerificationEvent.query.filter_by(vendor_id=v.id).order_by(VerificationEvent.id.desc()).all()
    return render_template("admin/vendor_detail.html", v=v, events=events, can_docs=current_user.has_perm("vendor_docs"),
                           offers=v.offers.all())


@bp.route("/documents/<int:doc_id>")
@perm_required("vendor_docs")
def vendor_doc(doc_id):
    d = db.session.get(VendorDocument, doc_id) or abort(404)
    audit.log("vendor_doc.access", d, vendor=d.vendor.business_name, doc_type=d.doc_type)  # accès journalisé
    db.session.commit()
    return send_file(io.BytesIO(files.read_private(d.stored_name)), mimetype=d.mime, download_name=d.original_name,
                     as_attachment=False, max_age=0)


# ------------------------------------------------------------------ catalogue
@bp.route("/categories", methods=["GET", "POST"])
@perm_required("catalog")
def categories():
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("create", "update"):
            cat = db.session.get(Category, num("cat_id", 0)) if action == "update" else Category()
            if cat is None:
                abort(404)
            name = txt("name", 120)
            if not name:
                flash("Le nom est obligatoire.", "danger")
                return redirect(url_for("admin.categories"))
            cat.name = name
            cat.description = txt("description", 1000)
            cat.icon = txt("icon", 40) or "sparkles"
            cat.position = num("position", 0)
            cat.active = flag("active") if action == "update" else True
            parent = num("parent_id", None)
            if parent and parent != cat.id:
                cat.parent_id = parent
            elif action == "update":
                cat.parent_id = None if not parent else cat.parent_id
            raw = txt("attributes", 2000)
            try:
                cat.attributes = json.loads(raw) if raw else []
            except ValueError:
                cat.attributes = [{"key": slugify(a.strip(), 30), "label": a.strip(), "type": "text"} for a in raw.split(",") if a.strip()]
            if action == "create":
                cat.slug = slugify(name, 100)
                n = 1
                while Category.query.filter_by(slug=cat.slug).first():
                    n += 1
                    cat.slug = f"{slugify(name, 100)}-{n}"
                db.session.add(cat)
            audit.log(f"category.{action}", cat, name=name)
            db.session.commit()
            flash("Catégorie enregistrée.", "success")
        return redirect(url_for("admin.categories"))
    roots = Category.query.filter_by(parent_id=None).order_by(Category.position).all()
    return render_template("admin/categories.html", roots=roots, icons=sorted(ICONS))


@bp.route("/annonces")
@perm_required("offers")
def offers():
    status = request.args.get("statut", "submitted")
    q = Offer.query
    if status != "all":
        q = q.filter_by(status=status)
    term = (request.args.get("q") or "").strip()
    if term:
        q = q.filter(Offer.title.ilike(f"%{term}%"))
    page = paginate(q.order_by(Offer.updated_at.desc()), 20)
    return render_template("admin/offers.html", page=page, status=status, term=term)


@bp.route("/annonces/<int:oid>", methods=["POST"])
@perm_required("offers")
def offer_action(oid):
    o = db.session.get(Offer, oid) or abort(404)
    action, reason = request.form.get("action"), txt("reason", 500)
    if action == "approve":
        o.status, o.published_at, o.refusal_reason = "active", o.published_at or utcnow(), None
    elif action in ("refuse", "suspend"):
        if not reason:
            flash("Un motif est obligatoire.", "danger")
            return redirect(request.referrer or url_for("admin.offers"))
        o.status, o.refusal_reason = ("refused" if action == "refuse" else "suspended"), reason
    elif action == "feature":
        o.featured = not o.featured
    elif action == "reactivate" and o.status == "suspended":
        o.status = "active"
    audit.log(f"offer.admin_{action}", o, reason=reason)
    if action in ("approve", "refuse", "suspend"):
        notify(o.vendor.user, "offer_status", f"Offre « {o.title} » : {o.status_label.lower()}", reason, "/pro/offres",
               critical=action != "approve", dedupe_key=f"offer_status:{o.id}:{o.status}:{int(utcnow().timestamp())}")
    db.session.commit()
    flash("Annonce mise à jour (l'historique des commandes est conservé).", "success")
    return redirect(request.referrer or url_for("admin.offers"))


# ------------------------------------------------------------------ commandes, devis, transactions
@bp.route("/commandes")
@perm_required("orders_view")
def orders():
    status, term = request.args.get("statut", ""), (request.args.get("q") or "").strip()
    q = Order.query
    if status:
        q = q.filter_by(status=status)
    if term:
        q = q.filter(Order.reference.ilike(f"%{term}%"))
    page = paginate(q.order_by(Order.created_at.desc()), 20)
    return render_template("admin/orders.html", page=page, status=status, term=term)


@bp.route("/commandes/<int:oid>", methods=["GET", "POST"])
@perm_required("orders_view")
def order_detail(oid):
    o = db.session.get(Order, oid) or abort(404)
    if request.method == "POST":
        if not current_user.has_perm("refunds"):
            abort(403)
        try:
            if request.form.get("action") == "cancel":
                full = flag("full_refund")
                manual = num("amount", None, 0) if request.form.get("amount") else None
                res = order_service.cancel(o, by="admin", reason=txt("reason", 500) or "Annulation administrative",
                                           admin_full_refund=full, manual_amount=manual, actor=current_user)
                db.session.commit()
                flash(f"Commande annulée. Remboursement : {fmt_xof(res['refunded'])}.", "success")
            elif request.form.get("action") == "complete":
                order_service.complete(o)
                audit.log("order.admin_complete", o)
                db.session.commit()
                flash("Commande clôturée.", "success")
        except order_service.OrderError as e:
            flash(str(e), "danger")
        return redirect(url_for("admin.order_detail", oid=o.id))
    quote = finance.refund_quote(o) if o.status not in ("cancelled", "completed") else None
    return render_template("admin/order_detail.html", o=o, quote=quote)


@bp.route("/devis")
@perm_required("orders_view")
def quotes():
    status = request.args.get("statut", "")
    q = QuoteRequest.query
    if status:
        q = q.filter_by(status=status)
    return render_template("admin/quotes.html", page=paginate(q.order_by(QuoteRequest.created_at.desc()), 20), status=status)


@bp.route("/transactions")
@perm_required("finance")
def transactions():
    status = request.args.get("statut", "")
    q = Payment.query
    if status:
        q = q.filter_by(status=status)
    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Date", "Réf. externe", "Commande", "Type", "Méthode", "Statut", "Brut", "Commission", "Net prestataire", "Remboursé"])
        for p in q.order_by(Payment.id.desc()).all():
            w.writerow([p.created_at.strftime("%Y-%m-%d %H:%M"), p.external_ref, p.order.reference, p.kind, p.method, p.status,
                        p.amount_xof, p.commission_xof, p.net_vendor_xof, p.refunded_xof])
        audit.log("export.transactions")
        db.session.commit()
        return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=transactions.csv"})
    page = paginate(q.order_by(Payment.id.desc()), 25)
    refunds = Refund.query.order_by(Refund.id.desc()).limit(15).all()
    return render_template("admin/transactions.html", page=page, status=status, refunds=refunds,
                           webhooks=WebhookEvent.query.order_by(WebhookEvent.id.desc()).limit(10).all())


@bp.route("/reversements", methods=["GET", "POST"])
@perm_required("payouts")
def payouts():
    if request.method == "POST":
        p = db.session.get(Payout, num("payout_id", 0)) or abort(404)
        action, note = request.form.get("action"), txt("note", 500)
        flow = {"review": ("requested", "in_review"), "approve": ("in_review", "approved"), "transfer": ("approved", "transferring"),
                "paid": ("transferring", "paid")}
        if action in flow:
            frm, to = flow[action]
            if p.status != frm and not (action == "approve" and p.status == "requested"):
                flash("Transition de statut invalide.", "danger")
            else:
                p.status = to
                if to == "paid":
                    p.processed_at, p.processed_by = utcnow(), current_user.id
                    p.external_ref = txt("external_ref", 80) or p.external_ref
                audit.log(f"payout.{action}", p, amount=p.amount_xof, vendor=p.vendor.business_name)
                notify(p.vendor.user, "payout", f"Retrait {p.reference} : {p.status_label.lower()}", fmt_xof(p.amount_xof),
                       "/pro/revenus", dedupe_key=f"payout:{p.id}:{to}")
        elif action == "reject" and p.status in ("requested", "in_review", "approved"):
            p.status, p.note, p.processed_at, p.processed_by = "rejected", note or "Rejeté", utcnow(), current_user.id
            db.session.add(LedgerEntry(vendor_id=p.vendor_id, payout_id=p.id, kind="adjustment", amount_xof=p.amount_xof,
                                       available=True, note=f"Retrait {p.reference} rejeté : solde recrédité"))
            audit.log("payout.reject", p, note=note)
            notify(p.vendor.user, "payout", f"Retrait {p.reference} rejeté", note, "/pro/revenus", critical=True,
                   dedupe_key=f"payout:{p.id}:rejected")
        db.session.commit()
        return redirect(url_for("admin.payouts"))
    status = request.args.get("statut", "")
    q = Payout.query
    if status:
        q = q.filter_by(status=status)
    return render_template("admin/payouts.html", page=paginate(q.order_by(Payout.id.desc()), 20), status=status)


@bp.route("/commissions", methods=["GET", "POST"])
@perm_required("commissions")
def commissions():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "default":
            bp_ = pct_bp("rate_pct")
            if bp_ is not None:
                settings.set_("commission_default_bp", bp_)
                audit.log("commission.default", None, target_type="Setting", bp=bp_)
        elif action == "rule":
            scope, ref, bp_ = txt("scope", 10), txt("ref", 40), pct_bp("rate_pct")
            if scope in ("category", "vendor", "offer_type", "global") and bp_ is not None:
                r = CommissionRule(scope=scope, ref=ref or None, rate_bp=bp_, valid_from=dt("valid_from"), valid_to=dt("valid_to"),
                                   note=txt("note", 200))
                db.session.add(r)
                audit.log("commission.rule", None, target_type="CommissionRule", scope=scope, ref=ref, bp=bp_)
        elif action == "toggle":
            r = db.session.get(CommissionRule, num("rule_id", 0)) or abort(404)
            r.active = not r.active
            audit.log("commission.rule_toggle", r, active=r.active)
        db.session.commit()
        flash("Règles de commission mises à jour. Les commandes existantes conservent leur taux figé.", "success")
        return redirect(url_for("admin.commissions"))
    return render_template("admin/commissions.html", rules=CommissionRule.query.order_by(CommissionRule.id.desc()).all(),
                           default_bp=settings.get_int("commission_default_bp", 1000),
                           categories=Category.query.order_by(Category.name).all(), vendors=VendorProfile.query.order_by(VendorProfile.business_name).all())


# ------------------------------------------------------------------ support
@bp.route("/tickets")
@perm_required("support")
def tickets():
    status = request.args.get("statut", "")
    q = Ticket.query
    if status:
        q = q.filter_by(status=status)
    else:
        q = q.filter(Ticket.status.notin_(("closed",)))
    return render_template("admin/tickets.html", page=paginate(q.order_by(Ticket.updated_at.desc()), 20), status=status)


@bp.route("/tickets/<int:tid>", methods=["GET", "POST"])
@perm_required("support")
def ticket_detail(tid):
    t = db.session.get(Ticket, tid) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        body = txt("body", 4000)
        if action == "reply" and body:
            internal = flag("internal")
            t.messages.append(TicketMessage(author_id=current_user.id, body=body, internal=internal))
            if not internal:
                if t.status == "open":
                    t.status = "in_progress"
                notify(t.author, "ticket_reply", f"Réponse du support — {t.reference}", body[:140], f"/compte/support/{t.id}",
                       dedupe_key=f"tkreply:{t.id}:{len(t.messages)}")
        elif action == "status" and request.form.get("status") in ("open", "in_progress", "awaiting_user", "solution_proposed", "resolved", "closed"):
            t.status = request.form.get("status")
            if txt("resolution", 2000):
                t.resolution = txt("resolution", 2000)
            audit.log("ticket.status", t, status=t.status)
            if t.status == "solution_proposed":
                notify(t.author, "ticket_solution", f"Solution proposée — {t.reference}", t.resolution or "", f"/compte/support/{t.id}",
                       dedupe_key=f"tksol:{t.id}:{int(utcnow().timestamp())}")
        elif action == "assign":
            t.assigned_to = current_user.id
            if t.status == "open":
                t.status = "in_progress"
        t.updated_at = utcnow()
        db.session.commit()
        return redirect(url_for("admin.ticket_detail", tid=t.id))
    conv = None
    if t.order and current_user.has_perm("conversations"):
        conv = Conversation.query.filter_by(context_type="order", context_id=t.order_id).first()
    return render_template("admin/ticket_detail.html", t=t, conv=conv)


@bp.route("/messages-contact", methods=["GET", "POST"])
@perm_required("support")
def contact_messages():
    if request.method == "POST":
        m = db.session.get(ContactMessage, num("id", 0)) or abort(404)
        m.status = "handled" if m.status == "new" else "new"
        db.session.commit()
        return redirect(url_for("admin.contact_messages"))
    return render_template("admin/contact_messages.html", page=paginate(ContactMessage.query.order_by(ContactMessage.id.desc()), 20))


@bp.route("/demandes-rgpd", methods=["GET", "POST"])
@perm_required("support")
def data_requests():
    if request.method == "POST":
        r = db.session.get(AccountRequest, num("id", 0)) or abort(404)
        r.status = "done"
        audit.log("account_request.done", r, kind=r.kind, user=r.user_id)
        db.session.commit()
        return redirect(url_for("admin.data_requests"))
    return render_template("admin/data_requests.html", rows=AccountRequest.query.order_by(AccountRequest.id.desc()).limit(100).all())


# ------------------------------------------------------------------ avis
@bp.route("/avis", methods=["GET", "POST"])
@perm_required("reviews")
def reviews():
    if request.method == "POST":
        rv = db.session.get(Review, num("review_id", 0)) or abort(404)
        action, reason = request.form.get("action"), txt("reason", 250)
        if action == "hide":
            if not reason:
                flash("Un motif est obligatoire pour masquer un avis.", "danger")
                return redirect(url_for("admin.reviews"))
            rv.status, rv.hidden_reason = "hidden", reason
        elif action == "show":
            rv.status, rv.hidden_reason = "published", None
        elif action == "dismiss":
            rv.reported = False
        audit.log(f"review.{action}", rv, reason=reason)
        db.session.commit()
        return redirect(url_for("admin.reviews", f=request.args.get("f", "")))
    f = request.args.get("f", "reported")
    q = Review.query
    if f == "reported":
        q = q.filter_by(reported=True)
    elif f == "hidden":
        q = q.filter_by(status="hidden")
    return render_template("admin/reviews.html", page=paginate(q.order_by(Review.created_at.desc()), 15), f=f)


# ------------------------------------------------------------------ contenus
@bp.route("/contenus", methods=["GET", "POST"])
@perm_required("content")
def content():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "setting":
            for key in ("hero_title", "hero_subtitle", "support_hours", "support_email", "support_phone", "company_legal", "launch_zone_note"):
                if key in request.form:
                    settings.set_(key, request.form.get(key, "")[:1000])
            audit.log("settings.content")
        elif action == "banner":
            db.session.add(Banner(text=txt("text", 255), link=txt("link", 255), link_label=txt("link_label", 60), active=True,
                                  position=num("position", 0)))
        elif action == "banner_toggle":
            b = db.session.get(Banner, num("id", 0)) or abort(404)
            b.active = not b.active
        elif action == "banner_delete":
            b = db.session.get(Banner, num("id", 0)) or abort(404)
            db.session.delete(b)
        db.session.commit()
        flash("Contenu mis à jour.", "success")
        return redirect(url_for("admin.content"))
    return render_template("admin/content.html", pages=Page.query.order_by(Page.slug).all(),
                           articles=HelpArticle.query.order_by(HelpArticle.section, HelpArticle.position).all(),
                           banners=Banner.query.order_by(Banner.position).all())


@bp.route("/contenus/page/<slug>", methods=["GET", "POST"])
@perm_required("content")
def page_edit(slug):
    p = Page.query.filter_by(slug=slug).first() or (Page(slug=slug, title=slug.replace("-", " ").title(), body="") if slug != "nouvelle" else Page(slug="", title="", body=""))
    if request.method == "POST":
        new_slug = slugify(txt("slug", 80) or txt("title", 80), 80)
        p.slug, p.title, p.body, p.published = new_slug, txt("title", 200), request.form.get("body", "")[:60000], flag("published")
        if not p.title or not p.body:
            flash("Titre et contenu obligatoires.", "danger")
        else:
            db.session.add(p)
            audit.log("page.edit", None, target_type="Page", slug=new_slug)
            db.session.commit()
            flash("Page enregistrée.", "success")
            return redirect(url_for("admin.content"))
    return render_template("admin/page_edit.html", p=p)


@bp.route("/contenus/aide/<int:aid>", methods=["GET", "POST"])
@bp.route("/contenus/aide/nouveau", methods=["GET", "POST"])
@perm_required("content")
def help_edit(aid=None):
    a = db.session.get(HelpArticle, aid) if aid else HelpArticle(audience="all", published=True, title="", body="")
    if a is None:
        abort(404)
    if request.method == "POST":
        if request.form.get("action") == "delete" and aid:
            db.session.delete(a)
            db.session.commit()
            return redirect(url_for("admin.content"))
        a.title, a.section, a.body = txt("title", 200), txt("section", 80), request.form.get("body", "")[:20000]
        a.audience = request.form.get("audience") if request.form.get("audience") in ("buyer", "vendor", "all") else "all"
        a.position, a.published = num("position", 0), flag("published")
        a.slug = a.slug or slugify(a.title, 100)
        if not a.title or not a.body:
            flash("Titre et contenu obligatoires.", "danger")
        else:
            db.session.add(a)
            db.session.commit()
            flash("Article enregistré.", "success")
            return redirect(url_for("admin.content"))
    return render_template("admin/help_edit.html", a=a)


@bp.route("/parametres", methods=["GET", "POST"])
@perm_required("commissions")
def platform_settings():
    keys = [("hold_minutes", "Blocage temporaire pendant le paiement (minutes)"),
            ("quote_order_hold_hours", "Blocage après acceptation d'un devis (heures)"),
            ("max_buyer_counters", "Contre-propositions acheteur max. par demande"),
            ("payout_delay_days", "Délai avant disponibilité des fonds après clôture (jours)"),
            ("payout_min_xof", "Retrait minimum (FCFA)"),
            ("quote_default_validity_days", "Validité par défaut d'un devis (jours)"),
            ("balance_due_days_before", "Solde exigible (jours avant l'événement)")]
    if request.method == "POST":
        for k, _ in keys:
            val = num(k, None, 0)
            if val is not None:
                settings.set_(k, val)
        if request.form.get("confirm_rule") in ("deposit", "vendor_and_deposit"):
            settings.set_("confirm_rule", request.form["confirm_rule"])
        settings.set_("moderate_offers", "1" if flag("moderate_offers") else "0")
        audit.log("settings.platform")
        db.session.commit()
        flash("Paramètres enregistrés.", "success")
        return redirect(url_for("admin.platform_settings"))
    return render_template("admin/settings.html", keys=keys, values={k: settings.get(k) for k, _ in keys},
                           confirm_rule=settings.get("confirm_rule"), moderate=settings.get_bool("moderate_offers", True),
                           provider=current_app.config["PAYMENT_PROVIDER"], smtp=bool(current_app.config["SMTP_HOST"]))


# ------------------------------------------------------------------ audit, outbox, rapports
@bp.route("/audit")
@perm_required("audit")
def audit_log():
    action, actor = (request.args.get("action") or "").strip(), request.args.get("acteur", type=int)
    q = AuditLog.query
    if action:
        q = q.filter(AuditLog.action.like(f"{action}%"))
    if actor:
        q = q.filter_by(actor_id=actor)
    return render_template("admin/audit.html", page=paginate(q.order_by(AuditLog.id.desc()), 30), action=action)


@bp.route("/outbox")
@perm_required("audit")
def outbox():
    return render_template("admin/outbox.html", mails=EmailOutbox.query.order_by(EmailOutbox.id.desc()).limit(50).all(),
                           sms=SmsOutbox.query.order_by(SmsOutbox.id.desc()).limit(20).all())


@bp.route("/rapports")
@perm_required("reports")
def reports():
    kind = request.args.get("export")
    if kind in ("orders", "users", "vendors"):
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        if kind == "orders":
            w.writerow(["Référence", "Date", "Acheteur", "Prestataire", "Statut", "Total", "Payé", "Remboursé", "Commission"])
            for o in Order.query.order_by(Order.id):
                w.writerow([o.reference, o.created_at.date(), o.buyer.email, o.vendor.business_name, o.status, o.total_xof, o.paid_xof, o.refunded_xof, o.commission_xof])
        elif kind == "vendors":
            w.writerow(["Nom commercial", "E-mail", "Statut vérification", "Ville", "Offres actives"])
            for v in VendorProfile.query.order_by(VendorProfile.id):
                w.writerow([v.business_name, v.user.email, v.verification_status, v.city, v.offers.filter_by(status="active").count()])
        else:
            if not current_user.has_perm("support"):
                abort(403)
            w.writerow(["Id", "Rôle", "Nom", "E-mail", "Statut", "Créé le"])
            for u in User.query.order_by(User.id):
                w.writerow([u.id, u.role, u.full_name, u.email, u.status, u.created_at.date()])
        audit.log(f"export.{kind}")
        db.session.commit()
        return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=eventystore-{kind}.csv"})
    return render_template("admin/reports.html")


@bp.route("/donnees-demo/purger", methods=["POST"])
@perm_required("audit")
def purge_demo():
    from flask_login import logout_user
    from ..seed import purge_demo as _purge
    was_demo_admin = bool(current_user.is_demo)
    actor_id = None if was_demo_admin else current_user.id
    if was_demo_admin:  # le compte de démonstration qui exécute l'action sera supprimé : on le déconnecte d'abord
        logout_user()
    n = _purge()
    db.session.add(AuditLog(actor_id=actor_id, action="demo.purge", target_type="System", meta={"rows": n}))
    db.session.commit()
    flash(f"Données de démonstration supprimées ({n} enregistrements)."
          + (" Créez un administrateur avec « flask create-admin »." if was_demo_admin else ""), "success")
    return redirect(url_for("public.home" if was_demo_admin else "admin.dashboard"))
