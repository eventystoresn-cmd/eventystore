# Sécurité (état réel)
Implémenté : hash PBKDF2-SHA256 ; CSRF sur tous les POST ; cookies HttpOnly/SameSite (Secure en prod) ; CSP stricte sans
script inline, X-Frame-Options, nosniff, HSTS (prod) ; limitation des tentatives de connexion (compte + IP) ;
réponse identique au « mot de passe oublié » ; liens de réinitialisation signés, 1 h, usage unique ; redirection
`next` validée ; contrôle d'accès serveur et 404 sur ressources d'autrui ; uploads validés (extension, signature,
taille, Pillow), images ré-encodées ; documents sensibles chiffrés (Fernet) et accès journalisé ; coordonnées de
retrait chiffrées et masquées, modification soumise au mot de passe ; webhooks signés + idempotents + contrôle du montant ;
prix recalculés côté serveur ; journal d'audit ; 2FA TOTP du personnel (obligatoire si `ENFORCE_ADMIN_2FA`, défaut en production).
À faire avant production : définir `SECRET_KEY` et `DOCS_ENCRYPTION_KEY` (`flask gen-key`), HTTPS, PostgreSQL,
sauvegardes + test de restauration, anti-virus des uploads, limitation globale (reverse proxy), surveillance d'erreurs,
audit externe, validation juridique (CDP Sénégal).
