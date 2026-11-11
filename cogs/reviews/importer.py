"""Import d'un export Letterboxd (ZIP officiel ou CSV au format Letterboxd) ou IMDb (CSV).

Le ZIP (Réglages → Data → Export) contient `ratings.csv`, `reviews.csv`, `diary.csv` et
`watchlist.csv`. Un CSV seul est aussi accepté : export partiel, ou CSV produit par un
outil tiers depuis SensCritique (s2l, critique2boxd…) au format Letterboxd.

Règles : aucune XP, aucune quête, aucune annonce ; une note déjà présente dans le carnet
n'est jamais écrasée ; les films vus sans note sont ignorés (une note est obligatoire).
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
import io
import re
import time
from typing import TYPE_CHECKING, Awaitable, Callable
import zipfile

import discord

from .constants import logger
from .providers import MediaHit, _fold, _year_from

if TYPE_CHECKING:
    from utils import dataio

    from .providers import MediaCatalog

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MATCH_CONCURRENCY = 5


@dataclass
class LetterboxdEntry:
    title: str
    year: int | None
    rating: float | None = None  # sur 10
    watched: str = ""  # AAAA-MM-JJ
    review: str = ""
    logged: str = ""  # date d'ajout dans Letterboxd (AAAA-MM-JJ)
    imdb_id: str = ""  # export IMDb : correspondance exacte via TMDB /find

    @property
    def key(self) -> tuple[str, int | None]:
        return _fold(self.title).strip(), self.year


@dataclass
class LetterboxdData:
    rated: list[LetterboxdEntry] = field(default_factory=list)
    watchlist: list[LetterboxdEntry] = field(default_factory=list)
    source: str = "Letterboxd"
    skipped: int = 0  # épisodes, jeux… que TMDB ne couvre pas


@dataclass
class ImportReport:
    source: str = "Letterboxd"
    skipped: int = 0
    imported: int = 0
    already: int = 0
    bookmarked: int = 0
    not_found: list[str] = field(default_factory=list)
    total: int = 0


class ImportError_(Exception):
    """Fichier illisible : le message est montré tel quel au membre."""


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def _col(row: dict[str, str], *names: str) -> str:
    lowered = {(k or "").strip().casefold(): (v or "") for k, v in row.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value:
            return value.strip()
    return ""


def _rating(raw: str) -> float | None:
    """Letterboxd note de 0,5 à 5 étoiles : ×2 pour passer sur 10."""
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    if value <= 0:
        return None
    if value > 5:  # CSV déjà sur 10 (certains outils tiers)
        return max(0.0, min(10.0, round(value)))
    return max(0.0, min(10.0, round(value * 2)))


def _rating_on_10(row: dict[str, str]) -> float | None:
    """`Rating10` / `Note` (outils tiers, SensCritique) sont déjà sur 10 ; `Rating` est sur 5."""
    raw10 = _col(row, "Rating10", "Note")
    if raw10:
        try:
            value = float(raw10.replace(",", "."))
        except ValueError:
            return None
        return max(0.0, min(10.0, round(value))) if value > 0 else None
    return _rating(_col(row, "Rating"))


def _entry(row: dict[str, str]) -> LetterboxdEntry | None:
    title = _col(row, "Name", "Title", "Titre")
    if not title:
        return None
    year_raw = _col(row, "Year", "Année", "Annee")
    year = int(year_raw) if year_raw.isdigit() else _year_from(year_raw)
    watched = _col(row, "Watched Date", "WatchedDate", "Watched")[:10]
    return LetterboxdEntry(
        title=title,
        year=year,
        rating=_rating_on_10(row),
        watched=watched if len(watched) == 10 else "",
        review=_plain(_col(row, "Review", "Critique")),
        logged=_col(row, "Date")[:10],
    )


def _plain(text: str) -> str:
    """Critique Letterboxd en HTML (<i>, <br />, <p>) → texte Discord (italique en *…*)."""
    text = re.sub(r"<\s*br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"</?(i|em)>", "*", text, flags=re.I)
    text = re.sub(r"</?(b|strong)>", "**", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_letterboxd(raw: bytes, filename: str) -> LetterboxdData:
    name = filename.casefold()
    data = LetterboxdData()
    if name.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise ImportError_("Ce ZIP est illisible.") from exc
        files = {info.filename.rsplit("/", 1)[-1].casefold(): info for info in archive.infolist()}

        def rows(file: str) -> list[dict[str, str]]:
            info = files.get(file)
            return _read_csv(archive.read(info)) if info else []

        if "ratings.csv" not in files and "watchlist.csv" not in files:
            raise ImportError_("Ce ZIP ne ressemble pas à un export Letterboxd (pas de `ratings.csv`).")
        by_key: dict[tuple[str, int | None], LetterboxdEntry] = {}
        for row in rows("ratings.csv"):
            entry = _entry(row)
            if entry and entry.rating is not None:
                by_key[entry.key] = entry
        # Journal puis critiques : date de visionnage la plus récente et texte de la critique.
        for file in ("diary.csv", "reviews.csv"):
            for row in rows(file):
                extra = _entry(row)
                if extra is None:
                    continue
                entry = by_key.get(extra.key)
                if entry is None:
                    if extra.rating is None:
                        continue
                    entry = by_key[extra.key] = extra
                if extra.watched and extra.watched > entry.watched:
                    entry.watched = extra.watched
                if extra.review and not entry.review:
                    entry.review = extra.review
        data.rated = list(by_key.values())
        rated_keys = set(by_key)
        for row in rows("watchlist.csv"):
            entry = _entry(row)
            if entry and entry.key not in rated_keys:
                data.watchlist.append(entry)
        return data

    if name.endswith(".csv"):
        rows = _read_csv(raw)
        if rows and _is_imdb(rows[0]):
            return _parse_imdb(rows, name)
        entries = [entry for row in rows if (entry := _entry(row))]
        if not entries:
            raise ImportError_("Ce CSV est vide ou n'a pas de colonne `Name` / `Title`.")
        if "watchlist" in name or all(entry.rating is None for entry in entries):
            data.watchlist = entries
        else:
            data.rated = [entry for entry in entries if entry.rating is not None]
        return data
    raise ImportError_("Envoie le ZIP d'export Letterboxd, ou un fichier `.csv` (Letterboxd ou IMDb).")


# ---------------------------------------------------------------------------
# IMDb : export des notes (ratings) ou de la watchlist, en CSV
# ---------------------------------------------------------------------------

_IMDB_ID = re.compile(r"\b(tt\d{5,})\b")
# Types IMDb que TMDB connaît (films et séries) ; épisodes, jeux et podcasts sont ignorés.
_IMDB_SKIPPED_TYPES = ("episode", "game", "podcast", "music video")


def _is_imdb(row: dict[str, str]) -> bool:
    keys = {(k or "").strip().casefold() for k in row}
    return "const" in keys or ("url" in keys and "title type" in keys)


def _imdb_id(row: dict[str, str]) -> str:
    match = _IMDB_ID.search(_col(row, "Const") or _col(row, "URL"))
    return match.group(1) if match else ""


def _parse_imdb(rows: list[dict[str, str]], name: str) -> LetterboxdData:
    """Notes IMDb sur 10 (entier) ; watchlist si pas de note ou fichier nommé « watchlist »."""
    data = LetterboxdData(source="IMDb")
    watchlist_file = "watchlist" in name or any(
        (k or "").strip().casefold() == "position" for k in rows[0]
    )
    for row in rows:
        imdb_id = _imdb_id(row)
        title = _col(row, "Title", "Original Title")
        if not imdb_id or not title:
            continue
        kind = _col(row, "Title Type", "Type").casefold()
        if any(word in kind for word in _IMDB_SKIPPED_TYPES):
            data.skipped += 1
            continue
        year_raw = _col(row, "Year")
        raw_rating = _col(row, "Your Rating", "Your Ratings", "You rated")
        try:
            rating: float | None = max(1.0, min(10.0, round(float(raw_rating)))) if raw_rating else None
        except ValueError:
            rating = None
        entry = LetterboxdEntry(
            title=title,
            year=int(year_raw) if year_raw.isdigit() else None,
            rating=rating,
            logged=_col(row, "Date Rated", "Date Added", "Created")[:10],
            imdb_id=imdb_id,
        )
        if rating is not None and not watchlist_file:
            data.rated.append(entry)
        else:
            data.watchlist.append(entry)
    if not data.rated and not data.watchlist and not data.skipped:
        raise ImportError_("Ce CSV IMDb ne contient aucune œuvre lisible.")
    return data


# ---------------------------------------------------------------------------
# Correspondance TMDB et écriture
# ---------------------------------------------------------------------------

async def match_film(catalog: "MediaCatalog", entry: LetterboxdEntry) -> MediaHit | None:
    """ID IMDb : correspondance exacte (film ou série). Sinon film TMDB du même titre, à un an près."""
    if entry.imdb_id:
        hits = await catalog.tmdb.find_imdb(entry.imdb_id)
        return hits[0] if hits else None
    query =f"{entry.title} {entry.year}" if entry.year else entry.title
    for attempt in (query, entry.title):
        hits = await catalog.tmdb.search(attempt, "movie", 5)
        for hit in hits:
            if entry.year is None or hit.year is None or abs(hit.year - entry.year) <= 1:
                return hit
        if not entry.year:
            break
    return None


ProgressCallback = Callable[[int, int], Awaitable[None]]


class ImportMixin:
    """Import Letterboxd dans le carnet du membre (base commune)."""

    db: "dataio.ModelDataManager"
    catalog: "MediaCatalog | None"

    async def import_letterboxd(
        self,
        guild: discord.Guild,
        user_id: int,
        data: LetterboxdData,
        progress: ProgressCallback,
    ) -> ImportReport:
        assert self.catalog is not None
        report = ImportReport(
            source=data.source, skipped=data.skipped, total=len(data.rated) + len(data.watchlist),
        )
        max_len = await self.get_comment_max(guild)  # type: ignore[attr-defined]
        await self.remember_member(guild.id, user_id)  # type: ignore[attr-defined]
        semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)
        done = 0
        last_push = 0.0

        async def matched(entry: LetterboxdEntry) -> tuple[LetterboxdEntry, MediaHit | None]:
            nonlocal done, last_push
            async with semaphore:
                try:
                    hit = await match_film(self.catalog, entry)  # type: ignore[arg-type]
                except Exception:
                    logger.exception("Import Letterboxd : recherche impossible pour %s", entry.title)
                    hit = None
            done += 1
            if time.monotonic() - last_push > 3:
                last_push = time.monotonic()
                await progress(done, report.total)
            return entry, hit

        rated = await asyncio.gather(*(matched(entry) for entry in data.rated))
        wanted = await asyncio.gather(*(matched(entry) for entry in data.watchlist))

        now = int(time.time())
        for entry, hit in rated:
            if hit is None:
                report.not_found.append(f"{entry.title} ({entry.year})" if entry.year else entry.title)
                continue
            media_id = await self._import_media_id(guild, hit)
            exists = await self.db.fetchone(
                "SELECT 1 AS n FROM reviews WHERE user_id=? AND media_id=?", user_id, media_id
            )
            if exists:
                report.already += 1
                continue
            # Date d'import, pas celle de Letterboxd : sinon un import ferait de son auteur le
            # « premier à noter » des centaines d'œuvres (titre Pionnier). « Vu le » garde la vraie date.
            stamp = now
            await self.db.execute(
                """INSERT INTO reviews (user_id, media_id, rating, comment, experienced_at, spoiler, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?) ON CONFLICT(user_id, media_id) DO NOTHING""",
                user_id, media_id, entry.rating, entry.review[:max_len], entry.watched, stamp, stamp,
                commit=False,
            )
            await self.db.execute(
                "DELETE FROM watchlist WHERE user_id=? AND media_id=?", user_id, media_id, commit=False,
            )
            report.imported += 1
        for entry, hit in wanted:
            if hit is None:
                report.not_found.append(f"{entry.title} ({entry.year})" if entry.year else entry.title)
                continue
            media_id = await self._import_media_id(guild, hit)
            rated_row = await self.db.fetchone(
                "SELECT 1 AS n FROM reviews WHERE user_id=? AND media_id=?", user_id, media_id
            )
            listed = await self.db.fetchone(
                "SELECT 1 AS n FROM watchlist WHERE user_id=? AND media_id=?", user_id, media_id
            )
            if rated_row or listed:
                report.already += 1
                continue
            # Directement en base : pas d'événement de quête pour un import.
            await self.db.execute(
                "INSERT OR IGNORE INTO watchlist (user_id, media_id, added_at) VALUES (?, ?, ?)",
                user_id, media_id, _stamp(entry.logged) or now, commit=False,
            )
            report.bookmarked += 1
        await self.db.commit()
        await progress(report.total, report.total)
        return report

    async def _import_media_id(self, guild: discord.Guild, hit: MediaHit) -> int:
        # Œuvre déjà connue : on ne remplace pas ses infos enrichies par celles de la recherche.
        known = await self.lookup_media_id(guild, hit)  # type: ignore[attr-defined]
        if known:
            return known
        return await self.upsert_media(guild, hit)  # type: ignore[attr-defined]


def _stamp(day: str) -> int | None:
    try:
        return int(time.mktime(time.strptime(day[:10], "%Y-%m-%d"))) + 12 * 3600
    except (ValueError, TypeError):
        return None


def report_text(report: ImportReport) -> str:
    lines = [
        f"**Import {report.source} terminé**",
        f"{report.imported} note{'s' if report.imported != 1 else ''} ajoutée{'s' if report.imported != 1 else ''}"
        f" · {report.bookmarked} signet{'s' if report.bookmarked != 1 else ''}"
        f" · {report.already} déjà dans ton carnet",
    ]
    if report.skipped:
        lines.append(f"-# {report.skipped} ignoré{'s' if report.skipped > 1 else ''} (épisodes, jeux…) : TMDB ne les couvre pas.")
    if report.not_found:
        shown = report.not_found[:15]
        more = len(report.not_found) - len(shown)
        lines.append(f"\n**Pas trouvé{'s' if len(report.not_found) > 1 else ''} sur TMDB ({len(report.not_found)})**")
        lines.append("\n".join(f"- {title}" for title in shown) + (f"\n-# et {more} autre(s)" if more else ""))
        lines.append("-# Ajoute-les à la main avec `/search` si besoin.")
    lines.append("-# Pas d'XP ni d'annonce pour un import. Tes notes existantes n'ont pas été touchées.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export : vers Letterboxd (films) ou carnet complet (tous types)
# ---------------------------------------------------------------------------

TYPE_NAMES = {"movie": "Film", "tv": "Série", "game": "Jeu", "album": "Album", "track": "Morceau", "book": "Livre"}


def _csv_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM UTF-8 : Excel lit correctement les accents.
    return buffer.getvalue().encode("utf-8-sig")


LETTERBOXD_MAX_BYTES = 900 * 1024  # Letterboxd refuse les fichiers de plus de 1 Mo


def _letterboxd_csvs(name: str, header: list[str], rows: list[list[object]]) -> list[tuple[str, bytes]]:
    """CSV au format exact de Letterboxd : UTF-8 sans BOM, guillemets échappés `\\"`, sans espace
    après les virgules, découpé en fichiers de moins de 1 Mo (en-tête répété)."""

    def line(values: list[object]) -> str:
        cells = []
        for value in values:
            text = str(value).replace("\r", "")
            if any(char in text for char in ',"\n'):
                text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
            cells.append(text)
        return ",".join(cells) + "\n"

    head = line(header)
    files: list[str] = []
    current = head
    for row in rows:
        text = line(row)
        if len((current + text).encode("utf-8")) > LETTERBOXD_MAX_BYTES and current != head:
            files.append(current)
            current = head
        current += text
    files.append(current)
    if len(files) == 1:
        return [(f"{name}.csv", files[0].encode("utf-8"))]
    return [(f"{name}-{index}.csv", content.encode("utf-8")) for index, content in enumerate(files, 1)]


def _day(stamp: object) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(stamp)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""


def _to_html(text: str) -> str:
    """Commentaire Discord → critique Letterboxd (qui lit le HTML) : gras, italique, sauts de ligne."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"\|\|(.+?)\|\|", r"\1", text, flags=re.S)  # spoiler Discord : le texte reste
    return text.replace("\n", "<br />")


