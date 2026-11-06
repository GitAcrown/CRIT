"""Base commune du carnet : schéma, et fusion des anciennes bases par serveur.

Avant, chaque serveur avait sa base (`data/guild_<id>.db`). Tout vit désormais dans
`data/carnet.db` : notes, XP, signets et préférences suivent le membre sur tous les
serveurs ; réglages, listes communes et streams planifiés restent rattachés au serveur.
La table `members` dit quels membres du carnet sont présents sur chaque serveur.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from pathlib import Path

from .progress import format_unlocked_skins, level_for_xp, parse_unlocked_skins

logger = logging.getLogger("CRIT.Reviews")

DB_NAME = "carnet"

# Réglages par serveur repris des anciennes bases (les drapeaux de migration sont ignorés).
GUILD_SETTING_KEYS = ("AnnounceChannelID", "AnnounceChannelsByType", "MaxCommentLength")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (guild_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS members (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )""",
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
    )""",
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
    )""",
    """CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        xp INTEGER NOT NULL DEFAULT 0,
        daily_xp INTEGER NOT NULL DEFAULT 0,
        daily_date TEXT NOT NULL DEFAULT '',
        daily_awards INTEGER NOT NULL DEFAULT 0,
        last_level INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        media_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, slot)
    )""",
    """CREATE TABLE IF NOT EXISTS watchlist (
        user_id INTEGER NOT NULL,
        media_id INTEGER NOT NULL,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, media_id)
    )""",
    """CREATE TABLE IF NOT EXISTS preferences (
        user_id INTEGER PRIMARY KEY,
        default_date TEXT NOT NULL DEFAULT 'empty',
        default_list_edit TEXT NOT NULL DEFAULT 'owner',
        default_search_type TEXT NOT NULL DEFAULT 'all',
        announce_notes INTEGER NOT NULL DEFAULT 1,
        stream_remind INTEGER NOT NULL DEFAULT 0,
        stream_voice_status INTEGER NOT NULL DEFAULT 1,
        star_skin TEXT NOT NULL DEFAULT 'classique',
        unlocked_skins TEXT NOT NULL DEFAULT '',
        bookmark_notify INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS shared_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        edit_mode TEXT NOT NULL DEFAULT 'owner',
        allowed_types TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS shared_list_editors (
        list_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (list_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS shared_list_items (
        list_id INTEGER NOT NULL,
        media_id INTEGER NOT NULL,
        added_by INTEGER NOT NULL,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (list_id, media_id)
    )""",
    """CREATE TABLE IF NOT EXISTS stream_links (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        hit_json TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        post_channel_id INTEGER NOT NULL DEFAULT 0,
        voice_status INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS stream_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        hit_json TEXT NOT NULL,
        starts_at INTEGER NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stream_plan_interest (
        plan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (plan_id, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_members_user ON members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_media ON reviews(media_id)",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_media ON watchlist(media_id)",
    "CREATE INDEX IF NOT EXISTS idx_shared_lists_guild ON shared_lists(guild_id)",
    "CREATE INDEX IF NOT EXISTS idx_stream_plans_due ON stream_plans(notified, starts_at)",
)


def prepare_database(data_dir: Path) -> Path:
    """Crée la base commune et y fusionne les anciennes bases par serveur.

    Bloquant (sqlite3) : à lancer dans un thread, avant la première connexion aiosqlite.
    Chaque ancienne base fusionnée est rangée dans `data/legacy/` (jamais supprimée).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{DB_NAME}.db"
    legacy = sorted(
        data_dir.glob("guild_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # la base la plus récente d'abord : ses préférences priment
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for statement in SCHEMA:
            conn.execute(statement)
        conn.commit()
        for legacy_path in legacy:
            try:
                guild_id = int(legacy_path.stem.removeprefix("guild_"))
            except ValueError:
                continue
            key = f"legacy:{guild_id}"
            if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() is None:
                stats = _merge_legacy(conn, legacy_path, guild_id)
                logger.info("Base du serveur %s fusionnée : %s", guild_id, stats)
            _archive(legacy_path, data_dir / "legacy")
    finally:
        conn.close()
    return path


def _archive(path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = path.with_name(path.name + suffix)
        if src.exists():
            shutil.move(str(src), str(folder / src.name))


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:  # table absente d'une vieille base
        return []


def _field(row: sqlite3.Row, key: str, default=None):
    if key not in row.keys():
        return default
    value = row[key]
    return default if value is None else value


def _merge_legacy(dst: sqlite3.Connection, src_path: Path, guild_id: int) -> dict[str, int]:
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    stats = {"notes": 0, "doublons": 0, "listes": 0, "membres": 0}
    try:
        with dst:  # une transaction par serveur : tout ou rien
            settings = {row["key"]: row["value"] for row in _rows(src, "settings")}
            scale = 1.0
            if settings.get("RatingsOnTen") != "1":
                try:
                    top = src.execute("SELECT MAX(rating) FROM reviews").fetchone()
                except sqlite3.OperationalError:
                    top = None
                if top and top[0] is not None and 0 < float(top[0]) <= 5:
                    scale = 2.0  # très vieilles notes sur 5
            fix_remind = settings.get("StreamRemindMode") != "1"
            for key in GUILD_SETTING_KEYS:
                value = settings.get(key)
                if value not in (None, "", "0", "{}"):
                    dst.execute(
                        "INSERT OR REPLACE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                        (guild_id, key, str(value)),
                    )

            media_map: dict[int, int] = {}
            for row in _rows(src, "media"):
                dst.execute(
                    """INSERT INTO media (source, source_id, media_type, title, subtitle, year, poster_url, url, overview, genres, extra_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, source_id, media_type) DO NOTHING""",
                    (
                        row["source"], row["source_id"], row["media_type"], row["title"],
                        _field(row, "subtitle", ""), _field(row, "year"), _field(row, "poster_url"),
                        _field(row, "url"), _field(row, "overview", ""), _field(row, "genres", ""),
                        _field(row, "extra_json", "{}"),
                    ),
                )
                found = dst.execute(
                    "SELECT id FROM media WHERE source=? AND source_id=? AND media_type=?",
                    (row["source"], row["source_id"], row["media_type"]),
                ).fetchone()
                media_map[int(row["id"])] = int(found["id"])

            users: set[int] = set()
            reviewers: dict[int, tuple[int, int]] = {}
            for row in _rows(src, "reviews"):
                media_id = media_map.get(int(row["media_id"]))
                if media_id is None:
                    continue
                user_id = int(row["user_id"])
                users.add(user_id)
                count, comments = reviewers.get(user_id, (0, 0))
                reviewers[user_id] = (count + 1, comments + (1 if _field(row, "comment", "") else 0))
                values = (
                    float(row["rating"]) * scale,
                    _field(row, "comment", ""),
                    _field(row, "experienced_at", ""),
                    int(_field(row, "spoiler", 0)),
                    int(row["created_at"]),
                    int(row["updated_at"]),
                )
                existing = dst.execute(
                    "SELECT id, created_at, updated_at FROM reviews WHERE user_id=? AND media_id=?",
                    (user_id, media_id),
                ).fetchone()
                if existing is None:
                    dst.execute(
                        """INSERT INTO reviews (user_id, media_id, rating, comment, experienced_at, spoiler, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, media_id, *values),
                    )
                    stats["notes"] += 1
                    continue
                # Même œuvre notée sur deux serveurs : on garde la note la plus récente.
                stats["doublons"] += 1
                created = min(int(existing["created_at"]), values[4])
                if values[5] > int(existing["updated_at"]):
                    dst.execute(
                        """UPDATE reviews SET rating=?, comment=?, experienced_at=?, spoiler=?, created_at=?, updated_at=?
                           WHERE id=?""",
                        (*values[:4], created, values[5], existing["id"]),
                    )
                else:
                    dst.execute("UPDATE reviews SET created_at=? WHERE id=?", (created, existing["id"]))

            profiles = {int(row["user_id"]): row for row in _rows(src, "profiles")}
            backfill = settings.get("BackfilledXP") != "1"
            for user_id in set(profiles) | (set(reviewers) if backfill else set()):
                row = profiles.get(user_id)
                users.add(user_id)
                if row is None:
                    count, comments = reviewers[user_id]
                    xp, daily = count * 10 + comments * 10, (0, "", 0)
                else:
                    xp = int(row["xp"])
                    daily = (int(row["daily_xp"]), str(row["daily_date"] or ""), int(row["daily_awards"]))
                existing = dst.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
                if existing is not None:
                    xp += int(existing["xp"])  # l'XP des serveurs s'additionne
                    ours = (int(existing["daily_xp"]), str(existing["daily_date"] or ""), int(existing["daily_awards"]))
                    if ours[1] == daily[1]:
                        daily = (ours[0] + daily[0], daily[1], ours[2] + daily[2])
                    elif ours[1] > daily[1]:
                        daily = ours
                dst.execute(
                    """INSERT OR REPLACE INTO profiles (user_id, xp, daily_xp, daily_date, daily_awards, last_level)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, xp, daily[0], daily[1], daily[2], level_for_xp(xp)),
                )

            for row in _rows(src, "favorites"):
                media_id = media_map.get(int(row["media_id"]))
                user_id = int(row["user_id"])
                users.add(user_id)
                taken = dst.execute(
                    "SELECT 1 FROM favorites WHERE user_id=? AND (slot=? OR media_id=?)",
                    (user_id, int(row["slot"]), media_id),
                ).fetchone()
                if media_id is not None and taken is None:
                    dst.execute(
                        "INSERT INTO favorites (user_id, slot, media_id) VALUES (?, ?, ?)",
                        (user_id, int(row["slot"]), media_id),
                    )

            for row in _rows(src, "watchlist"):
                media_id = media_map.get(int(row["media_id"]))
                if media_id is None:
                    continue
                users.add(int(row["user_id"]))
                dst.execute(
                    "INSERT OR IGNORE INTO watchlist (user_id, media_id, added_at) VALUES (?, ?, ?)",
                    (int(row["user_id"]), media_id, int(row["added_at"])),
                )

            for row in _rows(src, "preferences"):
                user_id = int(row["user_id"])
                users.add(user_id)
                skins = parse_unlocked_skins(_field(row, "unlocked_skins", ""))
                existing = dst.execute("SELECT unlocked_skins FROM preferences WHERE user_id=?", (user_id,)).fetchone()
                if existing is not None:
                    merged = parse_unlocked_skins(existing["unlocked_skins"]) | skins
                    dst.execute(
                        "UPDATE preferences SET unlocked_skins=? WHERE user_id=?",
                        (format_unlocked_skins(merged), user_id),
                    )
                    continue
                remind = int(_field(row, "stream_remind", 0))
                if fix_remind and remind == 1:
                    remind = 2
                dst.execute(
                    """INSERT INTO preferences
                       (user_id, default_date, default_list_edit, default_search_type, announce_notes, stream_remind,
                        stream_voice_status, star_skin, unlocked_skins, bookmark_notify)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        _field(row, "default_date", "empty"),
                        _field(row, "default_list_edit", "owner"),
                        _field(row, "default_search_type", "all"),
                        int(_field(row, "announce_notes", 1)),
                        remind,
                        int(_field(row, "stream_voice_status", 1)),
                        _field(row, "star_skin", "classique"),
                        format_unlocked_skins(skins),
                        int(_field(row, "bookmark_notify", 1)),
                    ),
                )

            list_map: dict[int, int] = {}
            for row in _rows(src, "shared_lists"):
                cursor = dst.execute(
                    """INSERT INTO shared_lists (guild_id, owner_id, title, description, edit_mode, allowed_types, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id, int(row["owner_id"]), row["title"], _field(row, "description", ""),
                        _field(row, "edit_mode", "owner"), _field(row, "allowed_types", ""), int(row["created_at"]),
                    ),
                )
                list_map[int(row["id"])] = int(cursor.lastrowid)
                users.add(int(row["owner_id"]))
                stats["listes"] += 1
            for row in _rows(src, "shared_list_editors"):
                list_id = list_map.get(int(row["list_id"]))
                if list_id is not None:
                    dst.execute(
                        "INSERT OR IGNORE INTO shared_list_editors (list_id, user_id) VALUES (?, ?)",
                        (list_id, int(row["user_id"])),
                    )
            for row in _rows(src, "shared_list_items"):
                list_id = list_map.get(int(row["list_id"]))
                media_id = media_map.get(int(row["media_id"]))
                if list_id is not None and media_id is not None:
                    dst.execute(
                        "INSERT OR IGNORE INTO shared_list_items (list_id, media_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                        (list_id, media_id, int(row["added_by"]), int(row["added_at"])),
                    )

            for row in _rows(src, "stream_links"):
                dst.execute(
                    """INSERT OR REPLACE INTO stream_links
                       (guild_id, user_id, channel_id, hit_json, source, created_at, post_channel_id, voice_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id, int(row["user_id"]), int(row["channel_id"]), row["hit_json"], row["source"],
                        int(row["created_at"]), int(_field(row, "post_channel_id", 0)), int(_field(row, "voice_status", 0)),
                    ),
                )
            plan_map: dict[int, int] = {}
            now = int(time.time())
            for row in _rows(src, "stream_plans"):
                if int(_field(row, "notified", 0)) or int(row["starts_at"]) <= now:
                    continue
                cursor = dst.execute(
                    """INSERT INTO stream_plans (guild_id, user_id, hit_json, starts_at, notified, created_at)
                       VALUES (?, ?, ?, ?, 0, ?)""",
                    (guild_id, int(row["user_id"]), row["hit_json"], int(row["starts_at"]), int(row["created_at"])),
                )
                plan_map[int(row["id"])] = int(cursor.lastrowid)
            for row in _rows(src, "stream_plan_interest"):
                plan_id = plan_map.get(int(row["plan_id"]))
                if plan_id is not None:
                    dst.execute(
                        "INSERT OR IGNORE INTO stream_plan_interest (plan_id, user_id) VALUES (?, ?)",
                        (plan_id, int(row["user_id"])),
                    )

            # Un signet sur une œuvre déjà notée (sur un autre serveur) n'a plus lieu d'être.
            dst.execute(
                """DELETE FROM watchlist WHERE EXISTS (
                       SELECT 1 FROM reviews r WHERE r.user_id = watchlist.user_id AND r.media_id = watchlist.media_id
                   )"""
            )
            dst.executemany(
                "INSERT OR IGNORE INTO members (guild_id, user_id) VALUES (?, ?)",
                [(guild_id, user_id) for user_id in users],
            )
            stats["membres"] = len(users)
            dst.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (f"legacy:{guild_id}", str(int(time.time()))),
            )
    finally:
        src.close()
    return stats
