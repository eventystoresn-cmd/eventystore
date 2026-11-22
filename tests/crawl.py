"""Exploration exhaustive : visite toutes les routes GET (avec des ids réels) pour chaque rôle et signale
les erreurs 5xx, les rendus cassés ("None", "Undefined"…) et les liens internes morts.
Usage : .venv/bin/python tests/crawl.py"""
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")
from eventystore import create_app, seed
from eventystore.config import TestingConfig
from eventystore.extensions import db
from eventystore.models import *  # noqa
from eventystore.services import orders as os_, payments, quotes as qs

app = create_app(TestingConfig)
problems = []
with app.app_context():
    db.create_all(); seed.seed_core(); seed.seed_demo()
    PW = "Demo1234"
    buyer = User.query.filter_by(email="acheteur@demo.eventystore.sn").first()
    offers = Offer.query.all()
    ev = Event.query.first()
    # scénario riche : commande payée, devis, ticket, avis…
    d = date.today() + timedelta(days=40)
    off = next(o for o in offers if o.price_unit == "person")
    ci = CartItem(user_id=buyer.id, offer_id=off.id, event_id=ev.id, start_date=d, end_date=d, quantity=60)
    db.session.add(ci); db.session.commit()
    order = os_.create_orders_from_cart(buyer, [ci])[0]; db.session.commit()
    p = payments.create_payment(order, order.deposit_xof, "deposit", "sandbox"); db.session.commit()
    e, ok = payments.sandbox_event(p, True); payments.process_event("sandbox", e, ok)
    os_.confirm(order); db.session.commit()
    qoffer = next(o for o in offers if o.price_model == "quote")
    req = qs.create_request(buyer, qoffer, qoffer.vendor, dict(event_id=ev.id, start_date=d, end_date=d, quantity=1, need="x" * 30, contact_consent=True))
    qs.build_quote(req, qoffer.vendor.user, "vendor", [{"kind": "item", "label": "Déco", "quantity": 1, "unit": "forfait", "unit_price_xof": 300000, "discount_xof": 0, "total_xof": 300000}],
                   valid_until=date.today() + timedelta(days=5), deposit_percent=30, balance_days=3)
    t = Ticket(reference="TK-1", author_id=buyer.id, order_id=order.id, subject="Test", description="d" * 20)
    t.messages.append(TicketMessage(author_id=buyer.id, body="hello")); db.session.add(t)
    order.status = "completed"; order.completed_at = utcnow()
    db.session.add(Review(order_id=order.id, buyer_id=buyer.id, vendor_id=order.vendor_id, offer_id=off.id, rating=5, comment="Super", is_demo=True))
    db.session.commit()
    conv = Conversation.query.first()
    vendor_user = order.vendor.user
    ids = {"offer_id": off.id, "order_id": order.id, "oid": order.id, "req_id": req.id, "event_id": ev.id, "vid": order.vendor_id,
           "uid": buyer.id, "slug": order.vendor.slug, "tid": t.id, "ticket_id": t.id, "conv_id": conv.id if conv else 1,
           "aid": HelpArticle.query.first().id, "pay_id": order.payments[0].id, "nid": 1, "doc_id": 0, "item_id": 0, "media_id": 0,
           "block_id": 0, "att_id": 0, "msg_id": 0, "payout_id": 0, "quote_id": 0, "ref": p.external_ref, "icon": "utensils",
           "rel": "x", "provider": "sandbox", "action": "x", "token": "x"}
    roles = {"anon": None, "buyer": buyer.email, "vendor": vendor_user.email, "admin": "admin@demo.eventystore.sn"}
    skip = ("static", "media.public_file", "auth.logout", "public.dev_outbox")
    for role, email in roles.items():
        c = app.test_client()
        if email:
            c.post("/connexion", data={"identifier": email, "password": PW})
        for rule in app.url_map.iter_rules():
            if "GET" not in rule.methods or rule.endpoint in skip or rule.endpoint.startswith("static"):
                continue
            args = {a: ids.get(a, 1) for a in rule.arguments}
            if rule.endpoint == "public.offer" and "slug" not in args:
                continue
            if rule.endpoint == "public.offer":
                args["slug"] = off.slug
            if "slug" in rule.arguments and rule.endpoint.startswith("public.vendor"):
                args["slug"] = order.vendor.slug
            if rule.endpoint in ("public.category",):
                args["slug"] = Category.query.first().slug
            if rule.endpoint == "public.help_article":
                args["slug"] = HelpArticle.query.first().slug
            if rule.endpoint == "public.page":
                args["slug"] = "conditions-generales"
            if rule.endpoint == "admin.page_edit":
                args["slug"] = "conditions-generales"
            try:
                from flask import url_for
                with app.test_request_context():
                    url = url_for(rule.endpoint, **args)
            except Exception as ex:  # noqa: BLE001
                problems.append((role, rule.endpoint, "url_for", str(ex)[:100])); continue
            r = c.get(url)
            body = r.get_data(as_text=True) if r.mimetype.startswith("text") else ""
            tag = (role, url, r.status_code)
            if r.status_code >= 500:
                problems.append(tag + ("ERREUR SERVEUR",))
            elif r.status_code == 200 and r.mimetype == "text/html":
                for pat in (r">\s*None\s*<", r"Undefined", r"\{\{", r"\{%", r"Traceback", r"jinja2"):
                    m = re.search(pat, body)
                    if m:
                        problems.append(tag + (f"motif {pat!r}: …{body[max(0,m.start()-60):m.end()+40]!r}",))
                        break
                # liens internes
                for href in set(re.findall(r'href="(/[^"#]*)', body)):
                    href = href.replace("&amp;", "&")
                    if href.startswith(("/media/", "/static/")) or href in ("/deconnexion",):
                        continue
                    rr = c.get(href)
                    if rr.status_code >= 500 or (rr.status_code == 404 and not href.startswith("/offres/")):
                        problems.append((role, url, rr.status_code, f"lien {href}"))
for p in problems:
    print(p)
print(len(problems), "problème(s)")
