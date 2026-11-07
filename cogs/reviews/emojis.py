"""Emojis custom du serveur CRIT, utilisés par le cog critiques."""

MOVIE = "<:movie:1551680932665102438>"
TV = "<:show:1551680815811797134>"
GAME = "<:game:1551681214182727700>"
MUSIC = "<:music:1551680930735849483>"
ALBUM = "<:album:1551680813643341894>"
BOOK = "<:book:1551680928575783093>"

XP = "<:xp:1545160995851345960>"
TWIN = "<:twin:1551680927350923424>"
RIVAL = "<:rivals:1551680926172446911>"

STAR = "<:note_star:1551336856132780214>"
STAR_HALF = "<:note_half_star:1551336755498717324>"
STAR_EMPTY = "<:note_no_star:1551336855205847110>"

HEART_EMPTY = "<:heartempty:1551148800188612620>"
HEART_HALF = "<:hearthalf:1551148801689911446>"
HEART_FULL = "<:heart:1551148802780438538>"

CROWN_EMPTY = "<:crownempty:1551151870280470548>"
CROWN_HALF = "<:crownhalf:1551151869047603272>"
CROWN_FULL = "<:crown:1551151883056447518>"

FLOWER_EMPTY = "<:flowerempty:1551695262089744404>"
FLOWER_HALF = "<:flowerhalf:1551695260483194911>"
FLOWER_FULL = "<:flower:1551695263129804840>"

PUMPKIN_EMPTY = "<:pumpkinempty:1554972310270902342>"
PUMPKIN_HALF = "<:pumpkinhalf:1554972308811030559>"
PUMPKIN_FULL = "<:pumpkinfull:1554972305598324776>"

REVIEW_ACCLAIMED = "<:medal:1545148587921510450>"
REVIEW_UP = "<:thumbsup:1545149518407016548>"
REVIEW_OK = "<:smile:1545150053629296680>"
REVIEW_MIXED = "<:mixed:1545150052501033091>"
REVIEW_DOWN = "<:thumbsdown:1545149514388607016>"
REVIEW_BOMB = "<:bomb:1545150051561508924>"

SALE = "<:sale:1545148944508788858>"
EXPLICIT = "<:explicit:1545148820038623293>"
SHARE = "<:share:1551681780543783012>"
MORE = "<:menu:1551701677562921010>"
HOUSE = "<:house:1553497698861191208>"
STREAMING = "<:signalstream:1549879575222419589>"
PLANNING = "<:planning:1555499639451680868>"
BOOKMARK_ON = "<:bookmarkon:1552791608674623590>"
BOOKMARK_OFF = "<:bookmarkoff:1552791607491825785>"
BOOKMARK_LIST = "<:bookmarklist:1552795149828886618>"
LIST = "<:list:1552794832118878238>"
EDIT_LIST = "<:editlist:1552794833842868254>"
ADD_LIST = "<:addlist:1552792730890010705>"
JOURNAL = "<:journal:1552795827108446359>"
AFFINITY = "<:affinity:1552795828140384357>"
TRASH = "<:trash:1553496864538628196>"
PLUS_SMALL = "<:plussmall:1553496866845360159>"
MINUS_SMALL = "<:minussmall:1553496865624817865>"
SETTINGS = "<:settings:1568616643456798811>"
SHUFFLE = "🔀"  # remplacé par assets/emojis/shuffle.png à la synchro
OPEN = "↗️"  # remplacé par assets/emojis/open.png à la synchro
SHELF = "🗂️"  # bouton Ranger (signet + listes), remplacé par assets/emojis/shelf.png

# Barres du graphique de répartition (assets/emojis/bar_0.png … bar_11.png) :
# BAR_0 = aucune note, BAR_1 à BAR_11 = hauteur croissante.
BAR_0 = "▁"
BAR_1 = BAR_2 = "▂"
BAR_3 = BAR_4 = "▃"
BAR_5 = BAR_6 = "▄"
BAR_7 = BAR_8 = "▅"
BAR_9 = BAR_10 = "▆"
BAR_11 = "█"
BAR_MAX = 11


# ---------------------------------------------------------------------------
# Synchronisation automatique (émojis d'application)
# ---------------------------------------------------------------------------
# Chaque `assets/emojis/<nom>.png` est envoyé comme émoji d'application du bot
# (visible partout, hors quota des serveurs). Le nom du fichier correspond au
# nom dans les tags ci-dessus : `movie.png` remplace `<:movie:…>`. Pour un
# émoji sans tag (valeur Unicode de secours), le nom de la constante en
# minuscules : `SHUFFLE` ↔ `shuffle.png`.
# Un PNG modifié est ré-envoyé au démarrage suivant (empreinte dans .sync.json).
# Les constantes sans PNG gardent leur valeur ci-dessus.

import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

import discord  # noqa: E402

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets" / "emojis"
_STATE_FILE = ASSETS_DIR / ".sync.json"
_TAG = re.compile(r"<a?:(\w+):\d+>")
_log = logging.getLogger("CRIT.Emojis")


async def sync_app_emojis(bot: discord.Client) -> None:
    """Envoie les PNG manquants ou modifiés puis met à jour les constantes.

    À appeler après la connexion (setup_hook) et AVANT le chargement des cogs,
    qui importent les constantes par valeur.
    """
    pngs = {p.stem: p for p in sorted(ASSETS_DIR.glob("*.png"))}
    if not pngs:
        return
    try:
        state = json.loads(_STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        state = {}
    known: dict[str, str] = state.setdefault(str(bot.application_id), {})

    try:
        remote = {e.name: e for e in await bot.fetch_application_emojis()}
    except discord.HTTPException as e:
        _log.error(f"Lecture des émojis d'application impossible : {e}")
        return

    resolved: dict[str, str] = {}
    created = 0
    for name, path in pngs.items():
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        emoji = remote.get(name)
        try:
            if emoji is not None and known.get(name) != digest:
                await emoji.delete()  # ancienne image : on remplace
                emoji = None
            if emoji is None:
                emoji = await bot.create_application_emoji(name=name, image=data)
                known[name] = digest
                created += 1
        except discord.HTTPException as e:
            _log.error(f"Émoji '{name}' non synchronisé : {e}")
            continue
        resolved[name] = str(emoji)

    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), "utf-8")
    except OSError as e:
        _log.warning(f"Écriture de {_STATE_FILE.name} impossible : {e}")

    g = globals()
    for const, value in list(g.items()):
        if const.isupper() and isinstance(value, str):
            m = _TAG.fullmatch(value)
            name = m[1] if m else const.lower()  # nouvel émoji : `SHUFFLE` ↔ shuffle.png
            if name in resolved:
                g[const] = resolved[name]
    _log.info(f"Émojis : {len(resolved)} synchronisé(s), dont {created} envoyé(s).")
