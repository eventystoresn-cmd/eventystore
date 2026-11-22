"""Envoie des formulaires vides / absurdes sur toutes les routes POST pour chaque rôle : aucune ne doit renvoyer 5xx.
Usage : .venv/bin/python tests/fuzz_post.py"""
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")
from flask import url_for
from eventystore import create_app, seed
from eventystore.config import TestingConfig
from eventystore.extensions import db
from eventystore.models import *  # noqa
from eventystore.services import orders as os_, payments, quotes as qs

app = create_app(TestingConfig)
bad = []
PAYLOADS = [
    {},
    {"title": "", "name": "", "email": "nope", "amount": "abc", "quantity": "-5", "start": "2020-01-01", "start_date": "xx",
     "price_xof": "-1", "rating": "9", "percent": "999", "reason": "x" * 5000, "action": "zzz", "status": "zzz", "uid": "99999",
     "offer_id": "99999", "event_id": "99999", "category_id": "99999", "review_id": "99999", "promo_id": "99999", "id": "99999"},
]
with app.app_context():
    db.create_all(); seed.seed_core(); seed.seed_demo()
    buyer = User.query.filter_by(role="buyer").first()
    ev = Event.query.first()
    off = Offer.query.filter_by(price_unit="person").first()
    d = date.today() + timedelta(days=40)
    ci = CartItem(user_id=buyer.id, offer_id=off.id, event_id=ev.id, start_date=d, end_date=d, quantity=60)
    db.session.add(ci); db.session.commit()
    order = os_.create_orders_from_cart(buyer, [ci])[0]; db.session.commit()
    qoffer = Offer.query.filter_by(price_model="quote").first()
    req = qs.create_request(buyer, qoffer, qoffer.vendor, dict(event_id=ev.id, start_date=d, end_date=d, quantity=1, need="x" * 30, contact_consent=True))
    t = Ticket(reference="TK-1", author_id=buyer.id, order_id=order.id, subject="T", description="d" * 20)
    t.messages.append(TicketMessage(author_id=buyer.id, body="h")); db.session.add(t); db.session.commit()
    conv = __import__("eventystore.services.messaging", fromlist=["x"]).for_order(order); db.session.commit()
    ids = {"offer_id": off.id, "order_id": order.id, "oid": order.id, "req_id": req.id, "event_id": ev.id, "vid": order.vendor_id,
           "uid": buyer.id, "tid": t.id, "ticket_id": t.id, "conv_id": conv.id, "aid": 1, "pay_id": 1, "nid": 1, "doc_id": 1, "item_id": 1,
           "media_id": 1, "block_id": 1, "att_id": 1, "msg_id": 1, "payout_id": 1, "quote_id": 1, "ref": "X", "slug": "conditions-generales",
           "provider": "sandbox", "action": "publish", "token": "x"}
    vendor_user = order.vendor.user
    roles = {"anon": None, "buyer": buyer.email, "vendor": vendor_user.email, "admin": "admin@demo.eventystore.sn"}
    skip = ("auth.logout", "static")
    for role, email in roles.items():
        c = app.test_client()
        if email:
            c.post("/connexion", data={"identifier": email, "password": "Demo1234"})
        for rule in app.url_map.iter_rules():
            if "POST" not in rule.methods or rule.endpoint in skip or rule.endpoint.startswith("static"):
                continue
            args = {a: ids.get(a, 1) for a in rule.arguments}
            if "action" in args and rule.endpoint.startswith("vendor.offer_photo"):
                args["action"] = "cover"
            with app.test_request_context():
                try:
                    url = url_for(rule.endpoint, **args)
                except Exception as ex:  # noqa: BLE001
                    bad.append((role, rule.endpoint, "url_for", str(ex)[:80])); continue
            for payload in PAYLOADS:
                try:
                    r = c.post(url, data=payload)
                except Exception as ex:  # noqa: BLE001
                    bad.append((role, url, "EXCEPTION", repr(ex)[:200])); db.session.rollback(); break
                if r.status_code >= 500:
                    bad.append((role, url, r.status_code)); db.session.rollback()
for b in bad:
    print(b)
print(len(bad), "problème(s)")
