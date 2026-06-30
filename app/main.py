"""FastAPI app: pick a profile, import its ManaBox collection, describe a deck
wish in French, and get Commander suggestions with gap analysis and a
budget-constrained buylist. Profiles are lightweight — no accounts/passwords;
the active profile is remembered in a cookie.
"""
import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import analysis, chat, db, deckgen, formats60, intent, llm, manabox, poolbuild
from .config import settings

app = FastAPI(title="MTG Assistant")

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

db.init_db()

COOKIE = "profile_id"
CONV_COOKIE = "conversation_id"
COLOR_NAMES = {"W": "Blanc", "U": "Bleu", "B": "Noir", "R": "Rouge", "G": "Vert"}
templates.env.globals["COLOR_NAMES"] = COLOR_NAMES


def current_profile(request: Request) -> dict:
    """Resolve the active profile from the cookie, falling back to a default."""
    profile = db.get_profile(request.cookies.get(COOKIE))
    if profile is None:
        profile = db.get_profile(db.ensure_default_profile())
    return profile


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
    resp.set_cookie(COOKIE, str(profile["id"]), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


def _home_context(request: Request, profile: dict, **extra) -> dict:
    distinct, total = db.collection_count(profile["id"])
    return _base_context(
        request,
        profile,
        distinct=distinct,
        total=total,
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
    resp.set_cookie(COOKIE, str(profile["id"]), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/profiles/create")
def create_profile(request: Request, name: str = Form(...)):
    new_id = db.create_profile(name)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(COOKIE, str(new_id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/profiles/delete")
def delete_profile(request: Request, profile_id: str = Form(...)):
    target = db.get_profile(profile_id)
    if target:
        db.delete_profile(target["id"])
    # Land on a profile that still exists.
    active = db.ensure_default_profile()
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(COOKIE, str(active), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# --- Collection ----------------------------------------------------------

@app.post("/import", response_class=HTMLResponse)
async def import_collection(request: Request, file: UploadFile = File(...)):
    profile = current_profile(request)
    content = await file.read()
    text = content.decode("utf-8-sig", errors="ignore")
    rows, errors = manabox.parse_manabox_csv(text)
    db.replace_collection(profile["id"], rows, source=f"ManaBox: {file.filename}")
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
    cards = db.collection_names(profile["id"])
    ctx = _base_context(
        request,
        profile,
        cards=cards,
        distinct=len(cards),
        total=sum(q for _, _, q in cards),
    )
    return _render(request, profile, "collection.html", ctx)


# --- Suggestions ---------------------------------------------------------

@app.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request, wish: str = Form("")):
    profile = current_profile(request)
    parsed = await run_in_threadpool(intent.parse_intent, wish)
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
    theme: str = Form(""),
):
    profile = current_profile(request)
    deck, _data = await run_in_threadpool(
        deckgen.generate_full_deck, commander, _parse_budget(budget), theme, profile["id"]
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
    file: UploadFile | None = File(None),
):
    profile = current_profile(request)

    text, filename = pool, ""
    if file is not None and file.filename:
        content = await file.read()
        text = content.decode("utf-8-sig", errors="ignore")
        filename = file.filename

    fmt = format if format in poolbuild.SPECS else poolbuild.DEFAULT_FORMAT
    pool_items = poolbuild.parse_pool(text, filename)
    parsed = intent._coerce({
        "colors": colors,
        "theme": theme,
        "budget_eur": _parse_budget(budget),
        "source": "build-form",
    })
    conv_id = await run_in_threadpool(
        chat.create_pool_conversation, profile["id"], pool_items, fmt, parsed
    )

    resp = RedirectResponse(url="/chat", status_code=303)
    resp.set_cookie(COOKIE, str(profile["id"]), max_age=60 * 60 * 24 * 365, samesite="lax")
    resp.set_cookie(CONV_COOKIE, str(conv_id), max_age=60 * 60 * 24 * 365, samesite="lax")
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
    resp.set_cookie(CONV_COOKIE, str(conv["id"]), max_age=60 * 60 * 24 * 365, samesite="lax")
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
    resp.set_cookie(CONV_COOKIE, str(conv["id"]), max_age=60 * 60 * 24 * 365, samesite="lax")
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
    resp.set_cookie(CONV_COOKIE, str(new_id), max_age=60 * 60 * 24 * 365, samesite="lax")
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
