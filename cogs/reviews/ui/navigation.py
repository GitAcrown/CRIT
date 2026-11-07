"""Clics sur les messages publics (fiche publiée, fiche de lien, annonce).

Leurs boutons sont utilisables par tout le monde : chaque clic agit pour la personne qui
clique et ne modifie jamais le message public pour elle. « Noter » ouvre directement son
formulaire ; « Ouvrir » lui affiche la fiche en privé (éphémère).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import NO_PINGS
from ..dyn import FicheRecord, get_record, hit_from_dict, is_live, mark_stripped
from ..providers import MediaHit
from .actions import rate_modal_for
from .fiche import render_announce_record, render_dyn_record
from .session import FicheView

if TYPE_CHECKING:
    from ..reviews import Reviews


async def open_private_fiche(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    published_wid: str | None,
    tab: str = "fiche",
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    view = FicheView(cog, guild, [hit], author_id=interaction.user.id, tab=tab, published_wid=published_wid)
    await view.start(interaction, deferred=True)


async def rate_from_public(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    published_wid: str | None,
) -> None:
    """Formulaire Noter direct ; la confirmation arrive en message privé."""
    view = FicheView(cog, guild, [hit], author_id=interaction.user.id, published_wid=published_wid)
    view.from_published_modal = True
    await view.load_member()
    await view.load_my_review()
    await interaction.response.send_modal(rate_modal_for(view, interaction.user.id))


async def _live_record(
    interaction: discord.Interaction,
    wid: str,
    render,
) -> tuple[FicheRecord, "Reviews", discord.Guild, MediaHit] | None:
    """Fiche encore active, ou on retire ses boutons et on prévient la personne."""
    rec = get_record(wid)
    if not is_live(rec):
        if rec is not None:
            view = render(rec, live=False)
            if view is not None:
                try:
                    await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    pass
            mark_stripped(rec.id)
        if not interaction.response.is_done():
            await interaction.response.send_message("Les boutons ont expiré.", ephemeral=True)
        return None
    cog = interaction.client.get_cog("Reviews")
    guild = interaction.guild
    raw = rec.payload.get("hit") if rec else None
    if cog is None or not isinstance(guild, discord.Guild) or not isinstance(raw, dict):
        await interaction.response.send_message("**Erreur ·** Impossible d'ouvrir cette fiche.", ephemeral=True)
        return None
    return rec, cog, guild, hit_from_dict(raw)  # type: ignore[return-value]


async def handle_published_fiche_click(interaction: discord.Interaction, wid: str, action: str) -> None:
    found = await _live_record(interaction, wid, render_dyn_record)
    if found is None:
        return
    rec, cog, guild, hit = found
    # La fiche de lien (compacte) n'affiche pas de statistiques à resynchroniser.
    published = None if rec.payload.get("kind") == "compact" else wid
    if action == "rate":
        await rate_from_public(interaction, cog, guild, hit, published_wid=published)
        return
    tab = "critiques" if action == "critiques" else "fiche"
    await open_private_fiche(interaction, cog, guild, hit, published_wid=published, tab=tab)


async def handle_announce_click(interaction: discord.Interaction, wid: str, action: str) -> None:
    found = await _live_record(interaction, wid, render_announce_record)
    if found is None:
        return
    _rec, cog, guild, hit = found
    if action == "rate":
        await rate_from_public(interaction, cog, guild, hit, published_wid=None)
        return
    await open_private_fiche(interaction, cog, guild, hit, published_wid=None)
