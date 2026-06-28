"""FastAPI app: pick a profile, import its ManaBox collection, describe a deck
wish in French, and get Commander suggestions with gap analysis and a
budget-constrained buylist. Profiles are lightweight — no accounts/passwords;
the active profile is remembered in a cookie.
"""
import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import analysis, db, intent, llm, manabox
from .config import settings

app = FastAPI(title="MTG Assistant")

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

db.init_db()

COOKIE = "profile_id"
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
        ollama_ok=llm.is_available(),
        ollama_model=settings.ollama_model,
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

    data = await run_in_threadpool(analysis.analyze, parsed, profile["id"])
    ctx = _base_context(
        request, profile, wish=wish, intent=parsed, data=data, empty_collection=False
    )
    return _render(request, profile, "results.html", ctx)
