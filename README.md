# MTG Assistant

A local-first web app to help build Magic: the Gathering decks from your own
collection. Import your cards, describe in plain French the deck you want, and
get Commander suggestions you can actually build — with a completeness analysis
and a budget-constrained buylist priced in EUR.

> This release: ManaBox import, French natural-language intent via **Claude
> (Anthropic API)**, Commander suggestions with gap analysis and an EUR buylist,
> full Commander decklist generation, **60-card format archetype research**
> (Standard, Pauper, Modern, Pioneer…) grounded by Brave Search + validated
> against Scryfall, and an **iterative chat** that drives the whole pipeline by
> conversation (tool-calling).

## How it works

0. **Pick a profile** — several people can share the app, each with their own
   collection. Switch profiles from the top bar (create/delete too). No accounts
   or passwords; the active profile is remembered in a cookie.
1. **Import your collection** — upload a **ManaBox CSV** export. Cards are stored
   per profile in a local SQLite database (quantity, set, foil, condition,
   Scryfall id).
2. **Describe your wish** — e.g. *« un deck Commander aristocrats sacrifice en
   noir/rouge, budget 50€ »*. **Claude** (Anthropic API) turns it into a
   structured intent (format, colours, theme, budget). If no API key is set, a
   heuristic parser takes over so the app keeps working.
