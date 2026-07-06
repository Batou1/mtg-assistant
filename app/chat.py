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

from . import analysis, cardsearch, commanders, db, deckgen, formats60, intent, llm, poolbuild, scryfall
from .config import settings

logger = logging.getLogger(__name__)

_LEGALITY_FORMATS = (
    "commander", "standard", "pioneer", "modern", "legacy", "vintage", "pauper", "premodern",
)

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
    "demandés. « monocouleur » => max_colors=1. Convertis les noms de "
    "guildes/shards/wedges en couleurs WUBRG quand tu appelles un outil "
    "(ex: Grixis=U,B,R ; Rakdos=B,R ; Jeskai=U,R,W ; Esper=W,U,B ; Bant=G,W,U).\n"
    "- BUDGET TOTAL vs PLAFOND PAR CARTE : ce sont deux contraintes "
    "différentes et indépendantes. budget_eur est le total à ne pas dépasser ; "
    "max_card_price_eur est un plafond individuel (« pas plus de 5€ la carte », "
    "« chaque carte à 5€ max »). Si le joueur donne les deux (« budget 30€ mais "
    "pas plus de 5€ par carte »), passe TOUJOURS les deux paramètres — aucune "
    "carte individuelle ne doit dépasser max_card_price_eur même s'il reste du "
    "budget total.\n"
    "- Si le joueur veut un commandant qu'il NE possède PAS (« que je n'ai pas », "
    "« à acquérir », « un nouveau commandant »), appelle suggest_commanders avec "
    "unowned_only=true.\n"
    "- RECHERCHE PAR THÈME, HORS COLLECTION : si le joueur veut découvrir les "
    "commandants possibles pour un thème/des caractéristiques SANS se limiter à "
    "sa collection (« cherche sur EDHREC/Scryfall », « indépendamment de ma "
    "collection », « quels commandants existent pour ce thème ? »), appelle "
    "find_commanders (et non suggest_commanders). Présente les candidats et "
    "demande-lui lesquels il RETIENT — ne génère aucune decklist avant qu'il ait "
    "validé. Quand il valide un ou plusieurs commandants (« je retiens X et Y »), "
    "la conversation reprend normalement : propose generate_decklist pour CHAQUE "
    "commandant retenu — les cartes de sa collection qui conviennent y sont "
    "réutilisées et signalées comme possédées.\n"
    "- Pour le Commander (EDH), le Duel Commander (1 contre 1) ou le Pauper "
    "Commander (cartes majoritairement communes, aussi appelé PDH), appelle "
    "suggest_commanders avec le paramètre format adéquat (commander, "
    "duelcommander ou paupercommander). Pour Standard, Modern, Pioneer, Pauper, "
    "Legacy, Vintage, Premodern (60 cartes), appelle research_archetype.\n"
    "- DEPUIS UNE LISTE : le joueur peut importer une LISTE de cartes (page "
    "« Depuis une liste ») et demander le meilleur deck d'un format donné "
    "(Limité, Commander, Modern, Pauper…). Si une liste a déjà été importée (voir "
    "« DECK … » dans le CONTEXTE ACTUEL), tu peux reconstruire ou ajuster le deck "
    "(autres couleurs, plus agressif, autre thème, autre format) avec "
    "build_pool_deck ; n'utilise QUE des cartes de la liste. Pour les formats "
    "construits/Commander, l'outil propose AUSSI des cartes bonus (possédées + à "
    "acheter) à ajouter EN PLUS du deck. Si aucune liste n'a été importée, invite "
    "le joueur à le faire sur la page « Depuis une liste ».\n"
    "- Quand le joueur choisit un commandant, propose-lui de générer la decklist "
    "complète (generate_decklist).\n"
    "- Pour des cartes de SYNERGIE précises (aristocrates, affinité artefacts, "
    "contrôle de cimetière, tribal zombie, etc.) au-delà de ce que propose "
    "EDHREC ou de ta seule connaissance, appelle search_cards : il interroge "
    "toute la base Scryfall locale par type, mots-clés d'capacité et texte "
    "d'oracle, et ne renvoie que des cartes réelles. Utilise-le quand le "
    "joueur décrit une stratégie/un thème plutôt qu'un simple commandant, ou "
    "pour suggérer des ajouts/remplacements à un deck déjà généré.\n"
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
    "budget_eur": {"type": "number", "description": "Budget TOTAL en euros, si mentionné."},
    "max_card_price_eur": {
        "type": "number",
        "description": (
            "Prix MAXIMUM par carte en euros, UNIQUEMENT si le joueur précise "
            "un plafond individuel distinct du budget total (ex: « budget 30€ "
            "mais pas plus de 5€ par carte » => budget_eur=30, "
            "max_card_price_eur=5). Omettre si non précisé."
        ),
    },
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
            "Pour un format COMMANDER (commander, duelcommander, paupercommander) : "
            "à partir des cartes possédées, propose des commandants jouables qui "
            "collent aux couleurs/thème, avec le taux de complétude EDHREC et une "
            "liste d'achat dans le budget. Inclut aussi des commandants NON "
            "possédés mais liés à tes cartes (champ owned=false, avec price_eur, "
            "link_count et total_cost_eur) qui respectent thème, couleurs et budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": sorted(commanders.FORMATS),
                    "description": (
                        "Variante Commander visée : \"commander\" (EDH classique, "
                        "défaut), \"duelcommander\" (Duel Commander, 1 contre 1) ou "
                        "\"paupercommander\" (Pauper Commander / PDH, cartes "
                        "majoritairement communes)."
                    ),
                },
                **_INTENT_PROPS,
            },
        },
    },
    {
        "name": "find_commanders",
        "description": (
            "Cherche des commandants potentiels pour un thème/des caractéristiques "
            "donnés, INDÉPENDAMMENT de la collection du joueur : interroge les "
            "pages de thème EDHREC (commandants les plus joués du thème) et la "
            "base Scryfall locale (légendaires dont le texte ou le type colle au "
            "thème). Chaque candidat est ensuite comparé à la collection à titre "
            "indicatif (complétude, liste d'achat, possédé ou non). Utilise-le "
            "quand le joueur veut explorer les commandants possibles au-delà de "
            "ses cartes, puis demande-lui lesquels il RETIENT avant de générer "
            "des decklists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": sorted(commanders.FORMATS),
                    "description": (
                        "Variante Commander visée : \"commander\" (défaut), "
                        "\"duelcommander\" ou \"paupercommander\"."
                    ),
                },
                **_INTENT_PROPS,
            },
        },
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
                "format": {
                    "type": "string",
                    "enum": sorted(commanders.FORMATS),
                    "description": (
                        "Variante Commander visée (défaut \"commander\") ; reprends "
                        "celle déjà utilisée pour ce commandant dans la conversation "
                        "(suggest_commanders) si elle est connue."
                    ),
                },
                "budget_eur": {"type": "number", "description": "Budget TOTAL d'achat en euros."},
                "max_card_price_eur": dict(_INTENT_PROPS["max_card_price_eur"]),
                "theme": {"type": "string", "description": "Thème pour le plan de jeu."},
            },
            "required": ["commander"],
        },
    },
    {
        "name": "build_pool_deck",
        "description": (
            "(Re)construit le meilleur deck à partir de la LISTE de cartes déjà "
            "importée dans cette conversation, pour le format demandé (limited, "
            "commander, modern, pauper, standard, pioneer, legacy, vintage, "
            "premodern). Utilise-le pour ajuster le deck — autres couleurs, plus "
            "agressif, autre thème, autre format. Ne fonctionne que si une liste a "
            "été importée via la page « Depuis une liste ». Choisit automatiquement "
            "les meilleures couleurs si elles ne sont pas imposées ; pour les formats "
            "construits/Commander, propose aussi des cartes bonus (possédées + à "
            "acheter) en plus du deck."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": sorted(poolbuild.SPECS),
                    "description": "Format visé pour le deck construit depuis la liste (par défaut, celui déjà utilisé).",
                },
                "colors": dict(_INTENT_PROPS["colors"]),
                "max_colors": dict(_INTENT_PROPS["max_colors"]),
                "theme": dict(_INTENT_PROPS["theme"]),
                "keywords": dict(_INTENT_PROPS["keywords"]),
                "budget_eur": dict(_INTENT_PROPS["budget_eur"]),
                "max_card_price_eur": dict(_INTENT_PROPS["max_card_price_eur"]),
            },
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
    {
        "name": "search_cards",
        "description": (
            "Cherche dans TOUTE la base Scryfall locale (~35 000 cartes — pas "
            "seulement ce que propose EDHREC ou ta connaissance) des cartes "
            "réelles correspondant à des critères de gameplay : couleurs, type "
            "de carte, mots-clés d'capacité officiels, texte d'oracle, coût de "
            "mana, légalité. Chaque résultat est une vraie carte : tu peux la "
            "citer directement. Utilise-le pour trouver des cartes de synergie "
            "précises (ex: 'sacrifice de créatures en mono-noir', 'affinité "
            "artefacts', 'tribal zombie') au-delà de la popularité EDHREC."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "colors": dict(_INTENT_PROPS["colors"]),
                "type_contains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Termes de type de carte, en anglais (ex: 'Creature', "
                        "'Artifact', 'Zombie'). Une carte matchant AU MOINS un "
                        "terme est incluse."
                    ),
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Mots-clés d'capacité OFFICIELS Scryfall, en anglais "
                        "(flying, trample, deathtouch, proliferate, convoke…). "
                        "Pour un thème qui n'est pas un mot-clé officiel "
                        "(sacrifice, cimetière, pioche…), utilise text_contains."
                    ),
                },
                "text_contains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Extraits de texte d'oracle à chercher, en anglais (ex: "
                        "'sacrifice a creature', 'return...from your "
                        "graveyard'). Une carte matchant AU MOINS un extrait "
                        "est incluse."
                    ),
                },
                "legal_in": {
                    "type": "string",
                    "enum": list(_LEGALITY_FORMATS),
                    "description": "Format de légalité à respecter.",
                },
                "min_cmc": {"type": "number", "description": "Coût de mana minimum."},
                "max_cmc": {"type": "number", "description": "Coût de mana maximum."},
                "limit": {
                    "type": "integer",
                    "description": "Nombre maximum de résultats (défaut 25, max 60).",
                },
            },
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


