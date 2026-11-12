"""Exploration du serveur : récentes, catalogue, top et recommandations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord


from ..constants import CATALOG_PAGE, RATING_MAX
from ..formatting import format_stars_select, type_label
from ..progress import StarSkin
from ..providers import MediaHit
from .base import page_nav, ReviewsLayout, sep_tight, TabButton
from .profile import SortFilterButton
from .session import OpenFicheSelect, RandomFicheButton

if TYPE_CHECKING:
    from ..reviews import Reviews


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
        self.catalog_type = "all"  # filtre par type, réglé dans le formulaire de l'entonnoir
        self.star_skin = star_skin
        self.catalog_page = 0
        self.for_you_page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            TabButton(self, "catalogue", f"Catalogue ({len(self.catalog)})"),
            TabButton(self, "pour_toi", "Pour toi"),
        )

    def _sorted_catalog(self) -> list[tuple[MediaHit, float, int]]:
        if self.catalog_sort == "best":
            ordered = sorted(self.catalog, key=lambda item: (item[1], item[2], item[3]), reverse=True)
        elif self.catalog_sort == "worst":
            ordered = sorted(self.catalog, key=lambda item: (item[1], -item[2], -item[3]))
        else:
            ordered = sorted(self.catalog, key=lambda item: (item[3], item[1]), reverse=True)
        if self.catalog_type != "all":
            ordered = [item for item in ordered if item[0].media_type == self.catalog_type]
        return [(hit, avg, count) for hit, avg, count, _latest in ordered]

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
        funnel = SortFilterButton(
            self, title="Trier le catalogue", sort_attr="catalog_sort", type_attr="catalog_type",
            page_attr="catalog_page",
        ) if page_attr == "catalog_page" else None
        if not items:
            body.append(discord.ui.TextDisplay(empty))
            if funnel is not None:  # filtre trop strict : l'entonnoir reste pour le retirer
                rows.append(discord.ui.ActionRow(funnel))
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
        rows.append(discord.ui.ActionRow(OpenFicheSelect(
            self,
            [hit for hit, _avg, _count in page_items],
            descriptions=[
                f"{type_label(hit.media_type)} · {format_stars_select(avg)}/{RATING_MAX} · "
                f"{count} {count_label}{'s' if count > 1 else ''}"
                for hit, avg, count in page_items
            ],
        )))
        middle: list[discord.ui.Item] = []
        if funnel is not None:
            middle.append(funnel)
        middle.append(RandomFicheButton(self, [hit for hit, _avg, _count in items]))
        rows.append(page_nav(self, page_attr, max_page, *middle))
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
