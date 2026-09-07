"""LLM access via the Anthropic API (Claude).

The LLM turns a free-text wish into a structured intent, proposes 60-card
archetypes, and writes deck game-plan summaries. Card selection stays
deterministic elsewhere; for 60-card formats the model names cards but every
card is validated against Scryfall downstream, so nothing fake/illegal shows.

The API key is read by the SDK from the ANTHROPIC_API_KEY environment variable
(kept in a gitignored .env). If it's absent, is_available() is False and callers
fall back gracefully (heuristic intent parsing, no game plan, etc.).
"""
import json
import os
import re

import anthropic

from .config import settings

_client: anthropic.Anthropic | None = None
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def is_available() -> bool:
    """True if an Anthropic API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the env
    return _client


def _message(system: str, user: str, max_tokens: int) -> str | None:
    """Single-turn request; returns the concatenated text, or None on failure."""
    if not is_available():
        return None
    try:
        resp = _get_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError:
        return None
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def create_message(system: str, messages: list[dict], tools: list[dict] | None = None,
                   max_tokens: int | None = None, tool_choice: dict | None = None):
    """Low-level call returning the full Anthropic response (or None on failure).

    Unlike ``chat_json``/``chat_text``, this exposes the raw response so callers
    can inspect ``stop_reason`` and ``tool_use`` blocks to drive an agent loop.
    ``tool_choice={"type": "none"}`` forces a text answer while keeping ``tools``
    declared — required when the transcript already contains tool blocks.
    """
    if not is_available():
        return None
    kwargs = {
        "model": settings.anthropic_model,
        "max_tokens": max_tokens or settings.anthropic_max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    try:
        return _get_client().messages.create(**kwargs)
    except anthropic.APIError:
        return None


def chat_json(system: str, user: str, max_tokens: int | None = None) -> dict | None:
    """Ask for a strict JSON object back. Returns the parsed dict or None.

    ``max_tokens`` must cover the model's adaptive-thinking spend PLUS the JSON
    itself; a truncated response is invalid JSON and comes back as None, so
    callers expecting long outputs (full decklists) should pass a large budget.
    """
    system = (
        system
        + "\n\nRéponds UNIQUEMENT avec un objet JSON valide, sans texte autour "
        "ni balises Markdown."
    )
    content = _message(system, user, max_tokens or settings.anthropic_max_tokens)
    if content is None:
        return None
    content = _FENCE_RE.sub("", content).strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def chat_text(system: str, user: str) -> str | None:
    """Ask for free-form text back, or None if unavailable."""
    return _message(system, user, 800) or None


# Filter vocabulary handed to the model for collection search. It mirrors
# app/scryquery.py (_KEYS) — keep the two in sync when adding a filter. The
# model can only ever *propose* a query: app/nlquery.py re-parses whatever
# comes back with the real engine, so an invented key is rejected rather than
# silently applied (anti-hallucination anchoring, invariant 1).
_QUERY_SYNTAX = """\
- t:<type ou sous-type> — ex. t:creature, t:goblin, t:equipment, t:legendary
- o:<texte> — texte de règles (oracle), en ANGLAIS ; guillemets si plusieurs mots
- kw:<mot-clé> — mot-clé officiel en anglais : kw:flying, kw:trample, kw:deathtouch
- mv, pow, tou, loy — coût de mana converti, force, endurance, loyauté
  (opérateurs : mv:3, mv<=3, mv>3, mv>=2)
- r:<rareté> — common, uncommon, rare, mythic (r>=rare fonctionne)
- c:<couleurs> — couleurs de la carte : c:r, c:rg (contient), c=r (exactement),
  c:m (multicolore), c:c (incolore)
- id<=<couleurs> — identité de couleur INCLUSE dans (ce qui est jouable sous un
  commandant) ; accepte aussi les noms de guildes : id<=izzet, id<=jund
