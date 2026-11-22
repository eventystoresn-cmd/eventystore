"""Tests fonctionnels bout en bout (SQLite mémoire, paiement sandbox)."""
import json
import re
from datetime import date, timedelta

import pytest

from eventystore import create_app, seed
from eventystore.config import TestingConfig
from eventystore.extensions import db
from eventystore.models import (AvailabilityBlock, Event, LedgerEntry, Notification, Offer, Order, Payment, Payout,
                                Quote, QuoteRequest, User, VendorProfile)
from eventystore.services import availability, finance, jobs, orders as order_service, payments

PW = "Demo1234"


@pytest.fixture()
def app():
    app = create_app(TestingConfig)
    with app.app_context():
        db.create_all()
        seed.seed_core()
        seed.seed_demo()
        yield app
        db.session.remove()
        db.drop_all()


def login(client, email, pw=PW):
    r = client.post("/connexion", data={"identifier": email, "password": pw}, follow_redirects=False)
    assert r.status_code == 302, r.data[:300]


@pytest.fixture()
def clients(app):
    return {k: app.test_client() for k in ("buyer", "vendor", "admin", "anon")}


def test_public_pages_render(clients):
    c = clients["anon"]
    for url in ["/", "/categories", "/recherche", "/recherche?q=traiteur&place=Dakar", "/comment-ca-marche",
                "/devenir-prestataire", "/a-propos", "/faq", "/aide", "/contact", "/connexion", "/inscription",
                "/inscription-prestataire", "/p/conditions-generales", "/sitemap.xml", "/robots.txt", "/comparer",
                "/api/v1/categories", "/api/v1/suggest?q=tra", "/mot-de-passe-oublie"]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
    o = Offer.query.filter_by(status="active").first()
    assert c.get(f"/offres/{o.id}-{o.slug}").status_code == 200
    assert c.get(f"/prestataires/{o.vendor.slug}").status_code == 200
    assert c.get("/espace/").status_code == 302  # accès protégé
    assert c.get("/admin/").status_code == 302


def test_role_isolation(clients):
    login(clients["buyer"], "acheteur@demo.eventystore.sn")
    assert clients["buyer"].get("/admin/").status_code == 403
    assert clients["buyer"].get("/pro/").status_code == 403
    login(clients["vendor"], "prestataire1@demo.eventystore.sn")
    assert clients["vendor"].get("/espace/").status_code == 403


def test_dashboards_and_pages(clients):
    login(clients["buyer"], "acheteur@demo.eventystore.sn")
    for u in ["/espace/", "/espace/evenements", "/espace/evenements/nouveau", "/espace/devis", "/espace/commandes",
              "/espace/panier", "/espace/paiements", "/espace/favoris", "/compte/parametres", "/compte/support",
              "/compte/notifications", "/messages/", "/espace/devis/comparer"]:
        assert clients["buyer"].get(u).status_code == 200, u
    login(clients["vendor"], "prestataire1@demo.eventystore.sn")
    for u in ["/pro/", "/pro/profil", "/pro/verification", "/pro/offres", "/pro/offres/nouvelle", "/pro/calendrier",
              "/pro/devis", "/pro/commandes", "/pro/revenus", "/pro/promotions", "/pro/avis"]:
        assert clients["vendor"].get(u).status_code == 200, u
    login(clients["admin"], "admin@demo.eventystore.sn")
    for u in ["/admin/", "/admin/utilisateurs", "/admin/equipe", "/admin/prestataires?tous=1", "/admin/categories",
              "/admin/annonces?statut=all", "/admin/commandes", "/admin/devis", "/admin/transactions", "/admin/reversements",
              "/admin/commissions", "/admin/tickets", "/admin/messages-contact", "/admin/demandes-rgpd", "/admin/avis",
              "/admin/contenus", "/admin/parametres", "/admin/audit", "/admin/outbox", "/admin/rapports",
              "/admin/contenus/page/conditions-generales", "/admin/contenus/aide/nouveau"]:
        assert clients["admin"].get(u).status_code == 200, u


