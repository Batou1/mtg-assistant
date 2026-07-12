"""FastAPI app: pick a profile, import its ManaBox collection, describe a deck
wish in French, and get Commander suggestions with gap analysis and a
budget-constrained buylist. Profiles are lightweight — no accounts/passwords;
the active profile is remembered in a cookie.
"""
import logging
import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import (
    analysis, bulk_data, chat, collection as collection_mod, commanders, db, deckgen, formats60,
    intent, llm, manabox, poolbuild, textutil,
)
from .config import settings

APP_VERSION = "1.2"

logger = logging.getLogger(__name__)

app = FastAPI(title="MTG Assistant", version=APP_VERSION)

# Uploads (ManaBox CSV, decklists) are read fully into memory: cap their size
# so a huge/accidental file can't take the single-process app down.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

_ERROR_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>MTG Assistant — erreur</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#333}
a{color:#7c4a2d}</style></head>
<body><h1>Oups, une erreur est survenue</h1>
<p>Le service a rencontré un problème inattendu (souvent une source externe
— Scryfall ou EDHREC — momentanément indisponible). Réessaie dans un instant.</p>
<p><a href="/">← Retour à l'accueil</a></p></body></html>"""


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """Friendly 500 page instead of a bare "Internal Server Error" (details go
    to the log). Poll endpoints get JSON so the chat page's poller stops cleanly."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/chat/status"):
        return JSONResponse({"pending": False}, status_code=500)
    return HTMLResponse(_ERROR_PAGE, status_code=500)

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _static_version() -> str:
    """Cache-busting token from the stylesheet's mtime.

    Cloudflare caches ``.css`` at the edge by default, so a plain
    ``/static/style.css`` URL keeps serving a stale file across deploys (even in
    private browsing). Appending ``?v=<mtime>`` changes the URL whenever the file
    changes, forcing the edge to fetch the fresh copy. Computed at startup — the
    service is restarted on every deploy anyway.
    """
    try:
        return str(int(os.path.getmtime(os.path.join(STATIC_DIR, "style.css"))))
    except OSError:
        return "0"


templates.env.globals["static_v"] = _static_version()
templates.env.globals["app_version"] = APP_VERSION
templates.env.filters["paragraphs"] = textutil.paragraphs

# Looks up a card's text-view fields (cost/type/oracle text/P-T) by name in
# the local Scryfall cache (no network call), memoized in memory — see
# collection.card_text_info. Used by _cardview.html to render the text
# alternative to the card image, toggled globally via the navbar switch.
templates.env.globals["card_info"] = collection_mod.card_text_info

db.init_db()
# Keeps the local card cache filled from Scryfall's bulk-data exports, checked
# hourly and re-imported whenever the export is more than bulk_refresh_hours
# old — see app/bulk_data.py. Runs in the background so a slow first import
# never blocks server startup; until it lands, resolve_cards/resolve_ids fall
# back to the live API on cache misses as before.
bulk_data.start_background_refresh()

COOKIE = "profile_id"
CONV_COOKIE = "conversation_id"
COLOR_NAMES = {"W": "Blanc", "U": "Bleu", "B": "Noir", "R": "Rouge", "G": "Vert"}
templates.env.globals["COLOR_NAMES"] = COLOR_NAMES
# French display label per format slug (e.g. "duelcommander" -> "Duel Commander"),
# reusing poolbuild's FormatSpec labels so the two stay in sync.
templates.env.globals["FORMAT_LABELS"] = {f: spec.label for f, spec in poolbuild.SPECS.items()}


def current_profile(request: Request) -> dict:
    """Resolve the active profile from the cookie, falling back to a default."""
    profile = db.get_profile(request.cookies.get(COOKIE))
    if profile is None:
        profile = db.get_profile(db.ensure_default_profile())
    return profile


def _set_cookie(resp, name: str, value) -> None:
    """(Re)assert a long-lived app cookie (active profile / conversation)."""
    resp.set_cookie(name, str(value), max_age=60 * 60 * 24 * 365, samesite="lax")


async def _read_upload(file: UploadFile) -> bytes | None:
    """Read an upload chunk by chunk, or None if it exceeds MAX_UPLOAD_BYTES."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _base_context(request: Request, profile: dict, **extra) -> dict:
    ctx = {
        "request": request,
        "profiles": db.list_profiles(),
        "profile": profile,
    }
    ctx.update(extra)
    return ctx


def _render(request: Request, profile: dict, name: str, ctx: dict) -> HTMLResponse:
    """Render a template and (re)assert the active-profile cookie."""
    resp = templates.TemplateResponse(request, name, ctx)
    _set_cookie(resp, COOKIE, profile["id"])
    return resp


def _home_context(request: Request, profile: dict, **extra) -> dict:
    stats = collection_mod.stats(profile["id"])
    return _base_context(
        request,
        profile,
        distinct=stats["distinct"],
        total=stats["total"],
        total_value=stats["total_value"],
        color_breakdown=stats["color_breakdown"] if stats["total"] else None,
        source=profile.get("collection_source"),
        llm_ok=llm.is_available(),
        llm_model=settings.anthropic_model,
        **extra,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    profile = current_profile(request)
    return _render(request, profile, "index.html", _home_context(request, profile))


# --- Profiles ------------------------------------------------------------

@app.post("/profiles/switch")
def switch_profile(request: Request, profile_id: str = Form(...)):
    profile = db.get_profile(profile_id) or current_profile(request)
    resp = RedirectResponse(url="/", status_code=303)
    _set_cookie(resp, COOKIE, profile["id"])
    return resp


@app.post("/profiles/create")
def create_profile(request: Request, name: str = Form(...)):
    new_id = db.create_profile(name)
    resp = RedirectResponse(url="/", status_code=303)
    _set_cookie(resp, COOKIE, new_id)
    return resp


@app.post("/profiles/delete")
def delete_profile(request: Request, profile_id: str = Form(...)):
    target = db.get_profile(profile_id)
    if target:
        db.delete_profile(target["id"])
    # Land on a profile that still exists.
    active = db.ensure_default_profile()
    resp = RedirectResponse(url="/", status_code=303)
    _set_cookie(resp, COOKIE, active)
    return resp


# --- Collection ----------------------------------------------------------

def _do_import(profile_id: int, text: str, filename: str):
    """Parse + store a ManaBox CSV. Blocking; run it in a threadpool."""
    rows, errors = manabox.parse_manabox_csv(text)
    db.replace_collection(profile_id, rows, source=f"ManaBox: {filename}")
    return rows, errors


@app.post("/import", response_class=HTMLResponse)
async def import_collection(request: Request, file: UploadFile = File(...)):
    profile = current_profile(request)
    content = await _read_upload(file)
    if content is None:
        return _render(
            request, profile, "index.html",
            _home_context(request, profile, import_failed=(
                f"Fichier trop volumineux (limite {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo) "
                "— un export ManaBox fait normalement quelques Mo au plus."
            )),
        )
    text = content.decode("utf-8-sig", errors="ignore")
    rows, errors = await run_in_threadpool(_do_import, profile["id"], text, file.filename)
    # Reload so collection_source / counts reflect the import.
    profile = db.get_profile(profile["id"])

    return _render(
        request,
        profile,
        "index.html",
        _home_context(
            request,
            profile,
            import_done=True,
            imported_rows=len(rows),
            import_errors=errors[:20],
            import_error_count=len(errors),
        ),
    )


@app.get("/collection", response_class=HTMLResponse)
def collection(request: Request):
    profile = current_profile(request)
    cards = collection_mod.enrich(profile["id"])
    ctx = _base_context(
        request,
        profile,
        cards=cards,
        distinct=len(cards),
        total=sum(c["qty"] for c in cards),
        total_value=collection_mod.total_value_eur(cards),
    )
    return _render(request, profile, "collection.html", ctx)


# --- Suggestions ---------------------------------------------------------

@app.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request, wish: str = Form(""), beyond: str = Form("")):
    profile = current_profile(request)
    parsed = await run_in_threadpool(intent.parse_intent, wish)

    # "Beyond the collection": theme-first commander search (EDHREC theme pages
    # + local Scryfall pool), independent of the cards the player owns — it
    # works even with an empty collection. 60-card formats have no commander,
    # so the option only applies to the Commander variants.
    beyond_collection = bool(beyond) and parsed.get("format") not in formats60.FORMATS
    if beyond_collection:
        data = await run_in_threadpool(analysis.find_commanders, parsed, profile["id"])
        ctx = _base_context(
            request, profile, wish=wish, intent=parsed, data=data, empty_collection=False
        )
        return _render(request, profile, "results.html", ctx)

    distinct, _total = db.collection_count(profile["id"])
    if distinct == 0:
        ctx = _base_context(
            request, profile, wish=wish, intent=parsed, data=None, empty_collection=True
        )
        return _render(request, profile, "results.html", ctx)

    # 60-card formats use the archetype-research pipeline; Commander uses EDHREC.
    if parsed.get("format") in formats60.FORMATS:
        data = await run_in_threadpool(formats60.analyze, parsed, profile["id"])
        ctx = _base_context(request, profile, wish=wish, intent=parsed, data=data)
        return _render(request, profile, "archetype.html", ctx)

    data = await run_in_threadpool(analysis.analyze, parsed, profile["id"])
    ctx = _base_context(
        request, profile, wish=wish, intent=parsed, data=data, empty_collection=False
    )
    return _render(request, profile, "results.html", ctx)


# --- Deck generation -----------------------------------------------------

def _parse_budget(raw: str) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@app.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    commander: str = Form(...),
    budget: str = Form(""),
    max_card_price: str = Form(""),
    theme: str = Form(""),
    format: str = Form("commander"),
):
    profile = current_profile(request)
    fmt = format if format in commanders.FORMATS else "commander"
    deck, _data = await run_in_threadpool(
        deckgen.generate_full_deck, commander, _parse_budget(budget), theme, profile["id"],
        _parse_budget(max_card_price), fmt,
    )
    ctx = _base_context(
        request, profile, commander=commander, budget=_parse_budget(budget), deck=deck
    )
    return _render(request, profile, "deck.html", ctx)


