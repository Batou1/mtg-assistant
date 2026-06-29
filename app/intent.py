"""Turn a free-text deck wish into a structured intent.

Primary path: the LLM (Claude via the Anthropic API). Fallback path: a small
heuristic parser so the app stays useful when no API key is set. Both produce
the same shape:

    {
      "format": "commander" | "standard" | "modern" | "pioneer" | "pauper" | None,
      "colors": ["W","U","B","R","G"]  (subset, may be empty),
      "theme": "<short free text>",
      "keywords": ["aristocrats", "sacrifice", ...],
      "budget_eur": float | None,
      "source": "llm" | "heuristic",
    }
"""
import re

from . import llm

VALID_COLORS = {"W", "U", "B", "R", "G"}
VALID_FORMATS = {"commander", "standard", "modern", "pioneer", "pauper", "legacy", "vintage"}

# French + English color words -> WUBRG symbol.
_COLOR_WORDS = {
    "white": "W", "blanc": "W", "blanche": "W",
    "blue": "U", "bleu": "U", "bleue": "U",
    "black": "B", "noir": "B", "noire": "B",
    "red": "R", "rouge": "R",
    "green": "G", "vert": "G", "verte": "G",
}

_FORMAT_WORDS = {
    "commander": "commander", "edh": "commander", "duel commander": "commander",
    "standard": "standard",
    "modern": "modern", "moderne": "modern",
    "pioneer": "pioneer",
    "pauper": "pauper",
    "legacy": "legacy",
    "vintage": "vintage",
}

# Theme keywords worth matching against EDHREC card pools (FR/EN).
_THEME_WORDS = [
    "aristocrats", "sacrifice", "tokens", "jetons", "lifegain", "gain de vie",
    "graveyard", "cimetiere", "reanimator", "mill", "meule", "counters",
    "+1/+1", "blink", "clignotement", "voltron", "equipment", "equipement",
    "aura", "spellslinger", "sorts", "artifacts", "artefacts", "lands", "terrains",
    "ramp", "control", "controle", "aggro", "agro", "midrange", "combo",
    "tribal", "elves", "elfes", "goblins", "gobelins", "zombies", "dragons",
    "vampires", "angels", "anges", "wizards", "mages", "dinosaurs", "dinosaures",
    "group hug", "stax", "burn", "tempo", "energy", "energie", "poison", "infect",
]

_SYSTEM_PROMPT = (
    "Tu es un assistant de deckbuilding Magic: the Gathering. "
    "L'utilisateur decrit en francais le deck qu'il veut construire. "
    "Reponds UNIQUEMENT avec un objet JSON valide, sans texte autour, avec ces cles:\n"
    '- "format": un parmi "commander","standard","modern","pioneer","pauper",'
    '"legacy","vintage", ou null si non precise.\n'
    '- "colors": liste de symboles parmi "W","U","B","R","G" (blanc, bleu, noir, '
    "rouge, vert). Liste vide si non precise.\n"
    '- "theme": courte description du theme/archetype en quelques mots.\n'
    '- "keywords": liste de mots-cles en anglais decrivant la strategie '
    '(ex: "aristocrats","tokens","reanimator","ramp").\n'
    '- "max_colors": entier = nombre MAXIMUM de couleurs autorisees si '
    'l\'utilisateur le precise ("monocouleur"/"mono"/"monocolore" => 1, '
    '"bicolore"/"deux couleurs" => 2), sinon null.\n'
    '- "budget_eur": nombre (euros) si un budget est mentionne, sinon null.\n'
    "RESPECTE STRICTEMENT les couleurs, le nombre de couleurs et le theme "
    'demandes. Pour "noir OU blanc monocouleur", colors=["B","W"] et '
    "max_colors=1 (chaque deck propose sera mono-noir OU mono-blanc). "
    "N'invente jamais de noms de cartes."
)


