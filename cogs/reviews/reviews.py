"""Cog Critiques — carnet de notes type Senscritique / Letterboxd, par serveur."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .dyn import (
    ANNOUNCE_TTL,
    AnnounceDynButton,
    FicheDynButton,
    FicheRecord,
    bind_record,
    create_record,
    get_record,
    hit_from_dict,
    hit_to_dict,
    is_live,
    mark_stripped,
    sweep_expired,
    update_payload,
)
from .emojis import (
    BOOK,
    MORE,
    EXPLICIT,
    GAME,
    MOVIE,
    MUSIC,
    RIVAL,
    SALE,
    SHARE,
    STAR,
    STAR_EMPTY,
    STAR_HALF,
    TWIN,
    TV,
    XP,
)
from .progress import (
    Affinity,
    MIN_AFFINITY_OVERLAP,
    XpAward,
    agreement_percent,
    apply_daily_limits,
    compute_review_xp,
    level_for_xp,
    level_progress,
    title_for_level,
)
from .providers import MediaCatalog, MediaHit, parse_search_query
from utils import dataio, fuzzy, pretty

logger = logging.getLogger("CRIT.Reviews")

NO_PINGS = discord.AllowedMentions.none()


MENU_TIMEOUT = 840.0
EM_DASH = "\u2014"


def format_tab_label(label: str, *, index: int, total: int) -> str:
    """Onglets : emdash pour les distinguer des boutons d'action."""
    if total <= 1 or index == 0:
        return f"{label} {EM_DASH}"
    if index == total - 1:
        return f"{EM_DASH} {label}"
    return f"{EM_DASH} {label} {EM_DASH}"


def labeled_tabs(*labels: str) -> tuple[str, ...]:
    total = len(labels)
    return tuple(format_tab_label(label, index=i, total=total) for i, label in enumerate(labels))


def _disable_interactive(item: discord.ui.Item) -> None:
    if getattr(item, "disabled", None) is False:
        item.disabled = True  # type: ignore[attr-defined]
    children = getattr(item, "children", None)
    if children:
        for child in children:
            _disable_interactive(child)


def _remember_session_view(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    message_id: int | None,
) -> None:
    """Après un defer éphémère, l'id webhook peut différer de celui du clic."""
    if view.is_finished() or not view.is_dispatchable():
        return
    store = interaction.client._connection.store_view
    if message_id is not None:
        store(view, message_id)
    store(view, None)


def bind_view_message(
    view: discord.ui.LayoutView,
    message: discord.Message | discord.WebhookMessage | None,
) -> None:
    if message is None:
        return
    view.message = message
    if hasattr(view, "_message"):
        view._message = message


async def publish_layout_message(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    files: list[discord.File] | None = None,
) -> discord.Message | None:
    """Publie un layout dans le salon, hors webhook d'interaction."""
    channel = interaction.channel
    if channel is None:
        return None
    try:
        kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
        if files:
            kwargs["files"] = files
        return await channel.send(**kwargs)
    except (AttributeError, discord.HTTPException) as exc:
        logger.warning("Impossible de publier dans le salon : %s", exc)
        return None


async def discard_ephemeral_menu(interaction: discord.Interaction) -> None:
    """Supprime le menu éphémère qui a déclenché le partage."""
    try:
        if interaction.message is not None:
            await interaction.message.delete()
            return
    except discord.HTTPException:
        pass
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


