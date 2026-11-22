# EventyStore — marketplace événementielle (Sénégal)

Plateforme web complète : site public, catalogue/recherche, espaces organisateur, prestataire et administration,
devis versionnés avec négociation, calendrier/stock, panier, paiements (sandbox + adaptateurs Wave/Orange Money
préparés), messagerie, notifications, avis, support.

**Stack** : Python 3.9+, Flask 3, SQLAlchemy 2 (SQLite en dev, PostgreSQL en production via `DATABASE_URL`),
Jinja (rendu serveur, SEO), CSS/JS sans dépendance externe. Détails : `docs/ARCHITECTURE.md`.

## Démarrage

```bash
python3 -m venv .venv && .venv/bin/pip install --only-binary=:all: -r requirements.txt
cp .env.example .env            # facultatif en développement
.venv/bin/flask --app wsgi init-db       # tables + catalogue + pages légales (modèles) + aide
.venv/bin/flask --app wsgi seed-demo     # DONNÉES FICTIVES (facultatif)
.venv/bin/flask --app wsgi run           # http://127.0.0.1:5000
```

Comptes de démonstration (mot de passe `Demo1234`, créés uniquement par `seed-demo`) :
`acheteur@demo.eventystore.sn`, `prestataire1@demo.eventystore.sn` … `prestataire5@…`, `admin@demo.eventystore.sn`.
Les e-mails ne sont pas envoyés sans SMTP : consultez `/dev/outbox` (dev) ou **Admin > E-mails sortants**.

Production : `flask --app wsgi create-admin`, **ne pas** lancer `seed-demo` (ou `flask purge-demo` / bouton admin).
Tâches périodiques : `flask --app wsgi run-jobs` toutes les 5 minutes (cron). Voir `docs/DEPLOYMENT.md`.

## Publier pour des testeurs
`docs/PUBLISH.md` : hébergement Render (permanent) ou tunnel temporaire. Le bouton « Donner mon avis » (toutes les pages)
enregistre les retours, lisibles dans **Administration > Retours testeurs** (export CSV). `DEMO_AUTOSEED=1` charge les
données fictives au démarrage, `AUTO_VERIFY_EMAIL=1` évite l'étape e-mail quand aucun SMTP n'est configuré.

## Tests
`.venv/bin/python -m pytest tests -q` — 18 tests bout en bout (réservation → paiement → confirmation → clôture → retrait,
négociation de devis, stock, signatures de webhook, droits d'accès, upload chiffré, CSRF, recherche…).

## Ce qui est opérationnel / ce qui ne l'est pas (lisez ceci)
Opérationnel et testé : comptes, rôles, vérification prestataire, offres, recherche/filtres, calendrier & stock,
devis/négociation, panier, commandes, acompte/solde, commissions, grand livre, retraits (workflow), remboursements
selon barème, messagerie, notifications in-app, avis vérifiés, tickets, back-office, audit, exports CSV.

**Non branché (volontairement, jamais simulé en silence)** :
- **Wave** : intégré d'après la doc publique (session Checkout, webhook signé avec anti-rejeu, relecture de la
  session chez Wave avant de créditer, remboursement total). Testé uniquement avec une API simulée : à valider en
  réel avec votre compte marchand (`PAYMENT_PROVIDER=wave`, `WAVE_API_KEY`, `WAVE_WEBHOOK_SECRET`, URL de webhook
  `https://<site>/webhooks/payments/wave`, site en HTTPS).
- **Orange Money** : non intégré — l'API officielle exige un compte développeur et un contrat marchand ; aucun
  endpoint n'a été inventé (`services/payments.py`, classe `OrangeMoneyProvider`). Une seule passerelle active à la fois.
- Par défaut `PAYMENT_PROVIDER=sandbox` : **simulation, aucun argent réel**, signalée à l'écran. Pas de carte bancaire.
- **Virement effectif des reversements** : workflow complet, mais le transfert est exécuté manuellement par la finance.
- **SMS** et **e-mail** : e-mail via SMTP si configuré (sinon journalisé) ; aucun fournisseur SMS.
- **Connexion Google/Facebook**, vérification du téléphone par code, synchronisation calendrier externe, PDF natif
  (les reçus/récapitulatifs s'impriment en PDF via le navigateur).
- **Textes juridiques** (CGU, confidentialité, annulation…) : modèles de travail marqués « à valider ».
Voir `docs/DECISIONS.md` pour les incohérences relevées et les décisions prises.


## Contrôles complémentaires
`.venv/bin/python tests/crawl.py` visite toutes les routes GET de chaque rôle (erreurs 5xx, rendus cassés, liens morts) ;
`.venv/bin/python tests/fuzz_post.py` envoie des formulaires invalides sur toutes les routes POST (aucune 5xx attendue).
