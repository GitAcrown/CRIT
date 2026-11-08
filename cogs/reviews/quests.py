"""Quêtes hebdomadaires, quête thématique du mois, séries de semaines et titres spéciaux.

- 3 quêtes actives par membre. Une quête finie reste affichée jusqu'au lundi, puis est
  remplacée ; une quête pas finie continue la semaine suivante.
- 1 relance (reroll) par semaine sur une quête pas finie.
- Semaine validée = 3 quêtes finies dans la semaine. Les semaines validées d'affilée font
  la série ; 2 semaines validées dans un mois débloquent le skin de saison du mois.
- 4e quête : thématique du mois, répétable sans limite, moins d'XP.
L'XP des quêtes passe hors plafond quotidien.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import random
from typing import TYPE_CHECKING, Any, Callable
import unicodedata
from zoneinfo import ZoneInfo

from . import emojis as icons
from .constants import TYPE_META
from .progress import (
    STAR_SKINS,
    StarSkin,
    level_for_xp,
    level_title_label,
    season_skin_for_month,
)
from .providers import MediaHit

if TYPE_CHECKING:
    from utils import dataio

PARIS = ZoneInfo("Europe/Paris")
QUEST_SLOTS = 3
QUEST_XP = 40
THEME_XP = 10
SEASON_WEEKS = 2  # semaines validées dans le mois pour le skin de saison


# ---------------------------------------------------------------------------
# Calendrier
# ---------------------------------------------------------------------------

def today() -> date:
    return datetime.now(PARIS).date()


def week_key(day: date | None = None) -> str:
    year, week, _ = (day or today()).isocalendar()
    return f"{year}-W{week:02d}"


def week_monday(key: str) -> date:
    year, week = key.split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def previous_week_key(key: str) -> str:
    return week_key(week_monday(key) - timedelta(days=7))


def month_key(day: date | None = None) -> str:
    return (day or today()).strftime("%Y-%m")


def week_month_key(key: str) -> str:
    """Une semaine appartient au mois de son lundi."""
    return month_key(week_monday(key))


MONTH_NAMES = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


# ---------------------------------------------------------------------------
# Événements
# ---------------------------------------------------------------------------

@dataclass
class QuestEvent:
    kind: str  # review | bookmark | list_add
    hit: MediaHit
    created: bool = False
    pioneer: bool = False
    new_comment: bool = False
    from_bookmark: bool = False
    others_rated: int = 0


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _genres(hit: MediaHit) -> list[str]:
    names = [str(name) for name in hit.genres]
    extra = hit.extra.get("show_genres")
    if isinstance(extra, list):
        names.extend(str(name) for name in extra)
    return [_fold(name) for name in names if name]


def _has_genre(hit: MediaHit, *words: str) -> bool:
    keys = [_fold(word) for word in words]
    return any(key in genre for genre in _genres(hit) for key in keys)


def _new_review(event: QuestEvent) -> bool:
    return event.kind == "review" and event.created


# ---------------------------------------------------------------------------
# Quêtes hebdomadaires
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuestDef:
    id: str
    label: str
    target: int
    counts: Callable[[QuestEvent], bool]


_TYPE_ARTICLES = {
    "movie": "un film", "tv": "une série", "game": "un jeu",
    "album": "un album", "track": "un morceau", "book": "un livre",
}


def _type_quest(kind: str) -> QuestDef:
    return QuestDef(
        f"type_{kind}",
        f"Noter {_TYPE_ARTICLES[kind]}",
        1,
        lambda e, k=kind: _new_review(e) and e.hit.media_type == k,
    )


QUESTS: tuple[QuestDef, ...] = (
    QuestDef("rate3", "Noter 3 œuvres", 3, _new_review),
    QuestDef("comment2", "Commenter 2 notes", 2, lambda e: e.kind == "review" and e.new_comment),
    QuestDef("pioneer", "Être le premier du serveur à noter une œuvre", 1, lambda e: _new_review(e) and e.pioneer),
    QuestDef("join2", "Noter 2 œuvres déjà notées par d'autres", 2, lambda e: _new_review(e) and e.others_rated > 0),
    QuestDef(
        "classic", "Noter une œuvre sortie avant 2000", 1,
        lambda e: _new_review(e) and e.hit.year is not None and e.hit.year < 2000,
    ),
    QuestDef(
        "recent", "Noter une sortie de cette année ou de l'an dernier", 1,
        lambda e: _new_review(e) and e.hit.year is not None and e.hit.year >= today().year - 1,
    ),
    QuestDef("from_bookmark", "Noter une œuvre de tes signets", 1, lambda e: _new_review(e) and e.from_bookmark),
    QuestDef("bookmark2", "Mettre 2 œuvres en signet", 2, lambda e: e.kind == "bookmark"),
    QuestDef("list_add", "Ajouter une œuvre à une liste du serveur", 1, lambda e: e.kind == "list_add"),
    *(_type_quest(kind) for kind in TYPE_META),
)
QUEST_BY_ID = {quest.id: quest for quest in QUESTS}


def pick_quests(count: int, exclude: set[str]) -> list[str]:
    """Tirage sans doublon ; une seule quête « type » à la fois pour varier."""
    pool = [q.id for q in QUESTS if q.id not in exclude]
    has_type = any(qid.startswith("type_") for qid in exclude)
    random.shuffle(pool)
    picked: list[str] = []
    for qid in pool:
        if qid.startswith("type_"):
            if has_type:
                continue
            has_type = True
        picked.append(qid)
        if len(picked) == count:
            break
    return picked


# ---------------------------------------------------------------------------
# Quête thématique du mois (répétable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonthTheme:
    name: str
    label: str
    counts: Callable[[QuestEvent], bool]


def _screen(e: QuestEvent) -> bool:
    return e.hit.media_type in {"movie", "tv"}


def _christmas(e: QuestEvent) -> bool:
    text = _fold(f"{e.hit.title} {e.hit.overview}")
    return any(word in text for word in ("noel", "christmas", "xmas")) or (
        _screen(e) and _has_genre(e.hit, "familial", "family")
    )


MONTH_THEMES: dict[int, MonthTheme] = {
    1: MonthTheme("Nouvel an", "Noter une œuvre sortie l'an dernier",
                  lambda e: e.hit.year == today().year - 1),
    2: MonthTheme("Saint-Valentin", "Noter une romance", lambda e: _has_genre(e.hit, "romance")),
    3: MonthTheme("Printemps", "Noter un film ou une série d'animation",
                  lambda e: _screen(e) and _has_genre(e.hit, "animation")),
    4: MonthTheme("Pâques", "Chasse aux œufs : noter une œuvre de tes signets", lambda e: e.from_bookmark),
    5: MonthTheme("Joli mois de mai", "Noter une comédie", lambda e: _has_genre(e.hit, "comedie", "comedy")),
    6: MonthTheme("Fête de la musique", "Noter un album ou un morceau",
                  lambda e: e.hit.media_type in {"album", "track"}),
    7: MonthTheme("Été", "Noter un film ou une série d'aventure ou d'action",
                  lambda e: _screen(e) and _has_genre(e.hit, "aventure", "adventure", "action")),
    8: MonthTheme("Vacances", "Noter un jeu", lambda e: e.hit.media_type == "game"),
    9: MonthTheme("Rentrée", "Noter un livre", lambda e: e.hit.media_type == "book"),
    10: MonthTheme("Halloween", "Noter un film ou une série d'horreur",
                   lambda e: _screen(e) and _has_genre(e.hit, "horreur", "horror")),
    11: MonthTheme("Automne", "Noter un thriller, un polar ou un mystère",
                   lambda e: _has_genre(e.hit, "thriller", "crime", "policier", "mystere", "mystery")),
    12: MonthTheme("Noël", "Noter une œuvre de Noël ou un film familial", _christmas),
}


def current_theme() -> MonthTheme:
    return MONTH_THEMES[today().month]


# ---------------------------------------------------------------------------
# Titres spéciaux
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpecialTitle:
    id: str
    name: str
    icon: str  # attribut de `emojis`
    hint: str

    def label(self) -> str:
        return f"{getattr(icons, self.icon, '')} {self.name}".strip()


SPECIAL_TITLES: tuple[SpecialTitle, ...] = (
    SpecialTitle("pionnier", "Pionnier", "TITLE_PIONNIER", "Être le premier à noter 25 œuvres."),
    SpecialTitle("plume", "Plume", "TITLE_PLUME", "Laisser 50 commentaires."),
    SpecialTitle("eclectique", "Éclectique", "TITLE_ECLECTIQUE", "Noter 5 types d'œuvres différents le même mois."),
    SpecialTitle("contre", "Contre-courant", "TITLE_CONTRE", "10 notes à 4 points ou plus de la moyenne des autres."),
    SpecialTitle("increvable", "Increvable", "TITLE_INCREVABLE", "Une série de 8 semaines de quêtes."),
    SpecialTitle("aventurier", "Aventurier", "TITLE_AVENTURIER", "Finir 30 quêtes."),
    SpecialTitle("collection", "Collectionneur", "TITLE_COLLECTION", "Posséder 3 skins d'événement ou de saison."),
)
TITLE_BY_ID = {title.id: title for title in SPECIAL_TITLES}


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

@dataclass
class QuestSlot:
    slot: int
    quest: QuestDef
    progress: int
    done_week: str

    @property
    def done(self) -> bool:
        return bool(self.done_week)


@dataclass
class QuestState:
    week: str
    slots: list[QuestSlot]
    rerolled: bool
    streak: int  # série en cours (0 si cassée)
    best_streak: int
    week_done: int  # quêtes finies cette semaine
    quests_done: int
    theme_count: int  # quêtes thématiques ce mois-ci
    month_weeks: int  # semaines validées ce mois-ci
    title: str


@dataclass
class QuestOutcome:
    """Effets d'un événement, pour le message de confirmation."""

    xp: int = 0
    lines: list[str] = field(default_factory=list)
    skins: list[StarSkin] = field(default_factory=list)


