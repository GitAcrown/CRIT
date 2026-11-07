"""Ranger une œuvre : signet et listes dans un seul formulaire.

Un bouton (fiche perso ou message public) ouvre « Ranger cette œuvre » : une case
Signet et un menu des listes modifiables, déjà cochées si l'œuvre y est. Cocher
ajoute, décocher retire.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TYPE_CHECKING

import discord

from utils import pretty

from ..emojis import SHELF
from ..formatting import list_accepts_type
from ..providers import MediaHit

if TYPE_CHECKING:
    from ..reviews import Reviews
    from .session import FicheView

NOTICE_SECONDS = 12
Done = Callable[[discord.Interaction, str], Awaitable[None]]


class ShelfModal(discord.ui.Modal, title="Ranger cette œuvre"):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        user_id: int,
        rated: bool,
        bookmarked: bool,
        lists: list[dict[str, Any]],
        in_lists: set[int],
        done: Done,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.user_id = user_id
        self.bookmarked = bookmarked
        self.lists = lists
        self.in_lists = in_lists
        self.done = done

        self.bookmark_check: discord.ui.Checkbox | None = None
        if rated:
            self.add_item(discord.ui.TextDisplay("-# Déjà notée : pas besoin de signet."))
        else:
            self.bookmark_check = discord.ui.Checkbox(custom_id="bookmark", default=bookmarked)
            self.add_item(discord.ui.Label(
                text="Signet",
                description="À voir plus tard, dans /carnet",
                component=self.bookmark_check,
            ))

        self.list_select: discord.ui.Select | None = None
        if lists:
            options = [
                discord.SelectOption(
                    label=pretty.shorten_text(record["title"], 95) or "Sans titre",
                    value=str(record["id"]),
                    description=f"{record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''}",
                    default=int(record["id"]) in in_lists,
                )
                for record in lists
            ]
            self.list_select = discord.ui.Select(
                placeholder="Aucune liste",
                options=options,
                min_values=0,
                max_values=len(options),
                required=False,
            )
            self.add_item(discord.ui.Label(
                text="Listes",
                description="Coche les listes où ranger l'œuvre",
                component=self.list_select,
            ))
        else:
            self.add_item(discord.ui.TextDisplay(
                "-# Aucune liste que tu peux modifier n'accepte ce type. Crée-en une avec `/listes`."
            ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog, guild, hit = self.cog, self.guild, self.hit
        title = pretty.shorten_text(hit.title, 80)
        lines: list[str] = []

        if self.bookmark_check is not None and bool(self.bookmark_check.value) != self.bookmarked:
            if self.bookmark_check.value:
                await cog.add_watchlist(guild, self.user_id, hit)
                lines.append(f"**Signet ajouté ·** {title}")
            else:
                media_id = await cog.lookup_media_id(guild, hit)
                if media_id:
                    await cog.remove_watchlist(guild, self.user_id, media_id)
                lines.append(f"**Signet retiré ·** {title}")

        if self.list_select is not None:
            names = {int(r["id"]): pretty.shorten_text(r["title"], 60) for r in self.lists}
            chosen = {int(value) for value in self.list_select.values}
            added: list[str] = []
            for list_id in sorted(chosen - self.in_lists):
                error = await cog.add_shared_list_item(guild, list_id, self.user_id, hit)
                if error:
                    lines.append(f"**{names.get(list_id, 'Liste')} ·** {error}")
                else:
                    added.append(names.get(list_id, "la liste"))
            removed: list[str] = []
            dropped = self.in_lists - chosen
            if dropped:
                media_id = await cog.lookup_media_id(guild, hit)
                for list_id in sorted(dropped):
                    if media_id:
                        await cog.remove_shared_list_item(guild, list_id, media_id)
                    removed.append(names.get(list_id, "la liste"))
            if added:
                lines.append(f"**Ajouté ·** {title} dans " + ", ".join(f"**{n}**" for n in added) + ".")
            if removed:
                lines.append(f"**Retiré ·** {title} de " + ", ".join(f"**{n}**" for n in removed) + ".")

        await self.done(interaction, "\n".join(lines) or "-# Rien n'a changé.")


async def open_shelf(
    interaction: discord.Interaction,
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    done: Done,
) -> None:
    """Ouvre le formulaire (réponse directe au clic : rien d'autre avant)."""
    user_id = interaction.user.id
    media_id = await cog.lookup_media_id(guild, hit)
    rated = bool(media_id and await cog.get_review(guild, user_id, media_id))
    bookmarked = bool(media_id) and await cog.is_on_watchlist(guild, user_id, media_id)
    lists = [
        record for record in await cog.writable_shared_lists(guild, user_id)
        if list_accepts_type(record.get("allowed_types"), hit.media_type)
    ][:25]
    if rated and not lists:
        await interaction.response.send_message(
            "**Ranger ·** Tu as déjà noté cette œuvre, et aucune liste que tu peux modifier "
            "n'accepte ce type. Crée-en une avec `/listes`.",
            ephemeral=True,
            delete_after=NOTICE_SECONDS,
        )
        return
    in_lists = await cog.shared_lists_with_media(media_id or 0, [int(r["id"]) for r in lists])
    await interaction.response.send_modal(ShelfModal(
        cog, guild, hit,
        user_id=user_id,
        rated=rated,
        bookmarked=bookmarked,
        lists=lists,
        in_lists=in_lists,
        done=done,
    ))


async def public_shelf_done(interaction: discord.Interaction, text: str) -> None:
    """Depuis un message public : le résultat en message éphémère, qui s'efface."""
    await interaction.response.send_message(text, ephemeral=True, delete_after=NOTICE_SECONDS)


class ShelfButton(discord.ui.Button):
    """Fiche perso : bouton Ranger, en bleu si l'œuvre est en signet."""

    def __init__(self, parent: "FicheView"):
        on = bool(parent.on_watchlist) and not parent.my_review
        super().__init__(
            style=discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(SHELF),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        hub = self._hub

        async def done(submit: discord.Interaction, text: str) -> None:
            await submit.response.defer()
            hub.notice = text
            await hub.refresh(submit)

        await open_shelf(interaction, hub.cog, hub.guild, hub.hit, done=done)
