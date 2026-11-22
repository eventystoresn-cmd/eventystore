# Publier une version de démonstration (lien public)

La démo publique charge des **données fictives**, simule les paiements (aucun argent réel), confirme les e-mails
automatiquement (pas de SMTP) et affiche un bouton « Donner mon avis » : les retours arrivent dans
**Administration > Retours testeurs** (export CSV).

## Option recommandée : Render (gratuit, HTTPS automatique)
1. Mettre le dossier du projet dans un dépôt GitHub (`git init`, `git add .`, `git commit`, `git push`).
2. Sur render.com : *New > Blueprint* → choisir le dépôt : `render.yaml` crée le service.
3. Après le premier déploiement, renseigner la variable `SITE_URL` avec l'adresse fournie (`https://….onrender.com`).
4. Partager cette adresse. Comptes de test (affichés sur la page de connexion) : mot de passe `Demo1234`.

Limites de l'offre gratuite : le service s'endort après inactivité (premier chargement lent) et la base SQLite est
réinitialisée à chaque redémarrage — les retours doivent donc être exportés régulièrement (CSV) ou la base passée
sur PostgreSQL (`DATABASE_URL`). Pour une vraie mise en production, suivre `docs/DEPLOYMENT.md`.

## Alternative immédiate : lien temporaire depuis cet ordinateur
`flask --app wsgi run` puis un tunnel HTTPS (ex. `ssh -R 80:localhost:5000 nokey@localhost.run`). Le lien ne
fonctionne que tant que l'ordinateur reste allumé et connecté.
