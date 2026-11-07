"""Actions d'une fiche : noter, signet, listes, saison, partage, suppression."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import RATING_MAX
from ..emojis import ADD_LIST, BOOKMARK_OFF, BOOKMARK_ON, SHARE, TRASH, XP
from ..formatting import (
    ONGOING,
    allows_ongoing,
    experienced_to_input,
    format_score,
    format_stars,
    is_ongoing,
    ongoing_label,
    parse_experienced_date,
    parse_rating,
    season_label,
    select_emoji,
    select_hit_description,
)
from ..prefs import note_experienced_default, note_spoiler_default, UserPrefs
from ..progress import skins_unlocked_between, StarSkin, title_for_level, XpAward
from ..providers import MediaHit
from .base import apply_view
from .fiche import send_published_fiche

if TYPE_CHECKING:
    from .session import FicheView


# ---------------------------------------------------------------------------
# Modal de notation
# ---------------------------------------------------------------------------

def review_saved_lines(
    hit: MediaHit,
    rating: float,
    created: bool,
    award: XpAward,
    *,
    skin: StarSkin | str | None = None,
    collections: list[StarSkin] | None = None,
) -> list[str]:
    verb = "enregistrée" if created else "mise à jour"
    parts = [f"**Critique {verb} ·** {format_stars(rating, skin)}  **{format_score(rating)}** — {hit.title}."]
    if award.gained:
        parts.append(f"{XP} +{award.gained} · niveau {award.level}")
        if award.capped:
            parts.append("(plafond quotidien atteint)")
    elif award.capped:
        parts.append("Plafond d'XP quotidien atteint.")
    if award.leveled_up:
        new_title = title_for_level(award.level)
        old_title = title_for_level(award.previous_level)
        parts.append(f"Nouveau titre · {new_title}" if new_title != old_title else f"Niveau {award.level}")
        unlocked = skins_unlocked_between(award.previous_level, award.level)
        if unlocked:
            names = " · ".join(skin.name for skin in unlocked)
            parts.append(f"Nouveau skin · {names} (`/reglages`)")
    if collections:
        names = " · ".join(skin.name for skin in collections)
        parts.append(f"Nouvelle collection · {names} (`/reglages`)")
    return parts


def rating_options(default: float | None) -> list[discord.SelectOption]:
    """Notes de 10 à 0 ; un menu n'affiche que des étoiles unicode."""
    selected = int(round(default)) if default is not None else None
    options = []
    for points in range(RATING_MAX, -1, -1):
        stars = "★" * (points // 2) + ("½" if points % 2 else "")
        options.append(
            discord.SelectOption(
                label=f"{points}/{RATING_MAX}  {stars}".rstrip(),
                value=str(points),
                default=points == selected,
            )
        )
    return options


class RateModal(discord.ui.Modal, title="Noter cette œuvre"):
    def __init__(
        self,
        parent: Any,
        *,
        max_comment: int,
        default_rating: float | None,
        default_comment: str,
        default_experienced: str = "",
        default_spoiler: bool = False,
    ):
        super().__init__()
        self._hub = parent
        self._default_experienced = default_experienced
        # Un menu plutôt qu'un champ libre : la note ne peut pas être invalide,
        # donc le commentaire tapé n'est jamais perdu sur une erreur de saisie.
        self.rating_select = discord.ui.Select(
            placeholder="Choisis ta note",
            options=rating_options(default_rating),
        )
        self.comment_input = discord.ui.TextInput(
            label="Commentaire (optionnel)",
            style=discord.TextStyle.paragraph,
            placeholder="Un avis court…",
            default=default_comment[:max_comment] if default_comment else None,
            max_length=max_comment,
            required=False,
        )
        self.date_input = discord.ui.TextInput(
            label="Date (optionnel)",
            placeholder="Ex. 12/03/2024 — vu, joué, écouté ou lu",
            default=experienced_to_input(default_experienced) or None,
            max_length=12,
            required=False,
        )
        media_type = getattr(getattr(parent, "hit", None), "media_type", "") or ""
        self.ongoing_check: discord.ui.Checkbox | None = None
        self.spoiler_check = discord.ui.Checkbox(custom_id="spoiler", default=bool(default_spoiler))
        self.add_item(discord.ui.Label(text="Note", description="Une étoile = 2 points", component=self.rating_select))
        self.add_item(self.comment_input)
        self.add_item(self.date_input)
        if allows_ongoing(media_type):
            self.ongoing_check = discord.ui.Checkbox(
                custom_id="ongoing",
                default=is_ongoing(default_experienced),
            )
            self.add_item(
                discord.ui.Label(
                    text=ongoing_label(media_type),
                    description="Remplace la date",
                    component=self.ongoing_check,
                )
            )
        self.add_item(
            discord.ui.Label(
                text="Spoiler",
                description="Masque le commentaire en public",
                component=self.spoiler_check,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = parse_rating(self.rating_select.values[0]) if self.rating_select.values else None
        if rating is None:
            await interaction.response.send_message("**Erreur ·** Choisis une note.", ephemeral=True)
            return
        if self.ongoing_check is not None and self.ongoing_check.value:
            experienced_at, date_error = ONGOING, None
        else:
            experienced_at, date_error = parse_experienced_date(str(self.date_input.value or ""))
        if date_error:
            # La note est enregistrée quand même ; seule la date mal tapée est ignorée.
            experienced_at = self._default_experienced
        from_public = bool(self._hub.from_published_modal)
        orphan = from_public or (self._hub._interaction is None and self._hub._message is None)
        await interaction.response.defer(ephemeral=orphan or from_public)
        if orphan and not from_public:
            self._hub._interaction = interaction
        await self._hub.save_review(
            interaction,
            rating,
            str(self.comment_input.value or "").strip(),
            experienced_at or "",
            spoiler=bool(self.spoiler_check.value),
        )
        if date_error:
            await interaction.followup.send(f"**Date ignorée ·** {date_error}", ephemeral=True)


# ---------------------------------------------------------------------------
# Boutons de la fiche privée
# ---------------------------------------------------------------------------

class MediaSelect(discord.ui.Select):
    def __init__(self, parent: "FicheView", hits: list[MediaHit], selected: int):
        options = []
        for index, hit in enumerate(hits[:25]):
            label = f"{hit.title} · {hit.year}" if hit.year else hit.title
            options.append(
                discord.SelectOption(
                    label=pretty.shorten_text(label, 100) or "Sans titre",
                    value=str(index),
                    description=select_hit_description(hit),
                    emoji=select_emoji(hit.media_type),
                    default=index == selected,
                )
            )
        super().__init__(placeholder="Choisir une œuvre", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.selected = int(self.values[0])
        self._hub.season = None
        self._hub.review_page = 0
        self._hub.tab = "fiche"
        self._hub.confirm_delete = False
        self._hub.picking_list = False
        await self._hub.show_selected(interaction)


class SeasonScopeSelect(discord.ui.Select):
    """Série complète, ou une saison. Chaque cible a sa propre note."""

    def __init__(self, parent: "FicheView", seasons: list[dict[str, Any]], *, include_show: bool):
        current = parent.season
        options: list[discord.SelectOption] = []
        if include_show:
            options.append(discord.SelectOption(
                label="Série complète",
                value="show",
                description="Une note pour toute la série",
                default=current is None,
            ))
        for item in seasons:
            number = int(item["n"])
            episodes = int(item.get("episodes") or 0)
            bits: list[str] = []
            if item.get("year"):
                bits.append(str(item["year"]))
            if episodes:
                bits.append(f"{episodes} épisode{'s' if episodes != 1 else ''}")
            options.append(discord.SelectOption(
                label=season_label(number),
                value=str(number),
                description=pretty.shorten_text(" · ".join(bits), 95) or None,
                default=current == number,
            ))
        super().__init__(placeholder="Série ou saison", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = self.values[0]
        self._hub.season = None if raw == "show" else int(raw)
        self._hub.review_page = 0
        self._hub.pending_rating = None
        self._hub.pending_comment = ""
        self._hub.confirm_delete = False
        self._hub.picking_list = False
        await interaction.response.defer()
        await self._hub.reload_stats()
        self._hub._build()
        await self._hub.push(interaction)


def rate_modal_for(parent: Any, user_id: int) -> RateModal:
    """Formulaire Noter prérempli avec la note existante (ou celle passée à /search)."""
    mine = parent.author_id == user_id
    existing = (parent.my_review if mine else None) or {}
    prefs = parent.prefs or UserPrefs()
    return RateModal(
        parent,
        max_comment=parent.cog.cached_comment_max(parent.guild),
        default_rating=existing.get("rating", parent.pending_rating if mine else None),
        default_comment=existing.get("comment") or (parent.pending_comment if mine else ""),
        default_experienced=note_experienced_default(prefs, existing),
        default_spoiler=note_spoiler_default(existing),
    )


class RateButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(label="Noter", style=discord.ButtonStyle.green)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._hub
        if parent.pending_rating is not None and parent.my_review is None:
            # /search avec une note : un clic suffit, sans formulaire.
            await interaction.response.defer()
            await parent.save_review(
                interaction,
                parent.pending_rating,
                parent.pending_comment,
                experienced_at=note_experienced_default(parent.prefs or UserPrefs(), {}),
                spoiler=False,
            )
            return
        await interaction.response.send_modal(rate_modal_for(parent, interaction.user.id))


class WatchlistButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        rated = bool(parent.my_review)
        on = bool(parent.on_watchlist) and not rated
        super().__init__(
            style=discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(BOOKMARK_ON if on else BOOKMARK_OFF),
            disabled=rated,
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.toggle_watchlist(interaction)


class AddToListButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(ADD_LIST),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.start_list_pick(interaction)


class FicheListSelect(discord.ui.Select):
    def __init__(self, parent: "FicheView", lists: list[dict[str, Any]]):
        options = [
            discord.SelectOption(
                label=pretty.shorten_text(record["title"], 95) or "Sans titre",
                value=str(record["id"]),
                description=pretty.shorten_text(
                    f"{record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''}",
                    95,
                ),
            )
            for record in lists
        ]
        super().__init__(placeholder="Ajouter à quelle liste ?", options=options)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.add_to_list(interaction, int(self.values[0]))


class CancelListPickButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(label="Annuler", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.picking_list = False
        self._hub._build()
        await apply_view(interaction, self._hub)


class DeleteReviewButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(style=discord.ButtonStyle.red, emoji=discord.PartialEmoji.from_str(TRASH))
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.confirm_delete = True
        self._hub._build()
        await apply_view(interaction, self._hub)


class DeleteReviewConfirmButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(label="Supprimer", style=discord.ButtonStyle.danger)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._hub.delete_review(interaction)


class DeleteReviewCancelButton(discord.ui.Button):
    def __init__(self, parent: "FicheView"):
        super().__init__(label="Annuler", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        self._hub.confirm_delete = False
        self._hub._build()
        await apply_view(interaction, self._hub)


def share_button_kwargs() -> dict[str, Any]:
    return {
        "label": "Partager",
        "style": discord.ButtonStyle.secondary,
        "emoji": discord.PartialEmoji.from_str(SHARE),
    }


class FicheShareButton(discord.ui.Button):
    """Publie la fiche dans le salon (avec ses boutons Noter / Ouvrir pour tous)."""

    def __init__(self, parent: "FicheView"):
        super().__init__(**share_button_kwargs())
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        message = await send_published_fiche(
            self._hub.cog,
            self._hub.guild,
            self._hub.hit,
            interaction,
            close_ephemeral=self._hub.parent is None,
        )
        if message is None:
            return
        if self._hub.parent is None:
            self._hub.stop()
            return
        # Ouverte depuis un autre menu : on y reste.
        self._hub.notice = f"**Partagé ·** {message.jump_url}"
        self._hub._build()
        await self._hub.push(interaction)
