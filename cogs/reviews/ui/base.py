"""Socle des menus : `ReviewsLayout`, séparateurs, envoi et remplacement des menus éphémères."""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord.ext import commands

from ..constants import MENU_TIMEOUT, NO_PINGS, logger
from ..formatting import format_stars, format_stars_compact
from ..progress import StarSkin

NOTICE_TTL = 10  # secondes d'affichage d'un avis (« Critique enregistrée », « Ajouté »…)


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
    if hasattr(view, "_interaction"):
        # Dernier clic : son jeton permet de rééditer un message éphémère plus tard (effacement d'un avis).
        view._interaction = interaction
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


class PageJumpModal(discord.ui.Modal, title="Aller à la page"):
    def __init__(self, parent: Any, attr: str, max_page: int):
        super().__init__()
        self._hub = parent
        self._attr = attr
        self._max_page = max_page
        self.page = discord.ui.TextInput(
            placeholder=f"Page actuelle : {getattr(parent, attr) + 1}",
            min_length=1,
            max_length=4,
        )
        self.add_item(discord.ui.Label(text=f"Page (1 à {max_page + 1})", component=self.page))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.page.value).strip()
        if not raw.isdigit():
            self._hub.notice = f"**Page invalide ·** Entre un nombre entre 1 et {self._max_page + 1}."
        else:
            setattr(self._hub, self._attr, max(0, min(self._max_page, int(raw) - 1)))
        self._hub._build()
        await apply_view(interaction, self._hub)


class PageJumpButton(discord.ui.Button):
    """« 2/5 » entre les flèches : ouvre un formulaire pour sauter à une page. Grisé s'il n'y en a qu'une."""

    def __init__(self, parent: Any, attr: str, max_page: int):
        current = max(0, min(max_page, getattr(parent, attr)))
        super().__init__(
            label=f"{current + 1}/{max_page + 1}",
            style=discord.ButtonStyle.secondary,
            disabled=max_page <= 0,
        )
        self._hub = parent
        self._attr = attr
        self._max_page = max_page

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PageJumpModal(self._hub, self._attr, self._max_page))


def page_nav(parent: Any, attr: str, max_page: int, *middle: discord.ui.Item) -> discord.ui.ActionRow:
    """← N/M → puis les contrôles du menu (tri, au hasard…) ; flèches et numéro grisés sur une seule page."""
    max_page = max(0, max_page)
    return discord.ui.ActionRow(
        PageButton(parent, attr, -1, "←", max_page),
        PageJumpButton(parent, attr, max_page),
        PageButton(parent, attr, 1, "→", max_page),
        *middle,
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
        self._layout_version = 0
        self._notice_tasks: set[asyncio.Task] = set()

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
                try:
                    await message.edit(view=self, allowed_mentions=NO_PINGS)
                    return
                except discord.HTTPException:
                    # Message éphémère lié par un clic : seul le jeton d'interaction peut l'éditer.
                    if self._interaction is None:
                        raise
            if self._interaction is not None:
                await self._interaction.edit_original_response(view=self, allowed_mentions=NO_PINGS)
        except discord.HTTPException as exc:
            logger.warning("Impossible de rafraîchir %s : %s", type(self).__name__, exc)

    def _schedule_notice_clear(
        self,
        version: int,
        container: discord.ui.Container,
        notice_items: tuple[discord.ui.Item, ...],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._clear_notice_later(version, container, notice_items))
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _clear_notice_later(
        self,
        version: int,
        container: discord.ui.Container,
        notice_items: tuple[discord.ui.Item, ...],
    ) -> None:
        await asyncio.sleep(NOTICE_TTL)
        # Menu redessiné entre-temps (l'avis est déjà parti), fermé ou recouvert : rien à faire.
        if version != self._layout_version or self.is_finished() or self._covered:
            return
        kept: list[discord.ui.Item] = []
        for item in container.children:
            if any(item is n for n in notice_items):
                continue
            if _is_separator(item) and (not kept or _is_separator(kept[-1])):
                continue
            kept.append(item)
        while kept and _is_separator(kept[-1]):
            kept.pop()
        container.clear_items()
        for item in kept:
            container.add_item(item)
        await self.push()

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
        self._layout_version += 1
        notice_items: tuple[discord.ui.Item, ...] = ()
        if self.notice:
            # Avis séparé de la fiche, puis effacé au bout de NOTICE_TTL secondes.
            notice_items = (sep_tight(), discord.ui.TextDisplay(self.notice))
            children.extend(notice_items)
            self.notice = None
        for row in rows:
            if row is None:
                continue
            # Texte parmi les actions (question de confirmation…) : un trait le sépare du contenu.
            if not _is_control_row(row) and not _is_separator(row) and children and not _is_separator(children[-1]):
                children.append(sep_tight())
            children.append(row)
        if control_separators:
            head = children[:sticky_head]
            children = head + with_control_separators(children[sticky_head:])
        if notice_items:
            # Discord refuse plus de 40 composants : l'avis cède sa place (trait, puis texte) plutôt que de casser le menu.
            # +1 pour le Container, +2 pour la rangée « Partager » que certains menus ajoutent ensuite.
            used = sum(item._total_count for item in self.children) + 1 + 2
            for extra in notice_items:
                if used + sum(child._total_count for child in children) <= MAX_COMPONENTS:
                    break
                children = [child for child in children if child is not extra]
            notice_items = tuple(item for item in notice_items if any(child is item for child in children))
        if children:
            container = discord.ui.Container(*children, accent_colour=self.accent_colour)
            self.add_item(container)
            if notice_items:
                self._schedule_notice_clear(self._layout_version, container, notice_items)


MAX_COMPONENTS = 40  # limite Discord par message (tous niveaux confondus)


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