async def apply_view(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    """Met à jour le message qui porte les boutons, pas un autre webhook."""
    kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
    message: discord.Message | discord.WebhookMessage | None = None
    if not interaction.response.is_done():
        await interaction.response.edit_message(**kwargs)
        message = interaction.message
    else:
        try:
            message = await interaction.edit_original_response(**kwargs)
        except discord.HTTPException:
            if interaction.message is None:
                raise
            message = await interaction.message.edit(**kwargs)
    bind_view_message(view, message or interaction.message)


class ReviewsLayout(discord.ui.LayoutView):
    """LayoutView CRIT : un Container (texte + ActionRows), comme MARIA."""

    def __init__(self, *, timeout: float | None = MENU_TIMEOUT):
        super().__init__(timeout=timeout)
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Vue %s / %s : %s", type(self).__name__, type(item).__name__, error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "**Erreur ·** Le bouton a planté. Réessaie après un `&reload reviews`.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "**Erreur ·** Le bouton a planté. Réessaie après un `&reload reviews`.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        for item in list(self.children):
            _disable_interactive(item)
        message = self.message or self._message
        if message is None:
            return
        try:
            await message.edit(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass

    async def attach(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        try:
            bind_view_message(self, await interaction.original_response())
        except discord.HTTPException:
            bind_view_message(self, interaction.message)

    async def push(self, interaction: discord.Interaction | None = None) -> None:
        try:
            if interaction is not None:
                await apply_view(interaction, self)
                mid = getattr(self.message, "id", None) or (
                    interaction.message.id if interaction.message else None
                )
                _remember_session_view(interaction, self, mid)
                return
            message = self.message or self._message
            if message is not None:
                await message.edit(view=self, allowed_mentions=NO_PINGS)
                return
            if self._interaction is not None:
                await apply_view(self._interaction, self)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir %s : %s", type(self).__name__, exc)

    def set_layout(self, body: list[discord.ui.Item], *rows: discord.ui.Item | None) -> None:
        self.clear_items()
        children = list(body)
        for row in rows:
            if row is None:
                continue
            if children:
                children.append(sep_tight())
            children.append(row)
        if children:
            self.add_item(discord.ui.Container(*children))

VALID_RATINGS = tuple(range(11))
RATING_MAX = 10
DEFAULT_COMMENT_MAX = 280
MIN_COMMENT_MAX = 50
MAX_COMMENT_MAX = 500
JOURNAL_PAGE = 4
REVIEWS_PAGE = 5
CATALOG_PAGE = 8
LIST_PAGE = 4
MAX_SHARED_LISTS = 20
MAX_LIST_ITEMS = 100
LIST_TITLE_MAX = 80
LIST_DESC_MAX = 200
LIST_EDIT_MODES = ("owner", "members", "public")
TEXT_DISPLAY_MAX = 4000

TYPE_META: dict[str, tuple[str, str]] = {
    "movie": (MOVIE, "Film"),
    "tv": (TV, "Série"),
    "game": (GAME, "Jeu"),
    "album": (MUSIC, "Album"),
    "track": (MUSIC, "Morceau"),
    "book": (BOOK, "Livre"),
}

TYPE_CHOICES = [
    app_commands.Choice(name="Tous les types", value="all"),
    app_commands.Choice(name="Film", value="movie"),
    app_commands.Choice(name="Série", value="tv"),
    app_commands.Choice(name="Jeu", value="game"),
    app_commands.Choice(name="Album", value="album"),
    app_commands.Choice(name="Morceau", value="track"),
    app_commands.Choice(name="Livre", value="book"),
]

ANNOUNCE_ROUTE_ALL = "all"
ANNOUNCE_ROUTE_ORDER = (ANNOUNCE_ROUTE_ALL, *TYPE_META)

PERIOD_SECONDS = {"semaine": 7 * 86400, "mois": 30 * 86400}

WHEN_CHOICES = [
    app_commands.Choice(name="N'importe quand", value="all"),
    app_commands.Choice(name="Ajoutée cette semaine", value="semaine"),
    app_commands.Choice(name="Ajoutée ce mois", value="mois"),
]

_TYPE_STATS = (
    ("movie", "film", "films"),
    ("tv", "série", "séries"),
    ("game", "jeu", "jeux"),
    ("album", "album", "albums"),
    ("track", "morceau", "morceaux"),
    ("book", "livre", "livres"),
)

FAVORITE_LABELS = {
    1: "Fétiche",
    2: "Coup de cœur",
    3: "Pépite",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_stars(rating: float) -> str:
    """Rangée de 5 étoiles : chaque étoile vaut 2 points, une demi vaut 1."""
    points = int(round(max(0.0, min(float(RATING_MAX), float(rating)))))
    full = points // 2
    half = points % 2 == 1
    empty = 5 - full - (1 if half else 0)
    return STAR * full + (STAR_HALF if half else "") + STAR_EMPTY * empty


def format_stars_compact(rating: float) -> str:
    """Une seule étoile custom + note, pour boutons et texte rich."""
    points = int(round(max(0.0, min(float(RATING_MAX), float(rating)))))
    if points <= 0:
        icon = STAR_EMPTY
    elif points % 2 == 1:
        icon = STAR_HALF
    else:
        icon = STAR
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


def experienced_verb(media_type: str) -> str:
    return {
        "movie": "Vu",
        "tv": "Vu",
        "game": "Joué",
        "album": "Écouté",
        "track": "Écouté",
        "book": "Lu",
    }.get(media_type, "Vu")


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


def experienced_to_input(raw: str) -> str:
    if not raw:
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
    total = 0.0
    this_year = 0
    year_now = date.today().year
    for hit, row in entries:
        counts[hit.media_type] = counts.get(hit.media_type, 0) + 1
        total += float(row["rating"])
        if entry_year(row) == year_now:
            this_year += 1
    parts: list[str] = []
    for key, singular, plural in _TYPE_STATS:
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else plural}")
    avg = total / len(entries)
    parts.append(f"moy. {avg:.1f}".replace(".", ","))
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
    avg = sum(score * n for score, n in enumerate(counts)) / len(entries)
    lines = [
        "### Répartition",
        f"-# moyenne **{format_score(avg, average=True)}**",
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


DATE_PREF_VALUES = ("empty", "today")


@dataclass(frozen=True)
class UserPrefs:
    default_date: str = "empty"
    default_list_edit: str = "owner"
    default_search_type: str = "all"
    announce_notes: bool = True


def today_experienced() -> str:
    return date.today().isoformat()


def note_experienced_default(prefs: UserPrefs, existing: dict[str, Any]) -> str:
    if existing:
        return str(existing.get("experienced_at") or "")
    return today_experienced() if prefs.default_date == "today" else ""


def note_spoiler_default(existing: dict[str, Any]) -> bool:
    return bool(existing.get("spoiler")) if "spoiler" in existing else False


def date_pref_label(value: str) -> str:
    return "Aujourd'hui" if value == "today" else "Vide"


def parse_search_types(value: str) -> tuple[str, ...]:
    if not value or value == "all":
        return ()
    chosen = {part.strip() for part in value.split(",")}
    return tuple(kind for kind in TYPE_META if kind in chosen)


def normalize_search_pref(value: Any) -> str:
    if value is None or value == "" or value == "all":
        return "all"
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        return "all"
    if "all" in parts:
        return "all"
    types = [kind for kind in TYPE_META if kind in parts]
    return ",".join(types) if types else "all"


def search_pref_label(value: str) -> str:
    types = parse_search_types(normalize_search_pref(value))
    if not types:
        return "Tous les types"
    return pretty.shorten_text(" · ".join(type_label(kind) for kind in types), 100)


def search_pref_includes(media_type: str, *kinds: str) -> bool:
    normalized = normalize_search_pref(media_type)
    if normalized == "all":
        return False
    chosen = set(parse_search_types(normalized))
    return any(kind in chosen for kind in kinds)


def announce_pref_label(value: bool) -> str:
    return "Publier" if value else "Ne pas annoncer"


def _row_field(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def prefs_from_row(row: Any | None) -> UserPrefs:
    if row is None:
        return UserPrefs()
    date_value = str(_row_field(row, "default_date", "empty") or "empty")
    edit_value = str(_row_field(row, "default_list_edit", "owner") or "owner")
    search_value = normalize_search_pref(_row_field(row, "default_search_type", "all") or "all")
    return UserPrefs(
        default_date=date_value if date_value in DATE_PREF_VALUES else "empty",
        default_list_edit=edit_value if edit_value in LIST_EDIT_MODES else "owner",
        default_search_type=search_value,
        announce_notes=bool(int(_row_field(row, "announce_notes", 1) or 0)),
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


def resolve_guild_text_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


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


def sep_tight() -> discord.ui.Separator:
    """Séparateur compact, entre les critiques."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def sep_wide() -> discord.ui.Separator:
    """Séparateur large, sous un titre."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def critiques_summary_line(
    hit: MediaHit,
    *,
    count: int,
    avg: float | None,
    page: int = 0,
    total_pages: int = 1,
) -> str:
    line = f"-# {count} critique(s) · moyenne {format_stars(avg or 0)} {format_score(avg or 0, average=True)}"
    if count:
        line += f" · page {page + 1}/{total_pages}"
    if hit.url:
        line += f"  ·  [{_link_label(hit)}]({hit.url})"
    return line


def section_with_thumbnail(text: str, url: str | None) -> discord.ui.Item:
    body = discord.ui.TextDisplay(text)
    if not url:
        return body
    try:
        return discord.ui.Section(body, accessory=discord.ui.Thumbnail(url))
    except Exception:
        return body


def chunk_text_displays(lines: list[str], *, limit: int = TEXT_DISPLAY_MAX) -> list[discord.ui.TextDisplay]:
    """Regroupe des lignes en TextDisplay sans dépasser la limite Discord."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        piece = line if len(line) <= limit else pretty.shorten_text(line, limit)
        addition = f"{current}\n{piece}" if current else piece
        if current and len(addition) > limit:
            chunks.append(current)
            current = piece
        else:
            current = addition
    if current:
        chunks.append(current)
    return [discord.ui.TextDisplay(chunk) for chunk in chunks]


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


def _user_display(guild: discord.Guild, bot: commands.Bot, user_id: int) -> tuple[str, str | None]:
    member = guild.get_member(user_id)
    if member:
        return member.display_name, member.display_avatar.url
    user = bot.get_user(user_id)
    if user:
        return user.display_name, user.display_avatar.url
    return f"Utilisateur {user_id}", None


def _mention(guild: discord.Guild, bot: commands.Bot, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.mention
    name, _avatar = _user_display(guild, bot, user_id)
    return f"**{name}**"


def _mention_silent(user_id: int) -> str:
    """Mention Discord ; le ping est coupé via AllowedMentions.none()."""
    return f"<@{user_id}>"


def _format_people_fr(mentions: list[str], extra: int = 0) -> str:
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


def _titled(mention: str, title: str) -> str:
    return f"{mention}\n{format_grade(title)}"


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


SOURCE_NAMES = {
    "tmdb": "TMDB",
    "steam": "Steam",
    "spotify": "Spotify",
    "openlibrary": "Open Library",
}


def _source_name(hit: MediaHit) -> str:
    return SOURCE_NAMES.get(hit.source, hit.source)


def _official_line(hit: MediaHit) -> str:
    source = _source_name(hit)
    if hit.source == "tmdb":
        rating = float(hit.extra.get("vote_average") or 0)
        count = int(hit.extra.get("vote_count") or 0)
        if rating:
            stars = format_stars(rating)
            votes = f"  ·  {_fmt_int(count)} votes" if count else ""
            return f"**{source}** · {stars}  **{rating:.1f}/10**{votes}"
    if hit.source == "steam":
        label = hit.extra.get("review_label") or ""
        emoji = hit.extra.get("review_emoji") or ""
        if label:
            return f"**{source}** · {f'{emoji} {label}'.strip()}"
    popularity = hit.extra.get("popularity")
    if hit.media_type == "track" and popularity:
        return f"**{source}** · Popularité **{popularity}/100**"
    return ""


def _price_line(hit: MediaHit) -> str:
    if hit.media_type != "game":
        return ""
    if hit.extra.get("is_free"):
        return "**Gratuit**"
    final = hit.extra.get("price_final")
    if not isinstance(final, int):
        return ""
    text = f"**{final / 100:.2f} €**"
    discount = hit.extra.get("discount") or 0
    initial = hit.extra.get("price_initial")
    if discount and isinstance(initial, int):
        text += f"  ~~{initial / 100:.2f} €~~  {SALE} **-{discount}%**"
    return text


def _runtime_label(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}" if hours else f"{mins} min"


def _title_line(hit: MediaHit) -> str:
    line = f"## {type_emoji(hit.media_type)} {hit.title}"
    if hit.year:
        line += f"  ·  {hit.year}"
    return line


def _meta_line(hit: MediaHit) -> str:
    parts = [type_label(hit.media_type)]
    if hit.subtitle:
        parts.append(hit.subtitle)
    parts.extend(hit.genres[:3])
    return "  ·  ".join(parts)


def _footer_line(hit: MediaHit) -> str:
    parts: list[str] = []
    extra = hit.extra
    if extra.get("runtime"):
        parts.append(_runtime_label(extra["runtime"]))
    if extra.get("seasons"):
        seasons = extra["seasons"]
        parts.append(f"{seasons} saison{'s' if seasons > 1 else ''}")
    if extra.get("director"):
        parts.append(extra["director"])
    created = extra.get("created_by") or []
    if created and not extra.get("director"):
        parts.append(", ".join(created[:2]))
    cast = extra.get("cast") or []
    if cast:
        parts.append(", ".join(cast))
    if extra.get("album"):
        parts.append(extra["album"])
    if extra.get("duration"):
        parts.append(extra["duration"])
    if extra.get("explicit"):
        parts.append(EXPLICIT)
    if extra.get("total_tracks"):
        tracks = extra["total_tracks"]
        parts.append(f"{tracks} piste{'s' if tracks > 1 else ''}")
    lang = extra.get("original_language") or ""
    if lang and lang != "fr":
        parts.append(lang.upper())
    if hit.url:
        parts.append(f"[{_source_name(hit)}]({hit.url})")
    elif hit.source:
        parts.append(_source_name(hit))
    return "  ·  ".join(parts)


def append_fiche_sections(
    body: list[discord.ui.Item],
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    my_review: dict | None,
    social_line: str = "",
) -> None:
    head: list[str] = []
    official = _official_line(hit)
    if official:
        head.append(official)
    if count:
        stars = format_stars(avg or 0)
        head.append(
            f"**Serveur** · {stars}  **{format_score(avg or 0, average=True)}**  ·  "
            f"{count} critique{'s' if count > 1 else ''}"
        )
    else:
        head.append("*Aucune note sur ce serveur pour l'instant.*")
    if my_review:
        comment = format_comment(
            my_review.get("comment") or "",
            spoiler=row_spoiler(my_review),
            hide=False,
            limit=180,
        )
        mine = f"Ta note · {format_stars(my_review['rating'])}  **{format_score(my_review['rating'])}**"
        if comment:
            mine += f"\n{comment}"
        seen = experienced_line(hit.media_type, experienced_from_row(my_review))
        if seen:
            mine += f"\n{seen}"
        head.append(mine)
    if social_line:
        head.append(f"-# {social_line}")
    price = _price_line(hit)
    if price:
        head.append(price)

    tail: list[str] = []
    overview = pretty.shorten_text(hit.overview, 380) if hit.overview else ""
    if overview:
        tail.append(overview)
    elif not official and not count:
        tail.append("-# Aucune description disponible.")

    body.append(section_with_thumbnail("\n".join(head), hit.poster_url))
    if tail:
        body.append(discord.ui.Separator())
        body.append(discord.ui.TextDisplay("\n".join(tail)))


def fiche_intro(hit: MediaHit) -> list[discord.ui.Item]:
    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(f"{_title_line(hit)}\n-# {_meta_line(hit)}"),
        sep_tight(),
    ]
    backdrop = hit.extra.get("backdrop_url")
    if backdrop:
        try:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(backdrop)))
        except Exception:
            pass
    return items


def _link_label(hit: MediaHit) -> str:
    return SOURCE_NAMES.get(hit.source, "Fiche")


def render_published_fiche(
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    social: str,
    wid: str,
    live: bool,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    body: list[discord.ui.Item] = []
    body.extend(fiche_intro(hit))
    append_fiche_sections(body, hit, avg=avg, count=count, my_review=None, social_line=social)
    footer = _footer_line(hit)
    if footer:
        body.append(discord.ui.Separator())
        body.append(discord.ui.TextDisplay(f"-# {footer}"))
    if live:
        body.append(discord.ui.ActionRow(
            FicheDynButton(wid, "critiques", label=f"Critiques ({count})"),
            FicheDynButton(wid, "noter", emoji=MORE, style=discord.ButtonStyle.green),
        ))
    view.add_item(discord.ui.Container(*body))
    return view


def render_published_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    return render_published_fiche(
        hit_from_dict(raw),
        avg=rec.payload.get("avg"),
        count=int(rec.payload.get("count") or 0),
        social=str(rec.payload.get("social") or ""),
        wid=rec.id,
        live=live,
    )


async def send_published_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    interaction: discord.Interaction,
    *,
    close_ephemeral: bool = False,
) -> discord.Message | None:
    if cog.catalog is not None:
        try:
            hit = await cog.catalog.enrich(hit)
        except Exception:
            logger.exception("Enrichissement de fiche publiée impossible")
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
    wid = create_record({
        "kind": "fiche",
        "guild_id": guild.id,
        "hit": hit_to_dict(hit),
        "avg": avg,
        "count": count,
        "social": social,
    })
    view = render_published_fiche(hit, avg=avg, count=count, social=social, wid=wid, live=True)
    message = await publish_layout_message(interaction, view)
    if message is None:
        try:
            await interaction.followup.send(
                "**Erreur ·** Impossible de publier dans ce salon.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return None
    bind_record(wid, message.channel.id, message.id)
    if close_ephemeral:
        await discard_ephemeral_menu(interaction)
    return message


async def sync_published_fiche(cog: "Reviews", guild: discord.Guild, wid: str, hit: MediaHit) -> None:
    rec = get_record(wid)
    if not is_live(rec) or rec is None:
        return
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
    rec.payload.update({"hit": hit_to_dict(hit), "avg": avg, "count": count, "social": social})
    update_payload(wid, rec.payload)
    view = render_published_fiche(hit, avg=avg, count=count, social=social, wid=wid, live=True)
    if not rec.channel_id or not rec.message_id:
        return
    try:
        channel = cog.bot.get_channel(rec.channel_id) or await cog.bot.fetch_channel(rec.channel_id)
        message = await channel.fetch_message(rec.message_id)
        await message.edit(view=view, allowed_mentions=NO_PINGS)
    except Exception as exc:
        logger.info("Maj fiche publiée %s : %s", wid, exc)


async def send_ephemeral_menu(interaction: discord.Interaction, view: ReviewsLayout) -> None:
    """Nouveau message éphémère — ne jamais éditer la fiche publique."""
    view._interaction = interaction
    if interaction.response.is_done():
        message = await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
        bind_view_message(view, message)
        return
    await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
    await view.attach(interaction)


async def open_personal_hit_menu(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    published_wid: str | None,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    menu = await MyNoteView.create(cog, guild, hit, interaction.user.id, published_wid=published_wid)
    await send_ephemeral_menu(interaction, menu)


async def handle_published_fiche_click(
    interaction: discord.Interaction,
    wid: str,
    action: str,
) -> None:
    rec = get_record(wid)
    if not is_live(rec):
        if rec is not None:
            view = render_published_record(rec, live=False)
            if view is not None:
                try:
                    await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    pass
            mark_stripped(rec.id)
        if not interaction.response.is_done():
            await interaction.response.send_message("Les boutons ont expiré.", ephemeral=True)
        return
    cog = interaction.client.get_cog("Reviews")
    guild = interaction.guild
    raw = rec.payload.get("hit") if rec else None
    if cog is None or not isinstance(guild, discord.Guild) or not isinstance(raw, dict):
        await interaction.response.send_message(
            "**Erreur ·** Impossible d'ouvrir ce menu.",
            ephemeral=True,
        )
        return
    hit = hit_from_dict(raw)
    if action in {"noter", "voir"}:
        await open_personal_hit_menu(interaction, cog, guild, hit, published_wid=wid)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    if action == "critiques":
        menu = await PublicCritiquesView.create(cog, guild, hit, interaction.user.id)
    else:
        menu = await PublicFichePeekView.create(cog, guild, hit)
    await send_ephemeral_menu(interaction, menu)


async def handle_announce_click(
    interaction: discord.Interaction,
    wid: str,
    action: str,
) -> None:
    rec = get_record(wid)
    if not is_live(rec):
        if rec is not None:
            view = render_announce_record(rec, live=False)
            if view is not None:
                try:
                    await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    pass
            mark_stripped(rec.id)
        if not interaction.response.is_done():
            await interaction.response.send_message("Les boutons ont expiré.", ephemeral=True)
        return
    cog = interaction.client.get_cog("Reviews")
    guild = interaction.guild
    raw = rec.payload.get("hit") if rec else None
    if cog is None or not isinstance(guild, discord.Guild) or not isinstance(raw, dict):
        await interaction.response.send_message(
            "**Erreur ·** Impossible d'ouvrir ce menu.",
            ephemeral=True,
        )
        return
    if action != "noter":
        await interaction.response.send_message("**Erreur ·** Action inconnue.", ephemeral=True)
        return
    await open_personal_hit_menu(
        interaction, cog, guild, hit_from_dict(raw), published_wid=None,
    )


async def open_public_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    interaction: discord.Interaction,
    hit: MediaHit,
) -> None:
    await send_published_fiche(cog, guild, hit, interaction)


async def open_session_followup(
    cog: "Reviews",
    guild: discord.Guild,
    interaction: discord.Interaction,
    hit: MediaHit,
    *,
    author_id: int,
) -> None:
    view = MediaSessionView(cog, guild, [hit], author_id=author_id, ephemeral=True)
    await view.prepare()
    view._interaction = interaction
    message = await interaction.followup.send(
        view=view, ephemeral=True, allowed_mentions=NO_PINGS,
    )
    bind_view_message(view, message)


# ---------------------------------------------------------------------------
# Annonce publique (présentation seule)
# ---------------------------------------------------------------------------

def build_announce_view(
    hit: MediaHit,
    *,
    mention: str,
    title: str,
    rating: float,
    comment: str,
    updated: bool,
    experienced_at: str = "",
    spoiler: bool = False,
    posted_at: int | None = None,
    wid: str | None = None,
    live: bool = False,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    verb = "a mis à jour sa note" if updated else "a noté"
    stamped = f"<t:{int(posted_at or time.time())}:f>"
    profile = f"{mention} · _{title}_ · le {stamped} · {verb}"
    film = f"{_title_line(hit)}\n{format_stars(rating)}  **{format_score(rating)}**"
    shown = format_comment(comment, spoiler=spoiler, hide=True, limit=240)
    if shown:
        film += f"\n{shown}"
    seen = experienced_line(hit.media_type, experienced_at)
    if seen:
        film += f"\n{seen}"
    container.add_item(discord.ui.TextDisplay(profile))
    container.add_item(discord.ui.Separator())
    container.add_item(section_with_thumbnail(film, hit.poster_url))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(f"-# {_meta_line(hit)}" + (f"  ·  [{_link_label(hit)}]({hit.url})" if hit.url else "")))
    view.add_item(container)
    if live and wid:
        view.add_item(discord.ui.ActionRow(AnnounceDynButton(wid, label="Actions")))
    return view


def render_announce_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    posted = rec.payload.get("posted_at")
    try:
        posted_at = int(posted) if posted else None
    except (TypeError, ValueError):
        posted_at = None
    return build_announce_view(
        hit_from_dict(raw),
        mention=str(rec.payload.get("mention") or ""),
        title=str(rec.payload.get("title") or ""),
        rating=float(rec.payload.get("rating") or 0),
        comment=str(rec.payload.get("comment") or ""),
        updated=bool(rec.payload.get("updated")),
        experienced_at=str(rec.payload.get("experienced_at") or ""),
        spoiler=bool(rec.payload.get("spoiler")),
        posted_at=posted_at,
        wid=rec.id,
        live=live,
    )


def render_dyn_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    if rec.payload.get("kind") == "announce":
        return render_announce_record(rec, live=live)
    return render_published_record(rec, live=live)


# ---------------------------------------------------------------------------
# Modal de notation
# ---------------------------------------------------------------------------

def _review_saved_lines(hit: MediaHit, rating: float, created: bool, award: XpAward) -> list[str]:
    verb = "enregistrée" if created else "mise à jour"
    parts = [f"**Critique {verb} ·** {format_stars(rating)}  **{format_score(rating)}** — {hit.title}."]
    if award.gained:
        parts.append(f"{XP} +{award.gained} · niveau {award.level}")
        if award.capped:
            parts.append("(plafond quotidien atteint)")
    elif award.capped:
        parts.append("Plafond d'XP quotidien atteint.")
    if award.leveled_up:
        new_title = title_for_level(award.level)
        old_title = title_for_level(award.previous_level)
        parts.append(f"Nouveau titre · {new_title}" if new_title != old_title else f"Niveau {award.level}")
    return parts


class RateModal(discord.ui.Modal, title="Noter cette œuvre"):
    def __init__(
        self,
        parent: Any,
        *,
        max_comment: int,
        default_rating: float | None,
        default_comment: str,
        default_experienced: str = "",
        default_spoiler: bool = False,
    ):
        super().__init__()
        self._hub = parent
        self.rating_input = discord.ui.TextInput(
            label="Note (0 à 10, entier)",
            placeholder="Ex. 8",
            default="" if default_rating is None else f"{int(round(default_rating))}",
            max_length=2,
            required=True,
        )
        self.comment_input = discord.ui.TextInput(
            label="Commentaire (optionnel)",
            style=discord.TextStyle.paragraph,
            placeholder="Un avis court…",
            default=default_comment[:max_comment] if default_comment else None,
            max_length=max_comment,
            required=False,
        )
        self.date_input = discord.ui.TextInput(
            label="Date (optionnel)",
            placeholder="Ex. 12/03/2024 — vu, joué, écouté ou lu",
            default=experienced_to_input(default_experienced) or None,
            max_length=12,
            required=False,
        )
        self.spoiler_check = discord.ui.Checkbox(custom_id="spoiler", default=bool(default_spoiler))
        self.add_item(self.rating_input)
        self.add_item(self.comment_input)
        self.add_item(self.date_input)
        self.add_item(
            discord.ui.Label(
                text="Spoiler",
                description="Masque le commentaire en public",
                component=self.spoiler_check,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = parse_rating(self.rating_input.value)
        if rating is None:
            await interaction.response.send_message(
                "**Erreur ·** La note doit être un entier entre 0 et 10 (ex. `8`).",
                ephemeral=True,
            )
            return
        experienced_at, date_error = parse_experienced_date(str(self.date_input.value or ""))
        if date_error:
            await interaction.response.send_message(f"**Erreur ·** {date_error}", ephemeral=True)
            return
        from_public = bool(self._hub.from_published_modal)
        orphan = from_public or (self._hub._interaction is None and self._hub._message is None)
        await interaction.response.defer(ephemeral=orphan or from_public)
        if orphan and not from_public:
            self._hub._interaction = interaction
        await self._hub.save_review(
            interaction,
            rating,
            str(self.comment_input.value or "").strip(),
            experienced_at or "",
            spoiler=bool(self.spoiler_check.value),
        )


class MyNoteEditButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(
            label="Modifier" if parent.my_review else "Ajouter une note",
            style=discord.ButtonStyle.secondary if parent.my_review else discord.ButtonStyle.green,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        existing = self._hub.my_review or {}
        prefs = getattr(self._hub, "prefs", None) or UserPrefs()
        await interaction.response.send_modal(
            RateModal(
                self._hub,
                max_comment=self._hub.cog.cached_comment_max(self._hub.guild),
                default_rating=existing.get("rating"),
                default_comment=existing.get("comment") or "",
                default_experienced=note_experienced_default(prefs, existing),
                default_spoiler=note_spoiler_default(existing),
            )
        )


class MyNoteDeleteButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.red)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.delete_review(interaction)


class MyNoteView(ReviewsLayout):
    """Menu éphémère : uniquement la note du clicqueur."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        my_review: dict | None,
        published_wid: str | None,
        prefs: UserPrefs | None = None,
        on_watchlist: bool = False,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.my_review = my_review
        self.published_wid = published_wid
        self.prefs = prefs or UserPrefs()
        self.on_watchlist = on_watchlist and not bool(my_review)
        self.from_published_modal = False
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
        *,
        published_wid: str | None,
    ) -> "MyNoteView":
        media_id = await cog.lookup_media_id(guild, hit)
        mine = await cog.get_review(guild, author_id, media_id) if media_id else None
        on_watchlist = (
            bool(media_id)
            and mine is None
            and await cog.is_on_watchlist(guild, author_id, media_id)
        )
        await cog.get_comment_max(guild)
        prefs = await cog.get_user_prefs(guild, author_id)
        return cls(
            cog,
            guild,
            hit,
            author_id=author_id,
            my_review=mine,
            published_wid=published_wid,
            prefs=prefs,
            on_watchlist=on_watchlist,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne concerne que ta note.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        mine = self.my_review
        if mine:
            text = (
                f"{_title_line(hit)}\n"
                f"{format_stars(mine['rating'])}  **{format_score(mine['rating'])}**\n"
            )
            shown = format_comment(
                mine.get("comment") or "",
                spoiler=row_spoiler(mine),
                hide=False,
                limit=240,
            )
            if shown:
                text += f"{shown}\n"
            seen = experienced_line(hit.media_type, mine.get("experienced_at"))
            if seen:
                text += f"{seen}\n"
            text += f"-# Ta note · {_meta_line(hit)}"
        else:
            text = (
                f"{_title_line(hit)}\n"
                f"*Tu n'as pas encore noté cette œuvre.*\n"
                f"-# {_meta_line(hit)}"
            )
        actions: list[discord.ui.Item] = [MyNoteEditButton(self)]
        if mine:
            actions.append(MyNoteDeleteButton(self))
        actions.append(WatchlistButton(self))
        self.set_layout([section_with_thumbnail(text, hit.poster_url)], discord.ui.ActionRow(*actions))

    async def save_review(
        self,
        interaction: discord.Interaction,
        rating: float,
        comment: str,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> None:
        created, award = await self.cog.upsert_review(
            self.guild,
            interaction.user,
            self.hit,
            rating,
            comment,
            experienced_at=experienced_at,
            spoiler=spoiler,
        )
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id) if media_id else None
        self.on_watchlist = False
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        await interaction.followup.send("\n".join(_review_saved_lines(self.hit, rating, created, award)), ephemeral=True)
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at, spoiler=spoiler,
        )

    async def delete_review(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_review(self.guild, self.author_id, self.hit)
        self.my_review = None
        self.on_watchlist = False
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        if media_id:
            self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id)
            self.on_watchlist = (not self.my_review) and await self.cog.is_on_watchlist(
                self.guild, self.author_id, media_id
            )
        else:
            self.my_review = None
            self.on_watchlist = False
        self._build()
        if interaction is not None:
            await apply_view(interaction, self)


class PublicFichePeekView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Fiche public."""

    def __init__(self, hit: MediaHit, *, avg: float | None, count: int, social: str):
        super().__init__()
        self._interaction: discord.Interaction | None = None
        body: list[discord.ui.Item] = []
        body.extend(fiche_intro(hit))
        append_fiche_sections(body, hit, avg=avg, count=count, my_review=None, social_line=social)
        footer = _footer_line(hit)
        if footer:
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay(f"-# {footer}"))
        self.set_layout(body)

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, hit: MediaHit) -> "PublicFichePeekView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        social = cog.social_line_for_reviews(guild, reviews, viewer_id=None)
        return cls(hit, avg=avg, count=count, social=social)


class PublicCritiquesPageButton(discord.ui.Button):
    def __init__(self, parent: "PublicCritiquesView", delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.page = max(0, self._hub.page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)


class PublicCritiquesView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Critiques public."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        reviews: list[Any],
        avg: float | None,
        count: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.reviews = reviews
        self.avg = avg
        self.count = count
        self.page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
    ) -> "PublicCritiquesView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        return cls(cog, guild, hit, author_id=author_id, reviews=reviews, avg=avg, count=count)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = list(fiche_intro(hit)[:2])
        rows: list[discord.ui.ActionRow] = []
        total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE) if self.reviews else 1
        if self.reviews:
            max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
            self.page = min(self.page, max_page)
        body.append(discord.ui.TextDisplay(critiques_summary_line(
            hit, count=self.count, avg=self.avg, page=self.page, total_pages=total_pages,
        )))
        body.append(sep_tight())
        if not self.reviews:
            body.append(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
            self.set_layout(body)
            return
        start = self.page * REVIEWS_PAGE
        page_rows = self.reviews[start:start + REVIEWS_PAGE]
        for index, row in enumerate(page_rows):
            if index:
                body.append(sep_tight())
            user_id = int(row["user_id"])
            _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
            text = (
                f"{_mention(self.guild, self.cog.bot, user_id)}\n"
                f"{format_stars(row['rating'])}  **{format_score(row['rating'])}** · <t:{row['updated_at']}:R>"
            )
            shown = format_comment(
                row["comment"] or "",
                spoiler=row_spoiler(row),
                hide=True,
                limit=220,
            )
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar))
        max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
        if max_page > 0:
            prev_btn = PublicCritiquesPageButton(self, -1, "← Précédent")
            next_btn = PublicCritiquesPageButton(self, 1, "Suivant →")
            prev_btn.disabled = self.page <= 0
            next_btn.disabled = self.page >= max_page
            rows.append(discord.ui.ActionRow(prev_btn, next_btn))
        self.set_layout(body, *rows)


# ---------------------------------------------------------------------------
# Vue session (sélection + fiche + critiques)
# ---------------------------------------------------------------------------

class MediaSelect(discord.ui.Select):
    def __init__(self, parent: "MediaSessionView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(
            placeholder="Choisir une œuvre",
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"crit:sess:{id(parent)}:pick",
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.selected = int(self.values[0])
        self._hub.review_page = 0
        self._hub.tab = "fiche"
        await self._hub.show_selected(interaction)


class TabButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView", tab: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.tab = self._tab
        self._hub.review_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


class RateButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Noter", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._hub
        existing = parent.my_review if interaction.user.id == parent.author_id else None
        pending = parent.pending_rating
        prefs = parent.prefs or UserPrefs()
        if pending is not None and existing is None and interaction.user.id == parent.author_id:
            await interaction.response.defer()
            await parent.save_review(
                interaction,
                pending,
                parent.pending_comment,
                experienced_at=note_experienced_default(prefs, {}),
                spoiler=False,
            )
            return
        existing = existing or {}
        await interaction.response.send_modal(
            RateModal(
                parent,
                max_comment=parent.cog.cached_comment_max(parent.guild),
                default_rating=existing.get("rating", parent.pending_rating if interaction.user.id == parent.author_id else None),
                default_comment=existing.get("comment") or (parent.pending_comment if interaction.user.id == parent.author_id else ""),
                default_experienced=note_experienced_default(prefs, existing),
                default_spoiler=note_spoiler_default(existing),
            )
        )


class WatchlistButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        rated = bool(parent.my_review)
        on = bool(parent.on_watchlist) and not rated
        super().__init__(
            label='Retirer « À voir »' if on else "À voir",
            style=discord.ButtonStyle.secondary if (on or rated) else discord.ButtonStyle.primary,
            disabled=rated,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._hub.my_review:
            await interaction.response.send_message(
                "**Déjà noté ·** Cette œuvre n'est plus dans ta liste à voir.",
                ephemeral=True,
                delete_after=8,
            )
            return
        await interaction.response.defer()
        if self._hub.on_watchlist:
            media_id = await self._hub.cog.lookup_media_id(self._hub.guild, self._hub.hit)
            if media_id:
                await self._hub.cog.remove_watchlist(self._hub.guild, interaction.user.id, media_id)
        else:
            await self._hub.cog.add_watchlist(self._hub.guild, interaction.user.id, self._hub.hit)
        await self._hub.refresh(interaction)


class DeleteReviewButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.red)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.cog.delete_review(self._hub.guild, interaction.user.id, self._hub.hit)
        self._hub.my_review = None
        await self._hub.reload_stats()
        if self._hub.published_wid:
            await sync_published_fiche(self._hub.cog, self._hub.guild, self._hub.published_wid, self._hub.hit)
        await self._hub.refresh(interaction)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)


def _share_button_kwargs() -> dict[str, Any]:
    return {
        "label": "Partager",
        "style": discord.ButtonStyle.secondary,
        "emoji": discord.PartialEmoji.from_str(SHARE),
    }


class FicheShareButton(discord.ui.Button):
    def __init__(self, parent: "MediaSessionView"):
        super().__init__(**_share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        message = await send_published_fiche(
            self._hub.cog,
            self._hub.guild,
            self._hub.hit,
            interaction,
            close_ephemeral=True,
        )
        if message is not None:
            self._hub.stop()


class ProfileShareButton(discord.ui.Button):
    def __init__(self, parent: "ProfileView"):
        super().__init__(**_share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        was_editable = self._hub.editable
        self._hub.editable = False
        body, _rows = self._hub._profil_layout()
        self._hub.editable = was_editable
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body))
        message = await publish_layout_message(interaction, view)
        if message is None:
            await interaction.followup.send("**Erreur ·** Impossible de publier ce profil.", ephemeral=True)
            return
        self._hub.stop()
        await discard_ephemeral_menu(interaction)


class HubTabButton(discord.ui.Button):
    def __init__(self, parent: Any, tab: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.tab = self._tab
        self._hub._build()
        await apply_view(interaction, self._hub)


class HubPageButton(discord.ui.Button):
    def __init__(self, parent: Any, attr: str, delta: int, label: str, max_page: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._attr = attr
        self._delta = delta
        self._max_page = max_page

    async def callback(self, interaction: discord.Interaction) -> None:
        current = getattr(self._hub, self._attr)
        setattr(self._hub, self._attr, max(0, min(self._max_page, current + self._delta)))
        self._hub._build()
        await apply_view(interaction, self._hub)


class MediaSessionView(ReviewsLayout):
    """Recherche → fiche → critiques, dans une seule vue interactive."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
        ephemeral: bool,
        pending_rating: float | None = None,
        pending_comment: str = "",
        selected: int = 0,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.ephemeral = ephemeral
        self.pending_rating = pending_rating
        self.pending_comment = pending_comment
        self.selected = selected
        self.tab = "fiche"
        self.review_page = 0
        self.avg: float | None = None
        self.count = 0
        self.my_review: dict | None = None
        self.on_watchlist = False
        self.reviews: list[Any] = []
        self.social_line = ""
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self.published_wid: str | None = None
        self.from_published_modal = False
        self.prefs = UserPrefs()
        self._enriched: set[int] = set()

    @property
    def hit(self) -> MediaHit:
        return self.hits[self.selected]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ephemeral and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def prepare(self) -> None:
        await self.cog.get_comment_max(self.guild)
        self.prefs = await self.cog.get_user_prefs(self.guild, self.author_id)
        await self.enrich_selected()
        self._enriched.add(self.selected)
        await self.reload_stats()
        self._build()

    async def enrich_selected(self) -> None:
        if self.cog.catalog is None:
            return
        try:
            self.hits[self.selected] = await self.cog.catalog.enrich(self.hit)
        except Exception:
            logger.exception("Enrichissement de fiche impossible")

    async def reload_stats(self) -> None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.avg, self.count = await self.cog.media_stats(self.guild, media_id) if media_id else (None, 0)
        self.reviews = await self.cog.list_reviews(self.guild, media_id) if media_id else []
        self.my_review = None
        self.on_watchlist = False
        if media_id and self.ephemeral:
            self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id)
            self.on_watchlist = await self.cog.is_on_watchlist(self.guild, self.author_id, media_id)
        self.social_line = self.cog.social_line_for_reviews(
            self.guild,
            self.reviews,
            viewer_id=self.author_id if self.ephemeral else None,
        )

    async def save_review(
        self,
        interaction: discord.Interaction,
        rating: float,
        comment: str,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> None:
        created, award = await self.cog.upsert_review(
            self.guild,
            interaction.user,
            self.hit,
            rating,
            comment,
            experienced_at=experienced_at,
            spoiler=spoiler,
        )
        await self.reload_stats()
        self.pending_rating = None
        self.tab = "fiche"
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        if not self.from_published_modal:
            await self.refresh(interaction)
        await interaction.followup.send("\n".join(_review_saved_lines(self.hit, rating, created, award)), ephemeral=True)
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at, spoiler=spoiler,
        )

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = []
        rows: list[discord.ui.ActionRow] = []

        if len(self.hits) > 1:
            body.append(discord.ui.TextDisplay(f"### Résultats · {len(self.hits)} œuvre(s)"))
            if not any(item.source == "tmdb" for item in self.hits) and any(
                item.source == "spotify" for item in self.hits
            ):
                if self.cog.catalog is not None and not self.cog.catalog.tmdb.available:
                    body.append(discord.ui.TextDisplay("-# Films et séries absents · clé TMDB manquante."))
                else:
                    body.append(discord.ui.TextDisplay(
                        "-# Aucun film ou série trouvé — précise le type si besoin."
                    ))
            body.append(discord.ui.ActionRow(MediaSelect(self, self.hits, self.selected)))
            body.append(discord.ui.Separator())

        if self.tab == "fiche":
            body.extend(fiche_intro(hit))
            append_fiche_sections(
                body,
                hit,
                avg=self.avg,
                count=self.count,
                my_review=self.my_review if self.ephemeral and not self.published_wid else None,
                social_line=self.social_line,
            )
            footer = _footer_line(hit)
            if footer:
                body.append(discord.ui.Separator())
                body.append(discord.ui.TextDisplay(f"-# {footer}"))
        else:
            body.extend(fiche_intro(hit)[:2])
            total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE) if self.reviews else 1
            if self.reviews:
                max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
                self.review_page = min(self.review_page, max_page)
            body.append(discord.ui.TextDisplay(critiques_summary_line(
                hit, count=self.count, avg=self.avg, page=self.review_page, total_pages=total_pages,
            )))
            body.append(sep_tight())
            if not self.reviews:
                body.append(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
            else:
                start = self.review_page * REVIEWS_PAGE
                page_rows = self.reviews[start:start + REVIEWS_PAGE]
                for index, row in enumerate(page_rows):
                    if index:
                        body.append(sep_tight())
                    user_id = int(row["user_id"])
                    _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
                    text = (
                        f"{_mention(self.guild, self.cog.bot, user_id)}\n"
                        f"{format_stars(row['rating'])}  **{format_score(row['rating'])}** · <t:{row['updated_at']}:R>"
                    )
                    shown = format_comment(
                        row["comment"] or "",
                        spoiler=row_spoiler(row),
                        hide=True,
                        limit=220,
                    )
                    if shown:
                        text += f"\n{shown}"
                    seen = experienced_line(hit.media_type, experienced_from_row(row))
                    if seen:
                        text += f"\n{seen}"
                    body.append(section_with_thumbnail(text, avatar))

        fiche_tab, critiques_tab = labeled_tabs("Fiche", f"Critiques ({self.count})")
        rows.append(discord.ui.ActionRow(
            TabButton(self, "fiche", fiche_tab),
            TabButton(self, "critiques", critiques_tab),
        ))
        if not self.published_wid:
            rate_label = "Noter"
            if self.ephemeral and self.pending_rating is not None and self.my_review is None:
                rate_label = f"Noter {format_stars_compact(self.pending_rating)}"
            elif self.ephemeral and self.my_review:
                rate_label = "Modifier ma note"
            rate_btn = RateButton(self)
            rate_btn.label = rate_label
            actions: list[discord.ui.Item] = [rate_btn]
            if self.ephemeral and self.my_review:
                actions.append(DeleteReviewButton(self))
            if self.ephemeral:
                actions.append(WatchlistButton(self))
            rows.append(discord.ui.ActionRow(*actions[:5]))
        if self.tab == "critiques" and len(self.reviews) > REVIEWS_PAGE:
            nav_btns: list[discord.ui.Item] = []
            if self.review_page > 0:
                nav_btns.append(_ReviewPageButton(self, -1, "← Précédent"))
            if (self.review_page + 1) * REVIEWS_PAGE < len(self.reviews):
                nav_btns.append(_ReviewPageButton(self, 1, "Suivant →"))
            if nav_btns:
                rows.append(discord.ui.ActionRow(*nav_btns))
        self.set_layout(body, *rows)
        if not self.published_wid:
            self.add_item(discord.ui.ActionRow(FicheShareButton(self)))

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self.reload_stats()
        self._build()
        await self.push(interaction)

    async def show_selected(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        if self.selected not in self._enriched:
            await self.enrich_selected()
            self._enriched.add(self.selected)
        await self.reload_stats()
        self._build()
        await self.push(interaction)

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False) -> None:
        self._interaction = interaction
        await self.prepare()
        if deferred:
            message = await interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
            bind_view_message(self, message)
            _remember_session_view(interaction, self, getattr(message, "id", None))
            return
        await interaction.response.send_message(
            view=self, ephemeral=self.ephemeral, allowed_mentions=NO_PINGS
        )
        await self.attach(interaction)


class _ReviewPageButton(discord.ui.Button):
    def __init__(self, parent: MediaSessionView, delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.review_page = max(0, self._hub.review_page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)


# ---------------------------------------------------------------------------
# Sélections partagées (profil / explorateur)
# ---------------------------------------------------------------------------

class JournalTypeSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=parent.journal_type == choice.value,
            )
            for choice in TYPE_CHOICES
        ]
        super().__init__(placeholder="Type", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.journal_type = self.values[0]
        self._hub.journal_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


class JournalSortSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(label="Plus récentes", value="recent", default=parent.journal_sort == "recent"),
            discord.SelectOption(label="Mieux notées", value="rating", default=parent.journal_sort == "rating"),
        ]
        super().__init__(placeholder="Trier", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.journal_sort = self.values[0]
        self._hub.journal_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


class WatchlistOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description=pretty.shorten_text(f"{type_label(hit.media_type)} · {hit.year or '—'}", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, _row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        if self._hub.editable:
            await open_session_followup(
                self._hub.cog, self._hub.guild, interaction, hit, author_id=interaction.user.id,
            )
            return
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class WatchlistRemoveSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description="Retirer de la liste",
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, _row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Retirer de la liste", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._hub.member.id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul le propriétaire du carnet peut modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        hit, _row = self._items[int(self.values[0])]
        media_id = await self._hub.cog.lookup_media_id(self._hub.guild, hit)
        if media_id:
            await self._hub.cog.remove_watchlist(self._hub.guild, self._hub.member.id, media_id)
        await self._hub.refresh(interaction)


class CatalogOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView", page_items: list[tuple[MediaHit, float, int]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(
                    f"{type_label(hit.media_type)} · {format_stars_select(avg)}/{RATING_MAX} · {count} note{'s' if count > 1 else ''}",
                    95,
                ),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, avg, count) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _avg, _count = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class RecentOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(f"{format_stars_select(row['rating'])}/{RATING_MAX} · {type_label(hit.media_type)}", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class AffinityCompareSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(parent._name(item.user_id), 95),
                value=str(item.user_id),
                description=pretty.shorten_text(
                    f"{item.percent:.0f} % · {item.overlap} œuvre{'s' if item.overlap > 1 else ''} en commun",
                    95,
                ),
            )
            for item in parent.affinities[:25]
        ]
        super().__init__(placeholder="Comparer avec…", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        other_id = int(self.values[0])
        view = AffinityCompareView(
            self._hub.cog,
            self._hub.guild,
            self._hub.member.id,
            other_id,
            affinity=next(a for a in self._hub.affinities if a.user_id == other_id),
            titles=self._hub.titles,
        )
        view._interaction = interaction
        bind_view_message(view, await interaction.followup.send(view=view))


class AffinityCompareView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        left_id: int,
        right_id: int,
        *,
        affinity: Affinity,
        titles: dict[int, str],
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.left_id = left_id
        self.right_id = right_id
        self.affinity = affinity
        self.titles = titles
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _build(self) -> None:
        _left_name, left_avatar = _user_display(self.guild, self.cog.bot, self.left_id)
        body: list[discord.ui.Item] = [
            section_with_thumbnail(
                f"{self._person(self.left_id)}\n{self._person(self.right_id)}\n"
                f"-# {self.affinity.percent:.0f} % d'accord  ·  {self.affinity.overlap} œuvre(s) en commun",
                left_avatar,
            ),
        ]
        if self.affinity.agreements:
            lines = ["**D'accord**"]
            for title, left, right in self.affinity.agreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        if self.affinity.disagreements:
            lines = ["**Désaccord**"]
            for title, left, right in self.affinity.disagreements:
                lines.append(f"{format_stars_compact(left)} / {format_stars_compact(right)}  ·  {title}")
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        self.set_layout(body)


# ---------------------------------------------------------------------------
# Listes communes
# ---------------------------------------------------------------------------

class CreateSharedListModal(discord.ui.Modal, title="Nouvelle liste"):
    def __init__(self, parent: "ListsHubView"):
        super().__init__()
        self._hub = parent
        self.title_input = discord.ui.TextInput(
            label="Titre",
            placeholder="Ex. Soirée Horreur",
            max_length=LIST_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optionnel)",
            style=discord.TextStyle.paragraph,
            placeholder="Une courte phrase pour présenter la liste",
            max_length=LIST_DESC_MAX,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message("**Erreur ·** Donne un titre à la liste.", ephemeral=True)
            return
        await interaction.response.defer()
        owned = await self._hub.cog.count_owned_lists(self._hub.guild, interaction.user.id)
        if owned >= MAX_SHARED_LISTS:
            await interaction.followup.send(
                f"**Erreur ·** Tu as déjà {MAX_SHARED_LISTS} listes sur ce serveur.",
                ephemeral=True,
            )
            return
        record = await self._hub.cog.create_shared_list(
            self._hub.guild,
            interaction.user.id,
            title,
            str(self.desc_input.value or "").strip(),
        )
        view = await SharedListView.create(
            self._hub.cog, self._hub.guild, record["id"], viewer_id=self._hub.viewer_id,
        )
        if view is None:
            await self._hub.refresh(interaction)
            return
        view._interaction = interaction
        await apply_view(interaction, view)


class EditSharedListModal(discord.ui.Modal, title="Modifier la liste"):
    def __init__(self, parent: "SharedListView"):
        super().__init__()
        self._hub = parent
        self.title_input = discord.ui.TextInput(
            label="Titre",
            default=parent.record["title"][:LIST_TITLE_MAX],
            max_length=LIST_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optionnel)",
            style=discord.TextStyle.paragraph,
            default=(parent.record["description"] or "")[:LIST_DESC_MAX] or None,
            max_length=LIST_DESC_MAX,
            required=False,
        )
        mode = parent.record["edit_mode"]
        self.mode_select = discord.ui.Select(
            placeholder="Qui peut ajouter des œuvres ?",
            min_values=1,
            max_values=1,
            required=True,
            options=[
                discord.SelectOption(
                    label="Créateur seul",
                    value="owner",
                    description="Toi uniquement",
                    default=mode == "owner",
                ),
                discord.SelectOption(
                    label="Membres choisis",
                    value="members",
                    description="Toi + les membres ci-dessous",
                    default=mode == "members",
                ),
                discord.SelectOption(
                    label="Tout le serveur",
                    value="public",
                    description="Tout le monde peut ajouter des œuvres",
                    default=mode == "public",
                ),
            ],
        )
        editors_kwargs: dict[str, Any] = {
            "placeholder": "Membres qui peuvent ajouter des œuvres…",
            "min_values": 0,
            "max_values": 25,
            "required": False,
        }
        if parent.editor_ids:
            editors_kwargs["default_values"] = [
                discord.Object(id=user_id) for user_id in parent.editor_ids[:25]
            ]
        self.editors_select = discord.ui.UserSelect(**editors_kwargs)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(
            discord.ui.Label(
                text="Peuvent éditer la liste",
                description="Qui peut ajouter ou retirer des œuvres",
                component=self.mode_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Membres choisis",
                description="Utilisé seulement si « Membres choisis »",
                component=self.editors_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message("**Erreur ·** Donne un titre à la liste.", ephemeral=True)
            return
        mode = (self.mode_select.values or ["owner"])[0]
        if mode not in LIST_EDIT_MODES:
            mode = "owner"
        await interaction.response.defer()
        await self._hub.cog.update_shared_list(
            self._hub.guild,
            self._hub.record["id"],
            title=title,
            description=str(self.desc_input.value or "").strip(),
            edit_mode=mode,
        )
        if mode == "members":
            ids = [
                user.id
                for user in self.editors_select.values
                if not getattr(user, "bot", False) and user.id != self._hub.record["owner_id"]
            ]
            await self._hub.cog.set_shared_list_editors(
                self._hub.guild, self._hub.record["id"], ids,
            )
        await self._hub.refresh(interaction)


class SharedListAddModal(discord.ui.Modal, title="Ajouter une œuvre"):
    def __init__(self, parent: "SharedListView"):
        super().__init__()
        self._hub = parent
        self.query_input = discord.ui.TextInput(
            label="Titre de l'œuvre",
            placeholder="Ex. Dune 2021",
            max_length=80,
            required=True,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        query = str(self.query_input.value or "").strip()
        catalog = self._hub.cog.catalog
        if catalog is None:
            await interaction.followup.send("**Erreur ·** Catalogue média indisponible.", ephemeral=True)
            return
        try:
            hits = await catalog.search(query, "all")
        except Exception:
            logger.exception("Recherche pour liste commune impossible")
            await interaction.followup.send("**Erreur ·** Recherche impossible pour le moment.", ephemeral=True)
            return
        if not hits:
            await interaction.followup.send(
                f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} ».",
                ephemeral=True,
            )
            return
        if len(hits) == 1:
            error = await self._hub.cog.add_shared_list_item(
                self._hub.guild, self._hub.record["id"], interaction.user.id, hits[0],
            )
            await self._hub.refresh(interaction)
            await interaction.followup.send(
                f"**Liste ·** {error}" if error else f"**Ajouté ·** {hits[0].title}",
                ephemeral=True,
            )
            return
        view = SharedListPickView(self._hub, hits)
        await interaction.followup.send(view=view, ephemeral=True)


class SharedListHitSelect(discord.ui.Select):
    def __init__(self, parent: "SharedListPickView", hits: list[MediaHit]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description=select_hit_description(hit),
                emoji=select_emoji(hit.media_type),
            )
            for index, hit in enumerate(hits[:25])
        ]
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        hit = self._hub.hits[int(self.values[0])]
        error = await self._hub.liste.cog.add_shared_list_item(
            self._hub.liste.guild, self._hub.liste.record["id"], interaction.user.id, hit,
        )
        await self._hub.liste.refresh()
        done = discord.ui.LayoutView(timeout=30)
        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay(
            f"**Liste ·** {error}" if error else f"**Ajouté ·** {hit.title}"
        ))
        done.add_item(box)
        await interaction.edit_original_response(view=done)


class SharedListPickView(ReviewsLayout):
    def __init__(self, liste: "SharedListView", hits: list[MediaHit]):
        super().__init__()
        self.liste = liste
        self.hits = hits
        self.set_layout(
            [discord.ui.TextDisplay(
                f"## Ajouter à {pretty.shorten_text(liste.record['title'], 60)}\n"
                f"-# {len(hits)} résultat(s) — choisis l'œuvre"
            )],
            discord.ui.ActionRow(SharedListHitSelect(self, hits)),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.liste.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True


class ListsHubOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ListsHubView", page_items: list[dict[str, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(record["title"], 95),
                value=str(record["id"]),
                description=pretty.shorten_text(
                    f"{record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''} · {list_edit_label(record['edit_mode'])}",
                    95,
                ),
            )
            for record in page_items
        ]
        super().__init__(placeholder="Ouvrir une liste", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = await SharedListView.create(
            self._hub.cog, self._hub.guild, int(self.values[0]), viewer_id=self._hub.viewer_id,
        )
        if view is None:
            await self._hub.refresh(interaction)
            return
        view._interaction = interaction
        await apply_view(interaction, view)


class CreateSharedListButton(discord.ui.Button):
    def __init__(self, parent: "ListsHubView"):
        super().__init__(label="Créer une liste", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CreateSharedListModal(self._hub))


class SharedListBackButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="← Listes", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        hub = await ListsHubView.create(
            self._hub.cog, self._hub.guild, viewer_id=self._hub.viewer_id,
        )
        hub._interaction = interaction
        await apply_view(interaction, hub)


class SharedListAddButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Ajouter", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.send_modal(SharedListAddModal(self._hub))


class SharedListDrawButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Tirage", style=discord.ButtonStyle.primary, disabled=not parent.items)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        hit = await self._hub.cog.draw_shared_list(self._hub.guild, self._hub.record["id"])
        if hit is None:
            await interaction.followup.send("**Tirage ·** Cette liste est vide.", ephemeral=True)
            return
        await open_session_followup(
            self._hub.cog, self._hub.guild, interaction, hit, author_id=interaction.user.id,
        )


class SharedListEditButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Seul le créateur peut modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.send_modal(EditSharedListModal(self._hub))


class SharedListShareButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(**_share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        body = self._hub._share_layout()
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body))
        message = await publish_layout_message(interaction, view)
        if message is None:
            await interaction.followup.send("**Erreur ·** Impossible de publier cette liste.", ephemeral=True)
            return
        self._hub.stop()
        await discard_ephemeral_menu(interaction)


class SharedListDeleteButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.red)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Seul le créateur peut supprimer cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        await self._hub.cog.delete_shared_list(self._hub.guild, self._hub.record["id"])
        hub = await ListsHubView.create(
            self._hub.cog, self._hub.guild, viewer_id=self._hub.viewer_id,
        )
        hub._interaction = interaction
        await apply_view(interaction, hub)


class SharedListRemoveSelect(discord.ui.Select):
    def __init__(self, parent: "SharedListView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description="Retirer de la liste",
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, _row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Retirer une œuvre", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        hit, _row = self._items[int(self.values[0])]
        media_id = await self._hub.cog.lookup_media_id(self._hub.guild, hit)
        if media_id:
            await self._hub.cog.remove_shared_list_item(self._hub.guild, self._hub.record["id"], media_id)
        await self._hub.refresh(interaction)


class ListsHubView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        lists: list[dict[str, Any]],
        viewer_id: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.lists = lists
        self.viewer_id = viewer_id
        self.page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, *, viewer_id: int) -> "ListsHubView":
        return cls(cog, guild, lists=await cog.load_shared_lists(guild), viewer_id=viewer_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _page_nav(self, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        prev_btn = HubPageButton(self, "page", -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, "page", 1, "Suivant →", max_page)
        prev_btn.disabled = self.page <= 0
        next_btn.disabled = self.page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _build(self) -> None:
        body: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"## Listes\n-# {len(self.lists)} liste{'s' if len(self.lists) != 1 else ''} sur ce serveur"
            ),
            sep_wide(),
        ]
        rows: list[discord.ui.ActionRow] = []
        if not self.lists:
            body.append(discord.ui.TextDisplay(
                "*Aucune liste pour l'instant. Crée-en une avec un titre et une courte description.*"
            ))
            rows.append(discord.ui.ActionRow(CreateSharedListButton(self)))
            self.set_layout(body, *rows)
            return
        max_page = max(0, (len(self.lists) - 1) // LIST_PAGE)
        self.page = min(self.page, max_page)
        start = self.page * LIST_PAGE
        page_items = self.lists[start:start + LIST_PAGE]
        for index, record in enumerate(page_items):
            if index:
                body.append(sep_tight())
            desc = pretty.shorten_text(record["description"], 160) if record["description"] else "*Pas de description.*"
            n = record["item_count"]
            text = (
                f"### {record['title']}\n{desc}\n"
                f"-# {_mention(self.guild, self.cog.bot, record['owner_id'])} · "
                f"{n} œuvre{'s' if n != 1 else ''} · {list_edit_label(record['edit_mode'])}"
            )
            body.append(discord.ui.TextDisplay(text))
        rows.append(discord.ui.ActionRow(ListsHubOpenSelect(self, page_items)))
        rows.append(discord.ui.ActionRow(CreateSharedListButton(self)))
        nav = self._page_nav(max_page)
        if nav:
            rows.append(nav)
        self.set_layout(body, *rows)

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self.lists = await self.cog.load_shared_lists(self.guild)
        self._build()
        await self.push(interaction)


class SharedListView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        record: dict[str, Any],
        items: list[tuple[MediaHit, Any]],
        editor_ids: list[int],
        viewer_id: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.record = record
        self.items = items
        self.editor_ids = editor_ids
        self.viewer_id = viewer_id
        self.item_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(
        cls, cog: "Reviews", guild: discord.Guild, list_id: int, *, viewer_id: int
    ) -> "SharedListView | None":
        record = await cog.get_shared_list(guild, list_id)
        if record is None:
            return None
        return cls(
            cog,
            guild,
            record=record,
            items=await cog.load_shared_list_items(guild, list_id),
            editor_ids=await cog.load_shared_list_editors(guild, list_id),
            viewer_id=viewer_id,
        )

    def is_owner(self, user_id: int) -> bool:
        return user_id == int(self.record["owner_id"])

    def can_edit(self, user_id: int) -> bool:
        return self.cog.can_edit_shared_list(self.record, user_id, self.editor_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _page_nav(self, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        prev_btn = HubPageButton(self, "item_page", -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, "item_page", 1, "Suivant →", max_page)
        prev_btn.disabled = self.item_page <= 0
        next_btn.disabled = self.item_page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _header(self) -> str:
        desc = pretty.shorten_text(self.record["description"], 220) if self.record["description"] else "*Pas de description.*"
        n = len(self.items)
        lines = [
            f"## {self.record['title']}",
            desc,
            f"-# {_mention(self.guild, self.cog.bot, self.record['owner_id'])} · "
            f"{n} œuvre{'s' if n != 1 else ''} · édition : {list_edit_label(self.record['edit_mode']).lower()}",
        ]
        if self.record["edit_mode"] == "members" and self.editor_ids:
            shown = [_mention(self.guild, self.cog.bot, user_id) for user_id in self.editor_ids[:8]]
            extra = len(self.editor_ids) - len(shown)
            lines.append(f"-# Éditeurs · {', '.join(shown)}" + (f" et {extra} autre(s)" if extra else ""))
        elif self.record["edit_mode"] == "members":
            lines.append("-# Aucun membre choisi pour l'instant.")
        return "\n".join(lines)

    def _share_item_line(self, index: int, hit: MediaHit) -> str:
        year = f" ({hit.year})" if hit.year else ""
        return f"{index}. {type_emoji(hit.media_type)} **{hit.title}**{year}"

    def _share_layout(self) -> list[discord.ui.Item]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._header()), sep_wide()]
        if not self.items:
            body.append(discord.ui.TextDisplay("*Cette liste est vide.*"))
            return body
        lines = [self._share_item_line(index, hit) for index, (hit, _row) in enumerate(self.items, start=1)]
        body.extend(chunk_text_displays(lines))
        return body

    def _build(self) -> None:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._header()), sep_wide()]
        rows: list[discord.ui.ActionRow] = []
        if not self.items:
            body.append(discord.ui.TextDisplay("*Cette liste est vide.*"))
        else:
            max_page = max(0, (len(self.items) - 1) // LIST_PAGE)
            self.item_page = min(self.item_page, max_page)
            start = self.item_page * LIST_PAGE
            page_items = self.items[start:start + LIST_PAGE]
            for index, (hit, row) in enumerate(page_items):
                if index:
                    body.append(sep_tight())
                year = f" ({hit.year})" if hit.year else ""
                added_by = 0
                added_at = 0
                try:
                    added_by = int(row["added_by"] or 0)
                    added_at = int(row["added_at"] or 0)
                except (KeyError, IndexError, TypeError):
                    if isinstance(row, dict):
                        added_by = int(row.get("added_by") or 0)
                        added_at = int(row.get("added_at") or 0)
                who = _mention(self.guild, self.cog.bot, added_by) if added_by else "quelqu'un"
                when = f" · <t:{added_at}:R>" if added_at else ""
                text = f"**{hit.title}**{year}\n-# {type_label(hit.media_type)} · ajouté par {who}{when}"
                body.append(section_with_thumbnail(text, hit.poster_url))
            if self.can_edit(self.viewer_id):
                rows.append(discord.ui.ActionRow(SharedListRemoveSelect(self, page_items)))
            nav = self._page_nav(max_page)
            if nav:
                rows.append(nav)
        actions: list[discord.ui.Item] = [SharedListBackButton(self), SharedListDrawButton(self)]
        if self.can_edit(self.viewer_id):
            actions.insert(1, SharedListAddButton(self))
        if self.is_owner(self.viewer_id):
            actions.append(SharedListEditButton(self))
            if len(actions) < 5:
                actions.append(SharedListDeleteButton(self))
        rows.append(discord.ui.ActionRow(*actions[:5]))
        self.set_layout(body, *rows)
        self.add_item(discord.ui.ActionRow(SharedListShareButton(self)))

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        record = await self.cog.get_shared_list(self.guild, self.record["id"])
        if record is None:
            hub = await ListsHubView.create(self.cog, self.guild, viewer_id=self.viewer_id)
            hub._interaction = interaction or self._interaction
            editor = interaction or self._interaction
            if editor is not None:
                await apply_view(editor, hub)
            return
        self.record = record
        self.items = await self.cog.load_shared_list_items(self.guild, record["id"])
        self.editor_ids = await self.cog.load_shared_list_editors(self.guild, record["id"])
        self._build()
        await self.push(interaction)


class ProfileView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        member: discord.Member | discord.User,
        *,
        xp: int,
        review_count: int,
        average: float | None,
        twin: Affinity | None,
        rival: Affinity | None,
        titles: dict[int, str],
        journal_entries: list[tuple[MediaHit, Any]],
        watchlist_entries: list[tuple[MediaHit, Any]],
        affinities: list[Affinity],
        viewer_id: int,
        tab: str = "profil",
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.member = member
        self.xp = xp
        self.review_count = review_count
        self.average = average
        self.twin = twin
        self.rival = rival
        self.titles = titles
        self.journal_entries = journal_entries
        self.watchlist_entries = watchlist_entries
        self._highlights = pick_rating_highlights(journal_entries)
        self.affinities = sorted(affinities, key=lambda a: (-a.percent, -a.overlap))
        self.viewer_id = viewer_id
        self.editable = viewer_id == member.id
        self.tab = tab
        self.journal_page = 0
        self.watchlist_page = 0
        self.journal_type = "all"
        self.journal_sort = "recent"
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce profil ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _name(self, user_id: int) -> str:
        name, _avatar = _user_display(self.guild, self.cog.bot, user_id)
        return name

    def _person(self, user_id: int) -> str:
        return _titled(
            _mention(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _profile_header(self, extra: str = "") -> str:
        level, into, need, total = level_progress(self.xp)
        title = self.titles.get(self.member.id, title_for_level(level))
        mention = _mention(self.guild, self.cog.bot, self.member.id)
        notes = f"**{self.review_count}** note{'s' if self.review_count != 1 else ''}"
        if self.average is not None:
            notes += f"  ·  moyenne **{format_score(self.average, average=True)}**"
        lines = [
            f"## {mention}",
            f"{XP} **{total} XP** · niveau **{level}**  ·  {notes}",
            f"-# {into}/{need} vers le niveau {level + 1}",
        ]
        stats = journal_stats_line(self.journal_entries)
        if stats:
            lines.append(stats)
        if extra:
            lines.append(extra)
        lines.append(format_grade(title))
        return "\n".join(lines)

    def _tabs_row(self) -> discord.ui.ActionRow:
        profil, journal, avoir, affinites = labeled_tabs(
            "Profil",
            f"Journal ({self.review_count})",
            f"À voir ({len(self.watchlist_entries)})",
            "Affinités",
        )
        return discord.ui.ActionRow(
            HubTabButton(self, "profil", profil),
            HubTabButton(self, "journal", journal),
            HubTabButton(self, "avoire", avoir),
            HubTabButton(self, "affinites", affinites),
        )

    def _filtered_journal(self) -> list[tuple[MediaHit, Any]]:
        items = self.journal_entries
        if self.journal_type != "all":
            items = [(hit, row) for hit, row in items if hit.media_type == self.journal_type]
        if self.journal_sort == "rating":
            items = sorted(
                items,
                key=lambda item: (float(item[1]["rating"]), int(item[1]["updated_at"] or 0)),
                reverse=True,
            )
        return items

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, attr, 1, "Suivant →", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _highlight_block(self, label: str, hit: MediaHit, rating: float | None) -> discord.ui.Item:
        year = f"  ·  {hit.year}" if hit.year else ""
        lines = [
            f"### {label}",
            f"{type_emoji(hit.media_type)} **{hit.title}**{year}",
            f"-# {type_label(hit.media_type)}"
            + (f"  ·  {hit.subtitle}" if hit.subtitle else ""),
        ]
        if rating is not None:
            lines.append(f"{format_stars(rating)}  **{format_score(rating)}**")
        return section_with_thumbnail("\n".join(lines), hit.poster_url)

    def _profil_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        avatar = self.member.display_avatar.url if hasattr(self.member, "display_avatar") else None
        body: list[discord.ui.Item] = [section_with_thumbnail(self._profile_header(), avatar)]
        for index, (label, hit, rating) in enumerate(self._highlights):
            body.append(sep_wide() if index == 0 else sep_tight())
            body.append(self._highlight_block(label, hit, rating))
        graph = format_rating_graph(self.journal_entries)
        if graph:
            body.append(sep_wide() if self._highlights else sep_tight())
            body.append(discord.ui.TextDisplay(graph))
        rows: list[discord.ui.ActionRow] = []
        return body, rows

    def _journal_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        entries = self._filtered_journal()
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._profile_header()), sep_wide()]
        rows: list[discord.ui.ActionRow] = [
            discord.ui.ActionRow(JournalTypeSelect(self)),
            discord.ui.ActionRow(JournalSortSelect(self)),
        ]
        if not self.journal_entries:
            body.append(discord.ui.TextDisplay("*Aucune œuvre notée pour l'instant.*"))
            return body, rows
        if not entries:
            body.append(discord.ui.TextDisplay("*Aucune œuvre pour ce filtre.*"))
            return body, rows
        max_page = max(0, (len(entries) - 1) // JOURNAL_PAGE)
        self.journal_page = min(self.journal_page, max_page)
        start = self.journal_page * JOURNAL_PAGE
        page_items = entries[start:start + JOURNAL_PAGE]
        hide = self.viewer_id != self.member.id
        for index, (hit, row) in enumerate(page_items):
            if index:
                body.append(sep_tight())
            year = f" ({hit.year})" if hit.year else ""
            text = f"{format_stars(row['rating'])}  **{hit.title}**{year}\n-# {type_label(hit.media_type)}"
            shown = format_comment(
                row["comment"] or "",
                spoiler=row_spoiler(row),
                hide=hide,
                limit=180,
            )
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, hit.poster_url))
        nav = self._page_nav("journal_page", max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _watchlist_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._profile_header()), sep_wide()]
        rows: list[discord.ui.ActionRow] = []
        if not self.watchlist_entries:
            empty = (
                "*Rien dans ta liste à voir.*"
                if self.editable
                else "*Cette liste à voir est vide.*"
            )
            body.append(discord.ui.TextDisplay(empty))
            return body, rows
        max_page = max(0, (len(self.watchlist_entries) - 1) // JOURNAL_PAGE)
        self.watchlist_page = min(self.watchlist_page, max_page)
        start = self.watchlist_page * JOURNAL_PAGE
        page_items = self.watchlist_entries[start:start + JOURNAL_PAGE]
        for index, (hit, row) in enumerate(page_items):
            if index:
                body.append(sep_tight())
            year = f" ({hit.year})" if hit.year else ""
            added = 0
            try:
                added = int(row["added_at"] or 0)
            except (KeyError, IndexError, TypeError):
                if isinstance(row, dict):
                    added = int(row.get("added_at") or 0)
            when = f" · ajouté <t:{added}:R>" if added else ""
            text = f"**{hit.title}**{year}\n-# {type_label(hit.media_type)}{when}"
            body.append(section_with_thumbnail(text, hit.poster_url))
        rows.append(discord.ui.ActionRow(WatchlistOpenSelect(self, page_items)))
        if self.editable:
            rows.append(discord.ui.ActionRow(WatchlistRemoveSelect(self, page_items)))
        nav = self._page_nav("watchlist_page", max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _affinites_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._profile_header()), sep_wide()]
        rows: list[discord.ui.ActionRow] = []
        if not self.affinities:
            body.append(discord.ui.TextDisplay(
                f"*Pas encore assez d'œuvres en commun avec quelqu'un "
                f"(minimum {MIN_AFFINITY_OVERLAP}).*"
            ))
            return body, rows
        twins = self.affinities[:3]
        rival = min(self.affinities, key=lambda a: (a.percent, -a.overlap))
        lines = [
            f"**{TWIN} Jumeaux**",
            f"-# {len(self.affinities)} affinité(s) · min. {MIN_AFFINITY_OVERLAP} en commun",
        ]
        for twin in twins:
            lines.append(self._person(twin.user_id))
            lines.append(f"-# {twin.percent:.0f} % · {twin.overlap} en commun")
        if rival.user_id not in {t.user_id for t in twins}:
            lines.append(f"**{RIVAL} Rival**")
            lines.append(self._person(rival.user_id))
            lines.append(f"-# {rival.percent:.0f} %")
        body.append(discord.ui.TextDisplay("\n".join(lines)))
        rows.append(discord.ui.ActionRow(AffinityCompareSelect(self)))
        return body, rows

    def _build(self) -> None:
        if self.tab == "journal":
            body, rows = self._journal_layout()
        elif self.tab == "avoire":
            body, rows = self._watchlist_layout()
        elif self.tab == "affinites":
            body, rows = self._affinites_layout()
        else:
            body, rows = self._profil_layout()
        self.set_layout(body, *rows, self._tabs_row())
        self.add_item(discord.ui.ActionRow(ProfileShareButton(self)))

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self.watchlist_entries = await self.cog.load_watchlist(self.guild, self.member.id)
        self._build()
        await self.push(interaction)


