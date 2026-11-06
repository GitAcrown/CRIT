"""Menus des lives : lier une œuvre, lives en cours, streams planifiés."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import NO_PINGS, PARIS_TZ, STREAM_PLAN_MAX, logger
from ..emojis import PLANNING, STREAMING
from ..formatting import (
    hit_identity,
    mention_of,
    parse_plan_datetime,
    select_emoji,
    select_hit_description,
    type_emoji,
    type_label,
)
from ..providers import MediaHit
from ..streaming import member_stream_source, member_voice_channel_id
from .base import apply_view, replace_ephemeral_layout, ReviewsLayout, sep_tight
from .fiche import section_with_thumbnail, stream_mini_items
from .navigation import open_session_followup

if TYPE_CHECKING:
    from ..reviews import Reviews


class StreamHitSelect(discord.ui.Select):
    def __init__(self, parent: "StreamBindView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(
            placeholder="Choisir une œuvre",
            options=options,
            min_values=1,
            max_values=1,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.selected = int(self.values[0])
        await self._hub.show_selected(interaction)


class StreamMiniBindButton(discord.ui.Button):
    def __init__(self, parent: "StreamBindView"):
        super().__init__(label="Lier au stream", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = self._hub.guild
        member = guild.get_member(interaction.user.id)
        source = member_stream_source(member or interaction.user)
        if source is None:
            await interaction.followup.send(
                "**Stream ·** Tu n'es plus en live. Relance un Go Live, puis `/stream`.",
                ephemeral=True,
            )
            return
        channel_id = member_voice_channel_id(member or interaction.user)
        if not channel_id:
            await interaction.followup.send(
                "**Stream ·** Rejoins le salon vocal où tu stream, puis relie.",
                ephemeral=True,
            )
            return
        old = await self._hub.cog.get_stream_link(guild, interaction.user.id)
        await self._hub.cog.set_stream_link(
            guild,
            interaction.user.id,
            self._hub.hit,
            channel_id=channel_id,
            source=source,
            post_channel_id=int(interaction.channel_id or 0),
        )
        hub = await StreamHubView.create(self._hub.cog, guild, interaction.user.id)
        self._hub.stop()
        await apply_view(interaction, hub)
        if old is None or hit_identity(old["hit"]) != hit_identity(self._hub.hit):
            await self._hub.cog.announce_stream_link(
                guild, interaction.user.id, self._hub.hit, int(interaction.channel_id or 0),
            )


class StreamMiniUnlinkButton(discord.ui.Button):
    def __init__(self, parent: "StreamBindView"):
        super().__init__(label="Retirer le lien", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.cog.clear_stream_link(self._hub.guild, interaction.user.id)
        self._hub.mine = await self._hub.cog.get_stream_link(self._hub.guild, self._hub.author_id)
        self._hub._build()
        await apply_view(interaction, self._hub)


class StreamBindView(ReviewsLayout):
    """Recherche épurée pour lier (ou délier) un live."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.selected = 0
        self.mine: dict[str, Any] | None = None
        self._enriched: set[int] = set()
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None

    @property
    def hit(self) -> MediaHit:
        return self.hits[self.selected]

    def linked_to_selected(self) -> bool:
        if self.mine is None:
            return False
        return hit_identity(self.mine["hit"]) == hit_identity(self.hit)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def prepare(self) -> None:
        self.mine = await self.cog.get_stream_link(self.guild, self.author_id)
        await self.enrich_selected()
        self._enriched.add(self.selected)
        self._build()

    async def enrich_selected(self) -> None:
        if self.cog.catalog is None:
            return
        try:
            self.hits[self.selected] = await self.cog.catalog.enrich(self.hit)
        except Exception:
            logger.exception("Enrichissement de mini-fiche stream impossible")

    async def show_selected(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        if self.selected not in self._enriched:
            await self.enrich_selected()
            self._enriched.add(self.selected)
        self._build()
        await self.push(interaction)

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False) -> None:
        self._interaction = interaction
        await self.prepare()
        if deferred:
            await replace_ephemeral_layout(interaction, self)
            return
        await interaction.response.send_message(view=self, ephemeral=True, allowed_mentions=NO_PINGS)
        await self.attach(interaction)

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = [
            discord.ui.TextDisplay("### Lier au stream\n-# Choisis l'œuvre à afficher sur ton live")
        ]
        if len(self.hits) > 1:
            body.append(discord.ui.TextDisplay(f"-# {len(self.hits)} résultat(s)"))
            body.append(discord.ui.ActionRow(StreamHitSelect(self, self.hits, self.selected)))
        body.extend(stream_mini_items(hit))
        if self.linked_to_selected():
            action = StreamMiniUnlinkButton(self)
        else:
            action = StreamMiniBindButton(self)
        self.set_layout(body, discord.ui.ActionRow(action))


