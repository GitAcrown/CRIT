"""Listes communes du serveur."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import (
    LIST_DESC_MAX,
    LIST_EDIT_MODES,
    LIST_PAGE,
    LIST_TITLE_MAX,
    MAX_SHARED_LISTS,
    logger,
)
from ..emojis import EDIT_LIST, LIST, MINUS_SMALL, PLUS_SMALL, TRASH
from ..formatting import (
    format_list_types,
    list_accepts_type,
    list_edit_label,
    list_type_select,
    list_types_label,
    mention_of,
    parse_list_types,
    select_emoji,
    select_hit_description,
    type_emoji,
    type_label,
)
from ..providers import MediaHit
from .actions import share_button_kwargs
from .base import (
    apply_view,
    discard_ephemeral_menu,
    page_nav,
    publish_layout_message,
    ReviewsLayout,
    sep_tight,
    sep_wide,
)
from .fiche import chunk_text_displays, section_with_thumbnail
from .session import OpenFicheSelect, RandomFicheButton

if TYPE_CHECKING:
    from ..reviews import Reviews


# ---------------------------------------------------------------------------
# Listes communes
# ---------------------------------------------------------------------------

class CreateSharedListModal(discord.ui.Modal, title="Nouvelle liste"):
    def __init__(self, parent: "ListsHubView"):
        super().__init__()
        self._hub = parent
        self.title_input = discord.ui.TextInput(
            label="Titre",
            placeholder="Ex. Soirée Horreur",
            max_length=LIST_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optionnel)",
            style=discord.TextStyle.paragraph,
            placeholder="Une courte phrase pour présenter la liste",
            max_length=LIST_DESC_MAX,
            required=False,
        )
        self.types_select = list_type_select("")
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(
            discord.ui.Label(
                text="Types acceptés",
                description="Vide = tous les types",
                component=self.types_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message("**Erreur ·** Donne un titre à la liste.", ephemeral=True)
            return
        await interaction.response.defer()
        owned = await self._hub.cog.count_owned_lists(self._hub.guild, interaction.user.id)
        if owned >= MAX_SHARED_LISTS:
            await self._hub.refresh(interaction, f"**Erreur ·** Tu as déjà {MAX_SHARED_LISTS} listes sur ce serveur.")
            return
        record = await self._hub.cog.create_shared_list(
            self._hub.guild,
            interaction.user.id,
            title,
            str(self.desc_input.value or "").strip(),
            allowed_types=format_list_types(self.types_select.values or []),
        )
        view = await SharedListView.create(
            self._hub.cog, self._hub.guild, record["id"], viewer_id=self._hub.viewer_id,
        )
        if view is None:
            await self._hub.refresh(interaction)
            return
        await self._hub.open(interaction, view)


class EditSharedListModal(discord.ui.Modal, title="Modifier la liste"):
    def __init__(self, parent: "SharedListView"):
        super().__init__()
        self._hub = parent
        self.title_input = discord.ui.TextInput(
            label="Titre",
            default=parent.record["title"][:LIST_TITLE_MAX],
            max_length=LIST_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Description (optionnel)",
            style=discord.TextStyle.paragraph,
            default=(parent.record["description"] or "")[:LIST_DESC_MAX] or None,
            max_length=LIST_DESC_MAX,
            required=False,
        )
        mode = parent.record["edit_mode"]
        self.mode_select = discord.ui.Select(
            placeholder="Qui peut ajouter des œuvres ?",
            min_values=1,
            max_values=1,
            required=True,
            options=[
                discord.SelectOption(
                    label="Créateur seul",
                    value="owner",
                    description="Toi uniquement",
                    default=mode == "owner",
                ),
                discord.SelectOption(
                    label="Membres choisis",
                    value="members",
                    description="Toi + les membres ci-dessous",
                    default=mode == "members",
                ),
                discord.SelectOption(
                    label="Tout le serveur",
                    value="public",
                    description="Tout le monde peut ajouter des œuvres",
                    default=mode == "public",
                ),
            ],
        )
        editors_kwargs: dict[str, Any] = {
            "placeholder": "Membres qui peuvent ajouter des œuvres…",
            "min_values": 0,
            "max_values": 25,
            "required": False,
        }
        if parent.editor_ids:
            editors_kwargs["default_values"] = [
                discord.Object(id=user_id) for user_id in parent.editor_ids[:25]
            ]
        self.editors_select = discord.ui.UserSelect(**editors_kwargs)
        self.types_select = list_type_select(parent.record.get("allowed_types"))
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(
            discord.ui.Label(
                text="Types acceptés",
                description="Vide = tous les types",
                component=self.types_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Peuvent éditer la liste",
                description="Qui peut ajouter ou retirer des œuvres",
                component=self.mode_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Membres choisis",
                description="Utilisé seulement si « Membres choisis »",
                component=self.editors_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message("**Erreur ·** Donne un titre à la liste.", ephemeral=True)
            return
        mode = (self.mode_select.values or ["owner"])[0]
        if mode not in LIST_EDIT_MODES:
            mode = "owner"
        await interaction.response.defer()
        await self._hub.cog.update_shared_list(
            self._hub.guild,
            self._hub.record["id"],
            title=title,
            description=str(self.desc_input.value or "").strip(),
            edit_mode=mode,
            allowed_types=format_list_types(self.types_select.values or []),
        )
        if mode == "members":
            ids = [
                user.id
                for user in self.editors_select.values
                if not getattr(user, "bot", False) and user.id != self._hub.record["owner_id"]
            ]
            await self._hub.cog.set_shared_list_editors(
                self._hub.guild, self._hub.record["id"], ids,
            )
        await self._hub.refresh(interaction)


class SharedListAddModal(discord.ui.Modal, title="Ajouter une œuvre"):
    def __init__(self, parent: "SharedListView"):
        super().__init__()
        self._hub = parent
        self.query_input = discord.ui.TextInput(
            label="Titre de l'œuvre",
            placeholder="Ex. Dune 2021",
            max_length=80,
            required=True,
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        query = str(self.query_input.value or "").strip()
        catalog = self._hub.cog.catalog
        if catalog is None:
            await self._hub.refresh(interaction, "**Erreur ·** Catalogue média indisponible.")
            return
        try:
            kinds = parse_list_types(self._hub.record.get("allowed_types"))
            hits = await catalog.search(query, ",".join(kinds) if kinds else "all")
            hits = [hit for hit in hits if list_accepts_type(self._hub.record.get("allowed_types"), hit.media_type)]
        except Exception:
            logger.exception("Recherche pour liste commune impossible")
            await self._hub.refresh(interaction, "**Erreur ·** Recherche impossible pour le moment.")
            return
        if not hits:
            await self._hub.refresh(interaction, f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} ».")
            return
        if len(hits) == 1:
            error = await self._hub.cog.add_shared_list_item(
                self._hub.guild, self._hub.record["id"], interaction.user.id, hits[0],
            )
            await self._hub.refresh(interaction, f"**Liste ·** {error}" if error else f"**Ajouté ·** {hits[0].title}")
            return
        await self._hub.open(interaction, SharedListPickView(self._hub, hits))


class SharedListHitSelect(discord.ui.Select):
    def __init__(self, parent: "SharedListPickView", hits: list[MediaHit]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description=select_hit_description(hit),
                emoji=select_emoji(hit.media_type),
            )
            for index, hit in enumerate(hits[:25])
        ]
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        hit = self._hub.hits[int(self.values[0])]
        error = await self._hub.liste.cog.add_shared_list_item(
            self._hub.liste.guild, self._hub.liste.record["id"], interaction.user.id, hit,
        )
        self._hub.notice = f"**Liste ·** {error}" if error else f"**Ajouté ·** {hit.title}"
        await self._hub.go_back(interaction)


class SharedListPickView(ReviewsLayout):
    """Plusieurs résultats : choisir l'œuvre à ajouter, puis retour à la liste."""

    def __init__(self, liste: "SharedListView", hits: list[MediaHit]):
        super().__init__()
        self.liste = liste
        self.hits = hits
        self._build()

    def _build(self) -> None:
        self.set_layout(
            [discord.ui.TextDisplay(
                f"## Ajouter à {pretty.shorten_text(self.liste.record['title'], 60)}\n"
                f"-# {len(self.hits)} résultat(s) — choisis l'œuvre"
            )],
            discord.ui.ActionRow(SharedListHitSelect(self, self.hits)),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.liste.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True


class ListsHubOpenSelect(discord.ui.Select):
    def __init__(self, parent: "ListsHubView", page_items: list[dict[str, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(record["title"], 95),
                value=str(record["id"]),
                description=pretty.shorten_text(
                    f"{record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''} · {list_edit_label(record['edit_mode'])}",
                    95,
                ),
            )
            for record in page_items
        ]
        super().__init__(placeholder="Ouvrir une liste", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = await SharedListView.create(
            self._hub.cog, self._hub.guild, int(self.values[0]), viewer_id=self._hub.viewer_id,
        )
        if view is None:
            await self._hub.refresh(interaction)
            return
        await self._hub.open(interaction, view)


class CreateSharedListButton(discord.ui.Button):
    def __init__(self, parent: "ListsHubView"):
        super().__init__(label="Créer une liste", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CreateSharedListModal(self._hub))


class SharedListShareButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(**share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.share_list(interaction)


class SharedListDoneButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Terminé", style=discord.ButtonStyle.primary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.menu = "browse"
        self._hub._build()
        await apply_view(interaction, self._hub)


class SharedListAddButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(PLUS_SMALL),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.send_modal(SharedListAddModal(self._hub))


class SharedListRemoveButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(MINUS_SMALL),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        self._hub.menu = "remove"
        self._hub._build()
        await apply_view(interaction, self._hub)


class SharedListEditButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(EDIT_LIST),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Seul le créateur peut modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.send_modal(EditSharedListModal(self._hub))


class SharedListDeleteButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(style=discord.ButtonStyle.danger, emoji=discord.PartialEmoji.from_str(TRASH))
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.is_owner(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Seul le créateur peut supprimer cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        self._hub.menu = "confirm_delete"
        self._hub._build()
        await apply_view(interaction, self._hub)


class SharedListDeleteConfirmButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.danger)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.delete_list(interaction)


class SharedListDeleteCancelButton(discord.ui.Button):
    def __init__(self, parent: "SharedListView"):
        super().__init__(label="Annuler", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.menu = "browse"
        self._hub._build()
        await apply_view(interaction, self._hub)


class SharedListRemoveSelect(discord.ui.Select):
    def __init__(self, parent: "SharedListView", page_items: list[tuple[MediaHit, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(hit.title, 95) or "Sans titre",
                value=str(index),
                description="Retirer de la liste",
                emoji=select_emoji(hit.media_type),
            )
            for index, (hit, _row) in enumerate(page_items)
        ]
        super().__init__(placeholder="Retirer une œuvre", options=options)
        self._hub = parent
        self._items = page_items

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._hub.can_edit(interaction.user.id):
            await interaction.response.send_message(
                "**Action impossible ·** Tu ne peux pas modifier cette liste.",
                ephemeral=True,
                delete_after=10,
            )
            return
        await interaction.response.defer()
        hit, _row = self._items[int(self.values[0])]
        media_id = await self._hub.cog.lookup_media_id(self._hub.guild, hit)
        if media_id:
            await self._hub.cog.remove_shared_list_item(self._hub.guild, self._hub.record["id"], media_id)
        await self._hub.refresh(interaction)


class ListsHubView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        lists: list[dict[str, Any]],
        viewer_id: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.lists = lists
        self.viewer_id = viewer_id
        self.page = 0
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, *, viewer_id: int) -> "ListsHubView":
        return cls(cog, guild, lists=await cog.load_shared_lists(guild), viewer_id=viewer_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _build(self) -> None:
        body: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"## Listes\n-# {len(self.lists)} liste{'s' if len(self.lists) != 1 else ''} sur ce serveur"
            ),
            sep_wide(),
        ]
        rows: list[discord.ui.ActionRow] = []
        if not self.lists:
            body.append(discord.ui.TextDisplay(
                "*Aucune liste pour l'instant. Crée-en une avec un titre et une courte description.*"
            ))
            rows.append(discord.ui.ActionRow(CreateSharedListButton(self)))
            self.set_layout(body, *rows)
            return
        max_page = max(0, (len(self.lists) - 1) // LIST_PAGE)
        self.page = min(self.page, max_page)
        start = self.page * LIST_PAGE
        page_items = self.lists[start:start + LIST_PAGE]
        for index, record in enumerate(page_items):
            if index:
                body.append(sep_tight())
            desc = pretty.shorten_text(record["description"], 160) if record["description"] else "*Pas de description.*"
            n = record["item_count"]
            text = (
                f"### {LIST} {record['title']}\n{desc}\n"
                f"-# {mention_of(self.guild, self.cog.bot, record['owner_id'])} · "
                f"{n} œuvre{'s' if n != 1 else ''} · {list_edit_label(record['edit_mode'])}"
                + (
                    f" · {list_types_label(record.get('allowed_types'))}"
                    if parse_list_types(record.get("allowed_types")) else ""
                )
            )
            body.append(discord.ui.TextDisplay(text))
        rows.append(discord.ui.ActionRow(ListsHubOpenSelect(self, page_items)))
        rows.append(page_nav(self, "page", max_page, CreateSharedListButton(self)))
        self.set_layout(body, *rows)

    async def refresh(self, interaction: discord.Interaction | None = None, notice: str | None = None) -> None:
        self.lists = await self.cog.load_shared_lists(self.guild)
        self.notice = notice or self.notice
        self._build()
        await self.push(interaction)

    async def on_return(self, interaction: discord.Interaction) -> None:
        await self.refresh(interaction)


class SharedListView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        record: dict[str, Any],
        items: list[tuple[MediaHit, Any]],
        editor_ids: list[int],
        viewer_id: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.record = record
        self.items = items
        self.editor_ids = editor_ids
        self.viewer_id = viewer_id
        self.item_page = 0
        self.menu = "browse"
        self._interaction: discord.Interaction | None = None
        self._build()

    @classmethod
    async def create(
        cls, cog: "Reviews", guild: discord.Guild, list_id: int, *, viewer_id: int
    ) -> "SharedListView | None":
        record = await cog.get_shared_list(guild, list_id)
        if record is None:
            return None
        return cls(
            cog,
            guild,
            record=record,
            items=await cog.load_shared_list_items(guild, list_id),
            editor_ids=await cog.load_shared_list_editors(guild, list_id),
            viewer_id=viewer_id,
        )

    def is_owner(self, user_id: int) -> bool:
        return user_id == int(self.record["owner_id"])

    def can_edit(self, user_id: int) -> bool:
        return self.cog.can_edit_shared_list(self.record, user_id, self.editor_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _nav_row(self, max_page: int) -> discord.ui.ActionRow | None:
        middle: list[discord.ui.Item] = []
        if self.menu == "remove":
            middle.append(SharedListDoneButton(self))
        return page_nav(self, "item_page", max_page, *middle)

    def _header(self) -> str:
        desc = pretty.shorten_text(self.record["description"], 220) if self.record["description"] else "*Pas de description.*"
        n = len(self.items)
        lines = [
            f"## {LIST} {self.record['title']}",
            desc,
            f"-# {mention_of(self.guild, self.cog.bot, self.record['owner_id'])} · "
            f"{n} œuvre{'s' if n != 1 else ''} · édition : {list_edit_label(self.record['edit_mode']).lower()}"
            + (
                f" · {list_types_label(self.record.get('allowed_types'))}"
                if parse_list_types(self.record.get("allowed_types")) else ""
            ),
        ]
        if self.record["edit_mode"] == "members" and self.editor_ids:
            shown = [mention_of(self.guild, self.cog.bot, user_id) for user_id in self.editor_ids[:8]]
            extra = len(self.editor_ids) - len(shown)
            lines.append(f"-# Éditeurs · {', '.join(shown)}" + (f" et {extra} autre(s)" if extra else ""))
        elif self.record["edit_mode"] == "members":
            lines.append("-# Aucun membre choisi pour l'instant.")
        return "\n".join(lines)

    def _share_item_line(self, index: int, hit: MediaHit) -> str:
        year = f" ({hit.year})" if hit.year else ""
        return f"{index}. {type_emoji(hit.media_type)} **{hit.title}**{year}"

    def _share_layout(self) -> list[discord.ui.Item]:
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._header()), sep_wide()]
        if not self.items:
            body.append(discord.ui.TextDisplay("*Cette liste est vide.*"))
            return body
        lines = [self._share_item_line(index, hit) for index, (hit, _row) in enumerate(self.items, start=1)]
        body.extend(chunk_text_displays(lines))
        return body

    def _build(self) -> None:
        if self.menu == "remove" and (not self.can_edit(self.viewer_id) or not self.items):
            self.menu = "browse"
        if self.menu == "confirm_delete" and not self.is_owner(self.viewer_id):
            self.menu = "browse"
        body: list[discord.ui.Item] = [discord.ui.TextDisplay(self._header()), sep_wide()]
        rows: list[discord.ui.ActionRow] = []
        max_page = 0
        page_items: list[tuple[MediaHit, Any]] = []
        if not self.items:
            body.append(discord.ui.TextDisplay("*Cette liste est vide.*"))
        else:
            max_page = max(0, (len(self.items) - 1) // LIST_PAGE)
            self.item_page = min(self.item_page, max_page)
            start = self.item_page * LIST_PAGE
            page_items = self.items[start:start + LIST_PAGE]
            for index, (hit, row) in enumerate(page_items):
                if index:
                    body.append(sep_tight())
                year = f" ({hit.year})" if hit.year else ""
                added_by = 0
                added_at = 0
                try:
                    added_by = int(row["added_by"] or 0)
                    added_at = int(row["added_at"] or 0)
                except (KeyError, IndexError, TypeError):
                    if isinstance(row, dict):
                        added_by = int(row.get("added_by") or 0)
                        added_at = int(row.get("added_at") or 0)
                who = mention_of(self.guild, self.cog.bot, added_by) if added_by else "quelqu'un"
                when = f" · <t:{added_at}:R>" if added_at else ""
                text = f"**{hit.title}**{year}\n-# {type_label(hit.media_type)} · ajouté par {who}{when}"
                body.append(section_with_thumbnail(text, hit.poster_url))
        if self.menu == "confirm_delete":
            title = pretty.shorten_text(str(self.record["title"]), 80) or "cette liste"
            body.append(discord.ui.TextDisplay(
                f"**Supprimer {title} ?**\n"
                "-# La liste et les œuvres qu'elle contient seront retirées."
            ))
            rows.append(discord.ui.ActionRow(
                SharedListDeleteConfirmButton(self),
                SharedListDeleteCancelButton(self),
            ))
        elif self.menu == "remove" and page_items:
            rows.append(discord.ui.ActionRow(SharedListRemoveSelect(self, page_items)))
        else:
            if page_items:
                rows.append(discord.ui.ActionRow(OpenFicheSelect(self, [hit for hit, _row in page_items])))
            actions: list[discord.ui.Item] = [RandomFicheButton(self, [hit for hit, _row in self.items])]
            if self.can_edit(self.viewer_id):
                actions.append(SharedListAddButton(self))
                if self.items:
                    actions.append(SharedListRemoveButton(self))
            if self.is_owner(self.viewer_id):
                actions.append(SharedListEditButton(self))
                actions.append(SharedListDeleteButton(self))
            rows.append(discord.ui.ActionRow(*actions[:5]))
        rows.append(self._nav_row(max_page))
        self.set_layout(body, *rows)
        self.add_item(discord.ui.ActionRow(SharedListShareButton(self)))

    async def share_list(self, interaction: discord.Interaction) -> None:
        body = self._share_layout()
        view = discord.ui.LayoutView(timeout=None)
        if body:
            view.add_item(discord.ui.Container(*body))
        message = await publish_layout_message(interaction, view)
        if message is None:
            await interaction.followup.send("**Erreur ·** Impossible de publier cette liste.", ephemeral=True)
            return
        if self.parent is None:
            self.stop()
            await discard_ephemeral_menu(interaction)
            return
        self.notice = f"**Partagé ·** {message.jump_url}"
        self._build()
        await self.push(interaction)

    async def delete_list(self, interaction: discord.Interaction) -> None:
        if not self.is_owner(interaction.user.id):
            await interaction.followup.send(
                "**Action impossible ·** Seul le créateur peut supprimer cette liste.",
                ephemeral=True,
            )
            return
        await self.cog.delete_shared_list(self.guild, self.record["id"])
        self.notice = f"**Liste supprimée ·** {self.record['title']}"
        await self.go_back(interaction)

    async def refresh(self, interaction: discord.Interaction | None = None, notice: str | None = None) -> None:
        record = await self.cog.get_shared_list(self.guild, self.record["id"])
        if record is None:
            self.notice = "**Listes ·** Cette liste n'existe plus."
            if interaction is not None:
                await self.go_back(interaction)
            return
        self.record = record
        self.items = await self.cog.load_shared_list_items(self.guild, record["id"])
        self.editor_ids = await self.cog.load_shared_list_editors(self.guild, record["id"])
        self.notice = notice or self.notice
        self._build()
        await self.push(interaction)

    async def on_return(self, interaction: discord.Interaction) -> None:
        await self.refresh(interaction)
