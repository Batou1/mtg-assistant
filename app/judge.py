"""The rules judge — a chat that answers Magic rules questions by citing the
Comprehensive Rules (app/rules.py) and, for card-specific situations, the
cards' Oracle text and official rulings (Scryfall).

Same agent loop as the deckbuilding chat (``chat._agent_loop``), different
tools and system prompt. What makes the answer trustworthy is not the model's
memory but the anchoring (invariant 1 applied to rules): the model must fetch
the rules it cites through the tools, and every rule number in the final text
is re-checked against the stored corpus — a number that does not exist there is
rendered as unverified, never as a clickable citation.

Answer format: a short direct answer first, then the detailed explanation,
which the template renders as two visually distinct blocks. The split is done
on the ``Réponse :`` / ``Explication :`` markers the prompt requires, with a
first-paragraph fallback when the model drops them.
"""
import logging
import re

from markupsafe import Markup, escape

from . import chat, db, llm, rules, scryfall

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Tu es un juge Magic: the Gathering expérimenté. Tu réponds en français aux "
    "questions de règles d'un joueur : règles générales du jeu ou situations de "
    "partie précises impliquant des cartes nommées.\n\n"
    "SOURCES — c'est la partie la plus importante :\n"
    "- Ta seule autorité est le texte des Comprehensive Rules (CR) officielles, "
    "disponible via les outils search_rules, get_rule et lookup_glossary. Le "
    "corpus est en anglais : formule tes recherches avec les termes anglais "
    "officiels (trample, deathtouch, state-based actions, the stack, "
    "copy…) ; l'outil lookup_glossary traduit un terme en numéro de règle.\n"
    "- Consulte TOUJOURS les règles avant de répondre, même si tu crois "
    "connaître la réponse : une règle a pu changer. Fais plusieurs recherches "
    "si la question mêle plusieurs mécaniques, puis lis la règle complète avec "
    "get_rule quand un résultat est tronqué ou qu'il te faut ses sous-règles.\n"
    "- Quand une carte est citée, appelle lookup_card pour obtenir son texte "
    "Oracle exact et ses rulings officiels AVANT de raisonner. Ne suppose "
    "jamais le texte d'une carte de mémoire.\n"
    "- Ne cite QUE des numéros de règles que les outils t'ont effectivement "
    "renvoyés, sous la forme [702.19b] (crochets, numéro exact). Chaque "
    "affirmation de règle doit être appuyée par au moins une citation. "
    "N'invente aucun numéro : les citations sont vérifiées et une référence "
    "inexistante sera signalée au joueur.\n"
    "- Si les règles ne permettent pas de trancher (situation ambiguë, "
    "politique de tournoi hors CR, ruling d'arbitre), dis-le clairement.\n\n"
    "FORMAT DE RÉPONSE (obligatoire, texte brut sans titres Markdown) :\n"
    "Réponse : <la réponse directe en une à trois phrases, avec la ou les "
    "citations principales>\n\n"
    "Explication : <l'explication détaillée, étape par étape, qui reprend le "
    "raisonnement (quelles règles s'appliquent, dans quel ordre, ce qui se passe "
    "concrètement), chaque étape citant sa règle entre crochets. Tu peux "
    "utiliser des tirets pour lister des étapes.>\n\n"
    "Style : précis, pédagogique, sans jargon inutile ; les termes techniques "
    "sont donnés en français avec le terme anglais officiel entre parenthèses "
    "la première fois (ex. « les actions basées sur l'état (state-based "
    "actions) »). Si le joueur pose une question de suivi, réponds dans le même "
    "format en t'appuyant sur les règles déjà citées et de nouvelles recherches "
    "si nécessaire."
)