# --- Build a deck from an imported card list (Limited, Commander, 60-card) -

def _format_choices() -> list[dict]:
    return [
        {"value": f, "label": poolbuild.SPECS[f].label}
        for f in poolbuild.FORMAT_ORDER if f in poolbuild.SPECS
    ]


@app.get("/build", response_class=HTMLResponse)
def build_page(request: Request):
    profile = current_profile(request)
    distinct, _total = db.collection_count(profile["id"])
    ctx = _base_context(
        request, profile, llm_ok=llm.is_available(), llm_model=settings.anthropic_model,
        formats=_format_choices(), default_format=poolbuild.DEFAULT_FORMAT,
        has_collection=bool(distinct),
    )
    return _render(request, profile, "build.html", ctx)


@app.post("/build")
async def build_from_list(
    request: Request,
    pool: str = Form(""),
    format: str = Form(poolbuild.DEFAULT_FORMAT),
    colors: list[str] = Form([]),
    theme: str = Form(""),
    budget: str = Form(""),
    max_card_price: str = Form(""),
    file: UploadFile | None = File(None),
):
    profile = current_profile(request)

    text, filename = pool, ""
    if file is not None and file.filename:
        content = await _read_upload(file)
        # File over the cap: ignore it rather than crash — the pasted text (if
        # any) still goes through, and an empty pool yields a clear chat message.
        if content is not None:
            text = content.decode("utf-8-sig", errors="ignore")
            filename = file.filename

    fmt = format if format in poolbuild.SPECS else poolbuild.DEFAULT_FORMAT
    pool_items = poolbuild.parse_pool(text, filename)
    parsed = intent.coerce({
        "colors": colors,
        "theme": theme,
        "budget_eur": _parse_budget(budget),
        "max_card_price_eur": _parse_budget(max_card_price),
        "source": "build-form",
    })
    conv_id = await run_in_threadpool(
        chat.create_pool_conversation, profile["id"], pool_items, fmt, parsed
    )

    resp = RedirectResponse(url="/chat", status_code=303)
    _set_cookie(resp, COOKIE, profile["id"])
    _set_cookie(resp, CONV_COOKIE, conv_id)
    return resp