def _event(buyer_client, days=40):
    d = (date.today() + timedelta(days=days)).isoformat()
    r = buyer_client.post("/espace/evenements/nouveau", data={"title": "Test", "start_date": d, "end_date": d,
                                                                "city": "Dakar", "guests": "100", "budget_xof": "2000000"})
    assert r.status_code == 302
    return Event.query.order_by(Event.id.desc()).first(), d


def _sandbox_pay(client, order, choice="deposit"):
    r = client.post(f"/paiement/{order.id}", data={"choice": choice, "method": "sandbox"})
    assert r.status_code == 302 and "/paiement/sandbox/" in r.location
    ref = r.location.rsplit("/", 1)[-1]
    r = client.post(f"/paiement/sandbox/{ref}", data={"result": "success"})
    assert r.status_code == 302
    return Payment.query.filter_by(external_ref=ref).first()


def test_full_booking_flow_cart_to_payout(app, clients):
    b, v, a = clients["buyer"], clients["vendor"], clients["admin"]
    login(b, "acheteur@demo.eventystore.sn")
    login(v, "prestataire1@demo.eventystore.sn")
    login(a, "admin@demo.eventystore.sn")
    ev, d = _event(b)
    offer = Offer.query.filter_by(title="Traiteur complet — Teranga Délices").first() or Offer.query.filter_by(offer_type="service", price_unit="person").first()
    r = b.post("/espace/panier/ajouter", data={"offer_id": offer.id, "event_id": ev.id, "start": d, "end": d, "quantity": "100"})
    assert r.status_code == 302
    assert b.get("/espace/panier").status_code == 200
    r = b.post("/espace/panier/valider", data={"accept_conditions": "1"})
    assert r.status_code == 302 and "/paiement/" in r.location, r.location
    order = Order.query.order_by(Order.id.desc()).first()
    # prix recalculé côté serveur : 100 pers. avec palier dégressif
    assert order.total_xof == 100 * (offer.price_xof - 500)
    assert order.status == "pending_payment" and order.hold_expires_at
    # le créneau est bloqué pendant le paiement (service à capacité 1)
    assert not availability.check(offer, date.fromisoformat(d), date.fromisoformat(d)).ok
    pay = _sandbox_pay(b, order)
    db.session.refresh(order)
    assert pay.status == "succeeded" and order.status == "pending_confirmation"
    assert order.paid_xof == order.deposit_xof
    assert pay.commission_xof == round(pay.amount_xof * order.commission_bp / 10000)
    # le prestataire confirme
    r = v.post(f"/pro/commandes/{order.id}/confirm")
    db.session.refresh(order)
    assert order.status == "confirmed"
    # solde
    _sandbox_pay(b, order, "full")
    db.session.refresh(order)
    assert order.remaining_xof == 0
    # clôture + libération des fonds
    v.post(f"/pro/commandes/{order.id}/complete")
    db.session.refresh(order)
    assert order.status == "completed"
    order.completed_at = order.completed_at - timedelta(days=5)
    db.session.commit()
    jobs.run_all()
    vendor = order.vendor
    bal = finance.vendor_balance(vendor.id)
    assert bal["available"] == order.net_vendor_xof > 0
    # avis après commande terminée uniquement
    r = b.post(f"/espace/commandes/{order.id}/avis", data={"rating": "5", "comment": "Excellent"})
    assert r.status_code == 302 and order.review is not None or db.session.get(Order, order.id).review
    # retrait
    v.post("/pro/revenus", data={"action": "payout_settings", "method": "wave", "account": "771234567", "holder": "Test", "password": PW})
    r = v.post("/pro/revenus", data={"action": "payout_request", "amount": str(bal["available"])})
    p = Payout.query.first()
    assert p and p.amount_xof == bal["available"]
    assert finance.vendor_balance(vendor.id)["available"] == 0
    a.post("/admin/reversements", data={"payout_id": p.id, "action": "reject", "note": "test"})
    assert finance.vendor_balance(vendor.id)["available"] == bal["available"]  # recrédité


