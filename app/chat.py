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
import logging
import threading

from . import analysis, db, deckgen, formats60, intent, llm, scryfall
from .config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Tu es un assistant de deckbuilding Magic: the Gathering, en français, qui "
    "dialogue de façon itérative avec un joueur pour l'aider à construire un deck "
    "à partir de SA collection.\n\n"
    "Règles :\n"
    "- DIALOGUE D'ABORD : si la demande est vague ou s'il manque une information "
    "importante (format, couleurs, budget, thème), pose UNE ou DEUX questions "
    "courtes pour préciser AVANT de lancer un outil de génération. Ne génère ni "
    "suggestions ni decklist tant que l'essentiel n'est pas clair.\n"
    "- NE RÉGÉNÈRE PAS inutilement : si un deck, des commandants ou un archétype "
    "ont déjà été produits dans cette conversation (voir « CONTEXTE ACTUEL » "
    "ci-dessous quand il est présent), réponds aux questions du joueur (courbe de "
    "mana, rôle d'une carte, prix, remplacements, synergies…) À PARTIR DE CE "
    "CONTEXTE, sans rappeler l'outil de génération. Ne régénère QUE si le joueur "
    "change explicitement sa demande (autre commandant, autre budget, autres "
    "couleurs, autre format).\n"
    "- Utilise les outils pour OBTENIR des cartes ou des decks : ne cite jamais "
    "une carte que tu n'as pas obtenue via un outil ou qui ne figure pas déjà "
    "dans le CONTEXTE ACTUEL. N'invente AUCUN nom de carte. Pour vérifier le prix "
    "ou la légalité d'une carte précise, utilise lookup_card.\n"
    "- Respecte STRICTEMENT le format, les couleurs et le nombre de couleurs "
    "demandés. « monocouleur » => max_colors=1.\n"
    "- Si le joueur veut un commandant qu'il NE possède PAS (« que je n'ai pas », "
    "« à acquérir », « un nouveau commandant »), appelle suggest_commanders avec "
    "unowned_only=true.\n"
    "- Pour le Commander (EDH), appelle suggest_commanders. Pour Standard, "
    "Modern, Pioneer, Pauper, Legacy, Vintage, Premodern (60 cartes), appelle "
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
    "include_low_decks": {
        "type": "boolean",
        "description": (
            "true UNIQUEMENT si le joueur demande explicitement d'inclure les "
            "commandants peu joués / rares / sous le seuil de popularité EDHREC. "
            "Par défaut false (on masque les commandants trop confidentiels)."
        ),
    },
    "unowned_only": {
        "type": "boolean",
        "description": (
            "true si le joueur veut UNIQUEMENT des commandants qu'il ne possède "
            "PAS (« un commandant que je n'ai pas », « à acquérir », « pas dans "
            "ma collection », « propose-moi un nouveau commandant »). On ne "
            "renvoie alors que des commandants à acquérir. Par défaut false."
        ),
    },
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
            "complétude EDHREC et une liste d'achat dans le budget. Inclut aussi "
            "des commandants NON possédés mais liés à tes cartes (champ owned=false, "
            "avec price_eur, link_count et total_cost_eur) qui respectent thème, "
            "couleurs et budget."
        ),
        "input_schema": {"type": "object", "properties": dict(_INTENT_PROPS)},
    },
    {
        "name": "research_archetype",
        "description": (
            "Pour un format 60 cartes (standard, modern, pioneer, pauper, legacy, "
            "vintage, premodern) : recherche un archétype compétitif, valide chaque "
            "carte via Scryfall, analyse l'écart avec la collection et chiffre l'achat."
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

def _is_owned(r: dict) -> bool:
    """A suggested commander is owned unless explicitly flagged ``owned=False``.

    Mirrors the template (``r.owned is defined and not r.owned``) so the text we
    feed the model can never contradict what the UI shows the player.
    """
    return r.get("owned", True)


def _intent_from(args: dict, fmt: str | None) -> dict:
    return intent._coerce(
        {
            "format": fmt,
            "colors": args.get("colors") or [],
            "max_colors": args.get("max_colors"),
            "theme": args.get("theme") or "",
            "keywords": args.get("keywords") or [],
            "budget_eur": args.get("budget_eur"),
            "include_low_decks": args.get("include_low_decks"),
            "unowned_only": args.get("unowned_only"),
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

    # Keep both owned suggestions and proposed (unowned) commanders in the chat.
    owned_res = [r for r in results if _is_owned(r)]
    proposed_res = [r for r in results if not _is_owned(r)]
    if parsed.get("unowned_only"):
        # The player explicitly wants commanders they don't own yet.
        if not proposed_res:
            return (
                "Aucun commandant à acquérir trouvé pour ces critères : tes cartes "
                "ne pointent vers aucun nouveau commandant correspondant. Suggère "
                "d'élargir les couleurs/le thème ou d'augmenter le budget.",
                None,
            )
        top = proposed_res[:6]
    else:
        top = owned_res[:5] + proposed_res[:3]
    # Trim the heavy fields we don't render in the chat artifact.
    for r in top:
        r.pop("missing_cards", None)
    artifact = {"type": "commanders", "intent": parsed, "results": top,
                "notices": data.get("notices") or []}

    lines = []
    for r in top:
        buy = r.get("buylist") or {}
        if _is_owned(r):
            lines.append(
                f"- {r['name']} (possédé) : {r['pct']}% complété ({r['owned_count']}/"
                f"{r['total_recommended']}), {r['num_decks']} decks EDHREC, "
                f"achat {buy.get('total_eur', 0)} € ({buy.get('bought_count', 0)} cartes)"
            )
        else:
            price = r.get("price_eur")
            price_txt = f"{price} €" if price is not None else "prix indisponible"
            lines.append(
                f"- {r['name']} (à acquérir, {price_txt} ; {r.get('link_count', 0)} "
                f"de tes cartes y mènent) : {r['pct']}% complété ({r['owned_count']}/"
                f"{r['total_recommended']}), coût total ~{r.get('total_cost_eur', 0)} €"
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
    legal = [f for f in ("commander", "standard", "pioneer", "modern", "legacy",
                         "vintage", "pauper", "premodern")
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


# --- Conversation context snapshot --------------------------------------
# To answer follow-up questions ("pourquoi cette carte ?", "quelle est la
# courbe ?", "remplace X") WITHOUT re-running the expensive generation tools,
# we replay the latest generated artifacts as a compact text block in the
# system prompt. The model reuses this state instead of regenerating — and the
# cards stay grounded because they originally came from a tool result.

def _latest_artifacts(conversation_id: int) -> dict:
    """Most recent artifact of each type across the stored conversation."""
    latest: dict[str, dict] = {}
    for m in db.get_messages(conversation_id):
        for art in m.get("artifacts") or []:
            t = art.get("type")
            if t:
                latest[t] = art
    return latest


def _fmt_colors(colors) -> str:
    return "/".join(colors) if colors else "incolore"


def _snapshot_decklist(art: dict) -> str:
    deck = art.get("deck") or {}
    counts = deck.get("counts") or {}
    lines = [
        f"DECK GÉNÉRÉ — commandant {art.get('commander')} "
        f"({counts.get('total', 0)} cartes : {counts.get('owned', 0)} possédées, "
        f"{counts.get('to_buy', 0)} à acheter pour {deck.get('buy_total_eur', 0)} €)."
    ]
    if deck.get("gameplan"):
        lines.append(f"Plan de jeu : {deck['gameplan']}")
    lines.append("Cartes du deck :")
    for group in deck.get("groups") or []:
        names = []
        for c in group.get("cards") or []:
            qty = c.get("qty") or 1
            prefix = f"{qty}x " if qty > 1 else ""
            if c.get("owned"):
                names.append(f"{prefix}{c['name']} (possédée)")
            else:
                price = c.get("price_eur")
                tag = f"à acheter, {price} €" if price is not None else "à acheter"
                names.append(f"{prefix}{c['name']} ({tag})")
        lines.append(f"  {group.get('label')} : " + ", ".join(names))
    sideboard = deck.get("sideboard") or []
    if sideboard:
        sb = []
        for c in sideboard:
            if c.get("owned"):
                sb.append(f"{c['name']} (possédée)")
            else:
                price = c.get("price_eur")
                sb.append(f"{c['name']} (à acheter{f', {price} €' if price is not None else ''})")
        lines.append(
            f"Sideboard ({len(sideboard)} cartes pertinentes, "
            f"{counts.get('sideboard_to_buy', 0)} à acheter pour "
            f"{deck.get('sideboard_buy_total_eur', 0)} €) : " + ", ".join(sb)
        )
    return "\n".join(lines)


def _snapshot_commanders(art: dict) -> str:
    lines = ["COMMANDANTS SUGGÉRÉS :"]
    for r in art.get("results") or []:
        buy = r.get("buylist") or {}
        if _is_owned(r):
            tag = "possédé"
        else:
            price = r.get("price_eur")
            price_txt = f"{price} €" if price is not None else "prix indisponible"
            tag = (f"à acquérir, {price_txt}, {r.get('link_count', 0)} cartes liées, "
                   f"coût total ~{r.get('total_cost_eur', 0)} €")
        niche = " ; peu joué, sous le seuil EDHREC" if r.get("below_threshold") else ""
        lines.append(
            f"- {r['name']} ({_fmt_colors(r.get('color_identity'))}, {tag}{niche}) : "
            f"{r.get('pct')}% complété ({r.get('owned_count')}/{r.get('total_recommended')}), "
            f"{r.get('num_decks')} decks EDHREC, achat {buy.get('total_eur', 0)} €."
        )
    return "\n".join(lines)


def _snapshot_archetype(art: dict) -> str:
    data = art.get("data") or {}
    arch = data.get("archetype") or {}
    buy = data.get("buylist") or {}
    lines = [
        f"ARCHÉTYPE {(data.get('format') or '').upper()} — {arch.get('name')} "
        f"({_fmt_colors(arch.get('colors'))})."
    ]
    if arch.get("strategy"):
        lines.append(f"Stratégie : {arch['strategy']}")
    owned = [c["name"] for c in data.get("owned_cards") or []]
    if owned:
        lines.append("Cartes maîtresses possédées : " + ", ".join(owned))
    to_buy = [f"{c['name']} ({c.get('price_eur')} €)" for c in buy.get("to_buy") or []]
    if to_buy:
        lines.append("À acheter : " + ", ".join(to_buy))
    lines.append(
        f"{data.get('owned_count', 0)}/{data.get('valid_count', 0)} cartes maîtresses "
        f"possédées ; achat total {buy.get('total_eur', 0)} €."
    )
    return "\n".join(lines)


_SNAPSHOT_BUILDERS = {
    "decklist": _snapshot_decklist,
    "commanders": _snapshot_commanders,
    "archetype": _snapshot_archetype,
}


def _context_snapshot(conversation_id: int) -> str:
    """Compact text of the latest generated artifacts, for the system prompt."""
    latest = _latest_artifacts(conversation_id)
    # Most actionable first: a generated deck, then commander/archetype research.
    blocks = [
        _SNAPSHOT_BUILDERS[t](latest[t])
        for t in ("decklist", "commanders", "archetype")
        if t in latest
    ]
    if not blocks:
        return ""
    return (
        "CONTEXTE ACTUEL — éléments DÉJÀ générés dans cette conversation. "
        "Réutilise-les pour répondre aux questions du joueur SANS relancer les "
        "outils de génération ; ne régénère que si le joueur change explicitement "
        "sa demande.\n\n" + "\n\n".join(blocks)
    )


def _agent_loop(api_messages: list[dict], profile_id: int, system: str):
    """Run the tool-use loop. Returns (final_text, artifacts)."""
    texts: list[str] = []
    artifacts: list[dict] = []

    for _ in range(settings.chat_max_tool_iterations):
        resp = llm.create_message(system, api_messages, tools=TOOLS)
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


def _generate_reply(conversation_id: int, profile_id: int, user_text: str):
    """Run the agent (or key-free fallback) for an already-stored user message."""
    if not llm.is_available():
        return _fallback_turn(profile_id, user_text)
    api_messages = _history_messages(conversation_id)
    snapshot = _context_snapshot(conversation_id)
    system = f"{SYSTEM_PROMPT}\n\n{snapshot}" if snapshot else SYSTEM_PROMPT
    return _agent_loop(api_messages, profile_id, system)


def run_turn(conversation_id: int, profile_id: int, user_text: str) -> None:
    """Append the user message, run the agent, and store the assistant reply.

    Blocking (network I/O in the tools). Kept synchronous for the key-free path
    and the tests; the web app uses ``start_turn`` to run this off-request.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return
    db.add_message(conversation_id, "user", user_text)
    text, artifacts = _generate_reply(conversation_id, profile_id, user_text)
    db.add_message(conversation_id, "assistant", text, artifacts=artifacts or None)
    db.touch_conversation(conversation_id, title=user_text)


# --- Asynchronous turns --------------------------------------------------
# A chat turn can take far longer than Cloudflare's ~100s edge timeout (cold
# EDHREC/Scryfall lookups, several LLM rounds). We therefore answer the POST
# immediately and run the turn in a background thread; the page polls
# ``is_pending`` and shows a spinner until the reply lands. The registry is
# in-memory — fine for the single-worker personal deployment.
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def is_pending(conversation_id) -> bool:
    """True while a background turn for this conversation is still running."""
    try:
        cid = int(conversation_id)
    except (TypeError, ValueError):
        return False
    with _inflight_lock:
        return cid in _inflight


def _worker(conversation_id: int, profile_id: int, user_text: str) -> None:
    try:
        text, artifacts = _generate_reply(conversation_id, profile_id, user_text)
    except Exception:  # never leave the conversation hanging on a spinner
        logger.exception("chat turn failed (conversation %s)", conversation_id)
        text, artifacts = (
            "Désolé, une erreur est survenue pendant le traitement. Réessaie.",
            [],
        )
    try:
        db.add_message(conversation_id, "assistant", text, artifacts=artifacts or None)
    finally:
        # Clear the flag only after the reply is persisted, so a non-pending
        # poll always finds the new message.
        with _inflight_lock:
            _inflight.discard(conversation_id)


def start_turn(conversation_id: int, profile_id: int, user_text: str) -> None:
    """Store the user message and process the reply in a background thread.

    Returns immediately so the HTTP request stays well under the proxy timeout.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return
    cid = int(conversation_id)
    with _inflight_lock:
        if cid in _inflight:
            return  # a turn is already running for this conversation
        _inflight.add(cid)
    # Persist the user message + title up front so the redirect shows it at once.
    db.add_message(cid, "user", user_text)
    db.touch_conversation(cid, title=user_text)
    threading.Thread(
        target=_worker, args=(cid, profile_id, user_text), daemon=True
    ).start()
