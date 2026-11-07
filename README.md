# CRIT

Bot Discord (discord.py) dédié aux critiques d'œuvres : un carnet de notes type Senscritique / Letterboxd, commun à tous les serveurs, avec fiches en Components V2 (`LayoutView`) et persistance SQLite.

---

## Fonctionnalités

Noter (0 à 10, entier ; une étoile = 2 points) films et séries (TMDB), jeux (Steam), albums et morceaux (Spotify) ou livres (Open Library), avec commentaire optionnel (spoiler possible). Fiches d'œuvre, journal, signets, listes communes, affinités de goût, profil + XP, top du serveur, et annonces des nouvelles notes. Les récompenses de profil se craftent dans `cogs/reviews/progress.py`.

Notes, XP, signets, favoris et préférences suivent le membre sur tous les serveurs où est CRIT. Les vues « serveur » (catalogue, top, moyennes, critiques d'une fiche, recommandations, annonces) ne comptent que les membres présents sur ce serveur ; réglages, listes communes et streams planifiés restent propres au serveur.

Un message qui n'est qu'un lien TMDB, IMDb, Letterboxd, Steam, Spotify ou Open Library reçoit la fiche de l'œuvre en réponse, si au moins un membre du serveur l'a déjà notée. Sinon, ou dans une phrase, clic droit sur le message → **Ouvrir la fiche**.

## Commandes

- `/search` — catalogues externes : fiche, noter, signet ou liste (autocomplete dès 2 lettres)
- `/stream` — lives en cours (fiches liées) ; lie le tien ou planifie un stream
- `/carnet` — page d'un membre : profil, journal, signets et affinités (aussi via clic droit → Voir le carnet)
- `/explore` — feuillette ce que le serveur a déjà noté : récentes, catalogue, top et pour toi
- `/listes` — listes communes : titre, description, droits d'édition (autocomplete pour ouvrir une liste)
- `/reglages` — tes préférences (recherche, date, annonces, listes, stream) et l'apparence de tes étoiles, communes à tous les serveurs
- `/config` — panneau de configuration réservé à la modération (salons d'annonces par type, commentaires)
- `/help` — aide : commandes et comment noter

Chaque commande ouvre un seul message privé : choisir une œuvre y affiche sa fiche, la flèche **←** en haut ramène au menu précédent, et **Au hasard** tire une œuvre dans les signets, une liste ou le catalogue. Sur une fiche publiée dans le salon, utilisable par tous, **Noter** ouvre directement le formulaire de la personne qui clique et **Ouvrir** lui affiche la fiche en privé.

## Administration

Commandes propriétaire (préfixe `&`) : `ping`, `restart`, `update` (git pull + redémarrage), `shutdown`, gestion des cogs (`cogs`, `load`, `unload`, `reload`) et synchronisation des slash commands (`sync`).

## Code

`cogs/reviews/` :

- `reviews.py` — le cog : commandes, écouteurs, annonces
- `storage.py` / `live.py` — accès aux données et suivi des lives (mixins du cog)
- `database.py` — schéma de `data/carnet.db` et fusion des anciennes bases par serveur
- `providers.py` — catalogues externes ; `links.py` — reconnaissance des liens
- `constants.py`, `formatting.py`, `prefs.py`, `progress.py`, `streaming.py` — briques sans Discord
- `ui/` — les menus : `base` (socle, onglets, Retour), `fiche` (rendu et messages publics), `session` (fiche privée unique), `actions` (ses boutons), `navigation` (clics sur les messages publics), `streams`, `profile`, `preferences`, `lists`, `server`, `settings` (config et aide)

Au premier démarrage, les anciennes bases `data/guild_<id>.db` sont fusionnées dans `data/carnet.db` puis rangées dans `data/legacy/` (jamais supprimées). Une même œuvre notée sur deux serveurs garde la note la plus récente ; l'XP des serveurs s'additionne.

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord (Components V2 / `LayoutView`)
- aiosqlite — persistance locale
- aiohttp — clients TMDB, Steam, Spotify, Open Library
- python-dotenv — configuration via `.env` (`TOKEN`, `APP_ID`, plus `TMDB_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`)

## Licence

[MIT](LICENSE) — Acrone, 2026
