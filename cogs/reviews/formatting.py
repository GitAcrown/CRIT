"""Mise en forme du texte : étoiles, notes, dates, mentions, libellés et saisons."""

from __future__ import annotations

from datetime import date, datetime
import json
import random
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils import pretty

from .constants import (
    ANNOUNCE_ROUTE_ALL,
    PARIS_TZ,
    RATING_MAX,
    STREAM_PLAN_HORIZON,
    TYPE_META,
    TYPE_STATS,
    VALID_RATINGS,
)
from .progress import resolve_star_skin, StarSkin
from .providers import MediaHit, parse_search_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_stars(rating: float, skin: StarSkin | str | None = None) -> str:
    """Rangée de 5 étoiles : chaque étoile vaut 2 points, une demi vaut 1."""
    chosen = resolve_star_skin(skin)
    points = int(round(max(0.0, min(float(RATING_MAX), float(rating)))))
    full = points // 2
    half = points % 2 == 1
    empty = 5 - full - (1 if half else 0)
    return chosen.full * full + (chosen.half if half else "") + chosen.empty * empty


def format_stars_compact(rating: float, skin: StarSkin | str | None = None) -> str:
    """Une seule étoile custom + note, pour boutons et texte rich."""
    chosen = resolve_star_skin(skin)
    points = int(round(max(0.0, min(float(RATING_MAX), float(rating)))))
    if points <= 0:
        icon = chosen.empty
    elif points % 2 == 1:
        icon = chosen.half
    else:
        icon = chosen.full
    return f"{icon} {points}"


def format_stars_select(rating: float) -> str:
    """Étoiles unicode : un Select n'affiche pas les customs dans label/description."""
    points = int(round(max(0.0, min(float(RATING_MAX), float(rating)))))
    if points <= 0:
        icon = "☆"
    elif points % 2 == 1:
        icon = "★☆"
    else:
        icon = "★"
    return f"{icon} {points}"


def format_score(rating: float, *, average: bool = False) -> str:
    if average:
        return f"{rating:.1f}/{RATING_MAX}"
    return f"{int(round(rating))}/{RATING_MAX}"


RATING_CHOICES = [
    app_commands.Choice(name=f"{format_stars_select(r)}/{RATING_MAX}", value=float(r))
    for r in VALID_RATINGS
]


_MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


_DATE_DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$")


_DATE_YMD = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")


_TIME_HM = re.compile(r"^(\d{1,2})(?:[:hH](\d{2}))?$")


def experienced_verb(media_type: str) -> str:
    return {
        "movie": "Vu",
        "tv": "Vu",
        "game": "Joué",
        "album": "Écouté",
        "track": "Écouté",
        "book": "Lu",
    }.get(media_type, "Vu")


ONGOING = "ongoing"


def allows_ongoing(media_type: str) -> bool:
    return media_type not in {"movie", "track"}


def ongoing_label(media_type: str) -> str:
    return {
        "movie": "En cours de visionnage",
        "tv": "En cours de visionnage",
        "game": "En cours de jeu",
        "album": "En cours d'écoute",
        "track": "En cours d'écoute",
        "book": "En cours de lecture",
    }.get(media_type, "En cours")


def is_ongoing(raw: str | None) -> bool:
    return (raw or "").strip() == ONGOING


def format_experienced_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        year, month, day = (int(part) for part in raw.split("-"))
        return f"{day} {_MONTHS_FR[month - 1]} {year}"
    except (ValueError, IndexError):
        return raw


def experienced_line(media_type: str, raw: str | None) -> str:
    if not raw:
        return ""
    if is_ongoing(raw) and allows_ongoing(media_type):
        return f"-# {ongoing_label(media_type)}"
    return f"-# {experienced_verb(media_type)} le {format_experienced_date(raw)}"