def _cap_suffix(max_card_price) -> str:
    return f" (max {max_card_price}€/carte)" if max_card_price is not None else ""


def _intent_from(args: dict, fmt: str | None) -> dict:
    return intent._coerce(
        {
            "format": fmt,
            "colors": args.get("colors") or [],
            "max_colors": args.get("max_colors"),
            "theme": args.get("theme") or "",
            "keywords": args.get("keywords") or [],
            "budget_eur": args.get("budget_eur"),
            "max_card_price_eur": args.get("max_card_price_eur"),
            "include_low_decks": args.get("include_low_decks"),
            "unowned_only": args.get("unowned_only"),
            "source": "llm",
        }
    )


def _exec_collection_summary(args: dict, profile_id: int, ctx: dict | None = None):
    distinct, total = db.collection_count(profile_id)
    if not total:
        return "La collection est vide : aucune carte importée.", None
    return f"Collection : {total} cartes ({distinct} cartes uniques).", None


def _exec_suggest_commanders(args: dict, profile_id: int, ctx: dict | None = None):
    distinct, _ = db.collection_count(profile_id)
    if not distinct:
        return "La collection est vide ; impossible de proposer des commandants.", None

    fmt = (args.get("format") or "commander").lower()
    if fmt not in commanders.FORMATS:
        fmt = "commander"
    parsed = _intent_from(args, fmt)
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
        top = owned_res[:8] + proposed_res[:3]
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
                f"{_cap_suffix(buy.get('max_card_price_eur'))}"
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