def test_quote_negotiation_flow(app, clients):
    b, v = clients["buyer"], clients["vendor"]
    login(b, "acheteur@demo.eventystore.sn")
    login(v, "prestataire1@demo.eventystore.sn")
    ev, d = _event(b, 50)
    offer = Offer.query.filter_by(price_model="quote").first()
    r = b.post("/espace/devis/nouveau", data={"offer_id": offer.id, "event_id": ev.id, "start_date": d, "end_date": d,
                                                "need": "Décoration complète pour 150 invités, thème or et blanc.",
                                                "contact_consent": "1", "quantity": "1"})
    assert r.status_code == 302, r.data[:400]
    req = QuoteRequest.query.first()
    assert req.status == "open"
    # le prestataire répond
    valid = (date.today() + timedelta(days=7)).isoformat()
    r = v.post(f"/pro/devis/{req.id}/envoyer", data={"line_label": ["Décoration"], "line_qty": ["1"], "line_unit": ["forfait"],
                                                    "line_price": ["500000"], "line_discount": ["0"], "line_kind": ["item"],
                                                    "valid_until": valid, "deposit_percent": "30", "balance_days": "3", "send": "send"})
    db.session.refresh(req)
    assert req.status == "quoted" and len(req.quotes) == 1 and req.quotes[0].total_xof == 500000
    # contre-proposition -> nouvelle version, l'ancienne n'est pas écrasée
    r = b.post(f"/espace/devis/{req.id}/contre-proposition", data={"line_label": ["Décoration"], "line_qty": ["1"], "line_unit": ["forfait"],
                                                                  "line_price": ["420000"], "line_discount": ["0"], "line_kind": ["item"], "note": "Budget"})
    db.session.refresh(req)
    assert len(req.quotes) == 2 and req.quotes[0].total_xof == 500000 and req.quotes[1].total_xof == 420000
    assert req.quotes[0].status == "superseded" and req.counter_count == 1
    # l'acheteur ne peut pas accepter sa propre contre-proposition
    b.post(f"/espace/devis/{req.id}/versions/{req.quotes[1].id}/accepter")
    db.session.refresh(req)
    assert req.status == "negotiating"
    # le prestataire accepte
    v.post(f"/pro/devis/{req.id}/versions/{req.quotes[1].id}/accept")
    db.session.refresh(req)
    assert req.status == "accepted" and req.accepted_quote.total_xof == 420000
    # réservation depuis le devis accepté
    r = b.post(f"/espace/devis/{req.id}/reserver", data={"accept_conditions": "1"})
    assert r.status_code == 302 and "/paiement/" in r.location
    order = Order.query.filter_by(quote_id=req.accepted_quote.id).first()
    assert order.total_xof == 420000 and order.deposit_xof == 126000


def test_expired_quote_cannot_be_accepted(app, clients):
    from eventystore.services import quotes as qs
    b, v = clients["buyer"], clients["vendor"]
    login(b, "acheteur@demo.eventystore.sn")
    ev, d = _event(b, 30)
    offer = Offer.query.filter_by(price_model="quote").first()
    b.post("/espace/devis/nouveau", data={"offer_id": offer.id, "event_id": ev.id, "start_date": d, "need": "x" * 30, "contact_consent": "1"})
    req = QuoteRequest.query.first()
    vendor_user = req.vendor.user
    q = qs.build_quote(req, vendor_user, "vendor", [{"kind": "item", "label": "A", "quantity": 1, "unit": "", "unit_price_xof": 1000,
                                                    "discount_xof": 0, "total_xof": 1000}], valid_until=date.today() + timedelta(days=1),
                       deposit_percent=30, balance_days=3)
    q.valid_until = date.today() - timedelta(days=1)
    db.session.commit()
    with pytest.raises(qs.QuoteError):
        qs.accept(q, req.buyer)
    assert q.status == "expired"


