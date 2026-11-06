"""Détection des lives Discord d'un membre."""

from __future__ import annotations

import discord


def resolve_guild_text_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


def resolve_post_channel(guild: discord.Guild, channel_id: int | None) -> discord.abc.Messageable | None:
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id)) or guild.get_thread(int(channel_id))
    if channel is not None and hasattr(channel, "send"):
        return channel
    return None


def has_streaming_activity(member: discord.Member | discord.abc.User) -> bool:
    for activity in getattr(member, "activities", ()) or ():
        if isinstance(activity, discord.Streaming):
            return True
        if getattr(activity, "type", None) == discord.ActivityType.streaming:
            return True
    return False


def member_stream_source(member: discord.Member | discord.abc.User) -> str | None:
    """`voice` = Go Live Discord, `activity` = Twitch / YouTube sur le statut."""
    voice = getattr(member, "voice", None)
    if voice is not None and getattr(voice, "self_stream", False):
        return "voice"
    if has_streaming_activity(member):
        return "activity"
    return None


def member_voice_channel_id(member: discord.Member | discord.abc.User) -> int | None:
    voice = getattr(member, "voice", None)
    channel = getattr(voice, "channel", None) if voice is not None else None
    return int(channel.id) if channel is not None else None