def _exec_find_commanders(args: dict, profile_id: int, ctx: dict | None = None):
    fmt = (args.get("format") or "commander").lower()
    if fmt not in commanders.FORMATS:
        fmt = "commander"
    parsed = _intent_from(args, fmt)
    data = analysis.find_commanders(parsed, profile_id)
    results = data.get("results") or []
    if parsed.get("unowned_only"):
        results = [r for r in results if not _is_owned(r)]
    if not results:
        return (
            f"Aucun commandant trouvé pour ce thème ({data.get('candidate_count', 0)} "
            "candidat(s) examiné(s)). Suggère de reformuler le thème avec des "
            "mots-clés EDHREC en anglais (aristocrats, tokens, reanimator, "
            "zombies…) ou d'élargir les couleurs.",
            None,
        )

    for r in results:
        r.pop("missing_cards", None)
    artifact = {"type": "commanders", "finder": True, "intent": parsed,
                "results": results, "notices": data.get("notices") or []}

    lines = []
    for r in results:
        buy = r.get("buylist") or {}
        if _is_owned(r):
            tag = "possédé"
        else:
            price = r.get("price_eur")
            tag = (f"à acquérir, {price} €" if price is not None
                   else "à acquérir, prix indisponible")
        lines.append(
            f"- {r['name']} ({tag}) : {r['pct']}% complété ({r['owned_count']}/"
            f"{r['total_recommended']}), {r['num_decks']} decks EDHREC, "
            f"achat {buy.get('total_eur', 0)} € ({buy.get('bought_count', 0)} cartes)"
            f"{_cap_suffix(buy.get('max_card_price_eur'))}"
        )
    text = (
        "Commandants trouvés pour ce thème (recherche EDHREC + Scryfall, "
        "indépendante de la collection) :\n" + "\n".join(lines) +
        "\n\nDemande au joueur lesquels il retient avant de générer une decklist."
    )
    return text, artifact


