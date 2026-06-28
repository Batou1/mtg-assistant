# MTG Assistant

A local-first web app to help build Magic: the Gathering decks from your own
collection. Import your cards, describe in plain French the deck you want, and
get Commander suggestions you can actually build — with a completeness analysis
and a budget-constrained buylist priced in EUR.

> Phases 0–2a (this release): ManaBox import, French natural-language intent via
> a local LLM, Commander suggestions with gap analysis and an EUR buylist, and
> full Commander decklist generation. Phase 2b (planned): 60-card formats with
> web research of recent tournament decks (curated sources + Brave Search).

## How it works

0. **Pick a profile** — several people can share the app, each with their own
   collection. Switch profiles from the top bar (create/delete too). No accounts
   or passwords; the active profile is remembered in a cookie.
1. **Import your collection** — upload a **ManaBox CSV** export. Cards are stored
   per profile in a local SQLite database (quantity, set, foil, condition,
   Scryfall id).
2. **Describe your wish** — e.g. *« un deck Commander aristocrats sacrifice en
   noir/rouge, budget 50€ »*. A **local LLM (Ollama)** turns it into a structured
   intent (format, colours, theme, budget). If Ollama is offline, a heuristic
   parser takes over so the app keeps working.
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

Card data, prices and EDHREC pages are resolved **on demand and cached** in
SQLite — no multi-gigabyte bulk download, minimal disk footprint.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally with the recommended model:

  ```bash
  ollama pull qwen2.5:7b-instruct
  ```

  Chosen for strong structured-JSON output and French handling; ~4.7 GB (Q4),
  comfortable on a 16 GB Mac mini. The app still runs (heuristic fallback) if
  Ollama is unavailable.

## Run locally

```bash
./run.sh
```

Then open <http://127.0.0.1:8000>.

### Configuration (environment variables)

| Variable             | Default                     | Meaning                              |
| -------------------- | --------------------------- | ------------------------------------ |
| `MTG_DB_PATH`        | `data/app.db`               | SQLite database location             |
| `MTG_MIN_DECKS`      | `300`                       | Min EDHREC decks for a commander     |
| `MTG_OLLAMA_URL`     | `http://127.0.0.1:11434`    | Ollama server                        |
| `MTG_OLLAMA_MODEL`   | `qwen2.5:7b-instruct`       | Local model used for intent parsing  |
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

- **Commander only** for now (EDHREC-based). 60-card formats arrive in Phase 2.
- **EDHREC has no official API**; this uses its public JSON endpoints. If the
  payload shape changes, `app/edhrec.py` is the single place to adjust.
- The LLM never names cards itself — it only structures your request and
  summarises a chosen list — so card suggestions can't be hallucinated.
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
  llm.py        Ollama client (JSON mode + free-text game plan)
  intent.py     French wish → structured intent (LLM + heuristic fallback)
  analysis.py   intent-aware commander ranking + gap analysis
  buylist.py    budget-constrained EUR shopping list
  deckgen.py    full 100-card Commander decklist builder
  main.py       FastAPI routes + templates
deploy/         launchd service + Cloudflare tunnel snippet
tests/          pytest
```
