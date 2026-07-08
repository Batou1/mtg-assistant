# CLAUDE.md — Guide pour agents IA

Application web **locale et personnelle** d'aide au deckbuilding Magic: the Gathering.
L'utilisateur importe sa collection (CSV ManaBox), décrit en français le deck qu'il
veut, et obtient des suggestions de commandants, des decklists complètes, une analyse
de complétude et une liste d'achat en EUR — le tout piloté soit par formulaire, soit
par un chat itératif (tool-calling Claude).

## ⚠️ Base de données = données de production

`data/app.db` contient les **vraies collections des utilisateurs** (c'est la base
que le service launchd déployé utilise aussi). Ne JAMAIS tester un import, une
suppression de profil ou toute écriture contre le serveur de dev lancé sur ce
répertoire : une requête sans cookie atterrit sur le premier profil et
`replace_collection` écrase sa collection sans confirmation. Pour tout test
manuel, lancer une instance isolée :

```bash
MTG_DB_PATH=/tmp/test.db MTG_BULK_AUTO_REFRESH=0 .venv/bin/uvicorn app.main:app --port 8991
```

## Commandes

```bash
./run.sh                      # lance l'app en local (crée .venv, installe les deps)
.venv/bin/python -m pytest tests/ -q     # tests (~1s, AUCUN réseau requis)
.venv/bin/python -m app.bulk_data        # import bulk Scryfall forcé (long, ~2.7 GB)
.venv/bin/python -m app.bulk_data data/all-cards-*.jsonl  # bootstrap depuis un fichier local
./deploy/install-service.sh   # installe le service launchd macOS (prod perso)
```

Secrets dans `.env` (gitignoré, sourcé par `run.sh` et `deploy/serve.sh`) :
`ANTHROPIC_API_KEY` (chat/intent/archétypes), `MTG_BRAVE_API_KEY` (recherche web 60 cartes).
**Sans clé Anthropic, l'app doit continuer à fonctionner** (parsing heuristique,
pas de chat complet) — toute nouvelle fonctionnalité LLM doit avoir un fallback ou
un message explicite.

## Stack et architecture

FastAPI + Jinja2 (rendu serveur, pas de build JS) + SQLite (`data/app.db`) + httpx.
LLM : API Anthropic (`claude-sonnet-5` par défaut, `MTG_ANTHROPIC_MODEL` pour changer).
Un seul worker uvicorn ; l'état en mémoire (caches, registre `_inflight` du chat)
suppose ce déploiement mono-process.

### Flux principal (formulaire `/suggest`)

```
wish (texte FR) → intent.parse_intent (LLM + fallback heuristique, backfill croisé)
  → format Commander ?  → analysis.analyze  (candidats = commandants POSSÉDÉS
  |                        + discovery des non-possédés via pages carte EDHREC)
  |    « hors collection » → analysis.find_commanders (pages thème EDHREC + pool
  |                          Scryfall local, marche avec collection vide)
  → format 60 cartes ?  → formats60.analyze (Brave Search → LLM propose un deck
                           complet 60 cartes {name,count} + manabase → CHAQUE carte
                           validée Scryfall existence + légalité, copies clampées à 4,
                           complété à 60 avec des bases ; possession comptée PAR
                           exemplaire ; decklist_text exportable)
Résultats → buylist.build (glouton, ordre EDHREC, budget + plafond/carte en EUR ;
                           accepte nom seul ou (nom, qté))
```

### Modules (`app/`)

| Module | Rôle |
|---|---|
| `main.py` | Routes FastAPI, profils (cookie), rendu Jinja. Pas de logique métier. |
| `config.py` | `settings` (dataclass figée), tout surchargeable par env `MTG_*`. |
| `db.py` | Tout le SQLite : profils, collections, caches cards/edhrec, conversations, meta. |
| `intent.py` | Wish FR → intent structuré. LLM d'abord, heuristique en secours, backfill croisé. |
| `analysis.py` | Suggestions de commandants (possédés + discovery + finder par thème). |
| `edhrec.py` | Client EDHREC (endpoints json.edhrec.com non officiels) + extraction défensive. |
| `scryfall.py` | Résolution nom/id → carte (batch `/cards/collection`), accesseurs (prix, image…). |
| `bulk_data.py` | Import des exports bulk Scryfall + rafraîchissement auto en thread démon. |
| `cardsearch.py` | Recherche dans le pool Scryfall local en mémoire (type/keyword/texte oracle). |
| `deckgen.py` | Decklist Commander 100 cartes depuis la page EDHREC (déterministe). |
| `poolbuild.py` | Meilleur deck depuis une liste fournie (`FormatSpec` par format ; LLM + fallback). |
| `formats60.py` | Deck 60 cartes complet par archétype (Brave → LLM → validation Scryfall, 4-of, manabase). |
| `chat.py` | Boucle agent (tools Anthropic sur les modules ci-dessus), tours en thread. |
| `buylist.py` | Liste d'achat gloutonne sous budget, prix EUR Cardmarket. |
| `collection.py` | Enrichissement de la collection depuis le cache local uniquement. |
| `commanders.py` | Éligibilité commandant + variantes (`FORMATS`, `SCRYFALL_LEGALITY`). |
| `manabox.py` / `parsing.py` | Parsing CSV ManaBox / decklists collées. |
| `research.py` | Brave Search API (titres+snippets pour ancrer le LLM). |
| `textutil.py` | Aides de présentation (paragraphes, strip Markdown). |

