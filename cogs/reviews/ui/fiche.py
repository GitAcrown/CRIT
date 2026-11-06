"""Rendu des fiches d'œuvre : fiche publiée, fiche compacte, annonces."""

from __future__ import annotations

from datetime import datetime
import time
from typing import Any, TYPE_CHECKING

import discord

from utils import pretty

from ..constants import NO_PINGS, PARIS_TZ, TEXT_DISPLAY_MAX, logger
from ..dyn import (
    AnnounceDynButton,
    bind_record,
    create_record,
    FicheDynButton,
    FicheRecord,
    get_record,
    hit_from_dict,
    hit_to_dict,
    is_live,
    mark_stripped,
    update_payload,
)
from ..emojis import EXPLICIT, MORE, PLANNING, SALE, STREAMING
from ..formatting import (
    experienced_from_row,
    experienced_line,
    fmt_int,
    format_comment,
    format_score,
    format_stars,
    row_spoiler,
    type_emoji,
    type_label,
)
from ..progress import StarSkin
from ..providers import MediaHit
from .base import (
    discard_ephemeral_menu,
    publish_layout_message,
    sep_tight,
    with_control_separators,
)

if TYPE_CHECKING:
    from ..reviews import Reviews


def critiques_summary_line(
    hit: MediaHit,
    *,
    count: int,
    avg: float | None,
    page: int = 0,
    total_pages: int = 1,
    skin: StarSkin | str | None = None,
) -> str:
    line = f"-# {count} critique(s) · moyenne {format_stars(avg or 0, skin)} {format_score(avg or 0, average=True)}"
    if count:
        line += f" · page {page + 1}/{total_pages}"
    if hit.url:
        line += f"  ·  [{_link_label(hit)}]({hit.url})"
    return line


def section_with_thumbnail(text: str, url: str | None) -> discord.ui.Item:
    body = discord.ui.TextDisplay(text)
    if not url:
        return body
    try:
        return discord.ui.Section(body, accessory=discord.ui.Thumbnail(url))
    except Exception:
        return body


