"""Turn a free-text deck wish into a structured intent.

Primary path: the LLM (Claude via the Anthropic API). Fallback path: a small
heuristic parser so the app stays useful when no API key is set. Both produce
the same shape:

    {
      "format": "commander" | "standard" | "modern" | "pioneer" | "pauper" |
                "legacy" | "vintage" | "premodern" | None,
      "colors": ["W","U","B","R","G"]  (subset, may be empty),
      "theme": "<short free text>",
      "keywords": ["aristocrats", "sacrifice", ...],
      "budget_eur": float | None,
      "max_card_price_eur": float | None,
      "source": "llm" | "heuristic",
    }
"""
import re

from . import llm

VALID_COLORS = {"W", "U", "B", "R", "G"}
VALID_FORMATS = {"commander", "standard", "modern", "pioneer", "pauper", "legacy",
                 "vintage", "premodern"}

# French + English color words -> WUBRG symbol.
_COLOR_WORDS = {
    "white": "W", "blanc": "W", "blanche": "W",
    "blue": "U", "bleu": "U", "bleue": "U",
    "black": "B", "noir": "B", "noire": "B",
    "red": "R", "rouge": "R",
    "green": "G", "vert": "G", "verte": "G",
}

# Guild / shard / wedge names -> the colours they stand for. Players routinely
# describe a deck by these (e.g. "Grixis commanders") rather than by colour, so
# without this the colour filter stays empty and off-colour commanders surface
# while the requested ones get buried.
_GUILD_SHARD_WORDS = {
    # Two-colour guilds.
    "azorius": ["W", "U"], "dimir": ["U", "B"], "rakdos": ["B", "R"],
    "gruul": ["R", "G"], "selesnya": ["G", "W"], "orzhov": ["W", "B"],
    "izzet": ["U", "R"], "golgari": ["B", "G"], "boros": ["R", "W"],
    "simic": ["G", "U"],
    # Three-colour shards (allied) and wedges (enemy).
    "bant": ["G", "W", "U"], "esper": ["W", "U", "B"], "grixis": ["U", "B", "R"],
    "jund": ["B", "R", "G"], "naya": ["R", "G", "W"],
    "abzan": ["W", "B", "G"], "jeskai": ["U", "R", "W"], "sultai": ["B", "G", "U"],
    "mardu": ["R", "W", "B"], "temur": ["G", "U", "R"],
    # Four/five colour shorthands.
    "wubrg": ["W", "U", "B", "R", "G"], "5c": ["W", "U", "B", "R", "G"],
}

_FORMAT_WORDS = {
    "commander": "commander", "edh": "commander", "duel commander": "commander",
    "standard": "standard",
    # Premodern must be tried before "modern": "pre-modern" / "premoderne"
    # otherwise the \bmodern\b probe could match the hyphenated spelling.
    "premodern": "premodern", "pre-modern": "premodern", "pre modern": "premodern",
    "premoderne": "premodern", "prémoderne": "premodern",
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
    '"legacy","vintage","premodern", ou null si non precise. '
    '("premodern"/"pre-modern" = le format retro 4e edition a Scourge.)\n'
    '- "colors": liste de symboles parmi "W","U","B","R","G" (blanc, bleu, noir, '
    "rouge, vert). Liste vide si non precise. Convertis les noms de "
    "guildes/shards/wedges en couleurs (ex: Grixis=U,B,R ; Rakdos=B,R ; "
    "Jeskai=U,R,W ; Esper=W,U,B ; Bant=G,W,U).\n"
    '- "theme": courte description du theme/archetype en quelques mots.\n'
    '- "keywords": liste de mots-cles en anglais decrivant la strategie '
    '(ex: "aristocrats","tokens","reanimator","ramp").\n'
    '- "max_colors": entier = nombre MAXIMUM de couleurs autorisees si '
    'l\'utilisateur le precise ("monocouleur"/"mono"/"monocolore" => 1, '
    '"bicolore"/"deux couleurs" => 2), sinon null.\n'
    '- "budget_eur": nombre (euros) = budget TOTAL si mentionne, sinon null.\n'
    '- "max_card_price_eur": nombre (euros) = prix MAXIMUM par carte, '
    "UNIQUEMENT si l'utilisateur precise un plafond individuel distinct du "
    'budget total (ex: "budget 30 euros mais pas plus de 5 euros par carte" '
    "=> budget_eur=30, max_card_price_eur=5), sinon null.\n"
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

    max_card_price = data.get("max_card_price_eur")
    try:
        max_card_price = float(max_card_price) if max_card_price is not None else None
    except (TypeError, ValueError):
        max_card_price = None
    if max_card_price is not None and max_card_price <= 0:
        max_card_price = None

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
        "max_card_price_eur": max_card_price,
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
    # Guild/shard/wedge names expand to their colours (e.g. "grixis" -> U,B,R).
    for word, syms in _GUILD_SHARD_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            for sym in syms:
                if sym not in colors:
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

    # A per-card price cap ("max 5€ par carte", "chaque carte à 5€ max", "pas
    # plus de 5 euros par carte") is looked for FIRST and removed from the text
    # before the general budget regex runs below — otherwise "budget 30€ max
    # 5€ par carte" would let the generic budget probe swallow the "5€" as the
    # (wrong) total budget on some phrasings.
    max_card_price = None
    for pattern in (
        r"(\d+(?:[.,]\d+)?)\s*(?:€|euros?|eur)\s*(?:maximum|max)?\s*(?:par|/|la)\s*carte",
        r"chaque\s+carte[^\d]{0,40}(\d+(?:[.,]\d+)?)\s*(?:€|euros?|eur)",
        r"cartes?[^\d]{0,15}(?:à|a)\s*(\d+(?:[.,]\d+)?)\s*(?:€|euros?|eur)\s*max",
        r"(?:plafond|max(?:imum)?)[^\d]{0,20}par\s+carte[^\d]{0,10}(\d+(?:[.,]\d+)?)",
    ):
        m = re.search(pattern, low)
        if m:
            max_card_price = float(m.group(1).replace(",", "."))
            low = low[: m.start()] + low[m.end() :]
            break

    budget = None
    # ``€`` is a symbol, so a trailing \b never matches after it ("300€"); guard
    # the letter forms (euro/eur) against false hits like "europe" with a
    # negative lookahead instead.
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:€|euros?|eur)(?![a-z])", low)
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
            "max_card_price_eur": max_card_price,
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
        if intent["max_card_price_eur"] is None:
            intent["max_card_price_eur"] = heur["max_card_price_eur"]
        return intent

    return _heuristic(text)
