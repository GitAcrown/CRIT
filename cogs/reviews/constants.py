"""Constantes partagées du carnet : limites, types d'œuvres, choix des commandes."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .emojis import ALBUM, BOOK, GAME, MOVIE, MUSIC, TV


logger = logging.getLogger("CRIT.Reviews")


NO_PINGS = discord.AllowedMentions.none()


MENU_TIMEOUT = 840.0


VALID_RATINGS = tuple(range(11))


RATING_MAX = 10


LIKE_RATING = 7


BAD_RATING = 4


ALSO_PUBLIC_PERCENT = 70


ALSO_PUBLIC_GAP = 2


ALSO_LIKED_MIN = 2


ALSO_LIKED_LIMIT = 3


FOR_YOU_LIMIT = 24


FOR_YOU_MAX_GAP = 3.0


DEFAULT_COMMENT_MAX = 280


MIN_COMMENT_MAX = 50


MAX_COMMENT_MAX = 500


JOURNAL_PAGE = 4


REVIEWS_PAGE = 4  # 5 dépassait la limite de 40 composants avec la rangée de pages


CATALOG_PAGE = 8


LIST_PAGE = 4


MAX_SHARED_LISTS = 20


MAX_LIST_ITEMS = 100
MAX_FAVORITES = 6  # galerie du carnet


LIST_TITLE_MAX = 80


LIST_DESC_MAX = 200


LIST_EDIT_MODES = ("owner", "members", "public")


TEXT_DISPLAY_MAX = 4000


TYPE_META: dict[str, tuple[str, str]] = {
    "movie": (MOVIE, "Film"),
    "tv": (TV, "Série"),
    "game": (GAME, "Jeu"),
    "album": (ALBUM, "Album"),
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


TYPE_STATS = (
    ("movie", "film", "films"),
    ("tv", "série", "séries"),
    ("game", "jeu", "jeux"),
    ("album", "album", "albums"),
    ("track", "morceau", "morceaux"),
    ("book", "livre", "livres"),
)




STREAM_START_GRACE = 5.0


STREAM_END_GRACE = 30.0


STREAM_LINK_MAX_AGE = 12 * 3600


STREAM_PLAN_MAX = 3


STREAM_PLAN_HORIZON = 90 * 86400


PARIS_TZ = ZoneInfo("Europe/Paris")
