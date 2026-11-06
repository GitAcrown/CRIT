"""Exploration du serveur : récentes, catalogue, top et recommandations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from utils import pretty

from ..constants import CATALOG_PAGE, RATING_MAX
from ..formatting import format_stars_select, select_emoji, type_label
from ..progress import StarSkin
from ..providers import MediaHit
from .base import labeled_tabs, ReviewsLayout, sep_tight
from .navigation import open_public_fiche
from .profile import CycleSortButton
from .session import HubPageButton, HubTabButton

if TYPE_CHECKING:
    from ..reviews import Reviews


class CatalogOpenSelect(discord.ui.Select):
    def __init__(
        self,
        parent: "ServerHubView",
        page_items: list[tuple[MediaHit, float, int]],
        *,
        count_label: str = "note",
    ):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95),
                value=str(index),
                description=pretty.shorten_text(
                    f"{type_label(hit.media_type)} · {format_stars_select(avg)}/{RATING_MAX} · "
                    f"{count} {count_label}{'s' if count > 1 else ''}",
                    95,
                ),
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, avg, count) in enumerate(page_items)
        ]
        super().__init__(placeholder="Ouvrir une fiche", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        hit, _avg, _count = self._items[int(self.values[0])]
        await interaction.response.defer()
        await open_public_fiche(self._hub.cog, self._hub.guild, interaction, hit)


class ServerHubView(ReviewsLayout):
    """Catalogue du serveur et recommandations, dans une seule vue à onglets."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        catalog: list[tuple[MediaHit, float, int, int]],
        for_you: list[tuple[MediaHit, float, int]],
        for_you_hint: str,
        catalog_subtitle: str,
        star_skin: StarSkin | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.catalog = catalog
        self.for_you = for_you
        self.for_you_hint = for_you_hint
        self.catalog_subtitle = catalog_subtitle
        self.tab = "catalogue"
        self.catalog_sort = "recent"
        self.star_skin = star_skin
        self.catalog_page = 0
        self.for_you_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    def _tabs_row(self) -> discord.ui.ActionRow:
        catalogue, pour_toi = labeled_tabs(
            f"Catalogue ({len(self.catalog)})",
            "Pour toi",
        )
        return discord.ui.ActionRow(
            HubTabButton(self, "catalogue", catalogue),
            HubTabButton(self, "pour_toi", pour_toi),
        )

    def _page_nav(self, attr: str, max_page: int) -> discord.ui.ActionRow | None:
        if max_page <= 0:
            return None
        page = getattr(self, attr)
        prev_btn = HubPageButton(self, attr, -1, "←", max_page)
        next_btn = HubPageButton(self, attr, 1, "→", max_page)
        prev_btn.disabled = page <= 0
        next_btn.disabled = page >= max_page
        return discord.ui.ActionRow(prev_btn, next_btn)

    def _sorted_catalog(self) -> list[tuple[MediaHit, float, int]]:
        if self.catalog_sort == "best":
            ordered = sorted(self.catalog, key=lambda item: (item[1], item[2], item[3]), reverse=True)
        elif self.catalog_sort == "worst":
            ordered = sorted(self.catalog, key=lambda item: (item[1], -item[2], -item[3]))
        else:
            ordered = sorted(self.catalog, key=lambda item: (item[3], item[1]), reverse=True)
        return [(hit, avg, count) for hit, avg, count, _latest in ordered]

    def _catalog_nav(self, max_page: int) -> discord.ui.ActionRow:
        sort = CycleSortButton(self, "catalog_sort", "catalog_page")
        if max_page <= 0:
            return discord.ui.ActionRow(sort)
        prev_btn = HubPageButton(self, "catalog_page", -1, "←", max_page)
        next_btn = HubPageButton(self, "catalog_page", 1, "→", max_page)
        prev_btn.disabled = self.catalog_page <= 0
        next_btn.disabled = self.catalog_page >= max_page
        return discord.ui.ActionRow(prev_btn, sort, next_btn)

    def _ranked_layout(
        self,
        *,
        title: str,
        subtitle: str,
        items: list[tuple[MediaHit, float, int]],
        page_attr: str,
        extra_row: discord.ui.ActionRow | None = None,
        count_label: str = "note",
        empty: str = "*Aucune œuvre ne correspond à cette recherche.*",
    ) -> tuple[list[discord.ui.Item], list[discord.ui.ActionRow]]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(f"## {title}\n-# {subtitle}")]
        if extra_row:
            body.append(sep_tight())
            body.append(extra_row)
        rows: list[discord.ui.ActionRow] = []
        if not items:
            body.append(discord.ui.TextDisplay(empty))
            return body, rows
        max_page = max(0, (len(items) - 1) // CATALOG_PAGE)
        page = min(getattr(self, page_attr), max_page)
        setattr(self, page_attr, page)
        start = page * CATALOG_PAGE
        page_items = items[start:start + CATALOG_PAGE]
        lines = []
        for index, (hit, avg, count) in enumerate(page_items, start=start + 1):
            year = f" ({hit.year})" if hit.year else ""
            lines.append(
                f"**{index}.** {self.stars_compact(avg)}  **{hit.title}**{year}  ·  "
                f"{type_label(hit.media_type)}  ·  {count} {count_label}{'s' if count > 1 else ''}"
            )
        body.append(discord.ui.TextDisplay("\n".join(lines)))
        rows.append(discord.ui.ActionRow(CatalogOpenSelect(self, page_items, count_label=count_label)))
        if page_attr == "catalog_page":
            rows.append(self._catalog_nav(max_page))
        else:
            nav = self._page_nav(page_attr, max_page)
            if nav:
                rows.append(nav)
        return body, rows

    def _catalog_heading(self) -> str:
        hints = {
            "recent": "Les plus récemment notées",
            "best": "Les mieux notées",
            "worst": "Les moins bien notées",
        }
        hint = hints.get(self.catalog_sort, hints["recent"])
        extra = self.catalog_subtitle
        if extra and extra != "Toutes les œuvres notées":
            return f"{hint}  ·  {extra}"
        return hint

    def _build(self) -> None:
        if self.tab == "pour_toi":
            body, rows = self._ranked_layout(
                title="Pour toi",
                subtitle="Pas encore notées, aimées par des membres proches de tes goûts",
                items=self.for_you,
                page_attr="for_you_page",
                count_label="personne",
                empty=self.for_you_hint,
            )
        else:
            body, rows = self._ranked_layout(
                title="Catalogue du serveur",
                subtitle=self._catalog_heading(),
                items=self._sorted_catalog(),
                page_attr="catalog_page",
            )
        self.set_layout(body, *rows, above=[self._tabs_row()])

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        self._build()
        await self.push(interaction)
