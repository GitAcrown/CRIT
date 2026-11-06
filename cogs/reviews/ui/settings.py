"""Préférences, skins d'étoiles, configuration du serveur et aide."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import (
    ANNOUNCE_ROUTE_ALL,
    ANNOUNCE_ROUTE_ORDER,
    DEFAULT_COMMENT_MAX,
    MAX_COMMENT_MAX,
    MIN_COMMENT_MAX,
    TYPE_META,
)
from ..emojis import ALBUM, BOOK, GAME, MOVIE, MUSIC, STAR, TV, XP
from ..formatting import (
    announce_route_label,
    format_stars,
    list_edit_label,
    select_emoji,
    type_emoji,
    type_label,
)
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
from ..progress import (
    STAR_SKINS,
    event_is_open,
    format_xp_bar,
    level_progress,
    resolve_star_skin,
    StarSkin,
    title_for_level,
)
from .base import apply_view, ReviewsLayout, sep_wide
from .fiche import section_with_thumbnail
from .session import HubTabButton

if TYPE_CHECKING:
    from ..reviews import Reviews


# ---------------------------------------------------------------------------
# Préférences
# ---------------------------------------------------------------------------

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
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild,
            self._hub.user_id,
            **{self._field: value},
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


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
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild,
            self._hub.user_id,
            default_search_type=normalize_search_pref(self.values),
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


class PrefOnOffButton(discord.ui.Button):
    def __init__(self, parent: "PreferencesView", field: str, on: bool):
        super().__init__(
            label="ON" if on else "OFF",
            style=discord.ButtonStyle.green if on else discord.ButtonStyle.secondary,
        )
        self._hub = parent
        self._field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        current = bool(getattr(self._hub.prefs, self._field))
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild,
            self._hub.user_id,
            **{self._field: not current},
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


class PreferencesView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        user: discord.abc.User,
        prefs: UserPrefs,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.user_id = user.id
        self.prefs = prefs
        self.tab = "fiches"
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ces préférences ne s'affichent que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def _section(self, title: str, hint: str, select: discord.ui.Select) -> list[discord.ui.Item]:
        return [
            discord.ui.TextDisplay(f"**{title}**\n-# {hint}"),
            discord.ui.ActionRow(select),
        ]

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            HubTabButton(self, "fiches", "Fiches"),
            HubTabButton(self, "listes", "Listes"),
            HubTabButton(self, "stream", "Stream"),
        )

    def _build(self) -> None:
        prefs = self.prefs
        if self.tab == "stream":
            header = (
                f"## Préférences\n"
                f"-# Stream sur **{self.guild.name}**"
            )
            children: list[discord.ui.Item] = [
                section_with_thumbnail(header, self.user.display_avatar.url),
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
        elif self.tab == "listes":
            header = (
                f"## Préférences\n"
                f"-# Listes sur **{self.guild.name}**"
            )
            children = [
                section_with_thumbnail(header, self.user.display_avatar.url),
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
        else:
            header = (
                f"## Préférences\n"
                f"-# Fiches sur **{self.guild.name}**"
            )
            children = [
                section_with_thumbnail(header, self.user.display_avatar.url),
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
                    "**Signets notés**\n"
                    "-# MP quand quelqu'un note une œuvre qui est dans tes signets.",
                    accessory=PrefOnOffButton(self, "bookmark_notify", prefs.bookmark_notify),
                ),
            ]
        self.clear_items()
        self.add_item(self._tabs_row())
        self.add_item(discord.ui.Container(*children, accent_colour=self.accent_colour))

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)


# ---------------------------------------------------------------------------
# Skins d'étoiles
# ---------------------------------------------------------------------------

def _skin_lock_label(parent: "CustomView", skin: StarSkin) -> str:
    if skin.event:
        return "Octobre" if event_is_open(skin.event) else "Terminé"
    return f"Niv. {skin.unlock_level}"


def _skin_lock_reason(skin: StarSkin) -> str:
    if skin.event == "october_horror":
        if event_is_open(skin.event):
            return f"{skin.name} se débloque en notant un film ou une série d'horreur en octobre."
        return f"{skin.name} n'est plus gagnable. Elle se débloquait en notant un film ou une série d'horreur en octobre."
    return f"{skin.name} se débloque au niveau {skin.unlock_level}."


class EquipStarSkinButton(discord.ui.Button):
    def __init__(self, parent: "CustomView", skin: StarSkin):
        equipped = parent.equipped_id == skin.id
        locked = not parent.skin_unlocked(skin)
        if locked:
            super().__init__(
                label=_skin_lock_label(parent, skin),
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        elif equipped:
            super().__init__(label="Équipé", style=discord.ButtonStyle.green, disabled=True)
        else:
            super().__init__(label="Équiper", style=discord.ButtonStyle.primary)
        self._hub = parent
        self._skin_id = skin.id

    async def callback(self, interaction: discord.Interaction) -> None:
        skin = resolve_star_skin(
            self._skin_id,
            level=self._hub.level,
            owned=self._hub.unlocked_skins,
        )
        if skin.id != self._skin_id:
            await interaction.response.send_message(
                f"**Verrouillé ·** {_skin_lock_reason(resolve_star_skin(self._skin_id))}",
                ephemeral=True,
                delete_after=8,
            )
            return
        self._hub.prefs = await self._hub.cog.set_user_prefs(
            self._hub.guild, self._hub.user_id, star_skin=skin.id,
        )
        self._hub.star_skin = skin
        self._hub.equipped_id = skin.id
        self._hub._build()
        await apply_view(interaction, self._hub)


class CustomView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        user: discord.abc.User,
        prefs: UserPrefs,
        xp: int,
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.user_id = user.id
        self.prefs = prefs
        self.xp = xp
        level, into, need, total = level_progress(xp)
        self.level = level
        self.into = into
        self.need = need
        self.total = total
        self.unlocked_skins = prefs.unlocked_skins
        self.star_skin = resolve_star_skin(prefs.star_skin, level=level, owned=self.unlocked_skins)
        self.equipped_id = self.star_skin.id
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ce menu ne s'affiche que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def skin_unlocked(self, skin: StarSkin) -> bool:
        if skin.event:
            return skin.id in self.unlocked_skins
        return self.level >= skin.unlock_level

    def _header(self) -> str:
        title = title_for_level(self.level)
        lines = [
            "## Personnalisation",
            f"**Niveau {self.level}** · {title}",
        ]
        if self.level >= 99:
            lines.append(f"{XP} Niveau max · **{self.total} XP** au total")
        else:
            lines.append(
                f"{XP} **{self.into}** / **{self.need}** XP vers le niveau {self.level + 1}\n"
                f"{format_xp_bar(self.into, self.need)}\n"
                f"-# {self.total} XP au total"
            )
        return "\n".join(lines)

    def _skin_section(self, skin: StarSkin) -> discord.ui.Section:
        locked = not self.skin_unlocked(skin)
        equipped = self.equipped_id == skin.id
        status = " · équipé" if equipped else (" · verrouillé" if locked else "")
        if locked:
            hint = _skin_lock_reason(skin)
        elif skin.event:
            hint = "Débloquée. Elle reste équipable."
        else:
            hint = skin.description
        return discord.ui.Section(
            f"**{skin.name}**{status}\n{skin.preview(7)}\n-# {hint}",
            accessory=EquipStarSkinButton(self, skin),
        )

    def _build(self) -> None:
        children: list[discord.ui.Item] = [
            section_with_thumbnail(self._header(), self.user.display_avatar.url),
        ]
        for skin in STAR_SKINS:
            children.append(sep_wide())
            children.append(self._skin_section(skin))
        self.set_layout(children)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        if self.equipped_id != self.prefs.star_skin:
            self.prefs = await self.cog.set_user_prefs(
                self.guild, self.user_id, star_skin=self.equipped_id,
            )
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class CommentMaxModal(discord.ui.Modal, title="Longueur max. du commentaire"):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__()
        self._view_ref = view_ref
        self.length_input = discord.ui.TextInput(
            label=f"Caractères ({MIN_COMMENT_MAX}-{MAX_COMMENT_MAX})",
            placeholder=str(DEFAULT_COMMENT_MAX),
            default=str(view_ref.comment_max),
            max_length=3,
        )
        self.add_item(self.length_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        raw = self.length_input.value.strip()
        if not raw.isdigit() or not (MIN_COMMENT_MAX <= int(raw) <= MAX_COMMENT_MAX):
            await interaction.followup.send(
                f"**Erreur ·** Indique un entier entre {MIN_COMMENT_MAX} et {MAX_COMMENT_MAX}.",
                ephemeral=True,
            )
            return
        await self._view_ref.cog.set_comment_max(self._view_ref.guild, int(raw))
        await self._view_ref.refresh(interaction)


class EditCommentMaxButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CommentMaxModal(self._view_ref))


class AnnounceRouteModal(discord.ui.Modal, title="Salon d'annonce"):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__()
        self._view_ref = view_ref
        configured = set(view_ref.announce_channels)
        options: list[discord.SelectOption] = []
        for route in ANNOUNCE_ROUTE_ORDER:
            taken = route in configured
            options.append(
                discord.SelectOption(
                    label=announce_route_label(route),
                    value=route,
                    description="Mettre à jour le salon" if taken else "Nouveau salon",
                    emoji=select_emoji(route) if route != ANNOUNCE_ROUTE_ALL else None,
                )
            )
        self.type_select = discord.ui.Select(
            placeholder="Type de média",
            min_values=1,
            max_values=1,
            required=True,
            options=options,
        )
        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Salon textuel",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Type",
                description="« Tous les types » sert de salon par défaut",
                component=self.type_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Salon",
                description="Là où les nouvelles notes de ce type sont postées",
                component=self.channel_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        route = str(self.type_select.values[0]) if self.type_select.values else ""
        if route not in ANNOUNCE_ROUTE_ORDER:
            await interaction.followup.send("**Erreur ·** Type invalide.", ephemeral=True)
            return
        if not self.channel_select.values:
            await interaction.followup.send("**Erreur ·** Choisis un salon.", ephemeral=True)
            return
        channel = self.channel_select.values[0].resolve()
        if channel is None:
            try:
                channel = await self.channel_select.values[0].fetch()
            except discord.HTTPException:
                await interaction.followup.send("**Erreur ·** Salon introuvable.", ephemeral=True)
                return
        guild = self._view_ref.guild
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Seuls les salons textuels sont pris en charge.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.",
                ephemeral=True,
            )
            return
        await self._view_ref.cog.set_announce_route(guild, route, channel.id)
        await self._view_ref.refresh(interaction)


class AddAnnounceButton(discord.ui.Button):
    def __init__(self, view_ref: "ReviewsConfigView"):
        super().__init__(label="Ajouter", style=discord.ButtonStyle.green)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AnnounceRouteModal(self._view_ref))


class RemoveAnnounceSelect(discord.ui.Select):
    def __init__(self, view_ref: "ReviewsConfigView"):
        options = [
            discord.SelectOption(
                label=f"Retirer · {announce_route_label(route)}",
                value=route,
                description=f"#{view_ref.announce_channels[route].name}",
                emoji=select_emoji(route) if route != ANNOUNCE_ROUTE_ALL else None,
            )
            for route in ANNOUNCE_ROUTE_ORDER
            if route in view_ref.announce_channels
        ]
        super().__init__(
            placeholder="Retirer un salon…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        route = str(self.values[0]) if self.values else ""
        if route not in ANNOUNCE_ROUTE_ORDER:
            await self._view_ref.refresh(interaction)
            return
        await self._view_ref.cog.set_announce_route(self._view_ref.guild, route, None)
        await self._view_ref.refresh(interaction)


class ReviewsConfigView(ReviewsLayout):
    def __init__(
        self,
        cog: "Reviews",
        guild: discord.Guild,
        *,
        announce_channels: dict[str, discord.TextChannel],
        comment_max: int,
        review_count: int,
        media_count: int,
        api_status: dict[str, bool],
    ):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.announce_channels = announce_channels
        self.comment_max = comment_max
        self.review_count = review_count
        self.media_count = media_count
        self.api_status = api_status
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer le serveur` est requise.", ephemeral=True
            )
            return False
        return True

    def _announce_body(self) -> str:
        lines: list[str] = []
        for route in ANNOUNCE_ROUTE_ORDER:
            channel = self.announce_channels.get(route)
            if channel is None:
                continue
            if route == ANNOUNCE_ROUTE_ALL:
                lines.append(f"Tous les types · {channel.mention}")
            else:
                lines.append(f"{type_emoji(route)} {type_label(route)} · {channel.mention}")
        if not lines:
            return "**Salons d'annonce**\n*Aucun salon — les notes ne sont pas annoncées.*"
        hint = (
            "-# Un type sans salon dédié utilise *Tous les types*."
            if ANNOUNCE_ROUTE_ALL in self.announce_channels
            else "-# Les types sans salon dédié ne sont pas annoncés."
        )
        return "**Salons d'annonce**\n" + "\n".join(lines) + f"\n{hint}"

    def _build(self) -> None:
        apis = "  ·  ".join(f"{name} {'ok' if ok else 'manquant'}" for name, ok in self.api_status.items())
        rows: list[discord.ui.Item] = []
        if self.announce_channels:
            rows.append(discord.ui.ActionRow(RemoveAnnounceSelect(self)))
        self.set_layout(
            [
                discord.ui.TextDisplay(f"## Configuration des critiques — {self.guild.name}"),
                discord.ui.Separator(),
                discord.ui.Section(
                    self._announce_body(),
                    accessory=AddAnnounceButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.Section(
                    f"**Commentaire max.**\n{self.comment_max} caractères",
                    accessory=EditCommentMaxButton(self),
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"-# {self.review_count} critique(s) · {self.media_count} œuvre(s)\n-# APIs · {apis}"
                ),
            ],
            *rows,
        )

    async def _reload(self) -> None:
        self.announce_channels = await self.cog.get_announce_channels(self.guild)
        self.comment_max = await self.cog.get_comment_max(self.guild)
        self.review_count, self.media_count = await self.cog.counts(self.guild)
        self.api_status = self.cog.catalog.status() if self.cog.catalog else {}

    async def refresh(self, interaction: discord.Interaction | None = None) -> None:
        await self._reload()
        self._build()
        await self.push(interaction)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)


class HelpView(ReviewsLayout):
    """Aide du bot : commandes + comment noter, en un seul LayoutView."""

    def __init__(self, *, avatar_url: str | None = None):
        super().__init__()
        header = (
            "## CRIT\n"
            "**Vous avez toujours rêvé de critiquer les films streamés ?** "
            "Alors n'attendez plus : note films, séries, albums et livres — "
            "puis compare tes goûts avec les autres."
        )
        noter = (
            f"### {STAR} Comment noter\n"
            "1. Lance `/search` avec un titre (année bienvenue). Dès 2 lettres, "
            "des suggestions apparaissent. Tu peux déjà passer une **note** "
            "et un **commentaire**.\n"
            "2. S'il y a plusieurs résultats, choisis l'œuvre dans le menu.\n"
            "3. Clique **Noter** : un formulaire demande la note "
            f"({format_stars(0)} 0 → {format_stars(10)} 10, entier), "
            "un commentaire optionnel, la date (vu, joué, écouté ou lu), "
            "une case **En cours** qui remplace cette date "
            "(série, saison, album, livre ou jeu), "
            "et une case **Spoiler** pour masquer le commentaire en public.\n"
            "4. Sur une série, le menu choisit la série complète ou une saison. "
            "Chacune a sa propre note.\n"
            "5. Le signet l'ajoute à tes signets — il disparaît dès que tu notes.\n"
            "6. Si tu as déjà donné la note dans `/search` et que tu n'avais pas encore "
            "noté cette œuvre, **Noter** l'enregistre tout de suite.\n"
            "\n"
            "Une seule note par œuvre et par membre.\n"
            "-# Exemples · `Dune 2021`  ·  `film:Dune`  ·  `jeu:Hades`  ·  "
            "`album:Blonde`  ·  `livre:Dune`  ·  une URL TMDB, Steam, Spotify ou IMDb"
        )
        commandes = (
            "### Commandes\n"
            "`/search` — catalogues (TMDB, Steam, Spotify, Open Library) : fiche, noter ou signet\n"
            "`/stream` — lives en cours et streams planifiés : voir, s'intéresser, lier ou planifier\n"
            "`/carnet` — page d'un membre : profil, journal, signets, affinités "
            "(ou clic droit sur un membre → **Voir le carnet**)\n"
            "`/explore` — catalogue du serveur (récentes, meilleures, pires) et pour toi\n"
            "`/listes` — listes communes (autocomplete pour ouvrir une liste)\n"
            "`/tirage` — une œuvre au hasard (tes signets, ceux d'un membre, ou une liste commune)\n"
            "`/preferences` — tes défauts : date, listes, recherche, annonces, stream\n"
            "`/custom` — skins d'étoiles : niveaux, et citrouilles en notant un film ou une série d'horreur en octobre\n"
            "`/config` — salons d'annonces (par type) et longueur des commentaires "
            "*(Gérer le serveur)*\n"
            "`/help` — cette aide"
        )
        extras = (
            f"### {XP} Autour des notes\n"
            f"{MOVIE} Films  ·  {TV} Séries  ·  {GAME} Jeux  ·  {ALBUM} Albums  ·  {MUSIC} Morceaux  ·  {BOOK} Livres\n"
            "Le journal de `/carnet` se filtre par type, par œuvres en cours, et se trie (récentes, meilleures, pires). "
            "Ton commentaire spoiler reste lisible dans ton journal, pas en public. "
            "Les `/listes` sont partagées : le créateur décide qui peut les éditer "
            "(lui seul, des membres, ou tout le serveur). "
            "`/config` peut poster les notes dans un salon différent selon le type. "
            "`/stream` affiche les œuvres liées aux Go Live en cours "
            "(y compris ceux des autres), les streams planifiés, et permet d'y lier le tien "
            "ou d'en planifier un. "
            "Tes défauts (date, listes, recherche, annonces, signets notés, rappel et statut vocal) se règlent dans `/preferences`. "
            "Les skins d'étoiles se choisissent dans `/custom`. "
            "En octobre, noter un film ou une série d'horreur débloque les citrouilles, qui restent équipables ensuite. "
            "Colle seul dans un message un lien TMDB, IMDb, Letterboxd, Steam, Spotify ou Open Library : "
            "si l'œuvre a déjà été notée ici, CRIT répond avec la fiche. "
            "Sinon, ou dans une phrase, clic droit sur le message → **Ouvrir la fiche**. "
            "Tes notes, ton XP et tes signets te suivent sur tous les serveurs où est CRIT.\n"
            "-# Chaque note rapporte de l'XP (avec plafond quotidien)"
        )
        self.set_layout(
            [
                section_with_thumbnail(header, avatar_url),
                discord.ui.Separator(),
                discord.ui.TextDisplay(noter),
                discord.ui.Separator(),
                discord.ui.TextDisplay(commandes),
                discord.ui.Separator(),
                discord.ui.TextDisplay(extras),
            ]
        )