## Invariants à respecter absolument

1. **Ancrage anti-hallucination** : le LLM ne « décide » jamais seul d'une carte.
   Tout nom de carte sorti d'un prompt est re-vérifié en aval — existence Scryfall,
   légalité du format, appartenance au pool fourni (`poolbuild`), appartenance à la
   liste des possédées (`pool_bonus`). Si tu ajoutes un usage LLM qui produit des
   noms de cartes, ajoute la validation correspondante.

2. **EDHREC n'a que des pages Commander classique.** Duel Commander et Pauper
   Commander réutilisent la même page, filtrée par la légalité Scryfall
   (`commanders.SCRYFALL_LEGALITY` : `duel`, `paupercommander`) appliquée au
   commandant ET à chaque carte recommandée. Ne jamais construire d'URL EDHREC
   spécifique à ces variantes.

3. **Sentinelles EDHREC** : `edhrec.fetch_*` renvoie `{"_not_found": True}` (mis en
   cache) ou `{"_error": True}` (JAMAIS mis en cache, pour réessayer plus tard).
   Toujours tester les deux avant d'utiliser une réponse.

4. **Normalisation des noms** : la clé canonique d'une carte est
   `name.split("//")[0].strip().lower()` (face avant, minuscules). C'est la clé de
   la table `cards`, de `owned_name_keys`, des pools… Toute comparaison de noms
   doit passer par cette normalisation (helper `_norm` dans plusieurs modules).

5. **Deux contraintes budgétaires indépendantes** : `budget_eur` (total) et
   `max_card_price_eur` (plafond par carte). Elles se propagent ensemble partout
   (intent → analyse → buylist → deckgen → artefacts). Ne pas en fusionner une
   dans l'autre.

6. **Persistance du chat = texte + artefacts uniquement.** Le transcript
   `tool_use`/`tool_result` est éphémère (intra-tour). Le contexte inter-tours est
   reconstruit via `_context_snapshot` (les derniers artefacts re-sérialisés en
   texte dans le system prompt). Si tu ajoutes un type d'artefact : l'enregistrer
   dans `_SNAPSHOT_BUILDERS`, dans `_artifacts.html`, et penser à la
   rétro-compatibilité des artefacts déjà stockés (cf. `db._backfill_artifact`).

7. **Tours de chat asynchrones** : un tour peut dépasser le timeout edge
   Cloudflare (~100 s), donc `POST /chat/message` répond immédiatement et le tour
   tourne dans un thread (`chat.start_turn`) ; la page poll `/chat/status`.
   Ne jamais rendre le traitement d'un tour bloquant dans la requête.

8. **Requêtes lentes hors event loop** : les routes async qui font du travail
   bloquant (Scryfall/EDHREC/LLM) passent par `run_in_threadpool`. Idem pour tout
   nouveau pipeline.

9. **Caches et TTL** : cartes 7 j (`cache_ttl_days`), prix 1 j (`price_ttl_days`),
   EDHREC 7 j. Le cache `cards` est partagé entre profils ; les stats de la page
   d'accueil sont cachées dans `meta` (`collection_stats:<pid>`) et invalidées à
   l'import de collection et après un refresh bulk.

10. **Politesse envers les APIs externes** : throttle `request_delay`, User-Agent
    dédié, backoff avec jitter sur EDHREC (qui est derrière Cloudflare et bloque
    les clients non-navigateur — les en-têtes `_EDHREC_HEADERS` sont nécessaires).

## Conventions

- **UI et prompts LLM en français ; code, docstrings et commentaires en anglais.**
  Les réponses du modèle destinées à l'utilisateur sont en français.
- Docstrings expliquant le *pourquoi* (contraintes, pièges), pas le *quoi*.
- Tests dans `tests/`, sans réseau : les appels httpx/LLM sont monkeypatchés,
  la DB pointe vers un fichier temporaire via `MTG_DB_PATH` (voir les fixtures
  existantes). Toute nouvelle fonctionnalité suit ce modèle.
- Piège Jinja connu : ne pas nommer une clé de dict `items` (collision avec
  `dict.items` — cf. `buylist` qui utilise `to_buy`).
- Le schéma SQLite se migre en place au démarrage (`db.init_db` +
  `_migrate_collection`) : pas d'outil de migration, ajouter les colonnes/tables
  avec `IF NOT EXISTS` ou une migration idempotente du même style.
- **Incrémenter `APP_VERSION`** (`app/main.py`, affiché dans le footer via
  `base.html`) à CHAQUE nouvelle PR, même pour un correctif mineur (+0.1 ;
  passage majeur seulement pour une refonte significative).

## Déploiement (perso)

Service launchd macOS (`deploy/install-service.sh`), uvicorn lié à `127.0.0.1`
uniquement, exposé via un tunnel Cloudflare existant (`deploy/tunnel-ingress.snippet.yml`).
L'app n'a **aucune authentification propre** : la protection attendue est
**Cloudflare Access** devant le hostname. Ne jamais binder sur `0.0.0.0` ni
supposer que les routes sont privées.
