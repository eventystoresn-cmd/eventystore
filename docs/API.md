# API interne v1 (`/api/v1`, JSON)
Même origine, cookie de session ; les écritures exigent l'en-tête `X-CSRFToken` (meta `csrf-token`).

| Méthode | Route | Description |
|---|---|---|
| GET | `/offers` | recherche (mêmes paramètres que `/recherche` : q, category, place, start, end, guests, bmin, bmax, pmodel, punit, otype, rating, delivery, travel, online, sort, page) |
| GET | `/offers/<id>` | détail public (options, conditions, politique d'annulation) |
| GET | `/offers/<id>/availability?start&end&qty` | `available` \| `unavailable` \| `to_confirm` + motif |
| GET | `/offers/<id>/calendar?year&month` | état de chaque jour (free/partial/busy/blocked/past) |
| GET/POST | `/offers/<id>/price` | calcul serveur : lignes, remise, frais, total, acompte, disponibilité |
| GET | `/categories` | arbre des catégories |
| GET | `/suggest?q=` | autocomplétion (catégories, lieux, prestataires, offres) |
| GET | `/me/unread`, `/notifications` | compteurs et dernières notifications (session requise) |

Autres points d'entrée : `POST /webhooks/payments/<provider>` (signature HMAC obligatoire, idempotent),
`GET /messages/<id>/poll` (messagerie), `GET /sitemap.xml`, `GET /robots.txt`.
