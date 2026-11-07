"""Clics sur les messages publics (fiche publiée, fiche de lien, annonce).

Leurs boutons sont utilisables par tout le monde : chaque clic agit pour la personne qui
clique et ne modifie jamais le message public pour elle. La fiche est déjà sous ses yeux,
donc on ne la réaffiche pas : Noter ouvre son formulaire, Ranger ouvre celui du signet
et des listes, Critiques montre seulement les avis du serveur.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import NO_PINGS
from ..dyn import FicheRecord, get_record, hit_from_dict, is_live, mark_stripped
from ..providers import MediaHit
from .actions import rate_modal_for
from .base import present_ephemeral_layout
from .fiche import render_announce_record, render_dyn_record
from .session import CritiquesView, FicheView
from .shelf import open_shelf, public_shelf_done

if TYPE_CHECKING:
    from ..reviews import Reviews

async def open_private_fiche(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    published_wid: str | None,
) -> None:
    """Fiche perso éphémère : bouton « Ouvrir la fiche », ou Noter quand on a déjà noté."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    view = FicheView(cog, guild, [hit], author_id=interaction.user.id, published_wid=published_wid)
    await view.start(interaction, deferred=True)


async def rate_from_public(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    published_wid: str | None,
) -> None:
    """Formulaire Noter direct ; la confirmation arrive en message privé.

    Déjà noté : le menu de la note propose aussi « Sans note », qui supprime la note.
    """
    view = FicheView(cog, guild, [hit], author_id=interaction.user.id, published_wid=published_wid)
    view.from_published_modal = True
    await view.load_member()
    await view.load_my_review()
    await interaction.response.send_modal(rate_modal_for(view, interaction.user.id))


async def show_critiques_from_public(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    view = CritiquesView(cog, guild, hit, author_id=interaction.user.id)
    await view.prepare()
    await present_ephemeral_layout(interaction, view)


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


async def _dispatch(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    action: str,
    *,
    published_wid: str | None,
) -> None:
    if action == "rate":
        await rate_from_public(interaction, cog, guild, hit, published_wid=published_wid)
    elif action in ("shelf", "mark", "list"):  # mark / list : anciens boutons déjà postés
        await open_shelf(interaction, cog, guild, hit, done=public_shelf_done)
    elif action == "critiques":
        await show_critiques_from_public(interaction, cog, guild, hit)
    else:
        await open_private_fiche(interaction, cog, guild, hit, published_wid=published_wid)


async def handle_published_fiche_click(interaction: discord.Interaction, wid: str, action: str) -> None:
    found = await _live_record(interaction, wid, render_dyn_record)
    if found is None:
        return
    _rec, cog, guild, hit = found
    await _dispatch(interaction, cog, guild, hit, action, published_wid=wid)


async def handle_announce_click(interaction: discord.Interaction, wid: str, action: str) -> None:
    found = await _live_record(interaction, wid, render_announce_record)
    if found is None:
        return
    _rec, cog, guild, hit = found
    await _dispatch(interaction, cog, guild, hit, action, published_wid=None)
