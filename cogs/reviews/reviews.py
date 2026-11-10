"""Cog Critiques — carnet de notes type Senscritique / Letterboxd."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import dataio, pretty

from .constants import NO_PINGS, TYPE_CHOICES, TYPE_META, logger
from .database import prepare_database
from .dyn import (
    ANNOUNCE_TTL,
    AnnounceDynButton,
    bind_record,
    create_record,
    FicheDynButton,
    FicheRecord,
    find_recent_announce,
    hit_to_dict,
    mark_stripped,
    refresh_record,
    sweep_expired,
    update_payload,
)
from .formatting import (
    RATING_CHOICES,
    autocomplete_query_value,
    prefix_autocomplete_hint,
    format_score,
    hit_identity,
    mention_of,
    season_media,
    skip_note_autocomplete,
    type_label,
)
from .links import MediaLink, find_media_links, message_is_only_links, pick_fiche_hit
from .importer import MAX_IMPORT_BYTES, ImportError_, ImportMixin, parse_letterboxd, report_text
from .live import LiveMixin
from .prefs import UserPrefs, search_pref_includes
from .progress import resolve_star_skin, title_for_level
from .providers import MediaCatalog, MediaHit, parse_search_query
from .storage import StorageMixin
from .streaming import has_streaming_activity, member_stream_source
from .ui.base import (
    member_accent,
    present_ephemeral_layout,
    replace_ephemeral_layout,
    ReviewsLayout,
)
from .ui.fiche import build_announce_view, post_compact_fiche, render_dyn_record
from .ui.lists import ListsHubView, SharedListView
from .ui.preferences import PreferencesView
from .ui.profile import ProfileView
from .ui.server import ServerHubView
from .ui.session import FicheView
from .ui.quests import QuestsView
from .ui.settings import HelpView, ReviewsConfigView
from .ui.streams import StreamBindView, StreamHubView


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Reviews(StorageMixin, LiveMixin, ImportMixin, commands.Cog):
    """Carnet de critiques commun à tous les serveurs (films, séries, jeux, musique, livres)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)
        self._http: aiohttp.ClientSession | None = None
        self.catalog: MediaCatalog | None = None  # type: ignore[assignment]
        self._comment_max: dict[int, int] = {}
        self._prefs: dict[int, UserPrefs] = {}
        self._known_members: set[tuple[int, int]] = set()
        self._imports: set[int] = set()  # membres avec un import Letterboxd en cours
        self._stream_end_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._stream_remind_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._stream_session_reminded: set[tuple[int, int]] = set()

    async def cog_load(self) -> None:
        await asyncio.to_thread(prepare_database, self.data.cog_folder / "data")
        config = dict(getattr(self.bot, "config", {}) or {})
        try:
            from dotenv import dotenv_values
            for key, value in dotenv_values(".env").items():
                if value:
                    config[key] = value
        except Exception:
            pass
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8, sock_connect=3, sock_read=6),
            headers={"User-Agent": "CRIT-BOT/1.0 (Discord reviews)"},
        )
        self.catalog = MediaCatalog(
            self._http,
            tmdb_key=str(config.get("TMDB_API_KEY") or "").strip(),
            spotify_id=str(config.get("SPOTIFY_CLIENT_ID") or "").strip(),
            spotify_secret=str(config.get("SPOTIFY_CLIENT_SECRET") or "").strip(),
            google_books_key=str(config.get("GOOGLE_BOOKS_API_KEY") or "").strip(),
        )
        status = self.catalog.status()
        missing = [name for name, ok in status.items() if not ok]
        if missing:
            logger.warning("Fournisseurs incomplets : %s", ", ".join(missing))
        self.bot.add_dynamic_items(FicheDynButton, AnnounceDynButton)
        self._carnet_menu = app_commands.ContextMenu(
            name="Voir le carnet",
            callback=self.critique_carnet_user,
        )
        existing = self.bot.tree.get_command("Voir le carnet", type=discord.AppCommandType.user)
        if existing is not None:
            self.bot.tree.remove_command("Voir le carnet", type=discord.AppCommandType.user)
        self.bot.tree.add_command(self._carnet_menu)
        self._fiche_menu = app_commands.ContextMenu(
            name="Ouvrir la fiche",
            callback=self.open_message_fiche,
        )
        self.bot.tree.remove_command("Ouvrir la fiche", type=discord.AppCommandType.message)
        self.bot.tree.add_command(self._fiche_menu)
        self._sweep_fiches.start()
        if self.bot.is_ready():  # rechargement du cog à chaud
            asyncio.create_task(self._sync_all_members(), name="crit-sync-members")

    async def cog_unload(self) -> None:
        self._sweep_fiches.cancel()
        for task in list(self._stream_end_tasks.values()):
            task.cancel()
        self._stream_end_tasks.clear()
        for task in list(self._stream_remind_tasks.values()):
            task.cancel()
        self._stream_remind_tasks.clear()
        self._stream_session_reminded.clear()
        self.bot.remove_dynamic_items(FicheDynButton, AnnounceDynButton)
        self.bot.tree.remove_command("Voir le carnet", type=discord.AppCommandType.user)
        self.bot.tree.remove_command("Ouvrir la fiche", type=discord.AppCommandType.message)
        if self._http is not None:
            await self._http.close()
            self._http = None
        await self.data.close_all()

    @tasks.loop(seconds=30)
    async def _sweep_fiches(self) -> None:
        try:
            await sweep_expired(self.bot, render_dyn_record)
        except Exception:
            logger.exception("sweep fiches publiées")
        try:
            await self._sweep_stream_plans()
        except Exception:
            logger.exception("sweep streams planifiés")

    @_sweep_fiches.before_loop
    async def _before_sweep_fiches(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        was_live = bool(before.self_stream)
        still_live = bool(after.self_stream and after.channel)
        if still_live and after.channel is not None:
            moved = before.channel is None or before.channel.id != after.channel.id
            if moved:
                await self._update_stream_channel(member.guild, member.id, after.channel.id)
            if not was_live:
                await self._on_stream_started(member, "voice")
            return
        if was_live and not still_live:
            await self._on_stream_stopped(member, "voice")

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or after.guild is None:
            return
        was_activity = has_streaming_activity(before)
        still_activity = has_streaming_activity(after)
        if still_activity and not was_activity:
            if member_stream_source(after) == "activity":
                await self._on_stream_started(after, "activity")
            return
        if was_activity and not still_activity and member_stream_source(after) is None:
            await self._on_stream_stopped(after, "activity")

    async def _sync_all_members(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.sync_members(guild)
            except Exception:
                logger.exception("Synchro des membres de %s", guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._sync_all_members()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot and await self.has_carnet(member.id):
            await self.remember_member(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.forget_member(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id and not interaction.user.bot:
            await self.remember_member(interaction.guild_id, interaction.user.id)

    async def resolve_link(self, link: MediaLink) -> MediaHit | None:
        """Œuvre d'un lien, enrichie (saison comprise pour un lien TMDB)."""
        if self.catalog is None:
            return None
        try:
            hit = pick_fiche_hit(await self.catalog.search(link.query, "all"))
            if hit is None:
                return None
            hit = await self.catalog.enrich(hit)
        except Exception:
            logger.exception("Lien d'œuvre %s", link.query)
            return None
        if link.season is not None and hit.media_type == "tv":
            hit = season_media(hit, link.season)
        return hit

    async def resolve_links(self, content: str) -> list[MediaHit]:
        hits: list[MediaHit] = []
        for link in find_media_links(content):
            hit = await self.resolve_link(link)
            if hit is not None and all(hit_identity(hit) != hit_identity(other) for other in hits):
                hits.append(hit)
        return hits

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Un message qui n'est qu'un lien d'œuvre est un partage : on y répond par la fiche,
        # seulement si l'œuvre a déjà été notée sur ce serveur. Sinon (ou dans une phrase),
        # le lien reste accessible par clic droit → Ouvrir la fiche.
        guild = message.guild
        if message.author.bot or guild is None or not message_is_only_links(message.content):
            return
        posted: set[tuple[str, str, str]] = set()
        for link in find_media_links(message.content):
            # Lien avec id : on vérifie la base avant tout appel au catalogue.
            if link.identity is not None and not await self.rated_on_server(guild, link.identity):
                continue
            hit = await self.resolve_link(link)
            if hit is None or hit.identity in posted or not await self.rated_on_server(guild, hit.identity):
                continue
            posted.add(hit.identity)
            await post_compact_fiche(self, guild, hit, message.channel, reference=message)

    @app_commands.guild_only()
    async def open_message_fiche(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Menu clic droit : ouvre la fiche des œuvres liées dans un message."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return
        if not find_media_links(message.content):
            await interaction.response.send_message(
                "**Fiche ·** Aucun lien d'œuvre dans ce message (TMDB, IMDb, Letterboxd, Steam, Spotify, Open Library, Google Books, AniList).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        hits = await self.resolve_links(message.content)
        if not hits:
            await interaction.edit_original_response(content="**Introuvable ·** Aucune œuvre pour ce lien.")
            return
        view = FicheView(self, guild, hits, author_id=interaction.user.id)
        await view.start(interaction, deferred=True)

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # XP / affinités
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Persistance médias / critiques
    # ------------------------------------------------------------------

    async def announce_review(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        hit: MediaHit,
        rating: float,
        comment: str,
        *,
        updated: bool,
        experienced_at: str = "",
        spoiler: bool = False,
    ) -> None:
        prefs = await self.get_user_prefs(guild, user.id)
        if not prefs.announce_notes:
            return
        channel = await self.get_announce_channel(guild, hit.media_type)
        if channel is None:
            return
        titles = await self.get_titles(guild, [user.id])
        mention = mention_of(guild, self.bot, user.id)
        grade = titles.get(user.id, title_for_level(1))
        if updated:
            previous = find_recent_announce(guild_id=guild.id, user_id=user.id, hit=hit)
            if previous is not None and await self._edit_announce(
                previous,
                hit,
                mention=mention,
                title=grade,
                rating=rating,
                comment=comment,
                experienced_at=experienced_at,
                spoiler=spoiler,
            ):
                return
        skin = await self.star_skin_for(guild, user.id)
        posted_at = int(time.time())
        wid = create_record(
            {
                "kind": "announce",
                "guild_id": guild.id,
                "user_id": user.id,
                "hit": hit_to_dict(hit),
                "mention": mention,
                "title": grade,
                "rating": rating,
                "comment": comment,
                "updated": updated,
                "experienced_at": experienced_at,
                "spoiler": spoiler,
                "posted_at": posted_at,
                "star_skin": skin.id,
            },
            ttl=ANNOUNCE_TTL,
        )
        view = build_announce_view(
            hit,
            mention=mention,
            title=grade,
            rating=rating,
            comment=comment,
            updated=updated,
            experienced_at=experienced_at,
            spoiler=spoiler,
            posted_at=posted_at,
            wid=wid,
            live=True,
            skin=skin,
        )
        try:
            message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            mark_stripped(wid)
            logger.error("Impossible d'annoncer une critique sur %s : %s", guild.name, exc)
            return
        bind_record(wid, message.channel.id, message.id)

    async def _edit_announce(
        self,
        rec: FicheRecord,
        hit: MediaHit,
        *,
        mention: str,
        title: str,
        rating: float,
        comment: str,
        experienced_at: str,
        spoiler: bool,
    ) -> bool:
        if not rec.channel_id or not rec.message_id:
            return False
        try:
            posted_at = int(rec.payload.get("posted_at") or time.time())
        except (TypeError, ValueError):
            posted_at = int(time.time())
        was_update = bool(rec.payload.get("updated"))
        guild_id = int(rec.payload.get("guild_id") or 0)
        user_id = int(rec.payload.get("user_id") or 0)
        guild = self.bot.get_guild(guild_id)
        if guild is not None and user_id:
            skin = await self.star_skin_for(guild, user_id)
        else:
            skin = resolve_star_skin(rec.payload.get("star_skin"))
        rec.payload.update(
            {
                "hit": hit_to_dict(hit),
                "mention": mention,
                "title": title,
                "rating": rating,
                "comment": comment,
                "experienced_at": experienced_at,
                "spoiler": spoiler,
                "star_skin": skin.id,
            }
        )
        update_payload(rec.id, rec.payload)
        refresh_record(rec.id, ttl=ANNOUNCE_TTL)
        view = build_announce_view(
            hit,
            mention=mention,
            title=title,
            rating=rating,
            comment=comment,
            updated=was_update,
            experienced_at=experienced_at,
            spoiler=spoiler,
            posted_at=posted_at,
            wid=rec.id,
            live=True,
            skin=skin,
        )
        try:
            channel = self.bot.get_channel(rec.channel_id) or await self.bot.fetch_channel(rec.channel_id)
            message = await channel.fetch_message(rec.message_id)
            await message.edit(view=view, allowed_mentions=NO_PINGS)
            return True
        except Exception as exc:
            logger.info("Maj annonce %s : %s", rec.id, exc)
            return False

    async def search_hits(self, query: str, media_type: str) -> tuple[list[MediaHit], str]:
        """Résultats du catalogue, ou un message d'erreur prêt à afficher."""
        spec = parse_search_query(query)
        if not spec.lookup_id and len(spec.query.strip()) < 2:
            return [], "**Erreur ·** La recherche doit contenir au moins 2 caractères."
        if self.catalog is None:
            return [], "**Erreur ·** Catalogue média indisponible."
        wants_tmdb = spec.source in (None, "tmdb") and (
            spec.lookup_id
            or media_type == "all"
            or search_pref_includes(media_type, "movie", "tv")
            or spec.media_type in ("movie", "tv")
        )
        tmdb_missing = "**Erreur ·** Clé TMDB manquante (`TMDB_API_KEY` dans `.env`)."
        if wants_tmdb and spec.source == "tmdb" and not self.catalog.tmdb.available:
            return [], tmdb_missing
        if search_pref_includes(media_type, "movie", "tv") and spec.source is None and not self.catalog.tmdb.available:
            return [], tmdb_missing
        wants_spotify = spec.source == "spotify" or (
            spec.source is None and search_pref_includes(media_type, "album", "track")
        )
        if wants_spotify and not self.catalog.spotify.available:
            return [], "**Erreur ·** Clés Spotify manquantes (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` dans `.env`)."
        try:
            hits = await self.catalog.search(query.strip(), media_type)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Recherche échouée : %s", exc)
            return [], "**Erreur ·** La recherche a échoué. Réessaie dans un instant."
        if not hits:
            return [], f"**Erreur ·** Aucun résultat pour « {pretty.shorten_text(query, 80)} »."
        return hits, ""

    async def _search_or_reply(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str,
    ) -> list[MediaHit] | None:
        """Pour une commande : l'erreur remplace la réponse en attente."""
        hits, error = await self.search_hits(query, media_type)
        if error:
            await interaction.edit_original_response(content=error)
            return None
        return hits

    # ==================================================================
    # Commandes
    # ==================================================================

    @app_commands.command(name="search")
    @app_commands.guild_only()
    @app_commands.rename(query="recherche", media_type="type", rating="note", comment="commentaire")
    @app_commands.describe(
        query="Titre, année, ou préfixe (tmdb:Dune, tmdb:27205, URL TMDB)",
        media_type="Restreindre à un type (sinon tes types dans /reglages)",
        rating="Note de 0 à 10 (entier, une étoile = 2 points)",
        comment="Court commentaire optionnel",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, rating=RATING_CHOICES)
    async def critique_search(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: str | None = None,
        rating: float | None = None,
        comment: str | None = None,
    ) -> None:
        """Recherche une œuvre : ouvrir la fiche, noter, ou ajouter un signet."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        resolved_type = await self._resolve_search_type(guild, interaction.user.id, media_type)
        hits = await self._search_or_reply(interaction, query, resolved_type)
        if not hits:
            return
        view = FicheView(
            self,
            guild,
            hits,
            author_id=interaction.user.id,
            pending_rating=rating,
            pending_comment=(comment or "").strip(),
        )
        await view.start(interaction, deferred=True)

    @critique_search.autocomplete("query")
    async def search_query_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip()
        if skip_note_autocomplete(current) or self.catalog is None:
            # Préfixe (`gb:`, `manga:`…) ou lien : une ligne indique ce qui sera cherché.
            hint = prefix_autocomplete_hint(current)
            return [app_commands.Choice(name=hint[0], value=hint[1])] if hint else []
        ns = getattr(interaction, "namespace", None)
        explicit = None
        if ns is not None:
            explicit = getattr(ns, "media_type", None) or getattr(ns, "type", None)
        guild = interaction.guild
        if isinstance(guild, discord.Guild):
            media_type = await self._resolve_search_type(guild, interaction.user.id, explicit)
        else:
            media_type = explicit if explicit in TYPE_META or explicit == "all" else "all"
        try:
            hits = await asyncio.wait_for(
                self.catalog.search(current, media_type, quick=True),
                timeout=2.0,
            )
        except Exception:
            return []
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for hit in hits:
            value = autocomplete_query_value(hit)
            if not value or value in seen:
                continue
            seen.add(value)
            year = f" ({hit.year})" if hit.year else ""
            name = pretty.shorten_text(f"{hit.title}{year} · {type_label(hit.media_type)}", 100)
            choices.append(app_commands.Choice(name=name, value=value[:100]))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="stream")
    @app_commands.guild_only()
    @app_commands.rename(query="recherche", media_type="type")
    @app_commands.describe(
        query="Œuvre à lier à ton live (vide = lives en cours)",
        media_type="Restreindre à un type (sinon tes types dans /reglages)",
    )
    @app_commands.choices(media_type=TYPE_CHOICES)
    async def critique_stream(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        media_type: str | None = None,
    ) -> None:
        """Voit les œuvres liées aux lives en cours, ou lie la tienne."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        raw = (query or "").strip()
        if not raw:
            await interaction.response.defer(ephemeral=True)
            view = await StreamHubView.create(self, guild, interaction.user.id)
            await replace_ephemeral_layout(interaction, view)
            return
        member = guild.get_member(interaction.user.id) or interaction.user
        if member_stream_source(member) is None:
            await interaction.response.send_message(
                "**Stream ·** Tu n'es pas en live. Lance un Go Live, puis relance `/stream`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        resolved_type = await self._resolve_search_type(guild, interaction.user.id, media_type)
        hits = await self._search_or_reply(interaction, raw, resolved_type)
        if not hits:
            return
        view = StreamBindView(
            self,
            guild,
            hits,
            author_id=interaction.user.id,
        )
        await view.start(interaction, deferred=True)

    @critique_stream.autocomplete("query")
    async def stream_query_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self.search_query_autocomplete(interaction, current)

    async def _open_carnet(
        self,
        interaction: discord.Interaction,
        target: discord.Member | discord.User,
    ) -> None:
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.",
                    ephemeral=True,
                )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        view = await ProfileView.create(
            self,
            guild,
            target,
            viewer_id=interaction.user.id,
            accent=await member_accent(self.bot, guild, target),
        )
        view._interaction = interaction
        await present_ephemeral_layout(interaction, view)

    @app_commands.command(name="carnet")
    @app_commands.guild_only()
    @app_commands.rename(member="membre")
    @app_commands.describe(member="Membre dont afficher le carnet, le journal, les signets et les affinités")
    async def critique_carnet(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Carnet d'un membre : profil, journal, signets et affinités."""
        await self._open_carnet(interaction, member or interaction.user)

    @app_commands.guild_only()
    async def critique_carnet_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member | discord.User,
    ) -> None:
        await self._open_carnet(interaction, member)

    @app_commands.command(name="explore")
    @app_commands.guild_only()
    @app_commands.rename(query="recherche", member="membre", media_type="type", min_rating="note_min")
    @app_commands.describe(
        query="Filtrer le catalogue déjà noté du serveur",
        member="Limiter aux notes d'un membre",
        media_type="Filtrer par type",
        min_rating="Note minimale (sur l'œuvre ou la critique)",
    )
    @app_commands.choices(media_type=TYPE_CHOICES, min_rating=RATING_CHOICES)
    async def critique_explore(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        member: discord.Member | None = None,
        media_type: str = "all",
        min_rating: float | None = None,
    ) -> None:
        """Feuillette le catalogue du serveur : récentes, meilleures, pires, et pour toi."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        catalog = await self.load_catalog(
            guild,
            query=query,
            member_id=member.id if member else None,
            media_type=media_type,
            min_rating=min_rating,
        )
        for_you, for_you_hint = await self.load_for_you(
            guild, interaction.user.id, media_type=media_type,
        )
        subtitle_parts = []
        if query:
            subtitle_parts.append(f"« {pretty.shorten_text(query, 60)} »")
        if member:
            subtitle_parts.append(member.display_name)
        if media_type != "all":
            subtitle_parts.append(type_label(media_type))
        if min_rating is not None:
            subtitle_parts.append(f"≥ {format_score(min_rating)}")
        view = ServerHubView(
            self,
            guild,
            catalog=catalog,
            for_you=for_you,
            for_you_hint=for_you_hint,
            catalog_subtitle="  ·  ".join(subtitle_parts) or "Toutes les œuvres notées",
            star_skin=await self.star_skin_for(guild, interaction.user.id),
        )
        view._interaction = interaction
        await present_ephemeral_layout(interaction, view)

    async def _shared_list_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return []
        try:
            lists = await self.load_shared_lists(guild)
        except Exception:
            return []
        needle = (current or "").casefold()
        choices: list[app_commands.Choice[str]] = []
        for record in lists:
            if needle and needle not in record["title"].casefold():
                continue
            name = pretty.shorten_text(
                f"{record['title']} · {record['item_count']} œuvre{'s' if record['item_count'] != 1 else ''}",
                100,
            )
            choices.append(app_commands.Choice(name=name, value=str(record["id"])))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="listes")
    @app_commands.guild_only()
    @app_commands.describe(liste="Ouvrir une liste directement")
    async def critique_listes(self, interaction: discord.Interaction, liste: str | None = None) -> None:
        """Listes communes du serveur : créer, éditer et tirer au hasard."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        list_id = int(liste) if liste and liste.isdigit() else None
        hub = await ListsHubView.create(self, guild, viewer_id=interaction.user.id)
        view: ReviewsLayout = hub
        if list_id:
            opened = await SharedListView.create(self, guild, list_id, viewer_id=interaction.user.id)
            if opened is None:
                hub.notice = "**Listes ·** Cette liste n'existe plus."
                hub._build()
            else:
                opened.parent = hub
                hub._covered = True
                opened._build()
                view = opened
        view._interaction = interaction
        await present_ephemeral_layout(interaction, view)

    @critique_listes.autocomplete("liste")
    async def listes_liste_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._shared_list_choices(interaction, current)

    @app_commands.command(name="reglages")
    @app_commands.guild_only()
    async def critique_reglages(self, interaction: discord.Interaction) -> None:
        """Tes réglages : recherche, date, annonces, listes et stream."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        view = await PreferencesView.create(self, guild, interaction.user)
        await view.start(interaction)

    @app_commands.command(name="importer")
    @app_commands.guild_only()
    @app_commands.describe(fichier="ZIP d'export Letterboxd (Réglages → Data → Export) ou CSV au format Letterboxd")
    async def critique_importer(self, interaction: discord.Interaction, fichier: discord.Attachment) -> None:
        """Importe tes notes et ta watchlist Letterboxd dans ton carnet (sans XP ni annonce)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        user_id = interaction.user.id
        if self.catalog is None or not self.catalog.tmdb.available:
            return await interaction.response.send_message(
                "**Import impossible ·** TMDB n'est pas configuré sur ce bot.", ephemeral=True
            )
        if user_id in self._imports:
            return await interaction.response.send_message(
                "**Import en cours ·** Attends la fin de ton import précédent.", ephemeral=True
            )
        if fichier.size > MAX_IMPORT_BYTES:
            return await interaction.response.send_message(
                "**Fichier trop gros ·** 10 Mo maximum.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = parse_letterboxd(await fichier.read(), fichier.filename)
        except ImportError_ as exc:
            return await interaction.followup.send(f"**Import impossible ·** {exc}", ephemeral=True)
        except Exception:
            logger.exception("Lecture de l'import Letterboxd impossible")
            return await interaction.followup.send("**Import impossible ·** Fichier illisible.", ephemeral=True)
        total = len(data.rated) + len(data.watchlist)
        if not total:
            return await interaction.followup.send(
                "**Rien à importer ·** Aucun film noté ni de watchlist dans ce fichier.", ephemeral=True
            )

        async def progress(done: int, count: int) -> None:
            try:
                await interaction.edit_original_response(
                    content=f"**Import Letterboxd ·** {done}/{count} films traités…"
                )
            except discord.HTTPException:
                pass

        self._imports.add(user_id)
        try:
            await progress(0, total)
            report = await self.import_letterboxd(guild, user_id, data, progress)
        except Exception:
            logger.exception("Import Letterboxd échoué")
            await progress(0, total)
            return await interaction.followup.send(
                "**Import interrompu ·** Une erreur est survenue, réessaie plus tard.", ephemeral=True
            )
        finally:
            self._imports.discard(user_id)
        text = report_text(report)
        try:
            await interaction.edit_original_response(content=text[:2000])
        except discord.HTTPException:
            # Jeton expiré (import très long) : le bilan part en MP.
            try:
                await interaction.user.send(text[:2000])
            except discord.HTTPException:
                logger.info("Bilan d'import impossible à envoyer à %s", user_id)

    @app_commands.command(name="quetes")
    @app_commands.guild_only()
    async def critique_quetes(self, interaction: discord.Interaction) -> None:
        """Tes quêtes de la semaine, ta série, et l'apparence de ton carnet (titre, skins)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        view = await QuestsView.create(self, guild, interaction.user)
        await view.start(interaction)

    @app_commands.command(name="config")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def critique_config(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration des critiques (annonces, commentaires)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        review_count, media_count = await self.counts(guild)
        view = ReviewsConfigView(
            self,
            guild,
            announce_channels=await self.get_announce_channels(guild),
            comment_max=await self.get_comment_max(guild),
            review_count=review_count,
            media_count=media_count,
            api_status=self.catalog.status() if self.catalog else {},
        )
        await view.start(interaction)

    @app_commands.command(name="help")
    @app_commands.guild_only()
    async def critique_help(self, interaction: discord.Interaction) -> None:
        """Explique les commandes et comment noter."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        await interaction.response.send_message(view=HelpView(avatar_url=avatar), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reviews(bot))