TOOLS = [
    {
        "name": "search_rules",
        "description": (
            "Recherche par mots-clés (en ANGLAIS) dans les Comprehensive Rules. "
            "Renvoie les règles les plus pertinentes avec leur numéro et leur texte. "
            "Accepte aussi un numéro de règle ou de chapitre (ex. « 702.19 ») pour "
            "lister une règle et ses sous-règles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Mots-clés anglais ou numéro de règle."},
                "limit": {"type": "integer", "description": "Nombre max de résultats (défaut 12)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_rule",
        "description": (
            "Texte intégral d'une règle par son numéro (ex. « 702.19b », « 903.8 », "
            "« 704 » pour un chapitre), avec son contexte : règle parente, chapitre, "
            "sous-règles et exemples officiels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "string"}},
            "required": ["number"],
        },
    },
    {
        "name": "lookup_glossary",
        "description": (
            "Définition officielle d'un terme du glossaire des CR (en anglais), avec "
            "le renvoi vers la règle qui le définit. Utile pour trouver le bon numéro "
            "de règle à partir d'un mot-clé."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
    {
        "name": "lookup_card",
        "description": (
            "Texte Oracle exact d'une carte (coût, type, texte de règles, F/E) et ses "
            "rulings officiels, depuis Scryfall. À appeler pour CHAQUE carte "
            "mentionnée dans une question de situation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Nom anglais de la carte."}},
            "required": ["name"],
        },
    },
]

_RULE_TEXT_MAX = 700
_RULINGS_MAX = 12


def _rule_line(r: dict, full: bool = False) -> str:
    text = r["text"] if full or len(r["text"]) <= _RULE_TEXT_MAX else r["text"][:_RULE_TEXT_MAX] + "…"
    return f"[{r['number']}] {text}"


def _not_loaded() -> str:
    return (
        "Les Comprehensive Rules ne sont pas encore chargées dans l'application "
        "(import en attente ou impossible). Dis au joueur que tu ne peux pas "
        "citer les règles officielles pour l'instant et qu'il peut relancer "
        "l'import depuis l'onglet Règles."
    )


def _exec_search_rules(args: dict, profile_id: int, ctx: dict | None = None):
    if not rules.is_loaded():
        return _not_loaded(), None
    query = (args.get("query") or "").strip()
    limit = args.get("limit")
    try:
        limit = max(1, min(int(limit), 30)) if limit else None
    except (TypeError, ValueError):
        limit = None
    found = rules.search(query, limit)
    if not found:
        return (f"Aucune règle ne correspond à « {query} ». Essaie d'autres mots-clés "
                "anglais, ou lookup_glossary pour trouver le numéro de règle d'un terme."), None
    lines = [f"{len(found)} règle(s) pour « {query} » :"]
    for r in found:
        lines.append(_rule_line(r))
    return "\n".join(lines), None


def _exec_get_rule(args: dict, profile_id: int, ctx: dict | None = None):
    if not rules.is_loaded():
        return _not_loaded(), None
    number = (args.get("number") or "").strip()
    found = rules.get_rule(number)
    if found is None:
        return f"Aucune règle numérotée « {number} » dans les Comprehensive Rules.", None
    r = found["rule"]
    lines = []
    if found["chapter"]:
        lines.append(f"Chapitre {found['chapter']['number']} — {found['chapter']['text']}")
    if found["parent"] and found["parent"]["kind"] == "rule":
        lines.append(f"Règle parente : {_rule_line(found['parent'])}")
    lines.append(_rule_line(r, full=True))
    if r.get("examples"):
        for ex in r["examples"].split("\n"):
            lines.append(f"  Exemple officiel : {ex}")
    if found["children"]:
        lines.append("Sous-règles :")
        for c in found["children"][:40]:
            lines.append("  " + _rule_line(c))
            if c.get("examples"):
                for ex in c["examples"].split("\n"):
                    lines.append(f"    Exemple officiel : {ex}")
    return "\n".join(lines), None


def _exec_lookup_glossary(args: dict, profile_id: int, ctx: dict | None = None):
    if not rules.is_loaded():
        return _not_loaded(), None
    term = (args.get("term") or "").strip()
    found = rules.lookup_glossary(term)
    if not found:
        return f"Aucune entrée de glossaire pour « {term} ».", None
    return "\n\n".join(f"{g['term']} : {g['definition']}" for g in found), None


def _exec_lookup_card(args: dict, profile_id: int, ctx: dict | None = None):
    name = (args.get("name") or "").strip()
    if not name:
        return "Aucun nom de carte fourni.", None
    resolved, _nf = scryfall.resolve_cards([name])
    card = resolved.get(name.lower())
    if not card:
        return f"Carte « {name} » introuvable sur Scryfall (vérifie l'orthographe anglaise).", None
    lines = [chat._card_facts(card)]
    keywords = card.get("keywords") or []
    if keywords:
        lines.append("Mots-clés : " + ", ".join(keywords))
    found = scryfall.rulings(card)
    if found is None:
        lines.append("Rulings officiels : indisponibles pour l'instant (Scryfall injoignable).")
    elif not found:
        lines.append("Rulings officiels : aucun.")
    else:
        lines.append("Rulings officiels (Gatherer) :")
        for r in found[-_RULINGS_MAX:]:
            lines.append(f"- ({r['published_at']}) {r['comment']}")
    return "\n".join(lines), None


_EXECUTORS = {
    "search_rules": _exec_search_rules,
    "get_rule": _exec_get_rule,
    "lookup_glossary": _exec_lookup_glossary,
    "lookup_card": _exec_lookup_card,
}


# --- Answer post-processing ---------------------------------------------

_ANSWER_RE = re.compile(
    r"^\s*(?:\*\*)?\s*R[ée]ponse(?:\s+courte|\s+directe)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*",
    re.IGNORECASE,
)
_EXPLANATION_RE = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?\s*Explications?(?:\s+d[ée]taill[ée]e)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*",
    re.IGNORECASE,
)


