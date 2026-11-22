"""Configuration. Toutes les valeurs sensibles viennent de variables d'environnement (.env)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    ENV = os.environ.get("EVENTY_ENV", "development")  # development | production | testing
    DEBUG = ENV == "development"
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me-" + "x" * 24)
    # Clé de chiffrement des documents sensibles au repos (Fernet, 32 octets base64 url-safe).
    # Si absente, elle est dérivée de SECRET_KEY (acceptable en dev, à définir en production).
    DOCS_ENCRYPTION_KEY = os.environ.get("DOCS_ENCRYPTION_KEY", "")

    INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", BASE_DIR / "instance"))
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + str(INSTANCE_DIR / "eventystore.db")
    )
    if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", INSTANCE_DIR / "uploads"))  # public (images)
    PRIVATE_DIR = Path(os.environ.get("PRIVATE_DIR", INSTANCE_DIR / "private"))  # chiffré / privé
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MAX_DOC_BYTES = 10 * 1024 * 1024

    SEND_FILE_MAX_AGE_DEFAULT = 60 * 60 * 24 * 7 if ENV == "production" else 0
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = ENV == "production"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = ENV == "production"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 14
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8

    SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:5000")
    SITE_NAME = "EventyStore"
    CURRENCY = "XOF"
    TIMEZONE = "Africa/Dakar"

    # E-mail (SMTP). Sans SMTP_HOST, les e-mails sont conservés dans la table `email_outbox`
    # (consultable en dev sur /dev/outbox et dans l'admin) mais PAS envoyés.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_TLS = _bool("SMTP_TLS", True)
    MAIL_FROM = os.environ.get("MAIL_FROM", "EventyStore <no-reply@eventystore.sn>")

    # Démo publique : crée la base + données FICTIVES au démarrage ; valide les e-mails sans SMTP.
    DEMO_AUTOSEED = _bool("DEMO_AUTOSEED", False)
    AUTO_VERIFY_EMAIL = _bool("AUTO_VERIFY_EMAIL", False)

    # SMS : aucun fournisseur branché par défaut (point d'extension services/notifications.py).
    SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "")

    # Paiements : "sandbox" (simulation interne, AUCUN argent réel) | "wave" | "orange_money"
    PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "sandbox")
    PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "dev-webhook-secret")
    WAVE_API_BASE = os.environ.get("WAVE_API_BASE", "https://api.wave.com")
    WAVE_API_KEY = os.environ.get("WAVE_API_KEY", "")
    WAVE_WEBHOOK_SECRET = os.environ.get("WAVE_WEBHOOK_SECRET", "")
    ORANGE_MONEY_CLIENT_ID = os.environ.get("ORANGE_MONEY_CLIENT_ID", "")
    ORANGE_MONEY_CLIENT_SECRET = os.environ.get("ORANGE_MONEY_CLIENT_SECRET", "")
    ORANGE_MONEY_MERCHANT_KEY = os.environ.get("ORANGE_MONEY_MERCHANT_KEY", "")

    # Planificateur interne (expiration des blocages, rappels…). En production : `flask run-jobs` via cron.
    ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", ENV == "development")
    ENFORCE_ADMIN_2FA = _bool("ENFORCE_ADMIN_2FA", ENV == "production")
    LOGIN_MAX_ATTEMPTS = 6
    LOGIN_WINDOW_MINUTES = 15


class TestingConfig(Config):
    ENV = "testing"
    TESTING = True
    DEBUG = False
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    ENABLE_SCHEDULER = False
    ENFORCE_ADMIN_2FA = False
    SERVER_NAME = None
