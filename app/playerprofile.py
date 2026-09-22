"""Per-player style memory: how the app perceives each profile's play style.

Every exchange feeds it — chat conversations (messages + generated artifacts),
form-driven analyses (/suggest, /generate, /build, recorded as bounded events)
and the collection itself, in particular the decks the player has actually
built (ManaBox "Binder Type" = deck rows, grouped by deck name). The memory is
re-synthesised in a background thread after each of those events, so it always
reflects the latest library import and the latest conversation.

Synthesis is LLM-first with a mandatory heuristic fallback (the app must work
without an Anthropic key). Anti-hallucination anchoring (invariant 1) applies:
the model may only quote card names that appear in the signals we fed it —
anything else is dropped before storage.

Storage is one ``meta`` row per profile (``player_profile:<pid>``): a small
JSON dict with the French summary, style tags, colors, formats and the
signals fingerprint used to skip pointless re-synthesis when nothing changed.
"""
import hashlib
import json
import logging
import threading
import time

from . import db, llm, scryfall

logger = logging.getLogger(__name__)

PROFILE_META_PREFIX = "player_profile:"
EVENTS_META_PREFIX = "profile_events:"

# Bounded windows: the memory summarises recent behaviour, it is not an audit
# log. Events are capped at write time; chat signals at read time.
_EVENTS_MAX = 60
_CONVERSATIONS_MAX = 10
_WISHES_MAX = 12
_DECK_CARDS_SHOWN = 12

_WUBRG = ("W", "U", "B", "R", "G")
_COLOR_FR = {"W": "blanc", "U": "bleu", "B": "noir", "R": "rouge", "G": "vert"}


def _norm(name: str) -> str:
    """Canonical name key (front face, lowercase) — invariant of the codebase."""
    return name.split("//")[0].strip().lower()


# --- Event log (form-driven exchanges: /suggest, /generate, /build) -------

def record_event(profile_id: int, kind: str, data: dict) -> None:
    """Append one interaction to the profile's bounded event log.

    Chat turns are NOT recorded here — conversations are already persisted and
    read directly by ``_chat_signals``. Events cover the form flows, which
    leave no other per-profile trace.
    """
    key = f"{EVENTS_META_PREFIX}{int(profile_id)}"
    try:
        events = json.loads(db.get_meta(key) or "[]")
        if not isinstance(events, list):
            events = []
    except (json.JSONDecodeError, TypeError):
        events = []
    events.append({"kind": kind, "at": time.time(), **{k: v for k, v in data.items() if v}})
    db.set_meta(key, json.dumps(events[-_EVENTS_MAX:]))


