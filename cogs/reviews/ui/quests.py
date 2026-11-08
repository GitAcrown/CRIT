"""/quetes : quêtes de la semaine, série, quête du mois, puis apparence (titre et skins)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .. import emojis as icons
from ..progress import (
    STAR_SKINS,
    StarSkin,
    event_is_open,
    event_month,
    format_xp_bar,
    level_progress,
    level_title_label,
    resolve_star_skin,
    season_skin_for_month,
    skin_is_unlocked,
)
from ..quests import (
    MONTH_NAMES,
    QUEST_SLOTS,
    QUEST_XP,
    SEASON_WEEKS,
    SPECIAL_TITLES,
    THEME_XP,
    QuestSlot,
    QuestState,
    current_theme,
    today,
    week_monday,
)
from .base import apply_view, ReviewsLayout, sep_tight, sep_wide, TabButton
from .fiche import section_with_thumbnail

if TYPE_CHECKING:
    from ..reviews import Reviews


def _progress_bar(done: int, target: int) -> str:
    return "▰" * done + "▱" * max(0, target - done)


def _month_name(month: int) -> str:
    return MONTH_NAMES[month - 1]


# ---------------------------------------------------------------------------
# Composants
# ---------------------------------------------------------------------------

class RerollButton(discord.ui.Button):
    def __init__(self, parent: "QuestsView", slot: QuestSlot):
        disabled = slot.done or parent.state.rerolled
        super().__init__(
            emoji=discord.PartialEmoji.from_str(icons.REROLL),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self._hub = parent
        self._slot = slot.slot

    async def callback(self, interaction: discord.Interaction) -> None:
        quest = await self._hub.cog.reroll_quest(self._hub.user_id, self._slot)
        await self._hub.reload()
        self._hub.notice = (
            f"**Quête relancée ·** {quest.label}." if quest
            else "**Relance impossible ·** Une seule relance par semaine, sur une quête pas finie."
        )
        self._hub._build()
        await apply_view(interaction, self._hub)


class EquipStarSkinButton(discord.ui.Button):
    def __init__(self, parent: "QuestsView", skin: StarSkin):
        if not parent.skin_unlocked(skin):
            super().__init__(label=_skin_lock_label(skin), style=discord.ButtonStyle.secondary, disabled=True)
        elif parent.star_skin.id == skin.id:
            super().__init__(label="Équipé", style=discord.ButtonStyle.green, disabled=True)
        else:
            super().__init__(label="Équiper", style=discord.ButtonStyle.primary)
        self._hub = parent
        self._skin_id = skin.id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._hub.cog.set_user_prefs(self._hub.guild, self._hub.user_id, star_skin=self._skin_id)
        await self._hub.reload()
        self._hub._build()
        await apply_view(interaction, self._hub)


class TitleSelect(discord.ui.Select):
    def __init__(self, parent: "QuestsView"):
        options = [
            discord.SelectOption(
                label=f"Titre de niveau · {parent.level_title}",
                value="none",
                description="Suit ton niveau",
                default=not parent.state.title,
            )
        ]
        for title in SPECIAL_TITLES:
            if title.id not in parent.earned:
                continue
            options.append(discord.SelectOption(
                label=title.name,
                value=title.id,
                description=title.hint[:100],
                emoji=discord.PartialEmoji.from_str(getattr(icons, title.icon)),
                default=parent.state.title == title.id,
            ))
        super().__init__(placeholder="Titre affiché", options=options, min_values=1, max_values=1)
        self._hub = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        await self._hub.cog.set_special_title(self._hub.user_id, "" if value == "none" else value)
        await self._hub.reload()
        self._hub._build()
        await apply_view(interaction, self._hub)


def _skin_lock_label(skin: StarSkin) -> str:
    month = event_month(skin.event) if skin.event else None
    if month:
        return _month_name(month).capitalize() if event_is_open(skin.event) else "Terminé"
    return f"Niv. {skin.unlock_level}"


def _skin_lock_reason(skin: StarSkin) -> str:
    if skin.event == "october_horror":
        if event_is_open(skin.event):
            return f"{skin.name} se débloque en notant un film ou une série d'horreur en octobre."
        return f"{skin.name} se gagne en octobre, en notant un film ou une série d'horreur."
    month = event_month(skin.event) if skin.event else None
    if month:
        when = "ce mois-ci" if event_is_open(skin.event) else f"en {_month_name(month)}"
        return f"{skin.name} se gagne {when} en validant {SEASON_WEEKS} semaines de quêtes."
    return f"{skin.name} se débloque au niveau {skin.unlock_level}."


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

class QuestsView(ReviewsLayout):
    """Onglets Quêtes · Apparence."""

    def __init__(self, cog: "Reviews", guild: discord.Guild, user: discord.abc.User, *, tab: str = "quetes"):
        super().__init__()
        self.cog = cog
        self.guild = guild
        self.user = user
        self.user_id = user.id
        self.tab = tab

    @classmethod
    async def create(cls, cog: "Reviews", guild: discord.Guild, user: discord.abc.User) -> "QuestsView":
        view = cls(cog, guild, user)
        await view.reload()
        view._build()
        return view

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)
        await self.attach(interaction)

    async def reload(self) -> None:
        self.state: QuestState = await self.cog.load_quests(self.user_id)
        self.prefs = await self.cog.get_user_prefs(self.guild, self.user_id)
        self.level, self.into, self.need, self.total = level_progress(
            await self.cog.get_profile_xp(self.guild, self.user_id)
        )
        self.level_title = level_title_label(self.level)
        self.star_skin = resolve_star_skin(self.prefs.star_skin, level=self.level, owned=self.prefs.unlocked_skins)
        self.earned = await self.cog.earned_titles(self.user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "**Action impossible ·** Ces quêtes ne s'affichent que pour toi.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    def skin_unlocked(self, skin: StarSkin) -> bool:
        return skin_is_unlocked(skin, self.level, self.prefs.unlocked_skins)

    def _tabs_row(self) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            TabButton(self, "quetes", "Quêtes", emoji=icons.QUEST),
            TabButton(self, "apparence", "Apparence", emoji=icons.STAR),
        )

    # -- Quêtes -------------------------------------------------------------

    def _quetes(self) -> list[discord.ui.Item]:
        state = self.state
        monday = week_monday(state.week)
        reroll = "relance utilisée" if state.rerolled else f"{icons.REROLL} 1 relance dispo"
        streak = (
            f"{icons.STREAK} Série de **{state.streak}** semaine{'s' if state.streak > 1 else ''}"
            if state.streak else f"{icons.STREAK} Pas de série en cours"
        )
        if state.best_streak > state.streak:
            streak += f" · record **{state.best_streak}**"
        children: list[discord.ui.Item] = [
            section_with_thumbnail(
                f"## {icons.QUEST} Quêtes\n"
                f"-# Semaine du {monday.day} {_month_name(monday.month)} · "
                f"**{state.week_done}/{QUEST_SLOTS}** finies · {reroll}\n{streak}",
                self.user.display_avatar.url,
            ),
            sep_wide(),
        ]
        for index, slot in enumerate(state.slots):
            if index:
                children.append(sep_tight())
            if slot.done:
                text = f"~~{slot.quest.label}~~\n-# Accomplie · +{QUEST_XP} XP · remplacée lundi"
            else:
                text = (
                    f"**{slot.quest.label}**\n"
                    f"-# {_progress_bar(slot.progress, slot.quest.target)}  "
                    f"{slot.progress}/{slot.quest.target} · +{QUEST_XP} XP"
                )
            children.append(discord.ui.Section(text, accessory=RerollButton(self, slot)))
        theme = current_theme()
        count = state.theme_count
        children.extend([
            sep_wide(),
            discord.ui.TextDisplay(
                f"{icons.THEME} **{theme.name} ·** {theme.label}\n"
                f"-# Quête du mois, répétable · +{THEME_XP} XP à chaque fois"
                + (f" · **{count}** ce mois-ci" if count else "")
            ),
        ])
        skin = season_skin_for_month(today().month)
        if skin is not None:
            owned = skin.id in self.prefs.unlocked_skins
            weeks = min(state.month_weeks, SEASON_WEEKS)
            status = "Débloqué" if owned else f"**{weeks}/{SEASON_WEEKS}** semaines validées ce mois-ci"
            children.extend([
                sep_tight(),
                discord.ui.TextDisplay(f"**Skin du mois · {skin.name}**  {skin.preview(7)}\n-# {status}"),
            ])
        children.append(discord.ui.TextDisplay(
            "-# Finis les 3 quêtes pour valider la semaine. Une quête pas finie continue "
            "la semaine suivante ; les quêtes finies sont remplacées le lundi."
        ))
        return children

    # -- Apparence ----------------------------------------------------------

    def _apparence(self) -> list[discord.ui.Item]:
        if self.level >= 99:
            progress = f"{icons.XP} Niveau max · **{self.total} XP** au total"
        else:
            progress = (
                f"{icons.XP} **{self.into}** / **{self.need}** XP vers le niveau {self.level + 1}\n"
                f"{format_xp_bar(self.into, self.need)}"
            )
        locked = [title for title in SPECIAL_TITLES if title.id not in self.earned]
        locked_lines = "\n".join(f"-# {title.label()} · {title.hint}" for title in locked)
        children: list[discord.ui.Item] = [
            section_with_thumbnail(
                f"## Apparence\n**Niveau {self.level}** · {self.level_title}\n{progress}",
                self.user.display_avatar.url,
            ),
            sep_wide(),
            discord.ui.TextDisplay(
                "**Titre affiché**\n-# Sous ton nom dans le carnet et les annonces."
            ),
            discord.ui.ActionRow(TitleSelect(self)),
        ]
        if locked_lines:
            children.append(discord.ui.TextDisplay(f"-# **À gagner**\n{locked_lines}"))
        for skin in STAR_SKINS:
            is_locked = not self.skin_unlocked(skin)
            equipped = self.star_skin.id == skin.id
            status = " · équipé" if equipped else (" · verrouillé" if is_locked else "")
            if is_locked:
                hint = _skin_lock_reason(skin)
            elif skin.event or skin.id in self.prefs.unlocked_skins:
                hint = "Débloqué. Il reste équipable."
            else:
                hint = skin.description
            children.append(discord.ui.Section(
                f"**{skin.name}**{status}\n{skin.preview(7)}\n-# {hint}",
                accessory=EquipStarSkinButton(self, skin),
            ))
        return children

    def _build(self) -> None:
        page = self._apparence if self.tab == "apparence" else self._quetes
        self.set_layout(page(), above=[self._tabs_row()], control_separators=False)
