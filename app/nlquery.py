"""Natural-language collection search: a French question → a Scryfall query.

The collection page has two entry points that end up in the same place: the
Scryfall query box (see ``app.scryquery``) and the plain-French box this module
serves. Translating rather than filtering directly is deliberate — the answer
is a *query*, which the real engine then parses and applies, so:

- nothing the model says is trusted: an invented filter key or a malformed
  expression is caught by ``scryquery.parse`` and never reaches the results
  (the anti-hallucination anchoring of invariant 1, applied to filters instead
  of card names);
- the generated query is shown back in the query box, so the user can read,
  correct and reuse it — the feature teaches the syntax instead of hiding it.

Without an Anthropic API key the app must keep working (see CLAUDE.md), so a
small keyword translator covers the common asks (colours, types, rarity, price,
mana value, owned copies) and anything it can't read comes back as an explicit
message rather than as a silently empty filter.
"""
import hashlib
import re
import unicodedata

from . import db, llm, scryquery
from .config import settings

CACHE_META_PREFIX = "nlquery:"

# Bumped whenever the prompt or the supported syntax changes, so cached
# translations from an older vocabulary are not reused.
_PROMPT_VERSION = "1"

_NONE_ANSWER = "NONE"
_FENCE_RE = re.compile(r"^```[a-z]*\s*|\s*```$", re.IGNORECASE)

_NO_LLM_MESSAGE = (
    "La recherche en langage naturel est limitée : sans clé API Anthropic, "
    "seuls quelques mots-clés (couleurs, types, rareté, prix, coût de mana) "
    "sont reconnus. Utilise la requête Scryfall pour les cas précis."
)
_UNREADABLE_MESSAGE = (
    "Je n'ai pas su traduire cette demande en filtres. Reformule-la, ou écris "
    "directement une requête Scryfall (voir l'aide sur la syntaxe)."
)


