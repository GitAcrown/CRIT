"""Préférences membre (`UserPrefs`) et leurs libellés."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
import unicodedata

from utils import pretty

from .constants import LIST_EDIT_MODES, TYPE_META
from .formatting import type_label
from .progress import DEFAULT_STAR_SKIN, parse_unlocked_skins, resolve_star_skin
from .providers import MediaHit


DATE_PREF_VALUES = ("empty", "today")


STREAM_REMIND_OFF = 0


STREAM_REMIND_PLANNED = 1


STREAM_REMIND_BOTH = 2


STREAM_REMIND_MODES = (STREAM_REMIND_OFF, STREAM_REMIND_PLANNED, STREAM_REMIND_BOTH)


@dataclass(frozen=True)
class UserPrefs:
    default_date: str = "empty"
    default_list_edit: str = "owner"
    default_search_type: str = "all"
    announce_notes: bool = True
    stream_remind: int = STREAM_REMIND_OFF
    stream_voice_status: bool = True
    star_skin: str = DEFAULT_STAR_SKIN
    bookmark_notify: bool = True
    unlocked_skins: frozenset[str] = frozenset()


def _fold_genre(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


_HORROR_GENRES = {"horreur", "horror"}


def is_october_horror(hit: MediaHit) -> bool:
    """Film ou série dont un genre TMDB est l'horreur."""
    if hit.media_type not in {"movie", "tv"}:
        return False
    names = [str(name) for name in hit.genres]
    show_genres = hit.extra.get("show_genres")
    if isinstance(show_genres, list):
        names.extend(str(name) for name in show_genres)
    return any(_fold_genre(name) in _HORROR_GENRES for name in names if name)


def today_experienced() -> str:
    return date.today().isoformat()


def note_experienced_default(prefs: UserPrefs, existing: dict[str, Any]) -> str:
    if existing:
        return str(existing.get("experienced_at") or "")
    return today_experienced() if prefs.default_date == "today" else ""


def note_spoiler_default(existing: dict[str, Any]) -> bool:
    return bool(existing.get("spoiler")) if "spoiler" in existing else False


def date_pref_label(value: str) -> str:
    return "Aujourd'hui" if value == "today" else "Vide"


def parse_search_types(value: str) -> tuple[str, ...]:
    if not value or value == "all":
        return ()
    chosen = {part.strip() for part in value.split(",")}
    return tuple(kind for kind in TYPE_META if kind in chosen)


def normalize_search_pref(value: Any) -> str:
    if value is None or value == "" or value == "all":
        return "all"
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        return "all"
    if "all" in parts:
        return "all"
    types = [kind for kind in TYPE_META if kind in parts]
    return ",".join(types) if types else "all"


def search_pref_label(value: str) -> str:
    types = parse_search_types(normalize_search_pref(value))
    if not types:
        return "Tous les types"
    return pretty.shorten_text(" · ".join(type_label(kind) for kind in types), 100)


def search_pref_includes(media_type: str, *kinds: str) -> bool:
    normalized = normalize_search_pref(media_type)
    if normalized == "all":
        return False
    chosen = set(parse_search_types(normalized))
    return any(kind in chosen for kind in kinds)


def announce_pref_label(value: bool) -> str:
    return "Publier" if value else "Ne pas annoncer"


def normalize_stream_remind(value: object) -> int:
    try:
        mode = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return STREAM_REMIND_OFF
    return mode if mode in STREAM_REMIND_MODES else STREAM_REMIND_OFF


def stream_remind_label(mode: int) -> str:
    labels = {
        STREAM_REMIND_OFF: "Désactivé",
        STREAM_REMIND_PLANNED: "Activé seulement pour les streams planifiés",
        STREAM_REMIND_BOTH: "Activé pour les streams planifiés et les streams en cours",
    }
    return labels.get(normalize_stream_remind(mode), labels[STREAM_REMIND_OFF])


def row_field(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def prefs_from_row(row: Any | None) -> UserPrefs:
    if row is None:
        return UserPrefs()
    date_value = str(row_field(row, "default_date", "empty") or "empty")
    edit_value = str(row_field(row, "default_list_edit", "owner") or "owner")
    search_value = normalize_search_pref(row_field(row, "default_search_type", "all") or "all")
    return UserPrefs(
        default_date=date_value if date_value in DATE_PREF_VALUES else "empty",
        default_list_edit=edit_value if edit_value in LIST_EDIT_MODES else "owner",
        default_search_type=search_value,
        announce_notes=bool(int(row_field(row, "announce_notes", 1) or 0)),
        stream_remind=normalize_stream_remind(row_field(row, "stream_remind", 0)),
        stream_voice_status=bool(int(row_field(row, "stream_voice_status", 1) or 0)),
        star_skin=resolve_star_skin(row_field(row, "star_skin", DEFAULT_STAR_SKIN)).id,
        bookmark_notify=bool(int(row_field(row, "bookmark_notify", 1) or 0)),
        unlocked_skins=parse_unlocked_skins(row_field(row, "unlocked_skins", "")),
    )