3. **Get suggestions** — for the legal commanders you own that match the
   requested colours, the app looks up [EDHREC](https://edhrec.com) and reports
   how many of each commander's most-played cards you already have.
4. **Buylist** — the missing cards are priced in EUR from
   [Scryfall](https://scryfall.com)'s Cardmarket data, and the most synergistic
   ones are picked greedily within your budget.
5. **Generate a full decklist** — from any suggested commander, build a complete
   100-card Commander deck: owned cards are reused, missing ones bought within
   budget (ranked by popularity-per-euro so the deck stays complete), lands
   topped up with basics. Each card is tagged *owned* / *to buy*, with a
   copy-paste export and an optional LLM-written game-plan summary.
6. **60-card formats** — describe a wish for Standard, Pauper, Modern… and the
   app researches the **archetype**: Brave Search surfaces recent meta pages, the
   LLM proposes the archetype's key cards, **every card is validated against
   Scryfall** (it must exist and be legal in the format, else it's dropped), then
   gap analysis vs your collection + an EUR buylist. This is an archetype base —
   not an exact tournament list — because the popular decklist sites are
   Cloudflare-blocked.
7. **Iterative chat** (`/chat`) — instead of one-shot forms, hold a conversation.
   **Claude** drives the whole pipeline through **tool-calling**: it inspects your
   collection, suggests commanders, researches 60-card archetypes, generates full
   decklists and looks up cards — all on demand, replying in French and keeping
   context, so you can refine (*« plutôt en mono-noir »*, *« monte le budget à
   80 € »*) without re-typing everything. The chat is **interactive**: when your
   request is vague it asks a clarifying question (format? couleurs? budget?)
   before building anything. Once a deck or set of suggestions exists, the latest
   one is **replayed into the model's context**, so follow-up questions (*«
   pourquoi cette carte ? »*, *« quelle est la courbe de mana ? »*, *« quelles
   cartes acheter en priorité ? »*) are answered from the deck already built —
   **without regenerating it** (no repeated EDHREC/Scryfall calls). Conversations
   are saved per profile. The model never names a card outside a tool result (or
   the replayed context of a previous tool result), so suggestions stay grounded.
   Without an API key the chat falls back to a one-shot analysis.

Card data, prices and EDHREC pages are resolved **on demand and cached** in
SQLite — no multi-gigabyte bulk download, minimal disk footprint.

## Requirements

- Python 3.11+
- An **Anthropic API key** for the LLM (intent parsing, 60-card archetypes, deck
  game-plans). Uses **`claude-sonnet-4-6`** by default — strong card-pool
  knowledge and French, a few cents/month for personal use. Put it in `.env`:

  ```bash
  echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
  ```

  Without a key the app still runs: intent parsing falls back to a heuristic and
  60-card research / game-plans are skipped. Commander suggestions, gap analysis,
  buylist and full decklist generation all work key-free (EDHREC + Scryfall).

## Run locally

```bash
./run.sh
```

Then open <http://127.0.0.1:8000>.

### Secrets (`.env`)

Keys live in a local **`.env`** file (gitignored — never committed); both
`run.sh` and the launchd service load it automatically:

```bash
ANTHROPIC_API_KEY=sk-ant-...      # LLM (intent, 60-card archetypes, game plans)
MTG_BRAVE_API_KEY=your-brave-key  # 60-card web research (free tier ≈ 2000 req/mo)
```

Without the Brave key, 60-card research still runs but ungrounded (Claude
proposes from its own knowledge only, still Scryfall-validated).

### Configuration (environment variables)

| Variable             | Default                     | Meaning                              |
| -------------------- | --------------------------- | ------------------------------------ |
| `MTG_DB_PATH`        | `data/app.db`               | SQLite database location             |
| `MTG_MIN_DECKS`      | `300`                       | Min EDHREC decks for a commander     |
| `ANTHROPIC_API_KEY`  | *(empty)*                   | Anthropic API key (read by the SDK)  |
| `MTG_ANTHROPIC_MODEL`| `claude-sonnet-4-6`         | Claude model used for all LLM tasks  |
| `MTG_CHAT_MAX_TOOL_ITERS` | `6`                    | Max tool-call rounds per chat turn   |
| `MTG_CHAT_HISTORY`   | `16`                        | Past messages replayed to the API    |
| `MTG_CACHE_TTL_DAYS` | `7`                         | Card / EDHREC cache freshness        |
| `MTG_PRICE_TTL_DAYS` | `1`                         | Price cache freshness                |
| `MTG_CURRENCY`       | `EUR`                       | Budget currency (Cardmarket)         |
| `MTG_BRAVE_API_KEY`  | *(empty)*                   | Brave Search key (Phase 2 research)  |

### Tests

```bash
source .venv/bin/activate
pytest
```

## Deploy as a background service + Cloudflare tunnel

Same model as the sibling `commander-analysis` app: a launchd LaunchAgent binds
the app to `127.0.0.1`, and a single Cloudflare tunnel routes a subdomain to it,
protected by Cloudflare Access (limited to your email).

```bash
./deploy/install-service.sh            # label com.phase.mtg, port 8771
```

Then add the subdomain to your existing tunnel's `config.yml` ingress (see
`deploy/tunnel-ingress.snippet.yml`), point DNS at the tunnel, and add a
Cloudflare Access policy. Remove later with `./deploy/uninstall-service.sh`.

## Notes & limitations

- **Commander** is the strongest path (EDHREC-backed, fully data-driven).
- **60-card formats** are archetype suggestions, not exact tournament lists: the
  popular decklist sites (MTGGoldfish, mtgdecks) are Cloudflare-blocked, so the
  app researches the archetype with Brave + Claude and validates every card via
  Scryfall.
- **EDHREC has no official API**; this uses its public JSON endpoints. If the
  payload shape changes, `app/edhrec.py` is the single place to adjust.
- For Commander, the LLM never names cards — it only structures your request and
  summarises a chosen list — so suggestions can't be hallucinated. For 60-card,
  the LLM does name cards, but Scryfall validation filters out anything that
  isn't a real, format-legal card.
- First analysis of a large collection makes many Scryfall/EDHREC calls;
  caching makes later runs fast.

## Project layout

```
app/
  config.py     settings (env-overridable)
  db.py         SQLite: profiles + per-profile collection + Scryfall/EDHREC caches
  manabox.py    ManaBox CSV → collection rows
  parsing.py    plain-text decklist parser (reused)
  scryfall.py   card resolution, prices (EUR), images, legality
  edhrec.py     commander pages: popularity + recommended cards
  commanders.py legal-commander detection
  llm.py        Anthropic (Claude) client: JSON intent, archetypes, game plans
  intent.py     French wish → structured intent (LLM + heuristic fallback)
  analysis.py   intent-aware commander ranking + gap analysis
  buylist.py    budget-constrained EUR shopping list
  deckgen.py    full 100-card Commander decklist builder
  research.py   Brave Search client (web research)
  formats60.py  60-card archetype pipeline (research + Scryfall validation)
  chat.py       iterative chat: agent loop + tools over the pipeline
  main.py       FastAPI routes + templates
deploy/         launchd service + Cloudflare tunnel snippet
tests/          pytest
```