def _norm(text: str) -> str:
    """Lowercase and strip accents, so 'Créatures' and 'creatures' both match."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


# --- Heuristic fallback --------------------------------------------------

def _has(text: str, stem: str) -> bool:
    """Word-match ``stem`` in ``text``, tolerating French plural/feminine endings.

    Plain substring matching was tempting and wrong: "red" hides inside
    "vendredi", which turned a question with no colour in it into ``id<=r``.
    The suffix group covers vert/verte/verts/vertes and blanc/blanche/blanches.
    """
    return re.search(rf"\b{re.escape(stem)}(?:e?s?|he?s?)\b", text) is not None


# Word stems matched against the accent-stripped question (see ``_has``).
# French and English often collapse to the same normalised word ("créature" and
# "creature"); where they don't, both spellings are listed.
_COLOR_STEMS = {
    "blanc": "w", "white": "w",
    "bleu": "u", "blue": "u",
    "noir": "b", "black": "b",
    "rouge": "r", "red": "r",
    "vert": "g", "green": "g",
}
_GUILD_STEMS = {
    "azorius": "wu", "dimir": "ub", "rakdos": "br", "gruul": "rg",
    "selesnya": "gw", "orzhov": "wb", "izzet": "ur", "golgari": "bg",
    "boros": "rw", "simic": "gu", "bant": "gwu", "esper": "wub",
    "grixis": "ubr", "jund": "brg", "naya": "rgw", "abzan": "wbg",
    "jeskai": "urw", "sultai": "bgu", "mardu": "rwb", "temur": "gur",
}
_TYPE_STEMS = {
    "creature": "creature", "instant": "instant", "ephemere": "instant",
    "rituel": "sorcery", "sorcery": "sorcery",
    "artefact": "artifact", "artifact": "artifact",
    "enchantement": "enchantment", "enchantment": "enchantment",
    "planeswalker": "planeswalker", "arpenteur": "planeswalker",
    "terrain": "land", "land": "land",
    "legendaire": "legendary", "legendary": "legendary",
    "equipement": "equipment", "equipment": "equipment",
}
_RARITY_STEMS = (
    (("mythique", "mythic"), "r:mythic"),
    (("peu commune", "uncommon"), "r:uncommon"),
    (("commune", "common"), "r:common"),
    (("rare",), "r:rare"),
)
_KEYWORD_STEMS = {
    "vol": "flying", "flying": "flying",
    "pietinement": "trample", "trample": "trample",
    "contact mortel": "deathtouch", "deathtouch": "deathtouch",
    "lien de vie": "lifelink", "lifelink": "lifelink",
    "vigilance": "vigilance", "hate": "haste", "haste": "haste",
    "menace": "menace", "defenseur": "defender", "defender": "defender",
    "initiative": "first strike", "first strike": "first strike",
}
_FORMAT_STEMS = {
    "commander": "commander", "edh": "commander", "modern": "modern",
    "pioneer": "pioneer", "standard": "standard", "pauper": "pauper",
    "legacy": "legacy", "vintage": "vintage",
}

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_AT_MOST = r"(?:moins de|au plus|max(?:i(?:mum)?)?|jusqu'?a|sous|<=?)"
_AT_LEAST = r"(?:plus de|au moins|au dessus de|mini(?:mum)?|>=?)"

_PRICE = r"\s*(?:€|eur(?:os?)?)"
_MANA = r"\s*(?:manas?|cmc|mv|de (?:cout|valeur) de mana)"


def _price_terms(text: str) -> list[str]:
    out = []
    for pattern, op in ((_AT_MOST, "<="), (_AT_LEAST, ">=")):
        match = re.search(pattern + r"\s*" + _NUMBER + _PRICE, text)
        if match:
            out.append(f"eur{op}{match.group(1).replace(',', '.')}")
    return out


def _mana_terms(text: str) -> list[str]:
    out = []
    for pattern, op in ((_AT_MOST, "<="), (_AT_LEAST, ">=")):
        match = re.search(pattern + r"\s*" + _NUMBER + _MANA, text)
        if match:
            out.append(f"mv{op}{match.group(1).replace(',', '.')}")
    return out


def _rarity_term(text: str) -> str | None:
    """First matching rarity, most specific first ("peu commune" before "commune")."""
    for stems, term in _RARITY_STEMS:
        if any(stem in text if " " in stem else _has(text, stem) for stem in stems):
            return term
    return None


def _heuristic(question: str) -> str:
    """Best-effort translation without an LLM. Returns "" when nothing matched.

    Deliberately conservative: a term is emitted only for an unambiguous word,
    because a wrong filter silently hides cards the player owns.
    """
    text = _norm(question)
    parts: list[str] = []

    colors = ""
    for stem, letters in _GUILD_STEMS.items():
        if _has(text, stem):
            colors = letters
            break
    if not colors:
        colors = "".join(
            letters for stem, letters in _COLOR_STEMS.items() if _has(text, stem)
        )
    if colors:
        # `id<=` (identity within) is what a player means by "mes cartes
        # vertes" far more often than the stricter `c:` — same choice the LLM
        # prompt makes, and the same default as the filter panel.
        parts.append(f"id<={''.join(sorted(set(colors)))}")

    for stem, value in _TYPE_STEMS.items():
        if _has(text, stem):
            parts.append(f"t:{value}")
    for stem, value in _KEYWORD_STEMS.items():
        if re.search(rf"\b{re.escape(stem)}\b", text):
            parts.append(f"kw:{scryquery.quote(value)}")

    rarity = _rarity_term(text)
    if rarity:
        parts.append(rarity)
    parts.extend(_price_terms(text))
    parts.extend(_mana_terms(text))

    if _has(text, "commandant") or _has(text, "commander"):
        parts.append("is:commander")
    if "en deck" in text or "dans un deck" in text or "dans mes decks" in text:
        parts.append("is:indeck")
    elif _has(text, "disponible") or _has(text, "libre") or "pas dans un deck" in text:
        parts.append("is:spare")

    match = re.search(r"leg(?:al|aux)\w*\s+en\s+([a-z]+)", text)
    if match and match.group(1) in _FORMAT_STEMS:
        parts.append(f"f:{_FORMAT_STEMS[match.group(1)]}")

    # De-duplicate while keeping the order the terms were recognised in.
    seen: set[str] = set()
    return " ".join(p for p in parts if not (p in seen or seen.add(p)))


# --- LLM path ------------------------------------------------------------

def _clean(answer: str) -> str:
    """Reduce a model answer to the single query line it was asked for."""
    text = _FENCE_RE.sub("", (answer or "").strip()).strip()
    text = text.splitlines()[0].strip() if text else ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _cache_key(question: str) -> str:
    digest = hashlib.sha1(
        f"{_PROMPT_VERSION}|{settings.anthropic_model}|{_norm(question)}".encode()
    ).hexdigest()
    return f"{CACHE_META_PREFIX}{digest}"


def _llm_query(question: str) -> tuple[str, str | None]:
    """(query, error) from the LLM, validated by the real parser.

    One repair attempt: the model gets its own rejected query plus the parser's
    complaint. A second failure falls through to the heuristic rather than
    looping.
    """
    answer = _clean(llm.collection_query(question) or "")
    if not answer:
        return "", None
    if answer.upper() == _NONE_ANSWER:
        return "", _UNREADABLE_MESSAGE

    for attempt in range(2):
        try:
            scryquery.parse(answer)
        except scryquery.QueryError as exc:
            if attempt:
                return "", None
            retry = _clean(llm.collection_query(question, answer, str(exc)) or "")
            if not retry or retry.upper() == _NONE_ANSWER:
                return "", None
            answer = retry
            continue
        return answer, None
    return "", None


def translate(question: str) -> dict:
    """Turn ``question`` into ``{"query", "source", "error"}``.

    ``source`` is one of ``"cache"``, ``"llm"``, ``"heuristic"`` or ``""``.
    ``error`` is a French message to show the user; it can be set even with a
    usable query (e.g. the heuristic only caught part of the request).
    """
    question = (question or "").strip()
    if not question:
        return {"query": "", "source": "", "error": None}

    key = _cache_key(question)
    cached = db.get_meta(key)
    if cached is not None:
        return {"query": cached, "source": "cache", "error": None}

    error = None
    if llm.is_available():
        query, error = _llm_query(question)
        if query:
            db.set_meta(key, query)
            return {"query": query, "source": "llm", "error": None}

    query = _heuristic(question)
    if query:
        return {
            "query": query,
            "source": "heuristic",
            "error": None if llm.is_available() else _NO_LLM_MESSAGE,
        }
    return {
        "query": "",
        "source": "",
        "error": error or (_UNREADABLE_MESSAGE if llm.is_available() else _NO_LLM_MESSAGE),
    }