def _coerce(data: dict) -> dict:
    """Validate/normalize an LLM (or heuristic) intent into the canonical shape."""
    fmt = data.get("format")
    fmt = fmt.lower() if isinstance(fmt, str) and fmt.lower() in VALID_FORMATS else None

    colors = []
    for c in data.get("colors") or []:
        c = str(c).strip().upper()
        if c in VALID_COLORS and c not in colors:
            colors.append(c)

    theme = str(data.get("theme") or "").strip()

    keywords = []
    for k in data.get("keywords") or []:
        k = str(k).strip().lower()
        if k and k not in keywords:
            keywords.append(k)

    budget = data.get("budget_eur")
    try:
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        budget = None

    max_colors = data.get("max_colors")
    try:
        max_colors = int(max_colors) if max_colors is not None else None
    except (TypeError, ValueError):
        max_colors = None
    if max_colors is not None and max_colors < 1:
        max_colors = None

    return {
        "format": fmt,
        "colors": colors,
        "max_colors": max_colors,
        "theme": theme,
        "keywords": keywords,
        "budget_eur": budget,
        "include_low_decks": bool(data.get("include_low_decks")),
        "unowned_only": bool(data.get("unowned_only")),
        "source": data.get("source", "llm"),
    }


def _heuristic(text: str) -> dict:
    """Regex/keyword fallback when the LLM is unavailable."""
    low = " " + text.lower() + " "

    fmt = None
    for word, canonical in _FORMAT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            fmt = canonical
            break

    colors = []
    for word, sym in _COLOR_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low) and sym not in colors:
            colors.append(sym)

    keywords = [w for w in _THEME_WORDS if w in low]

    # "monocouleur", "mono noir", "monocolore" -> at most one colour.
    max_colors = 1 if re.search(r"\bmono", low) else None

    # Opt-in to niche commanders under the EDHREC popularity floor.
    include_low_decks = bool(
        re.search(r"\b(peu jou|rares?|niche|confidentiel|méconnus?|meconnus?|"
                  r"obscurs?|sous le seuil|sous le palier)", low)
    )

    # Restrict to commanders the player doesn't already own.
    unowned_only = bool(
        re.search(r"(que je n['e ]?ai pas|que je ne poss|pas dans ma collection|"
                  r"je ne poss[èe]de pas|nouveau commandant|à acqu[ée]rir|"
                  r"que je ne d[ée]tiens pas)", low)
    )

    budget = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:€|euros?|eur)\b", low)
    if not m:
        m = re.search(r"budget[^\d]{0,12}(\d+(?:[.,]\d+)?)", low)
    if m:
        budget = float(m.group(1).replace(",", "."))

    return _coerce(
        {
            "format": fmt,
            "colors": colors,
            "max_colors": max_colors,
            "theme": text.strip()[:120],
            "keywords": keywords,
            "budget_eur": budget,
            "include_low_decks": include_low_decks,
            "unowned_only": unowned_only,
            "source": "heuristic",
        }
    )


def parse_intent(text: str) -> dict:
    """Parse ``text`` into a structured intent, LLM-first with heuristic fallback."""
    text = (text or "").strip()
    if not text:
        return _coerce({"theme": "", "source": "heuristic"})

    data = llm.chat_json(_SYSTEM_PROMPT, text)
    if data is not None:
        intent = _coerce({**data, "source": "llm"})
        heur = _heuristic(text)
        # If the model returned an empty theme, keep the user's text as theme.
        if not intent["theme"]:
            intent["theme"] = text[:120]
        # Backfill from the heuristic whatever the model dropped. Colours and
        # the mono constraint matter most: a missing colour list would silently
        # disable the colour filter and surface off-colour commanders.
        if not intent["keywords"]:
            intent["keywords"] = heur["keywords"]
        if not intent["colors"]:
            intent["colors"] = heur["colors"]
        if intent["max_colors"] is None:
            intent["max_colors"] = heur["max_colors"]
        return intent

    return _heuristic(text)
