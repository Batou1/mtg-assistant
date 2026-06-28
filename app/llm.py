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
    return _message(system, user, 600) or None


def archetype_research(fmt: str, intent: dict, context: str) -> dict | None:
    """Propose a structured archetype for a 60-card format.

    Given the format, the player's wish and recent web-search context, return a
    JSON archetype. Every card name is validated against Scryfall downstream, so
    a few hallucinated names are filtered out rather than trusted.
    """
    system = (
        f"Tu es un expert Magic: the Gathering, format {fmt}. À partir de l'envie "
        "du joueur et d'extraits web récents sur le métagame, propose UN archétype "
        "compétitif et réaliste, FIDÈLE aux couleurs et à la stratégie demandées. "
        "Réponds UNIQUEMENT en JSON avec ces clés :\n"
        '- "archetype": nom court de l\'archétype.\n'
        '- "colors": liste de symboles parmi "W","U","B","R","G".\n'
        '- "strategy": 2-3 phrases en français décrivant le plan de jeu.\n'
        '- "key_cards": liste de 30 à 40 noms de cartes RÉELLES, en anglais, avec '
        f"l'orthographe EXACTE (telle que sur la carte), toutes légales en {fmt} : "
        "les cartes les plus jouées de cet archétype (sorts ET terrains non-basiques). "
        "N'invente AUCUNE carte ; si tu n'es pas certain qu'une carte existe et est "
        f"légale en {fmt}, ne la mets pas. Privilégie les staples reconnus."
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


def deck_gameplan(commander_name: str, card_names: list[str], theme: str = "") -> str | None:
    """Write a short French game-plan summary from the chosen cards.

    The model is given the actual card names, so it describes the deck rather
    than inventing cards. Returns None when the LLM is unavailable.
    """
    system = (
        "Tu es un expert Magic: the Gathering (format Commander). On te donne un "
        "commandant et une sélection de cartes RÉELLES du deck. Rédige en français "
        "un résumé concis (3 à 5 phrases) du plan de jeu : stratégie générale, "
        "comment le deck gagne, et 1-2 conseils de pilotage. Ne mentionne que des "
        "cartes de la liste fournie. N'invente aucune carte."
    )
    sample = ", ".join(card_names[:18])
    user = f"Commandant : {commander_name}\n"
    if theme:
        user += f"Thème souhaité : {theme}\n"
    user += f"Cartes clés du deck : {sample}"
    return chat_text(system, user)
