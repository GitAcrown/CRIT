"""Clics sur les messages publics (fiche publiée, fiche de lien, annonce).

Leurs boutons sont utilisables par tout le monde : chaque clic agit pour la personne qui
clique et ne modifie jamais le message public pour elle. La fiche est déjà sous ses yeux,
donc on ne la réaffiche pas : Noter ouvre son formulaire, le signet bascule dans ses
signets, la liste propose ses listes, Critiques montre seulement les avis du serveur.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import NO_PINGS
from ..dyn import FicheRecord, get_record, hit_from_dict, is_live, mark_stripped
from ..formatting import list_accepts_type
from ..providers import MediaHit
from .actions import FicheListSelect, rate_modal_for
from .base import present_ephemeral_layout, ReviewsLayout
from .fiche import render_announce_record, render_dyn_record
from .session import CritiquesView, FicheView

if TYPE_CHECKING:
    from ..reviews import Reviews

NOTICE_SECONDS = 12


async def _notice(interaction: discord.Interaction, text: str) -> None:
    await interaction.response.send_message(text, ephemeral=True, delete_after=NOTICE_SECONDS)


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

    Déjà noté : la fiche perso s'ouvre à la place, pour modifier ou supprimer sa note.
    """
    view = FicheView(cog, guild, [hit], author_id=interaction.user.id, published_wid=published_wid)
    view.from_published_modal = True
    await view.load_member()
    await view.load_my_review()
    if view.my_review:
        await open_private_fiche(interaction, cog, guild, hit, published_wid=published_wid)
        return
    await interaction.response.send_modal(rate_modal_for(view, interaction.user.id))


async def toggle_bookmark_from_public(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
) -> None:
    user_id = interaction.user.id
    title = pretty.shorten_text(hit.title, 80)
    media_id = await cog.lookup_media_id(guild, hit)
    if media_id and await cog.get_review(guild, user_id, media_id):
        await _notice(interaction, f"**Signet ·** Tu as déjà noté **{title}**.")
        return
    if media_id and await cog.is_on_watchlist(guild, user_id, media_id):
        await cog.remove_watchlist(guild, user_id, media_id)
        await _notice(interaction, f"**Signet retiré ·** {title}")
        return
    await cog.add_watchlist(guild, user_id, hit)
    await _notice(interaction, f"**Signet ajouté ·** {title} — retrouve-le dans `/carnet`.")


class PublicListPickView(ReviewsLayout):
    """Juste le choix de la liste, puis la confirmation à sa place."""

    def __init__(self, cog: "Reviews", guild: discord.Guild, hit: MediaHit, *, author_id: int, lists: list[dict[str, Any]]):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.writable_lists = lists
        self.done = ""
        self._build()

    async def add_to_list(self, interaction: discord.Interaction, list_id: int) -> None:
        record = next((item for item in self.writable_lists if int(item["id"]) == list_id), None)
        error = await self.cog.add_shared_list_item(self.guild, list_id, self.author_id, self.hit)
        name = pretty.shorten_text(record["title"], 80) if record else "la liste"
        self.done = f"**Liste ·** {error}" if error else f"**Ajouté ·** {self.hit.title} dans **{name}**."
        self._build()
        await self.push(interaction)
        self.stop()

    def _build(self) -> None:
        if self.done:
            self.set_layout([discord.ui.TextDisplay(self.done)])
            return
        title = pretty.shorten_text(self.hit.title, 80)
        self.set_layout(
            [discord.ui.TextDisplay(f"Ajouter **{title}** à…")],
            discord.ui.ActionRow(FicheListSelect(self, self.writable_lists)),
        )


async def pick_list_from_public(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
) -> None:
    lists = [
        record for record in await cog.writable_shared_lists(guild, interaction.user.id)
        if list_accepts_type(record.get("allowed_types"), hit.media_type)
    ][:25]
    if not lists:
        await _notice(interaction, "**Listes ·** Aucune liste que tu peux modifier n'accepte ce type. Crée-en une avec `/listes`.")
        return
    view = PublicListPickView(cog, guild, hit, author_id=interaction.user.id, lists=lists)
    await present_ephemeral_layout(interaction, view)


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
    elif action == "mark":
        await toggle_bookmark_from_public(interaction, cog, guild, hit)
    elif action == "list":
        await pick_list_from_public(interaction, cog, guild, hit)
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
