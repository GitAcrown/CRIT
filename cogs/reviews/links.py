"""Reconnaissance des liens d'œuvres collés dans un message.

TMDB, IMDb, Letterboxd, Steam, Spotify et Open Library. Chaque lien devient une requête
que `MediaCatalog.search` sait résoudre directement (`tmdb:movie/27205`, `steam:620`…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .providers import MediaHit


@dataclass(frozen=True)
class MediaLink:
    query: str
    season: int | None = None
    # (source, source_id, media_type) quand le lien donne l'id : on peut chercher
    # l'œuvre dans la base sans appeler le catalogue. Absent pour IMDb et Letterboxd.
    identity: tuple[str, str, str] | None = None


_TMDB_LINK = re.compile(
    r"(?:www\.)?themoviedb\.org/(movie|tv)/(\d+)(?:-[^/?#\s>]*)?(?:/season/(\d+))?",
    re.I,
)
_IMDB_LINK = re.compile(r"(?:[a-z]+\.)?imdb\.com/(?:[a-z]{2}/)?title/(tt\d+)", re.I)
_LETTERBOXD_LINK = re.compile(r"letterboxd\.com/(?:[^/\s>]+/)?film/([a-z0-9-]+)", re.I)
_STEAM_LINK = re.compile(r"(?:store\.steampowered|steamcommunity)\.com/app/(\d+)", re.I)
_SPOTIFY_LINK = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(album|track)/([A-Za-z0-9]{22})"
    r"|spotify:(album|track):([A-Za-z0-9]{22})",
    re.I,
)
_OPENLIBRARY_LINK = re.compile(r"openlibrary\.org/works/(OL\d+W)", re.I)
_SLUG_YEAR = re.compile(r"-(19\d{2}|20\d{2})(?:-\d+)?$")


def _letterboxd_query(slug: str) -> str:
    """`dune-part-two` → `film:dune part two` ; `dune-2021` → `film:dune 2021`."""
    year = _SLUG_YEAR.search(slug)
    title = slug[: year.start()] if year else slug
    words = title.replace("-", " ").strip()
    return f"film:{words} {year.group(1)}" if year else f"film:{words}"


def _parse_link(token: str) -> MediaLink | None:
    if match := _TMDB_LINK.search(token):
        kind, tmdb_id = match.group(1).lower(), match.group(2)
        season = int(match.group(3)) if match.group(3) else None
        source_id = f"{tmdb_id}:s{season}" if season is not None else tmdb_id
        return MediaLink(f"tmdb:{kind}/{tmdb_id}", season, ("tmdb", source_id, kind))
    if match := _IMDB_LINK.search(token):
        return MediaLink(f"imdb:{match.group(1)}")
    if match := _LETTERBOXD_LINK.search(token):
        return MediaLink(_letterboxd_query(match.group(1).lower()))
    if match := _STEAM_LINK.search(token):
        return MediaLink(f"steam:{match.group(1)}", identity=("steam", match.group(1), "game"))
    if match := _SPOTIFY_LINK.search(token):
        kind = (match.group(1) or match.group(3)).lower()
        spotify_id = match.group(2) or match.group(4)
        return MediaLink(f"spotify:{kind}/{spotify_id}", identity=("spotify", spotify_id, kind))
    if match := _OPENLIBRARY_LINK.search(token):
        key = match.group(1).upper()
        return MediaLink(f"ol:{key}", identity=("openlibrary", f"/works/{key}", "book"))
    return None


def _tokens(content: str) -> list[tuple[str, MediaLink | None]]:
    return [(token, _parse_link(token)) for token in (content or "").split()]


def find_media_links(content: str, *, limit: int = 3) -> list[MediaLink]:
    """Liens d'œuvres du message, sans doublon, dans l'ordre."""
    links: list[MediaLink] = []
    for _token, link in _tokens(content):
        if link is not None and link not in links:
            links.append(link)
            if len(links) >= limit:
                break
    return links


def message_is_only_links(content: str) -> bool:
    """Le message ne contient que des liens d'œuvres (un partage, pas une phrase)."""
    tokens = _tokens(content)
    return bool(tokens) and all(link is not None for _token, link in tokens)


def pick_fiche_hit(hits: list[MediaHit]) -> MediaHit | None:
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]

    def rank(hit: MediaHit) -> tuple[int, float]:
        extra = hit.extra or {}
        return (int(extra.get("vote_count") or 0), float(extra.get("popularity") or 0))

    return max(hits, key=rank)
