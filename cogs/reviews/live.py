"""Lives : œuvre liée au stream, rappels, statut du salon vocal et streams planifiés."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import discord

from .constants import (
    logger,
    NO_PINGS,
    STREAM_END_GRACE,
    STREAM_LINK_MAX_AGE,
    STREAM_PLAN_HORIZON,
    STREAM_PLAN_MAX,
    STREAM_START_GRACE,
)
from .dyn import hit_from_dict, hit_to_dict
from .formatting import hit_identity, mention_of
from .prefs import row_field, STREAM_REMIND_BOTH, STREAM_REMIND_OFF
from .providers import MediaCatalog, MediaHit
from .streaming import (
    member_stream_source,
    member_voice_channel_id,
    resolve_post_channel,
)
from .ui.base import bind_view_message
from .ui.fiche import post_published_fiche, stream_status_text
from .ui.streams import StreamPlanDueView, StreamRemindView

if TYPE_CHECKING:
    from discord.ext import commands

    from utils import dataio


class LiveMixin:
    """Suivi des lives des membres et des streams planifiés (rattachés au serveur)."""

    bot: commands.Bot
    db: dataio.ModelDataManager
    catalog: MediaCatalog | None
    _stream_end_tasks: dict[tuple[int, int], asyncio.Task[None]]
    _stream_remind_tasks: dict[tuple[int, int], asyncio.Task[None]]
    _stream_session_reminded: set[tuple[int, int]]

    def _cancel_stream_end(self, guild_id: int, user_id: int) -> None:
        task = self._stream_end_tasks.pop((guild_id, user_id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _cancel_stream_remind(self, guild_id: int, user_id: int) -> None:
        task = self._stream_remind_tasks.pop((guild_id, user_id), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _on_stream_started(self, member: discord.Member, _source: str) -> None:
        guild = member.guild
        self._cancel_stream_end(guild.id, member.id)
        channel_id = member_voice_channel_id(member)
        if channel_id:
            await self._update_stream_channel(guild, member.id, channel_id)
        await self._schedule_stream_remind(guild, member.id)

    async def _on_stream_stopped(self, member: discord.Member, ended: str) -> None:
        guild = member.guild
        self._cancel_stream_remind(guild.id, member.id)
        await self._schedule_stream_end(guild, member.id, ended)

    async def _schedule_stream_remind(self, guild: discord.Guild, user_id: int) -> None:
        key = (guild.id, user_id)
        if key in self._stream_session_reminded:
            return
        if await self.get_stream_link(guild, user_id):
            self._stream_session_reminded.add(key)
            return
        prefs = await self.get_user_prefs(guild, user_id)
        if prefs.stream_remind != STREAM_REMIND_BOTH:
            return
        self._cancel_stream_remind(guild.id, user_id)
        task = asyncio.create_task(self._confirm_stream_remind(guild.id, user_id))
        self._stream_remind_tasks[key] = task

    async def _confirm_stream_remind(self, guild_id: int, user_id: int) -> None:
        try:
            await asyncio.sleep(STREAM_START_GRACE)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            member = guild.get_member(user_id)
            if member is None or member_stream_source(member) is None:
                return
            if await self.get_stream_link(guild, user_id):
                self._stream_session_reminded.add((guild_id, user_id))
                return
            prefs = await self.get_user_prefs(guild, user_id)
            if prefs.stream_remind != STREAM_REMIND_BOTH:
                return
            if await self._send_stream_remind(guild, member):
                self._stream_session_reminded.add((guild_id, user_id))
        except asyncio.CancelledError:
            return
        finally:
            key = (guild_id, user_id)
            if self._stream_remind_tasks.get(key) is asyncio.current_task():
                self._stream_remind_tasks.pop(key, None)

    async def _send_stream_remind(self, guild: discord.Guild, member: discord.Member) -> bool:
        channel_id = member_voice_channel_id(member)
        view = StreamRemindView(self, guild, viewer_id=member.id, channel_id=channel_id)
        try:
            message = await member.send(view=view, allowed_mentions=NO_PINGS)
            bind_view_message(view, message)
            return True
        except discord.HTTPException:
            pass
        dest = resolve_post_channel(guild, channel_id)
        if dest is None and channel_id:
            try:
                fetched = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                fetched = None
            if fetched is not None and hasattr(fetched, "send"):
                dest = fetched
        if dest is None:
            return False
        view = StreamRemindView(self, guild, viewer_id=member.id, channel_id=channel_id)
        try:
            message = await dest.send(view=view, allowed_mentions=NO_PINGS, delete_after=300)
            bind_view_message(view, message)
            return True
        except discord.HTTPException as exc:
            logger.info("Rappel stream impossible : %s", exc)
            return False

    async def get_stream_link(self, guild: discord.Guild, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """SELECT channel_id, hit_json, source, created_at, post_channel_id, voice_status
               FROM stream_links WHERE guild_id=? AND user_id=?""",
            guild.id,
            user_id,
        )
        if row is None:
            return None
        try:
            raw = json.loads(row["hit_json"] or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        return {
            "channel_id": int(row["channel_id"]),
            "hit": raw,
            "source": str(row["source"] or ""),
            "created_at": int(row["created_at"] or 0),
            "post_channel_id": int(row_field(row, "post_channel_id", 0) or 0),
            "voice_status": int(row_field(row, "voice_status", 0) or 0),
        }

    async def list_stream_links(self, guild: discord.Guild) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT user_id, channel_id, hit_json, source, created_at, post_channel_id, voice_status
               FROM stream_links WHERE guild_id=? ORDER BY created_at ASC""",
            guild.id,
        )
        links: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw = json.loads(row["hit_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            links.append(
                {
                    "user_id": int(row["user_id"]),
                    "channel_id": int(row["channel_id"]),
                    "hit": raw,
                    "source": str(row["source"] or ""),
                    "created_at": int(row["created_at"] or 0),
                    "post_channel_id": int(row_field(row, "post_channel_id", 0) or 0),
                    "voice_status": int(row_field(row, "voice_status", 0) or 0),
                }
            )
        return links

    async def list_active_stream_links(self, guild: discord.Guild) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for link in await self.list_stream_links(guild):
            member = guild.get_member(link["user_id"])
            if member is None or member_stream_source(member) is None:
                continue
            item = dict(link)
            item["hit"] = hit_from_dict(link["hit"])
            live_channel = member_voice_channel_id(member)
            if live_channel:
                item["channel_id"] = live_channel
            active.append(item)
        return active

    async def stream_channels_for_hit(self, guild: discord.Guild, hit: MediaHit) -> list[int]:
        key = hit_identity(hit)
        seen: set[int] = set()
        channels: list[int] = []
        for link in await self.list_active_stream_links(guild):
            if hit_identity(link["hit"]) != key:
                continue
            channel_id = int(link.get("channel_id") or 0)
            if not channel_id or channel_id in seen:
                continue
            seen.add(channel_id)
            channels.append(channel_id)
        return channels

    async def count_upcoming_plans(self, guild: discord.Guild, user_id: int) -> int:
        row = await self.db.fetchone(
            """SELECT COUNT(*) AS n FROM stream_plans
               WHERE guild_id=? AND user_id=? AND notified=0 AND starts_at>?""",
            guild.id,
            user_id,
            int(time.time()),
        )
        return int(row["n"] or 0) if row else 0

    async def list_upcoming_plans(self, guild: discord.Guild, viewer_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT p.id, p.user_id, p.hit_json, p.starts_at,
                      (SELECT COUNT(*) FROM stream_plan_interest i WHERE i.plan_id = p.id) AS interest,
                      EXISTS(
                          SELECT 1 FROM stream_plan_interest i
                          WHERE i.plan_id = p.id AND i.user_id = ?
                      ) AS mine
               FROM stream_plans p
               WHERE p.guild_id = ? AND p.notified = 0 AND p.starts_at > ?
               ORDER BY p.starts_at ASC""",
            viewer_id,
            guild.id,
            int(time.time()),
        )
        plans: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw = json.loads(row["hit_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            plans.append({
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "hit": hit_from_dict(raw),
                "starts_at": int(row["starts_at"] or 0),
                "interest": int(row["interest"] or 0),
                "mine": bool(int(row["mine"] or 0)),
            })
        return plans

    async def plans_for_hit(self, guild: discord.Guild, hit: MediaHit) -> list[dict[str, Any]]:
        key = hit_identity(hit)
        return [
            plan for plan in await self.list_upcoming_plans(guild, 0)
            if hit_identity(plan["hit"]) == key
        ]

    async def create_stream_plan(
        self,
        guild: discord.Guild,
        user_id: int,
        hit: MediaHit,
        starts_at: int,
    ) -> str | None:
        now = int(time.time())
        if starts_at <= now:
            return "Cette date est déjà passée."
        if starts_at > now + STREAM_PLAN_HORIZON:
            return "Tu peux planifier jusqu'à 90 jours à l'avance."
        if await self.count_upcoming_plans(guild, user_id) >= STREAM_PLAN_MAX:
            return "Tu as déjà 3 streams planifiés. Annule-en un pour en ajouter."
        upcoming = await self.list_upcoming_plans(guild, user_id)
        if any(hit_identity(plan["hit"]) == hit_identity(hit) and int(plan["user_id"]) == user_id for plan in upcoming):
            return "Tu as déjà planifié cette œuvre."
        if self.catalog is not None:
            try:
                hit = await self.catalog.enrich(hit)
            except Exception:
                logger.exception("Enrichissement du stream planifié impossible")
        await self.db.execute(
            """INSERT INTO stream_plans (guild_id, user_id, hit_json, starts_at, notified, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            guild.id,
            user_id,
            json.dumps(hit_to_dict(hit), ensure_ascii=False),
            int(starts_at),
            now,
        )
        return None

    async def cancel_stream_plan(self, guild: discord.Guild, plan_id: int, user_id: int) -> None:
        row = await self.db.fetchone(
            "SELECT 1 AS ok FROM stream_plans WHERE id=? AND guild_id=? AND user_id=? AND notified=0",
            plan_id,
            guild.id,
            user_id,
        )
        if row is None:
            return
        await self.db.execute("DELETE FROM stream_plan_interest WHERE plan_id=?", plan_id, commit=False)
        await self.db.execute("DELETE FROM stream_plans WHERE id=?", plan_id)

    async def toggle_plan_interest(
        self,
        guild: discord.Guild,
        plan_id: int,
        user_id: int,
    ) -> bool | None:
        row = await self.db.fetchone(
            "SELECT user_id, starts_at, notified FROM stream_plans WHERE id=? AND guild_id=?",
            plan_id,
            guild.id,
        )
        now = int(time.time())
        if row is None or int(row["notified"] or 0) or int(row["starts_at"] or 0) <= now:
            return None
        if int(row["user_id"]) == user_id:
            return None
        existing = await self.db.fetchone(
            "SELECT 1 AS ok FROM stream_plan_interest WHERE plan_id=? AND user_id=?",
            plan_id,
            user_id,
        )
        if existing is not None:
            await self.db.execute(
                "DELETE FROM stream_plan_interest WHERE plan_id=? AND user_id=?",
                plan_id,
                user_id,
            )
            return False
        await self.db.execute(
            "INSERT OR IGNORE INTO stream_plan_interest (plan_id, user_id) VALUES (?, ?)",
            plan_id,
            user_id,
        )
        return True

    async def _sweep_stream_plans(self) -> None:
        rows = await self.db.fetchall(
            """SELECT id, guild_id, user_id, hit_json FROM stream_plans
               WHERE notified=0 AND starts_at<=?""",
            int(time.time()),
        )
        for row in rows:
            plan_id = int(row["id"])
            guild = self.bot.get_guild(int(row["guild_id"]))
            if guild is not None:
                try:
                    await self._deliver_plan_notice(guild, int(row["user_id"]), str(row["hit_json"] or ""))
                except Exception:
                    logger.exception("notif stream planifié %s", plan_id)
            await self.db.execute("DELETE FROM stream_plan_interest WHERE plan_id=?", plan_id, commit=False)
            await self.db.execute("UPDATE stream_plans SET notified=1 WHERE id=?", plan_id)

    async def _deliver_plan_notice(self, guild: discord.Guild, user_id: int, hit_json: str) -> None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return
        if member_stream_source(member) is not None:
            return
        if (await self.get_user_prefs(guild, user_id)).stream_remind == STREAM_REMIND_OFF:
            return
        try:
            raw = json.loads(hit_json or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(raw, dict):
            return
        view = StreamPlanDueView(guild, hit_from_dict(raw))
        try:
            message = await member.send(view=view, allowed_mentions=NO_PINGS)
            bind_view_message(view, message)
        except discord.HTTPException as exc:
            logger.info("Notif stream planifié impossible (%s) : %s", user_id, exc)

    async def _voice_channel_for_status(
        self,
        guild: discord.Guild,
        channel_id: int | None,
    ) -> discord.VoiceChannel | discord.StageChannel | None:
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        try:
            fetched = await self.bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, (discord.VoiceChannel, discord.StageChannel)) else None

    async def _set_voice_channel_status(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        hit: MediaHit | None,
    ) -> bool:
        channel = await self._voice_channel_for_status(guild, channel_id)
        if channel is None:
            return False
        text = stream_status_text(hit) if hit is not None else None
        try:
            await channel.edit(status=text)
            return True
        except TypeError:
            logger.info("Statut vocal non supporté")
            return False
        except discord.HTTPException as exc:
            logger.info("Statut vocal impossible (%s) : %s", channel_id, exc)
            return False

    async def _refresh_voice_channel_status(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        *,
        we_owned: bool,
    ) -> None:
        if not channel_id or not we_owned:
            return
        others = [
            link
            for link in await self.list_active_stream_links(guild)
            if int(link.get("channel_id") or 0) == int(channel_id)
            and int(link.get("voice_status") or 0)
        ]
        if others:
            await self._set_voice_channel_status(guild, channel_id, others[0]["hit"])
            return
        await self._set_voice_channel_status(guild, channel_id, None)

    async def set_stream_link(
        self,
        guild: discord.Guild,
        user_id: int,
        hit: MediaHit,
        *,
        channel_id: int,
        source: str,
        post_channel_id: int = 0,
    ) -> None:
        old = await self.get_stream_link(guild, user_id)
        self._cancel_stream_end(guild.id, user_id)
        self._cancel_stream_remind(guild.id, user_id)
        self._stream_session_reminded.add((guild.id, user_id))
        await self.db.execute(
            """INSERT OR REPLACE INTO stream_links
               (guild_id, user_id, channel_id, hit_json, source, created_at, post_channel_id, voice_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            guild.id,
            user_id,
            channel_id,
            json.dumps(hit_to_dict(hit), ensure_ascii=False),
            source,
            int(time.time()),
            int(post_channel_id or 0),
            0,
        )
        prefs = await self.get_user_prefs(guild, user_id)
        owned = False
        old_channel = int(old.get("channel_id") or 0) if old else 0
        old_owned = bool(old and int(old.get("voice_status") or 0))
        if old_owned and old_channel and old_channel != channel_id:
            await self._refresh_voice_channel_status(guild, old_channel, we_owned=True)
        if prefs.stream_voice_status:
            owned = await self._set_voice_channel_status(guild, channel_id, hit)
        elif old_owned and old_channel == channel_id:
            await self._refresh_voice_channel_status(guild, channel_id, we_owned=True)
        if owned:
            await self.db.execute(
                "UPDATE stream_links SET voice_status=1 WHERE guild_id=? AND user_id=?",
                guild.id,
                user_id,
            )

    async def _update_stream_channel(self, guild: discord.Guild, user_id: int, channel_id: int) -> None:
        link = await self.get_stream_link(guild, user_id)
        if link is None or int(link.get("channel_id") or 0) == channel_id:
            return
        old_channel = int(link.get("channel_id") or 0)
        owned = bool(int(link.get("voice_status") or 0))
        await self.db.execute(
            "UPDATE stream_links SET channel_id=? WHERE guild_id=? AND user_id=?",
            channel_id,
            guild.id,
            user_id,
        )
        if not owned:
            return
        await self._refresh_voice_channel_status(guild, old_channel, we_owned=True)
        still = await self._set_voice_channel_status(guild, channel_id, hit_from_dict(link["hit"]))
        if not still:
            await self.db.execute(
                "UPDATE stream_links SET voice_status=0 WHERE guild_id=? AND user_id=?",
                guild.id,
                user_id,
            )

    async def clear_stream_link(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        cancel_end: bool = True,
    ) -> None:
        link = await self.get_stream_link(guild, user_id)
        if cancel_end:
            self._cancel_stream_end(guild.id, user_id)
        await self.db.execute(
            "DELETE FROM stream_links WHERE guild_id=? AND user_id=?",
            guild.id,
            user_id,
        )
        if link and int(link.get("voice_status") or 0):
            await self._refresh_voice_channel_status(
                guild, int(link.get("channel_id") or 0), we_owned=True,
            )

    async def _schedule_stream_end(self, guild: discord.Guild, user_id: int, ended: str) -> None:
        link = await self.get_stream_link(guild, user_id)
        if link is not None and link["source"] != ended:
            return
        if link is None and (guild.id, user_id) not in self._stream_session_reminded:
            return
        self._cancel_stream_end(guild.id, user_id)
        task = asyncio.create_task(self._confirm_stream_end(guild.id, user_id, ended))
        self._stream_end_tasks[(guild.id, user_id)] = task

    async def _confirm_stream_end(self, guild_id: int, user_id: int, ended: str) -> None:
        try:
            await asyncio.sleep(STREAM_END_GRACE)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            member = guild.get_member(user_id)
            if member is not None and member_stream_source(member) == ended:
                return
            self._stream_session_reminded.discard((guild_id, user_id))
            link = await self.get_stream_link(guild, user_id)
            if link is None or link["source"] != ended:
                return
            await self.finish_stream_link(guild, user_id)
        except asyncio.CancelledError:
            return
        finally:
            key = (guild_id, user_id)
            if self._stream_end_tasks.get(key) is asyncio.current_task():
                self._stream_end_tasks.pop(key, None)

    async def _fetch_messageable(
        self,
        guild: discord.Guild,
        channel_id: int | None,
    ) -> discord.abc.Messageable | None:
        channel = resolve_post_channel(guild, channel_id)
        if channel is not None:
            return channel
        if not channel_id:
            return None
        try:
            fetched = await self.bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
        if fetched is not None and hasattr(fetched, "send"):
            return fetched  # type: ignore[return-value]
        return None

    async def _stream_end_destinations(
        self,
        guild: discord.Guild,
        hit: MediaHit,
        link: dict[str, Any],
        member: discord.Member | None,
    ) -> list[discord.abc.Messageable]:
        seen: set[int] = set()
        dests: list[discord.abc.Messageable] = []

        def add(channel: discord.abc.Messageable | None) -> None:
            channel_id = getattr(channel, "id", None)
            if channel is None or not channel_id or int(channel_id) in seen:
                return
            seen.add(int(channel_id))
            dests.append(channel)

        add(await self._fetch_messageable(guild, int(link.get("post_channel_id") or 0)))
        add(await self.get_announce_channel(guild, hit.media_type))
        voice_id = member_voice_channel_id(member) if member else None
        add(await self._fetch_messageable(guild, voice_id or int(link.get("channel_id") or 0)))
        return dests

    async def announce_stream_link(
        self,
        guild: discord.Guild,
        user_id: int,
        hit: MediaHit,
        post_channel_id: int,
    ) -> None:
        """Publie la fiche dès qu'un live en cours est lié à une œuvre."""
        dests: list[discord.abc.Messageable] = []
        for channel in (
            await self._fetch_messageable(guild, post_channel_id),
            await self.get_announce_channel(guild, hit.media_type),
        ):
            if channel is not None and all(getattr(d, "id", None) != getattr(channel, "id", None) for d in dests):
                dests.append(channel)
        mention = mention_of(guild, self.bot, user_id)
        banner = f"{mention} · en stream"
        for channel in dests:
            try:
                message = await post_published_fiche(self, guild, hit, channel, banner=banner)
            except Exception:
                logger.exception("Publication fiche stream impossible")
                continue
            if message is not None:
                return
        logger.info("Fiche de début de stream : envoi impossible (%s)", user_id)

    async def finish_stream_link(self, guild: discord.Guild, user_id: int) -> None:
        self._stream_session_reminded.discard((guild.id, user_id))
        self._cancel_stream_remind(guild.id, user_id)
        link = await self.get_stream_link(guild, user_id)
        if link is None:
            return
        created_at = int(link.get("created_at") or 0)
        too_old = bool(created_at and int(time.time()) - created_at > STREAM_LINK_MAX_AGE)
        await self.clear_stream_link(guild, user_id, cancel_end=False)
        if too_old:
            return
        hit = hit_from_dict(link["hit"])
        member = guild.get_member(user_id)
        dests = await self._stream_end_destinations(guild, hit, link, member)
        if not dests:
            logger.info("Fiche de fin de stream : aucun salon pour poster (%s)", user_id)
            return
        mention = mention_of(guild, self.bot, user_id)
        banner = f"{mention} · stream terminé"
        for channel in dests:
            try:
                message = await post_published_fiche(self, guild, hit, channel, banner=banner)
            except Exception:
                logger.exception("Publication fiche stream impossible")
                continue
            if message is not None:
                return
        logger.info("Fiche de fin de stream : envoi impossible dans %s salon(s)", len(dests))
