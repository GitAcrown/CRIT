"""Fiche privée d'une œuvre (`FicheView`) : la même partout.

Recherche, carnet, explore, listes, lives, lien collé ou bouton « Ouvrir » d'une fiche
publique ouvrent tous ce menu éphémère, réservé à la personne qui l'a ouvert. Tout s'y
fait en place : noter, signet, liste, saison, critiques du serveur.
"""

from __future__ import annotations

import random
from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import NO_PINGS, REVIEWS_PAGE, logger
from ..formatting import (
    experienced_from_row,
    experienced_line,
    format_comment,
    format_score,
    list_accepts_type,
    mention_of,
    row_spoiler,
    season_media,
    select_emoji,
    select_hit_description,
    split_tv_scope,
    tv_season_list,
    user_display,
)
from ..prefs import UserPrefs
from ..providers import MediaHit
from .actions import (
    AddToListButton,
    CancelListPickButton,
    DeleteReviewButton,
    DeleteReviewCancelButton,
    DeleteReviewConfirmButton,
    FicheListSelect,
    FicheShareButton,
    MediaSelect,
    RateButton,
    review_saved_lines,
    SeasonScopeSelect,
    WatchlistButton,
)
from .base import (
    PageButton,
    present_ephemeral_layout,
    ReviewsLayout,
    sep_tight,
    TabButton,
)
from .fiche import (
    also_liked_line,
    append_fiche_sections,
    critiques_summary_line,
    fiche_intro,
    fiche_plan_payload,
    section_with_thumbnail,
    stream_live_items,
    sync_published_fiche,
)

if TYPE_CHECKING:
    from ..reviews import Reviews