# --- Iterative chat (Phase 3) --------------------------------------------

def _active_conversation(request: Request, profile: dict) -> dict:
    """Resolve the active conversation for this profile, creating one if needed."""
    conv = db.get_conversation(request.cookies.get(CONV_COOKIE))
    # The cookie must point to a conversation owned by the active profile.
    if conv is None or conv["profile_id"] != profile["id"]:
        conv = None
        existing = db.list_conversations(profile["id"])
        if existing:
            conv = db.get_conversation(existing[0]["id"])
    if conv is None:
        conv = db.get_conversation(db.create_conversation(profile["id"]))
    return conv


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    profile = current_profile(request)
    conv = _active_conversation(request, profile)
    ctx = _base_context(
        request,
        profile,
        conversation=conv,
        conversations=db.list_conversations(profile["id"]),
        messages=db.get_messages(conv["id"]),
        pending=chat.is_pending(conv["id"]),
        llm_ok=llm.is_available(),
        llm_model=settings.anthropic_model,
    )
    resp = _render(request, profile, "chat.html", ctx)
    _set_cookie(resp, CONV_COOKIE, conv["id"])
    return resp


@app.post("/chat/message")
async def chat_message(
    request: Request, message: str = Form(""), conversation_id: str = Form("")
):
    profile = current_profile(request)
    conv = db.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile["id"]:
        conv = _active_conversation(request, profile)

    if message.strip():
        # Kick the turn off in the background and answer right away — a single
        # turn can outlast the Cloudflare edge timeout, so we never hold the
        # request open. The page polls /chat/status and shows a spinner.
        chat.start_turn(conv["id"], profile["id"], message)

    resp = RedirectResponse(url="/chat", status_code=303)
    _set_cookie(resp, CONV_COOKIE, conv["id"])
    return resp


@app.get("/chat/status")
def chat_status(request: Request, conversation_id: str = ""):
    """Lightweight poll target: is the conversation's background turn done?"""
    profile = current_profile(request)
    conv = db.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile["id"]:
        return JSONResponse({"pending": False})
    return JSONResponse({"pending": chat.is_pending(conv["id"])})


@app.post("/chat/new")
def chat_new(request: Request):
    profile = current_profile(request)
    new_id = db.create_conversation(profile["id"])
    resp = RedirectResponse(url="/chat", status_code=303)
    _set_cookie(resp, CONV_COOKIE, new_id)
    return resp


@app.post("/chat/{conversation_id}/delete")
def chat_delete(request: Request, conversation_id: int):
    profile = current_profile(request)
    conv = db.get_conversation(conversation_id)
    if conv and conv["profile_id"] == profile["id"]:
        db.delete_conversation(conversation_id)
    resp = RedirectResponse(url="/chat", status_code=303)
    resp.delete_cookie(CONV_COOKIE)
    return resp