def test_stock_no_overbooking(app):
    offer = Offer.query.filter_by(offer_type="rental").first()
    start = date.today() + timedelta(days=20)
    assert availability.check(offer, start, start, 400).ok
    buyer = User.query.filter_by(role="buyer").first()
    from eventystore.models import CartItem
    ev = Event.query.first()
    db.session.add(CartItem(user_id=buyer.id, offer_id=offer.id, event_id=ev.id, start_date=start, end_date=start, quantity=400))
    db.session.commit()
    items = CartItem.query.filter_by(user_id=buyer.id).all()
    order_service.create_orders_from_cart(buyer, items)
    db.session.commit()
    assert not availability.check(offer, start, start, 200).ok   # 400 + 200 > 500
    assert availability.check(offer, start, start, 100).ok       # 400 + 100 <= 500


def test_vendor_block_and_hold_expiry(app):
    offer = Offer.query.filter_by(price_unit="person").first()
    start = date.today() + timedelta(days=15)
    db.session.add(AvailabilityBlock(vendor_id=offer.vendor_id, start_date=start, end_date=start + timedelta(days=1), reason="congé"))
    db.session.commit()
    assert not availability.check(offer, start, start).ok
    assert availability.check(offer, start + timedelta(days=5), start + timedelta(days=5)).ok


def test_payment_requires_server_confirmation_and_signature(app, clients):
    b = clients["buyer"]
    login(b, "acheteur@demo.eventystore.sn")
    ev, d = _event(b, 45)
    offer = Offer.query.filter_by(price_unit="person").first()
    b.post("/espace/panier/ajouter", data={"offer_id": offer.id, "event_id": ev.id, "start": d, "quantity": "50"})
    b.post("/espace/panier/valider", data={"accept_conditions": "1"})
    order = Order.query.order_by(Order.id.desc()).first()
    r = b.post(f"/paiement/{order.id}", data={"choice": "deposit", "method": "sandbox"})
    ref = r.location.rsplit("/", 1)[-1]
    # la page de retour ne finalise rien
    b.get(f"/paiement/resultat/{ref}")
    assert Payment.query.filter_by(external_ref=ref).first().status == "pending"
    # webhook avec mauvaise signature : refusé
    body = json.dumps({"id": "evt1", "type": "payment.succeeded", "reference": ref, "amount_xof": order.deposit_xof})
    r = b.post("/webhooks/payments/sandbox", data=body, headers={"X-Eventy-Signature": "bad", "Content-Type": "application/json"})
    assert r.status_code == 401
    assert Payment.query.filter_by(external_ref=ref).first().status == "pending"
    # signature valide mais montant incohérent : refusé
    prov = payments.PROVIDERS["sandbox"]
    bad = json.dumps({"id": "evt2", "type": "payment.succeeded", "reference": ref, "amount_xof": 1})
    r = b.post("/webhooks/payments/sandbox", data=bad, headers={"X-Eventy-Signature": prov.sign(bad.encode()), "Content-Type": "application/json"})
    assert r.status_code == 422
    # un autre acheteur ne peut pas payer cette commande
    other = app.test_client()
    other.post("/inscription", data={"first_name": "A", "last_name": "B", "email": "x@y.sn", "phone": "771110000", "password": "Abcdef12",
                                     "password2": "Abcdef12", "accept_terms": "1"})
    assert other.get(f"/paiement/{order.id}").status_code == 404


def test_webhook_idempotent_and_valid(app, clients):
    b = clients["buyer"]
    login(b, "acheteur@demo.eventystore.sn")
    ev, d = _event(b, 45)
    offer = Offer.query.filter_by(price_unit="person").first()
    b.post("/espace/panier/ajouter", data={"offer_id": offer.id, "event_id": ev.id, "start": d, "quantity": "50"})
    b.post("/espace/panier/valider", data={"accept_conditions": "1"})
    order = Order.query.order_by(Order.id.desc()).first()
    ref = b.post(f"/paiement/{order.id}", data={"choice": "deposit", "method": "sandbox"}).location.rsplit("/", 1)[-1]
    prov = payments.PROVIDERS["sandbox"]
    body = json.dumps({"id": "evt9", "type": "payment.succeeded", "reference": ref, "amount_xof": order.deposit_xof})
    h = {"X-Eventy-Signature": prov.sign(body.encode()), "Content-Type": "application/json"}
    assert b.post("/webhooks/payments/sandbox", data=body, headers=h).status_code == 200
    assert b.post("/webhooks/payments/sandbox", data=body, headers=h).status_code == 200  # rejeu
    db.session.refresh(order)
    assert order.paid_xof == order.deposit_xof  # compté une seule fois
    assert LedgerEntry.query.filter_by(order_id=order.id, kind="credit").count() == 1


