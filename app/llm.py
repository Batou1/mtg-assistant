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
                   max_tokens: int | None = None):
    """Low-level call returning the full Anthropic response (or None on failure).

    Unlike ``chat_json``/``chat_text``, this exposes the raw response so callers
    can inspect ``stop_reason`` and ``tool_use`` blocks to drive an agent loop.
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
    try:
        return _get_client().messages.create(**kwargs)
    except anthropic.APIError:
        return None


def chat_json(system: str, user: str) -> dict | None:
    """Ask for a strict JSON object back. Returns the parsed dict or None."""
    system = (
        system
        + "\n\nRéponds UNIQUEMENT avec un objet JSON valide, sans texte autour "
        "ni balises Markdown."
    )
    content = _message(system, user, settings.anthropic_max_tokens)
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


def archetype_research(fmt: str, intent: dict, context: str) -> dict | None:
    """Propose a complete 60-card decklist for a non-singleton format.

    Given the format, the player's wish and recent web-search context, return a
    JSON archetype with a full main deck (multiple copies allowed, 4-of rule)
    and a basic-land manabase. Every card name is validated against Scryfall
    downstream and copy counts are re-clamped, so a few hallucinated names are
    filtered out rather than trusted.
    """
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
    parts = [f"Format : {fmt}"]
    if intent.get("theme"):
        parts.append(f"Envie du joueur : {intent['theme']}")
    if intent.get("keywords"):
        parts.append(f"Mots-clés : {', '.join(intent['keywords'])}")
    if intent.get("colors"):
        parts.append(f"Couleurs souhaitées : {', '.join(intent['colors'])}")
    if context:
        parts.append(f"\nExtraits web récents (métagame) :\n{context}")
    return chat_json(system, "\n".join(parts))


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
        rules.append(f"Reste STRICTEMENT dans les couleurs imposées : {', '.join(intent['colors'])}.")
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
    return chat_json(system, "\n".join(parts))


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
