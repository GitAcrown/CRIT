# CRIT

Bot Discord (discord.py) dédié aux critiques d'œuvres : un carnet de notes type Senscritique / Letterboxd, commun à tous les serveurs, avec fiches en Components V2 (`LayoutView`) et persistance SQLite.

---

## Fonctionnalités

Noter (0 à 10, entier ; une étoile = 2 points) films et séries (TMDB), jeux (Steam), albums et morceaux (Spotify) ou livres (Open Library), avec commentaire optionnel (spoiler possible). Fiches d'œuvre, journal, signets, listes communes, affinités de goût, profil + XP, top du serveur, et annonces des nouvelles notes. Les récompenses de profil se craftent dans `cogs/reviews/progress.py`.

Notes, XP, signets, favoris et préférences suivent le membre sur tous les serveurs où est CRIT. Les vues « serveur » (catalogue, top, moyennes, critiques d'une fiche, recommandations, annonces) ne comptent que les membres présents sur ce serveur ; réglages, listes communes et streams planifiés restent propres au serveur.

Un message qui n'est qu'un lien TMDB, IMDb, Letterboxd, Steam, Spotify ou Open Library reçoit la fiche de l'œuvre en réponse, si au moins un membre du serveur l'a déjà notée. Sinon, ou dans une phrase, clic droit sur le message → **Ouvrir la fiche**.

## Commandes

- `/search` — catalogues externes : fiche, noter ou signet (autocomplete dès 2 lettres)
- `/stream` — lives en cours (fiches liées) ; lie le tien pour poster la fiche à la fin
- `/carnet` — page d'un membre : profil, journal, signets et affinités (aussi via clic droit → Voir le carnet)
- `/explore` — feuillette ce que le serveur a déjà noté : récentes, catalogue et top
- `/listes` — listes communes : titre, description, droits d'édition, tirage (autocomplete pour ouvrir une liste)
- `/tirage` — tire une œuvre au hasard (tes signets, ceux d'un membre, ou une liste commune)
- `/preferences` — tes défauts : date vu/joué/lu, édition des nouvelles listes, types de recherche, annonces
- `/config` — panneau de configuration réservé à la modération (salons d'annonces par type, commentaires)
- `/help` — aide : commandes et comment noter

## Administration

Commandes propriétaire (préfixe `&`) : `ping`, `restart`, `update` (git pull + redémarrage), `shutdown`, gestion des cogs (`cogs`, `load`, `unload`, `reload`) et synchronisation des slash commands (`sync`).

## Code

`cogs/reviews/` :

- `reviews.py` — le cog : commandes, écouteurs, annonces
- `storage.py` / `live.py` — accès aux données et suivi des lives (mixins du cog)
- `database.py` — schéma de `data/carnet.db` et fusion des anciennes bases par serveur
- `providers.py` — catalogues externes ; `links.py` — reconnaissance des liens
- `constants.py`, `formatting.py`, `prefs.py`, `progress.py`, `streaming.py` — briques sans Discord
- `ui/` — les menus : `base` (socle), `fiche`, `actions`, `note`, `session`, `navigation`, `streams`, `profile`, `lists`, `server`, `settings`

Au premier démarrage, les anciennes bases `data/guild_<id>.db` sont fusionnées dans `data/carnet.db` puis rangées dans `data/legacy/` (jamais supprimées). Une même œuvre notée sur deux serveurs garde la note la plus récente ; l'XP des serveurs s'additionne.

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord (Components V2 / `LayoutView`)
- aiosqlite — persistance locale
- aiohttp — clients TMDB, Steam, Spotify, Open Library
- python-dotenv — configuration via `.env` (`TOKEN`, `APP_ID`, plus `TMDB_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`)

## Licence

[MIT](LICENSE) — Acrone, 2026