def test_cancellation_policy_and_refund(app, clients):
    b = clients["buyer"]
    login(b, "acheteur@demo.eventystore.sn")
    ev, d = _event(b, 60)
    offer = Offer.query.filter_by(price_unit="person").first()
    b.post("/espace/panier/ajouter", data={"offer_id": offer.id, "event_id": ev.id, "start": d, "quantity": "50"})
    b.post("/espace/panier/valider", data={"accept_conditions": "1"})
    order = Order.query.order_by(Order.id.desc()).first()
    _sandbox_pay(b, order)
    db.session.refresh(order)
    paid = order.paid_xof
    # avant confirmation du prestataire : remboursement intégral
    b.post(f"/espace/commandes/{order.id}/annuler", data={"confirm": "1"})
    db.session.refresh(order)
    assert order.status == "cancelled" and order.refunded_xof == paid
    assert finance.refund_percent("moderate", 20) == 100 and finance.refund_percent("moderate", 10) == 50 and finance.refund_percent("moderate", 3) == 0
    assert finance.refund_percent("strict", 31) == 50 and finance.refund_percent("flexible", 2) == 0
    # le créneau est libéré
    assert availability.check(offer, date.fromisoformat(d), date.fromisoformat(d)).ok


def test_registration_login_reset_and_bruteforce(app):
    c = app.test_client()
    data = {"first_name": "Moussa", "last_name": "Fall", "email": "moussa@example.com", "phone": "77 555 00 11",
            "password": "Secret123", "password2": "Secret123", "accept_terms": "1"}
    assert c.post("/inscription", data={**data, "password": "short", "password2": "short"}).status_code == 200  # mot de passe faible
    assert User.query.filter_by(email="moussa@example.com").first() is None
    assert c.post("/inscription", data=data).status_code == 302
    u = User.query.filter_by(email="moussa@example.com").first()
    assert u.phone == "+221775550011" and not u.email_verified and u.password_hash != "Secret123"
    from eventystore.blueprints.auth import _ser
    token = _ser("verify-email").dumps({"uid": u.id, "email": u.email})
    c.get(f"/verifier-email/{token}")
    db.session.refresh(u)
    assert u.email_verified
    # doublon refusé
    assert app.test_client().post("/inscription", data=data).status_code == 200
    # connexion par téléphone
    c2 = app.test_client()
    assert c2.post("/connexion", data={"identifier": "775550011", "password": "Secret123"}).status_code == 302
    # force brute
    c3 = app.test_client()
    codes = [c3.post("/connexion", data={"identifier": "moussa@example.com", "password": "bad"}).status_code for _ in range(8)]
    assert 429 in codes
    # open redirect bloqué
    c4 = app.test_client()
    r = c4.post("/connexion", data={"identifier": "acheteur@demo.eventystore.sn", "password": PW, "next": "//evil.com"})
    assert "evil.com" not in r.location


