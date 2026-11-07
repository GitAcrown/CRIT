"""Socle des menus : `ReviewsLayout`, séparateurs, envoi et remplacement des menus éphémères."""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from ..constants import MENU_TIMEOUT, NO_PINGS, logger
from ..formatting import format_stars, format_stars_compact
from ..progress import StarSkin


def _disable_interactive(item: discord.ui.Item) -> None:
    if getattr(item, "disabled", None) is False:
        item.disabled = True  # type: ignore[attr-defined]
    children = getattr(item, "children", None)
    if children:
        for child in children:
            _disable_interactive(child)


def _remember_session_view(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    message_id: int | None,
) -> None:
    """Après un defer éphémère, l'id webhook peut différer de celui du clic."""
    if view.is_finished() or not view.is_dispatchable():
        return
    store = interaction.client._connection.store_view
    if message_id is not None:
        store(view, message_id)
    store(view, None)


def bind_view_message(
    view: discord.ui.LayoutView,
    message: discord.Message | discord.WebhookMessage | None,
) -> None:
    if message is None:
        return
    view.message = message
    if hasattr(view, "_message"):
        view._message = message


async def publish_layout_message(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    files: list[discord.File] | None = None,
) -> discord.Message | None:
    """Publie un layout dans le salon, hors webhook d'interaction."""
    channel = interaction.channel
    if channel is None:
        return None
    try:
        kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
        if files:
            kwargs["files"] = files
        return await channel.send(**kwargs)
    except (AttributeError, discord.HTTPException) as exc:
        logger.warning("Impossible de publier dans le salon : %s", exc)
        return None


async def discard_ephemeral_menu(interaction: discord.Interaction) -> None:
    """Supprime le menu éphémère qui a déclenché le partage."""
    try:
        if interaction.message is not None:
            await interaction.message.delete()
            return
    except discord.HTTPException:
        pass
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


async def apply_view(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    """Met à jour le message cliqué, pas le defer d'origine."""
    kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
    target = interaction.message
    if not interaction.response.is_done():
        await interaction.response.edit_message(**kwargs)
        bind_view_message(view, target)
        return
    if target is not None:
        try:
            message = await target.edit(**kwargs)
            bind_view_message(view, message)
            return
        except discord.HTTPException:
            pass
    message = await interaction.edit_original_response(**kwargs)
    bind_view_message(view, message)


class TabButton(discord.ui.Button):
    """Onglet : `parent.tab` prend la valeur `tab`, l'onglet actif est en bleu."""

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


class PageButton(discord.ui.Button):
    """Flèche de pagination sur l'attribut `attr` du menu, grisée en bout de course."""

    def __init__(self, parent: Any, attr: str, delta: int, label: str, max_page: int):
        target = getattr(parent, attr) + delta
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=not 0 <= target <= max_page)
        self._hub = parent
        self._attr = attr
        self._delta = delta
        self._max_page = max_page

    async def callback(self, interaction: discord.Interaction) -> None:
        current = getattr(self._hub, self._attr)
        setattr(self._hub, self._attr, max(0, min(self._max_page, current + self._delta)))
        self._hub._build()
        await apply_view(interaction, self._hub)


def page_nav(parent: Any, attr: str, max_page: int, *middle: discord.ui.Item) -> discord.ui.ActionRow | None:
    """← [contrôles du milieu] → ; seulement les contrôles s'il n'y a qu'une page."""
    if max_page <= 0:
        return discord.ui.ActionRow(*middle) if middle else None
    return discord.ui.ActionRow(
        PageButton(parent, attr, -1, "←", max_page),
        *middle,
        PageButton(parent, attr, 1, "→", max_page),
    )


class BackButton(discord.ui.Button):
    def __init__(self, parent: "ReviewsLayout"):
        super().__init__(label="←", style=discord.ButtonStyle.secondary)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._hub.go_back(interaction)


