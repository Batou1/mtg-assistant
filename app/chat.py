"""Iterative deckbuilding chat — an agent loop over the existing pipeline.

Claude converses in French and grounds every proposal by calling the app's own
modules as **tools** (collection summary, Commander suggestions, 60-card
archetype research, full decklist generation, card lookup). The model never
invents cards: card data always comes from a tool, which itself goes through
Scryfall/EDHREC.

Persistence is intentionally text-only: we store each turn's display text and
its rich *artifacts* (for rendering), but we do NOT persist the raw
``tool_use``/``tool_result`` transcript. Each turn we rebuild the API history as
a plain user/assistant text exchange — the intra-turn tool loop is ephemeral.
This keeps stored payloads (and resent tokens) small while preserving context.

Without an API key the chat still answers: ``run_turn`` falls back to the
one-shot intent → analyse pipeline and flags that the full chat needs a key.
"""
from . import analysis, db, deckgen, formats60, intent, llm, scryfall
from .config import settings

SYSTEM_PROMPT = (
    "Tu es un assistant de deckbuilding Magic: the Gathering, en français, qui "
    "dialogue de façon itérative avec un joueur pour l'aider à construire un deck "
    "à partir de SA collection.\n\n"
    "Règles :\n"
    "- Utilise TOUJOURS les outils pour proposer des cartes ou des decks : ne "
    "cite jamais une carte que tu n'as pas obtenue via un outil. N'invente "
    "AUCUN nom de carte.\n"
    "- Respecte STRICTEMENT le format, les couleurs et le nombre de couleurs "
    "demandés. « monocouleur » => max_colors=1.\n"
    "- Pour le Commander (EDH), appelle suggest_commanders. Pour Standard, "
    "Modern, Pioneer, Pauper, Legacy, Vintage (60 cartes), appelle "
    "research_archetype.\n"
    "- Quand le joueur choisit un commandant, propose-lui de générer la decklist "
    "complète (generate_decklist).\n"
    "- Garde le contexte de la conversation : si le joueur affine une demande "
    "(« plutôt en bleu », « monte le budget à 80 € »), réutilise ce qui a déjà "
    "été dit.\n"
    "- Réponds de façon concise et naturelle. Les cartes et listes sont affichées "
    "séparément par l'interface : résume, ne récite pas la liste entière."
)


# --- Tool schemas (Anthropic) -------------------------------------------

_INTENT_PROPS = {
    "colors": {
        "type": "array",
        "items": {"type": "string", "enum": ["W", "U", "B", "R", "G"]},
        "description": "Couleurs souhaitées (WUBRG). Vide si non précisé.",
    },
    "max_colors": {
        "type": "integer",
        "description": "Nombre maximum de couleurs (1 pour monocouleur). Omettre si non précisé.",
    },
    "theme": {"type": "string", "description": "Thème/archétype en quelques mots."},
    "keywords": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Mots-clés de stratégie en anglais (aristocrats, tokens, reanimator…).",
    },
    "budget_eur": {"type": "number", "description": "Budget en euros, si mentionné."},
}

TOOLS = [
    {
        "name": "get_collection_summary",
        "description": "Résume la collection du joueur (nombre de cartes possédées).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suggest_commanders",
        "description": (
            "Pour le format COMMANDER : à partir des cartes possédées, propose des "
            "commandants jouables qui collent aux couleurs/thème, avec le taux de "
            "complétude EDHREC et une liste d'achat dans le budget."
        ),
        "input_schema": {"type": "object", "properties": dict(_INTENT_PROPS)},
    },
    {
        "name": "research_archetype",
        "description": (
            "Pour un format 60 cartes (standard, modern, pioneer, pauper, legacy, "
            "vintage) : recherche un archétype compétitif, valide chaque carte via "
            "Scryfall, analyse l'écart avec la collection et chiffre l'achat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": sorted(formats60.FORMATS),
                    "description": "Format 60 cartes visé.",
                },
                **_INTENT_PROPS,
            },
            "required": ["format"],
        },
    },
    {
        "name": "generate_decklist",
        "description": (
            "Génère une decklist Commander complète (100 cartes) pour un commandant "
            "choisi : cartes possédées réutilisées, manquantes achetées dans le "
            "budget, terrains complétés."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commander": {"type": "string", "description": "Nom exact du commandant."},
                "budget_eur": {"type": "number", "description": "Budget d'achat en euros."},
                "theme": {"type": "string", "description": "Thème pour le plan de jeu."},
            },
            "required": ["commander"],
        },
    },
    {
        "name": "lookup_card",
        "description": "Donne le prix EUR et la légalité d'une carte précise via Scryfall.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Nom de la carte."}},
            "required": ["name"],
        },
    },
]


