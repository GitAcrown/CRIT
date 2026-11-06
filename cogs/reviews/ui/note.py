"""Ma note et les critiques publiques d'une fiche."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import REVIEWS_PAGE
from ..emojis import TRASH
from ..formatting import (
    experienced_from_row,
    experienced_line,
    format_comment,
    format_score,
    mention_of,
    row_spoiler,
    user_display,
)
from ..prefs import note_experienced_default, note_spoiler_default, UserPrefs
from ..progress import resolve_star_skin, StarSkin
from ..providers import MediaHit
from .actions import AddToListButton, RateModal, review_saved_lines, WatchlistButton
from .base import apply_view, ReviewsLayout, sep_tight
from .fiche import (
    append_fiche_sections,
    critiques_summary_line,
    fiche_footer_line,
    fiche_intro,
    fiche_meta_line,
    fiche_plan_payload,
    fiche_title_line,
    section_with_thumbnail,
    stream_live_items,
    sync_published_fiche,
)

if TYPE_CHECKING:
    from ..reviews import Reviews


class MyNoteEditButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(
            label="Modifier" if parent.my_review else "Ajouter une note",
            style=discord.ButtonStyle.secondary if parent.my_review else discord.ButtonStyle.green,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        existing = self._hub.my_review or {}
        prefs = getattr(self._hub, "prefs", None) or UserPrefs()
        await interaction.response.send_modal(
            RateModal(
                self._hub,
                max_comment=self._hub.cog.cached_comment_max(self._hub.guild),
                default_rating=existing.get("rating"),
                default_comment=existing.get("comment") or "",
                default_experienced=note_experienced_default(prefs, existing),
                default_spoiler=note_spoiler_default(existing),
            )
        )


class MyNoteDeleteButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(style=discord.ButtonStyle.red, emoji=discord.PartialEmoji.from_str(TRASH))
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._hub.menu = "confirm_delete"
        self._hub._build()
        await apply_view(interaction, self._hub)


class MyNoteDeleteConfirmButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.danger)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.delete_review(interaction)


class MyNoteDeleteCancelButton(discord.ui.Button):
    def __init__(self, parent: "MyNoteView"):
        super().__init__(label="Annuler", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.menu = "browse"
        self._hub._build()
        await apply_view(interaction, self._hub)


class MyNoteView(ReviewsLayout):
    """Menu éphémère : uniquement la note du clicqueur."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        my_review: dict | None,
        published_wid: str | None,
        prefs: UserPrefs | None = None,
        on_watchlist: bool = False,
        star_skin: StarSkin | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.my_review = my_review
        self.published_wid = published_wid
        self.prefs = prefs or UserPrefs()
        self.star_skin = star_skin if star_skin is not None else resolve_star_skin(self.prefs.star_skin)
        self.on_watchlist = on_watchlist and not bool(my_review)
        self.from_published_modal = False
        self.menu = "browse"
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
        *,
        published_wid: str | None,
    ) -> "MyNoteView":
        media_id = await cog.lookup_media_id(guild, hit)
        mine = await cog.get_review(guild, author_id, media_id) if media_id else None
        on_watchlist = (
            bool(media_id)
            and mine is None
            and await cog.is_on_watchlist(guild, author_id, media_id)
        )
        await cog.get_comment_max(guild)
        prefs = await cog.get_user_prefs(guild, author_id)
        return cls(
            cog,
            guild,
            hit,
            author_id=author_id,
            my_review=mine,
            published_wid=published_wid,
            prefs=prefs,
            on_watchlist=on_watchlist,
            star_skin=await cog.star_skin_for(guild, author_id),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne concerne que ta note.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        mine = self.my_review
        if self.menu == "confirm_delete" and not mine:
            self.menu = "browse"
        if mine:
            text = (
                f"{fiche_title_line(hit)}\n"
                f"{self.stars(mine['rating'])}  **{format_score(mine['rating'])}**\n"
            )
            shown = format_comment(
                mine.get("comment") or "",
                spoiler=row_spoiler(mine),
                hide=False,
                limit=240,
            )
            if shown:
                text += f"{shown}\n"
            seen = experienced_line(hit.media_type, mine.get("experienced_at"))
            if seen:
                text += f"{seen}\n"
            text += f"-# Ta note · {fiche_meta_line(hit)}"
        else:
            text = (
                f"{fiche_title_line(hit)}\n"
                f"*Tu n'as pas encore noté cette œuvre.*\n"
                f"-# {fiche_meta_line(hit)}"
            )
        body: list[discord.ui.Item] = [section_with_thumbnail(text, hit.poster_url)]
        if self.menu == "confirm_delete":
            title = pretty.shorten_text(hit.title, 80) or "cette œuvre"
            body.append(discord.ui.TextDisplay(
                f"**Supprimer {title} ?**\n"
                "-# Ta note et ton commentaire seront retirés."
            ))
            actions: list[discord.ui.Item] = [
                MyNoteDeleteConfirmButton(self),
                MyNoteDeleteCancelButton(self),
            ]
        else:
            actions = [MyNoteEditButton(self)]
            if mine:
                actions.append(MyNoteDeleteButton(self))
            actions.append(WatchlistButton(self))
            actions.append(AddToListButton(self))
        self.set_layout(body, discord.ui.ActionRow(*actions))

    async def save_review(
        self,
        interaction: discord.Interaction,
        rating: float,
        comment: str,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> None:
        created, award = await self.cog.upsert_review(
            self.guild,
            interaction.user,
            self.hit,
            rating,
            comment,
            experienced_at=experienced_at,
            spoiler=spoiler,
        )
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id) if media_id else None
        self.on_watchlist = False
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        collections = await self.cog.grant_event_collections(self.guild, interaction.user.id, self.hit)
        await interaction.followup.send(
            "\n".join(review_saved_lines(
                self.hit, rating, created, award, skin=self.star_skin, collections=collections,
            )),
            ephemeral=True,
        )
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at, spoiler=spoiler,
        )

    async def delete_review(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_review(self.guild, self.author_id, self.hit)
        self.my_review = None
        self.on_watchlist = False
        self.menu = "browse"
        self._build()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await apply_view(interaction, self)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        if media_id:
            self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id)
            self.on_watchlist = (not self.my_review) and await self.cog.is_on_watchlist(
                self.guild, self.author_id, media_id
            )
        else:
            self.my_review = None
            self.on_watchlist = False
        self._build()
        if interaction is not None:
            await apply_view(interaction, self)


