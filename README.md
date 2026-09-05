# CRIT

Bot Discord (discord.py) dédié aux critiques d'œuvres : un carnet de notes type Senscritique / Letterboxd, par serveur, avec fiches en Components V2 (`LayoutView`) et persistance SQLite.

---

## Fonctionnalités

Noter (0 à 10, entier ; une étoile = 2 points) films et séries (TMDB), jeux (Steam), albums et morceaux (Spotify) ou livres (Open Library), avec commentaire optionnel (spoiler possible). Fiches d'œuvre, journal, liste à voir, listes communes, affinités de goût, profil + XP, top du serveur, et annonces des nouvelles notes. Les récompenses de profil se craftent dans `cogs/reviews/progress.py`.

## Commandes

- `/search` — catalogues externes : fiche, noter ou à voir (autocomplete dès 2 lettres)
- `/carnet` — page d'un membre : préférées, journal, à voir et affinités (aussi via clic droit → Voir le carnet)
- `/explore` — feuillette ce que le serveur a déjà noté : récentes, catalogue et top
- `/listes` — listes communes : titre, description, droits d'édition, tirage (autocomplete pour ouvrir une liste)
- `/tirage` — tire une œuvre encore à voir (ta liste, celle d'un membre) ou dans une liste commune
- `/config` — panneau de configuration réservé à la modération (salons d'annonces par type, commentaires)
- `/help` — aide : commandes et comment noter

## Administration

Commandes propriétaire (préfixe `&`) : `ping`, `restart`, `update` (git pull + redémarrage), `shutdown`, gestion des cogs (`cogs`, `load`, `unload`, `reload`) et synchronisation des slash commands (`sync`).

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord (Components V2 / `LayoutView`)
- aiosqlite — persistance locale
- aiohttp — clients TMDB, Steam, Spotify, Open Library
- python-dotenv — configuration via `.env` (`TOKEN`, `APP_ID`, plus `TMDB_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`)

## Licence

[MIT](LICENSE) — Acrone, 2026