# --- Tool executors -----------------------------------------------------
# Each returns (text_for_llm, artifact|None). The text is compact (it goes back
# to the model); the artifact carries the rich data the template renders.

def _intent_from(args: dict, fmt: str | None) -> dict:
    return intent._coerce(
        {
            "format": fmt,
            "colors": args.get("colors") or [],
            "max_colors": args.get("max_colors"),
            "theme": args.get("theme") or "",
            "keywords": args.get("keywords") or [],
            "budget_eur": args.get("budget_eur"),
            "source": "llm",
        }
    )


def _exec_collection_summary(args: dict, profile_id: int):
    distinct, total = db.collection_count(profile_id)
    if not total:
        return "La collection est vide : aucune carte importée.", None
    return f"Collection : {total} cartes ({distinct} cartes uniques).", None


def _exec_suggest_commanders(args: dict, profile_id: int):
    distinct, _ = db.collection_count(profile_id)
    if not distinct:
        return "La collection est vide ; impossible de proposer des commandants.", None

    parsed = _intent_from(args, "commander")
    data = analysis.analyze(parsed, profile_id)
    results = data.get("results") or []
    if not results:
        return (
            f"Aucun commandant correspondant trouvé ({data.get('candidate_count', 0)} "
            "candidat(s) examiné(s)). Suggère d'élargir les couleurs.",
            None,
        )

    top = results[:6]
    # Trim the heavy fields we don't render in the chat artifact.
    for r in top:
        r.pop("missing_cards", None)
    artifact = {"type": "commanders", "intent": parsed, "results": top,
                "notices": data.get("notices") or []}

    lines = []
    for r in top:
        buy = r.get("buylist") or {}
        lines.append(
            f"- {r['name']} : {r['pct']}% complété ({r['owned_count']}/"
            f"{r['total_recommended']}), {r['num_decks']} decks EDHREC, "
            f"achat {buy.get('total_eur', 0)} € ({buy.get('bought_count', 0)} cartes)"
        )
    return "Commandants proposés :\n" + "\n".join(lines), artifact


def _exec_research_archetype(args: dict, profile_id: int):
    fmt = (args.get("format") or "").lower()
    if fmt not in formats60.FORMATS:
        return f"Format « {fmt} » non géré par la recherche 60 cartes.", None

    parsed = _intent_from(args, fmt)
    data = formats60.analyze(parsed, profile_id)
    if data.get("llm_unavailable"):
        return "Le LLM est requis pour la recherche d'archétype et n'est pas disponible.", None

    arch = data.get("archetype") or {}
    buy = data.get("buylist") or {}
    artifact = {"type": "archetype", "intent": parsed, "data": data}
    text = (
        f"Archétype {fmt} : {arch.get('name')} "
        f"({'/'.join(arch.get('colors') or []) or 'incolore'}). "
        f"{data.get('owned_count', 0)}/{data.get('valid_count', 0)} cartes maîtresses "
        f"possédées ; achat {buy.get('total_eur', 0)} € "
        f"({buy.get('bought_count', 0)} cartes)."
    )
    return text, artifact


def _exec_generate_decklist(args: dict, profile_id: int):
    commander = (args.get("commander") or "").strip()
    if not commander:
        return "Aucun commandant fourni.", None

    deck, _data = deckgen.generate_full_deck(
        commander, args.get("budget_eur"), args.get("theme") or "", profile_id
    )
    if not deck:
        return (
            f"Impossible de générer le deck pour « {commander} » "
            "(page EDHREC introuvable ou commandant non reconnu).",
            None,
        )

    c = deck["counts"]
    artifact = {"type": "decklist", "commander": commander, "deck": deck}
    text = (
        f"Decklist {commander} générée : {c['total']} cartes, {c['owned']} possédées, "
        f"{c['to_buy']} à acheter pour {deck['buy_total_eur']} €."
    )
    return text, artifact