def chunk_text_displays(lines: list[str], *, limit: int = TEXT_DISPLAY_MAX) -> list[discord.ui.TextDisplay]:
    """Regroupe des lignes en TextDisplay sans dépasser la limite Discord."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        piece = line if len(line) <= limit else pretty.shorten_text(line, limit)
        addition = f"{current}\n{piece}" if current else piece
        if current and len(addition) > limit:
            chunks.append(current)
            current = piece
        else:
            current = addition
    if current:
        chunks.append(current)
    return [discord.ui.TextDisplay(chunk) for chunk in chunks]


SOURCE_NAMES = {
    "tmdb": "TMDB",
    "steam": "Steam",
    "spotify": "Spotify",
    "openlibrary": "Open Library",
}


def _source_name(hit: MediaHit) -> str:
    return SOURCE_NAMES.get(hit.source, hit.source)


def _official_line(hit: MediaHit, skin: StarSkin | str | None = None) -> str:
    source = _source_name(hit)
    if hit.source == "tmdb":
        rating = float(hit.extra.get("vote_average") or 0)
        count = int(hit.extra.get("vote_count") or 0)
        if rating:
            stars = format_stars(rating, skin)
            votes = f"  ·  {fmt_int(count)} votes" if count else ""
            return f"**{source}** · {stars}  **{rating:.1f}/10**{votes}"
    if hit.source == "steam":
        label = hit.extra.get("review_label") or ""
        emoji = hit.extra.get("review_emoji") or ""
        if label:
            return f"**{source}** · {f'{emoji} {label}'.strip()}"
    popularity = hit.extra.get("popularity")
    if hit.media_type == "track" and popularity:
        return f"**{source}** · Popularité **{popularity}/100**"
    return ""


def _price_line(hit: MediaHit) -> str:
    if hit.media_type != "game":
        return ""
    if hit.extra.get("is_free"):
        return "**Gratuit**"
    final = hit.extra.get("price_final")
    if not isinstance(final, int):
        return ""
    text = f"**{final / 100:.2f} €**"
    discount = hit.extra.get("discount") or 0
    initial = hit.extra.get("price_initial")
    if discount and isinstance(initial, int):
        text += f"  ~~{initial / 100:.2f} €~~  {SALE} **-{discount}%**"
    return text


def _runtime_label(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}" if hours else f"{mins} min"


def fiche_title_line(hit: MediaHit) -> str:
    line = f"## {type_emoji(hit.media_type)} {hit.title}"
    if hit.year:
        line += f"  ·  {hit.year}"
    return line


def fiche_meta_line(hit: MediaHit) -> str:
    parts = [type_label(hit.media_type)]
    if hit.subtitle:
        parts.append(hit.subtitle)
    parts.extend(hit.genres[:3])
    return "  ·  ".join(parts)


def fiche_footer_line(hit: MediaHit) -> str:
    parts: list[str] = []
    extra = hit.extra
    if extra.get("runtime"):
        parts.append(_runtime_label(extra["runtime"]))
    if extra.get("season") is not None:
        episodes = int(extra.get("episodes") or 0)
        if episodes:
            parts.append(f"{episodes} épisode{'s' if episodes != 1 else ''}")
    elif extra.get("seasons"):
        seasons = extra["seasons"]
        parts.append(f"{seasons} saison{'s' if seasons > 1 else ''}")
    if extra.get("director"):
        parts.append(extra["director"])
    created = extra.get("created_by") or []
    if created and not extra.get("director"):
        parts.append(", ".join(created[:2]))
    cast = extra.get("cast") or []
    if cast:
        parts.append(", ".join(cast))
    if extra.get("album"):
        parts.append(extra["album"])
    if extra.get("duration"):
        parts.append(extra["duration"])
    if extra.get("explicit"):
        parts.append(EXPLICIT)
    if extra.get("total_tracks"):
        tracks = extra["total_tracks"]
        parts.append(f"{tracks} piste{'s' if tracks > 1 else ''}")
    lang = extra.get("original_language") or ""
    if lang and lang != "fr":
        parts.append(lang.upper())
    if extra.get("trailer"):
        parts.append(f"[Bande-annonce]({extra['trailer']})")
    if hit.url:
        parts.append(f"[{_source_name(hit)}]({hit.url})")
    elif hit.source:
        parts.append(_source_name(hit))
    return "  ·  ".join(parts)


def append_fiche_sections(
    body: list[discord.ui.Item],
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    my_review: dict | None,
    social_line: str = "",
    guild_name: str = "",
    skin: StarSkin | str | None = None,
    pack_with_poster: bool = False,
) -> None:
    head: list[str] = []
    official = _official_line(hit, skin)
    if official:
        head.append(official)
    if count:
        stars = format_stars(avg or 0, skin)
        label = pretty.shorten_text((guild_name or "Serveur").strip() or "Serveur", 40)
        head.append(
            f"**{label}** · {stars}  **{format_score(avg or 0, average=True)}**  ·  "
            f"{count} critique{'s' if count > 1 else ''}"
        )
    else:
        head.append("*Aucune note sur ce serveur pour l'instant.*")
    if my_review:
        comment = format_comment(
            my_review.get("comment") or "",
            spoiler=row_spoiler(my_review),
            hide=False,
            limit=180,
        )
        mine = f"__**Ta note**__ · {format_stars(my_review['rating'], skin)}  **{format_score(my_review['rating'])}**"
        if comment:
            mine += f"\n{comment}"
        seen = experienced_line(hit.media_type, experienced_from_row(my_review))
        if seen:
            mine += f"\n{seen}"
        head.append(mine)
    if social_line:
        head.append(f"-# {social_line}")
    price = _price_line(hit)
    if price:
        head.append(price)

    tail: list[str] = []
    overview = pretty.shorten_text(hit.overview, 380) if hit.overview else ""
    if overview:
        quoted = "\n".join(
            f"> *{line}*" if line.strip() else ">"
            for line in overview.splitlines()
        )
        tail.append(quoted)
    elif not official and not count:
        tail.append("-# Aucune description disponible.")

    if pack_with_poster and hit.poster_url and (head or tail):
        blocks = []
        if head:
            blocks.append("\n".join(head))
        blocks.extend(tail)
        try:
            body.append(discord.ui.Section(
                *blocks[:3],
                accessory=discord.ui.Thumbnail(hit.poster_url),
            ))
            return
        except Exception:
            pass

    body.append(section_with_thumbnail("\n".join(head), hit.poster_url))
    if tail:
        body.append(discord.ui.TextDisplay("\n".join(tail)))


def also_liked_line(hit: MediaHit, items: list[tuple[MediaHit, float, int]]) -> str:
    """Une ligne, au même endroit que « X a mis 10/10 »."""
    if not items:
        return ""
    titles = [f"**{pretty.shorten_text(liked.title, 42)}**" for liked, _avg, _people in items[:3]]
    if len(titles) == 1:
        also = titles[0]
    elif len(titles) == 2:
        also = f"{titles[0]} et {titles[1]}"
    else:
        also = f"{', '.join(titles[:-1])} et {titles[-1]}"
    source = pretty.shorten_text(hit.title, 42)
    return f"Ceux qui ont aimé **{source}** ont aussi aimé {also}"


def stream_status_text(hit: MediaHit) -> str:
    title = (hit.title or "Sans titre").strip()
    label = f"{title} ({hit.year})" if hit.year else title
    return pretty.shorten_text(label, 100)


def stream_mini_items(hit: MediaHit) -> list[discord.ui.Item]:
    year = f" ({hit.year})" if hit.year else ""
    text = (
        f"{type_emoji(hit.media_type)} **{hit.title}**{year}\n"
        f"-# {fiche_meta_line(hit)}"
    )
    return [section_with_thumbnail(text, hit.poster_url)]


def fiche_plan_payload(plans: list[dict] | None) -> list[dict]:
    slim: list[dict] = []
    for plan in plans or []:
        slim.append({
            "user_id": int(plan["user_id"]),
            "starts_at": int(plan["starts_at"]),
            "interest": int(plan.get("interest") or 0),
        })
    return slim


def plan_banner_lines(plans: list[dict] | None) -> list[str]:
    now = int(time.time())
    lines: list[str] = []
    for plan in plans or []:
        starts = int(plan.get("starts_at") or 0)
        if starts <= now:
            continue
        when = datetime.fromtimestamp(starts, PARIS_TZ)
        if when.date() == datetime.now(PARIS_TZ).date():
            moment = f"à <t:{starts}:t>"
        else:
            moment = f"le <t:{starts}:F>"
        lines.append(
            f"{PLANNING} Stream planifié par <@{int(plan['user_id'])}> {moment}"
        )
    return lines


def stream_live_items(
    channel_ids: list[int] | None = None,
    plans: list[dict] | None = None,
) -> list[discord.ui.Item]:
    lines: list[str] = []
    if channel_ids:
        salons = " · ".join(f"<#{cid}>" for cid in channel_ids)
        lines.append(f"{STREAMING} Actuellement en stream sur {salons}")
    lines.extend(plan_banner_lines(plans))
    if not lines:
        return []
    return [discord.ui.TextDisplay("\n".join(lines)), sep_tight()]


def fiche_intro(
    hit: MediaHit,
    *,
    backdrop: bool = True,
    replace_title: discord.ui.Item | None = None,
) -> list[discord.ui.Item]:
    meta = fiche_meta_line(hit)
    if replace_title is not None:
        items: list[discord.ui.Item] = [replace_title]
        if meta:
            items.append(discord.ui.TextDisplay(f"-# {meta}"))
    else:
        text = fiche_title_line(hit)
        if meta:
            text += f"\n-# {meta}"
        items = [discord.ui.TextDisplay(text)]
    if not backdrop:
        return items
    backdrop_url = hit.extra.get("backdrop_url")
    if backdrop_url:
        try:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(backdrop_url)))
        except Exception:
            pass
    return items


def _link_label(hit: MediaHit) -> str:
    return SOURCE_NAMES.get(hit.source, "Fiche")


def render_published_fiche(
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    social: str,
    wid: str,
    live: bool,
    banner: str = "",
    stream_channels: list[int] | None = None,
    plans: list[dict] | None = None,
    guild_name: str = "",
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    body: list[discord.ui.Item] = []
    body.extend(stream_live_items(stream_channels or [], plans))
    if banner:
        body.append(discord.ui.TextDisplay(banner))
        body.append(sep_tight())
    body.extend(fiche_intro(hit))
    append_fiche_sections(
        body, hit, avg=avg, count=count, my_review=None, social_line=social, guild_name=guild_name,
    )
    footer = fiche_footer_line(hit)
    if footer:
        body.append(discord.ui.TextDisplay(f"-# {footer}"))
    if live:
        body.append(discord.ui.ActionRow(
            FicheDynButton(wid, "critiques", label=f"Critiques ({count})"),
            FicheDynButton(wid, "noter", label="Actions", emoji=MORE, style=discord.ButtonStyle.green),
        ))
    view.add_item(discord.ui.Container(*with_control_separators(body)))
    return view


def render_published_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    return render_published_fiche(
        hit_from_dict(raw),
        avg=rec.payload.get("avg"),
        count=int(rec.payload.get("count") or 0),
        social=str(rec.payload.get("social") or ""),
        wid=rec.id,
        live=live,
        plans=list(rec.payload.get("plans") or []),
        guild_name=str(rec.payload.get("guild_name") or ""),
    )


async def prepare_published_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    *,
    banner: str = "",
) -> tuple[discord.ui.LayoutView, str]:
    if cog.catalog is not None:
        try:
            hit = await cog.catalog.enrich(hit)
        except Exception:
            logger.exception("Enrichissement de fiche publiée impossible")
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = await cog.public_fiche_line(guild, hit, reviews, media_id)
    stream_channels = await cog.stream_channels_for_hit(guild, hit)
    plans = fiche_plan_payload(await cog.plans_for_hit(guild, hit))
    wid = create_record({
        "kind": "fiche",
        "guild_id": guild.id,
        "guild_name": guild.name,
        "hit": hit_to_dict(hit),
        "avg": avg,
        "count": count,
        "social": social,
        "plans": plans,
    })
    view = render_published_fiche(
        hit,
        avg=avg,
        count=count,
        social=social,
        wid=wid,
        live=True,
        banner=banner,
        stream_channels=stream_channels,
        plans=plans,
        guild_name=guild.name,
    )
    return view, wid


async def send_published_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    interaction: discord.Interaction,
    *,
    close_ephemeral: bool = False,
) -> discord.Message | None:
    view, wid = await prepare_published_fiche(cog, guild, hit)
    message = await publish_layout_message(interaction, view)
    if message is None:
        mark_stripped(wid)
        try:
            await interaction.followup.send(
                "**Erreur ·** Impossible de publier dans ce salon.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return None
    bind_record(wid, message.channel.id, message.id)
    if close_ephemeral:
        await discard_ephemeral_menu(interaction)
    return message


async def post_published_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    channel: discord.abc.Messageable,
    *,
    banner: str = "",
    reference: discord.Message | None = None,
) -> discord.Message | None:
    view, wid = await prepare_published_fiche(cog, guild, hit, banner=banner)
    try:
        kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
        if reference is not None:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False
        message = await channel.send(**kwargs)
    except discord.HTTPException as exc:
        mark_stripped(wid)
        logger.info("Impossible de poster la fiche stream : %s", exc)
        return None
    bind_record(wid, message.channel.id, message.id)
    return message


def render_compact_fiche(
    hit: MediaHit,
    *,
    avg: float | None,
    count: int,
    social: str,
    stream_channels: list[int] | None = None,
    plans: list[dict] | None = None,
    guild_name: str = "",
    wid: str | None = None,
) -> discord.ui.LayoutView:
    """Fiche postée en réponse à un lien : sans image large, avec le bouton Actions."""
    view = discord.ui.LayoutView(timeout=None)
    body: list[discord.ui.Item] = []
    body.extend(stream_live_items(stream_channels or [], plans))
    body.extend(fiche_intro(hit, backdrop=False))
    append_fiche_sections(
        body, hit, avg=avg, count=count, my_review=None, social_line=social, guild_name=guild_name,
        pack_with_poster=True,
    )
    footer = fiche_footer_line(hit)
    if footer:
        body.append(discord.ui.TextDisplay(f"-# {footer}"))
    if body:
        view.add_item(discord.ui.Container(*body))
    if wid:
        view.add_item(discord.ui.ActionRow(
            FicheDynButton(wid, "noter", label="Actions", emoji=MORE),
        ))
    return view


async def post_compact_fiche(
    cog: "Reviews",
    guild: discord.Guild,
    hit: MediaHit,
    channel: discord.abc.Messageable,
    *,
    reference: discord.Message | None = None,
) -> discord.Message | None:
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = await cog.public_fiche_line(guild, hit, reviews, media_id)
    stream_channels = await cog.stream_channels_for_hit(guild, hit)
    plans = fiche_plan_payload(await cog.plans_for_hit(guild, hit))
    wid = create_record({
        "kind": "compact",
        "guild_id": guild.id,
        "guild_name": guild.name,
        "hit": hit_to_dict(hit),
        "avg": avg,
        "count": count,
        "social": social,
        "stream_channels": stream_channels,
        "plans": plans,
    })
    view = render_compact_fiche(
        hit,
        avg=avg,
        count=count,
        social=social,
        stream_channels=stream_channels,
        plans=plans,
        guild_name=guild.name,
        wid=wid,
    )
    try:
        kwargs: dict[str, Any] = {"view": view, "allowed_mentions": NO_PINGS}
        if reference is not None:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False
        message = await channel.send(**kwargs)
    except discord.HTTPException as exc:
        mark_stripped(wid)
        logger.info("Impossible de poster la fiche compacte : %s", exc)
        return None
    bind_record(wid, message.channel.id, message.id)
    return message


async def sync_published_fiche(cog: "Reviews", guild: discord.Guild, wid: str, hit: MediaHit) -> None:
    rec = get_record(wid)
    if not is_live(rec) or rec is None:
        return
    media_id = await cog.lookup_media_id(guild, hit)
    avg, count = await cog.media_stats(guild, media_id) if media_id else (None, 0)
    reviews = await cog.list_reviews(guild, media_id) if media_id else []
    social = await cog.public_fiche_line(guild, hit, reviews, media_id)
    plans = fiche_plan_payload(await cog.plans_for_hit(guild, hit))
    rec.payload.update({
        "hit": hit_to_dict(hit),
        "avg": avg,
        "count": count,
        "social": social,
        "guild_name": guild.name,
        "plans": plans,
    })
    update_payload(wid, rec.payload)
    stream_channels = await cog.stream_channels_for_hit(guild, hit)
    view = render_published_fiche(
        hit,
        avg=avg,
        count=count,
        social=social,
        wid=wid,
        live=True,
        stream_channels=stream_channels,
        plans=plans,
        guild_name=guild.name,
    )
    if not rec.channel_id or not rec.message_id:
        return
    try:
        channel = cog.bot.get_channel(rec.channel_id) or await cog.bot.fetch_channel(rec.channel_id)
        message = await channel.fetch_message(rec.message_id)
        await message.edit(view=view, allowed_mentions=NO_PINGS)
    except Exception as exc:
        logger.info("Maj fiche publiée %s : %s", wid, exc)


# ---------------------------------------------------------------------------
# Annonce publique (présentation seule)
# ---------------------------------------------------------------------------

def build_announce_view(
    hit: MediaHit,
    *,
    mention: str,
    title: str,
    rating: float,
    comment: str,
    updated: bool,
    experienced_at: str = "",
    spoiler: bool = False,
    posted_at: int | None = None,
    wid: str | None = None,
    live: bool = False,
    skin: StarSkin | str | None = None,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    verb = "a mis à jour sa note" if updated else "a noté"
    stamped = f"<t:{int(posted_at or time.time())}:f>"
    profile = f"{mention} · _{title}_ · le {stamped} · {verb}"
    film = f"{fiche_title_line(hit)}\n{format_stars(rating, skin)}  **{format_score(rating)}**"
    shown = format_comment(comment, spoiler=spoiler, hide=True, limit=240)
    if shown:
        film += f"\n{shown}"
    seen = experienced_line(hit.media_type, experienced_at)
    if seen:
        film += f"\n{seen}"
    container.add_item(discord.ui.TextDisplay(profile))
    container.add_item(discord.ui.Separator())
    container.add_item(section_with_thumbnail(film, hit.poster_url))
    container.add_item(discord.ui.TextDisplay(f"-# {fiche_meta_line(hit)}" + (f"  ·  [{_link_label(hit)}]({hit.url})" if hit.url else "")))
    view.add_item(container)
    if live and wid:
        view.add_item(discord.ui.ActionRow(AnnounceDynButton(wid, label="Actions")))
    return view


def render_announce_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    posted = rec.payload.get("posted_at")
    try:
        posted_at = int(posted) if posted else None
    except (TypeError, ValueError):
        posted_at = None
    return build_announce_view(
        hit_from_dict(raw),
        mention=str(rec.payload.get("mention") or ""),
        title=str(rec.payload.get("title") or ""),
        rating=float(rec.payload.get("rating") or 0),
        comment=str(rec.payload.get("comment") or ""),
        updated=bool(rec.payload.get("updated")),
        experienced_at=str(rec.payload.get("experienced_at") or ""),
        spoiler=bool(rec.payload.get("spoiler")),
        posted_at=posted_at,
        wid=rec.id,
        live=live,
        skin=rec.payload.get("star_skin"),
    )


def render_compact_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    raw = rec.payload.get("hit")
    if not isinstance(raw, dict):
        return None
    channels = rec.payload.get("stream_channels") or []
    return render_compact_fiche(
        hit_from_dict(raw),
        avg=rec.payload.get("avg"),
        count=int(rec.payload.get("count") or 0),
        social=str(rec.payload.get("social") or ""),
        stream_channels=[int(channel_id) for channel_id in channels if channel_id],
        plans=list(rec.payload.get("plans") or []),
        guild_name=str(rec.payload.get("guild_name") or ""),
        wid=rec.id if live else None,
    )


def render_dyn_record(rec: FicheRecord, *, live: bool) -> discord.ui.LayoutView | None:
    if rec.payload.get("kind") == "announce":
        return render_announce_record(rec, live=live)
    if rec.payload.get("kind") == "compact":
        return render_compact_record(rec, live=live)
    return render_published_record(rec, live=live)
