"""EventyStore — marketplace événementielle (Sénégal). Application factory."""
import logging
import threading
import time
from datetime import date

from flask import Flask, jsonify, render_template, request
from flask_login import current_user

from .config import Config, TestingConfig
from .extensions import csrf, db, login_manager


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config or Config)
    if config is None and app.config["ENV"] == "production" and app.config["SECRET_KEY"].startswith("dev-only"):
        raise RuntimeError("SECRET_KEY doit être défini en production.")
    for key in ("INSTANCE_DIR", "UPLOAD_DIR", "PRIVATE_DIR"):
        try:
            app.config[key].mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    logging.basicConfig(level=logging.INFO if not app.debug else logging.DEBUG)

    from . import models  # noqa: F401  (enregistre les tables)
    from .blueprints import register_blueprints
    from . import cli, templating

    register_blueprints(app)
    templating.init_app(app)
    cli.init_app(app)
    _security_headers(app)
    if app.config.get("TESTING"):
        # les tests gardent un contexte d'application ouvert : on évite que `g` mette en cache l'utilisateur
        from flask import g
        @app.before_request
        def _reset_login_cache():
            g.pop("_login_user", None)
    _error_handlers(app)

    if app.config.get("ENABLE_SCHEDULER") and not app.config.get("TESTING"):
        _start_scheduler(app)
    return app


def _security_headers(app):
    csp = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
           "font-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
        if app.config["ENV"] == "production":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # pages privées : jamais de cache partagé
        if request.endpoint and not request.endpoint.startswith(("static", "media.")) and current_user.is_authenticated:
            resp.headers["Cache-Control"] = "no-store"
        return resp


def _error_handlers(app):
    def wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(403)
    def forbidden(e):
        if wants_json():
            return jsonify(error="forbidden"), 403
        return render_template("errors/error.html", code=403, title="Accès refusé",
                               message="Vous n'avez pas les droits nécessaires pour consulter cette page."), 403

    @app.errorhandler(404)
    def not_found(e):
        if wants_json():
            return jsonify(error="not_found"), 404
        return render_template("errors/error.html", code=404, title="Page introuvable",
                               message="La page demandée n'existe pas ou a été déplacée."), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("errors/error.html", code=413, title="Fichier trop volumineux",
                               message="Le fichier envoyé dépasse la taille autorisée (12 Mo)."), 413

    @app.errorhandler(400)
    def bad_request(e):
        if wants_json():
            return jsonify(error="bad_request", message=getattr(e, "description", "")), 400
        return render_template("errors/error.html", code=400, title="Requête invalide",
                               message=getattr(e, "description", "") or "La requête est invalide ou a expiré. Rechargez la page et réessayez."), 400

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if wants_json():
            return jsonify(error="server_error"), 500
        return render_template("errors/error.html", code=500, title="Une erreur est survenue",
                               message="Notre équipe a été informée. Réessayez dans quelques instants."), 500


def _start_scheduler(app, interval=60):
    """Planificateur de développement : exécute les tâches périodiques dans un thread démon."""
    import os
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true" and os.environ.get("FLASK_RUN_FROM_CLI"):
        pass

    def loop():
        from .services import jobs
        while True:
            time.sleep(interval)
            with app.app_context():
                try:
                    jobs.run_all()
                except Exception:  # noqa: BLE001
                    app.logger.exception("Erreur tâches périodiques")
                    db.session.rollback()

    threading.Thread(target=loop, daemon=True, name="eventy-jobs").start()