def _exec_lookup_card(args: dict, profile_id: int):
    name = (args.get("name") or "").strip()
    if not name:
        return "Aucun nom de carte fourni.", None
    resolved, _nf = scryfall.resolve_cards([name])
    card = resolved.get(name.lower())
    if not card:
        return f"Carte « {name} » introuvable sur Scryfall.", None
    price = scryfall.price_eur(card)
    legal = [f for f in ("commander", "standard", "modern", "pioneer", "pauper")
             if scryfall.legal_in(card, f)]
    return (
        f"{card['name']} — prix {price if price is not None else 'indisponible'} € ; "
        f"légale en : {', '.join(legal) or 'aucun format listé'}.",
        None,
    )


_EXECUTORS = {
    "get_collection_summary": _exec_collection_summary,
    "suggest_commanders": _exec_suggest_commanders,
    "research_archetype": _exec_research_archetype,
    "generate_decklist": _exec_generate_decklist,
    "lookup_card": _exec_lookup_card,
}


# --- Agent loop ---------------------------------------------------------

def _serialize_content(blocks) -> list[dict]:
    """Turn an Anthropic response's content blocks into resendable dicts."""
    out = []
    for b in blocks:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def _history_messages(conversation_id: int) -> list[dict]:
    """Rebuild the API message list from stored text turns (bounded)."""
    msgs = db.get_messages(conversation_id)
    msgs = msgs[-settings.chat_history_limit:]
    api = []
    for m in msgs:
        if m["role"] in ("user", "assistant") and m["content"].strip():
            api.append({"role": m["role"], "content": m["content"]})
    return api


def _agent_loop(api_messages: list[dict], profile_id: int):
    """Run the tool-use loop. Returns (final_text, artifacts)."""
    texts: list[str] = []
    artifacts: list[dict] = []

    for _ in range(settings.chat_max_tool_iterations):
        resp = llm.create_message(SYSTEM_PROMPT, api_messages, tools=TOOLS)
        if resp is None:
            texts.append(
                "Désolé, le service Claude est momentanément indisponible. Réessaie."
            )
            break

        for b in resp.content:
            if b.type == "text" and b.text.strip():
                texts.append(b.text.strip())

        if resp.stop_reason != "tool_use":
            break

        # Execute every requested tool and feed the results back.
        api_messages.append({"role": "assistant", "content": _serialize_content(resp.content)})
        tool_results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            executor = _EXECUTORS.get(b.name)
            if executor is None:
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": b.id,
                     "content": f"Outil inconnu : {b.name}"}
                )
                continue
            text, artifact = executor(b.input or {}, profile_id)
            if artifact:
                artifacts.append(artifact)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": b.id, "content": text}
            )
        api_messages.append({"role": "user", "content": tool_results})

    final = "\n\n".join(texts).strip() or "D'accord."
    return final, artifacts


def _fallback_turn(profile_id: int, user_text: str):
    """Key-free path: one-shot intent → analyse, with a heads-up note."""
    parsed = intent.parse_intent(user_text)
    note = (
        "ℹ️ Le chat complet nécessite une clé ANTHROPIC_API_KEY. En attendant, "
        "voici une analyse ponctuelle de ta demande."
    )
    distinct, _ = db.collection_count(profile_id)
    if not distinct:
        return note + "\n\nTa collection est vide : importe d'abord un export ManaBox.", []

    if parsed.get("format") in formats60.FORMATS:
        text, artifact = _exec_research_archetype(
            {"format": parsed["format"], **parsed}, profile_id
        )
    else:
        text, artifact = _exec_suggest_commanders(parsed, profile_id)
    artifacts = [artifact] if artifact else []
    return f"{note}\n\n{text}", artifacts


def run_turn(conversation_id: int, profile_id: int, user_text: str) -> None:
    """Append the user message, run the agent, and store the assistant reply.

    Blocking (network I/O in the tools); call it from a threadpool.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return
    db.add_message(conversation_id, "user", user_text)

    if not llm.is_available():
        text, artifacts = _fallback_turn(profile_id, user_text)
    else:
        api_messages = _history_messages(conversation_id)
        text, artifacts = _agent_loop(api_messages, profile_id)

    db.add_message(conversation_id, "assistant", text, artifacts=artifacts or None)
    db.touch_conversation(conversation_id, title=user_text)