- m:<coût> — symboles du coût de mana, ex. m:{2}{R}
- s:<code d'édition> — ex. s:mh3 ; year>=2020 ; a:"<artiste>"
- f:<format> — légalité : f:commander, f:modern, f:pauper… ; banned:<format>
- is:commander, is:legendary, is:permanent, is:vanilla, is:dfc, is:reserved,
  is:multicolor, is:colorless, is:mono, is:hybrid, is:reprint, is:promo
- eur — prix unitaire en euros : eur<=5, eur>20
- qty, deck — exemplaires POSSÉDÉS et exemplaires déjà rangés dans un deck :
  qty>=4, deck>0 ; is:indeck (au moins une copie en deck),
  is:spare (au moins une copie libre)
- name:<texte> — nom de la carte ; !"Nom Exact" pour une correspondance exacte
Combinaisons : espace = ET, `or` = OU, `-` devant un filtre = exclusion,
parenthèses pour grouper. Ex. `t:creature id<=g mv<=3 -o:defender`."""

_QUERY_SYSTEM = (
    "Tu traduis la demande d'un joueur de Magic: the Gathering, écrite en "
    "français courant, en UNE requête de recherche au format Scryfall qui "
    "filtrera SA COLLECTION de cartes.\n\n"
    "Filtres autorisés (n'en utilise AUCUN autre) :\n" + _QUERY_SYNTAX + "\n\n"
    "Règles :\n"
    "- Réponds UNIQUEMENT par la requête, sur une seule ligne, sans explication, "
    "sans guillemets autour, sans balise Markdown.\n"
    "- Les valeurs de t:, o: et kw: s'écrivent en ANGLAIS (les cartes sont en "
    "anglais), même si la demande est en français.\n"
    "- Traduis l'intention, pas les mots : « ce que je peux jouer sous un "
    "commandant Simic » → id<=gu, « mes bombes » → mv>=5 t:creature, « pas cher » "
    "→ eur<=2.\n"
    "- Une demande de couleur ambiguë se traduit par l'identité (id<=), qui est "
    "ce qu'un joueur veut presque toujours dire.\n"
    "- N'ajoute aucun filtre qui n'est pas demandé, et surtout aucun nom de carte "
    "que le joueur n'a pas cité.\n"
    "- Si la demande ne contient aucun critère traduisible, réponds exactement : NONE"
)


def collection_query(question: str, previous: str | None = None,
                     parse_error: str | None = None) -> str | None:
    """Translate a French question into a Scryfall query string.

    ``previous``/``parse_error`` feed one repair attempt: the caller parses the
    first answer with the real engine and, when it doesn't compile, hands the
    model its own output plus the parser's message. That keeps the model honest
    without a free-form retry loop.
    """
    user = f"Demande du joueur : {question}"
    if previous and parse_error:
        user += (
            f"\n\nTa réponse précédente était « {previous} » et le moteur de "
            f"recherche l'a refusée : {parse_error}\n"
            "Corrige-la en n'utilisant QUE les filtres autorisés."
        )
    return _message(_QUERY_SYSTEM, user, 300)


def _budget_rules(intent: dict) -> list[str]:
    """Budget constraints, worded for a deck proposal (French, for the prompt).

    The two budget knobs stay independent — a total and a per-card ceiling —
    exactly as they are downstream (buylist, deckgen).
    """
    rules: list[str] = []
    budget = intent.get("budget_eur")
    cap = intent.get("max_card_price_eur")
    if budget is not None:
        rules.append(
            f"BUDGET STRICT : {budget:.0f} € MAXIMUM pour l'ensemble des cartes du "
            "deck (prix Cardmarket en euros ; les terrains de base sont gratuits). "
            "C'est une contrainte PRIORITAIRE sur la puissance : choisis la "
            "déclinaison « budget » de l'archétype et écarte les cartes hors de "
            "prix (Reserved List, duals, Power…) au profit d'alternatives "
            "abordables qui remplissent le même rôle. Un deck légèrement moins "
            "fort mais achetable vaut mieux qu'un deck injouable pour ce budget."
        )
    if cap is not None:
        rules.append(
            f"PLAFOND PAR CARTE : aucune carte du deck ne doit dépasser {cap:.0f} € "
            "l'unité."
        )
    return rules


def _archetype_system(fmt: str, intent: dict) -> str:
    system = (
        f"Tu es un expert Magic: the Gathering, format {fmt}. À partir de l'envie "
        "du joueur et d'extraits web récents sur le métagame, propose UN deck "
        "compétitif et réaliste de 60 cartes EXACTEMENT, FIDÈLE aux couleurs et à "
        "la stratégie demandées. Réponds UNIQUEMENT en JSON avec ces clés :\n"
        '- "archetype": nom court de l\'archétype.\n'
        '- "colors": liste de symboles parmi "W","U","B","R","G".\n'
        '- "strategy": 2-3 phrases en français décrivant le plan de jeu, en texte brut (pas de Markdown).\n'
        '- "main_deck": liste d\'objets {"name","count"} — TOUS les sorts ET les '
        "terrains non-basiques du deck, avec leur nombre d'exemplaires (count "
        "entre 1 et 4, règle des 4 exemplaires maximum). Reprends les proportions "
        "typiques de l'archétype : 4 exemplaires des cartes clés, moins pour les "
        "cartes situationnelles.\n"
        '- "basic_lands": objet {"Plains":n,"Island":n,"Swamp":n,"Mountain":n,'
        '"Forest":n} — les terrains de base qui complètent la manabase (omets les '
        "couleurs non jouées).\n"
        "Le deck complet (main_deck + basic_lands) fait EXACTEMENT 60 cartes, avec "
        "une manabase complète de 20 à 26 terrains adaptée à la courbe de mana. "
        "Noms de cartes RÉELS, en anglais, avec l'orthographe EXACTE (telle que "
        f"sur la carte), tous légaux en {fmt}. N'invente AUCUNE carte ; si tu "
        f"n'es pas certain qu'une carte existe et est légale en {fmt}, ne la mets "
        "pas. Privilégie les staples reconnus."
    )
    for rule in _budget_rules(intent):
        system += "\n" + rule
    return system


def _archetype_user(fmt: str, intent: dict) -> list[str]:
    parts = [f"Format : {fmt}"]
    if intent.get("theme"):
        parts.append(f"Envie du joueur : {intent['theme']}")
    if intent.get("keywords"):
        parts.append(f"Mots-clés : {', '.join(intent['keywords'])}")
    if intent.get("colors"):
        wanted = ", ".join(intent["colors"])
        min_colors = intent.get("min_colors")
        if min_colors is not None and min_colors >= len(intent["colors"]):
            parts.append(
                f"Couleurs imposées : {wanted} — le deck doit jouer TOUTES ces "
                "couleurs (pas un sous-ensemble)."
            )
        else:
            parts.append(f"Couleurs souhaitées : {wanted}")
    # Player-forced cards: the caller (formats60.analyze) still validates and
    # force-adds any the model leaves out, but asking here yields sensible
    # copy counts and a deck built AROUND them rather than a bolt-on.
    if intent.get("include_cards"):
        parts.append(
            "Cartes IMPOSÉES par le joueur — le deck DOIT les contenir, avec un "
            "nombre d'exemplaires adapté à leur rôle, et être construit autour : "
            + ", ".join(intent["include_cards"])
        )
    if intent.get("exclude_cards"):
        parts.append(
            "Cartes INTERDITES — ne les inclus PAS dans le deck : "
            + ", ".join(intent["exclude_cards"])
        )
    return parts


def archetype_research(fmt: str, intent: dict, context: str) -> dict | None:
    """Propose a complete 60-card decklist for a non-singleton format.

    Given the format, the player's wish and recent web-search context, return a
    JSON archetype with a full main deck (multiple copies allowed, 4-of rule)
    and a basic-land manabase. Every card name is validated against Scryfall
    downstream and copy counts are re-clamped, so a few hallucinated names are
    filtered out rather than trusted.

    The model only *aims* at the budget here (it prices cards from memory);
    ``archetype_revise`` is what enforces it, with real Cardmarket prices.
    """
    parts = _archetype_user(fmt, intent)
    if context:
        parts.append(f"\nExtraits web récents (métagame) :\n{context}")
    return chat_json(_archetype_system(fmt, intent), "\n".join(parts),
                     max_tokens=settings.anthropic_deck_max_tokens)


def archetype_revise(fmt: str, intent: dict, previous: dict, price_report: str,
                     cost_eur: float) -> dict | None:
    """Rebuild the 60-card deck under budget, given its REAL prices.

    The first proposal is priced from the model's memory, which is why it
    routinely lands far above budget. Here it gets the actual Cardmarket price
    of every card it just picked plus the resulting total, so the rewrite is
    grounded in numbers instead of guesses. Same output shape as
    ``archetype_research`` — the caller re-validates it identically.
    """
    budget = intent.get("budget_eur")
    parts = _archetype_user(fmt, intent)
    parts.append(
        f"\nTa proposition précédente (« {previous.get('archetype') or 'deck'} ») "
        f"coûte {cost_eur:.2f} € à l'achat, soit BIEN AU-DESSUS du budget de "
        f"{budget:.0f} €. Prix Cardmarket réels des cartes que tu avais "
        f"choisies :\n{price_report}"
    )
    parts.append(
        "\nPropose maintenant une NOUVELLE liste complète de 60 cartes pour la même "
        "envie, dont le coût total tient sous le budget. Remplace les cartes les "
        "plus chères ci-dessus par des alternatives abordables jouant le même rôle "
        "(mêmes effets, coût de mana proche) ; garde les cartes bon marché qui "
        "fonctionnent. Si l'archétype demandé est intrinsèquement inaccessible à ce "
        "budget, bascule sur la variante budget la plus proche et explique-le dans "
        '"strategy". Même format JSON que précédemment.'
    )
    return chat_json(_archetype_system(fmt, intent), "\n".join(parts),
                     max_tokens=settings.anthropic_deck_max_tokens)


def archetype_from_collection(fmt: str, intent: dict, pool_lines: list[str],
                              context: str = "") -> dict | None:
    """Propose a 60-card deck using ONLY the player's own cards.

    The zero-budget counterpart of ``archetype_research``: the model gets the
    owned pool (name, available copies, cost, type, text) instead of a
    shopping licence, and picks the best 60 from it. Same JSON shape as the
    other archetype calls; ``formats60`` re-checks every name against the
    collection and clamps copies to what is owned, so a card named outside
    the pool is dropped rather than bought.
    """
    system = (
        f"Tu es un expert Magic: the Gathering, format {fmt}. Le joueur veut un "
        "deck construit UNIQUEMENT avec les cartes qu'il possède déjà : il "
        "n'achètera RIEN. Choisis, parmi les cartes de sa collection listées "
        "ci-dessous (toutes légales dans le format), le MEILLEUR deck de 60 "
        "cartes EXACTEMENT, cohérent avec l'envie, les couleurs et la stratégie "
        "demandées. Réponds UNIQUEMENT en JSON avec ces clés :\n"
        '- "archetype": nom court de l\'archétype.\n'
        '- "colors": liste de symboles parmi "W","U","B","R","G".\n'
        '- "strategy": 2-3 phrases en français décrivant le plan de jeu, en texte brut (pas de Markdown).\n'
        '- "main_deck": liste d\'objets {"name","count"} — TOUS les sorts ET les '
        "terrains non-basiques du deck. Chaque name est recopié EXACTEMENT depuis "
        "la liste fournie, et count ne dépasse JAMAIS le nombre d'exemplaires "
        "indiqué (x2 => au plus 2 ; sans mention => 1), ni 4.\n"
        '- "basic_lands": objet {"Plains":n,"Island":n,"Swamp":n,"Mountain":n,'
        '"Forest":n} — les terrains de base, disponibles sans limite.\n'
        "Le deck complet (main_deck + basic_lands) fait EXACTEMENT 60 cartes, avec "
        "20 à 26 terrains adaptés à la courbe de mana. N'ajoute AUCUNE carte "
        "absente de la liste, même une carte évidente de l'archétype : elle "
        "serait retirée. Si la collection ne permet pas l'archétype idéal, "
        "construis le meilleur deck possible avec ce qui est là et explique le "
        'compromis dans "strategy".'
    )
    parts = _archetype_user(fmt, intent)
    if context:
        parts.append(f"\nExtraits web récents (métagame, pour t'inspirer) :\n{context}")
    parts.append("\nCartes possédées disponibles (nom | coût | type | texte) :\n"
                 + "\n".join(pool_lines))
    return chat_json(system, "\n".join(parts),
                     max_tokens=settings.anthropic_deck_max_tokens)


def pool_deck(spec, intent: dict, pool_lines: list[str]) -> dict | None:
    """Pick the best deck from a fixed card pool, per a format spec.

    Generic over ``spec`` (deck size, land target, singleton, label) so the same
    call serves Limited today and Commander/Modern/Pauper-from-pool later. The
    model names cards but only ones from ``pool_lines``; the caller re-checks
    every chosen name against the pool, so nothing outside it can slip through.
    """
    rules = [f"Construis le MEILLEUR deck de {spec.deck_size} cartes pour le format « {spec.label} »."]
    if spec.add_basics:
        rules.append(
            f"Vise environ {spec.target_lands} terrains au total. Les terrains de "
            "base (Plains/Island/Swamp/Mountain/Forest) ne font PAS partie du pool "
            "et s'ajoutent librement."
        )
    if spec.singleton:
        rules.append("Une seule copie maximum par carte (singleton).")
    else:
        rules.append("Plusieurs exemplaires d'une carte sont permis seulement si le pool en contient assez.")
    if spec.needs_commander:
        rules.append(
            "Choisis un COMMANDANT (créature légendaire) présent dans le pool ; le "
            "deck (99 cartes + le commandant) respecte STRICTEMENT son identité de couleur."
        )
    if intent.get("colors"):
        rule = f"Reste STRICTEMENT dans les couleurs imposées : {', '.join(intent['colors'])}."
        min_colors = intent.get("min_colors")
        if min_colors is not None and min_colors >= len(intent["colors"]):
            rule += " Le deck doit jouer TOUTES ces couleurs (pas un sous-ensemble)."
        rules.append(rule)
    else:
        rules.append("Choisis automatiquement la/les meilleure(s) couleur(s) à jouer d'après le pool.")
    rules.append(
        "N'utilise QUE des cartes présentes dans le POOL ci-dessous, avec leur "
        "orthographe EXACTE. N'invente AUCUNE carte."
    )
    system = (
        "Tu es un expert Magic: the Gathering, spécialiste du deckbuilding. "
        + " ".join(rules)
        + ' Réponds UNIQUEMENT en JSON avec ces clés :\n'
        '- "archetype": nom court de l\'archétype/du deck.\n'
        '- "colors": liste de symboles parmi "W","U","B","R","G".\n'
        '- "strategy": 2-3 phrases en français (plan de jeu, comment gagner), en texte brut (pas de Markdown).\n'
        '- "main_deck": liste d\'objets {"name","count"} des cartes NON terrain de '
        "base à jouer (count = nombre de copies prises dans le pool).\n"
        '- "basic_lands": objet {"Plains":n,"Island":n,...} de terrains de base '
        "pour compléter le deck (vide si non pertinent)."
        + ('\n- "commander": nom EXACT du commandant choisi (du pool).'
           if spec.needs_commander else "")
    )
    parts = [f"Format : {spec.label} ({spec.deck_size} cartes)."]
    if intent.get("theme"):
        parts.append(f"Souhait du joueur : {intent['theme']}")
    if intent.get("keywords"):
        parts.append(f"Mots-clés : {', '.join(intent['keywords'])}")
    if intent.get("colors"):
        parts.append(f"Couleurs imposées : {', '.join(intent['colors'])}")
    parts.append("POOL DISPONIBLE :\n" + "\n".join(pool_lines))
    # A Commander answer is ~90 singleton JSON entries; the default budget gets
    # eaten by thinking + truncated mid-JSON (=> None => heuristic fallback).
    return chat_json(system, "\n".join(parts),
                     max_tokens=settings.anthropic_deck_max_tokens)


def pool_bonus(spec, archetype: dict, deck_cards: list[str], colors: list[str],
               owned_eligible: list[str]) -> dict | None:
    """Suggest synergy cards beyond the deck: owned ones + a few to buy.

    ``owned_eligible`` is the pre-filtered list of cards the player owns that are
    legal and on-colour. The model may only pick owned bonus cards FROM that
    list (re-checked by the caller); buy suggestions are new cards, validated
    against Scryfall downstream.
    """
    system = (
        f"Tu es un expert Magic: the Gathering, format « {spec.label} ». On te "
        "donne un deck déjà construit et la liste des cartes que le joueur POSSÈDE "
        "et qui sont jouables dans ce deck. Recommande des cartes BONUS à ajouter "
        "EN PLUS du deck (renforts/synergies), sans contrainte de taille de deck. "
        "Réponds UNIQUEMENT en JSON avec :\n"
        '- "owned_bonus": liste de noms choisis STRICTEMENT dans la liste "Cartes '
        "possédées éligibles\" fournie (les plus synergiques avec le deck).\n"
        '- "buy_bonus": liste de 5 à 10 noms de cartes RÉELLES (orthographe exacte, '
        f"légales en {spec.label}) que le joueur ne possède pas mais qui "
        "amélioreraient le deck. N'invente aucune carte."
    )
    parts = [f"Deck : {archetype.get('name')} ({'/'.join(colors) or 'incolore'})."]
    if archetype.get("strategy"):
        parts.append(f"Stratégie : {archetype['strategy']}")
    parts.append("Cartes clés du deck : " + ", ".join(deck_cards[:40]))
    if owned_eligible:
        parts.append("Cartes possédées éligibles :\n" + "\n".join(f"- {n}" for n in owned_eligible))
    else:
        parts.append("Cartes possédées éligibles : (aucune)")
    return chat_json(system, "\n".join(parts))


def deck_gameplan(commander_name: str, card_names: list[str], theme: str = "") -> str | None:
    """Write a short French game-plan summary from the chosen cards.

    The model is given the actual card names, so it describes the deck rather
    than inventing cards. Returns None when the LLM is unavailable.
    """
    system = (
        "Tu es un expert Magic: the Gathering (format Commander). On te donne un "
        "commandant et une sélection de cartes RÉELLES du deck. Rédige en français "
        "le plan de jeu en TEXTE BRUT UNIQUEMENT (aucun Markdown : pas de #, pas de "
        "**, pas de listes à puces), organisé en EXACTEMENT 3 paragraphes séparés "
        "chacun par UNE LIGNE VIDE :\n"
        "1. La stratégie générale du deck (1-2 phrases).\n"
        "2. Comment le deck gagne concrètement (1-2 phrases).\n"
        "3. Un ou deux conseils de pilotage (1-2 phrases).\n"
        "Ne mentionne que des cartes de la liste fournie. N'invente aucune carte."
    )
    sample = ", ".join(card_names[:18])
    user = f"Commandant : {commander_name}\n"
    if theme:
        user += f"Thème souhaité : {theme}\n"
    user += f"Cartes clés du deck : {sample}"
    return chat_text(system, user)