class TopPeriodSelect(discord.ui.Select):
    def __init__(self, parent: "ServerHubView"):
        options = [
            discord.SelectOption(label="Toutes périodes", value="all", default=parent.period == "all"),
            discord.SelectOption(label="Cette semaine", value="semaine", default=parent.period == "semaine"),
            discord.SelectOption(label="Ce mois", value="mois", default=parent.period == "mois"),
        ]
        super().__init__(placeholder="Période du top", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._hub.period = self.values[0]
        self._hub.top_page = 0
        self._hub.top_items = await self._hub.cog.load_top(
            self._hub.guild,
            media_type=self._hub.media_type,
            period=self._hub.period,
        )
        await self._hub.refresh(interaction)


class ServerHubView(ReviewsLayout):
    """Récentes, catalogue et top du serveur, dans une seule vue à onglets."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        recent: list[tuple[MediaHit, Any]],
        catalog: list[tuple[MediaHit, float, int]],
        top: list[tuple[MediaHit, float, int]],
        catalog_subtitle: str,
        media_type: str = "all",
        period: str = "all",
        tab: str = "recentes",
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.recent = recent
        self.catalog = catalog
        self.top_items = top
        self.catalog_subtitle = catalog_subtitle
        self.media_type = media_type
        self.period = period
        self.tab = tab
        self.recent_page = 0
        self.catalog_page = 0
        self.top_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    def _tabs_row(self) -> discord.ui.ActionRow:
        recentes, catalogue, top = labeled_tabs(
            "Récentes",
            f"Catalogue ({len(self.catalog)})",
            "Top",
        )
        return discord.ui.ActionRow(
            HubTabButton(self, "recentes", recentes),
            HubTabButton(self, "catalogue", catalogue),
            HubTabButton(self, "top", top),
        )

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "← Précédent", max_page)
        next_btn = HubPageButton(self, attr, 1, "Suivant →", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _ranked_layout(
        self,
        *,
        title: str,
        subtitle: str,
        items: list[tuple[MediaHit, float, int]],
        page_attr: str,
        extra_row: discord.ui.ActionRow | None = None,
    ) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(f"## {title}\n-# {subtitle}")]
        rows: list[discord.ui.ActionRow] = []
        if extra_row:
            rows.append(extra_row)
        if not items:
            body.append(discord.ui.TextDisplay("*Aucune œuvre ne correspond à cette recherche.*"))
            return body, rows
        max_page = max(0, (len(items) - 1) // CATALOG_PAGE)
        page = min(getattr(self, page_attr), max_page)
        setattr(self, page_attr, page)
        start = page * CATALOG_PAGE
        page_items = items[start:start + CATALOG_PAGE]
        lines = []
        for index, (hit, avg, count) in enumerate(page_items, start=start + 1):
            year = f" ({hit.year})" if hit.year else ""
            lines.append(
                f"**{index}.** {format_stars(avg)}  **{hit.title}**{year}  ·  "
                f"{type_label(hit.media_type)}  ·  {count} note{'s' if count > 1 else ''}"
            )
        body.append(discord.ui.TextDisplay("\n".join(lines)))
        rows.append(discord.ui.ActionRow(CatalogOpenSelect(self, page_items)))
        nav = self._page_nav(page_attr, max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _recentes_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(
            f"## Dernières critiques\n-# {len(self.recent)} récente(s) sur ce serveur"
        )]
        rows: list[discord.ui.ActionRow] = []
        if not self.recent:
            body.append(discord.ui.TextDisplay("*Personne n'a encore noté d'œuvre ici.*"))
            return body, rows
        max_page = max(0, (len(self.recent) - 1) // JOURNAL_PAGE)
        self.recent_page = min(self.recent_page, max_page)
        start = self.recent_page * JOURNAL_PAGE
        page_items = self.recent[start:start + JOURNAL_PAGE]
        for hit, row in page_items:
            user_id = int(row["user_id"])
            _name, avatar = _user_display(self.guild, self.cog.bot, user_id)
            year = f" ({hit.year})" if hit.year else ""
            text = (
                f"{_mention(self.guild, self.cog.bot, user_id)}\n"
                f"{format_stars(row['rating'])}  **{format_score(row['rating'])}**\n"
                f"**{hit.title}**{year} · {type_label(hit.media_type)} · <t:{row['updated_at']}:R>"
            )
            shown = format_comment(
                row["comment"] or "",
                spoiler=row_spoiler(row),
                hide=True,
                limit=180,
            )
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar or hit.poster_url))
        rows.append(discord.ui.ActionRow(RecentOpenSelect(self, page_items)))
        nav = self._page_nav("recent_page", max_page)
        if nav:
            rows.append(nav)
        return body, rows

    def _build(self) -> None:
        if self.tab == "catalogue":
            body, rows = self._ranked_layout(
                title="Catalogue du serveur",
                subtitle=self.catalog_subtitle,
                items=self.catalog,
                page_attr="catalog_page",
            )
        elif self.tab == "top":
            period_label = {"all": "toutes périodes", "semaine": "cette semaine", "mois": "ce mois"}.get(
                self.period, self.period
            )
            type_part = type_label(self.media_type) if self.media_type != "all" else "Tous types"
            body, rows = self._ranked_layout(
                title="Top du serveur",
                subtitle=f"{type_part}  ·  {period_label}",
                items=self.top_items,
                page_attr="top_page",
                extra_row=discord.ui.ActionRow(TopPeriodSelect(self)),
            )
        else:
            body, rows = self._recentes_layout()
        self.set_layout(body, *rows, self._tabs_row())

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self._build()
        await self.push(interaction)


# ---------------------------------------------------------------------------
# Préférences
# ---------------------------------------------------------------------------

class PrefFieldSelect(discord.ui.Select):
    def __init__(
        self,
        parent: "PreferencesView",
        *,
        field: str,
        placeholder: str,
        options: list[discord.SelectOption],
    ):
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)
        self._hub = parent
        self._field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = self.values[0]
        value: str | bool = raw == "1" if self._field == "announce_notes" else raw
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild,
            self._hub.user_id,
            **{self._field: value},
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


class PrefSearchTypeSelect(discord.ui.Select):
    def __init__(self, parent: "PreferencesView"):
        current = normalize_search_pref(parent.prefs.default_search_type)
        chosen = set(parse_search_types(current))
        options = [
            discord.SelectOption(
                label="Tous les types",
                value="all",
                description="Films, séries, jeux et musique — pas les livres",
                default=current == "all",
            )
        ]
        for kind in TYPE_META:
            options.append(
                discord.SelectOption(
                    label=type_label(kind),
                    value=kind,
                    emoji=select_emoji(kind),
                    description="Peut être combiné avec d'autres types",
                    default=current != "all" and kind in chosen,
                )
            )
        super().__init__(
            placeholder=search_pref_label(current),
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild,
            self._hub.user_id,
            default_search_type=normalize_search_pref(self.values),
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


class PreferencesView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        user: discord.abc.User,
        prefs: UserPrefs,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.user_id = user.id
        self.prefs = prefs
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ces préférences ne s'affichent que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _section(self, title: str, hint: str, select: discord.ui.Select) -> list[discord.ui.Item]:
        return [
            discord.ui.TextDisplay(f"**{title}**\n-# {hint}"),
            discord.ui.ActionRow(select),
        ]

    def _build(self) -> None:
        prefs = self.prefs
        header = (
            f"## Préférences\n"
            f"-# Tes défauts sur **{self.guild.name}** — notes, recherches et nouvelles listes."
        )
        children: list[discord.ui.Item] = [
            section_with_thumbnail(header, self.user.display_avatar.url),
            sep_wide(),
            *self._section(
                "Date vu / joué / lu",
                "Préremplit le formulaire Noter, ou laisse le champ vide.",
                PrefFieldSelect(
                    self,
                    field="default_date",
                    placeholder=date_pref_label(prefs.default_date),
                    options=[
                        discord.SelectOption(
                            label="Aujourd'hui",
                            value="today",
                            description="Préremplit le formulaire Noter avec la date du jour",
                            default=prefs.default_date == "today",
                        ),
                        discord.SelectOption(
                            label="Vide",
                            value="empty",
                            description="Laisse le champ date vide",
                            default=prefs.default_date == "empty",
                        ),
                    ],
                ),
            ),
            sep_wide(),
            *self._section(
                "Édition des nouvelles listes",
                "Qui peut modifier une liste que tu viens de créer.",
                PrefFieldSelect(
                    self,
                    field="default_list_edit",
                    placeholder=list_edit_label(prefs.default_list_edit),
                    options=[
                        discord.SelectOption(
                            label="Créateur seul",
                            value="owner",
                            description="Toi seul peux modifier une liste que tu crées",
                            default=prefs.default_list_edit == "owner",
                        ),
                        discord.SelectOption(
                            label="Membres choisis",
                            value="members",
                            description="Tu pourras ajouter des éditeurs ensuite",
                            default=prefs.default_list_edit == "members",
                        ),
                        discord.SelectOption(
                            label="Tout le serveur",
                            value="public",
                            description="N'importe qui pourra modifier tes nouvelles listes",
                            default=prefs.default_list_edit == "public",
                        ),
                    ],
                ),
            ),
            sep_wide(),
            *self._section(
                "Types de recherche",
                "Un ou plusieurs types pour /search. « Tous les types » ignore les autres choix.",
                PrefSearchTypeSelect(self),
            ),
            sep_wide(),
            *self._section(
                "Annonces de tes notes",
                "Si tes notes apparaissent dans le salon d'annonces.",
                PrefFieldSelect(
                    self,
                    field="announce_notes",
                    placeholder=announce_pref_label(prefs.announce_notes),
                    options=[
                        discord.SelectOption(
                            label="Publier",
                            value="1",
                            description="Tes notes apparaissent dans le salon d'annonces",
                            default=prefs.announce_notes,
                        ),
                        discord.SelectOption(
                            label="Ne pas annoncer",
                            value="0",
                            description="Tes notes restent dans ton carnet seulement",
                            default=not prefs.announce_notes,
                        ),
                    ],
                ),
            ),
        ]
        self.clear_items()
        self.add_item(discord.ui.Container(*children))

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class CommentMaxModal(discord.ui.Modal, title="Longueur max. du commentaire"):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__()
        self._view_ref = view_ref
        self.length_input = discord.ui.TextInput(
            label=f"Caractères ({MIN_COMMENT_MAX}-{MAX_COMMENT_MAX})",
            placeholder=str(DEFAULT_COMMENT_MAX),
            default=str(view_ref.comment_max),
            max_length=3,
        )
        self.add_item(self.length_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        raw = self.length_input.value.strip()
        if not raw.isdigit() or not (MIN_COMMENT_MAX <= int(raw) <= MAX_COMMENT_MAX):
            await interaction.followup.send(
                f"**Erreur ·** Indique un entier entre {MIN_COMMENT_MAX} et {MAX_COMMENT_MAX}.",
                ephemeral=True,
            )
            return
        await self._view_ref.cog.data.get(self._view_ref.guild).set_dict_value("settings", "MaxCommentLength", int(raw))
        self._view_ref.cog._comment_max[self._view_ref.guild.id] = int(raw)
        await self._view_ref.refresh(interaction)


class EditCommentMaxButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CommentMaxModal(self._view_ref))


class AnnounceRouteModal(discord.ui.Modal, title="Salon d'annonce"):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__()
        self._view_ref = view_ref
        configured = set(view_ref.announce_channels)
        options: list[discord.SelectOption] = []
        for route in ANNOUNCE_ROUTE_ORDER:
            taken = route in configured
            options.append(
                discord.SelectOption(
                    label=announce_route_label(route),
                    value=route,
                    description="Mettre à jour le salon" if taken else "Nouveau salon",
                    emoji=select_emoji(route) if route != ANNOUNCE_ROUTE_ALL else None,
                )
            )
        self.type_select = discord.ui.Select(
            placeholder="Type de média",
            min_values=1,
            max_values=1,
            required=True,
            options=options,
        )
        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Salon textuel",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Type",
                description="« Tous les types » sert de salon par défaut",
                component=self.type_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Salon",
                description="Là où les nouvelles notes de ce type sont postées",
                component=self.channel_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        route = str(self.type_select.values[0]) if self.type_select.values else ""
        if route not in ANNOUNCE_ROUTE_ORDER:
            await interaction.followup.send("**Erreur ·** Type invalide.", ephemeral=True)
            return
        if not self.channel_select.values:
            await interaction.followup.send("**Erreur ·** Choisis un salon.", ephemeral=True)
            return
        channel = self.channel_select.values[0].resolve()
        if channel is None:
            try:
                channel = await self.channel_select.values[0].fetch()
            except discord.HTTPException:
                await interaction.followup.send("**Erreur ·** Salon introuvable.", ephemeral=True)
                return
        guild = self._view_ref.guild
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Seuls les salons textuels sont pris en charge.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.",
                ephemeral=True,
            )
            return
        await self._view_ref.cog.set_announce_route(guild, route, channel.id)
        await self._view_ref.refresh(interaction)


class AddAnnounceButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(label="Ajouter", style=discord.ButtonStyle.green)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AnnounceRouteModal(self._view_ref))


class RemoveAnnounceSelect(discord.ui.Select):
    def __init__(self, view_ref: "ReviewsConfigView"):
        options = [
            discord.SelectOption(
                label=f"Retirer · {announce_route_label(route)}",
                value=route,
                description=f"#{view_ref.announce_channels[route].name}",
                emoji=select_emoji(route) if route != ANNOUNCE_ROUTE_ALL else None,
            )
            for route in ANNOUNCE_ROUTE_ORDER
            if route in view_ref.announce_channels
        ]
        super().__init__(
            placeholder="Retirer un salon…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        route = str(self.values[0]) if self.values else ""
        if route not in ANNOUNCE_ROUTE_ORDER:
            await self._view_ref.refresh(interaction)
            return
        await self._view_ref.cog.set_announce_route(self._view_ref.guild, route, None)
        await self._view_ref.refresh(interaction)


class ReviewsConfigView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        announce_channels: dict[str, discord.TextChannel],
        comment_max: int,
        review_count: int,
        media_count: int,
        api_status: dict[str, bool],
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.announce_channels = announce_channels
        self.comment_max = comment_max
        self.review_count = review_count
        self.media_count = media_count
        self.api_status = api_status
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer le serveur` est requise.", ephemeral=True
            )
            return False
        return True

    def _announce_body(self) -> str:
        lines: list[str] = []
        for route in ANNOUNCE_ROUTE_ORDER:
            channel = self.announce_channels.get(route)
            if channel is None:
                continue
            if route == ANNOUNCE_ROUTE_ALL:
                lines.append(f"Tous les types · {channel.mention}")
            else:
                lines.append(f"{type_emoji(route)} {type_label(route)} · {channel.mention}")
        if not lines:
            return "**Salons d'annonce**\n*Aucun salon — les notes ne sont pas annoncées.*"
        hint = (
            "-# Un type sans salon dédié utilise *Tous les types*."
            if ANNOUNCE_ROUTE_ALL in self.announce_channels
            else "-# Les types sans salon dédié ne sont pas annoncés."
        )
        return "**Salons d'annonce**\n" + "\n".join(lines) + f"\n{hint}"

    def _build(self) -> None:
        apis = "  ·  ".join(f"{name} {'ok' if ok else 'manquant'}" for name, ok in self.api_status.items())
        rows: list[discord.ui.Item] = []
        if self.announce_channels:
            rows.append(discord.ui.ActionRow(RemoveAnnounceSelect(self)))
        self.set_layout(
            [
                discord.ui.TextDisplay(f"## Configuration des critiques — {self.guild.name}"),
                discord.ui.Separator(),
                discord.ui.Section(
                    self._announce_body(),
                    accessory=AddAnnounceButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.Section(
                    f"**Commentaire max.**\n{self.comment_max} caractères",
                    accessory=EditCommentMaxButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# {self.review_count} critique(s) · {self.media_count} œuvre(s)\n-# APIs · {apis}"
                ),
            ],
            *rows,
        )

    async def _reload(self) -> None:
        self.announce_channels = await self.cog.get_announce_channels(self.guild)
        self.comment_max = await self.cog.get_comment_max(self.guild)
        self.review_count, self.media_count = await self.cog.counts(self.guild)
        self.api_status = self.cog.catalog.status() if self.cog.catalog else {}

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self._reload()
        self._build()
        await self.push(interaction)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)


class HelpView(ReviewsLayout):
    """Aide du bot : commandes + comment noter, en un seul LayoutView."""

    def __init__(self, *, avatar_url: str | None = None):
        super().__init__()
        header = (
            "## CRIT\n"
            "**Vous avez toujours rêvé de critiquer les films streamés ?** "
            "Alors n'attendez plus : note films, séries, albums et livres — "
            "puis compare tes goûts avec les autres."
        )
        noter = (
            f"### {STAR} Comment noter\n"
            "1. Lance `/search` avec un titre (année bienvenue). Dès 2 lettres, "
            "des suggestions apparaissent. Tu peux déjà passer une **note** "
            "et un **commentaire**.\n"
            "2. S'il y a plusieurs résultats, choisis l'œuvre dans le menu.\n"
            "3. Clique **Noter** : un formulaire demande la note "
            f"({format_stars(0)} 0 → {format_stars(10)} 10, entier), "
            "un commentaire optionnel, la date (vu, joué, écouté ou lu) "
            "et une case **Spoiler** pour masquer le commentaire en public.\n"
            "4. **À voir** l'ajoute à ta liste — elle disparaît dès que tu notes.\n"
            "5. Si tu as déjà donné la note dans `/search` et que tu n'avais pas encore "
            "noté cette œuvre, **Noter** l'enregistre tout de suite.\n"
            "\n"
            "Une seule note par œuvre et par membre.\n"
            "-# Exemples · `Dune 2021`  ·  `film:Dune`  ·  `jeu:Hades`  ·  "
            "`album:Blonde`  ·  `livre:Dune`  ·  une URL TMDB, Steam, Spotify ou IMDb"
        )
        commandes = (
            "### Commandes\n"
            "`/search` — catalogues (TMDB, Steam, Spotify, Open Library) : fiche, noter ou à voir\n"
            "`/carnet` — page d'un membre : profil, journal, à voir, affinités "
            "(ou clic droit sur un membre → **Voir le carnet**)\n"
            "`/explore` — ce que le salon a déjà noté : récentes, catalogue, top\n"
            "`/listes` — listes communes (autocomplete pour ouvrir une liste)\n"
            "`/tirage` — une œuvre au hasard (ta liste à voir, celle d'un membre, ou une liste commune)\n"
            "`/preferences` — tes défauts : date, listes, recherche, annonces\n"
            "`/config` — salons d'annonces (par type) et longueur des commentaires "
            "*(Gérer le serveur)*\n"
            "`/help` — cette aide"
        )
        extras = (
            f"### {XP} Autour des notes\n"
            f"{MOVIE} Films  ·  {TV} Séries  ·  {GAME} Jeux  ·  {MUSIC} Albums et morceaux  ·  {BOOK} Livres\n"
            "Le journal de `/carnet` se filtre par type et se trie (récentes / mieux notées). "
            "Ton commentaire spoiler reste lisible dans ton journal, pas en public. "
            "Les `/listes` sont partagées : le créateur décide qui peut les éditer "
            "(lui seul, des membres, ou tout le serveur). "
            "`/config` peut poster les notes dans un salon différent selon le type. "
            "Tes défauts (date, listes, recherche, annonces) se règlent dans `/preferences`.\n"
            "-# Chaque note rapporte de l'XP (avec plafond quotidien)"
        )
        self.set_layout(
            [
                section_with_thumbnail(header, avatar_url),
                discord.ui.Separator(),
                discord.ui.TextDisplay(noter),
                discord.ui.Separator(),
                discord.ui.TextDisplay(commandes),
                discord.ui.Separator(),
                discord.ui.TextDisplay(extras),
            ]
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Reviews(commands.Cog):
    """Carnet de critiques interne au serveur (films, séries, jeux, musique, livres)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        self._http: aiohttp.ClientSession | None = None
        self.catalog: MediaCatalog | None = None  # type: ignore[assignment]
        self._schema_ready: set[int] = set()
        self._comment_max: dict[int, int] = {}
        self._prefs: dict[tuple[int, int], UserPrefs] = {}

        settings = dataio.DictTableBuilder(
            "settings",
            {
                "AnnounceChannelID": 0,
                "LastAnnounceChannelID": 0,
                "AnnounceChannelsByType": "{}",
                "MaxCommentLength": DEFAULT_COMMENT_MAX,
                "BackfilledXP": "0",
                "RatingsOnTen": "0",
            },
        )
        media_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                year INTEGER,
                poster_url TEXT,
                url TEXT,
                overview TEXT NOT NULL DEFAULT '',
                genres TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(source, source_id, media_type)
            )"""
        )
        reviews_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                rating REAL NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                experienced_at TEXT NOT NULL DEFAULT '',
                spoiler INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, media_id)
            )"""
        )
        profiles_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                xp INTEGER NOT NULL DEFAULT 0,
                daily_xp INTEGER NOT NULL DEFAULT 0,
                daily_date TEXT NOT NULL DEFAULT '',
                daily_awards INTEGER NOT NULL DEFAULT 0,
                last_level INTEGER NOT NULL DEFAULT 1
            )"""
        )
        favorites_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, slot)
            )"""
        )
        watchlist_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, media_id)
            )"""
        )
        shared_lists_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS shared_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                edit_mode TEXT NOT NULL DEFAULT 'owner',
                created_at INTEGER NOT NULL
            )"""
        )
        shared_list_editors_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS shared_list_editors (
                list_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (list_id, user_id)
            )"""
        )
        shared_list_items_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS shared_list_items (
                list_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (list_id, media_id)
            )"""
        )
        preferences_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY,
                default_date TEXT NOT NULL DEFAULT 'empty',
                default_list_edit TEXT NOT NULL DEFAULT 'owner',
                default_spoiler INTEGER NOT NULL DEFAULT 0,
                default_search_type TEXT NOT NULL DEFAULT 'all',
                announce_notes INTEGER NOT NULL DEFAULT 1
            )"""
        )
        self.data.link(
            discord.Guild,
            settings,
            media_table,
            reviews_table,
            profiles_table,
            favorites_table,
            watchlist_table,
            shared_lists_table,
            shared_list_editors_table,
            shared_list_items_table,
            preferences_table,
        )

    async def cog_load(self) -> None:
        config = dict(getattr(self.bot, "config", {}) or {})
        try:
            from dotenv import dotenv_values
            for key, value in dotenv_values(".env").items():
                if value:
                    config[key] = value
        except Exception:
            pass
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8, sock_connect=3, sock_read=6),
            headers={"User-Agent": "CRIT-BOT/1.0 (Discord reviews)"},
        )
        self.catalog = MediaCatalog(
            self._http,
            tmdb_key=str(config.get("TMDB_API_KEY") or "").strip(),
            spotify_id=str(config.get("SPOTIFY_CLIENT_ID") or "").strip(),
            spotify_secret=str(config.get("SPOTIFY_CLIENT_SECRET") or "").strip(),
        )
        status = self.catalog.status()
        missing = [name for name, ok in status.items() if not ok]
        if missing:
            logger.warning("Fournisseurs incomplets : %s", ", ".join(missing))
        self.bot.add_dynamic_items(FicheDynButton, AnnounceDynButton)
        self._carnet_menu = app_commands.ContextMenu(
            name="Voir le carnet",
            callback=self.critique_carnet_user,
        )
        existing = self.bot.tree.get_command("Voir le carnet", type=discord.AppCommandType.user)
        if existing is not None:
            self.bot.tree.remove_command("Voir le carnet", type=discord.AppCommandType.user)
        self.bot.tree.add_command(self._carnet_menu)
        self._sweep_fiches.start()

    async def cog_unload(self) -> None:
        self._sweep_fiches.cancel()
        self.bot.remove_dynamic_items(FicheDynButton, AnnounceDynButton)
        self.bot.tree.remove_command("Voir le carnet", type=discord.AppCommandType.user)
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.data.close_all()

    @tasks.loop(seconds=30)
    async def _sweep_fiches(self) -> None:
        try:
            await sweep_expired(self.bot, render_dyn_record)
        except Exception:
            logger.exception("sweep fiches publiées")

    @_sweep_fiches.before_loop
    async def _before_sweep_fiches(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    async def get_announce_route_ids(self, guild: discord.Guild) -> dict[str, int]:
        settings = self.data.get(guild)
        routes: dict[str, int] = {}
        default_id = await settings.get_dict_value("settings", "AnnounceChannelID", cast=int)
        if default_id:
            routes[ANNOUNCE_ROUTE_ALL] = int(default_id)
        raw = await settings.get_dict_value("settings", "AnnounceChannelsByType")
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if key not in TYPE_META:
                        continue
                    try:
                        channel_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if channel_id:
                        routes[key] = channel_id
        return routes

    async def get_announce_channels(self, guild: discord.Guild) -> dict[str, discord.TextChannel]:
        resolved: dict[str, discord.TextChannel] = {}
        for route, channel_id in (await self.get_announce_route_ids(guild)).items():
            channel = resolve_guild_text_channel(guild, channel_id)
            if channel is not None:
                resolved[route] = channel
        return resolved

    async def set_announce_route(
        self, guild: discord.Guild, route: str, channel_id: int | None
    ) -> None:
        settings = self.data.get(guild)
        if route == ANNOUNCE_ROUTE_ALL:
            await settings.set_dict_value("settings", "AnnounceChannelID", channel_id or 0)
            return
        if route not in TYPE_META:
            return
        typed = {
            key: value
            for key, value in (await self.get_announce_route_ids(guild)).items()
            if key in TYPE_META
        }
        if channel_id:
            typed[route] = int(channel_id)
        else:
            typed.pop(route, None)
        await settings.set_dict_value("settings", "AnnounceChannelsByType", json.dumps(typed))

    async def get_announce_channel(
        self, guild: discord.Guild, media_type: str | None = None
    ) -> discord.TextChannel | None:
        routes = await self.get_announce_route_ids(guild)
        channel_id = None
        if media_type and media_type in routes:
            channel_id = routes[media_type]
        elif ANNOUNCE_ROUTE_ALL in routes:
            channel_id = routes[ANNOUNCE_ROUTE_ALL]
        return resolve_guild_text_channel(guild, channel_id)

    def cached_comment_max(self, guild: discord.Guild) -> int:
        return self._comment_max.get(guild.id, DEFAULT_COMMENT_MAX)

    async def get_comment_max(self, guild: discord.Guild) -> int:
        cached = self._comment_max.get(guild.id)
        if cached is not None:
            return cached
        value = await self.data.get(guild).get_dict_value("settings", "MaxCommentLength", cast=int)
        if not value:
            value = DEFAULT_COMMENT_MAX
        value = max(MIN_COMMENT_MAX, min(MAX_COMMENT_MAX, int(value)))
        self._comment_max[guild.id] = value
        return value

    async def get_user_prefs(self, guild: discord.Guild, user_id: int) -> UserPrefs:
        key = (guild.id, user_id)
        cached = self._prefs.get(key)
        if cached is not None:
            return cached
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            "SELECT * FROM preferences WHERE user_id=?",
            user_id,
        )
        prefs = prefs_from_row(row)
        self._prefs[key] = prefs
        return prefs

    async def set_user_prefs(self, guild: discord.Guild, user_id: int, **updates: Any) -> UserPrefs:
        current = await self.get_user_prefs(guild, user_id)
        cleaned: dict[str, Any] = {}
        if "default_date" in updates:
            value = updates["default_date"]
            cleaned["default_date"] = value if value in DATE_PREF_VALUES else current.default_date
        if "default_list_edit" in updates:
            value = updates["default_list_edit"]
            cleaned["default_list_edit"] = value if value in LIST_EDIT_MODES else current.default_list_edit
        if "default_search_type" in updates:
            cleaned["default_search_type"] = normalize_search_pref(updates["default_search_type"])
        if "announce_notes" in updates:
            cleaned["announce_notes"] = bool(updates["announce_notes"])
        prefs = replace(current, **cleaned) if cleaned else current
        await self._ensure_schema(guild)
        await self.data.get(guild).execute(
            """INSERT OR REPLACE INTO preferences
               (user_id, default_date, default_list_edit, default_spoiler, default_search_type, announce_notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            user_id,
            prefs.default_date,
            prefs.default_list_edit,
            0,
            prefs.default_search_type,
            int(prefs.announce_notes),
        )
        self._prefs[(guild.id, user_id)] = prefs
        return prefs

    async def _resolve_search_type(
        self,
        guild: discord.Guild,
        user_id: int,
        explicit: str | None,
    ) -> str:
        if explicit in TYPE_META or explicit == "all":
            return explicit
        prefs = await self.get_user_prefs(guild, user_id)
        return normalize_search_pref(prefs.default_search_type)

    async def counts(self, guild: discord.Guild) -> tuple[int, int]:
        db = self.data.get(guild)
        reviews = await db.fetchone("SELECT COUNT(*) AS n FROM reviews")
        media = await db.fetchone("SELECT COUNT(*) AS n FROM media")
        return int(reviews["n"] if reviews else 0), int(media["n"] if media else 0)

    # ------------------------------------------------------------------
    # XP / affinités
    # ------------------------------------------------------------------

    async def ensure_progress(self, guild: discord.Guild) -> None:
        settings = self.data.get(guild)
        flag = await settings.get_dict_value("settings", "BackfilledXP")
        if flag == "1":
            return
        rows = await settings.fetchall(
            """SELECT user_id, COUNT(*) AS n,
                      SUM(CASE WHEN comment != '' THEN 1 ELSE 0 END) AS comments
               FROM reviews GROUP BY user_id"""
        )
        for row in rows:
            xp = int(row["n"]) * 10 + int(row["comments"] or 0) * 10
            await settings.execute(
                """INSERT OR IGNORE INTO profiles
                   (user_id, xp, daily_xp, daily_date, daily_awards, last_level)
                   VALUES (?, ?, 0, '', 0, ?)""",
                int(row["user_id"]),
                xp,
                level_for_xp(xp),
            )
        await settings.set_dict_value("settings", "BackfilledXP", "1")

    async def get_profile_xp(self, guild: discord.Guild, user_id: int) -> int:
        await self.ensure_progress(guild)
        row = await self.data.get(guild).fetchone("SELECT xp FROM profiles WHERE user_id=?", user_id)
        return int(row["xp"]) if row else 0

    async def get_titles(self, guild: discord.Guild, user_ids: list[int]) -> dict[int, str]:
        await self.ensure_progress(guild)
        unique = list({int(user_id) for user_id in user_ids})
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        rows = await self.data.get(guild).fetchall(
            f"SELECT user_id, xp FROM profiles WHERE user_id IN ({placeholders})",
            *unique,
        )
        xp_by_user = {int(row["user_id"]): int(row["xp"]) for row in rows}
        return {user_id: title_for_level(level_for_xp(xp_by_user.get(user_id, 0))) for user_id in unique}

    async def _ensure_schema(self, guild: discord.Guild) -> None:
        if guild.id in self._schema_ready:
            return
        db = self.data.get(guild)
        columns = await db.column_names("reviews")
        if "experienced_at" not in columns:
            await db.execute("ALTER TABLE reviews ADD COLUMN experienced_at TEXT NOT NULL DEFAULT ''")
        if "spoiler" not in columns:
            await db.execute("ALTER TABLE reviews ADD COLUMN spoiler INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, media_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS shared_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                edit_mode TEXT NOT NULL DEFAULT 'owner',
                created_at INTEGER NOT NULL
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS shared_list_editors (
                list_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (list_id, user_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS shared_list_items (
                list_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (list_id, media_id)
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY,
                default_date TEXT NOT NULL DEFAULT 'empty',
                default_list_edit TEXT NOT NULL DEFAULT 'owner',
                default_spoiler INTEGER NOT NULL DEFAULT 0,
                default_search_type TEXT NOT NULL DEFAULT 'all',
                announce_notes INTEGER NOT NULL DEFAULT 1
            )"""
        )
        pref_columns = await db.column_names("preferences")
        pref_alters = {
            "default_date": "TEXT NOT NULL DEFAULT 'empty'",
            "default_list_edit": "TEXT NOT NULL DEFAULT 'owner'",
            "default_spoiler": "INTEGER NOT NULL DEFAULT 0",
            "default_search_type": "TEXT NOT NULL DEFAULT 'all'",
            "announce_notes": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, spec in pref_alters.items():
            if name not in pref_columns:
                await db.execute(f"ALTER TABLE preferences ADD COLUMN {name} {spec}")
        scaled = await db.get_dict_value("settings", "RatingsOnTen")
        if scaled != "1":
            row = await db.fetchone("SELECT MAX(rating) AS m FROM reviews")
            max_rating = float(row["m"]) if row and row["m"] is not None else 0.0
            if 0 < max_rating <= 5:
                await db.execute("UPDATE reviews SET rating = rating * 2")
            await db.set_dict_value("settings", "RatingsOnTen", "1")
        self._schema_ready.add(guild.id)

    async def get_favorites(
        self, guild: discord.Guild, user_id: int
    ) -> list[tuple[MediaHit, float | None] | None]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT f.slot, m.*, r.rating AS fav_rating
               FROM favorites f
               JOIN media m ON m.id = f.media_id
               LEFT JOIN reviews r ON r.media_id = f.media_id AND r.user_id = f.user_id
               WHERE f.user_id=?""",
            user_id,
        )
        slots: list[tuple[MediaHit, float | None] | None] = [None, None, None]
        for row in rows:
            slot = int(row["slot"])
            if slot < 1 or slot > 3:
                continue
            rating = row["fav_rating"]
            slots[slot - 1] = (hit_from_row(row), float(rating) if rating is not None else None)
        return slots

    async def set_favorite(
        self, guild: discord.Guild, user_id: int, slot: int, hit: MediaHit
    ) -> None:
        if slot not in FAVORITE_LABELS:
            return
        media_id = await self.upsert_media(guild, hit)
        db = self.data.get(guild)
        await db.execute("DELETE FROM favorites WHERE user_id=? AND media_id=?", user_id, media_id)
        await db.execute(
            """INSERT INTO favorites (user_id, slot, media_id) VALUES (?, ?, ?)
               ON CONFLICT(user_id, slot) DO UPDATE SET media_id=excluded.media_id""",
            user_id,
            slot,
            media_id,
        )

    async def clear_favorite(self, guild: discord.Guild, user_id: int, slot: int) -> None:
        await self.data.get(guild).execute(
            "DELETE FROM favorites WHERE user_id=? AND slot=?",
            user_id,
            slot,
        )

    async def add_watchlist(self, guild: discord.Guild, user_id: int, hit: MediaHit) -> None:
        await self._ensure_schema(guild)
        media_id = await self.upsert_media(guild, hit)
        await self.data.get(guild).execute(
            """INSERT INTO watchlist (user_id, media_id, added_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id, media_id) DO UPDATE SET added_at=excluded.added_at""",
            user_id,
            media_id,
            int(time.time()),
        )

    async def remove_watchlist(self, guild: discord.Guild, user_id: int, media_id: int) -> None:
        await self._ensure_schema(guild)
        await self.data.get(guild).execute(
            "DELETE FROM watchlist WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )

    async def is_on_watchlist(self, guild: discord.Guild, user_id: int, media_id: int) -> bool:
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            "SELECT 1 AS n FROM watchlist WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )
        return row is not None

    async def load_watchlist(
        self, guild: discord.Guild, user_id: int
    ) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT w.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                      m.poster_url, m.url, m.overview, m.genres, m.extra_json
               FROM watchlist w JOIN media m ON m.id = w.media_id
               WHERE w.user_id=?
               ORDER BY w.added_at DESC""",
            user_id,
        )
        return [(hit_from_row(row), row) for row in rows]

    async def draw_watchlist(
        self,
        guild: discord.Guild,
        *,
        user_id: int | None,
        media_type: str = "all",
        period: str = "all",
    ) -> MediaHit | None:
        await self._ensure_schema(guild)
        since = int(time.time()) - PERIOD_SECONDS[period] if period in PERIOD_SECONDS else 0
        db = self.data.get(guild)
        clauses = ["(? = 0 OR w.added_at >= ?)"]
        args: list[Any] = [since, since]
        if user_id is not None:
            clauses.append("w.user_id=?")
            args.append(user_id)
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        where = " AND ".join(clauses)
        rows = await db.fetchall(
            f"""SELECT m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                       m.poster_url, m.url, m.overview, m.genres, m.extra_json
                FROM watchlist w JOIN media m ON m.id = w.media_id
                WHERE {where}
                GROUP BY w.media_id""",
            *args,
        )
        if not rows:
            return None
        return hit_from_row(random.choice(rows))

    def can_edit_shared_list(self, record: dict[str, Any], user_id: int, editor_ids: list[int]) -> bool:
        if int(record["owner_id"]) == user_id:
            return True
        mode = record.get("edit_mode") or "owner"
        if mode == "public":
            return True
        return mode == "members" and user_id in editor_ids

    def _shared_list_from_row(self, row: Any, *, item_count: int | None = None) -> dict[str, Any]:
        count = item_count
        if count is None:
            try:
                count = int(row["item_count"] or 0)
            except (KeyError, IndexError, TypeError):
                count = 0
        return {
            "id": int(row["id"]),
            "owner_id": int(row["owner_id"]),
            "title": row["title"] or "Sans titre",
            "description": row["description"] or "",
            "edit_mode": row["edit_mode"] if row["edit_mode"] in LIST_EDIT_MODES else "owner",
            "created_at": int(row["created_at"] or 0),
            "item_count": count,
        }

    async def load_shared_lists(self, guild: discord.Guild) -> list[dict[str, Any]]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT l.*, COUNT(i.media_id) AS item_count
               FROM shared_lists l
               LEFT JOIN shared_list_items i ON i.list_id = l.id
               GROUP BY l.id
               ORDER BY l.created_at DESC"""
        )
        return [self._shared_list_from_row(row) for row in rows]

    async def get_shared_list(self, guild: discord.Guild, list_id: int) -> dict[str, Any] | None:
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            """SELECT l.*, COUNT(i.media_id) AS item_count
               FROM shared_lists l
               LEFT JOIN shared_list_items i ON i.list_id = l.id
               WHERE l.id=?
               GROUP BY l.id""",
            list_id,
        )
        return self._shared_list_from_row(row) if row else None

    async def count_owned_lists(self, guild: discord.Guild, user_id: int) -> int:
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            "SELECT COUNT(*) AS n FROM shared_lists WHERE owner_id=?",
            user_id,
        )
        return int(row["n"]) if row else 0

    async def create_shared_list(
        self, guild: discord.Guild, user_id: int, title: str, description: str
    ) -> dict[str, Any]:
        await self._ensure_schema(guild)
        prefs = await self.get_user_prefs(guild, user_id)
        edit_mode = prefs.default_list_edit if prefs.default_list_edit in LIST_EDIT_MODES else "owner"
        now = int(time.time())
        db = self.data.get(guild)
        await db.execute(
            """INSERT INTO shared_lists (owner_id, title, description, edit_mode, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            user_id,
            title.strip()[:LIST_TITLE_MAX],
            description.strip()[:LIST_DESC_MAX],
            edit_mode,
            now,
        )
        row = await db.fetchone("SELECT last_insert_rowid() AS id")
        created = await self.get_shared_list(guild, int(row["id"])) if row else None
        assert created is not None
        return created

    async def update_shared_list(
        self,
        guild: discord.Guild,
        list_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        edit_mode: str | None = None,
    ) -> None:
        await self._ensure_schema(guild)
        record = await self.get_shared_list(guild, list_id)
        if record is None:
            return
        await self.data.get(guild).execute(
            "UPDATE shared_lists SET title=?, description=?, edit_mode=? WHERE id=?",
            (title.strip()[:LIST_TITLE_MAX] if title is not None else record["title"]),
            (description.strip()[:LIST_DESC_MAX] if description is not None else record["description"]),
            (edit_mode if edit_mode in LIST_EDIT_MODES else record["edit_mode"]),
            list_id,
        )

    async def delete_shared_list(self, guild: discord.Guild, list_id: int) -> None:
        await self._ensure_schema(guild)
        db = self.data.get(guild)
        await db.execute("DELETE FROM shared_list_items WHERE list_id=?", list_id)
        await db.execute("DELETE FROM shared_list_editors WHERE list_id=?", list_id)
        await db.execute("DELETE FROM shared_lists WHERE id=?", list_id)

    async def load_shared_list_editors(self, guild: discord.Guild, list_id: int) -> list[int]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            "SELECT user_id FROM shared_list_editors WHERE list_id=?",
            list_id,
        )
        return [int(row["user_id"]) for row in rows]

    async def set_shared_list_editors(
        self, guild: discord.Guild, list_id: int, user_ids: list[int]
    ) -> None:
        await self._ensure_schema(guild)
        db = self.data.get(guild)
        await db.execute("DELETE FROM shared_list_editors WHERE list_id=?", list_id)
        unique = []
        seen: set[int] = set()
        for user_id in user_ids:
            if user_id in seen:
                continue
            seen.add(user_id)
            unique.append(user_id)
        if unique:
            await db.executemany(
                "INSERT INTO shared_list_editors (list_id, user_id) VALUES (?, ?)",
                [(list_id, user_id) for user_id in unique[:25]],
            )

    async def load_shared_list_items(
        self, guild: discord.Guild, list_id: int
    ) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT i.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                      m.poster_url, m.url, m.overview, m.genres, m.extra_json
               FROM shared_list_items i JOIN media m ON m.id = i.media_id
               WHERE i.list_id=?
               ORDER BY i.added_at DESC""",
            list_id,
        )
        return [(hit_from_row(row), row) for row in rows]

    async def add_shared_list_item(
        self, guild: discord.Guild, list_id: int, user_id: int, hit: MediaHit
    ) -> str | None:
        await self._ensure_schema(guild)
        record = await self.get_shared_list(guild, list_id)
        if record is None:
            return "Cette liste n'existe plus."
        if record["item_count"] >= MAX_LIST_ITEMS:
            return f"Cette liste est pleine ({MAX_LIST_ITEMS} œuvres)."
        media_id = await self.upsert_media(guild, hit)
        existing = await self.data.get(guild).fetchone(
            "SELECT 1 AS n FROM shared_list_items WHERE list_id=? AND media_id=?",
            list_id,
            media_id,
        )
        if existing:
            return "Cette œuvre est déjà dans la liste."
        await self.data.get(guild).execute(
            "INSERT INTO shared_list_items (list_id, media_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            list_id,
            media_id,
            user_id,
            int(time.time()),
        )
        return None

    async def remove_shared_list_item(
        self, guild: discord.Guild, list_id: int, media_id: int
    ) -> None:
        await self._ensure_schema(guild)
        await self.data.get(guild).execute(
            "DELETE FROM shared_list_items WHERE list_id=? AND media_id=?",
            list_id,
            media_id,
        )

    async def draw_shared_list(
        self,
        guild: discord.Guild,
        list_id: int,
        *,
        media_type: str = "all",
        period: str = "all",
    ) -> MediaHit | None:
        items = await self.load_shared_list_items(guild, list_id)
        since = int(time.time()) - PERIOD_SECONDS[period] if period in PERIOD_SECONDS else 0
        pool = []
        for hit, row in items:
            if media_type != "all" and hit.media_type != media_type:
                continue
            added = 0
            try:
                added = int(row["added_at"] or 0)
            except (KeyError, IndexError, TypeError):
                if isinstance(row, dict):
                    added = int(row.get("added_at") or 0)
            if since and added < since:
                continue
            pool.append(hit)
        if not pool:
            return None
        return random.choice(pool)

    async def load_journal(
        self, guild: discord.Guild, user_id: int, media_type: str = "all"
    ) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        db = self.data.get(guild)
        if media_type == "all":
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=?
                   ORDER BY r.updated_at DESC""",
                user_id,
            )
        else:
            rows = await db.fetchall(
                """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                          m.poster_url, m.url, m.overview, m.genres, m.extra_json
                   FROM reviews r JOIN media m ON m.id = r.media_id
                   WHERE r.user_id=? AND m.media_type=?
                   ORDER BY r.updated_at DESC""",
                user_id,
                media_type,
            )
        return [(hit_from_row(row), row) for row in rows]

    async def load_recent(self, guild: discord.Guild, *, limit: int = 40) -> list[tuple[MediaHit, Any]]:
        await self._ensure_schema(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT r.*, m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,
                      m.poster_url, m.url, m.overview, m.genres, m.extra_json
               FROM reviews r JOIN media m ON m.id = r.media_id
               ORDER BY r.updated_at DESC
               LIMIT ?""",
            limit,
        )
        return [(hit_from_row(row), row) for row in rows]

    async def load_catalog(
        self,
        guild: discord.Guild,
        *,
        query: str | None = None,
        member_id: int | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> list[tuple[MediaHit, float, int]]:
        sql = """SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n
                 FROM media m JOIN reviews r ON r.media_id = m.id"""
        clauses: list[str] = []
        args: list[Any] = []
        if member_id:
            clauses.append("r.user_id=?")
            args.append(member_id)
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        if min_rating is not None:
            clauses.append("r.rating>=?")
            args.append(min_rating)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY m.id"
        rows = await self.data.get(guild).fetchall(sql, *args)
        items = [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]
        if query:
            items = fuzzy.finder(
                query,
                items,
                key=lambda item: f"{item[0].title} {item[0].subtitle} {item[0].year or ''}",
            )
        return items

    async def load_top(
        self,
        guild: discord.Guild,
        *,
        media_type: str = "all",
        period: str = "all",
    ) -> list[tuple[MediaHit, float, int]]:
        sql = """SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n
                 FROM media m JOIN reviews r ON r.media_id = m.id"""
        clauses: list[str] = []
        args: list[Any] = []
        if media_type != "all":
            clauses.append("m.media_type=?")
            args.append(media_type)
        if period in PERIOD_SECONDS:
            clauses.append("r.updated_at>=?")
            args.append(int(time.time()) - PERIOD_SECONDS[period])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY m.id ORDER BY avg_rating DESC, n DESC LIMIT 25"
        rows = await self.data.get(guild).fetchall(sql, *args)
        return [(hit_from_row(row), float(row["avg_rating"]), int(row["n"])) for row in rows]

    async def grant_review_xp(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        created: bool,
        pioneer: bool,
        new_comment: bool,
    ) -> XpAward:
        await self.ensure_progress(guild)
        db = self.data.get(guild)
        today = time.strftime("%Y-%m-%d")
        row = await db.fetchone("SELECT * FROM profiles WHERE user_id=?", user_id)
        if row is None:
            xp = daily_xp = daily_awards = 0
            daily_date = ""
            last_level = 1
        else:
            xp = int(row["xp"])
            daily_xp = int(row["daily_xp"])
            daily_date = row["daily_date"] or ""
            daily_awards = int(row["daily_awards"])
            last_level = int(row["last_level"])
        if daily_date != today:
            daily_xp = 0
            daily_awards = 0
            daily_date = today
        base = compute_review_xp(created=created, pioneer=pioneer, new_comment=new_comment)
        gained, capped = apply_daily_limits(base, awards_today=daily_awards, daily_xp=daily_xp)
        xp += gained
        daily_xp += gained
        if gained:
            daily_awards += 1
        new_level = level_for_xp(xp)
        await db.execute(
            """INSERT INTO profiles (user_id, xp, daily_xp, daily_date, daily_awards, last_level)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 xp=excluded.xp,
                 daily_xp=excluded.daily_xp,
                 daily_date=excluded.daily_date,
                 daily_awards=excluded.daily_awards,
                 last_level=excluded.last_level""",
            user_id,
            xp,
            daily_xp,
            daily_date,
            daily_awards,
            new_level,
        )
        return XpAward(
            gained=gained,
            total=xp,
            daily=daily_xp,
            level=new_level,
            previous_level=last_level,
            capped=capped,
        )

    def social_line_for_reviews(
        self,
        guild: discord.Guild,
        reviews: list[Any],
        *,
        viewer_id: int | None,
    ) -> str:
        if not reviews:
            return ""

        groups: dict[float, list[int]] = {}
        for row in reviews:
            user_id = int(row["user_id"])
            if viewer_id is not None and user_id == viewer_id:
                continue
            groups.setdefault(float(row["rating"]), []).append(user_id)
        if not groups:
            return ""

        rating, user_ids = max(groups.items(), key=lambda item: (len(item[1]), item[0]))
        named_max = 3
        shown = user_ids[:named_max]
        extra = max(0, len(user_ids) - named_max)
        people = _format_people_fr([_mention_silent(uid) for uid in shown], extra)
        verb = "a mis" if len(user_ids) == 1 else "ont mis"
        return f"{people} {verb} {format_score(rating)}"

    async def list_affinities(self, guild: discord.Guild, user_id: int) -> list[Affinity]:
        await self.ensure_progress(guild)
        rows = await self.data.get(guild).fetchall(
            """SELECT r2.user_id AS other_id, r1.rating AS left_rating, r2.rating AS right_rating,
                      m.title, m.year
               FROM reviews r1
               JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id != r1.user_id
               JOIN media m ON m.id = r1.media_id
               WHERE r1.user_id=?""",
            user_id,
        )
        grouped: dict[int, list[tuple[str, float, float]]] = {}
        for row in rows:
            other_id = int(row["other_id"])
            if guild.get_member(other_id) is None:
                continue
            title = row["title"] + (f" ({row['year']})" if row["year"] else "")
            grouped.setdefault(other_id, []).append(
                (title, float(row["left_rating"]), float(row["right_rating"]))
            )
        affinities: list[Affinity] = []
        for other_id, pairs in grouped.items():
            if len(pairs) < MIN_AFFINITY_OVERLAP:
                continue
            ranked = sorted(pairs, key=lambda item: abs(item[1] - item[2]))
            affinities.append(
                Affinity(
                    user_id=other_id,
                    overlap=len(pairs),
                    percent=agreement_percent([(left, right) for _title, left, right in pairs]),
                    agreements=[(title, left, right) for title, left, right in ranked[:3] if abs(left - right) <= 1],
                    disagreements=[
                        (title, left, right) for title, left, right in reversed(ranked[-3:]) if abs(left - right) >= 1
                    ],
                )
            )
        affinities.sort(key=lambda item: (-item.percent, -item.overlap))
        return affinities

    async def get_affinity(self, guild: discord.Guild, left_id: int, right_id: int) -> Affinity | None:
        rows = await self.data.get(guild).fetchall(
            """SELECT r1.rating AS left_rating, r2.rating AS right_rating, m.title, m.year
               FROM reviews r1
               JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id=?
               JOIN media m ON m.id = r1.media_id
               WHERE r1.user_id=?""",
            right_id,
            left_id,
        )
        if not rows:
            return None
        pairs = [
            (
                row["title"] + (f" ({row['year']})" if row["year"] else ""),
                float(row["left_rating"]),
                float(row["right_rating"]),
            )
            for row in rows
        ]
        ranked = sorted(pairs, key=lambda item: abs(item[1] - item[2]))
        return Affinity(
            user_id=right_id,
            overlap=len(pairs),
            percent=agreement_percent([(left, right) for _title, left, right in pairs]),
            agreements=[(title, left, right) for title, left, right in ranked[:3] if abs(left - right) <= 1],
            disagreements=[
                (title, left, right) for title, left, right in reversed(ranked[-3:]) if abs(left - right) >= 1
            ],
        )

    # ------------------------------------------------------------------
    # Persistance médias / critiques
    # ------------------------------------------------------------------

    async def upsert_media(self, guild: discord.Guild, hit: MediaHit) -> int:
        db = self.data.get(guild)
        await db.execute(
            """INSERT INTO media (source, source_id, media_type, title, subtitle, year, poster_url, url, overview, genres, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, source_id, media_type) DO UPDATE SET
                 title=excluded.title,
                 subtitle=excluded.subtitle,
                 year=excluded.year,
                 poster_url=excluded.poster_url,
                 url=excluded.url,
                 overview=excluded.overview,
                 genres=excluded.genres,
                 extra_json=excluded.extra_json""",
            hit.source,
            hit.source_id,
            hit.media_type,
            hit.title,
            hit.subtitle,
            hit.year,
            hit.poster_url,
            hit.url,
            hit.overview,
            "|".join(hit.genres),
            json.dumps(hit.extra, ensure_ascii=False),
        )
        row = await db.fetchone(
            "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
            hit.source,
            hit.source_id,
            hit.media_type,
        )
        assert row is not None
        return int(row["id"])

    async def lookup_media_id(self, guild: discord.Guild, hit: MediaHit) -> int | None:
        row = await self.data.get(guild).fetchone(
            "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
            hit.source,
            hit.source_id,
            hit.media_type,
        )
        return int(row["id"]) if row else None

    async def media_stats(self, guild: discord.Guild, media_id: int | None) -> tuple[float | None, int]:
        if not media_id:
            return None, 0
        row = await self.data.get(guild).fetchone(
            "SELECT AVG(rating) AS avg_rating, COUNT(*) AS n FROM reviews WHERE media_id=?",
            media_id,
        )
        if not row or not row["n"]:
            return None, 0
        return float(row["avg_rating"]), int(row["n"])

    async def list_reviews(self, guild: discord.Guild, media_id: int) -> list[Any]:
        await self._ensure_schema(guild)
        return await self.data.get(guild).fetchall(
            "SELECT * FROM reviews WHERE media_id=? ORDER BY updated_at DESC",
            media_id,
        )

    async def get_review(self, guild: discord.Guild, user_id: int, media_id: int) -> dict | None:
        await self._ensure_schema(guild)
        row = await self.data.get(guild).fetchone(
            "SELECT * FROM reviews WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )
        if row is None:
            return None
        return {
            "rating": float(row["rating"]),
            "comment": row["comment"] or "",
            "updated_at": row["updated_at"],
            "experienced_at": experienced_from_row(row),
            "spoiler": row_spoiler(row),
        }

    async def upsert_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> tuple[bool, XpAward]:
        await self._ensure_schema(guild)
        max_len = await self.get_comment_max(guild)
        comment = comment.strip()[:max_len]
        media_id = await self.upsert_media(guild, hit)
        existing = await self.get_review(guild, user.id, media_id)
        count_row = await self.data.get(guild).fetchone(
            "SELECT COUNT(*) AS n FROM reviews WHERE media_id=?", media_id
        )
        pioneer = int(count_row["n"] if count_row else 0) == 0
        new_comment = bool(comment) and (not existing or not existing["comment"])
        now = int(time.time())
        created = existing is None
        if existing:
            await self.data.get(guild).execute(
                "UPDATE reviews SET rating=?, comment=?, experienced_at=?, spoiler=?, updated_at=? WHERE user_id=? AND media_id=?",
                rating,
                comment,
                experienced_at,
                int(spoiler),
                now,
                user.id,
                media_id,
            )
        else:
            await self.data.get(guild).execute(
                "INSERT INTO reviews (user_id, media_id, rating, comment, experienced_at, spoiler, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                user.id,
                media_id,
                rating,
                comment,
                experienced_at,
                int(spoiler),
                now,
                now,
            )
        await self.remove_watchlist(guild, user.id, media_id)
        award = await self.grant_review_xp(
            guild,
            user.id,
            created=created,
            pioneer=pioneer,
            new_comment=new_comment,
        )
        return created, award

    async def delete_review(self, guild: discord.Guild, user_id: int, hit: MediaHit) -> None:
        media_id = await self.lookup_media_id(guild, hit)
        if media_id:
            await self.data.get(guild).execute(
                "DELETE FROM reviews WHERE user_id=? AND media_id=?",
                user_id,
                media_id,
            )

    async def announce_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
        *,
        updated: bool,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> None:
        prefs = await self.get_user_prefs(guild, user.id)
        if not prefs.announce_notes:
            return
        channel = await self.get_announce_channel(guild, hit.media_type)
        if channel is None:
            return
        titles = await self.get_titles(guild, [user.id])
        mention = _mention(guild, self.bot, user.id)
        grade = titles.get(user.id, title_for_level(1))
        posted_at = int(time.time())
        wid = create_record(
            {
                "kind": "announce",
                "guild_id": guild.id,
                "user_id": user.id,
                "hit": hit_to_dict(hit),
                "mention": mention,
                "title": grade,
                "rating": rating,
                "comment": comment,
                "updated": updated,
                "experienced_at": experienced_at,
                "spoiler": spoiler,
                "posted_at": posted_at,
            },
            ttl=ANNOUNCE_TTL,
        )
        view = build_announce_view(
            hit,
            mention=mention,
            title=grade,
            rating=rating,
            comment=comment,
            updated=updated,
            experienced_at=experienced_at,
            spoiler=spoiler,
            posted_at=posted_at,
            wid=wid,
            live=True,
        )
        try:
            message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            mark_stripped(wid)
            logger.error("Impossible d'annoncer une critique sur %s : %s", guild.name, exc)
            return
        bind_record(wid, message.channel.id, message.id)

    async def _search_or_reply(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str,
    ) -> list[MediaHit] | None:
        spec = parse_search_query(query)
        if not spec.lookup_id and len(spec.query.strip()) < 2:
            await interaction.edit_original_response(content="**Erreur ·** La recherche doit contenir au moins 2 caractères.")
            return None
        if self.catalog is None:
            await interaction.edit_original_response(content="**Erreur ·** Catalogue média indisponible.")
            return None
        wants_tmdb = spec.source in (None, "tmdb") and (
            spec.lookup_id
            or media_type == "all"
            or search_pref_includes(media_type, "movie", "tv")
            or spec.media_type in ("movie", "tv")
        )
        if wants_tmdb and spec.source == "tmdb" and not self.catalog.tmdb.available:
            await interaction.edit_original_response(content="**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`).")
            return None
        if search_pref_includes(media_type, "movie", "tv") and spec.source is None and not self.catalog.tmdb.available:
            await interaction.edit_original_response(content="**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`).")
            return None
        wants_spotify = spec.source == "spotify" or (
            spec.source is None and search_pref_includes(media_type, "album", "track")
        )
        if wants_spotify and not self.catalog.spotify.available:
            await interaction.edit_original_response(
                content="**Erreur ·** Clés Spotify manquantes (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` dans `.env`)."
            )
            return None
        try:
            hits = await self.catalog.search(query.strip(), media_type)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Recherche /search échouée : %s", exc)
            await interaction.edit_original_response(
                content="**Erreur ·** La recherche a échoué. Réessaie dans un instant."
            )
            return None
        if not hits:
            await interaction.edit_original_response(
                content=f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} »."
            )
            return None
        return hits

    # ==================================================================
    # Commandes
    # ==================================================================

    @app_commands.command(name="search")
    @app_commands.guild_only()
    @app_commands.rename(query="recherche", media_type="type", rating="note", comment="commentaire")
    @app_commands.describe(
        query="Titre, année, ou préfixe (tmdb:Dune, tmdb:27205, URL TMDB)",
        media_type="Restreindre à un type (sinon tes types /preferences)",
        rating="Note de 0 à 10 (entier, une étoile = 2 points)",
        comment="Court commentaire optionnel",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, rating=RATING_CHOICES)
    async def critique_search(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str | None = None,
        rating: float | None = None,
        comment: str | None = None,
    ) -> None:
        """Recherche une œuvre : ouvrir la fiche, noter, ou ajouter à voir."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        resolved_type = await self._resolve_search_type(guild, interaction.user.id, media_type)
        hits = await self._search_or_reply(interaction, query, resolved_type)
        if not hits:
            return
        view = MediaSessionView(
            self,
            guild,
            hits,
            author_id=interaction.user.id,
            ephemeral=True,
            pending_rating=rating,
            pending_comment=(comment or "").strip(),
        )
        await view.start(interaction, deferred=True)

    @critique_search.autocomplete("query")
    async def search_query_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip()
        if skip_note_autocomplete(current) or self.catalog is None:
            return []
        ns = getattr(interaction, "namespace", None)
        explicit = None
        if ns is not None:
            explicit = getattr(ns, "media_type", None) or getattr(ns, "type", None)
        guild = interaction.guild
        if isinstance(guild, discord.Guild):
            media_type = await self._resolve_search_type(guild, interaction.user.id, explicit)
        else:
            media_type = explicit if explicit in TYPE_META or explicit == "all" else "all"
        try:
            hits = await asyncio.wait_for(
                self.catalog.search(current, media_type, quick=True),
                timeout=2.0,
            )
        except Exception:
            return []
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for hit in hits:
            value = autocomplete_query_value(hit)
            if not value or value in seen:
                continue
            seen.add(value)
            year = f" ({hit.year})" if hit.year else ""
            name = pretty.shorten_text(f"{hit.title}{year} · {type_label(hit.media_type)}", 100)
            choices.append(app_commands.Choice(name=name, value=value[:100]))
            if len(choices) >= 25:
                break
        return choices

    async def _open_carnet(
        self,
        interaction: discord.Interaction,
        target: discord.Member | discord.User,
    ) -> None:
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.",
                    ephemeral=True,
                )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await self.ensure_progress(guild)
        xp = await self.get_profile_xp(guild, target.id)
        journal_entries = await self.load_journal(guild, target.id)
        review_count = len(journal_entries)
        average = (
            sum(float(row["rating"]) for _hit, row in journal_entries) / review_count
            if journal_entries else None
        )
        affinities = await self.list_affinities(guild, target.id)
        twin = affinities[0] if affinities else None
        rival = min(affinities, key=lambda item: (item.percent, -item.overlap)) if affinities else None
        title_ids = [target.id, *[item.user_id for item in affinities]]
        view = ProfileView(
            self,
            guild,
            target,
            xp=xp,
            review_count=review_count,
            average=average,
            twin=twin,
            rival=rival,
            titles=await self.get_titles(guild, title_ids),
            journal_entries=journal_entries,
            watchlist_entries=await self.load_watchlist(guild, target.id),
            affinities=affinities,
            viewer_id=interaction.user.id,
        )
        view._interaction = interaction
        await apply_view(interaction, view)
        await view.attach(interaction)

    @app_commands.command(name="carnet")
    @app_commands.guild_only()
    @app_commands.rename(member="membre")
    @app_commands.describe(member="Membre dont afficher le carnet, le journal et les affinités")
    async def critique_carnet(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Carnet d'un membre : profil, journal, à voir et affinités."""
        await self._open_carnet(interaction, member or interaction.user)

    @app_commands.guild_only()
    async def critique_carnet_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member | discord.User,
    ) -> None:
        await self._open_carnet(interaction, member)

    @app_commands.command(name="explore")
    @app_commands.guild_only()
    @app_commands.rename(query="recherche", member="membre", media_type="type", min_rating="note_min")
    @app_commands.describe(
        query="Filtrer le catalogue déjà noté du serveur",
        member="Limiter aux notes d'un membre",
        media_type="Filtrer par type",
        min_rating="Note minimale (sur l'œuvre ou la critique)",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, min_rating=RATING_CHOICES)
    async def critique_explore(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        member: discord.Member | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> None:
        """Feuillette ce que le serveur a déjà noté : récentes, catalogue et top."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        recent = await self.load_recent(guild)
        catalog = await self.load_catalog(
            guild,
            query=query,
            member_id=member.id if member else None,
            media_type=media_type,
            min_rating=min_rating,
        )
        top = await self.load_top(guild, media_type=media_type)
        subtitle_parts = []
        if query:
            subtitle_parts.append(f"« {pretty.shorten_text(query, 60)} »")
        if member:
            subtitle_parts.append(member.display_name)
        if media_type != "all":
            subtitle_parts.append(type_label(media_type))
        if min_rating is not None:
            subtitle_parts.append(f"≥ {format_score(min_rating)}")
        filtered = bool(query or member or min_rating is not None or media_type != "all")
        view = ServerHubView(
            self,
            guild,
            recent=recent,
            catalog=catalog,
            top=top,
            catalog_subtitle="  ·  ".join(subtitle_parts) or "Toutes les œuvres notées",
            media_type=media_type,
            tab="catalogue" if filtered else "recentes",
        )
        view._interaction = interaction
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
        await view.attach(interaction)

    async def _shared_list_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return []
        try:
            lists = await self.load_shared_lists(guild)
        except Exception:
            return []
        needle = (current or "").casefold()
        choices: list[app_commands.Choice[str]] = []
        for record in lists:
            if needle and needle not in record["title"].casefold():
                continue
            name = pretty.shorten_text(
                f"{record['title']} · {record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''}",
                100,
            )
            choices.append(app_commands.Choice(name=name, value=str(record["id"])))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="listes")
    @app_commands.guild_only()
    @app_commands.describe(liste="Ouvrir une liste directement")
    async def critique_listes(self, interaction: discord.Interaction, liste: str | None = None) -> None:
        """Listes communes du serveur : créer, éditer et tirer au hasard."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        list_id = int(liste) if liste and liste.isdigit() else None
        view: ReviewsLayout | None = None
        if list_id:
            view = await SharedListView.create(self, guild, list_id, viewer_id=interaction.user.id)
            if view is None:
                await interaction.edit_original_response(content="**Listes ·** Cette liste n'existe plus.")
                return
        else:
            view = await ListsHubView.create(self, guild, viewer_id=interaction.user.id)
        view._interaction = interaction
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
        await view.attach(interaction)

    @critique_listes.autocomplete("liste")
    async def listes_liste_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._shared_list_choices(interaction, current)

    @app_commands.command(name="tirage")
    @app_commands.guild_only()
    @app_commands.rename(member="membre", media_type="type", period="quand", liste="liste")
    @app_commands.describe(
        member="Tirer dans la liste à voir de ce membre (par défaut : la tienne)",
        media_type="Restreindre à un type",
        period="Quand l'œuvre a été ajoutée à la liste",
        liste="Tirer dans une liste commune (prioritaire sur le membre)",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, period=WHEN_CHOICES)
    async def critique_tirage(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        media_type: str = "all",
        period: str = "all",
        liste: str | None = None,
    ) -> None:
        """Tire une œuvre encore à voir, ou dans une liste commune."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        list_id = int(liste) if liste and liste.isdigit() else None
        if list_id:
            record = await self.get_shared_list(guild, list_id)
            if record is None:
                await interaction.edit_original_response(content="**Tirage ·** Cette liste n'existe plus.")
                return
            hit = await self.draw_shared_list(
                guild, list_id, media_type=media_type, period=period,
            )
            empty = f"**Tirage ·** « {record['title']} » n'a rien pour ce filtre."
        else:
            target = member or interaction.user
            hit = await self.draw_watchlist(
                guild,
                user_id=target.id,
                media_type=media_type,
                period=period,
            )
            empty = (
                "**Tirage ·** Ta liste à voir est vide."
                if target.id == interaction.user.id
                else f"**Tirage ·** La liste à voir de {target.display_name} est vide."
            )
        if hit is None:
            await interaction.edit_original_response(content=empty)
            return
        view = MediaSessionView(
            self,
            guild,
            [hit],
            author_id=interaction.user.id,
            ephemeral=True,
        )
        await view.start(interaction, deferred=True)

    @critique_tirage.autocomplete("liste")
    async def tirage_liste_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._shared_list_choices(interaction, current)

    @app_commands.command(name="preferences")
    @app_commands.guild_only()
    async def critique_preferences(self, interaction: discord.Interaction) -> None:
        """Tes défauts : date, listes, recherche et annonces."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        view = PreferencesView(
            self,
            guild,
            user=interaction.user,
            prefs=await self.get_user_prefs(guild, interaction.user.id),
        )
        await view.start(interaction)

    @app_commands.command(name="config")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def critique_config(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration des critiques (annonces, commentaires)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        review_count, media_count = await self.counts(guild)
        view = ReviewsConfigView(
            self,
            guild,
            announce_channels=await self.get_announce_channels(guild),
            comment_max=await self.get_comment_max(guild),
            review_count=review_count,
            media_count=media_count,
            api_status=self.catalog.status() if self.catalog else {},
        )
        await view.start(interaction)

    @app_commands.command(name="help")
    @app_commands.guild_only()
    async def critique_help(self, interaction: discord.Interaction) -> None:
        """Explique les commandes et comment noter."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        await interaction.response.send_message(view=HelpView(avatar_url=avatar), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reviews(bot))
