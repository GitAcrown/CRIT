"""Carnet d'un membre : profil, journal, signets et affinités."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import JOURNAL_PAGE, NO_PINGS, TYPE_CHOICES
from ..emojis import AFFINITY, BOOKMARK_LIST, HOUSE, JOURNAL, RIVAL, TWIN, XP
from ..formatting import (
    experienced_from_row,
    experienced_line,
    format_comment,
    format_grade,
    format_rating_graph,
    format_score,
    format_stars_compact,
    is_ongoing,
    journal_stats_line,
    mention_of,
    pick_rating_highlights,
    row_spoiler,
    select_emoji,
    titled,
    type_emoji,
    type_label,
    user_display,
)
from ..progress import (
    MIN_AFFINITY_OVERLAP,
    Affinity,
    level_progress,
    StarSkin,
    title_for_level,
)
from ..providers import MediaHit
from .actions import share_button_kwargs
from .base import (
    apply_view,
    bind_view_message,
    discard_ephemeral_menu,
    publish_layout_message,
    ReviewsLayout,
    sep_tight,
    sep_wide,
)
from .fiche import section_with_thumbnail
from .navigation import open_public_fiche, open_session_followup
from .session import HubPageButton, HubTabButton, ProfileShareButton

if TYPE_CHECKING:
    from ..reviews import Reviews


# ---------------------------------------------------------------------------
# Sélections partagées (profil / explorateur)
# ---------------------------------------------------------------------------

class JournalTypeSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=parent.journal_type == choice.value,
            )
            for choice in TYPE_CHOICES
        ]
        super().__init__(placeholder="Type", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.journal_type = self.values[0]
        self._hub.journal_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


SORT_CYCLE = ("recent", "best", "worst")


SORT_LABELS = {
    "recent": "Tri : Récentes",
    "best": "Tri : Meilleures",
    "worst": "Tri : Pires",
}


SORT_STYLES = {
    "recent": discord.ButtonStyle.primary,
    "best": discord.ButtonStyle.success,
    "worst": discord.ButtonStyle.danger,
}


def next_sort(current: str) -> str:
    try:
        index = SORT_CYCLE.index(current)
    except ValueError:
        return "recent"
    return SORT_CYCLE[(index + 1) % len(SORT_CYCLE)]


class JournalOngoingButton(discord.ui.Button):
    def __init__(self, parent: "ProfileView"):
        on = parent.journal_ongoing
        super().__init__(
            label="En cours",
            style=discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.journal_ongoing = not self._hub.journal_ongoing
        self._hub.journal_page = 0
        self._hub._build()
        await apply_view(interaction, self._hub)


class CycleSortButton(discord.ui.Button):
    """Bouton entre les flèches : Récentes → Meilleures → Pires."""

    def __init__(self, parent: Any, attr: str, page_attr: str):
        current = getattr(parent, attr)
        super().__init__(
            label=SORT_LABELS.get(current, SORT_LABELS["recent"]),
            style=SORT_STYLES.get(current, discord.ButtonStyle.primary),
        )
        self._hub = parent
        self._attr = attr
        self._page_attr = page_attr

    async def callback(self, interaction: discord.Interaction) -> None:
        setattr(self._hub, self._attr, next_sort(getattr(self._hub, self._attr)))
        setattr(self._hub, self._page_attr, 0)
        self._hub._build()
        await apply_view(interaction, self._hub)


class WatchlistOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description=pretty.shorten_text(f"{type_label(hit.media_type)} · {hit.year or '—'}", 95),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, _row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _row = self._items[int(self.values[0])]
        await interaction.response.defer()
        if self._hub.editable:
            await open_session_followup(
                self._hub.cog, self._hub.guild, interaction, hit, author_id=interaction.user.id,
            )
            return
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class AffinityCompareSelect(discord.ui.Select):
    def __init__(self, parent: "ProfileView"):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(parent._name(item.user_id), 95),
                value=str(item.user_id),
                description=pretty.shorten_text(
                    f"{item.percent:.0f} % · {item.overlap} œuvre{'s' if item.overlap > 1 else ''} en commun",
                    95,
                ),
            )
            for item in parent.affinities[:25]
        ]
        super().__init__(placeholder="Comparer avec…", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        other_id = int(self.values[0])
        cog = self._hub.cog
        guild = self._hub.guild
        view = AffinityCompareView(
            cog,
            guild,
            self._hub.member.id,
            other_id,
            affinity=next(a for a in self._hub.affinities if a.user_id == other_id),
            titles=self._hub.titles,
            left_skin=await cog.star_skin_for(guild, self._hub.member.id),
            right_skin=await cog.star_skin_for(guild, other_id),
        )
        view._interaction = interaction
        bind_view_message(
            view,
            await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS),
        )


class AffinityShareButton(discord.ui.Button):
    def __init__(self, parent: "AffinityCompareView"):
        super().__init__(**share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        body = self._hub._compare_items()
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body))
        message = await publish_layout_message(interaction, view)
        if message is None:
            await interaction.followup.send(
                "**Erreur ·** Impossible de publier cette comparaison.",
                ephemeral=True,
            )
            return
        self._hub.stop()
        await discard_ephemeral_menu(interaction)


class AffinityCompareView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        left_id: int,
        right_id: int,
        *,
        affinity: Affinity,
        titles: dict[int, str],
        left_skin: StarSkin | None = None,
        right_skin: StarSkin | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.left_id = left_id
        self.right_id = right_id
        self.affinity = affinity
        self.titles = titles
        self.left_skin = left_skin
        self.right_skin = right_skin
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self._build()

    def _person(self, user_id: int) -> str:
        return titled(
            mention_of(self.guild, self.cog.bot, user_id),
            self.titles.get(user_id, title_for_level(1)),
        )

    def _pair_line(self, title: str, left: float, right: float) -> str:
        return (
            f"{format_stars_compact(left, self.left_skin)} / "
            f"{format_stars_compact(right, self.right_skin)}  ·  {title}"
        )

    def _compare_items(self) -> list[discord.ui.Item]:
        _left_name, left_avatar = user_display(self.guild, self.cog.bot, self.left_id)
        body: list[discord.ui.Item] = [
            section_with_thumbnail(
                f"{self._person(self.left_id)}\n{self._person(self.right_id)}\n"
                f"-# {self.affinity.percent:.0f} % d'accord  ·  {self.affinity.overlap} œuvre(s) en commun",
                left_avatar,
            ),
        ]
        if self.affinity.agreements:
            lines = ["**D'accord**"]
            for title, left, right in self.affinity.agreements:
                lines.append(self._pair_line(title, left, right))
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        if self.affinity.disagreements:
            lines = ["**Désaccord**"]
            for title, left, right in self.affinity.disagreements:
                lines.append(self._pair_line(title, left, right))
            body.append(discord.ui.Separator())
            body.append(discord.ui.TextDisplay("\n".join(lines)))
        return body

    def _build(self) -> None:
        self.set_layout(self._compare_items())
        self.add_item(discord.ui.ActionRow(AffinityShareButton(self)))


class ProfileView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        member: discord.Member | discord.User,
        *,
        xp: int,
        review_count: int,
        average: float | None,
        twin: Affinity | None,
        rival: Affinity | None,
        titles: dict[int, str],
        journal_entries: list[tuple[MediaHit, Any]],
        watchlist_entries: list[tuple[MediaHit, Any]],
        affinities: list[Affinity],
        viewer_id: int,
        tab: str = "profil",
        star_skin: StarSkin | None = None,
        accent: discord.Colour | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.member = member
        self.accent_colour = accent
        self.xp = xp
        self.review_count = review_count
        self.average = average
        self.twin = twin
        self.rival = rival
        self.titles = titles
        self.journal_entries = journal_entries
        self.watchlist_entries = watchlist_entries
        self._highlights = pick_rating_highlights(journal_entries)
        self.affinities = sorted(affinities, key=lambda a: (-a.percent, -a.overlap))
        self.viewer_id = viewer_id
        self.editable = viewer_id == member.id
        self.tab = tab
        self.star_skin = star_skin
        self.journal_page = 0
        self.watchlist_page = 0
        self.journal_type = "all"
        self.journal_sort = "recent"
        self.journal_ongoing = False
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce profil ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _name(self, user_id: int) -> str:
        name, _avatar = user_display(self.guild, self.cog.bot, user_id)
        return name

    def _profile_header(self, extra: str = "") -> str:
        level, into, need, total = level_progress(self.xp)
        title = self.titles.get(self.member.id, title_for_level(level))
        mention = mention_of(self.guild, self.cog.bot, self.member.id)
        notes = f"**{self.review_count}** note{'s' if self.review_count != 1 else ''}"
        if self.average is not None:
            notes += f"  ·  moyenne **{format_score(self.average, average=True)}**"
        lines = [
            f"## {mention}",
            f"{XP} **{total} XP** · niveau **{level}**  ·  {notes}",
            f"-# {into}/{need} vers le niveau {level + 1}",
        ]
        stats = journal_stats_line(self.journal_entries)
        if stats:
            lines.append(stats)
        if extra:
            lines.append(extra)
        lines.append(format_grade(title))
        return "\n".join(lines)

    def _header_item(self, *, avatar: bool) -> discord.ui.Item:
        if avatar:
            url = self.member.display_avatar.url if hasattr(self.member, "display_avatar") else None
            return section_with_thumbnail(self._profile_header(), url)
        return discord.ui.TextDisplay(self._profile_header())

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            HubTabButton(self, "profil", None, emoji=HOUSE),
            HubTabButton(self, "journal", str(self.review_count), emoji=JOURNAL),
            HubTabButton(self, "signets", str(len(self.watchlist_entries)), emoji=BOOKMARK_LIST),
            HubTabButton(self, "affinites", str(len(self.affinities)), emoji=AFFINITY),
        )

    def _assemble(
        self,
        header: discord.ui.Item,
        content: list[discord.ui.Item],
        *,
        filters: list[discord.ui.ActionRow] | None = None,
        actions: list[discord.ui.ActionRow] | None = None,
    ) -> None:
        body: list[discord.ui.Item] = [header]
        for row in filters or []:
            body.append(sep_tight())
            body.append(row)
        if content:
            body.append(sep_wide())
            body.extend(content)
        self.set_layout(body, *(actions or []), above=[self._tabs_row()])

    def _filtered_journal(self) -> list[tuple[MediaHit, Any]]:
        items = self.journal_entries
        if self.journal_type != "all":
            items = [(hit, row) for hit, row in items if hit.media_type == self.journal_type]
        if self.journal_ongoing:
            items = [(hit, row) for hit, row in items if is_ongoing(experienced_from_row(row))]
        if self.journal_sort == "best":
            items = sorted(
                items,
                key=lambda item: (float(item[1]["rating"]), int(item[1]["updated_at"] or 0)),
                reverse=True,
            )
        elif self.journal_sort == "worst":
            items = sorted(
                items,
                key=lambda item: (float(item[1]["rating"]), -int(item[1]["updated_at"] or 0)),
            )
        return items

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "←", max_page)
        next_btn = HubPageButton(self, attr, 1, "→", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _highlight_block(self, label: str, hit: MediaHit, rating: float | None) -> discord.ui.Item:
        year = f"  ·  {hit.year}" if hit.year else ""
        lines = [
            f"### {label}",
            f"{type_emoji(hit.media_type)} **{hit.title}**{year}",
            f"-# {type_label(hit.media_type)}"
            + (f"  ·  {hit.subtitle}" if hit.subtitle else ""),
        ]
        if rating is not None:
            lines.append(f"{self.stars(rating)}  **{format_score(rating)}**")
        return section_with_thumbnail("\n".join(lines), hit.poster_url)

    def _profil_content(self) -> list[discord.ui.Item]:
        content: list[discord.ui.Item] = []
        for index, (label, hit, rating) in enumerate(self._highlights):
            if index:
                content.append(sep_tight())
            content.append(self._highlight_block(label, hit, rating))
        graph = format_rating_graph(self.journal_entries)
        if graph:
            if content:
                content.append(sep_wide())
            content.append(discord.ui.TextDisplay(graph))
        return content

    def _journal_nav(self, max_page: int) -> discord.ui.ActionRow:
        sort = CycleSortButton(self, "journal_sort", "journal_page")
        ongoing = JournalOngoingButton(self)
        if max_page <= 0:
            return discord.ui.ActionRow(sort, ongoing)
        prev_btn = HubPageButton(self, "journal_page", -1, "←", max_page)
        next_btn = HubPageButton(self, "journal_page", 1, "→", max_page)
        prev_btn.disabled = self.journal_page <= 0
        next_btn.disabled = self.journal_page >= max_page
        return discord.ui.ActionRow(prev_btn, sort, ongoing, next_btn)

    def _journal_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow], list[discord.ui.ActionRow]]:
        entries = self._filtered_journal()
        filters = [discord.ui.ActionRow(JournalTypeSelect(self))]
        content: list[discord.ui.Item] = []
        actions: list[discord.ui.ActionRow] = []
        if not self.journal_entries:
            content.append(discord.ui.TextDisplay("*Aucune œuvre notée pour l'instant.*"))
            return content, filters, actions
        if not entries:
            content.append(discord.ui.TextDisplay("*Aucune œuvre pour ce filtre.*"))
            actions.append(self._journal_nav(0))
            return content, filters, actions
        max_page = max(0, (len(entries) - 1) // JOURNAL_PAGE)
        self.journal_page = min(self.journal_page, max_page)
        start = self.journal_page * JOURNAL_PAGE
        page_items = entries[start:start + JOURNAL_PAGE]
        hide = self.viewer_id != self.member.id
        for index, (hit, row) in enumerate(page_items):
            if index:
                content.append(sep_tight())
            year = f" ({hit.year})" if hit.year else ""
            text = f"{self.stars(row['rating'])}  **{hit.title}**{year}\n-# {type_label(hit.media_type)}"
            shown = format_comment(
                row["comment"] or "",
                spoiler=row_spoiler(row),
                hide=hide,
                limit=180,
            )
            if shown:
                text += f"\n{shown}"
            seen = experienced_line(hit.media_type, experienced_from_row(row))
            if seen:
                text += f"\n{seen}"
            content.append(section_with_thumbnail(text, hit.poster_url))
        actions.append(self._journal_nav(max_page))
        return content, filters, actions

    def _watchlist_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        content: list[discord.ui.Item] = []
        actions: list[discord.ui.ActionRow] = []
        if not self.watchlist_entries:
            empty = (
                "*Aucun signet pour l'instant.*"
                if self.editable
                else "*Pas de signet.*"
            )
            content.append(discord.ui.TextDisplay(empty))
            return content, actions
        max_page = max(0, (len(self.watchlist_entries) - 1) // JOURNAL_PAGE)
        self.watchlist_page = min(self.watchlist_page, max_page)
        start = self.watchlist_page * JOURNAL_PAGE
        page_items = self.watchlist_entries[start:start + JOURNAL_PAGE]
        for index, (hit, row) in enumerate(page_items):
            if index:
                content.append(sep_tight())
            year = f" ({hit.year})" if hit.year else ""
            added = 0
            try:
                added = int(row["added_at"] or 0)
            except (KeyError, IndexError, TypeError):
                if isinstance(row, dict):
                    added = int(row.get("added_at") or 0)
            when = f" · ajouté <t:{added}:R>" if added else ""
            text = f"**{hit.title}**{year}\n-# {type_label(hit.media_type)}{when}"
            content.append(section_with_thumbnail(text, hit.poster_url))
        actions.append(discord.ui.ActionRow(WatchlistOpenSelect(self, page_items)))
        nav = self._page_nav("watchlist_page", max_page)
        if nav:
            actions.append(nav)
        return content, actions

    def _affinity_line(self, item: Affinity) -> str:
        common = "œuvre" if item.overlap == 1 else "œuvres"
        return (
            f"{mention_of(self.guild, self.cog.bot, item.user_id)}\n"
            f"-# {item.percent:.0f} % d'accord · {item.overlap} {common} en commun"
        )

    def _affinites_layout(self) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        content: list[discord.ui.Item] = []
        actions: list[discord.ui.ActionRow] = []
        if not self.affinities:
            content.append(discord.ui.TextDisplay(
                f"*Pas encore assez d'œuvres en commun avec quelqu'un "
                f"(minimum {MIN_AFFINITY_OVERLAP}).*"
            ))
            return content, actions
        twins = self.affinities[:3]
        rival = min(self.affinities, key=lambda a: (a.percent, -a.overlap))
        count = len(self.affinities)
        content.append(discord.ui.TextDisplay(
            f"**{TWIN} Jumeaux**\n"
            f"-# {count} affinité{'s' if count != 1 else ''} · min. {MIN_AFFINITY_OVERLAP} en commun"
        ))
        for index, twin in enumerate(twins):
            if index:
                content.append(sep_tight())
            content.append(discord.ui.TextDisplay(self._affinity_line(twin)))
        if rival.user_id not in {twin.user_id for twin in twins}:
            content.append(sep_wide())
            content.append(discord.ui.TextDisplay(f"**{RIVAL} Rival**"))
            content.append(sep_tight())
            content.append(discord.ui.TextDisplay(self._affinity_line(rival)))
        actions.append(discord.ui.ActionRow(AffinityCompareSelect(self)))
        return content, actions

    def _build(self) -> None:
        if self.tab == "journal":
            content, filters, actions = self._journal_layout()
            self._assemble(self._header_item(avatar=False), content, filters=filters, actions=actions)
        elif self.tab == "signets":
            content, actions = self._watchlist_layout()
            self._assemble(self._header_item(avatar=False), content, actions=actions)
        elif self.tab == "affinites":
            content, actions = self._affinites_layout()
            self._assemble(self._header_item(avatar=False), content, actions=actions)
        else:
            self._assemble(self._header_item(avatar=True), self._profil_content())
        self.add_item(discord.ui.ActionRow(ProfileShareButton(self)))

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self.watchlist_entries = await self.cog.load_watchlist(self.guild, self.member.id)
        self._build()
        await self.push(interaction)