def split_answer(text: str) -> tuple[str, str]:
    """(direct answer, explanation) from the model's text.

    Prefers the ``Réponse :`` / ``Explication :`` markers; otherwise the
    first paragraph is the answer and the rest the explanation.
    """
    text = (text or "").strip()
    m = _EXPLANATION_RE.search(text)
    if m:
        head, tail = text[: m.start()], text[m.end():]
    else:
        parts = re.split(r"\n\s*\n", text, maxsplit=1)
        head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    head = _ANSWER_RE.sub("", head.strip(), count=1).strip()
    return head, tail.strip()


def citations(text: str) -> list[dict]:
    """Every rule number mentioned in ``text``, checked against the corpus.

    ``[{"number", "found", "title"}]`` in order of first appearance; ``title``
    is the enclosing chapter (shown as the link's tooltip).
    """
    seen: dict[str, dict] = {}
    numbers = list(rules.NUMBER_RE.findall(text or ""))
    numbers += list(rules.CHAPTER_REF_RE.findall(text or ""))
    for num in numbers:
        if num in seen:
            continue
        found = rules.get_rule(num) if rules.is_loaded() else None
        seen[num] = {
            "number": num,
            "found": found is not None,
            "title": (found["rule"]["chapter"] if found else ""),
        }
    return list(seen.values())


def build_artifact(text: str) -> dict:
    answer, explanation = split_answer(text)
    cites = citations(text)
    return {
        "type": "ruling",
        "answer": answer,
        "explanation": explanation,
        "citations": cites,
        "unverified": [c["number"] for c in cites if not c["found"]],
        "rules_effective": db.get_meta("rules_effective_date") or "",
    }


# --- Turn generation -----------------------------------------------------

def _fallback_reply(user_text: str):
    """Key-free path: no reasoning, but the rules are still searchable — show
    the closest rules to the question's words (English keywords work best)."""
    note = (
        "ℹ️ Le juge complet nécessite une clé ANTHROPIC_API_KEY. En attendant, "
        "voici les règles officielles qui correspondent le mieux aux mots de ta "
        "question (les mots-clés anglais donnent de meilleurs résultats)."
    )
    if not rules.is_loaded():
        text = note + "\n\nLes Comprehensive Rules ne sont pas encore chargées : lance l'import depuis le bouton « Mettre à jour » de cet onglet."
        return text, [build_artifact(text)]
    found = rules.search(user_text, 6)
    if not found:
        text = note + "\n\nAucune règle ne correspond à ces mots. Essaie avec les termes anglais officiels (ex. « trample », « state-based actions »)."
        return text, [build_artifact(text)]
    lines = [note, "", "Explication :"]
    for r in found:
        lines.append(f"- [{r['number']}] {r['text'][:300]}{'…' if len(r['text']) > 300 else ''}")
    text = "\n".join(lines)
    return text, [build_artifact(text)]