def experienced_from_row(row: Any) -> str:
    if row is None:
        return ""
    if isinstance(row, dict):
        return str(row.get("experienced_at") or "")
    try:
        return str(row["experienced_at"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def parse_experienced_date(raw: str) -> tuple[str | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return "", None
    match = _DATE_DMY.match(text) or _DATE_YMD.match(text)
    if match is None:
        return None, "Indique une date du type `12/03/2024` ou `2024-03-12`."
    if match.re is _DATE_YMD:
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
    else:
        day, month, year = (int(match.group(i)) for i in (1, 2, 3))
    try:
        date(year, month, day)
    except ValueError:
        return None, "Cette date n'existe pas."
    return f"{year:04d}-{month:02d}-{day:02d}", None


def parse_plan_datetime(date_raw: str, time_raw: str) -> tuple[int | None, str | None]:
    iso, err = parse_experienced_date(date_raw)
    if err or not iso:
        return None, err or "Indique une date du type `12/03/2026`."
    match = _TIME_HM.match((time_raw or "").strip())
    if match is None:
        return None, "Indique une heure du type `21:30`."
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None, "Cette heure n'existe pas."
    year, month, day = (int(part) for part in iso.split("-"))
    when = datetime(year, month, day, hour, minute, tzinfo=PARIS_TZ)
    stamp = int(when.timestamp())
    now = int(time.time())
    if stamp <= now:
        return None, "Cette date est déjà passée."
    if stamp > now + STREAM_PLAN_HORIZON:
        return None, "Tu peux planifier jusqu'à 90 jours à l'avance."
    return stamp, None


def experienced_to_input(raw: str) -> str:
    if not raw or is_ongoing(raw):
        return ""
    parts = raw.split("-")
    if len(parts) != 3:
        return raw
    try:
        return f"{int(parts[2]):02d}/{parts[1]}/{parts[0]}"
    except ValueError:
        return raw


def row_spoiler(row: Any) -> bool:
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("spoiler"))
    try:
        return bool(row["spoiler"])
    except (KeyError, IndexError, TypeError):
        return False


def format_comment(comment: str, *, spoiler: bool, hide: bool, limit: int = 180) -> str:
    if not comment:
        return ""
    text = pretty.shorten_text(comment, limit)
    if spoiler and hide:
        return f"||{text}||"
    return f"*{text}*"


def entry_year(row: Any) -> int | None:
    exp = experienced_from_row(row)
    if exp and len(exp) >= 4 and exp[:4].isdigit():
        return int(exp[:4])
    created = 0
    try:
        created = int(row["created_at"] or 0)
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            created = int(row.get("created_at") or 0)
    if created:
        return time.localtime(created).tm_year
    return None


def journal_stats_line(entries: list[tuple[MediaHit, Any]]) -> str:
    if not entries:
        return ""
    counts: dict[str, int] = {}
    this_year = 0
    year_now = date.today().year
    for hit, row in entries:
        counts[hit.media_type] = counts.get(hit.media_type, 0) + 1
        if entry_year(row) == year_now:
            this_year += 1
    parts: list[str] = []
    for key, singular, plural in TYPE_STATS:
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else plural}")
    if this_year:
        parts.append(f"{this_year} cette année")
    return "-# " + " · ".join(parts)


GRAPH_BAR_WIDTH = 8


GRAPH_FILL = "━"


GRAPH_BASE = "─"


def rating_counts(entries: list[tuple[MediaHit, Any]]) -> list[int]:
    counts = [0] * (RATING_MAX + 1)
    for _hit, row in entries:
        points = int(round(max(0.0, min(float(RATING_MAX), float(row["rating"])))))
        counts[points] += 1
    return counts


def format_rating_graph(entries: list[tuple[MediaHit, Any]]) -> str:
    if not entries:
        return ""
    counts = rating_counts(entries)
    peak = max(counts)
    lines = [
        "### Répartition",
    ]
    for score in range(RATING_MAX, -1, -1):
        n = counts[score]
        filled = 0 if peak <= 0 or n <= 0 else max(1, round(GRAPH_BAR_WIDTH * n / peak))
        bar = GRAPH_FILL * min(GRAPH_BAR_WIDTH, filled)
        if n:
            lines.append(f"{score}  {bar}  **{n}**")
        else:
            lines.append(f"-# {score}  {GRAPH_BASE}  0")
    return "\n".join(lines)


def pick_rating_highlights(
    entries: list[tuple[MediaHit, Any]],
) -> list[tuple[str, MediaHit, float]]:
    """Une mieux notée et une pire, au hasard parmi les notes extrêmes."""
    if not entries:
        return []
    scored = [(hit, float(row["rating"])) for hit, row in entries]
    best_score = max(rating for _hit, rating in scored)
    worst_score = min(rating for _hit, rating in scored)
    best_hit, best_rating = random.choice(
        [(hit, rating) for hit, rating in scored if rating == best_score]
    )
    picks = [("Mieux notée", best_hit, best_rating)]
    if best_score == worst_score:
        return picks
    worst_pool = [
        (hit, rating)
        for hit, rating in scored
        if rating == worst_score and hit.identity != best_hit.identity
    ]
    if not worst_pool:
        return picks
    worst_hit, worst_rating = random.choice(worst_pool)
    picks.append(("Pire note", worst_hit, worst_rating))
    return picks


def skip_note_autocomplete(raw: str) -> bool:
    text = (raw or "").strip()
    if len(text) < 2:
        return True
    spec = parse_search_query(text)
    return bool(spec.lookup_id or spec.source)


def autocomplete_query_value(hit: MediaHit) -> str:
    if hit.source == "tmdb":
        kind = "movie" if hit.media_type == "movie" else "tv"
        return f"tmdb:{kind}/{hit.source_id}"
    if hit.source == "steam":
        return f"steam:{hit.source_id}"
    if hit.source == "spotify":
        return f"spotify:{hit.media_type}:{hit.source_id}"
    if hit.source == "openlibrary":
        return f"ol:{hit.source_id}"
    return pretty.shorten_text(hit.title, 100)


def list_edit_label(mode: str) -> str:
    return {
        "owner": "Créateur seul",
        "members": "Membres choisis",
        "public": "Tout le serveur",
    }.get(mode, "Créateur seul")


def parse_list_types(raw: str | None) -> tuple[str, ...]:
    """Types autorisés. Vide = tous les types."""
    if not raw or raw == "all":
        return ()
    chosen = {part.strip() for part in str(raw).split(",") if part.strip() in TYPE_META}
    if not chosen or chosen >= set(TYPE_META):
        return ()
    return tuple(kind for kind in TYPE_META if kind in chosen)


def format_list_types(kinds: list[str] | tuple[str, ...]) -> str:
    chosen = [kind for kind in TYPE_META if kind in set(kinds)]
    if not chosen or set(chosen) >= set(TYPE_META):
        return ""
    return ",".join(chosen)


def list_types_label(raw: str | None) -> str:
    kinds = parse_list_types(raw)
    if not kinds:
        return "Tous types"
    return " · ".join(type_label(kind) for kind in kinds)


def list_accepts_type(raw: str | None, media_type: str) -> bool:
    kinds = parse_list_types(raw)
    return not kinds or media_type in kinds


def list_type_select(raw: str | None) -> discord.ui.Select:
    chosen = set(parse_list_types(raw))
    return discord.ui.Select(
        placeholder="Tous les types",
        min_values=0,
        max_values=len(TYPE_META),
        required=False,
        options=[
            discord.SelectOption(
                label=type_label(kind),
                value=kind,
                emoji=select_emoji(kind),
                description="Peut être combiné",
                default=kind in chosen,
            )
            for kind in TYPE_META
        ],
    )


def parse_rating(raw: str) -> float | None:
    cleaned = raw.strip().replace("/10", "").replace("/5", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if 0 <= value <= RATING_MAX:
        return float(value)
    return None


def type_label(media_type: str) -> str:
    return TYPE_META.get(media_type, ("", "Média"))[1]


def type_emoji(media_type: str) -> str:
    return TYPE_META.get(media_type, ("", "Média"))[0]


def announce_route_label(route: str) -> str:
    if route == ANNOUNCE_ROUTE_ALL:
        return "Tous les types"
    return type_label(route)


def select_emoji(media_type: str) -> discord.PartialEmoji | None:
    raw = type_emoji(media_type)
    if not raw:
        return None
    return discord.PartialEmoji.from_str(raw)


def select_hit_description(hit: MediaHit) -> str:
    year = f"{hit.year}" if hit.year else "—"
    parts = [type_label(hit.media_type), year]
    by = hit.subtitle or hit.extra.get("director") or ""
    if not by:
        created = hit.extra.get("created_by") or []
        by = created[0] if created else ""
    if by:
        parts.append(by)
    return pretty.shorten_text(" · ".join(parts), 95)


def hit_from_row(row: Any) -> MediaHit:
    extra: dict[str, Any] = {}
    try:
        extra = json.loads(row["extra_json"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    genres = [part for part in (row["genres"] or "").split("|") if part]
    year = row["year"]
    return MediaHit(
        source=row["source"],
        source_id=row["source_id"],
        media_type=row["media_type"],
        title=row["title"],
        subtitle=row["subtitle"] or "",
        year=int(year) if year else None,
        poster_url=row["poster_url"] or None,
        url=row["url"] or "",
        overview=row["overview"] or "",
        genres=genres,
        extra=extra,
    )


def user_display(guild: discord.Guild, bot: commands.Bot, user_id: int) -> tuple[str, str | None]:
    member = guild.get_member(user_id)
    if member:
        return member.display_name, member.display_avatar.url
    user = bot.get_user(user_id)
    if user:
        return user.display_name, user.display_avatar.url
    return f"Utilisateur {user_id}", None


def mention_of(guild: discord.Guild, bot: commands.Bot, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.mention
    name, _avatar = user_display(guild, bot, user_id)
    return f"**{name}**"


def mention_silent(user_id: int) -> str:
    """Mention Discord ; le ping est coupé via AllowedMentions.none()."""
    return f"<@{user_id}>"


def format_people_fr(mentions: list[str], extra: int = 0) -> str:
    if extra > 0:
        named = ", ".join(mentions)
        autres = "autre" if extra == 1 else "autres"
        return f"{named} et {extra} {autres}"
    if len(mentions) == 1:
        return mentions[0]
    if len(mentions) == 2:
        return f"{mentions[0]} et {mentions[1]}"
    return f"{', '.join(mentions[:-1])} et {mentions[-1]}"


def format_grade(title: str) -> str:
    return f"-# **_{title}_**"


def titled(mention: str, title: str) -> str:
    return f"{mention}\n{format_grade(title)}"


def fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def hit_identity(hit: MediaHit | dict[str, Any]) -> tuple[str, str, str]:
    if isinstance(hit, MediaHit):
        return (hit.source, str(hit.source_id), hit.media_type)
    return (
        str(hit.get("source") or ""),
        str(hit.get("source_id") or ""),
        str(hit.get("media_type") or ""),
    )


def season_label(number: int) -> str:
    if number == 0:
        return "Spéciales"
    return f"Saison {number}"


def season_number_of(hit: MediaHit) -> int | None:
    if hit.media_type != "tv" or hit.source != "tmdb":
        return None
    raw = hit.extra.get("season")
    if raw is None and ":s" in str(hit.source_id):
        raw = str(hit.source_id).rsplit(":s", 1)[-1]
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def tv_season_list(hit: MediaHit) -> list[dict[str, Any]]:
    raw = hit.extra.get("season_list") if hit.media_type == "tv" else None
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("n") is None:
            continue
        try:
            number = int(item["n"])
        except (TypeError, ValueError):
            continue
        items.append({**item, "n": number})
    items.sort(key=lambda item: int(item["n"]))
    return items


def _strip_season_title(title: str) -> str:
    marker = " — "
    if marker not in title:
        return title
    head, tail = title.rsplit(marker, 1)
    if tail == "Spéciales" or tail.startswith("Saison "):
        return head
    return title


def split_tv_scope(hit: MediaHit) -> tuple[MediaHit, int | None]:
    """Ramène une fiche saison à la série, et le numéro de saison choisi."""
    number = season_number_of(hit)
    if number is None:
        return hit, None
    show_id = str(hit.extra.get("show_id") or str(hit.source_id).split(":s", 1)[0])
    if not show_id:
        return hit, None
    drop = {
        "season", "show_id", "show_title", "show_subtitle", "show_year",
        "show_poster", "show_overview", "show_genres", "episodes",
    }
    extra = {key: value for key, value in hit.extra.items() if key not in drop}
    genres = hit.extra.get("show_genres")
    if not isinstance(genres, list):
        genres = list(hit.genres)
    year = hit.extra.get("show_year")
    try:
        year_value = int(year) if year else None
    except (TypeError, ValueError):
        year_value = None
    show = MediaHit(
        source="tmdb",
        source_id=show_id,
        media_type="tv",
        title=str(hit.extra.get("show_title") or _strip_season_title(hit.title)),
        subtitle=str(hit.extra.get("show_subtitle") or ""),
        year=year_value,
        poster_url=hit.extra.get("show_poster") or None,
        url=f"https://www.themoviedb.org/tv/{show_id}",
        overview=str(hit.extra.get("show_overview") or ""),
        genres=[str(item) for item in genres if item],
        extra=extra,
    )
    return show, number


def season_media(show: MediaHit, number: int) -> MediaHit:
    info = next((item for item in tv_season_list(show) if int(item["n"]) == number), {})
    label = season_label(number)
    show_id = str(show.source_id).split(":s", 1)[0]
    poster = info.get("poster") or show.poster_url
    year = info.get("year") or show.year
    try:
        year_value = int(year) if year else None
    except (TypeError, ValueError):
        year_value = show.year
    overview = (info.get("overview") or "").strip() or show.overview
    extra = {
        key: value
        for key, value in show.extra.items()
        if key not in {"seasons", "runtime", "vote_average", "vote_count"}
    }
    extra.update({
        "show_id": show_id,
        "show_title": show.title,
        "show_subtitle": show.subtitle,
        "show_year": show.year,
        "show_poster": show.poster_url or "",
        "show_overview": show.overview,
        "show_genres": list(show.genres),
        "season": number,
        "episodes": int(info.get("episodes") or 0),
        "season_list": tv_season_list(show),
    })
    return MediaHit(
        source="tmdb",
        source_id=f"{show_id}:s{number}",
        media_type="tv",
        title=f"{show.title} — {label}",
        subtitle=show.subtitle,
        year=year_value,
        poster_url=poster or None,
        url=f"https://www.themoviedb.org/tv/{show_id}/season/{number}",
        overview=overview,
        genres=list(show.genres),
        extra=extra,
    )
