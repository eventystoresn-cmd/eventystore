# Décisions, incohérences et limites

## Incohérences relevées dans les documents
1. **Bouton principal** : la charte V2 le veut violet (secondaire jaune/anthracite), le cahier §17.4 le veut jaune-or.
   → Décision : charte V2 (document de référence) ; le jaune-or est utilisé pour les actions de conversion
   (Rechercher, Réserver, Devis), le violet pour les actions courantes.
2. **Description du logo/palette (cahier §17.1–17.2)** parle d'un logo « parenthèse », de vert/bleu et d'une zone de
   protection de ¼ de parenthèse : cela décrit une ancienne identité. → Appliqués : logo, signature et palette de la
   charte V2 (violet #4B00A5, or #F4B400, lavande #E8E1FF, anthracite #1F1F1F, fonds #FFFFFF/#F8F5FF).
3. **Police** : Gotham n'est pas fournie (licence) → pile `Gotham, Verdana` (substitution prévue par la charte).
4. **Logos** : fichiers PNG générés par IA, avec de légers artefacts de détourage. Utilisés tels quels et sans
   déformation (variantes : compacte sans signature, avec signature, blanche, symbole, favicons dérivées par
   recadrage/aplat blanc). La charte demande un redessin vectoriel avant diffusion large.
   `Logo EventyStore .png` (effet lumineux sur fond noir) n'est pas utilisé.
5. Le cahier cite « carte de registre ? » (docs prestataires) : liste retenue = identité (indépendant) ;
   identité + RCCM + NINEA (entreprise), à confirmer.

## Décisions techniques (informations manquantes du cahier §24)
| Sujet | Décision par défaut (modifiable en admin) |
|---|---|
| Commission | 10 % global ; règles par catégorie / prestataire / type d'offre ; taux figé sur chaque commande |
| Confirmation | acompte payé **et** confirmation prestataire (option : auto dès l'acompte) |
| Blocage | 15 min pendant le paiement (panier) ; 48 h après acceptation d'un devis ; une demande de devis ne bloque rien |
| Négociation | prix/lignes, quantité, dates et conditions ; 5 contre-propositions acheteur max |
| Annulation | 3 barèmes (flexible/modérée/stricte) choisis par offre ; annulation prestataire = remboursement intégral |
| Caution | affichée avant paiement, encaissement/restitution **hors plateforme** (non géré) |
| Multi-prestataires | un panier → une commande et un paiement par prestataire |
| Reversement | net disponible 2 jours après clôture ; retrait min. 5 000 FCFA ; transfert manuel |
| Facturation/TVA | non gérée : reçu non fiscal ; aucune taxe ajoutée automatiquement |
| Zone de lancement | toutes les régions disponibles ; texte « Dakar et périphérie » éditable |
| Granularité | jour calendaire (pas de créneaux horaires dans le calcul de disponibilité) |
| Modération | annonces modérées avant publication (désactivable) |
| Paiement hors plateforme | possible si l'offre est marquée « non payable en ligne » ; clairement signalé « non protégé » |
| Fuseau | Africa/Dakar = UTC+0 sans changement d'heure ; dates stockées en UTC |
| Hash mots de passe | PBKDF2-SHA256 600 000 itérations (scrypt indisponible sur le Python système macOS) |

## Limites connues
Paiements réels, SMS, OAuth, vérification téléphone, PDF natif, anti-virus de fichiers, Alembic (migrations),
file de tâches, recherche plein texte avancée, traitement automatisé des demandes RGPD : non implémentés (voir README).
Les données de démonstration sont balisées `is_demo` et supprimables (`flask purge-demo`).