class StreamUnlinkButton(discord.ui.Button):
    def __init__(self, parent: "StreamHubView"):
        super().__init__(label="Retirer mon lien", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.cog.clear_stream_link(self._hub.guild, interaction.user.id)
        await self._hub.reload(interaction)


class StreamSearchModal(discord.ui.Modal, title="Lier mon stream"):
    def __init__(self, cog: "Reviews", guild: discord.Guild):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.query_input = discord.ui.TextInput(
            label="Titre de l'œuvre",
            placeholder="Ex. Dune 2021, Hades, Blonde…",
            min_length=2,
            max_length=80,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = self.guild
        member = guild.get_member(interaction.user.id)
        if member_stream_source(member or interaction.user) is None:
            await interaction.followup.send(
                "**Stream ·** Tu n'es plus en live. Relance un Go Live, puis réessaie.",
                ephemeral=True,
            )
            return
        resolved_type = await self.cog._resolve_search_type(guild, interaction.user.id, None)
        hits = await self.cog._search_or_reply(interaction, str(self.query_input.value), resolved_type)
        if not hits:
            return
        view = StreamBindView(
            self.cog,
            guild,
            hits,
            author_id=interaction.user.id,
        )
        await view.start(interaction, deferred=True)


class StreamHubBindButton(discord.ui.Button):
    def __init__(self, parent: "StreamHubView"):
        super().__init__(label="Lier mon stream", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        member = self._hub.guild.get_member(interaction.user.id)
        if member_stream_source(member or interaction.user) is None:
            await interaction.response.send_message(
                "**Stream ·** Passe d'abord en live (Go Live), puis reclique.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StreamSearchModal(self._hub.cog, self._hub.guild))


class StreamHubOpenSelect(discord.ui.Select):
    def __init__(self, parent: "StreamHubView", links: list[dict[str, Any]]):
        options = []
        for index, link in enumerate(links[:25]):
            hit: MediaHit = link["hit"]
            member = parent.guild.get_member(link["user_id"])
            who = member.display_name if member else "Membre"
            year = f" · {hit.year}" if hit.year else ""
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=pretty.shorten_text(
                        f"{who} · {type_label(hit.media_type)}{year}",
                        95,
                    ),
                    emoji=select_emoji(hit.media_type),
                )
            )
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._links = links

    async def callback(self, interaction: discord.Interaction) -> None:
        hit = self._links[int(self.values[0])]["hit"]
        await interaction.response.defer()
        await open_session_followup(
            self._hub.cog, self._hub.guild, interaction, hit, author_id=interaction.user.id,
        )


class StreamPlanHitSelect(discord.ui.Select):
    def __init__(self, parent: "StreamPlanPickView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(placeholder="Choisir une œuvre", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.selected = int(self.values[0])
        self._hub._build()
        await apply_view(interaction, self._hub)


class StreamPlanConfirmButton(discord.ui.Button):
    def __init__(self, parent: "StreamPlanPickView"):
        super().__init__(label="Planifier", style=discord.ButtonStyle.primary, )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        err = await self._hub.cog.create_stream_plan(
            self._hub.guild,
            interaction.user.id,
            self._hub.hit,
            self._hub.starts_at,
        )
        if err:
            await interaction.followup.send(f"**Stream ·** {err}", ephemeral=True)
            return
        view = await StreamHubView.create(self._hub.cog, self._hub.guild, interaction.user.id)
        await apply_view(interaction, view)


class StreamPlanPickView(ReviewsLayout):
    """Plusieurs résultats : choisir l'œuvre à planifier."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
        starts_at: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.starts_at = starts_at
        self.selected = 0
        self._build()

    @property
    def hit(self) -> MediaHit:
        return self.hits[self.selected]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        body: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"### Planifier\n-# <t:{self.starts_at}:F>"),
        ]
        if len(self.hits) > 1:
            body.append(discord.ui.ActionRow(StreamPlanHitSelect(self, self.hits, self.selected)))
        body.extend(stream_mini_items(self.hit))
        self.set_layout(body, discord.ui.ActionRow(StreamPlanConfirmButton(self)))


class StreamPlanModal(discord.ui.Modal, title="Planifier un stream"):
    def __init__(self, cog: "Reviews", guild: discord.Guild):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.query_input = discord.ui.TextInput(
            label="Titre de l'œuvre",
            placeholder="Ex. Dune 2021, Hades, Blonde…",
            min_length=2,
            max_length=80,
        )
        today = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
        self.date_input = discord.ui.TextInput(
            label="Date",
            placeholder="JJ/MM/AAAA",
            default=today,
            min_length=6,
            max_length=10,
        )
        self.time_input = discord.ui.TextInput(
            label="Heure (Paris)",
            placeholder="21:30",
            min_length=1,
            max_length=5,
        )
        self.add_item(self.query_input)
        self.add_item(self.date_input)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        stamp, err = parse_plan_datetime(str(self.date_input.value), str(self.time_input.value))
        if err or stamp is None:
            await interaction.followup.send(f"**Stream ·** {err or 'Date invalide.'}", ephemeral=True)
            return
        resolved_type = await self.cog._resolve_search_type(self.guild, interaction.user.id, None)
        hits = await self.cog._search_or_reply(interaction, str(self.query_input.value), resolved_type)
        if not hits:
            return
        if len(hits) == 1:
            save_err = await self.cog.create_stream_plan(self.guild, interaction.user.id, hits[0], stamp)
            if save_err:
                await interaction.followup.send(f"**Stream ·** {save_err}", ephemeral=True)
                return
            view = await StreamHubView.create(self.cog, self.guild, interaction.user.id)
            await apply_view(interaction, view)
            return
        view = StreamPlanPickView(
            self.cog,
            self.guild,
            hits,
            author_id=interaction.user.id,
            starts_at=stamp,
        )
        await apply_view(interaction, view)


class StreamHubPlanButton(discord.ui.Button):
    def __init__(self, parent: "StreamHubView"):
        super().__init__(label="Planifier un stream", style=discord.ButtonStyle.primary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        count = await self._hub.cog.count_upcoming_plans(self._hub.guild, interaction.user.id)
        if count >= STREAM_PLAN_MAX:
            await interaction.response.send_message(
                "**Stream ·** Tu as déjà 3 streams planifiés. Annule-en un pour en ajouter.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StreamPlanModal(self._hub.cog, self._hub.guild))


class StreamPlanInterestButton(discord.ui.Button):
    def __init__(self, parent: "StreamHubView", plan: dict[str, Any]):
        owner = int(plan["user_id"]) == parent.viewer_id
        interested = bool(plan.get("mine"))
        if owner:
            label, style = "Annuler", discord.ButtonStyle.danger
        elif interested:
            label, style = "Intéressé", discord.ButtonStyle.primary
        else:
            label, style = "Ça m'intéresse", discord.ButtonStyle.secondary
        super().__init__(label=label, style=style)
        self._hub = parent
        self._plan_id = int(plan["id"])
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._owner:
            await self._hub.cog.cancel_stream_plan(self._hub.guild, self._plan_id, interaction.user.id)
        else:
            saved = await self._hub.cog.toggle_plan_interest(
                self._hub.guild, self._plan_id, interaction.user.id,
            )
            if saved is None:
                await interaction.followup.send(
                    "**Stream ·** Ce stream n'est plus planifié.",
                    ephemeral=True,
                )
        await self._hub.reload(interaction)


class StreamHubView(ReviewsLayout):
    """Lives en cours et streams planifiés sur le serveur."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        viewer_id: int,
        links: list[dict[str, Any]],
        plans: list[dict[str, Any]],
        mine: dict[str, Any] | None,
        live: bool,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.viewer_id = viewer_id
        self.links = links
        self.plans = plans
        self.mine = mine
        self.live = live
        self._build()

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, viewer_id: int) -> "StreamHubView":
        member = guild.get_member(viewer_id)
        return cls(
            cog,
            guild,
            viewer_id=viewer_id,
            links=await cog.list_active_stream_links(guild),
            plans=await cog.list_upcoming_plans(guild, viewer_id),
            mine=await cog.get_stream_link(guild, viewer_id),
            live=member_stream_source(member) is not None if member else False,
        )

    async def reload(self, interaction: discord.Interaction) -> None:
        self.links = await self.cog.list_active_stream_links(self.guild)
        self.plans = await self.cog.list_upcoming_plans(self.guild, self.viewer_id)
        self.mine = await self.cog.get_stream_link(self.guild, self.viewer_id)
        member = self.guild.get_member(self.viewer_id)
        self.live = member_stream_source(member) is not None if member else False
        self._build()
        await apply_view(interaction, self)

    def _plan_block(self, plan: dict[str, Any]) -> discord.ui.Item:
        hit: MediaHit = plan["hit"]
        year = f" ({hit.year})" if hit.year else ""
        mention = mention_of(self.guild, self.cog.bot, plan["user_id"])
        interest = int(plan.get("interest") or 0)
        if interest == 1:
            people = "  ·  1 intéressé"
        elif interest:
            people = f"  ·  {interest} intéressés"
        else:
            people = ""
        text = (
            f"{mention}\n"
            f"{type_emoji(hit.media_type)} **{hit.title}**{year}\n"
            f"-# {type_label(hit.media_type)}  ·  <t:{int(plan['starts_at'])}:F>{people}"
        )
        button = StreamPlanInterestButton(self, plan)
        try:
            return discord.ui.Section(discord.ui.TextDisplay(text), accessory=button)
        except Exception:
            return discord.ui.TextDisplay(text)

    def _build(self) -> None:
        body: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"### Streams\n-# {len(self.links)} live(s) · {len(self.plans)} planifié(s)"
            )
        ]
        actions: list[discord.ui.ActionRow] = []
        if self.plans:
            for plan in self.plans[:8]:
                body.append(sep_tight())
                body.append(self._plan_block(plan))
        if self.links:
            for link in self.links[:8]:
                hit: MediaHit = link["hit"]
                year = f" ({hit.year})" if hit.year else ""
                mention = mention_of(self.guild, self.cog.bot, link["user_id"])
                text = (
                    f"{mention}\n{type_emoji(hit.media_type)} **{hit.title}**{year}\n"
                    f"-# {type_label(hit.media_type)}"
                    + (f"  ·  <#{link['channel_id']}>" if link.get("channel_id") else "")
                )
                body.append(sep_tight())
                body.append(section_with_thumbnail(text, hit.poster_url))
            actions.append(discord.ui.ActionRow(StreamHubOpenSelect(self, self.links)))
        elif not self.plans:
            body.append(discord.ui.TextDisplay(
                "*Aucun live ni stream planifié. **Planifier** une date, "
                "ou passe en Go Live puis **Lier mon stream**.*"
            ))
        row: list[discord.ui.Item] = [StreamHubPlanButton(self)]
        if self.mine:
            row.append(StreamUnlinkButton(self))
        elif self.live:
            row.append(StreamHubBindButton(self))
        actions.append(discord.ui.ActionRow(*row))
        self.set_layout(body, *actions)


class StreamRemindView(ReviewsLayout):
    """Rappel privé (ou salon vocal) pour lier le live en cours."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        viewer_id: int,
        channel_id: int | None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.viewer_id = viewer_id
        salon = f" sur <#{channel_id}>" if channel_id else ""
        self.set_layout(
            [
                discord.ui.TextDisplay(
                    f"{STREAMING} **Stream en cours{salon}**\n"
                    f"-# Lance `/stream` sur **{guild.name}** pour lier une œuvre."
                )
            ]
        )


class StreamPlanDueView(ReviewsLayout):
    """MP à l'heure d'un stream planifié, si la personne n'est pas déjà en live."""

    def __init__(self, guild: discord.Guild, hit: MediaHit):
        super().__init__(timeout=None)
        year = f" ({hit.year})" if hit.year else ""
        self.set_layout(
            [
                discord.ui.TextDisplay(
                    f"{PLANNING} **C'est l'heure**\n"
                    f"{type_emoji(hit.media_type)} **{hit.title}**{year}\n"
                    f"-# Sur **{guild.name}**. Lance un Go Live, puis `/stream` pour lier l'œuvre."
                )
            ]
        )
