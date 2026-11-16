"""Liaison avec HERTZ (la chaîne TV Discord de la régie) : chaque film diffusé est lié au live.

HERTZ tourne sur le PC de chaque streamer et se connecte ici par WebSocket (`/hertz/ws`).
CRIT écoute en local (127.0.0.1:8790 par défaut) ; Tailscale Funnel le publie en https.

Protocole (JSON, un objet par message, champ `t`) :
  CRIT → HERTZ  salut {bot_id, nom, protocole}             dès la connexion
  HERTZ → CRIT  bonjour {jeton}                            → ok {user_id, nom} | inconnu
                appairer {user_id, chaine}                 → attente, puis jeton {jeton} | refuse | expire
                                                             (ou appairage_impossible {raison})
  (connecté)    programme {uid, guild_id, titre, correction?, fiche}  → lie {uid, oeuvre, autres, fiche} | non_lie {uid, raison}
                corriger {uid, ref}                        → lie
                chercher {q}                               → resultats {q, oeuvres}
                apercu {id, guild_id, titre, correction?}  → apercu {id, oeuvre, note}  (le prochain film : sa note
                                                              arrive avant qu'il commence ; rien n'est lié ni publié)
                quiz {guild_id}                            → quiz {guild_id, oeuvres: [{titre, annee, type, moyenne,
                                                              nombre, notes: [{nom, note}]}]}  (notes du serveur)
                fin_programme {uid} · fin_soiree · oublier → oublie
  CRIT → HERTZ  note {uid, note: {moyenne, nombre, avis, derniere?}}   dès qu'un membre note le film en cours ou le
                dernier film terminé (mise à jour en direct à l'écran)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import discord
from aiohttp import WSMsgType, web
from discord.ext import commands

from cogs.reviews.constants import NO_PINGS, logger as _logger

log = _logger.getChild("Hertz")

PROTOCOLE = 1
MAX_MESSAGE = 16 * 1024
MAX_CONNEXIONS = 40
DELAI_AUTH = 30            # secondes pour s'identifier (ou demander l'accès) après la connexion
DELAI_APPAIRAGE = 300      # le message privé « Autoriser / Refuser » reste valable 5 min
ENTRE_DEMANDES = 60        # une demande d'accès par minute au plus pour un même compte
JETONS_PAR_COMPTE = 3
FICHIER = Path("cogs/hertz/data/jetons.json")

_EPISODE = re.compile(r"\b(s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{2,3}|saison\s*\d+|season\s*\d+|[ée]pisode\s*\d+)\b", re.I)
_ANNEE = re.compile(r"[(\[]?\b((?:19|20)\d{2})\b[)\]]?")


def _empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


def requete_depuis_titre(titre: str) -> tuple[str, str]:
    """Titre d'un fichier vu par HERTZ → (recherche, type TMDB). Les épisodes cherchent la série."""
    t = re.sub(r"\s+", " ", titre or "").strip()
    m = _EPISODE.search(t)
    if m:
        serie = t[: m.start()].strip(" -–·_.")
        return (serie or t), "tv"
    annee = _ANNEE.search(t)
    if annee and annee.start() > 1:
        # « Aftersun (2022) » → « Aftersun 2022 » : l'année filtre la recherche TMDB
        t = f"{t[: annee.start()].strip(' -–·_.')} {annee.group(1)}"
    return t, "movie"


def oeuvre_json(hit: Any) -> dict[str, Any]:
    return {
        "ref": f"{hit.source}:{hit.media_type}:{hit.source_id}",
        "titre": hit.title,
        "annee": hit.year,
        "type": "série" if hit.media_type == "tv" else "film",
        "affiche": hit.poster_url,
        "url": hit.url,
    }


