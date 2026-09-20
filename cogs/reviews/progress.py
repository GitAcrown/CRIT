"""XP, paliers, skins d'étoiles et affinités de goût.

La liste PROFILE_REWARDS se craft ici. XP_LEVEL_STEP règle la durée des niveaux :
le palier 2 vaut ~10 notes sans commentaire (100 XP).
"""

from __future__ import annotations

from dataclasses import dataclass

from .emojis import (
    CROWN_EMPTY,
    CROWN_FULL,
    CROWN_HALF,
    HEART_EMPTY,
    HEART_FULL,
    HEART_HALF,
    STAR,
    STAR_EMPTY,
    STAR_HALF,
)

# ---------------------------------------------------------------------------
# Récompenses de profil
# Ajoute tes emojis custom CRIT dans `emoji` (<:name:id>) quand tu les as.
# `unlock_level` = niveau à partir duquel ça s'affiche sur le profil.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileReward:
    id: str
    name: str
    unlock_level: int
    emoji: str = ""
    description: str = ""

    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip() if self.emoji else self.name


PROFILE_REWARDS: tuple[ProfileReward, ...] = (
    ProfileReward("novice", "Novice", 1, description="Le carnet est ouvert."),
    ProfileReward("curieux", "Curieux", 2, description="Tu reviens noter."),
    ProfileReward("chroniqueur", "Chroniqueur", 3, description="Les commentaires comptent."),
    ProfileReward("critique", "Critique", 5, description="On lit tes notes."),
    ProfileReward("referent", "Référent", 8, description="Les autres se calent sur toi."),
    ProfileReward("legende", "Légende du salon", 12, description="Le goût du serveur."),
)

LEVEL_TITLES: tuple[tuple[int, str], ...] = (
    (12, "Légende du salon"),
    (8, "Référent"),
    (5, "Critique"),
    (3, "Chroniqueur"),
    (2, "Curieux"),
    (1, "Novice"),
)

XP_PIONEER = 15
XP_JOIN = 10
XP_COMMENT = 10
XP_UPDATE = 2
DAILY_CAP = 80
FULL_AWARDS_PER_DAY = 5
MIN_AFFINITY_OVERLAP = 3
# XP pour passer du niveau n au n+1 : STEP * n  → niveau 2 = 100 XP ≈ 10 notes.
XP_LEVEL_STEP = 100
DEFAULT_STAR_SKIN = "classique"
XP_BAR_WIDTH = 10


@dataclass
class XpAward:
    gained: int
    total: int
    daily: int
    level: int
    previous_level: int
    capped: bool

    @property
    def leveled_up(self) -> bool:
        return self.level > self.previous_level

    @property
    def new_rewards(self) -> list[ProfileReward]:
        return [r for r in PROFILE_REWARDS if self.previous_level < r.unlock_level <= self.level]


def title_for_level(level: int) -> str:
    for minimum, title in LEVEL_TITLES:
        if level >= minimum:
            return title
    return "Novice"


def rewards_for_level(level: int) -> list[ProfileReward]:
    return [r for r in PROFILE_REWARDS if r.unlock_level <= level]


@dataclass(frozen=True)
class StarSkin:
    id: str
    name: str
    full: str
    half: str
    empty: str
    unlock_level: int
    description: str = ""

    def preview(self, rating: float = 10) -> str:
        points = int(round(max(0.0, min(10.0, float(rating)))))
        full = points // 2
        half = points % 2 == 1
        empty = 5 - full - (1 if half else 0)
        return self.full * full + (self.half if half else "") + self.empty * empty


STAR_SKINS: tuple[StarSkin, ...] = (
    StarSkin(
        DEFAULT_STAR_SKIN,
        "Étoiles",
        STAR,
        STAR_HALF,
        STAR_EMPTY,
        1,
        "Disponibles dès le niveau 1.",
    ),
    StarSkin(
        "coeurs",
        "Cœurs",
        HEART_FULL,
        HEART_HALF,
        HEART_EMPTY,
        2,
        "Se débloque au niveau 2.",
    ),
    StarSkin(
        "couronnes",
        "Couronnes",
        CROWN_FULL,
        CROWN_HALF,
        CROWN_EMPTY,
        3,
        "Se débloque au niveau 3.",
    ),
)
STAR_SKIN_ALIASES = {"rpg": "coeurs"}
STAR_SKIN_BY_ID: dict[str, StarSkin] = {skin.id: skin for skin in STAR_SKINS}


def resolve_star_skin(skin: StarSkin | str | None, *, level: int | None = None) -> StarSkin:
    classic = STAR_SKIN_BY_ID[DEFAULT_STAR_SKIN]
    if isinstance(skin, StarSkin):
        chosen = skin
    else:
        key = STAR_SKIN_ALIASES.get(str(skin or "").strip(), str(skin or "").strip())
        chosen = STAR_SKIN_BY_ID.get(key, classic)
    if level is not None and level < chosen.unlock_level:
        return classic
    return chosen


def skins_unlocked_between(previous_level: int, level: int) -> list[StarSkin]:
    return [skin for skin in STAR_SKINS if previous_level < skin.unlock_level <= level]


def format_xp_bar(into: int, need: int, *, width: int = XP_BAR_WIDTH) -> str:
    if need <= 0:
        filled = width
    else:
        filled = round(width * max(0, into) / need)
        filled = max(0, min(width, filled))
        if into > 0 and filled == 0:
            filled = 1
        if 0 < into < need and filled == width:
            filled = width - 1
    return "█" * filled + "░" * (width - filled)


def xp_to_reach_level(level: int) -> int:
    """XP cumulé nécessaire pour atteindre `level` (le niveau 1 vaut 0)."""
    if level <= 1:
        return 0
    n = level - 1
    return XP_LEVEL_STEP * n * (n + 1) // 2


def level_for_xp(xp: int) -> int:
    level = 1
    while xp >= xp_to_reach_level(level + 1):
        level += 1
        if level > 99:
            break
    return level


def level_progress(xp: int) -> tuple[int, int, int, int]:
    """(niveau, xp dans le palier, xp requis pour le suivant, xp total)."""
    level = level_for_xp(xp)
    current_floor = xp_to_reach_level(level)
    next_floor = xp_to_reach_level(level + 1)
    return level, xp - current_floor, next_floor - current_floor, xp


def apply_daily_limits(base: int, *, awards_today: int, daily_xp: int) -> tuple[int, bool]:
    if base <= 0:
        return 0, daily_xp >= DAILY_CAP
    if daily_xp >= DAILY_CAP:
        return 0, True
    amount = base if awards_today < FULL_AWARDS_PER_DAY else max(1, base // 2)
    room = DAILY_CAP - daily_xp
    if amount > room:
        return room, True
    return amount, False


def compute_review_xp(*, created: bool, pioneer: bool, new_comment: bool) -> int:
    if created:
        amount = XP_PIONEER if pioneer else XP_JOIN
        if new_comment:
            amount += XP_COMMENT
        return amount
    amount = XP_UPDATE
    if new_comment:
        amount += XP_COMMENT
    return amount


def agreement_percent(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    mean_diff = sum(abs(a - b) for a, b in pairs) / len(pairs)
    return max(0.0, min(100.0, 100.0 * (1.0 - mean_diff / 10.0)))


@dataclass
class Affinity:
    user_id: int
    overlap: int
    percent: float
    agreements: list[tuple[str, float, float]]
    disagreements: list[tuple[str, float, float]]