class FicheView(ReviewsLayout):
    """Fiche · Critiques d'une œuvre, avec ta note et tes actions."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        hits: list[MediaHit],
        *,
        author_id: int,
        pending_rating: float | None = None,
        pending_comment: str = "",
        selected: int = 0,
        tab: str = "fiche",
        published_wid: str | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.hits = hits
        self.author_id = author_id
        self.pending_rating = pending_rating
        self.pending_comment = pending_comment
        self.selected = selected
        self.season: int | None = None
        self.tab = tab
        self.review_page = 0
        self.avg: float | None = None
        self.count = 0
        self.my_review: dict | None = None
        self.on_watchlist = False
        self.reviews: list[Any] = []
        self.social_line = ""
        self.stream_channels: list[int] = []
        self.stream_plans: list[dict] = []
        # Fiche publique d'où l'on vient : resynchronisée après une note.
        self.published_wid = published_wid
        # Noté depuis le bouton Noter d'une fiche publique : ce menu n'est pas affiché.
        self.from_published_modal = False
        self.confirm_delete = False
        self.picking_list = False
        self.writable_lists: list[dict[str, Any]] = []
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
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "**Action impossible ·** Cette fiche ne s'affiche que pour la personne qui l'a ouverte.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Données
    # ------------------------------------------------------------------

    async def prepare(self) -> None:
        await self.load_member()
        await self.enrich_selected()
        self._enriched.add(self.selected)
        await self.reload_stats()
        self._build()

    async def load_member(self) -> None:
        await self.cog.get_comment_max(self.guild)
        self.prefs = await self.cog.get_user_prefs(self.guild, self.author_id)
        self.star_skin = await self.cog.star_skin_for(self.guild, self.author_id)

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

    async def load_my_review(self) -> int | None:
        media_id = await self.cog.lookup_media_id(self.guild, self.hit)
        self.my_review = await self.cog.get_review(self.guild, self.author_id, media_id) if media_id else None
        self.on_watchlist = bool(media_id) and await self.cog.is_on_watchlist(self.guild, self.author_id, media_id)
        return media_id

    async def reload_stats(self) -> None:
        media_id = await self.load_my_review()
        self.avg, self.count = await self.cog.media_stats(self.guild, media_id) if media_id else (None, 0)
        self.reviews = await self.cog.list_reviews(self.guild, media_id) if media_id else []
        liked = await self.cog.also_liked(self.guild, media_id, viewer_id=self.author_id) if media_id else []
        self.social_line = also_liked_line(self.hit, liked)
        self.stream_channels = await self.cog.stream_channels_for_hit(self.guild, self.show_hit)
        self.stream_plans = fiche_plan_payload(await self.cog.plans_for_hit(self.guild, self.show_hit))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

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
        self.pending_rating = None
        self.confirm_delete = False
        self.picking_list = False
        self.tab = "fiche"
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        collections = await self.cog.grant_event_collections(self.guild, interaction.user.id, self.hit)
        saved = "\n".join(review_saved_lines(
            self.hit, rating, created, award, skin=self.star_skin, collections=collections,
        ))
        if self.from_published_modal:
            await interaction.followup.send(saved, ephemeral=True)
        else:
            self.notice = saved
            await self.refresh(interaction)
        await self.cog.announce_review(
            self.guild, interaction.user, self.hit, rating, comment,
            updated=not created, experienced_at=experienced_at, spoiler=spoiler,
        )

    async def delete_review(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_review(self.guild, self.author_id, self.hit)
        self.confirm_delete = False
        if self.published_wid:
            await sync_published_fiche(self.cog, self.guild, self.published_wid, self.hit)
        self.notice = "**Critique supprimée ·** Ta note a été retirée."
        await self.refresh(interaction)

    async def toggle_watchlist(self, interaction: discord.Interaction) -> None:
        if self.on_watchlist:
            media_id = await self.cog.lookup_media_id(self.guild, self.hit)
            if media_id:
                await self.cog.remove_watchlist(self.guild, self.author_id, media_id)
        else:
            await self.cog.add_watchlist(self.guild, self.author_id, self.hit)
        await self.load_my_review()
        self._build()
        await self.push(interaction)

    async def start_list_pick(self, interaction: discord.Interaction) -> None:
        lists = await self.cog.writable_shared_lists(self.guild, self.author_id)
        self.writable_lists = [
            record for record in lists
            if list_accepts_type(record.get("allowed_types"), self.hit.media_type)
        ][:25]
        if self.writable_lists:
            self.picking_list = True
        else:
            self.notice = "-# Aucune liste que tu peux modifier n'accepte ce type. Crée-en une avec `/listes`."
        self._build()
        await self.push(interaction)

    async def add_to_list(self, interaction: discord.Interaction, list_id: int) -> None:
        record = next((item for item in self.writable_lists if int(item["id"]) == list_id), None)
        error = await self.cog.add_shared_list_item(self.guild, list_id, self.author_id, self.hit)
        title = pretty.shorten_text(record["title"], 80) if record else "la liste"
        self.notice = f"**Liste ·** {error}" if error else f"**Ajouté ·** {self.hit.title} dans **{title}**."
        self.picking_list = False
        self._build()
        await self.push(interaction)

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
        await interaction.response.send_message(view=self, ephemeral=True, allowed_mentions=NO_PINGS)
        await self.attach(interaction)

    # ------------------------------------------------------------------
    # Affichage
    # ------------------------------------------------------------------

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            TabButton(self, "fiche", "Fiche"),
            TabButton(self, "critiques", f"Critiques ({self.count})"),
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
            rows.append(discord.ui.ActionRow(SeasonScopeSelect(self, chunk, include_show=index == 0)))
            if len(rows) == 2:
                break
        return rows

    def _missing_hint(self) -> str:
        if len(self.hits) <= 1 or any(item.source == "tmdb" for item in self.hits):
            return ""
        if not any(item.source == "spotify" for item in self.hits):
            return ""
        if self.cog.catalog is not None and not self.cog.catalog.tmdb.available:
            return "-# Films et séries absents · clé TMDB manquante."
        return "-# Aucun film ou série trouvé — précise le type si besoin."

    def _critiques_body(self, body: list[discord.ui.Item], actions: list[discord.ui.Item]) -> None:
        hit = self.hit
        total_pages = max(1, (len(self.reviews) + REVIEWS_PAGE - 1) // REVIEWS_PAGE)
        max_page = max(0, total_pages - 1)
        self.review_page = min(self.review_page, max_page)
        body.append(discord.ui.TextDisplay(critiques_summary_line(
            hit, count=self.count, avg=self.avg, page=self.review_page, total_pages=total_pages,
            skin=self.star_skin,
        )))
        body.append(sep_tight())
        if not self.reviews:
            body.append(discord.ui.TextDisplay("*Pas encore de critique sur ce serveur.*"))
            return
        start = self.review_page * REVIEWS_PAGE
        for index, row in enumerate(self.reviews[start:start + REVIEWS_PAGE]):
            if index:
                body.append(sep_tight())
            user_id = int(row["user_id"])
            _name, avatar = user_display(self.guild, self.cog.bot, user_id)
            text = (
                f"{mention_of(self.guild, self.cog.bot, user_id)}\n"
                f"{self.stars(row['rating'])}  **{format_score(row['rating'])}** · <t:{row['updated_at']}:R>"
            )
            shown = format_comment(row["comment"] or "", spoiler=row_spoiler(row), hide=True, limit=220)
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            body.append(section_with_thumbnail(text, avatar))
        if max_page > 0:
            prev_btn = PageButton(self, "review_page", -1, "←", max_page)
            next_btn = PageButton(self, "review_page", 1, "→", max_page)
            actions.append(discord.ui.ActionRow(prev_btn, next_btn))

    def _action_rows(self) -> list[discord.ui.Item]:
        hit = self.hit
        if self.confirm_delete and not self.my_review:
            self.confirm_delete = False
        if self.confirm_delete:
            title = pretty.shorten_text(hit.title, 80) or "cette œuvre"
            return [
                discord.ui.TextDisplay(f"**Supprimer ta note de {title} ?**\n-# Ta note et ton commentaire seront retirés."),
                discord.ui.ActionRow(DeleteReviewConfirmButton(self), DeleteReviewCancelButton(self)),
            ]
        if self.picking_list:
            return [
                discord.ui.ActionRow(FicheListSelect(self, self.writable_lists)),
                discord.ui.ActionRow(CancelListPickButton(self)),
            ]
        rate_btn = RateButton(self)
        if self.pending_rating is not None and self.my_review is None:
            rate_btn.label = f"Noter {int(round(self.pending_rating))}"
        elif self.my_review:
            rate_btn.label = "Modifier ma note"
        row: list[discord.ui.Item] = [rate_btn]
        if self.my_review:
            row.append(DeleteReviewButton(self))
        row.append(WatchlistButton(self))
        row.append(AddToListButton(self))
        if not self.published_wid:  # déjà publique : pas de second partage
            row.append(FicheShareButton(self))
        return [discord.ui.ActionRow(*row)]

    def _build(self) -> None:
        hit = self.hit
        title_row = discord.ui.ActionRow(MediaSelect(self, self.hits, self.selected)) if len(self.hits) > 1 else None
        body: list[discord.ui.Item] = []
        actions: list[discord.ui.Item] = []
        hint = self._missing_hint()
        if hint:
            body.append(discord.ui.TextDisplay(hint))
        body.extend(stream_live_items(self.stream_channels, self.stream_plans))
        if self.tab == "critiques":
            body.extend(fiche_intro(hit, backdrop=False, replace_title=title_row))
            sticky_head = len(body)
            self._critiques_body(body, actions)
        else:
            body.extend(fiche_intro(hit, replace_title=title_row))
            sticky_head = len(body)
            append_fiche_sections(
                body,
                hit,
                avg=self.avg,
                count=self.count,
                my_review=self.my_review,
                social_line=self.social_line,
                guild_name=self.guild.name,
                skin=self.star_skin,
            )
        actions.extend(self._season_rows())
        actions.extend(self._action_rows())
        self.set_layout(body, *actions, above=[self._tabs_row()], sticky_head=sticky_head)


async def open_fiche(
    parent: ReviewsLayout,
    interaction: discord.Interaction,
    hit: MediaHit,
    *,
    tab: str = "fiche",
) -> None:
    """Ouvre la fiche privée d'une œuvre à la place du menu `parent`, avec Retour."""
    if not interaction.response.is_done():
        await interaction.response.defer()
    view = FicheView(parent.cog, parent.guild, [hit], author_id=interaction.user.id, tab=tab)  # type: ignore[attr-defined]
    await view.prepare()
    await parent.open(interaction, view)