class PublicFichePeekView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Fiche public."""

    def __init__(
        self,
        hit: MediaHit,
        *,
        avg: float | None,
        count: int,
        social: str,
        stream_channels: list[int] | None = None,
        plans: list[dict] | None = None,
        guild_name: str = "",
        star_skin: StarSkin | None = None,
    ):
        super().__init__()
        self.star_skin = star_skin
        self._interaction: discord.Interaction | None = None
        body: list[discord.ui.Item] = []
        body.extend(stream_live_items(stream_channels or [], plans))
        body.extend(fiche_intro(hit))
        append_fiche_sections(
            body, hit, avg=avg, count=count, my_review=None, social_line=social, guild_name=guild_name,
            skin=self.star_skin,
        )
        footer = fiche_footer_line(hit)
        if footer:
            body.append(discord.ui.TextDisplay(f"-# {footer}"))
        self.set_layout(body)

    @classmethod
    async def create(
        cls, cog: "Reviews", guild: discord.Guild, hit: MediaHit, viewer_id: int,
    ) -> "PublicFichePeekView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        social = await cog.public_fiche_line(guild, hit, reviews, media_id)
        stream_channels = await cog.stream_channels_for_hit(guild, hit)
        plans = fiche_plan_payload(await cog.plans_for_hit(guild, hit))
        return cls(
            hit,
            avg=avg,
            count=count,
            social=social,
            stream_channels=stream_channels,
            plans=plans,
            guild_name=guild.name,
            star_skin=await cog.star_skin_for(guild, viewer_id),
        )


class PublicCritiquesPageButton(discord.ui.Button):
    def __init__(self, parent: "PublicCritiquesView", delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.page = max(0, self._hub.page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)


class PublicCritiquesView(ReviewsLayout):
    """Menu éphémère lecture seule — ouvert depuis le bouton Critiques public."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        *,
        author_id: int,
        reviews: list[Any],
        avg: float | None,
        count: int,
        stream_channels: list[int] | None = None,
        plans: list[dict] | None = None,
        star_skin: StarSkin | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hit = hit
        self.author_id = author_id
        self.reviews = reviews
        self.avg = avg
        self.count = count
        self.stream_channels = stream_channels or []
        self.stream_plans = plans or []
        self.star_skin = star_skin
        self.page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(
        cls,
        cog: "Reviews",
        guild: discord.Guild,
        hit: MediaHit,
        author_id: int,
    ) -> "PublicCritiquesView":
        media_id = await cog.lookup_media_id(guild, hit)
        avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
        reviews = await cog.list_reviews(guild, media_id) if media_id else []
        stream_channels = await cog.stream_channels_for_hit(guild, hit)
        plans = fiche_plan_payload(await cog.plans_for_hit(guild, hit))
        return cls(
            cog,
            guild,
            hit,
            author_id=author_id,
            reviews=reviews,
            avg=avg,
            count=count,
            stream_channels=stream_channels,
            plans=plans,
            star_skin=await cog.star_skin_for(guild, author_id),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = []
        body.extend(stream_live_items(self.stream_channels, self.stream_plans))
        body.extend(fiche_intro(hit, backdrop=False))
        rows: list[discord.ui.ActionRow] = []
        total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE) if self.reviews else 1
        if self.reviews:
            max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
            self.page = min(self.page, max_page)
        body.append(discord.ui.TextDisplay(critiques_summary_line(
            hit, count=self.count, avg=self.avg, page=self.page, total_pages=total_pages,
            skin=self.star_skin,
        )))
        body.append(sep_tight())
        if not self.reviews:
            body.append(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
            self.set_layout(body)
            return
        start = self.page * REVIEWS_PAGE
        page_rows = self.reviews[start:start + REVIEWS_PAGE]
        for index, row in enumerate(page_rows):
            if index:
                body.append(sep_tight())
            user_id = int(row["user_id"])
            _name, avatar = user_display(self.guild, self.cog.bot, user_id)
            text = (
                f"{mention_of(self.guild, self.cog.bot, user_id)}\n"
                f"{self.stars(row['rating'])}  **{format_score(row['rating'])}** · <t:{row['updated_at']}:R>"
            )
            shown = format_comment(
                row["comment"] or "",
                spoiler=row_spoiler(row),
                hide=True,
                limit=220,
            )
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar))
        max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
        if max_page > 0:
            prev_btn = PublicCritiquesPageButton(self, -1, "←")
            next_btn = PublicCritiquesPageButton(self, 1, "→")
            prev_btn.disabled = self.page <= 0
            next_btn.disabled = self.page >= max_page
            rows.append(discord.ui.ActionRow(prev_btn, next_btn))
        self.set_layout(body, *rows)