def _exec_research_archetype(args: dict, profile_id: int, ctx: dict | None = None):
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
        f"({buy.get('bought_count', 0)} cartes)"
        f"{_cap_suffix(buy.get('max_card_price_eur'))}."
    )
    return text, artifact


def _exec_generate_decklist(args: dict, profile_id: int, ctx: dict | None = None):
    commander = (args.get("commander") or "").strip()
    if not commander:
        return "Aucun commandant fourni.", None

    fmt = (args.get("format") or "commander").lower()
    if fmt not in commanders.FORMATS:
        fmt = "commander"
    deck, _data = deckgen.generate_full_deck(
        commander, args.get("budget_eur"), args.get("theme") or "", profile_id,
        max_card_price=args.get("max_card_price_eur"), fmt=fmt,
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
        f"{c['to_buy']} à acheter pour {deck['buy_total_eur']} €"
        f"{_cap_suffix(deck.get('max_card_price_eur'))}."
    )
    return text, artifact


def _exec_lookup_card(args: dict, profile_id: int, ctx: dict | None = None):
    name = (args.get("name") or "").strip()
    if not name:
        return "Aucun nom de carte fourni.", None
    resolved, _nf = scryfall.resolve_cards([name])
    card = resolved.get(name.lower())
    if not card:
        return f"Carte « {name} » introuvable sur Scryfall.", None
    price = scryfall.price_eur(card)
    legal = [f for f in _LEGALITY_FORMATS if scryfall.legal_in(card, f)]
    return (
        f"{card['name']} — prix {price if price is not None else 'indisponible'} € ; "
        f"légale en : {', '.join(legal) or 'aucun format listé'}.",
        None,
    )


def _search_result_line(card: dict) -> str:
    cost = card.get("mana_cost") or ""
    type_line = card.get("type_line") or ""
    oracle = card.get("oracle_text") or ""
    if not oracle:
        faces = card.get("card_faces") or []
        oracle = " // ".join(f.get("oracle_text", "") for f in faces if f.get("oracle_text"))
    oracle = oracle.replace("\n", " ").strip()
    if len(oracle) > 140:
        oracle = oracle[:140] + "…"
    price = scryfall.price_eur(card)
    price_txt = f"{price}€" if price is not None else "prix indisponible"
    return f"- {card['name']} | {cost} | {type_line} | {price_txt} | {oracle}"


def _exec_search_cards(args: dict, profile_id: int, ctx: dict | None = None):
    limit = min(int(args.get("limit") or 25), 60)
    results = cardsearch.search(
        colors=args.get("colors") or None,
        type_contains=args.get("type_contains"),
        keywords=args.get("keywords"),
        text_contains=args.get("text_contains"),
        legal_in=args.get("legal_in"),
        min_cmc=args.get("min_cmc"),
        max_cmc=args.get("max_cmc"),
        limit=limit,
    )
    if not results:
        return "Aucune carte trouvée pour ces critères.", None
    lines = [_search_result_line(c) for c in results]
    return f"{len(results)} carte(s) trouvée(s) :\n" + "\n".join(lines), None


# The pool-deck artifact type. "limited_deck" is kept as a read alias so decks
# built before the multi-format generalisation still render and replay.
POOL_ARTIFACT_TYPES = ("pool_deck", "limited_deck")


def _stored_pool_deck(conversation_id) -> dict | None:
    """Latest pool-built deck artifact for this conversation (any format)."""
    if conversation_id is None:
        return None
    latest = _latest_artifacts(conversation_id)
    for t in POOL_ARTIFACT_TYPES:
        if t in latest:
            return latest[t]
    return None