class OpenFicheSelect(discord.ui.Select):
    """« Ouvrir une fiche » dans une liste d'œuvres : la fiche remplace le menu, avec Retour."""

    def __init__(
        self,
        parent: ReviewsLayout,
        hits: list[MediaHit],
        *,
        descriptions: list[str] | None = None,
        placeholder: str = "Ouvrir une fiche",
    ):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description=pretty.shorten_text(descriptions[index], 95) if descriptions else select_hit_description(hit),
                emoji=select_emoji(hit.media_type),
            )
            for index, hit in enumerate(hits[:25])
        ]
        super().__init__(placeholder=placeholder, options=options)
        self._hub = parent
        self._hits = hits

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_fiche(self._hub, interaction, self._hits[int(self.values[0])])


class RandomFicheButton(discord.ui.Button):
    """Ouvre une œuvre tirée au hasard dans `pool`."""

    def __init__(self, parent: ReviewsLayout, pool: list[MediaHit]):
        super().__init__(label="Au hasard", emoji="🎲", style=discord.ButtonStyle.secondary, disabled=not pool)
        self._hub = parent
        self._pool = pool

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_fiche(self._hub, interaction, random.choice(self._pool))


class CritiquesView(FicheView):
    """Critiques du serveur sur une œuvre, sans la fiche autour (bouton public Critiques)."""

    def __init__(self, cog: "Reviews", guild: discord.Guild, hit: MediaHit, *, author_id: int):
        super().__init__(cog, guild, [hit], author_id=author_id, tab="critiques")

    async def prepare(self) -> None:
        # La fiche est déjà dans le salon : pas d'enrichissement, juste les avis.
        await self.load_member()
        await self.reload_stats()
        self._build()

    def _build(self) -> None:
        hit = self.hit
        year = f"  ·  {hit.year}" if hit.year else ""
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(f"### Critiques · {hit.title}{year}")]
        actions: list[discord.ui.Item] = []
        self._critiques_body(body, actions)
        self.set_layout(body, *actions)