def live_streak(streak: int, last_full_week: str, week: str) -> int:
    """La série tient si la dernière semaine validée est celle-ci ou la précédente."""
    if last_full_week in {week, previous_week_key(week)}:
        return streak
    return 0


class QuestMixin:
    """Quêtes et titres ; s'appuie sur la base commune du carnet (`self.db`)."""

    db: dataio.ModelDataManager

    # -- état ---------------------------------------------------------------

    async def _quest_profile(self, user_id: int) -> Any:
        row = await self.db.fetchone("SELECT * FROM quest_profile WHERE user_id=?", user_id)
        if row is None:
            await self.db.execute("INSERT OR IGNORE INTO quest_profile (user_id) VALUES (?)", user_id)
            row = await self.db.fetchone("SELECT * FROM quest_profile WHERE user_id=?", user_id)
        return row

    async def _roll_week(self, user_id: int) -> Any:
        """Passage de semaine : remplace les quêtes finies, garde les autres, rend la relance."""
        profile = await self._quest_profile(user_id)
        week = week_key()
        rows = await self.db.fetchall("SELECT * FROM quest_slots WHERE user_id=? ORDER BY slot", user_id)
        valid = {int(r["slot"]): r for r in rows if str(r["quest_id"]) in QUEST_BY_ID}
        if profile["week"] == week and len(valid) == QUEST_SLOTS:
            return profile
        keep = {slot: r for slot, r in valid.items() if not r["done_week"] or r["done_week"] == week}
        free = [slot for slot in range(QUEST_SLOTS) if slot not in keep]
        fresh = pick_quests(len(free), {str(r["quest_id"]) for r in keep.values()})
        for slot, quest_id in zip(free, fresh):
            await self.db.execute(
                """INSERT INTO quest_slots (user_id, slot, quest_id, progress, done_week) VALUES (?, ?, ?, 0, '')
                   ON CONFLICT(user_id, slot) DO UPDATE SET quest_id=excluded.quest_id, progress=0, done_week=''""",
                user_id, slot, quest_id, commit=False,
            )
        if profile["week"] != week:
            await self.db.execute(
                "UPDATE quest_profile SET week=?, rerolled=0 WHERE user_id=?", week, user_id, commit=False,
            )
        await self.db.commit()
        return await self._quest_profile(user_id)

    async def load_quests(self, user_id: int) -> QuestState:
        profile = await self._roll_week(user_id)
        week = str(profile["week"])
        rows = await self.db.fetchall("SELECT * FROM quest_slots WHERE user_id=? ORDER BY slot", user_id)
        slots = [
            QuestSlot(int(r["slot"]), QUEST_BY_ID[str(r["quest_id"])], int(r["progress"]), str(r["done_week"]))
            for r in rows if str(r["quest_id"]) in QUEST_BY_ID
        ]
        week_row = await self.db.fetchone(
            "SELECT completed FROM quest_weeks WHERE user_id=? AND week=?", user_id, week
        )
        month_row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM quest_weeks WHERE user_id=? AND month=? AND completed>=?",
            user_id, month_key(), QUEST_SLOTS,
        )
        theme_count = int(profile["theme_count"]) if profile["theme_month"] == month_key() else 0
        return QuestState(
            week=week,
            slots=slots,
            rerolled=bool(profile["rerolled"]),
            streak=live_streak(int(profile["streak"]), str(profile["last_full_week"]), week),
            best_streak=int(profile["best_streak"]),
            week_done=int(week_row["completed"]) if week_row else 0,
            quests_done=int(profile["quests_done"]),
            theme_count=theme_count,
            month_weeks=int(month_row["n"]) if month_row else 0,
            title=str(profile["title"] or ""),
        )

    async def reroll_quest(self, user_id: int, slot: int) -> QuestDef | None:
        """Relance une quête pas finie ; une fois par semaine. Renvoie la nouvelle quête."""
        state = await self.load_quests(user_id)
        target = next((s for s in state.slots if s.slot == slot), None)
        if state.rerolled or target is None or target.done:
            return None
        picked = pick_quests(1, {s.quest.id for s in state.slots})
        if not picked:
            return None
        await self.db.execute(
            "UPDATE quest_slots SET quest_id=?, progress=0, done_week='' WHERE user_id=? AND slot=?",
            picked[0], user_id, slot, commit=False,
        )
        await self.db.execute("UPDATE quest_profile SET rerolled=1 WHERE user_id=?", user_id)
        return QUEST_BY_ID[picked[0]]

    # -- progression --------------------------------------------------------

    async def add_bonus_xp(self, user_id: int, amount: int) -> tuple[int, int]:
        """XP hors plafond quotidien. Renvoie (niveau avant, niveau après)."""
        row = await self.db.fetchone("SELECT xp FROM profiles WHERE user_id=?", user_id)
        before = int(row["xp"]) if row else 0
        total = before + amount
        await self.db.execute(
            """INSERT INTO profiles (user_id, xp, last_level) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET xp=excluded.xp, last_level=excluded.last_level""",
            user_id, total, level_for_xp(total),
        )
        return level_for_xp(before), level_for_xp(total)

    async def record_quest_event(self, user_id: int, event: QuestEvent) -> QuestOutcome:
        outcome = QuestOutcome()
        try:
            state = await self.load_quests(user_id)
        except Exception:
            return outcome
        week = state.week
        finished = 0
        for slot in state.slots:
            if slot.done or not slot.quest.counts(event):
                continue
            progress = min(slot.quest.target, slot.progress + 1)
            done_week = week if progress >= slot.quest.target else ""
            await self.db.execute(
                "UPDATE quest_slots SET progress=?, done_week=? WHERE user_id=? AND slot=?",
                progress, done_week, user_id, slot.slot, commit=False,
            )
            if done_week:
                finished += 1
                outcome.xp += QUEST_XP
                outcome.lines.append(f"{icons.QUEST} Quête accomplie · {slot.quest.label} (+{QUEST_XP} XP)")
        if event.kind == "review" and event.created and current_theme().counts(event):
            month = month_key()
            await self.db.execute(
                """UPDATE quest_profile SET theme_count = CASE WHEN theme_month=? THEN theme_count + 1 ELSE 1 END,
                   theme_month=? WHERE user_id=?""",
                month, month, user_id, commit=False,
            )
            outcome.xp += THEME_XP
            outcome.lines.append(f"{icons.THEME} {current_theme().name} · +{THEME_XP} XP")
        if finished:
            await self.db.execute(
                """INSERT INTO quest_weeks (user_id, week, month, completed) VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, week) DO UPDATE SET completed = completed + excluded.completed""",
                user_id, week, week_month_key(week), finished, commit=False,
            )
            await self.db.execute(
                "UPDATE quest_profile SET quests_done = quests_done + ? WHERE user_id=?",
                finished, user_id, commit=False,
            )
            if state.week_done < QUEST_SLOTS <= state.week_done + finished:
                outcome.lines.extend(await self._validate_week(user_id, week))
                outcome.skins.extend(await self._grant_season_skin(user_id, week))
        await self.db.commit()
        if outcome.xp:
            before, after = await self.add_bonus_xp(user_id, outcome.xp)
            if after > before:
                outcome.lines.append(f"Niveau {after} · {level_title_label(after)}")
        return outcome

    async def _validate_week(self, user_id: int, week: str) -> list[str]:
        profile = await self._quest_profile(user_id)
        previous = previous_week_key(week)
        streak = int(profile["streak"]) + 1 if profile["last_full_week"] == previous else 1
        best = max(streak, int(profile["best_streak"]))
        await self.db.execute(
            "UPDATE quest_profile SET streak=?, best_streak=?, last_full_week=? WHERE user_id=?",
            streak, best, week, user_id, commit=False,
        )
        plural = "s" if streak > 1 else ""
        return [f"{icons.STREAK} Semaine validée · série de **{streak}** semaine{plural}"]

    async def _grant_season_skin(self, user_id: int, week: str) -> list[StarSkin]:
        monday = week_monday(week)
        skin = season_skin_for_month(monday.month)
        if skin is None or monday.month != today().month:
            return []
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM quest_weeks WHERE user_id=? AND month=? AND completed>=?",
            user_id, month_key(monday), QUEST_SLOTS,
        )
        # Même connexion : la semaine qu'on vient d'écrire (pas encore commitée) est comptée.
        validated = int(row["n"]) if row else 0
        if validated < SEASON_WEEKS:
            return []
        prefs = await self.get_user_prefs(None, user_id)  # type: ignore[attr-defined]
        if skin.id in prefs.unlocked_skins:
            return []
        await self.set_user_prefs(None, user_id, unlocked_skins=set(prefs.unlocked_skins) | {skin.id})  # type: ignore[attr-defined]
        return [skin]

    # -- titres -------------------------------------------------------------

    async def earned_titles(self, user_id: int) -> set[str]:
        earned: set[str] = set()
        db = self.db
        row = await db.fetchone(
            """SELECT COUNT(*) AS n FROM reviews r WHERE r.user_id=? AND NOT EXISTS (
                 SELECT 1 FROM reviews o WHERE o.media_id=r.media_id AND o.created_at < r.created_at)""",
            user_id,
        )
        if row and int(row["n"]) >= 25:
            earned.add("pionnier")
        row = await db.fetchone("SELECT COUNT(*) AS n FROM reviews WHERE user_id=? AND comment != ''", user_id)
        if row and int(row["n"]) >= 50:
            earned.add("plume")
        row = await db.fetchone(
            """SELECT COUNT(DISTINCT m.media_type) AS n FROM reviews r JOIN media m ON m.id=r.media_id
               WHERE r.user_id=? GROUP BY strftime('%Y-%m', r.created_at, 'unixepoch')
               ORDER BY n DESC LIMIT 1""",
            user_id,
        )
        if row and int(row["n"]) >= 5:
            earned.add("eclectique")
        row = await db.fetchone(
            """SELECT COUNT(*) AS n FROM (
                 SELECT r.rating, AVG(o.rating) AS avg FROM reviews r
                 JOIN reviews o ON o.media_id=r.media_id AND o.user_id != r.user_id
                 WHERE r.user_id=? GROUP BY r.id HAVING COUNT(o.id) >= 2
               ) WHERE ABS(rating - avg) >= 4""",
            user_id,
        )
        if row and int(row["n"]) >= 10:
            earned.add("contre")
        profile = await self._quest_profile(user_id)
        if int(profile["best_streak"]) >= 8:
            earned.add("increvable")
        if int(profile["quests_done"]) >= 30:
            earned.add("aventurier")
        prefs = await self.get_user_prefs(None, user_id)  # type: ignore[attr-defined]
        if sum(1 for skin in STAR_SKINS if skin.event and skin.id in prefs.unlocked_skins) >= 3:
            earned.add("collection")
        return earned

    async def set_special_title(self, user_id: int, title_id: str) -> None:
        if title_id and title_id not in await self.earned_titles(user_id):
            return
        await self._quest_profile(user_id)
        await self.db.execute("UPDATE quest_profile SET title=? WHERE user_id=?", title_id, user_id)

    async def special_titles_for(self, user_ids: list[int]) -> dict[int, str]:
        """Titre spécial équipé (libellé avec icône), pour ceux qui en ont un."""
        if not user_ids:
            return {}
        placeholders = ", ".join("?" for _ in user_ids)
        rows = await self.db.fetchall(
            f"SELECT user_id, title FROM quest_profile WHERE title != '' AND user_id IN ({placeholders})",
            *user_ids,
        )
        return {
            int(r["user_id"]): TITLE_BY_ID[str(r["title"])].label()
            for r in rows if str(r["title"]) in TITLE_BY_ID
        }


def unlocked_skin_line(skins: list[StarSkin]) -> str:
    names = " · ".join(skin.name for skin in skins)
    return f"Nouveau skin · {names} (`/quetes`)"


__all__ = [
    "MONTH_NAMES",
    "QUESTS",
    "QUEST_XP",
    "QuestEvent",
    "QuestMixin",
    "QuestOutcome",
    "QuestState",
    "SPECIAL_TITLES",
    "THEME_XP",
    "TITLE_BY_ID",
    "current_theme",
    "unlocked_skin_line",
]