class Liaison:
    """Une régie HERTZ connectée."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.user_id: int | None = None
        self.chaine = ""
        self.appairage: asyncio.Task | None = None
        self.verrou = asyncio.Lock()              # les messages d'une même régie sont traités dans l'ordre
        self._fenetre: list[float] = []
        self._derniere_recherche = 0.0

    def limiter(self) -> bool:
        """30 messages par tranche de 10 s au plus."""
        now = time.monotonic()
        self._fenetre = [t for t in self._fenetre if now - t < 10]
        self._fenetre.append(now)
        return len(self._fenetre) <= 30

    async def envoyer(self, data: dict[str, Any]) -> None:
        if not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(data, ensure_ascii=False))
            except (ConnectionError, RuntimeError):
                pass


class VueAppairage(discord.ui.LayoutView):
    """Message privé : la régie HERTZ demande l'accès."""

    def __init__(self, chaine: str):
        super().__init__(timeout=DELAI_APPAIRAGE)
        self.resultat: asyncio.Future[bool | None] = asyncio.get_running_loop().create_future()
        self.chaine = chaine
        self.message: discord.Message | None = None
        self._construire(None)

    def _construire(self, choix: bool | None) -> None:
        self.clear_items()
        nom = discord.utils.escape_markdown(self.chaine or "HERTZ")
        if choix is None:
            texte = (
                "### Connexion HERTZ\n"
                f"Ta régie **{nom}** demande à se connecter à CRIT.\n"
                "Si tu acceptes, chaque film diffusé pendant ton Go Live sera lié à ton live : "
                "statut du salon vocal, et une fiche à noter pour les spectateurs.\n"
                "-# Ce n'est pas toi qui viens de cliquer « connecter CRIT » dans HERTZ ? Refuse."
            )
        elif choix:
            texte = f"### Connexion HERTZ\n✓ **{nom}** est connectée à CRIT.\n-# Pour couper l'accès : « déconnecter » dans la régie HERTZ."
        else:
            texte = f"### Connexion HERTZ\nDemande de **{nom}** refusée."
        items: list[discord.ui.Item] = [discord.ui.TextDisplay(texte)]
        if choix is None:
            oui = discord.ui.Button(label="Autoriser", style=discord.ButtonStyle.green)
            non = discord.ui.Button(label="Refuser", style=discord.ButtonStyle.secondary)
            oui.callback = lambda i: self._repondre(i, True)
            non.callback = lambda i: self._repondre(i, False)
            items.append(discord.ui.ActionRow(oui, non))
        self.add_item(discord.ui.Container(*items))

    async def _repondre(self, interaction: discord.Interaction, choix: bool) -> None:
        self._construire(choix)
        await interaction.response.edit_message(view=self)
        if not self.resultat.done():
            self.resultat.set_result(choix)
        self.stop()

    async def on_timeout(self) -> None:
        if not self.resultat.done():
            self.resultat.set_result(None)
        if self.message is not None:
            self.clear_items()
            self.add_item(discord.ui.Container(discord.ui.TextDisplay(
                "### Connexion HERTZ\nDemande expirée : relance « connecter CRIT » depuis la régie.")))
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Hertz(commands.Cog):
    """Serveur de liaison avec les régies HERTZ."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.port = int(os.getenv("HERTZ_PORT") or (getattr(bot, "config", {}) or {}).get("HERTZ_PORT") or 8790)
        self.hote = os.getenv("HERTZ_HOTE") or "127.0.0.1"
        self.liaisons: set[Liaison] = set()
        self.jetons: dict[str, dict[str, Any]] = {}
        # (serveur, membre) → film en cours lié par HERTZ : survit à une coupure de la connexion
        self.en_cours: dict[tuple[int, int], dict[str, Any]] = {}
        # (serveur, membre) → dernier film terminé : ses nouvelles notes partent encore vers HERTZ
        self.termines: dict[tuple[int, int], dict[str, Any]] = {}
        self._demandes: dict[int, float] = {}
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------ cycle de vie
    async def cog_load(self) -> None:
        self._charger()
        app = web.Application(client_max_size=MAX_MESSAGE)
        app.router.add_get("/hertz", self._info)
        app.router.add_get("/hertz/ws", self._ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self.hote, self.port).start()
            log.info("Liaison HERTZ à l'écoute sur %s:%s", self.hote, self.port)
        except OSError as exc:
            log.error("Liaison HERTZ impossible sur le port %s : %s", self.port, exc)

    async def cog_unload(self) -> None:
        for li in list(self.liaisons):
            if li.appairage:
                li.appairage.cancel()
            await li.ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _charger(self) -> None:
        try:
            data = json.loads(FICHIER.read_text("utf-8"))
            self.jetons = {k: v for k, v in (data.get("jetons") or {}).items() if isinstance(v, dict)}
        except FileNotFoundError:
            self.jetons = {}
        except Exception:
            log.exception("Jetons HERTZ illisibles")
            self.jetons = {}

    def _sauver(self) -> None:
        FICHIER.parent.mkdir(parents=True, exist_ok=True)
        tmp = FICHIER.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jetons": self.jetons}, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, FICHIER)

    # ------------------------------------------------------------------ utilisé par les lives
    def connecte(self, user_id: int) -> bool:
        """Une régie HERTZ de ce membre est connectée : elle dit elle-même ce qu'il diffuse."""
        return any(li.user_id == user_id and not li.ws.closed for li in self.liaisons)

    async def fin_du_live(self, guild: discord.Guild, user_id: int) -> bool:
        """Go Live terminé alors qu'un film HERTZ était lié : sa fiche passe en « à noter » (pas de 2e fiche)."""
        if (guild.id, user_id) not in self.en_cours:
            return False
        await self._terminer(guild.id, user_id)
        return True

    # ------------------------------------------------------------------ http
    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response({"crit": True, "protocole": PROTOCOLE})

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if len(self.liaisons) >= MAX_CONNEXIONS:
            return web.Response(status=503, text="trop de connexions")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MESSAGE)
        await ws.prepare(request)
        li = Liaison(ws)
        self.liaisons.add(li)
        chien = asyncio.create_task(self._chien(li))
        moi = self.bot.user
        await li.envoyer({"t": "salut", "protocole": PROTOCOLE, "bot_id": str(moi.id) if moi else None,
                          "nom": moi.name if moi else "CRIT"})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if not li.limiter():
                    await li.envoyer({"t": "erreur", "raison": "trop_vite"})
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                async with li.verrou:
                    try:
                        await self._traiter(li, data)
                    except Exception:
                        log.exception("Message HERTZ %s", data.get("t"))
                        await li.envoyer({"t": "erreur", "raison": "interne"})
        finally:
            chien.cancel()
            if li.appairage:
                li.appairage.cancel()
            self.liaisons.discard(li)
        return ws

    async def _chien(self, li: Liaison) -> None:
        """Une connexion qui ne s'identifie pas est coupée."""
        await asyncio.sleep(DELAI_AUTH)
        if li.user_id is None and li.appairage is None:
            await li.ws.close()

    # ------------------------------------------------------------------ messages
    async def _traiter(self, li: Liaison, d: dict[str, Any]) -> None:
        t = d.get("t")
        if t == "bonjour":
            info = self.jetons.get(_empreinte(str(d.get("jeton") or "")))
            if not info:
                await li.envoyer({"t": "inconnu"})
                return
            li.user_id, li.chaine = int(info["user_id"]), str(d.get("chaine") or info.get("chaine") or "")
            info["vu"] = int(time.time())
            self._sauver()
            user = self.bot.get_user(li.user_id)
            await li.envoyer({"t": "ok", "user_id": str(li.user_id), "nom": user.display_name if user else None})
            return
        if t == "appairer":
            await self._demander_acces(li, d)
            return
        if li.user_id is None:
            await li.envoyer({"t": "inconnu"})
            return
        if t == "programme":
            await self._programme(li, d)
        elif t == "corriger":
            await self._corriger(li, d)
        elif t == "chercher":
            await self._chercher(li, d)
        elif t == "quiz":
            await self._quiz(li, d)
        elif t == "apercu":
            await self._apercu(li, d)
        elif t == "fin_programme":
            for (gid, uid), c in list(self.en_cours.items()):
                if uid == li.user_id and c["uid"] == str(d.get("uid") or ""):
                    await self._terminer(gid, uid)
        elif t == "fin_soiree":
            for gid, uid in [k for k in self.en_cours if k[1] == li.user_id]:
                await self._terminer(gid, uid)
        elif t == "oublier":
            self.jetons = {k: v for k, v in self.jetons.items() if int(v.get("user_id", 0)) != li.user_id}
            self._sauver()
            li.user_id = None
            await li.envoyer({"t": "oublie"})

    # ------------------------------------------------------------------ appairage
    def _membre_quelque_part(self, user_id: int) -> discord.Member | None:
        for g in self.bot.guilds:
            m = g.get_member(user_id)
            if m is not None:
                return m
        return None

    async def _demander_acces(self, li: Liaison, d: dict[str, Any]) -> None:
        brut = str(d.get("user_id") or "")
        if li.user_id is not None or li.appairage is not None or not brut.isdigit() or len(brut) > 20:
            await li.envoyer({"t": "appairage_impossible", "raison": "requete"})
            return
        user_id = int(brut)
        if self._membre_quelque_part(user_id) is None:
            await li.envoyer({"t": "appairage_impossible", "raison": "pas_de_serveur_commun"})
            return
        now = time.monotonic()
        if now - self._demandes.get(user_id, -ENTRE_DEMANDES) < ENTRE_DEMANDES:
            await li.envoyer({"t": "appairage_impossible", "raison": "trop_tot"})
            return
        self._demandes[user_id] = now
        li.chaine = str(d.get("chaine") or "HERTZ")[:40]
        li.appairage = asyncio.create_task(self._appairer(li, user_id))

    async def _appairer(self, li: Liaison, user_id: int) -> None:
        try:
            membre = self._membre_quelque_part(user_id)
            vue = VueAppairage(li.chaine)
            try:
                vue.message = await membre.send(view=vue, allowed_mentions=NO_PINGS)  # type: ignore[union-attr]
            except (discord.HTTPException, AttributeError):
                await li.envoyer({"t": "appairage_impossible", "raison": "mp_fermes"})
                return
            await li.envoyer({"t": "attente"})
            choix = await vue.resultat
            if choix is None:
                await li.envoyer({"t": "expire"})
                return
            if not choix:
                await li.envoyer({"t": "refuse"})
                return
            jeton = secrets.token_urlsafe(32)
            siens = sorted((v.get("cree", 0), k) for k, v in self.jetons.items() if int(v.get("user_id", 0)) == user_id)
            for _, k in siens[: max(0, len(siens) - JETONS_PAR_COMPTE + 1)]:
                self.jetons.pop(k, None)
            self.jetons[_empreinte(jeton)] = {"user_id": user_id, "chaine": li.chaine,
                                              "cree": int(time.time()), "vu": int(time.time())}
            self._sauver()
            li.user_id = user_id
            log.info("Régie HERTZ « %s » autorisée pour %s", li.chaine, user_id)
            await li.envoyer({"t": "jeton", "jeton": jeton, "user_id": str(user_id), "nom": membre.display_name})
        except asyncio.CancelledError:
            pass
        finally:
            li.appairage = None

    # ------------------------------------------------------------------ films
    def _reviews(self):
        return self.bot.get_cog("Reviews")

    async def _trouver(self, titre: str, correction: str | None) -> tuple[Any, list[Any]]:
        reviews = self._reviews()
        catalog = getattr(reviews, "catalog", None)
        if catalog is None or not catalog.tmdb.available:
            return None, []
        if correction:
            source, _, reste = str(correction).partition(":")
            kind, _, sid = reste.partition(":")
            if source == "tmdb" and kind in ("movie", "tv") and sid.isdigit():
                trouves = await catalog.tmdb.lookup(sid, kind)
                if trouves:
                    return trouves[0], []
        requete, kind = requete_depuis_titre(titre)
        if len(requete) < 2:
            return None, []
        hits = await catalog.tmdb.search(requete, kind, 6)
        if not hits and kind == "movie":
            hits = await catalog.tmdb.search(requete, "tv", 6)
        return (hits[0], hits[1:6]) if hits else (None, [])

    async def _programme(self, li: Liaison, d: dict[str, Any]) -> None:
        uid = str(d.get("uid") or "")[:32]
        reviews = self._reviews()
        guild = self.bot.get_guild(int(d.get("guild_id") or 0)) if str(d.get("guild_id") or "").isdigit() else None
        if reviews is None or guild is None:
            await li.envoyer({"t": "non_lie", "uid": uid, "raison": "serveur"})
            return
        from cogs.reviews.formatting import hit_identity
        from cogs.reviews.streaming import member_stream_source, member_voice_channel_id

        membre = guild.get_member(li.user_id)
        # c'est CRIT qui vérifie : on ne lie que le Go Live de la personne qui a autorisé cette régie
        if membre is None or member_stream_source(membre) != "voice":
            await li.envoyer({"t": "non_lie", "uid": uid, "raison": "pas_en_live"})
            return
        hit, autres = await self._trouver(str(d.get("titre") or "")[:200], d.get("correction"))
        if hit is None:
            await li.envoyer({"t": "non_lie", "uid": uid, "raison": "introuvable"})
            return
        cle = (guild.id, li.user_id)
        courant = self.en_cours.get(cle)
        if courant and (courant["uid"] == uid or hit_identity(courant["hit"]) == hit_identity(hit)):
            # même film (HERTZ s'est reconnecté, ou a été relancé) : pas de deuxième fiche
            courant["uid"] = uid
            await li.envoyer({"t": "lie", "uid": uid, "oeuvre": oeuvre_json(courant["hit"]),
                              "autres": [oeuvre_json(h) for h in autres], "fiche": bool(courant.get("message_id")),
                              "note": await self._note(guild, courant["hit"])})
            return
        if courant:
            await self._terminer(guild.id, li.user_id)
        salon = member_voice_channel_id(membre) or 0
        await reviews.set_stream_link(guild, li.user_id, hit, channel_id=salon, source="voice", post_channel_id=0)
        c = {"uid": uid, "hit": hit, "salon": salon, "channel_id": 0, "message_id": 0}
        self.en_cours[cle] = c
        if d.get("fiche", True):
            message = await self._poster_fiche(reviews, guild, li.user_id, hit, salon)
            if message is not None:
                c["channel_id"], c["message_id"] = message.channel.id, message.id
        await li.envoyer({"t": "lie", "uid": uid, "oeuvre": oeuvre_json(hit),
                          "autres": [oeuvre_json(h) for h in autres], "fiche": bool(c["message_id"]),
                          "note": await self._note(guild, hit)})

    async def _note(self, guild: discord.Guild, hit: Any, derniere: dict | None = None) -> dict[str, Any]:
        """Note du serveur pour cette œuvre (sur 10), comme sur la fiche."""
        reviews = self._reviews()
        moyenne, nombre, avis = None, 0, []
        if reviews is not None:
            media_id = await reviews.lookup_media_id(guild, hit)
            if media_id:
                moyenne, nombre = await reviews.media_stats(guild, media_id)
                avis = await self._avis(reviews, guild, media_id)
        note: dict[str, Any] = {"moyenne": round(moyenne, 2) if moyenne is not None else None, "nombre": nombre,
                                "avis": avis}
        if derniere:
            note["derniere"] = derniere
        return note

    async def _avis(self, reviews, guild: discord.Guild, media_id: Any) -> list[dict[str, Any]]:
        """Les commentaires des membres (les plus récents d'abord), pour les faire défiler à l'écran.
        Jamais ceux marqués « spoiler ». Au plus 12, coupés à 280 caractères."""
        try:
            from cogs.reviews.formatting import row_spoiler, user_display
            out = []
            for r in await reviews.list_reviews(guild, media_id):
                texte = " ".join(str(r["comment"] or "").split())
                if not texte or row_spoiler(r):
                    continue
                if len(texte) > 280:
                    texte = texte[:277].rsplit(" ", 1)[0] + "…"
                out.append({"nom": user_display(guild, self.bot, int(r["user_id"]))[0], "note": float(r["rating"]),
                            "texte": texte})
                if len(out) == 12:
                    break
            return out
        except Exception:
            log.exception("Commentaires pour HERTZ impossibles")
            return []

    async def note_changee(self, guild: discord.Guild, hit: Any, user: Any, rating: float | None) -> None:
        """Appelé quand un membre note (ou retire sa note) : les régies qui diffusent ce film sont prévenues."""
        try:
            cible = hit.identity
            derniere = {"nom": getattr(user, "display_name", None) or str(user), "note": rating} if rating is not None else None
            for li in list(self.liaisons):
                if li.user_id is None or li.ws.closed:
                    continue
                cle = (guild.id, li.user_id)
                for c in (self.en_cours.get(cle), self.termines.get(cle)):
                    if c and c["hit"].identity == cible:
                        await li.envoyer({"t": "note", "uid": c["uid"], "note": await self._note(guild, hit, derniere)})
        except Exception:
            log.exception("Note vers HERTZ impossible")

    async def _destinations(self, reviews, guild: discord.Guild, salon: int, media_type: str) -> list:
        """Le chat du vocal où tout le monde regarde, sinon le salon d'annonces de CRIT."""
        dests = []
        for ch in (await reviews._fetch_messageable(guild, salon), await reviews.get_announce_channel(guild, media_type)):
            if ch is not None and all(getattr(x, "id", None) != getattr(ch, "id", None) for x in dests):
                dests.append(ch)
        return dests

    async def _poster_fiche(self, reviews, guild: discord.Guild, user_id: int, hit: Any, salon: int):
        from cogs.reviews.formatting import mention_of
        from cogs.reviews.ui.fiche import post_compact_fiche

        mention = mention_of(guild, self.bot, user_id)
        for ch in await self._destinations(reviews, guild, salon, hit.media_type):
            try:
                message = await post_compact_fiche(reviews, guild, hit, ch, streamer=mention)
            except Exception:
                log.exception("Fiche HERTZ impossible")
                continue
            if message is not None:
                return message
        log.info("Fiche HERTZ : aucun salon où poster (%s)", user_id)
        return None

    async def _message(self, c: dict[str, Any]) -> discord.Message | None:
        if not c.get("message_id"):
            return None
        try:
            ch = self.bot.get_channel(c["channel_id"]) or await self.bot.fetch_channel(c["channel_id"])
            return await ch.fetch_message(c["message_id"])  # type: ignore[union-attr]
        except (discord.HTTPException, AttributeError):
            return None

    def _desactiver_ancienne(self, message_id: int) -> None:
        """La fiche est remplacée sur le même message : l'ancienne ne doit plus être retouchée par le ménage."""
        from cogs.reviews.dyn import _db, mark_stripped

        with _db() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM fiches WHERE message_id = ?", (message_id,)).fetchall()]
        for wid in ids:
            mark_stripped(wid)

    async def _corriger(self, li: Liaison, d: dict[str, Any]) -> None:
        uid = str(d.get("uid") or "")
        cle = next((k for k, c in self.en_cours.items() if k[1] == li.user_id and c["uid"] == uid), None)
        hit, _ = await self._trouver("", str(d.get("ref") or ""))
        reviews = self._reviews()
        guild = self.bot.get_guild(cle[0]) if cle else None
        if hit is None or guild is None or reviews is None:
            await li.envoyer({"t": "non_lie", "uid": uid, "raison": "introuvable" if cle else "plus_en_cours"})
            return
        c = self.en_cours[cle]
        if hit.identity == c["hit"].identity:   # c'était déjà le bon film : rien à refaire
            await li.envoyer({"t": "lie", "uid": uid, "oeuvre": oeuvre_json(hit), "autres": [],
                              "fiche": bool(c["message_id"]), "corrige": True, "note": await self._note(guild, hit)})
            return
        c["hit"] = hit
        await reviews.set_stream_link(guild, li.user_id, hit, channel_id=c["salon"], source="voice", post_channel_id=0)
        message = await self._message(c)
        if message is not None:
            from cogs.reviews.formatting import mention_of
            from cogs.reviews.ui.fiche import post_compact_fiche

            # on reposte la bonne fiche à la place de la mauvaise
            self._desactiver_ancienne(message.id)
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            c["message_id"] = 0
            nouveau = await post_compact_fiche(reviews, guild, hit, message.channel,
                                               streamer=mention_of(guild, self.bot, li.user_id))
            if nouveau is not None:
                c["channel_id"], c["message_id"] = nouveau.channel.id, nouveau.id
        await li.envoyer({"t": "lie", "uid": uid, "oeuvre": oeuvre_json(hit), "autres": [],
                          "fiche": bool(c["message_id"]), "corrige": True, "note": await self._note(guild, hit)})

    async def _chercher(self, li: Liaison, d: dict[str, Any]) -> None:
        q = str(d.get("q") or "").strip()[:100]
        now = time.monotonic()
        if len(q) < 2 or now - li._derniere_recherche < 1.5:
            await li.envoyer({"t": "resultats", "q": q, "oeuvres": []})
            return
        li._derniere_recherche = now
        catalog = getattr(self._reviews(), "catalog", None)
        oeuvres: list[dict] = []
        if catalog is not None and catalog.tmdb.available:
            films, series = await asyncio.gather(catalog.tmdb.search(q, "movie", 5), catalog.tmdb.search(q, "tv", 3))
            oeuvres = [oeuvre_json(h) for h in [*films, *series]]
        await li.envoyer({"t": "resultats", "q": q, "oeuvres": oeuvres})

    async def _apercu(self, li: Liaison, d: dict[str, Any]) -> None:
        """Le film qui va passer : l'œuvre trouvée et sa note du serveur, sans rien lier ni publier."""
        ident = str(d.get("id") or "")[:32]
        brut = str(d.get("guild_id") or "")
        guild = self.bot.get_guild(int(brut)) if brut.isdigit() else None
        if guild is None or guild.get_member(li.user_id) is None:
            await li.envoyer({"t": "apercu", "id": ident, "oeuvre": None, "note": None})
            return
        hit, _ = await self._trouver(str(d.get("titre") or "")[:200], d.get("correction"))
        await li.envoyer({"t": "apercu", "id": ident, "oeuvre": oeuvre_json(hit) if hit else None,
                          "note": await self._note(guild, hit) if hit else None})

    async def _quiz(self, li: Liaison, d: dict[str, Any]) -> None:
        """Les œuvres notées sur le serveur, pour les questions du mini-jeu de HERTZ."""
        brut = str(d.get("guild_id") or "")
        guild = self.bot.get_guild(int(brut)) if brut.isdigit() else None
        reviews = self._reviews()
        # seulement un serveur où se trouve la personne qui a autorisé cette régie
        if guild is None or reviews is None or guild.get_member(li.user_id) is None:
            await li.envoyer({"t": "quiz", "guild_id": brut, "oeuvres": []})
            return
        from cogs.reviews.formatting import user_display

        on_server = "JOIN members gm ON gm.user_id = r.user_id AND gm.guild_id = ?"
        lignes = await reviews.db.fetchall(
            f"""SELECT m.id, m.title, m.year, m.media_type, AVG(r.rating) AS moy, COUNT(*) AS n
                FROM reviews r {on_server} JOIN media m ON m.id = r.media_id
                WHERE m.media_type IN ('movie', 'tv')
                GROUP BY m.id ORDER BY n DESC, MAX(r.updated_at) DESC LIMIT 80""",
            guild.id,
        )
        oeuvres: dict[int, dict[str, Any]] = {}
        for r in lignes:
            oeuvres[int(r["id"])] = {"titre": r["title"], "annee": r["year"],
                                     "type": "série" if r["media_type"] == "tv" else "film",
                                     "moyenne": round(float(r["moy"]), 2), "nombre": int(r["n"]), "notes": []}
        if oeuvres:
            marques = ",".join("?" * len(oeuvres))
            notes = await reviews.db.fetchall(
                f"""SELECT r.media_id, r.user_id, r.rating FROM reviews r {on_server}
                    WHERE r.media_id IN ({marques}) ORDER BY r.rating DESC""",
                guild.id, *oeuvres.keys(),
            )
            for r in notes:
                o = oeuvres[int(r["media_id"])]
                if len(o["notes"]) < 8:
                    o["notes"].append({"nom": user_display(guild, self.bot, int(r["user_id"]))[0],
                                       "note": float(r["rating"])})
        avis: list[dict[str, Any]] = []
        noms: dict[int, str] = {}
        try:
            avis, noms = await self._avis_quiz(reviews, guild, on_server)
        except Exception:
            log.exception("Commentaires du mini-jeu pour HERTZ impossibles")
        await li.envoyer({"t": "quiz", "guild_id": brut, "oeuvres": list(oeuvres.values()), "avis": avis,
                          "membres": list(dict.fromkeys(noms.values()))})

    async def _avis_quiz(self, reviews, guild: discord.Guild, on_server: str):
        """« Qui a noté ça ? » : des commentaires récents (jamais ceux marqués spoiler), et les membres qui notent."""
        from cogs.reviews.formatting import row_spoiler, user_display

        avis: list[dict[str, Any]] = []
        noms: dict[int, str] = {}
        lignes = await reviews.db.fetchall(
            f"""SELECT r.*, m.title AS m_titre, m.year AS m_annee, m.media_type AS m_type, m.poster_url AS m_affiche
                FROM reviews r {on_server} JOIN media m ON m.id = r.media_id
                WHERE m.media_type IN ('movie', 'tv') AND r.comment IS NOT NULL AND TRIM(r.comment) != ''
                ORDER BY r.updated_at DESC LIMIT 200""",
            guild.id,
        )
        for r in lignes:
            texte = " ".join(str(r["comment"] or "").split())
            if len(texte) < 6 or row_spoiler(r):
                continue
            if len(texte) > 200:
                texte = texte[:197].rsplit(" ", 1)[0] + "…"
            uid = int(r["user_id"])
            noms.setdefault(uid, user_display(guild, self.bot, uid)[0])
            avis.append({"nom": noms[uid], "note": float(r["rating"]), "texte": texte, "titre": r["m_titre"],
                         "annee": r["m_annee"], "type": "série" if r["m_type"] == "tv" else "film",
                         "affiche": r["m_affiche"]})
            if len(avis) == 80:
                break
        membres = await reviews.db.fetchall(
            f"""SELECT r.user_id, COUNT(*) AS n FROM reviews r {on_server}
                GROUP BY r.user_id ORDER BY n DESC LIMIT 40""",
            guild.id,
        )
        for r in membres:
            uid = int(r["user_id"])
            noms.setdefault(uid, user_display(guild, self.bot, uid)[0])
        return avis, noms

    async def _terminer(self, guild_id: int, user_id: int) -> None:
        """Fin du film : le lien au live est retiré et sa fiche invite à noter."""
        c = self.en_cours.pop((guild_id, user_id), None)
        if c is not None:
            self.termines[(guild_id, user_id)] = {"uid": c["uid"], "hit": c["hit"]}
        guild = self.bot.get_guild(guild_id)
        reviews = self._reviews()
        if c is None or guild is None or reviews is None:
            return
        link = await reviews.get_stream_link(guild, user_id)
        if link is not None:
            await reviews.clear_stream_link(guild, user_id)
        message = await self._message(c)
        if message is None:
            return
        from cogs.reviews.dyn import bind_record
        from cogs.reviews.formatting import mention_of
        from cogs.reviews.ui.fiche import prepare_published_fiche

        banner = f"{mention_of(guild, self.bot, user_id)} · fin du film · note-le !"
        try:
            vue, wid = await prepare_published_fiche(reviews, guild, c["hit"], banner=banner)
            self._desactiver_ancienne(message.id)
            await message.edit(view=vue, allowed_mentions=NO_PINGS)
            bind_record(wid, message.channel.id, message.id)
        except Exception:
            log.exception("Fiche de fin HERTZ impossible")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Hertz(bot))
