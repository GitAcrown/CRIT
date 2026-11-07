"""Données du carnet (base commune à tous les serveurs) : réglages, notes, XP, listes.

Notes, XP, signets, favoris et préférences appartiennent au membre et le suivent partout.
Les vues « serveur » (catalogue, moyennes, critiques d'une fiche, recommandations) ne
comptent que les membres présents sur ce serveur, via la table `members`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import random
import time
from typing import TYPE_CHECKING, Any

import discord

from utils import dataio, fuzzy

from .constants import (
    ALSO_LIKED_LIMIT,
    ALSO_LIKED_MIN,
    ALSO_PUBLIC_GAP,
    ALSO_PUBLIC_PERCENT,
    ANNOUNCE_ROUTE_ALL,
    BAD_RATING,
    DEFAULT_COMMENT_MAX,
    FAVORITE_LABELS,
    FOR_YOU_LIMIT,
    FOR_YOU_MAX_GAP,
    LIKE_RATING,
    LIST_DESC_MAX,
    LIST_EDIT_MODES,
    LIST_TITLE_MAX,
    logger,
    MAX_COMMENT_MAX,
    MAX_LIST_ITEMS,
    MIN_COMMENT_MAX,
    NO_PINGS,
    TYPE_META,
)
from .database import DB_NAME
from .formatting import (
    experienced_from_row,
    format_list_types,
    format_people_fr,
    format_score,
    format_stars,
    hit_from_row,
    list_accepts_type,
    list_types_label,
    user_display,
    parse_list_types,
    row_spoiler,
)
from .prefs import (
    DATE_PREF_VALUES,
    is_october_horror,
    normalize_search_pref,
    normalize_stream_remind,
    prefs_from_row,
    row_field,
    UserPrefs,
)
from .progress import (
    Affinity,
    agreement_percent,
    apply_daily_limits,
    compute_review_xp,
    event_is_open,
    format_unlocked_skins,
    level_for_xp,
    MIN_AFFINITY_OVERLAP,
    parse_unlocked_skins,
    resolve_star_skin,
    STAR_SKINS,
    StarSkin,
    title_for_level,
    XpAward,
)
from .providers import MediaHit
from .streaming import resolve_guild_text_channel
from .ui.fiche import also_liked_line

if TYPE_CHECKING:
    from discord.ext import commands

MEDIA_COLUMNS = (
    "m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year,"
    " m.poster_url, m.url, m.overview, m.genres, m.extra_json"
)
# Restreint une requête aux membres présents sur le serveur (paramètre : guild_id).
ON_SERVER = "JOIN members gm ON gm.user_id = {}.user_id AND gm.guild_id = ?"


class StorageMixin:
    """Accès à la base commune. Les vues passent la guilde pour les données propres au serveur."""

    bot: commands.Bot
    data: dataio.CogData
    _comment_max: dict[int, int]
    _prefs: dict[int, UserPrefs]
    _known_members: set[tuple[int, int]]

    @property
    def db(self) -> dataio.ModelDataManager:
        return self.data.get(DB_NAME)

    # ------------------------------------------------------------------
    # Membres présents par serveur
    # ------------------------------------------------------------------

    async def remember_member(self, guild_id: int, user_id: int) -> None:
        key = (guild_id, user_id)
        if key in self._known_members:
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO members (guild_id, user_id) VALUES (?, ?)",
            guild_id,
            user_id,
        )
        self._known_members.add(key)

    async def forget_member(self, guild_id: int, user_id: int) -> None:
        self._known_members.discard((guild_id, user_id))
        await self.db.execute("DELETE FROM members WHERE guild_id=? AND user_id=?", guild_id, user_id)

    async def has_carnet(self, user_id: int) -> bool:
        row = await self.db.fetchone(
            """SELECT 1 AS n FROM reviews WHERE user_id=?
               UNION ALL SELECT 1 FROM watchlist WHERE user_id=? LIMIT 1""",
            user_id,
            user_id,
        )
        return row is not None

    async def sync_members(self, guild: discord.Guild) -> None:
        """Aligne `members` sur la liste réelle du serveur pour tous les membres du carnet."""
        if not guild.chunked:
            return
        rows = await self.db.fetchall(
            "SELECT user_id FROM reviews UNION SELECT user_id FROM watchlist UNION SELECT user_id FROM profiles"
        )
        present = [(guild.id, int(row["user_id"])) for row in rows if guild.get_member(int(row["user_id"]))]
        await self.db.execute("DELETE FROM members WHERE guild_id=?", guild.id, commit=False)
        await self.db.executemany("INSERT INTO members (guild_id, user_id) VALUES (?, ?)", present)
        self._known_members = {key for key in self._known_members if key[0] != guild.id}
        self._known_members.update(present)

    # ------------------------------------------------------------------
    # Réglages du serveur
    # ------------------------------------------------------------------

    async def _guild_setting(self, guild: discord.Guild, key: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
            guild.id,
            key,
        )
        return str(row["value"]) if row else None

    async def _set_guild_setting(self, guild: discord.Guild, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
            guild.id,
            key,
            str(value),
        )

    async def get_announce_route_ids(self, guild: discord.Guild) -> dict[str, int]:
        routes: dict[str, int] = {}
        default_id = await self._guild_setting(guild, "AnnounceChannelID")
        if default_id and default_id.isdigit() and int(default_id):
            routes[ANNOUNCE_ROUTE_ALL] = int(default_id)
        raw = await self._guild_setting(guild, "AnnounceChannelsByType")
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
        if route == ANNOUNCE_ROUTE_ALL:
            await self._set_guild_setting(guild, "AnnounceChannelID", channel_id or 0)
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
        await self._set_guild_setting(guild, "AnnounceChannelsByType", json.dumps(typed))

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
        raw = await self._guild_setting(guild, "MaxCommentLength")
        value = int(raw) if raw and raw.isdigit() and int(raw) else DEFAULT_COMMENT_MAX
        value = max(MIN_COMMENT_MAX, min(MAX_COMMENT_MAX, value))
        self._comment_max[guild.id] = value
        return value

    async def set_comment_max(self, guild: discord.Guild, value: int) -> None:
        value = max(MIN_COMMENT_MAX, min(MAX_COMMENT_MAX, int(value)))
        await self._set_guild_setting(guild, "MaxCommentLength", value)
        self._comment_max[guild.id] = value

    # ------------------------------------------------------------------
    # Préférences (par membre, communes à tous les serveurs)
    # ------------------------------------------------------------------

    async def get_user_prefs(self, guild: discord.Guild | None, user_id: int) -> UserPrefs:
        cached = self._prefs.get(user_id)
        if cached is not None:
            return cached
        row = await self.db.fetchone("SELECT * FROM preferences WHERE user_id=?", user_id)
        prefs = prefs_from_row(row)
        self._prefs[user_id] = prefs
        return prefs

    async def set_user_prefs(self, guild: discord.Guild | None, user_id: int, **updates: Any) -> UserPrefs:
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
        for flag in ("announce_notes", "stream_voice_status", "bookmark_notify"):
            if flag in updates:
                cleaned[flag] = bool(updates[flag])
        if "stream_remind" in updates:
            cleaned["stream_remind"] = normalize_stream_remind(updates["stream_remind"])
        if "star_skin" in updates:
            cleaned["star_skin"] = resolve_star_skin(
                updates["star_skin"],
                owned=current.unlocked_skins,
            ).id
        if "unlocked_skins" in updates:
            cleaned["unlocked_skins"] = parse_unlocked_skins(updates["unlocked_skins"])
        prefs = replace(current, **cleaned) if cleaned else current
        await self.db.execute(
            """INSERT OR REPLACE INTO preferences
               (user_id, default_date, default_list_edit, default_search_type, announce_notes, stream_remind,
                stream_voice_status, star_skin, unlocked_skins, bookmark_notify)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            user_id,
            prefs.default_date,
            prefs.default_list_edit,
            prefs.default_search_type,
            int(prefs.announce_notes),
            int(prefs.stream_remind),
            int(prefs.stream_voice_status),
            prefs.star_skin,
            format_unlocked_skins(prefs.unlocked_skins),
            int(prefs.bookmark_notify),
        )
        self._prefs[user_id] = prefs
        return prefs

    async def star_skin_for(self, guild: discord.Guild | None, user_id: int) -> StarSkin:
        prefs = await self.get_user_prefs(guild, user_id)
        level = level_for_xp(await self.get_profile_xp(guild, user_id))
        return resolve_star_skin(prefs.star_skin, level=level, owned=prefs.unlocked_skins)

    async def grant_event_collections(
        self,
        guild: discord.Guild,
        user_id: int,
        hit: MediaHit,
    ) -> list[StarSkin]:
        """Débloque les collections d'événement encore gagnables pour cette note."""
        prefs = await self.get_user_prefs(guild, user_id)
        owned = set(prefs.unlocked_skins)
        earned: list[StarSkin] = []
        for skin in STAR_SKINS:
            if not skin.event or skin.id in owned or not event_is_open(skin.event):
                continue
            if skin.event == "october_horror" and is_october_horror(hit):
                owned.add(skin.id)
                earned.append(skin)
        if not earned:
            return []
        await self.set_user_prefs(guild, user_id, unlocked_skins=owned)
        return earned

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
        """Notes et œuvres notées par les membres de ce serveur."""
        row = await self.db.fetchone(
            f"""SELECT COUNT(*) AS n, COUNT(DISTINCT r.media_id) AS media
                FROM reviews r {ON_SERVER.format("r")}""",
            guild.id,
        )
        return (int(row["n"]), int(row["media"])) if row else (0, 0)

    # ------------------------------------------------------------------
    # XP / affinités
    # ------------------------------------------------------------------

    async def get_profile_xp(self, guild: discord.Guild | None, user_id: int) -> int:
        row = await self.db.fetchone("SELECT xp FROM profiles WHERE user_id=?", user_id)
        return int(row["xp"]) if row else 0

    async def get_titles(self, guild: discord.Guild | None, user_ids: list[int]) -> dict[int, str]:
        unique = list({int(user_id) for user_id in user_ids})
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        rows = await self.db.fetchall(
            f"SELECT user_id, xp FROM profiles WHERE user_id IN ({placeholders})",
            *unique,
        )
        xp_by_user = {int(row["user_id"]): int(row["xp"]) for row in rows}
        return {user_id: title_for_level(level_for_xp(xp_by_user.get(user_id, 0))) for user_id in unique}

    async def grant_review_xp(
        self,
        guild: discord.Guild | None,
        user_id: int,
        *,
        created: bool,
        pioneer: bool,
        new_comment: bool,
    ) -> XpAward:
        today = time.strftime("%Y-%m-%d")
        row = await self.db.fetchone("SELECT * FROM profiles WHERE user_id=?", user_id)
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
        await self.db.execute(
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

        scored: list[tuple[int, float]] = []
        for row in reviews:
            user_id = int(row["user_id"])
            if viewer_id is not None and user_id == viewer_id:
                continue
            scored.append((user_id, float(row["rating"])))
        if not scored:
            return ""

        good = [(user_id, rating) for user_id, rating in scored if rating >= LIKE_RATING]
        bad = [(user_id, rating) for user_id, rating in scored if rating <= BAD_RATING]
        total = len(scored)
        if len(good) * 2 > total:
            camp = good
            liked = True
        elif len(bad) * 2 > total:
            camp = bad
            liked = False
        else:
            return ""

        camp.sort(key=lambda item: item[1], reverse=liked)
        user_ids = [user_id for user_id, _rating in camp]
        scores = {rating for _user_id, rating in camp}
        if len(scores) == 1:
            verb = "a mis" if len(user_ids) == 1 else "ont mis"
            tail = f" {format_score(next(iter(scores)))}"
        elif liked:
            verb = "a aimé" if len(user_ids) == 1 else "ont aimé"
            tail = ""
        else:
            verb = "n'a pas aimé" if len(user_ids) == 1 else "n'ont pas aimé"
            tail = ""
        shown = user_ids[:3]
        extra = max(0, len(user_ids) - 3)
        # Noms en gras plutôt que des mentions : plus lisible qu'une rangée de pastilles.
        names = [f"**{user_display(guild, self.bot, uid)[0]}**" for uid in shown]
        people = format_people_fr(names, extra)
        return f"{people} {verb}{tail}"

    @staticmethod
    def _affinity(user_id: int, pairs: list[tuple[str, float, float]]) -> Affinity:
        ranked = sorted(pairs, key=lambda item: abs(item[1] - item[2]))
        return Affinity(
            user_id=user_id,
            overlap=len(pairs),
            percent=agreement_percent([(left, right) for _title, left, right in pairs]),
            agreements=[(title, left, right) for title, left, right in ranked[:3] if abs(left - right) <= 1],
            disagreements=[
                (title, left, right) for title, left, right in reversed(ranked[-3:]) if abs(left - right) >= 1
            ],
        )

    async def list_affinities(self, guild: discord.Guild, user_id: int) -> list[Affinity]:
        rows = await self.db.fetchall(
            f"""SELECT r2.user_id AS other_id, r1.rating AS left_rating, r2.rating AS right_rating,
                       m.title, m.year
                FROM reviews r1
                JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id != r1.user_id
                {ON_SERVER.format("r2")}
                JOIN media m ON m.id = r1.media_id
                WHERE r1.user_id=?""",
            guild.id,
            user_id,
        )
        grouped: dict[int, list[tuple[str, float, float]]] = {}
        for row in rows:
            title = row["title"] + (f" ({row['year']})" if row["year"] else "")
            grouped.setdefault(int(row["other_id"]), []).append(
                (title, float(row["left_rating"]), float(row["right_rating"]))
            )
        affinities = [
            self._affinity(other_id, pairs)
            for other_id, pairs in grouped.items()
            if len(pairs) >= MIN_AFFINITY_OVERLAP
        ]
        affinities.sort(key=lambda item: (-item.percent, -item.overlap))
        return affinities

    async def get_affinity(self, guild: discord.Guild, left_id: int, right_id: int) -> Affinity | None:
        rows = await self.db.fetchall(
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
        return self._affinity(right_id, pairs)

    # ------------------------------------------------------------------
    # Favoris / signets
    # ------------------------------------------------------------------

    async def get_favorites(
        self, guild: discord.Guild | None, user_id: int
    ) -> list[tuple[MediaHit, float | None] | None]:
        rows = await self.db.fetchall(
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
        self, guild: discord.Guild | None, user_id: int, slot: int, hit: MediaHit
    ) -> None:
        if slot not in FAVORITE_LABELS:
            return
        media_id = await self.upsert_media(guild, hit)
        await self.db.execute("DELETE FROM favorites WHERE user_id=? AND media_id=?", user_id, media_id, commit=False)
        await self.db.execute(
            """INSERT INTO favorites (user_id, slot, media_id) VALUES (?, ?, ?)
               ON CONFLICT(user_id, slot) DO UPDATE SET media_id=excluded.media_id""",
            user_id,
            slot,
            media_id,
        )

    async def clear_favorite(self, guild: discord.Guild | None, user_id: int, slot: int) -> None:
        await self.db.execute("DELETE FROM favorites WHERE user_id=? AND slot=?", user_id, slot)

    async def add_watchlist(self, guild: discord.Guild | None, user_id: int, hit: MediaHit) -> None:
        media_id = await self.upsert_media(guild, hit)
        await self.db.execute(
            """INSERT INTO watchlist (user_id, media_id, added_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id, media_id) DO UPDATE SET added_at=excluded.added_at""",
            user_id,
            media_id,
            int(time.time()),
        )

    async def remove_watchlist(self, guild: discord.Guild | None, user_id: int, media_id: int) -> None:
        await self.db.execute("DELETE FROM watchlist WHERE user_id=? AND media_id=?", user_id, media_id)

    async def is_on_watchlist(self, guild: discord.Guild | None, user_id: int, media_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 AS n FROM watchlist WHERE user_id=? AND media_id=?",
            user_id,
            media_id,
        )
        return row is not None

    async def load_watchlist(
        self, guild: discord.Guild | None, user_id: int
    ) -> list[tuple[MediaHit, Any]]:
        rows = await self.db.fetchall(
            f"""SELECT w.*, {MEDIA_COLUMNS}
                FROM watchlist w JOIN media m ON m.id = w.media_id
                WHERE w.user_id=?
                ORDER BY w.added_at DESC""",
            user_id,
        )
        return [(hit_from_row(row), row) for row in rows]

    # ------------------------------------------------------------------
    # Listes communes (propres au serveur)
    # ------------------------------------------------------------------

    def can_edit_shared_list(self, record: dict[str, Any], user_id: int, editor_ids: list[int]) -> bool:
        if int(record["owner_id"]) == user_id:
            return True
        mode = record.get("edit_mode") or "owner"
        if mode == "public":
            return True
        return mode == "members" and user_id in editor_ids

    async def writable_shared_lists(self, guild: discord.Guild, user_id: int) -> list[dict[str, Any]]:
        lists = await self.load_shared_lists(guild)
        writable: list[dict[str, Any]] = []
        for record in lists:
            editors: list[int] = []
            if record.get("edit_mode") == "members" and int(record["owner_id"]) != user_id:
                editors = await self.load_shared_list_editors(guild, record["id"])
            if self.can_edit_shared_list(record, user_id, editors):
                writable.append(record)
        return writable

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
            "allowed_types": format_list_types(parse_list_types(row_field(row, "allowed_types", ""))),
            "created_at": int(row["created_at"] or 0),
            "item_count": count,
        }

    async def load_shared_lists(self, guild: discord.Guild) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT l.*, COUNT(i.media_id) AS item_count
               FROM shared_lists l
               LEFT JOIN shared_list_items i ON i.list_id = l.id
               WHERE l.guild_id=?
               GROUP BY l.id
               ORDER BY l.created_at DESC""",
            guild.id,
        )
        return [self._shared_list_from_row(row) for row in rows]

    async def get_shared_list(self, guild: discord.Guild, list_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """SELECT l.*, COUNT(i.media_id) AS item_count
               FROM shared_lists l
               LEFT JOIN shared_list_items i ON i.list_id = l.id
               WHERE l.id=? AND l.guild_id=?
               GROUP BY l.id""",
            list_id,
            guild.id,
        )
        return self._shared_list_from_row(row) if row else None

    async def count_owned_lists(self, guild: discord.Guild, user_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM shared_lists WHERE guild_id=? AND owner_id=?",
            guild.id,
            user_id,
        )
        return int(row["n"]) if row else 0

    async def create_shared_list(
        self,
        guild: discord.Guild,
        user_id: int,
        title: str,
        description: str,
        allowed_types: str = "",
    ) -> dict[str, Any]:
        prefs = await self.get_user_prefs(guild, user_id)
        edit_mode = prefs.default_list_edit if prefs.default_list_edit in LIST_EDIT_MODES else "owner"
        await self.db.execute(
            """INSERT INTO shared_lists (guild_id, owner_id, title, description, edit_mode, allowed_types, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            guild.id,
            user_id,
            title.strip()[:LIST_TITLE_MAX],
            description.strip()[:LIST_DESC_MAX],
            edit_mode,
            format_list_types(parse_list_types(allowed_types)),
            int(time.time()),
        )
        row = await self.db.fetchone("SELECT last_insert_rowid() AS id")
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
        allowed_types: str | None = None,
    ) -> None:
        record = await self.get_shared_list(guild, list_id)
        if record is None:
            return
        await self.db.execute(
            "UPDATE shared_lists SET title=?, description=?, edit_mode=?, allowed_types=? WHERE id=?",
            (title.strip()[:LIST_TITLE_MAX] if title is not None else record["title"]),
            (description.strip()[:LIST_DESC_MAX] if description is not None else record["description"]),
            (edit_mode if edit_mode in LIST_EDIT_MODES else record["edit_mode"]),
            (
                format_list_types(parse_list_types(allowed_types))
                if allowed_types is not None
                else record.get("allowed_types") or ""
            ),
            list_id,
        )

    async def delete_shared_list(self, guild: discord.Guild, list_id: int) -> None:
        if await self.get_shared_list(guild, list_id) is None:
            return
        await self.db.execute("DELETE FROM shared_list_items WHERE list_id=?", list_id, commit=False)
        await self.db.execute("DELETE FROM shared_list_editors WHERE list_id=?", list_id, commit=False)
        await self.db.execute("DELETE FROM shared_lists WHERE id=?", list_id)

    async def load_shared_list_editors(self, guild: discord.Guild, list_id: int) -> list[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM shared_list_editors WHERE list_id=?",
            list_id,
        )
        return [int(row["user_id"]) for row in rows]

    async def set_shared_list_editors(
        self, guild: discord.Guild, list_id: int, user_ids: list[int]
    ) -> None:
        unique = list(dict.fromkeys(user_ids))[:25]
        await self.db.execute("DELETE FROM shared_list_editors WHERE list_id=?", list_id, commit=not unique)
        if unique:
            await self.db.executemany(
                "INSERT INTO shared_list_editors (list_id, user_id) VALUES (?, ?)",
                [(list_id, user_id) for user_id in unique],
            )

    async def load_shared_list_items(
        self, guild: discord.Guild, list_id: int
    ) -> list[tuple[MediaHit, Any]]:
        rows = await self.db.fetchall(
            f"""SELECT i.*, {MEDIA_COLUMNS}
                FROM shared_list_items i JOIN media m ON m.id = i.media_id
                WHERE i.list_id=?
                ORDER BY i.added_at DESC""",
            list_id,
        )
        return [(hit_from_row(row), row) for row in rows]

    async def add_shared_list_item(
        self, guild: discord.Guild, list_id: int, user_id: int, hit: MediaHit
    ) -> str | None:
        record = await self.get_shared_list(guild, list_id)
        if record is None:
            return "Cette liste n'existe plus."
        if not list_accepts_type(record.get("allowed_types"), hit.media_type):
            return f"Cette liste n'accepte que {list_types_label(record.get('allowed_types'))}."
        if record["item_count"] >= MAX_LIST_ITEMS:
            return f"Cette liste est pleine ({MAX_LIST_ITEMS} œuvres)."
        media_id = await self.upsert_media(guild, hit)
        existing = await self.db.fetchone(
            "SELECT 1 AS n FROM shared_list_items WHERE list_id=? AND media_id=?",
            list_id,
            media_id,
        )
        if existing:
            return "Cette œuvre est déjà dans la liste."
        await self.db.execute(
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
        await self.db.execute(
            "DELETE FROM shared_list_items WHERE list_id=? AND media_id=?",
            list_id,
            media_id,
        )

    # ------------------------------------------------------------------
    # Journal, catalogue du serveur et recommandations
    # ------------------------------------------------------------------

    async def load_journal(
        self, guild: discord.Guild | None, user_id: int, media_type: str = "all"
    ) -> list[tuple[MediaHit, Any]]:
        sql = f"""SELECT r.*, {MEDIA_COLUMNS}
                  FROM reviews r JOIN media m ON m.id = r.media_id
                  WHERE r.user_id=?"""
        args: list[Any] = [user_id]
        if media_type != "all":
            sql += " AND m.media_type=?"
            args.append(media_type)
        rows = await self.db.fetchall(sql + " ORDER BY r.updated_at DESC", *args)
        return [(hit_from_row(row), row) for row in rows]

    async def load_catalog(
        self,
        guild: discord.Guild,
        *,
        query: str | None = None,
        member_id: int | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> list[tuple[MediaHit, float, int, int]]:
        sql = f"""SELECT m.*, AVG(r.rating) AS avg_rating, COUNT(r.id) AS n,
                         MAX(r.updated_at) AS latest
                  FROM media m JOIN reviews r ON r.media_id = m.id
                  {ON_SERVER.format("r")}"""
        clauses: list[str] = []
        args: list[Any] = [guild.id]
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
        rows = await self.db.fetchall(sql, *args)
        items = [
            (hit_from_row(row), float(row["avg_rating"]), int(row["n"]), int(row["latest"] or 0))
            for row in rows
        ]
        if query:
            items = fuzzy.finder(
                query,
                items,
                key=lambda item: f"{item[0].title} {item[0].subtitle} {item[0].year or ''}",
            )
        return items

    async def also_liked(
        self,
        guild: discord.Guild,
        media_id: int | None,
        *,
        viewer_id: int | None = None,
    ) -> list[tuple[MediaHit, float, int]]:
        """Œuvres aussi notées haut par les membres du serveur qui ont aimé celle-ci."""
        if not media_id:
            return []
        likers = await self.db.fetchall(
            f"""SELECT r.user_id FROM reviews r {ON_SERVER.format("r")}
                WHERE r.media_id=? AND r.rating>=? AND r.user_id!=?""",
            guild.id,
            media_id,
            LIKE_RATING,
            viewer_id or 0,
        )
        ids = [int(row["user_id"]) for row in likers]
        if len(ids) < ALSO_LIKED_MIN:
            return []
        marks = ",".join("?" * len(ids))
        sql = f"""SELECT {MEDIA_COLUMNS},
                         AVG(r.rating) AS avg_rating, COUNT(DISTINCT r.user_id) AS people
                  FROM reviews r JOIN media m ON m.id = r.media_id
                  WHERE r.rating>=? AND r.media_id!=? AND r.user_id IN ({marks})"""
        args: list[Any] = [LIKE_RATING, media_id, *ids]
        if viewer_id is not None:
            sql += " AND r.media_id NOT IN (SELECT media_id FROM reviews WHERE user_id=?)"
            args.append(viewer_id)
        sql += """ GROUP BY m.id HAVING COUNT(DISTINCT r.user_id)>=?
                   ORDER BY people DESC, avg_rating DESC LIMIT ?"""
        args.extend([ALSO_LIKED_MIN, ALSO_LIKED_LIMIT])
        rows = await self.db.fetchall(sql, *args)
        return [(hit_from_row(row), float(row["avg_rating"]), int(row["people"])) for row in rows]

    async def also_liked_similar(
        self,
        guild: discord.Guild,
        media_id: int | None,
    ) -> list[tuple[MediaHit, float, int]]:
        """Œuvres notées pareil, par une large part de ceux qui ont noté les deux."""
        if not media_id:
            return []
        rows = await self.db.fetchall(
            f"""SELECT r1.rating AS left_rating, r2.rating AS right_rating,
                       m.id AS other_media, {MEDIA_COLUMNS}
                FROM reviews r1
                {ON_SERVER.format("r1")}
                JOIN reviews r2 ON r2.user_id = r1.user_id AND r2.media_id != r1.media_id
                JOIN media m ON m.id = r2.media_id
                WHERE r1.media_id=?""",
            guild.id,
            media_id,
        )
        grouped: dict[int, list[tuple[float, float]]] = {}
        sample: dict[int, Any] = {}
        for row in rows:
            other_id = int(row["other_media"])
            grouped.setdefault(other_id, []).append((float(row["left_rating"]), float(row["right_rating"])))
            sample[other_id] = row
        ranked: list[tuple[int, float, int, Any]] = []
        for other_id, pairs in grouped.items():
            if len(pairs) < ALSO_LIKED_MIN:
                continue
            similar = sum(
                1
                for left, right in pairs
                if abs(left - right) <= ALSO_PUBLIC_GAP and left >= LIKE_RATING and right >= LIKE_RATING
            )
            if similar * 100 < ALSO_PUBLIC_PERCENT * len(pairs):
                continue
            ranked.append((similar, similar / len(pairs), other_id, sample[other_id]))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [
            (hit_from_row(row), agreement_percent(grouped[other_id]), similar)
            for similar, _ratio, other_id, row in ranked[:ALSO_LIKED_LIMIT]
        ]

    async def public_fiche_line(
        self,
        guild: discord.Guild,
        hit: MediaHit,
        reviews: list[Any],
        media_id: int | None,
    ) -> str:
        social = self.social_line_for_reviews(guild, reviews, viewer_id=None)
        liked = await self.also_liked_similar(guild, media_id)
        options = [line for line in (social, also_liked_line(hit, liked)) if line]
        if not options:
            return ""
        return random.choice(options)

    async def load_for_you(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        media_type: str = "all",
    ) -> tuple[list[tuple[MediaHit, float, int]], str]:
        """Œuvres non notées, aimées par des membres du serveur proches du goût de user_id."""
        empty = "*Pas encore assez de goûts en commun pour recommander quelque chose.*"
        liked = await self.db.fetchone(
            "SELECT 1 AS n FROM reviews WHERE user_id=? AND rating>=? LIMIT 1",
            user_id,
            LIKE_RATING,
        )
        if liked is None:
            return [], "*Note quelques œuvres à 7 ou plus pour avoir des recommandations.*"
        peers = await self.db.fetchall(
            f"""SELECT r2.user_id AS other_id
                FROM reviews r1
                JOIN reviews r2 ON r1.media_id = r2.media_id AND r2.user_id != r1.user_id
                {ON_SERVER.format("r2")}
                WHERE r1.user_id=?
                GROUP BY r2.user_id
                HAVING COUNT(*)>=? AND AVG(ABS(r1.rating - r2.rating))<=?""",
            guild.id,
            user_id,
            MIN_AFFINITY_OVERLAP,
            FOR_YOU_MAX_GAP,
        )
        peer_ids = [int(row["other_id"]) for row in peers]
        if len(peer_ids) < ALSO_LIKED_MIN:
            return [], empty
        marks = ",".join("?" * len(peer_ids))
        sql = f"""SELECT {MEDIA_COLUMNS},
                         AVG(r.rating) AS avg_rating, COUNT(DISTINCT r.user_id) AS people
                  FROM reviews r JOIN media m ON m.id = r.media_id
                  WHERE r.rating>=? AND r.user_id IN ({marks})
                    AND r.media_id NOT IN (SELECT media_id FROM reviews WHERE user_id=?)"""
        args: list[Any] = [LIKE_RATING, *peer_ids, user_id]
        if media_type != "all":
            sql += " AND m.media_type=?"
            args.append(media_type)
        sql += """ GROUP BY m.id HAVING COUNT(DISTINCT r.user_id)>=?
                   ORDER BY people DESC, avg_rating DESC LIMIT ?"""
        args.extend([ALSO_LIKED_MIN, FOR_YOU_LIMIT])
        rows = await self.db.fetchall(sql, *args)
        items = [(hit_from_row(row), float(row["avg_rating"]), int(row["people"])) for row in rows]
        if not items:
            return [], empty
        return items, ""

    # ------------------------------------------------------------------
    # Persistance médias / critiques
    # ------------------------------------------------------------------

    async def upsert_media(self, guild: discord.Guild | None, hit: MediaHit) -> int:
        await self.db.execute(
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
        media_id = await self.lookup_media_id(guild, hit)
        assert media_id is not None
        return media_id

    async def lookup_media_id(self, guild: discord.Guild | None, hit: MediaHit) -> int | None:
        row = await self.db.fetchone(
            "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
            hit.source,
            hit.source_id,
            hit.media_type,
        )
        return int(row["id"]) if row else None

    async def rated_on_server(self, guild: discord.Guild, identity: tuple[str, str, str]) -> bool:
        """L'œuvre (source, source_id, media_type) a au moins une note d'un membre du serveur."""
        row = await self.db.fetchone(
            f"""SELECT 1 AS n FROM media m
                JOIN reviews r ON r.media_id = m.id
                {ON_SERVER.format("r")}
                WHERE m.source=? AND m.source_id=? AND m.media_type=?
                LIMIT 1""",
            guild.id,
            *identity,
        )
        return row is not None

    async def media_stats(self, guild: discord.Guild, media_id: int | None) -> tuple[float | None, int]:
        """Moyenne et nombre de notes des membres du serveur."""
        if not media_id:
            return None, 0
        row = await self.db.fetchone(
            f"""SELECT AVG(r.rating) AS avg_rating, COUNT(*) AS n
                FROM reviews r {ON_SERVER.format("r")}
                WHERE r.media_id=?""",
            guild.id,
            media_id,
        )
        if not row or not row["n"]:
            return None, 0
        return float(row["avg_rating"]), int(row["n"])

    async def list_reviews(self, guild: discord.Guild, media_id: int) -> list[Any]:
        """Critiques de l'œuvre par les membres du serveur, les plus récentes d'abord."""
        return await self.db.fetchall(
            f"""SELECT r.* FROM reviews r {ON_SERVER.format("r")}
                WHERE r.media_id=? ORDER BY r.updated_at DESC""",
            guild.id,
            media_id,
        )

    async def get_review(self, guild: discord.Guild | None, user_id: int, media_id: int) -> dict | None:
        row = await self.db.fetchone(
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
        max_len = await self.get_comment_max(guild)
        comment = comment.strip()[:max_len]
        media_id = await self.upsert_media(guild, hit)
        await self.remember_member(guild.id, user.id)
        existing = await self.get_review(guild, user.id, media_id)
        # Pionnier : premier du serveur à noter l'œuvre.
        _avg, count = await self.media_stats(guild, media_id)
        pioneer = count == 0
        new_comment = bool(comment) and (not existing or not existing["comment"])
        now = int(time.time())
        created = existing is None
        await self.db.execute(
            """INSERT INTO reviews (user_id, media_id, rating, comment, experienced_at, spoiler, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, media_id) DO UPDATE SET
                 rating=excluded.rating,
                 comment=excluded.comment,
                 experienced_at=excluded.experienced_at,
                 spoiler=excluded.spoiler,
                 updated_at=excluded.updated_at""",
            user.id,
            media_id,
            rating,
            comment,
            experienced_at,
            int(spoiler),
            now,
            now,
            commit=False,
        )
        await self.remove_watchlist(guild, user.id, media_id)
        award = await self.grant_review_xp(
            guild,
            user.id,
            created=created,
            pioneer=pioneer,
            new_comment=new_comment,
        )
        if created:
            asyncio.create_task(
                self.notify_bookmark_watchers(guild, user, hit, rating),
                name="crit-bookmark-notify",
            )
        return created, award

    async def notify_bookmark_watchers(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
    ) -> None:
        """MP les membres du serveur qui ont cette œuvre en signet."""
        try:
            media_id = await self.lookup_media_id(guild, hit)
            if not media_id:
                return
            rows = await self.db.fetchall(
                f"""SELECT w.user_id FROM watchlist w {ON_SERVER.format("w")}
                    WHERE w.media_id=? AND w.user_id!=?""",
                guild.id,
                media_id,
                user.id,
            )
            skin = await self.star_skin_for(guild, user.id)
            year = f" ({hit.year})" if hit.year else ""
            text = (
                f"**Signet ·** {user.display_name} a noté **{hit.title}**{year} "
                f"sur **{guild.name}**.\n"
                f"{format_stars(rating, skin)}  **{format_score(rating)}**"
            )
            for row in rows:
                user_id = int(row["user_id"])
                prefs = await self.get_user_prefs(guild, user_id)
                if not prefs.bookmark_notify:
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        continue
                try:
                    await member.send(text, allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    logger.info("MP signet impossible pour %s", user_id)
        except Exception:
            logger.exception("Notification de signet impossible")

    async def delete_review(self, guild: discord.Guild | None, user_id: int, hit: MediaHit) -> None:
        media_id = await self.lookup_media_id(guild, hit)
        if media_id:
            await self.db.execute(
                "DELETE FROM reviews WHERE user_id=? AND media_id=?",
                user_id,
                media_id,
            )
