# Déploiement
1. Serveur Linux, Python 3.9+, PostgreSQL ; reverse proxy HTTPS (Nginx/Caddy) transmettant `X-Forwarded-For`.
2. Variables : `EVENTY_ENV=production`, `SECRET_KEY`, `DOCS_ENCRYPTION_KEY`, `DATABASE_URL`, `SITE_URL`, SMTP_*, `PAYMENT_PROVIDER`
   (+ clés Wave/Orange après intégration), `UPLOAD_DIR`/`PRIVATE_DIR` sur volume persistant et sauvegardé.
3. `pip install -r requirements.txt` ; `flask --app wsgi init-db` ; `flask --app wsgi create-admin`.
4. `gunicorn -w 3 -b 127.0.0.1:8000 wsgi:app` (service systemd).
5. Cron : `*/5 * * * * cd /app && .venv/bin/flask --app wsgi run-jobs`.
6. Désactiver les données de démo (`flask purge-demo`). Environnements dev/recette/production séparés.
7. Montée de version du schéma : ajouter Alembic avant le premier déploiement réel (aujourd'hui `create_all`).