def _pool_summary(deck: dict) -> str:
    arch = deck.get("archetype") or {}
    counts = deck.get("counts") or {}
    head = f"Deck {deck.get('format_label') or 'Limité'} « {arch.get('name')} »"
    if deck.get("commander"):
        head += f", commandant {deck['commander']['name']}"
    text = (
        f"{head} ({_fmt_colors(arch.get('colors'))}) : {counts.get('total', 0)} cartes "
        f"({counts.get('creatures', 0)} créatures, {counts.get('lands', 0)} terrains) "
        f"depuis une liste de {counts.get('pool_size', 0)} cartes."
    )
    bonus = deck.get("bonus")
    if bonus and (bonus.get("owned_count") or bonus.get("buy_count")):
        text += (
            f" Bonus (en plus du deck) : {bonus.get('owned_count', 0)} carte(s) "
            f"possédée(s) + {bonus.get('buy_count', 0)} à acheter "
            f"({bonus.get('buy_total_eur', 0)} €"
            f"{_cap_suffix(bonus.get('max_card_price_eur'))})."
        )
    return text


def build_pool_artifact(pool_items, fmt: str, intent: dict,
                        profile_id: int | None = None) -> tuple[dict, dict]:
    """Build a deck from a card list and wrap it as a chat artifact.

    The pool + format are carried inside the artifact so later turns can rebuild
    from them (different colours/theme/format) without the player re-importing.
    """
    deck = poolbuild.build(pool_items, fmt, intent, profile_id=profile_id)
    deck["pool"] = [{"name": n, "qty": int(q)} for n, q in pool_items]
    artifact = {"type": "pool_deck", "intent": intent, "data": deck}
    return artifact, deck


def _exec_build_pool_deck(args: dict, profile_id: int, ctx: dict | None = None):
    prev = _stored_pool_deck((ctx or {}).get("conversation_id"))
    pool = (prev.get("data") or {}).get("pool") if prev else None
    if not pool:
        return (
            "Aucune liste de cartes n'a été importée dans cette conversation. "
            "Invite le joueur à l'importer sur la page « Depuis une liste ».",
            None,
        )
    # Default to the format the list was imported for; allow the model to switch.
    prev_fmt = (prev.get("data") or {}).get("format") or poolbuild.DEFAULT_FORMAT
    fmt = (args.get("format") or prev_fmt).lower()
    if fmt not in poolbuild.SPECS:
        fmt = poolbuild.DEFAULT_FORMAT
    parsed = _intent_from(args, None)
    pool_items = [(p["name"], p.get("qty", 1)) for p in pool]
    deck = poolbuild.build(pool_items, fmt, parsed, profile_id=profile_id)
    deck["pool"] = pool
    artifact = {"type": "pool_deck", "intent": parsed, "data": deck}
    return _pool_summary(deck), artifact