def test_vendor_verification_workflow_and_private_docs(app, clients):
    c = app.test_client()
    c.post("/inscription-prestataire", data={"first_name": "Ibou", "last_name": "Ndiaye", "email": "ibou@example.com", "phone": "770001122",
                                             "password": "Secret123", "password2": "Secret123", "accept_terms": "1", "business_name": "Ibou Events",
                                             "legal_status": "independant", "city": "Dakar", "categories": ["1"], "areas": ["Dakar"]})
    vp = VendorProfile.query.filter_by(business_name="Ibou Events").first()
    assert vp and vp.verification_status == "draft"
    # dossier incomplet (AC-11)
    c.post("/pro/verification", data={"action": "submit"})
    db.session.refresh(vp)
    assert vp.verification_status == "draft"
    c.post("/pro/profil", data={"business_name": "Ibou Events", "description": "Organisation et animation d'événements à Dakar.", "city": "Dakar", "areas": ["Dakar"], "contact_phone": "770001122"})
    import io
    c.post("/pro/verification", data={"action": "upload", "doc_type": "identity", "file": (io.BytesIO(b"%PDF-1.4 test"), "cni.pdf")}, content_type="multipart/form-data")
    # fichier non-PDF déguisé refusé
    c.post("/pro/verification", data={"action": "upload", "doc_type": "other", "file": (io.BytesIO(b"MZ exe"), "virus.pdf")}, content_type="multipart/form-data")
    db.session.refresh(vp)
    assert len(vp.documents) == 1
    raw = open(str(app.config["PRIVATE_DIR"] / vp.documents[0].stored_name), "rb").read()
    assert b"%PDF" not in raw  # chiffré au repos
    c.post("/pro/verification", data={"action": "submit"})
    db.session.refresh(vp)
    assert vp.verification_status == "submitted"
    # un prestataire non validé ne peut pas publier ; les docs ne sont jamais publics
    assert app.test_client().get(f"/prestataires/{vp.slug}").status_code == 404
    a = clients["admin"]
    login(a, "admin@demo.eventystore.sn")
    r = a.get(f"/admin/documents/{vp.documents[0].id}")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert a.post(f"/admin/prestataires/{vp.id}", data={"action": "refuse"}).status_code == 302
    db.session.refresh(vp)
    assert vp.verification_status == "submitted"  # motif obligatoire
    a.post(f"/admin/prestataires/{vp.id}", data={"action": "validate", "note": "OK"})
    db.session.refresh(vp)
    assert vp.verification_status == "validated" and vp.verified_badge
    assert app.test_client().get(f"/prestataires/{vp.slug}").status_code == 200
    from eventystore.models import AuditLog
    assert AuditLog.query.filter_by(action="vendor_doc.access").count() == 1


def test_csrf_enforced_when_enabled(app):
    cfg = type("C", (TestingConfig,), {"WTF_CSRF_ENABLED": True})
    a2 = create_app(cfg)
    with a2.app_context():
        db.create_all()
        r = a2.test_client().post("/connexion", data={"identifier": "x", "password": "y"})
        assert r.status_code == 400


def test_search_filters(app):
    from eventystore.services import search
    params = search.parse_params({"q": "traiteur", "place": "Dakar"})
    items, total, *_ = search.search(params)
    assert total >= 1 and all("dakar" in o.areas_text for o in items)
    items, total, *_ = search.search(search.parse_params({"bmin": "1000000"}))
    assert all(o.price_xof >= 1000000 for o in items)
    items, total, *_ = search.search(search.parse_params({"q": "zzzinexistant"}))
    assert total == 0


