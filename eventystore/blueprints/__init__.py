"""Enregistrement des blueprints."""


def register_blueprints(app):
    from . import account, admin, api, auth, buyer, media, messaging, payments, public, vendor
    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(buyer.bp, url_prefix="/espace")
    app.register_blueprint(vendor.bp, url_prefix="/pro")
    app.register_blueprint(admin.bp, url_prefix="/admin")
    app.register_blueprint(account.bp, url_prefix="/compte")
    app.register_blueprint(messaging.bp, url_prefix="/messages")
    app.register_blueprint(payments.bp)
    app.register_blueprint(api.bp, url_prefix="/api/v1")
    app.register_blueprint(media.bp, url_prefix="/media")