_EXECUTORS = {
    "get_collection_summary": _exec_collection_summary,
    "suggest_commanders": _exec_suggest_commanders,
    "find_commanders": _exec_find_commanders,
    "research_archetype": _exec_research_archetype,
    "generate_decklist": _exec_generate_decklist,
    "build_pool_deck": _exec_build_pool_deck,
    "lookup_card": _exec_lookup_card,
    "search_cards": _exec_search_cards,
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
    fmt = deck.get("format") or "commander"
    lines = [
        f"DECK GÉNÉRÉ ({fmt.upper()}) — commandant {art.get('commander')} "
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
    fmt = (art.get("intent") or {}).get("format") or "commander"
    head = ("COMMANDANTS TROUVÉS PAR THÈME, HORS COLLECTION"
            if art.get("finder") else "COMMANDANTS SUGGÉRÉS")
    lines = [f"{head} ({fmt.upper()}) :"]
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


def _snapshot_pool(art: dict) -> str:
    data = art.get("data") or {}
    arch = data.get("archetype") or {}
    counts = data.get("counts") or {}
    head = f"DECK {(data.get('format_label') or 'Limité').upper()} — {arch.get('name')}"
    if data.get("commander"):
        head += f" (commandant {data['commander']['name']})"
    lines = [
        f"{head} ({_fmt_colors(arch.get('colors'))}) : "
        f"{counts.get('total', 0)} cartes "
        f"({counts.get('creatures', 0)} créatures, {counts.get('lands', 0)} terrains) "
        f"depuis une liste de {counts.get('pool_size', 0)} cartes."
    ]
    if arch.get("strategy"):
        lines.append(f"Stratégie : {arch['strategy']}")
    for group in data.get("groups") or []:
        names = [f"{c['qty']}x {c['name']}" if c.get("qty", 1) > 1 else c["name"]
                 for c in group.get("cards") or []]
        if names:
            lines.append(f"{group.get('label')} : " + ", ".join(names))
    lands = data.get("lands") or {}
    land_bits = [f"{c['qty']}x {c['name']}" for c in lands.get("nonbasic") or []]
    land_bits += [f"{c['qty']}x {c['name']}" for c in lands.get("basics") or []]
    if land_bits:
        lines.append("Terrains : " + ", ".join(land_bits))
    bonus = data.get("bonus") or {}
    if bonus.get("owned"):
        lines.append("Bonus possédés (en plus du deck) : "
                     + ", ".join(c["name"] for c in bonus["owned"]))
    if bonus.get("buy"):
        lines.append(
            f"Bonus à acheter ({bonus.get('buy_total_eur', 0)} €) : "
            + ", ".join(f"{c['name']} ({c.get('price_eur')} €)" for c in bonus["buy"])
        )
    sideboard = data.get("sideboard") or []
    if sideboard:
        sb = [f"{c['qty']}x {c['name']}" if c.get("qty", 1) > 1 else c["name"]
              for c in sideboard]
        lines.append("Réserve (cartes de la liste non jouées) : " + ", ".join(sb))
    return "\n".join(lines)


_SNAPSHOT_BUILDERS = {
    "decklist": _snapshot_decklist,
    "commanders": _snapshot_commanders,
    "archetype": _snapshot_archetype,
    "pool_deck": _snapshot_pool,
    "limited_deck": _snapshot_pool,
}


def _context_snapshot(conversation_id: int) -> str:
    """Compact text of the latest generated artifacts, for the system prompt."""
    latest = _latest_artifacts(conversation_id)
    # Most actionable first: a generated deck, then commander/archetype research.
    blocks = [
        _SNAPSHOT_BUILDERS[t](latest[t])
        for t in ("decklist", "pool_deck", "limited_deck", "commanders", "archetype")
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


def _agent_loop(api_messages: list[dict], profile_id: int, system: str,
                conversation_id: int | None = None):
    """Run the tool-use loop. Returns (final_text, artifacts)."""
    texts: list[str] = []
    artifacts: list[dict] = []
    ctx = {"conversation_id": conversation_id}

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
            text, artifact = executor(b.input or {}, profile_id, ctx)
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
    return _agent_loop(api_messages, profile_id, system, conversation_id)


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


def create_pool_conversation(profile_id: int, pool_items, fmt: str, intent: dict) -> int:
    """Build a deck from an imported card list and open a chat seeded with it.

    Returns the new conversation id. Blocking (Scryfall + LLM); call it off the
    request thread. The deck lands as the first assistant message so the player
    can immediately discuss/refine it ("plutôt en bleu", "pourquoi cette carte").
    """
    spec = poolbuild.SPECS.get(fmt) or poolbuild.SPECS[poolbuild.DEFAULT_FORMAT]
    title = f"Deck {spec.label}"
    cid = db.create_conversation(profile_id, title=title)

    pool_total = sum(int(q) for _, q in pool_items)
    distinct = len(pool_items)
    user_text = (
        f"J'ai importé une liste de {pool_total} cartes ({distinct} cartes uniques) "
        f"pour construire un deck {spec.label}. Propose-moi le meilleur deck."
    )
    db.add_message(cid, "user", user_text)

    if not pool_items:
        db.add_message(
            cid, "assistant",
            "La liste importée est vide ou illisible. Colle une liste de cartes "
            "(une par ligne, ex. « 2 Lightning Bolt ») ou un export CSV.",
        )
        db.touch_conversation(cid, title=title)
        return cid

    artifact, deck = build_pool_artifact(pool_items, fmt, intent, profile_id=profile_id)
    invalid = deck.get("invalid") or []
    intro = _pool_summary(deck)
    if deck.get("archetype", {}).get("strategy"):
        intro += "\n\n" + deck["archetype"]["strategy"]
    if invalid:
        intro += (
            f"\n\n{len(invalid)} carte(s) de la liste n'ont pas été reconnues sur "
            "Scryfall (ou illégales dans ce format) et ont été ignorées."
        )
    intro += (
        "\n\nDis-moi si tu veux ajuster (autres couleurs, plus agressif, un thème "
        "précis, un autre format) ou pose-moi des questions sur le deck."
    )
    db.add_message(cid, "assistant", intro, artifacts=[artifact])
    db.touch_conversation(cid, title=title)
    return cid


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