class ReviewsLayout(discord.ui.LayoutView):
    """LayoutView CRIT : onglets au-dessus, puis un Container (texte + ActionRows).

    Navigation en place : `open()` affiche un sous-écran dans le même message, avec un
    bouton Retour ; le menu parent reste vivant et se réaffiche au retour (`on_return`).
    Un court `notice` s'affiche une fois au-dessus des actions, au lieu d'un nouveau message.
    """

    def __init__(self, *, timeout: float | None = MENU_TIMEOUT):
        super().__init__(timeout=timeout)
        self._interaction: discord.Interaction | None = None
        self._message: discord.WebhookMessage | discord.Message | None = None
        self.star_skin: StarSkin | None = None
        self.accent_colour: discord.Colour | None = None
        self.parent: ReviewsLayout | None = None
        self.notice: str | None = None
        self._covered = False

    def _build(self) -> None:
        """Construit le contenu ; chaque menu le redéfinit."""

    def _dispatch_item(self, item: discord.ui.Item, interaction: discord.Interaction):  # type: ignore[override]
        # Tant qu'on clique dans un sous-écran, les menus parents ne doivent pas expirer.
        parent = self.parent
        while parent is not None:
            parent._refresh_timeout()
            parent = parent.parent
        return super()._dispatch_item(item, interaction)

    async def open(self, interaction: discord.Interaction, child: "ReviewsLayout") -> None:
        """Affiche `child` à la place de ce menu (déjà préparé), avec un bouton Retour."""
        child.parent = self
        child._interaction = interaction
        child._message = self.message or self._message
        child.star_skin = child.star_skin or self.star_skin
        child.accent_colour = child.accent_colour or self.accent_colour
        self._covered = True
        child._build()
        await child.push(interaction)

    async def go_back(self, interaction: discord.Interaction) -> None:
        parent = self.parent
        if parent is None:
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.stop()
        parent._covered = False
        parent.notice = parent.notice or self.notice
        await parent.on_return(interaction)

    async def on_return(self, interaction: discord.Interaction) -> None:
        """Réaffiche ce menu au retour d'un sous-écran ; à redéfinir pour recharger les données."""
        self._build()
        await self.push(interaction)

    def stars(self, rating: float) -> str:
        return format_stars(rating, self.star_skin)

    def stars_compact(self, rating: float) -> str:
        return format_stars_compact(rating, self.star_skin)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Vue %s / %s : %s", type(self).__name__, type(item).__name__, error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "**Erreur ·** Le bouton a planté. Réessaie après un `&reload reviews`.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "**Erreur ·** Le bouton a planté. Réessaie après un `&reload reviews`.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self._covered:  # un sous-écran occupe le message
            return
        for item in list(self.children):
            _disable_interactive(item)
        message = self.message or self._message
        if message is None:
            return
        try:
            await message.edit(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass

    async def attach(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        try:
            bind_view_message(self, await interaction.original_response())
        except discord.HTTPException:
            bind_view_message(self, interaction.message)

    async def push(self, interaction: discord.Interaction | None = None) -> None:
        try:
            if interaction is not None:
                await apply_view(interaction, self)
                mid = getattr(self.message, "id", None) or (
                    interaction.message.id if interaction.message else None
                )
                _remember_session_view(interaction, self, mid)
                return
            message = self.message or self._message
            if message is not None:
                await message.edit(view=self, allowed_mentions=NO_PINGS)
                return
            if self._interaction is not None:
                await apply_view(self._interaction, self)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir %s : %s", type(self).__name__, exc)

    def set_layout(
        self,
        body: list[discord.ui.Item],
        *rows: discord.ui.Item | None,
        above: list[discord.ui.Item] | None = None,
        sticky_head: int = 0,
        control_separators: bool = True,
    ) -> None:
        """`control_separators=False` : pas de trait automatique avant un menu (formulaires de réglages)."""
        self.clear_items()
        above = list(above or [])
        if self.parent is not None:
            # Retour en tête de la rangée d'onglets, ou sur sa propre rangée.
            first = above[0] if above else None
            if isinstance(first, discord.ui.ActionRow) and len(first.children) < 5:
                above[0] = discord.ui.ActionRow(BackButton(self), *first.children)
            else:
                above.insert(0, discord.ui.ActionRow(BackButton(self)))
        for item in above:
            self.add_item(item)
        children = list(body)
        if self.notice:
            children.append(discord.ui.TextDisplay(self.notice))
            self.notice = None
        for row in rows:
            if row is None:
                continue
            children.append(row)
        if control_separators:
            head = children[:sticky_head]
            children = head + with_control_separators(children[sticky_head:])
        if children:
            self.add_item(discord.ui.Container(*children, accent_colour=self.accent_colour))


def sep_tight() -> discord.ui.Separator:
    """Séparateur compact, entre les critiques."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def sep_wide() -> discord.ui.Separator:
    """Séparateur large, sous un titre."""
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def _is_separator(item: discord.ui.Item) -> bool:
    return isinstance(item, discord.ui.Separator)


def _is_control_row(item: discord.ui.Item) -> bool:
    return isinstance(item, discord.ui.ActionRow)


def with_control_separators(items: list[discord.ui.Item]) -> list[discord.ui.Item]:
    """Un trait entre le contenu et les boutons/selects, jamais entre deux rangées de contrôles."""
    out: list[discord.ui.Item] = []
    last_control: bool | None = None
    for item in items:
        if _is_separator(item):
            if out and _is_separator(out[-1]):
                continue
            out.append(item)
            last_control = None
            continue
        is_control = _is_control_row(item)
        if out and last_control is not None and last_control != is_control and not _is_separator(out[-1]):
            out.append(sep_tight())
        out.append(item)
        last_control = is_control
    return out


def _member_role_colour(
    guild: discord.Guild,
    user: discord.Member | discord.User,
) -> discord.Colour | None:
    """Couleur affichée du membre : le rôle coloré le plus haut."""
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    if member is None:
        return None
    colour = member.colour
    if not colour.value:
        return None
    return colour


async def member_accent(
    bot: commands.Bot,
    guild: discord.Guild,
    user: discord.Member | discord.User,
) -> discord.Colour | None:
    """Couleur d'accent du carnet : celle du membre, sinon l'accent de profil."""
    colour = _member_role_colour(guild, user)
    if colour is not None:
        return colour
    cached = bot.get_user(user.id)
    if cached is not None and cached.accent_colour is not None:
        return cached.accent_colour
    try:
        fetched = await bot.fetch_user(user.id)
    except discord.HTTPException:
        return None
    return fetched.accent_colour


async def replace_ephemeral_layout(
    interaction: discord.Interaction,
    view: ReviewsLayout,
) -> None:
    """Remplace le menu déjà affiché, sans empiler un second message."""
    view._interaction = interaction
    if not interaction.response.is_done():
        if interaction.message is not None:
            await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
            bind_view_message(view, interaction.message)
        else:
            await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
            await view.attach(interaction)
        _remember_session_view(interaction, view, getattr(view.message, "id", None))
        return
    if interaction.message is not None:
        try:
            await apply_view(interaction, view)
            mid = getattr(view.message, "id", None) or interaction.message.id
            _remember_session_view(interaction, view, mid)
            return
        except discord.HTTPException:
            pass
    try:
        message = await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)
        bind_view_message(view, message)
        _remember_session_view(interaction, view, getattr(message, "id", None))
    except discord.HTTPException:
        await present_ephemeral_layout(interaction, view)


async def present_ephemeral_layout(
    interaction: discord.Interaction,
    view: ReviewsLayout,
) -> None:
    """Menu éphémère autonome : followup après un defer, jamais un edit du thinking."""
    view._interaction = interaction
    if not interaction.response.is_done():
        await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
        await view.attach(interaction)
        _remember_session_view(interaction, view, getattr(view.message, "id", None))
        return
    message = await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
    bind_view_message(view, message)
    _remember_session_view(interaction, view, getattr(message, "id", None))
    if interaction.type in (
        discord.InteractionType.application_command,
        discord.InteractionType.modal_submit,
    ):
        try:
            await interaction.edit_original_response(content="\u200b")
        except discord.HTTPException:
            pass

