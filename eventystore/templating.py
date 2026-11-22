"""Filtres, globales et variables de contexte Jinja."""
from datetime import date

from flask import current_app, request, session, url_for
from flask_login import current_user

from . import utils
from .icons import ICONS
from .extensions import db
from .models import (CANCELLATION_POLICIES, EVENT_TYPES, OFFER_STATUSES, OFFER_TYPES, ORDER_STATUSES,
                     PAYMENT_METHODS, PAYMENT_STATUSES, PAYOUT_STATUSES, PRICE_MODELS, PRICE_UNITS,
                     QUOTE_REQUEST_STATUSES, QUOTE_STATUSES, STAFF_ROLES, TICKET_CATEGORIES, TICKET_STATUSES,
                     VERIFICATION_STATUSES, Banner, CartItem, Category, Page)
from .services import messaging, notifications, pricing, settings

BADGE_MAPS = {
    "order": ORDER_STATUSES, "payment": PAYMENT_STATUSES, "quote": QUOTE_STATUSES, "request": QUOTE_REQUEST_STATUSES,
    "offer": OFFER_STATUSES, "vendor": VERIFICATION_STATUSES, "payout": PAYOUT_STATUSES, "ticket": TICKET_STATUSES,
}


def init_app(app):
    app.jinja_env.filters.update({
        "xof": utils.fmt_xof, "date": utils.fmt_date, "daterange": utils.fmt_date_range, "dt": utils.fmt_dt,
        "ago": utils.time_ago, "md": utils.render_markdown, "nl2p": utils.nl2p, "price": pricing.price_label,
        "shortdate": lambda d: utils.fmt_date(d, long=False),
    })
    app.jinja_env.globals.update(
        BADGE_MAPS=BADGE_MAPS, ICONS_RAW=ICONS, OFFER_TYPES=OFFER_TYPES, PRICE_MODELS=PRICE_MODELS, PRICE_UNITS=PRICE_UNITS,
        EVENT_TYPES=EVENT_TYPES, CANCELLATION_POLICIES=CANCELLATION_POLICIES, PAYMENT_METHODS=PAYMENT_METHODS,
        TICKET_CATEGORIES=TICKET_CATEGORIES, STAFF_ROLES=STAFF_ROLES, REGIONS=utils.REGIONS, today=date.today,
        quantity_meaning=pricing.quantity_meaning, current_user=current_user,
    )

    @app.context_processor
    def inject():
        ctx = {"site_name": "EventyStore", "setting": settings.get, "unread_notifications": 0, "unread_messages": 0,
               "cart_count": 0, "nav_categories": [], "compare_ids": session.get("compare", [])}
        try:
            ctx["nav_categories"] = (Category.query.filter_by(parent_id=None, active=True)
                                     .order_by(Category.position).all())
            ctx["site_banners"] = Banner.query.filter_by(active=True).order_by(Banner.position).all()
            if current_user.is_authenticated:
                ctx["unread_notifications"] = notifications.unread_count(current_user)
                ctx["unread_messages"] = messaging.unread_total(current_user)
                if current_user.is_buyer:
                    ctx["cart_count"] = CartItem.query.filter_by(user_id=current_user.id).count()
        except Exception:  # noqa: BLE001 - base non initialisée (ex. première exécution)
            db.session.rollback()
        ctx["payment_mode"] = current_app.config["PAYMENT_PROVIDER"]
        ctx["env"] = current_app.config["ENV"]
        return ctx
