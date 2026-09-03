# ACK

Bot Discord (discord.py) dédié aux critiques d'œuvres : un carnet de notes type Senscritique / Letterboxd, par serveur, avec fiches en Components V2 (`LayoutView`) et persistance SQLite.

---

## Fonctionnalités

Noter (0 à 5, demies étoiles) films et séries (TMDB), jeux (Steam), albums et morceaux (Spotify) ou livres (Open Library), avec commentaire optionnel. Fiches d'œuvre, journal, affinités de goût, profil + XP, top du serveur, et annonces des nouvelles notes. Les récompenses de profil se craftent dans `cogs/reviews/progress.py`.

## Commandes

- `/note` — recherche une œuvre et enregistre (ou prépare) ta note
- `/profil` — profil d'un membre : préférées, journal et affinités
- `/search` — explore le serveur : récentes, catalogue et top
- `/config` — panneau de configuration réservé à la modération (annonces, commentaires)

## Administration

Commandes propriétaire (préfixe `&`) : `ping`, `restart`, `update` (git pull + redémarrage), `shutdown`, gestion des cogs (`cogs`, `load`, `unload`, `reload`) et synchronisation des slash commands (`sync`).

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord (Components V2 / `LayoutView`)
- aiosqlite — persistance locale
- aiohttp — clients TMDB, Steam, Spotify, Open Library
- python-dotenv — configuration via `.env` (`TOKEN`, `APP_ID`, plus `TMDB_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`)

## Licence

[MIT](LICENSE) — Acrone, 2026