def generate_reply(conversation_id: int, profile_id: int, user_text: str):
    """(text, artifacts) for an already-stored user message."""
    if not llm.is_available():
        return _fallback_reply(user_text)
    system = SYSTEM_PROMPT
    status = rules.status()
    if status["loaded"]:
        system += (
            f"\n\nVersion des Comprehensive Rules chargée : en vigueur au "
            f"{status['effective_fr'] or status['effective'] or 'date inconnue'}."
        )
    else:
        system += "\n\nATTENTION : " + _not_loaded()
    api_messages = chat._history_messages(conversation_id)
    text, _arts = chat._agent_loop(api_messages, profile_id, system, conversation_id,
                                   tools=TOOLS, executors=_EXECUTORS)
    return text, [build_artifact(text)]


def run_turn(conversation_id: int, profile_id: int, user_text: str) -> None:
    """Synchronous turn (tests, CLI)."""
    chat.run_turn(conversation_id, profile_id, user_text,
                  generate=generate_reply, learn_style=False)


def start_turn(conversation_id: int, profile_id: int, user_text: str) -> None:
    """Background turn, polled through ``chat.is_pending``."""
    chat.start_turn(conversation_id, profile_id, user_text,
                    generate=generate_reply, learn_style=False)


# --- Rendering -----------------------------------------------------------
# The judge's text is plain text with [702.19b] citations and the occasional
# **bold** or "- " list the model adds anyway. Rendered here (not through a
# Markdown library) so that rule numbers become links to the in-page rule
# drawer, and only VERIFIED numbers do.

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_REF_RE = re.compile(r"\[?\b(\d{3}\.\d{1,3}[a-z]?)\b\]?")
_CHAPTER_REF_HTML_RE = re.compile(r"\b(rule|règle|section|chapitre)\s+(\d{3})(?!\d)(?!\.\d)", re.IGNORECASE)


def _ref_html(number: str, valid) -> str:
    if valid is None:
        ok = rules.exists(number)
    else:
        ok = number in valid
    if ok:
        return (f'<a class="rule-ref" href="/rules/r/{escape(number)}" '
                f'data-rule="{escape(number)}">{escape(number)}</a>')
    return (f'<span class="rule-ref missing" title="Numéro absent des Comprehensive '
            f'Rules chargées : citation non vérifiée">{escape(number)}</span>')


def _inline(text: str, valid) -> str:
    html = str(escape(text))
    html = _BOLD_RE.sub(r"<strong>\1</strong>", html)
    html = _REF_RE.sub(lambda m: _ref_html(m.group(1), valid), html)
    html = _CHAPTER_REF_HTML_RE.sub(
        lambda m: f"{m.group(1)} {_ref_html(m.group(2), valid)}", html
    )
    return html


def render_rules_text(text: str, valid=None) -> Markup:
    """Plain judge text -> safe HTML: paragraphs, "- " lists, bold, and rule
    citations as links (verified against ``valid`` — a set of numbers, or the
    live corpus when None)."""
    if valid is not None and not isinstance(valid, (set, frozenset)):
        # A stored artifact's citation list: only the verified ones link.
        valid = {c["number"] for c in valid if isinstance(c, dict) and c.get("found")} \
            | {c for c in valid if isinstance(c, str)}
    out: list[str] = []
    para: list[str] = []
    in_list = False

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    for raw in (text or "").split("\n"):
        line = raw.strip()
        item = re.match(r"^(?:[-•*]|\d+[.)])\s+(.*)$", line)
        if item:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1), valid)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            flush_para()
            continue
        para.append(_inline(line, valid))
    flush_para()
    if in_list:
        out.append("</ul>")
    return Markup("".join(out))
