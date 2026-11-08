"""Réglages du membre (/reglages) : fiches, listes et stream (l'apparence est dans /quetes).

Les préférences sont communes à tous les serveurs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import TYPE_META
from ..emojis import SETTINGS
from ..formatting import list_edit_label, select_emoji, type_label
from ..prefs import (
    STREAM_REMIND_BOTH,
    STREAM_REMIND_OFF,
    STREAM_REMIND_PLANNED,
    announce_pref_label,
    date_pref_label,
    normalize_search_pref,
    parse_search_types,
    search_pref_label,
    stream_remind_label,
    UserPrefs,
)
from .base import apply_view, ReviewsLayout, sep_wide, TabButton
from .fiche import section_with_thumbnail

if TYPE_CHECKING:
    from ..reviews import Reviews


class PrefFieldSelect(discord.ui.Select):
    def __init__(
        self,
        parent: "PreferencesView",
        *,
        field: str,
        placeholder: str,
        options: list[discord.SelectOption],
    ):
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)
        self._hub = parent
        self._field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = self.values[0]
        value: str | bool = raw == "1" if self._field == "announce_notes" else raw
        await self._hub.save(interaction, **{self._field: value})


class PrefSearchTypeSelect(discord.ui.Select):
    def __init__(self, parent: "PreferencesView"):
        current = normalize_search_pref(parent.prefs.default_search_type)
        chosen = set(parse_search_types(current))
        options = [
            discord.SelectOption(
                label="Tous les types",
                value="all",
                description="Films, séries, jeux et musique — pas les livres",
                default=current == "all",
            )
        ]
        for kind in TYPE_META:
            options.append(
                discord.SelectOption(
                    label=type_label(kind),
                    value=kind,
                    emoji=select_emoji(kind),
                    description="Peut être combiné avec d'autres types",
                    default=current != "all" and kind in chosen,
                )
            )
        super().__init__(
            placeholder=search_pref_label(current),
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._hub.save(interaction, default_search_type=normalize_search_pref(self.values))


class PrefOnOffButton(discord.ui.Button):
    def __init__(self, parent: "PreferencesView", field: str, on: bool):
        super().__init__(
            label="Activé" if on else "Désactivé",
            style=discord.ButtonStyle.green if on else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        current = bool(getattr(self._hub.prefs, self._field))
        await self._hub.save(interaction, **{self._field: not current})


class PreferencesView(ReviewsLayout):
    """Onglets Fiches · Listes · Stream ; chaque choix s'enregistre aussitôt."""

    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        user: discord.abc.User,
        prefs: UserPrefs,
        tab: str = "fiches",
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.user_id = user.id
        self.prefs = prefs
        self.tab = tab
        self._build()

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, user: discord.abc.User) -> "PreferencesView":
        return cls(
            cog,
            guild,
            user=user,
            prefs=await cog.get_user_prefs(guild, user.id),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ces réglages ne s'affichent que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def save(self, interaction: discord.Interaction, **updates: object) -> None:
        self.prefs = await self.cog.set_user_prefs(self.guild, self.user_id, **updates)
        self._build()
        await apply_view(interaction, self)

    def _section(self, title: str, hint: str, select: discord.ui.Select) -> list[discord.ui.Item]:
        return [
            discord.ui.TextDisplay(f"**{title}**\n-# {hint}"),
            discord.ui.ActionRow(select),
        ]

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            TabButton(self, "fiches", "Fiches"),
            TabButton(self, "listes", "Listes"),
            TabButton(self, "stream", "Stream"),
        )

    def _header(self, subtitle: str) -> discord.ui.Item:
        return section_with_thumbnail(
            f"## {SETTINGS} Réglages\n-# {subtitle} · communs à tous les serveurs",
            self.user.display_avatar.url,
        )

    def _fiches(self) -> list[discord.ui.Item]:
        prefs = self.prefs
        return [
            self._header("Fiches"),
            sep_wide(),
            *self._section(
                "Types de recherche",
                "Un ou plusieurs types pour /search. « Tous les types » ignore les autres choix.",
                PrefSearchTypeSelect(self),
            ),
            sep_wide(),
            *self._section(
                "Date vu / joué / lu",
                "Préremplit le formulaire Noter, ou laisse le champ vide.",
                PrefFieldSelect(
                    self,
                    field="default_date",
                    placeholder=date_pref_label(prefs.default_date),
                    options=[
                        discord.SelectOption(
                            label="Aujourd'hui",
                            value="today",
                            description="Préremplit le formulaire Noter avec la date du jour",
                            default=prefs.default_date == "today",
                        ),
                        discord.SelectOption(
                            label="Vide",
                            value="empty",
                            description="Laisse le champ date vide",
                            default=prefs.default_date == "empty",
                        ),
                    ],
                ),
            ),
            sep_wide(),
            *self._section(
                "Annonces de tes notes",
                "Si tes notes apparaissent dans le salon d'annonces.",
                PrefFieldSelect(
                    self,
                    field="announce_notes",
                    placeholder=announce_pref_label(prefs.announce_notes),
                    options=[
                        discord.SelectOption(
                            label="Publier",
                            value="1",
                            description="Tes notes apparaissent dans le salon d'annonces",
                            default=prefs.announce_notes,
                        ),
                        discord.SelectOption(
                            label="Ne pas annoncer",
                            value="0",
                            description="Tes notes restent dans ton carnet seulement",
                            default=not prefs.announce_notes,
                        ),
                    ],
                ),
            ),
            sep_wide(),
            discord.ui.Section(
                "**Signets notés**\n-# MP quand quelqu'un note une œuvre qui est dans tes signets.",
                accessory=PrefOnOffButton(self, "bookmark_notify", prefs.bookmark_notify),
            ),
        ]

    def _listes(self) -> list[discord.ui.Item]:
        prefs = self.prefs
        return [
            self._header("Listes"),
            sep_wide(),
            *self._section(
                "Édition des nouvelles listes",
                "Qui peut modifier une liste que tu viens de créer.",
                PrefFieldSelect(
                    self,
                    field="default_list_edit",
                    placeholder=list_edit_label(prefs.default_list_edit),
                    options=[
                        discord.SelectOption(
                            label="Créateur seul",
                            value="owner",
                            description="Toi seul peux modifier une liste que tu crées",
                            default=prefs.default_list_edit == "owner",
                        ),
                        discord.SelectOption(
                            label="Membres choisis",
                            value="members",
                            description="Tu pourras ajouter des éditeurs ensuite",
                            default=prefs.default_list_edit == "members",
                        ),
                        discord.SelectOption(
                            label="Tout le serveur",
                            value="public",
                            description="N'importe qui pourra modifier tes nouvelles listes",
                            default=prefs.default_list_edit == "public",
                        ),
                    ],
                ),
            ),
        ]

    def _stream(self) -> list[discord.ui.Item]:
        prefs = self.prefs
        return [
            self._header("Stream"),
            sep_wide(),
            *self._section(
                "Rappel de stream",
                "Aucun rappel, seulement à l'heure d'un stream planifié, ou aussi quand tu lances un Go Live.",
                PrefFieldSelect(
                    self,
                    field="stream_remind",
                    placeholder=stream_remind_label(prefs.stream_remind),
                    options=[
                        discord.SelectOption(
                            label="Désactivé",
                            value="0",
                            description="Aucun rappel",
                            default=prefs.stream_remind == STREAM_REMIND_OFF,
                        ),
                        discord.SelectOption(
                            label="Activé seulement pour les streams planifiés",
                            value="1",
                            description="MP à l'heure d'un stream que tu as planifié",
                            default=prefs.stream_remind == STREAM_REMIND_PLANNED,
                        ),
                        discord.SelectOption(
                            label="Activé pour les streams planifiés et les streams en cours",
                            value="2",
                            description="MP aussi quand tu lances un Go Live",
                            default=prefs.stream_remind == STREAM_REMIND_BOTH,
                        ),
                    ],
                ),
            ),
            sep_wide(),
            discord.ui.Section(
                "**Statut du salon vocal**\n"
                "-# Ajoute/retire automatiquement le nom de l'œuvre en statut du vocal (ex. \"Dune (2021)\").",
                accessory=PrefOnOffButton(self, "stream_voice_status", prefs.stream_voice_status),
            ),
        ]

    def _build(self) -> None:
        pages = {"listes": self._listes, "stream": self._stream}
        # Chaque titre colle à son menu ; seuls les traits larges séparent les réglages.
        self.set_layout(pages.get(self.tab, self._fiches)(), above=[self._tabs_row()], control_separators=False)