def _tmdb_movie_id(source: str, media_type: str, source_id: str) -> str:
    if source != "tmdb" or media_type != "movie":
        return ""
    return source_id.split(":", 1)[0] if source_id.split(":", 1)[0].isdigit() else ""


class ExportMixin:
    """Export du carnet d'un membre (notes communes à tous les serveurs)."""

    db: "dataio.ModelDataManager"

    async def _export_rows(self, user_id: int) -> list:
        return await self.db.fetchall(
            """SELECT m.source, m.source_id, m.media_type, m.title, m.subtitle, m.year, m.url,
                      r.rating, r.comment, r.experienced_at, r.spoiler, r.created_at
               FROM reviews r JOIN media m ON m.id = r.media_id
               WHERE r.user_id=? ORDER BY r.created_at""",
            user_id,
        )

    async def export_letterboxd(self, user_id: int) -> tuple[list[tuple[str, bytes]], int, int]:
        """`notes.csv` + `watchlist.csv` au format d'import Letterboxd (films TMDB seulement).

        Renvoie ([(nom, contenu)], films notés, films en signet)."""
        header = ["tmdbID", "Title", "Year", "Rating10", "WatchedDate", "Review"]
        rated: list[list[object]] = []
        for row in await self._export_rows(user_id):
            tmdb_id = _tmdb_movie_id(row["source"], row["media_type"], str(row["source_id"]))
            if not tmdb_id:
                continue
            rating = max(1, min(10, round(float(row["rating"]))))  # Letterboxd : 1 à 10
            rated.append([
                tmdb_id, row["title"], row["year"] or "", rating,
                str(row["experienced_at"] or "")[:10], _to_html(row["comment"] or ""),
            ])
        wanted_rows = await self.db.fetchall(
            """SELECT m.source, m.source_id, m.media_type, m.title, m.year
               FROM watchlist w JOIN media m ON m.id = w.media_id
               WHERE w.user_id=? ORDER BY w.added_at""",
            user_id,
        )
        wanted: list[list[object]] = []
        for row in wanted_rows:
            tmdb_id = _tmdb_movie_id(row["source"], row["media_type"], str(row["source_id"]))
            if tmdb_id:
                wanted.append([tmdb_id, row["title"], row["year"] or ""])
        # CSV directs (pas de ZIP) : Letterboxd prend les fichiers tels quels.
        files = _letterboxd_csvs("notes", header, rated) if rated else []
        if wanted:
            files += _letterboxd_csvs("watchlist", ["tmdbID", "Title", "Year"], wanted)
        return files, len(rated), len(wanted)

    async def export_carnet(self, user_id: int) -> tuple[bytes, int]:
        """CSV de toutes les notes, tous types : une sauvegarde lisible dans un tableur."""
        header = ["Titre", "Type", "Année", "Auteur / créateur", "Note /10", "Commentaire", "Spoiler",
                  "Vu le", "Noté le", "Source", "ID source", "Lien"]
        rows = [
            [
                row["title"], TYPE_NAMES.get(row["media_type"], row["media_type"]), row["year"] or "",
                row["subtitle"] or "", f"{float(row['rating']):g}", row["comment"] or "",
                "oui" if row["spoiler"] else "", str(row["experienced_at"] or "")[:10],
                _day(row["created_at"]), row["source"], row["source_id"], row["url"] or "",
            ]
            for row in await self._export_rows(user_id)
        ]
        return _csv_bytes(header, rows), len(rows)
