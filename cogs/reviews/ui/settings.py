"""Configuration du serveur (/config) et aide (/help)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..constants import (
    ANNOUNCE_ROUTE_ALL,
    ANNOUNCE_ROUTE_ORDER,
    DEFAULT_COMMENT_MAX,
    MAX_COMMENT_MAX,
    MIN_COMMENT_MAX,
)
from ..emojis import ALBUM, BOOK, GAME, MOVIE, MUSIC, STAR, TV, XP
from ..formatting import (
    announce_route_label,
    format_stars,
    select_emoji,
    type_emoji,
    type_label,
)
from .base import ReviewsLayout
from .fiche import section_with_thumbnail

if TYPE_CHECKING:
    from ..reviews import Reviews


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
        # « Retirer un salon » juste sous les salons d'annonce, sans trait entre les deux.
        remove_row = [discord.ui.ActionRow(RemoveAnnounceSelect(self))] if self.announce_channels else []
        self.set_layout(
            [
                discord.ui.TextDisplay(f"## Configuration des critiques — {self.guild.name}"),
                discord.ui.Separator(),
                discord.ui.Section(
                    self._announce_body(),
                    accessory=AddAnnounceButton(self),
                ),
                *remove_row,
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
            control_separators=False,
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
            "5. **Signet** l'ajoute à tes signets — il disparaît dès que tu notes. "
            "**Liste** l'ajoute à une liste commune, sans quitter la fiche.\n"
            "6. Si tu as déjà donné la note dans `/search` et que tu n'avais pas encore "
            "noté cette œuvre, **Noter** l'enregistre tout de suite.\n"
            "\n"
            "Une seule note par œuvre et par membre. Sur une fiche publiée dans le salon, "
            "**Noter** ouvre directement ton formulaire, le signet et la liste agissent pour toi, "
            "**Critiques** te montre les avis du serveur : "
            "personne d'autre ne voit tes actions.\n"
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
            "`/quetes` — 3 quêtes par semaine, ta série, la quête du mois, ton titre et tes skins d'étoiles\n"
            "`/reglages` — tes préférences (recherche, date, annonces, listes, stream)\n"
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
            "Tout s'ouvre dans le même message : choisir une œuvre affiche sa fiche, la flèche **←** en haut ramène où tu étais. "
            "**Au hasard** tire une œuvre dans tes signets, une liste ou le catalogue. "
            "Tes défauts (date, listes, recherche, annonces, signets notés, rappel et statut vocal) "
            "se règlent dans `/reglages`. "
            "Dans `/quetes` : finis tes 3 quêtes pour valider la semaine et faire grandir ta série "
            "(une quête pas finie continue, une relance par semaine). "
            "Chaque mois a sa quête thématique répétable et son skin de saison, gagné en validant 2 semaines. "
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