def _events(profile_id: int) -> list[dict]:
    try:
        events = json.loads(db.get_meta(f"{EVENTS_META_PREFIX}{int(profile_id)}") or "[]")
        return events if isinstance(events, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# --- Signal gathering -----------------------------------------------------

def _deck_signals(profile_id: int) -> list[dict]:
    """One entry per built deck (ManaBox), with colors resolved locally.

    Colors come from the cached Scryfall cards only — a card the cache hasn't
    resolved yet simply contributes no color, never a guessed one.
    """
    decks = db.deck_memberships(profile_id)
    if not decks:
        return []
    all_keys = {c["name_key"] for cards in decks.values() for c in cards}
    cached = db.get_cards(all_keys, ttl_days=db.ANY_AGE)
    out = []
    for deck_name, cards in sorted(decks.items()):
        colors: set[str] = set()
        commanders: list[str] = []
        for c in cards:
            card = cached.get(c["name_key"])
            if not card:
                continue
            colors.update(x for x in scryfall.color_identity(card) if x in _WUBRG)
            type_line = (card.get("type_line") or "").lower()
            if "legendary" in type_line and "creature" in type_line:
                commanders.append(c["raw_name"])
        out.append({
            "name": deck_name or "Deck sans nom",
            "size": sum(c["qty"] for c in cards),
            "colors": [c for c in _WUBRG if c in colors],
            "commanders": commanders[:3],
            "cards": [c["raw_name"] for c in cards[:_DECK_CARDS_SHOWN]],
        })
    return out


def _artifact_line(art: dict) -> str | None:
    """Compact French description of a generated artifact, or None."""
    t = art.get("type")
    if t == "decklist":
        deck = art.get("deck") or {}
        fmt = deck.get("format") or "commander"
        return f"decklist {fmt} générée pour le commandant {art.get('commander')}"
    if t == "commanders":
        it = art.get("intent") or {}
        bits = [f"recherche de commandants ({it.get('format') or 'commander'})"]
        if it.get("theme"):
            bits.append(f"thème « {it['theme']} »")
        if it.get("colors"):
            bits.append("couleurs " + "/".join(it["colors"]))
        if it.get("budget_eur") is not None:
            bits.append(f"budget {it['budget_eur']} €")
        return ", ".join(bits)
    if t == "archetype":
        data = art.get("data") or {}
        arch = data.get("archetype") or {}
        return (f"deck {data.get('format') or '60 cartes'} construit : "
                f"{arch.get('name')} ({'/'.join(arch.get('colors') or []) or 'incolore'})")
    if t in ("pool_deck", "limited_deck"):
        data = art.get("data") or {}
        arch = data.get("archetype") or {}
        return (f"deck {data.get('format_label') or 'Limité'} construit depuis une "
                f"liste : {arch.get('name')}")
    return None


def _chat_signals(profile_id: int) -> dict:
    """Player wishes and generated results from the stored conversations."""
    wishes: list[str] = []
    generated: list[str] = []
    convs = db.list_conversations(profile_id)[:_CONVERSATIONS_MAX]
    for conv in convs:
        for m in db.get_messages(conv["id"]):
            if m["role"] == "user":
                text = m["content"].strip()
                # Skip the synthetic pool-import openers; keep real requests.
                if text and not text.startswith("J'ai importé une liste"):
                    wishes.append(text[:200])
            for art in m.get("artifacts") or []:
                line = _artifact_line(art)
                if line:
                    generated.append(line)
    return {
        "conversations": len(convs),
        "wishes": wishes[-_WISHES_MAX:],
        "generated": generated[-_WISHES_MAX:],
    }


def _event_signals(profile_id: int) -> list[str]:
    lines = []
    for e in _events(profile_id):
        bits = [str(e.get("kind"))]
        for key in ("wish", "format", "theme", "commander"):
            if e.get(key):
                bits.append(f"{key}={str(e[key])[:120]}")
        if e.get("colors"):
            bits.append("colors=" + "/".join(e["colors"]))
        if e.get("budget_eur") is not None:
            bits.append(f"budget={e['budget_eur']}€")
        lines.append(", ".join(bits))
    return lines[-_EVENTS_MAX:]


def _collection_signals(profile_id: int) -> dict:
    """Cheap collection-level facts (counts + color breakdown, both cached)."""
    # Imported lazily: collection imports scryquery; keep this module light for
    # the modules (chat) that import us early.
    from . import collection
    distinct, total = db.collection_count(profile_id)
    if not total:
        return {"distinct": 0, "total": 0, "colors": {}}
    stats = collection.stats(profile_id)
    return {
        "distinct": distinct,
        "total": total,
        "colors": stats.get("color_breakdown") or {},
    }


def gather_signals(profile_id: int) -> dict:
    return {
        "collection": _collection_signals(profile_id),
        "decks": _deck_signals(profile_id),
        "chat": _chat_signals(profile_id),
        "events": _event_signals(profile_id),
    }


def _fingerprint(signals: dict) -> str:
    return hashlib.sha1(
        json.dumps(signals, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _has_signals(signals: dict) -> bool:
    return bool(
        signals["collection"]["total"]
        or signals["decks"]
        or signals["chat"]["wishes"]
        or signals["chat"]["generated"]
        or signals["events"]
    )


# --- Synthesis ------------------------------------------------------------

def _signals_text(signals: dict) -> str:
    """Render the signals as the French prompt body (also the heuristic input)."""
    parts: list[str] = []
    col = signals["collection"]
    if col["total"]:
        colors = col.get("colors") or {}
        ranked = sorted((c for c in _WUBRG if colors.get(c)),
                        key=lambda c: -colors[c])
        parts.append(
            f"COLLECTION : {col['total']} cartes ({col['distinct']} uniques). "
            "Couleurs dominantes : "
            + (", ".join(f"{_COLOR_FR[c]} ({colors[c]})" for c in ranked[:5]) or "aucune")
            + "."
        )
    if signals["decks"]:
        lines = ["DECKS CONSTRUITS (dans la bibliothèque ManaBox) :"]
        for d in signals["decks"]:
            line = (f"- « {d['name']} » ({d['size']} cartes, "
                    f"{'/'.join(d['colors']) or 'incolore'})")
            if d["commanders"]:
                line += " — légendaires : " + ", ".join(d["commanders"])
            if d["cards"]:
                line += " — extrait : " + ", ".join(d["cards"])
            lines.append(line)
        parts.append("\n".join(lines))
    chat = signals["chat"]
    if chat["wishes"]:
        parts.append("DEMANDES RÉCENTES DU JOUEUR (chat) :\n"
                     + "\n".join(f"- {w}" for w in chat["wishes"]))
    if chat["generated"]:
        parts.append("RÉSULTATS GÉNÉRÉS POUR LUI :\n"
                     + "\n".join(f"- {g}" for g in chat["generated"]))
    if signals["events"]:
        parts.append("AUTRES ACTIONS DANS L'APP (formulaires) :\n"
                     + "\n".join(f"- {e}" for e in signals["events"]))
    return "\n\n".join(parts)


_SYNTH_SYSTEM = (
    "Tu es la mémoire d'une application d'aide au deckbuilding Magic: the "
    "Gathering. À partir des signaux fournis sur UN joueur (sa collection, les "
    "decks qu'il a construits, ses demandes récentes et ce qui a été généré "
    "pour lui), dresse le portrait de son STYLE DE JEU. Réponds UNIQUEMENT en "
    "JSON avec ces clés :\n"
    '- "summary": 2 à 4 phrases en français, qui TUTOIENT le joueur ("Tu '
    'aimes…"), décrivant son style : couleurs et stratégies de prédilection, '
    "formats joués, rapport au budget, ce qui semble lui plaire. Texte brut, "
    "pas de Markdown.\n"
    '- "style_tags": 3 à 6 étiquettes courtes en français (ex: "aristocrates", '
    '"budget serré", "tribal zombies", "contrôle").\n'
    '- "colors": les couleurs de prédilection, liste de symboles parmi '
    '"W","U","B","R","G" (2 ou 3 max, la ou les plus marquantes).\n'
    '- "formats": les formats qui reviennent (ex: "commander", "modern").\n'
    '- "favorite_cards": 0 à 6 noms de cartes qui semblent lui tenir à cœur, '
    "choisis STRICTEMENT parmi les cartes citées dans les signaux (n'invente "
    "AUCUN nom de carte).\n"
    "Ne déduis que ce que les signaux étayent ; si un aspect est inconnu, "
    "omets-le plutôt que d'inventer."
)


def _allowed_card_keys(signals: dict) -> set[str]:
    """Every card name present in the signals — the only quotable cards."""
    allowed: set[str] = set()
    for d in signals["decks"]:
        allowed.update(_norm(n) for n in d["cards"])
        allowed.update(_norm(n) for n in d["commanders"])
    return allowed


def _synthesize_llm(signals: dict) -> dict | None:
    raw = llm.chat_json(_SYNTH_SYSTEM, _signals_text(signals))
    if not raw or not isinstance(raw, dict):
        return None
    summary = (raw.get("summary") or "").strip()
    if not summary:
        return None
    allowed = _allowed_card_keys(signals)
    # Anchoring (invariant 1): the model produced card names — keep only those
    # that exist in the signals it was shown; an invented card is dropped.
    favorites = [
        str(n) for n in (raw.get("favorite_cards") or [])
        if isinstance(n, str) and _norm(n) in allowed
    ]
    return {
        "summary": summary,
        "style_tags": [str(t).strip() for t in (raw.get("style_tags") or [])
                       if str(t).strip()][:6],
        "colors": [c for c in _WUBRG if c in (raw.get("colors") or [])],
        "formats": [str(f).strip().lower() for f in (raw.get("formats") or [])
                    if str(f).strip()][:5],
        "favorite_cards": favorites[:6],
        "source": "llm",
    }


def _top_colors(signals: dict) -> list[str]:
    """Dominant colors: built decks count far more than the raw collection."""
    weight: dict[str, float] = {c: 0.0 for c in _WUBRG}
    breakdown = signals["collection"].get("colors") or {}
    total = sum(breakdown.get(c, 0) for c in _WUBRG) or 1
    for c in _WUBRG:
        weight[c] += breakdown.get(c, 0) / total
    for d in signals["decks"]:
        for c in d["colors"]:
            weight[c] += 1.0
    ranked = [c for c in _WUBRG if weight[c] > 0]
    ranked.sort(key=lambda c: -weight[c])
    return ranked[:3]


def _synthesize_heuristic(signals: dict) -> dict:
    """Key-free portrait assembled from the same signals, plain and honest."""
    colors = _top_colors(signals)
    formats: list[str] = []
    for line in signals["chat"]["generated"] + signals["events"]:
        for fmt in ("commander", "duelcommander", "paupercommander", "standard",
                    "modern", "pioneer", "pauper", "legacy", "vintage",
                    "premodern", "limited"):
            if fmt in line.lower() and fmt not in formats:
                formats.append(fmt)
    decks = signals["decks"]

    sentences: list[str] = []
    if colors:
        sentences.append(
            "Tes couleurs de prédilection : "
            + ", ".join(_COLOR_FR[c] for c in colors) + "."
        )
    if decks:
        named = [d["name"] for d in decks if d["name"] != "Deck sans nom"]
        head = f"Tu as {len(decks)} deck(s) construit(s) dans ta bibliothèque"
        sentences.append(
            head + (" (" + ", ".join(f"« {n} »" for n in named[:4]) + ")." if named else ".")
        )
    if formats:
        sentences.append("Formats explorés : " + ", ".join(formats[:4]) + ".")
    if signals["chat"]["wishes"]:
        sentences.append(
            f"{len(signals['chat']['wishes'])} demande(s) récente(s) dans le chat "
            "nourrissent ce portrait."
        )
    if not sentences:
        sentences.append(
            "Pas encore assez d'informations : importe ta collection ou discute "
            "avec l'assistant pour affiner ton profil."
        )
    tags = [_COLOR_FR[c] for c in colors[:2]] + formats[:2]
    if any(d["commanders"] for d in decks):
        tags.append("commandants")
    return {
        "summary": " ".join(sentences),
        "style_tags": tags[:6],
        "colors": colors,
        "formats": formats[:5],
        "favorite_cards": [],
        "source": "heuristic",
    }


# --- Storage + refresh ----------------------------------------------------

def get_profile(profile_id: int) -> dict | None:
    """The stored style portrait for a profile, or None if never built."""
    raw = db.get_meta(f"{PROFILE_META_PREFIX}{int(profile_id)}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("summary"):
        return None
    if data.get("updated_at"):
        data["updated_display"] = time.strftime(
            "%d/%m/%Y %H:%M", time.localtime(data["updated_at"])
        )
    return data


def refresh(profile_id: int, force: bool = False) -> dict | None:
    """Rebuild the portrait now. Returns the stored dict, or None if skipped.

    Skips when the signals fingerprint is unchanged (nothing new to learn) and
    the stored portrait was built by a source at least as good as what is
    available now — so gaining an API key upgrades a heuristic portrait on the
    next refresh even if the signals didn't move.
    """
    pid = int(profile_id)
    signals = gather_signals(pid)
    if not _has_signals(signals):
        return None
    fp = _fingerprint(signals)
    stored = get_profile(pid)
    if (not force and stored and stored.get("fingerprint") == fp
            and not (llm.is_available() and stored.get("source") == "heuristic")):
        return None

    profile = None
    if llm.is_available():
        profile = _synthesize_llm(signals)
    if profile is None:
        profile = _synthesize_heuristic(signals)
    profile.update({
        "fingerprint": fp,
        "updated_at": time.time(),
        "deck_count": len(signals["decks"]),
        "conversation_count": signals["chat"]["conversations"],
    })
    db.set_meta(f"{PROFILE_META_PREFIX}{pid}", json.dumps(profile))
    return profile


# Refreshes triggered by app events run off-request in a daemon thread. One
# worker per profile at a time; a trigger landing mid-refresh sets a dirty
# flag so the worker runs once more with the newer signals instead of being
# dropped (a library import must never be silently ignored).
_inflight: set[int] = set()
_dirty: set[int] = set()
_lock = threading.Lock()


def _worker(profile_id: int) -> None:
    while True:
        try:
            refresh(profile_id)
        except Exception:
            logger.exception("player-profile refresh failed (profile %s)", profile_id)
        with _lock:
            if profile_id in _dirty:
                _dirty.discard(profile_id)
                continue
            _inflight.discard(profile_id)
            return


def schedule_refresh(profile_id: int) -> None:
    """Refresh the portrait in the background (coalesced per profile)."""
    pid = int(profile_id)
    with _lock:
        if pid in _inflight:
            _dirty.add(pid)
            return
        _inflight.add(pid)
    try:
        threading.Thread(target=_worker, args=(pid,), daemon=True).start()
    except BaseException:
        with _lock:
            _inflight.discard(pid)
        raise


# --- Chat integration -----------------------------------------------------

def style_prompt_block(profile_id: int) -> str:
    """Short system-prompt block so the chat knows the player, or ""."""
    profile = get_profile(profile_id)
    if not profile:
        return ""
    lines = [
        "PROFIL DU JOUEUR (mémoire de l'app, déduite de ses decks et de ses "
        "échanges précédents) : " + profile["summary"]
    ]
    if profile.get("style_tags"):
        lines.append("Styles repérés : " + ", ".join(profile["style_tags"]) + ".")
    lines.append(
        "Tiens-en compte pour personnaliser tes réponses (exemples, "
        "suggestions par défaut), mais une demande explicite du joueur prime "
        "TOUJOURS sur ce profil."
    )
    return "\n".join(lines)
