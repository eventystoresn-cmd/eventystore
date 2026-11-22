# Architecture

```
eventystore/
  __init__.py      create_app, en-têtes de sécurité (CSP…), erreurs, planificateur dev
  config.py        configuration par variables d'environnement
  models.py        modèle relationnel (≈45 tables), montants en FCFA entiers
  security.py      rôles/permissions, anti brute-force, TOTP (2FA staff)
  services/        logique métier SANS HTTP : availability, pricing, quotes, orders, payments, finance,
                   search, messaging, notifications, files (uploads/chiffrement), jobs, audit, settings
  blueprints/      public, auth, account, buyer (/espace), vendor (/pro), admin (/admin), messaging,
                   payments (+webhooks), api (/api/v1), media
  templates/ static/   rendu serveur Jinja, CSS/JS maison (charte V2)
tests/             pytest bout en bout
```

- **Rôles** : `buyer`, `vendor`, `staff` (+ rôles d'équipe : super_admin, catalog, vendor_validation, support,
  finance, moderation → permissions, cf. `models.ROLE_PERMISSIONS`). Contrôle côté serveur sur chaque route ;
  ressources d'autrui → 404.
- **Disponibilité** (`services/availability.py`) : jour calendaire ; blocages prestataire (ponctuels/hebdo) ;
  stock par jour pour la location ; capacité simultanée pour les services ; tampons avant/après ; blocage temporaire
  `hold_expires_at` pendant le paiement ; aucune réservation au-delà du stock.
- **Devis** (`services/quotes.py`) : une ligne `quotes` par version, jamais modifiée ; statuts, expiration, limite de
  contre-propositions, diff entre versions.
- **Commande** (`services/orders.py`) : instantané (prix, conditions, commission, politique d'annulation) ;
  règle de confirmation paramétrable (`deposit` | `vendor_and_deposit`).
- **Paiement** (`services/payments.py`) : un seul point de confirmation, `process_event` (idempotent, signature,
  rapprochement du montant). Grand livre `ledger_entries` : crédit net à l'encaissement, « disponible » après
  clôture + délai, débits de remboursement et de retrait.
- **Fichiers** : images ré-encodées (EXIF supprimé) ; documents sensibles chiffrés (Fernet) hors du dossier public ;
  accès via routes autorisées et journalisées.
- **Évolution** : PostgreSQL (changer `DATABASE_URL`, ajouter Alembic), multi-pays (région/devise isolées dans
  `utils`/`config`), file de tâches (Celery/RQ) à la place de `run-jobs`, stockage objet (S3) via `services/files.py`.

Modèle de données : voir `eventystore/models.py` (docstrings) ; entités du cahier §18 toutes couvertes.
