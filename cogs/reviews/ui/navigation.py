"""Ouverture des menus depuis une fiche publiée, une annonce ou une liste."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import NO_PINGS
from ..dyn import get_record, hit_from_dict, is_live, mark_stripped
from ..providers import MediaHit
from .base import present_ephemeral_layout, send_ephemeral_menu
from .fiche import render_announce_record, render_dyn_record, send_published_fiche
from .note import MyNoteView, PublicCritiquesView, PublicFichePeekView
from .session import MediaSessionView

if TYPE_CHECKING:
    from ..reviews import Reviews


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
            view = render_dyn_record(rec, live=False)
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
        published = None if rec.payload.get("kind") == "compact" else wid
        await open_personal_hit_menu(interaction, cog, guild, hit, published_wid=published)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    if action == "critiques":
        menu = await PublicCritiquesView.create(cog, guild, hit, interaction.user.id)
    else:
        menu = await PublicFichePeekView.create(cog, guild, hit, interaction.user.id)
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
    await present_ephemeral_layout(interaction, view)
