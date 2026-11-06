"""Menu principal d'une œuvre (`MediaSessionView`) et boutons d'onglets."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import NO_PINGS, REVIEWS_PAGE, logger
from ..formatting import (
    experienced_from_row,
    experienced_line,
    format_comment,
    format_score,
    mention_of,
    row_spoiler,
    season_media,
    split_tv_scope,
    tv_season_list,
    user_display,
)
from ..prefs import UserPrefs
from ..providers import MediaHit
from .actions import (
    AddToListButton,
    DeleteReviewButton,
    DeleteReviewCancelButton,
    DeleteReviewConfirmButton,
    FicheShareButton,
    MediaSelect,
    RateButton,
    review_saved_lines,
    SeasonScopeSelect,
    share_button_kwargs,
    TabButton,
    WatchlistButton,
)
from .base import (
    apply_view,
    discard_ephemeral_menu,
    labeled_tabs,
    present_ephemeral_layout,
    publish_layout_message,
    ReviewsLayout,
    sep_tight,
    sep_wide,
)
from .fiche import (
    also_liked_line,
    append_fiche_sections,
    critiques_summary_line,
    fiche_footer_line,
    fiche_intro,
    fiche_plan_payload,
    section_with_thumbnail,
    stream_live_items,
    sync_published_fiche,
)

if TYPE_CHECKING:
    from ..reviews import Reviews
    from .profile import ProfileView


class ProfileShareButton(discord.ui.Button):
    def __init__(self, parent: "ProfileView"):
        super().__init__(**share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        was_editable = self._hub.editable
        self._hub.editable = False
        body: list[discord.ui.Item] = [self._hub._header_item(avatar=True)]
        content = self._hub._profil_content()
        self._hub.editable = was_editable
        if content:
            body.append(sep_wide())
            body.extend(content)
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body, accent_colour=self._hub.accent_colour))
        message = await publish_layout_message(interaction, view)
        if message is None:
            await interaction.followup.send("**Erreur ·** Impossible de publier ce profil.", ephemeral=True)
            return
        self._hub.stop()
        await discard_ephemeral_menu(interaction)


class HubTabButton(discord.ui.Button):
    def __init__(self, parent: Any, tab: str, label: str | None, *, emoji: str | None = None):
        super().__init__(
            label=label,
            emoji=discord.PartialEmoji.from_str(emoji) if emoji else None,
            style=discord.ButtonStyle.primary if parent.tab == tab else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._tab = tab

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.tab = self._tab
        self._hub._build()
        await apply_view(interaction, self._hub)


class HubPageButton(discord.ui.Button):
    def __init__(self, parent: Any, attr: str, delta: int, label: str, max_page: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._attr = attr
        self._delta = delta
        self._max_page = max_page

    async def callback(self, interaction: discord.Interaction) -> None:
        current = getattr(self._hub, self._attr)
        setattr(self._hub, self._attr, max(0, min(self._max_page, current + self._delta)))
        self._hub._build()
        await apply_view(interaction, self._hub)


class MediaSessionView(ReviewsLayout):
    """Recherche → fiche → critiques, dans une seule vue interactive."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
        ephemeral: bool,
        pending_rating: float | None = None,
        pending_comment: str = "",
        selected: int = 0,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.ephemeral = ephemeral
        self.pending_rating = pending_rating
        self.pending_comment = pending_comment
        self.selected = selected
        self.season: int | None = None
        self.tab = "fiche"
        self.review_page = 0
        self.avg: float | None = None
        self.count = 0
        self.my_review: dict | None = None
        self.on_watchlist = False
        self.reviews: list[Any] = []
        self.social_line = ""
        self.stream_channels: list[int] = []
        self.stream_plans: list[dict] = []
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self.published_wid: str | None = None
        self.from_published_modal = False
        self.confirm_delete = False
        self.prefs = UserPrefs()
        self._enriched: set[int] = set()

    @property
    def show_hit(self) -> MediaHit:
        return self.hits[self.selected]

    @property
    def hit(self) -> MediaHit:
        show = self.show_hit
        if self.season is None or show.media_type != "tv":
            return show
        return season_media(show, self.season)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ephemeral and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def prepare(self) -> None:
        await self.cog.get_comment_max(self.guild)
        self.prefs = await self.cog.get_user_prefs(self.guild, self.author_id)
        self.star_skin = await self.cog.star_skin_for(self.guild, self.author_id)
        await self.enrich_selected()
        self._enriched.add(self.selected)
        await self.reload_stats()
        self._build()

    async def enrich_selected(self) -> None:
        show, season = split_tv_scope(self.hits[self.selected])
        self.hits[self.selected] = show
        if season is not None and self.season is None:
            self.season = season
        if self.cog.catalog is None:
            return
        try:
            self.hits[self.selected] = await self.cog.catalog.enrich(show)
        except Exception:
            logger.exception("Enrichissement de fiche impossible")

    async def reload_stats(self) -> None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.avg, self.count = await self.cog.media_stats(self.guild, media_id) if media_id else (None, 0)
        self.reviews = await self.cog.list_reviews(self.guild, media_id) if media_id else []
        self.my_review = None
        self.on_watchlist = False
        if media_id and self.ephemeral:
            self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id)
            self.on_watchlist = await self.cog.is_on_watchlist(self.guild, self.author_id, media_id)
        if self.ephemeral:
            liked = await self.cog.also_liked(self.guild, media_id, viewer_id=self.author_id) if media_id else []
            self.social_line = also_liked_line(self.hit, liked)
        else:
            self.social_line = self.cog.social_line_for_reviews(
                self.guild,
                self.reviews,
                viewer_id=None,
            )
        self.stream_channels = await self.cog.stream_channels_for_hit(self.guild, self.show_hit)
        self.stream_plans = fiche_plan_payload(await self.cog.plans_for_hit(self.guild, self.show_hit))

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
        await self.reload_stats()
        self.pending_rating = None
        self.confirm_delete = False
        self.tab = "fiche"
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        if not self.from_published_modal:
            await self.refresh(interaction)
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

    def _tabs_row(self) -> discord.ui.ActionRow:
        fiche_tab, critiques_tab = labeled_tabs("Fiche", f"Critiques ({self.count})")
        return discord.ui.ActionRow(
            TabButton(self, "fiche", fiche_tab),
            TabButton(self, "critiques", critiques_tab),
        )

    def _season_rows(self) -> list[discord.ui.ActionRow]:
        if self.show_hit.media_type != "tv":
            return []
        seasons = tv_season_list(self.show_hit)
        if self.season is not None and all(int(item["n"]) != self.season for item in seasons):
            seasons = [*seasons, {"n": self.season, "episodes": 0}]
        if not seasons:
            return []
        rows: list[discord.ui.ActionRow] = []
        for index in range(0, len(seasons), 24):
            chunk = seasons[index:index + 24]
            rows.append(discord.ui.ActionRow(
                SeasonScopeSelect(self, chunk, include_show=index == 0),
            ))
            if len(rows) == 3:
                break
        return rows

    def _build(self) -> None:
        hit = self.hit
        body: list[discord.ui.Item] = []
        actions: list[discord.ui.ActionRow] = []
        above: list[discord.ui.Item] = []
        title_row: discord.ui.Item | None = None

        if len(self.hits) > 1:
            if not any(item.source == "tmdb" for item in self.hits) and any(
                item.source == "spotify" for item in self.hits
            ):
                if self.cog.catalog is not None and not self.cog.catalog.tmdb.available:
                    above.append(discord.ui.TextDisplay("-# Films et séries absents · clé TMDB manquante."))
                else:
                    above.append(discord.ui.TextDisplay(
                        "-# Aucun film ou série trouvé — précise le type si besoin."
                    ))
            title_row = discord.ui.ActionRow(MediaSelect(self, self.hits, self.selected))

        above.append(self._tabs_row())
        body.extend(stream_live_items(self.stream_channels, self.stream_plans))
        sticky_head = 0

        if self.tab == "fiche":
            body.extend(fiche_intro(hit, replace_title=title_row))
            sticky_head = len(body)
            append_fiche_sections(
                body,
                hit,
                avg=self.avg,
                count=self.count,
                my_review=self.my_review if self.ephemeral and not self.published_wid else None,
                social_line=self.social_line,
                guild_name=self.guild.name,
                skin=self.star_skin,
            )
            footer = fiche_footer_line(hit)
            if footer:
                body.append(discord.ui.TextDisplay(f"-# {footer}"))
        else:
            body.extend(fiche_intro(hit, backdrop=False, replace_title=title_row))
            sticky_head = len(body)
            total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE) if self.reviews else 1
            if self.reviews:
                max_page = max(0, (len(self.reviews) - 1) // REVIEWS_PAGE)
                self.review_page = min(self.review_page, max_page)
            body.append(discord.ui.TextDisplay(critiques_summary_line(
                hit, count=self.count, avg=self.avg, page=self.review_page, total_pages=total_pages,
                skin=self.star_skin,
            )))
            body.append(sep_tight())
            if not self.reviews:
                body.append(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
            else:
                start = self.review_page * REVIEWS_PAGE
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
            if len(self.reviews) > REVIEWS_PAGE:
                nav_btns: list[discord.ui.Item] = []
                if self.review_page > 0:
                    nav_btns.append(_ReviewPageButton(self, -1, "←"))
                if (self.review_page + 1) * REVIEWS_PAGE < len(self.reviews):
                    nav_btns.append(_ReviewPageButton(self, 1, "→"))
                if nav_btns:
                    actions.append(discord.ui.ActionRow(*nav_btns))

        season_rows = self._season_rows()
        actions.extend(season_rows)
        if self.confirm_delete and not (self.ephemeral and self.my_review):
            self.confirm_delete = False
        if not self.published_wid:
            if season_rows:
                actions.append(sep_tight())
            if self.confirm_delete:
                title = pretty.shorten_text(hit.title, 80) or "cette œuvre"
                actions.append(discord.ui.TextDisplay(
                    f"**Supprimer {title} ?**\n"
                    "-# Ta note et ton commentaire seront retirés."
                ))
                actions.append(discord.ui.ActionRow(
                    DeleteReviewConfirmButton(self),
                    DeleteReviewCancelButton(self),
                ))
            else:
                rate_label = "Noter"
                if self.ephemeral and self.pending_rating is not None and self.my_review is None:
                    rate_label = f"Noter {int(round(self.pending_rating))}"
                elif self.ephemeral and self.my_review:
                    rate_label = "Modifier ma note"
                rate_btn = RateButton(self)
                rate_btn.label = rate_label
                page_actions: list[discord.ui.Item] = [rate_btn]
                if self.ephemeral and self.my_review:
                    page_actions.append(DeleteReviewButton(self))
                if self.ephemeral:
                    page_actions.append(WatchlistButton(self))
                    page_actions.append(AddToListButton(self))
                actions.append(discord.ui.ActionRow(*page_actions[:5]))
        self.set_layout(body, *actions, above=above, sticky_head=sticky_head)
        if not self.published_wid:
            self.add_item(discord.ui.ActionRow(FicheShareButton(self)))

    async def delete_review(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_review(self.guild, self.author_id, self.hit)
        self.my_review = None
        self.confirm_delete = False
        await self.reload_stats()
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        await self.refresh(interaction)
        await interaction.followup.send("**Critique supprimée ·** Ta note a été retirée.", ephemeral=True)

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self.reload_stats()
        if self.confirm_delete and not self.my_review:
            self.confirm_delete = False
        self._build()
        await self.push(interaction)

    async def show_selected(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        if self.selected not in self._enriched:
            await self.enrich_selected()
            self._enriched.add(self.selected)
        await self.reload_stats()
        self._build()
        await self.push(interaction)

    async def start(self, interaction: discord.Interaction, *, deferred: bool = False) -> None:
        self._interaction = interaction
        await self.prepare()
        if deferred:
            await present_ephemeral_layout(interaction, self)
            return
        await interaction.response.send_message(
            view=self, ephemeral=self.ephemeral, allowed_mentions=NO_PINGS
        )
        await self.attach(interaction)


class _ReviewPageButton(discord.ui.Button):
    def __init__(self, parent: MediaSessionView, delta: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._hub = parent
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.review_page = max(0, self._hub.review_page + self._delta)
        self._hub._build()
        await apply_view(interaction, self._hub)