def test_wave_integration_with_mocked_api(app, clients, monkeypatch):
    import hmac, hashlib, time
    app.config.update(PAYMENT_PROVIDER="wave", WAVE_API_KEY="k", WAVE_WEBHOOK_SECRET="whsec", SITE_URL="https://eventy.test")
    calls = []

    def fake(method, url, headers=None, body=None, timeout=15):
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/v1/checkout/sessions"):
            return 200, {"id": "cos-1", "wave_launch_url": "https://pay.wave.com/c/cos-1"}
        if method == "GET":
            return 200, {"payment_status": "succeeded", "client_reference": calls[0][2]["client_reference"],
                         "amount": str(calls[0][2]["amount"]), "currency": "XOF"}
        return 200, {}
    monkeypatch.setattr(payments, "_http_json", fake)
    b = clients["buyer"]
    login(b, "acheteur@demo.eventystore.sn")
    ev, d = _event(b, 45)
    offer = Offer.query.filter_by(price_unit="person").first()
    b.post("/espace/panier/ajouter", data={"offer_id": offer.id, "event_id": ev.id, "start": d, "quantity": "50"})
    b.post("/espace/panier/valider", data={"accept_conditions": "1"})
    order = Order.query.order_by(Order.id.desc()).first()
    r = b.post(f"/paiement/{order.id}", data={"choice": "deposit", "method": "wave"})
    assert r.location == "https://pay.wave.com/c/cos-1"
    pay = Payment.query.order_by(Payment.id.desc()).first()
    assert pay.provider == "wave" and pay.raw["wave_session_id"] == "cos-1" and calls[0][2]["currency"] == "XOF"
    # webhook : signature invalide refusée, valide acceptée (+ relecture serveur chez Wave)
    body = json.dumps({"id": "AE_1", "type": "checkout.session.completed", "data": {
        "id": "cos-1", "amount": str(pay.amount_xof), "currency": "XOF", "client_reference": pay.external_ref, "payment_status": "succeeded"}})
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec", (ts + body).encode(), hashlib.sha256).hexdigest()
    h = {"Wave-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}
    assert b.post("/webhooks/payments/wave", data=body, headers={**h, "Wave-Signature": f"t={ts},v1=bad"}).status_code == 401
    old = str(int(time.time()) - 3600)
    old_sig = hmac.new(b"whsec", (old + body).encode(), hashlib.sha256).hexdigest()
    assert b.post("/webhooks/payments/wave", data=body, headers={**h, "Wave-Signature": f"t={old},v1={old_sig}"}).status_code == 401  # rejeu
    assert Payment.query.get(pay.id).status == "pending"
    assert b.post("/webhooks/payments/wave", data=body, headers=h).status_code == 200
    db.session.refresh(order)
    assert Payment.query.get(pay.id).status == "succeeded" and order.status == "pending_confirmation"
    # si Wave ne confirme pas côté serveur, rien n'est crédité
    monkeypatch.setattr(payments, "_http_json", lambda *a, **k: (200, {"payment_status": "pending"}))
    p2 = payments.create_payment(order, 1000, "balance", "wave"); db.session.commit()
    body2 = json.dumps({"id": "AE_2", "type": "checkout.session.completed", "data": {"id": "cos-2", "amount": "1000", "currency": "XOF", "client_reference": p2.external_ref}})
    sig2 = hmac.new(b"whsec", (ts + body2).encode(), hashlib.sha256).hexdigest()
    assert b.post("/webhooks/payments/wave", data=body2, headers={"Wave-Signature": f"t={ts},v1={sig2}", "Content-Type": "application/json"}).status_code == 422
    assert Payment.query.get(p2.id).status == "pending"


def test_alternative_dates_when_unavailable(app, clients):
    b = clients["buyer"]
    login(b, "acheteur@demo.eventystore.sn")
    offer = Offer.query.filter_by(price_unit="person").first()
    d = date.today() + timedelta(days=15)
    db.session.add(AvailabilityBlock(vendor_id=offer.vendor_id, start_date=d, end_date=d))
    db.session.commit()
    r = b.get(f"/api/v1/offers/{offer.id}/price?start={d.isoformat()}&quantity=40").get_json()
    assert r["availability"]["status"] == "unavailable"
    assert r["alternatives"] and all(a > d.isoformat() for a in r["alternatives"])


def test_commission_percent_input_and_demo_purge_by_demo_admin(app, clients):
    a = clients["admin"]
    login(a, "admin@demo.eventystore.sn")
    v = VendorProfile.query.first()
    a.post(f"/admin/prestataires/{v.id}", data={"action": "commission", "commission_pct": "7,5"})
    db.session.refresh(v)
    assert v.commission_override_bp == 750
    a.post(f"/admin/prestataires/{v.id}", data={"action": "commission", "commission_pct": "75"})  # hors bornes : ignoré -> None
    db.session.refresh(v)
    assert v.commission_override_bp is None
    r = a.post("/admin/donnees-demo/purger")  # l'admin de démo se supprime lui-même : pas d'erreur 500
    assert r.status_code == 302 and r.location.endswith("/")
    assert User.query.filter_by(is_demo=True).count() == 0
