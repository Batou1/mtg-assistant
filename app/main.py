"""FastAPI app: import a ManaBox collection, describe a deck wish in French,
get Commander suggestions from your collection with gap analysis and a
budget-constrained buylist.
"""
import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
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

COLOR_NAMES = {"W": "Blanc", "U": "Bleu", "B": "Noir", "R": "Rouge", "G": "Vert"}
templates.env.globals["COLOR_NAMES"] = COLOR_NAMES


def _home_context(request: Request, **extra):
    distinct, total = db.collection_count()
    ctx = {
        "distinct": distinct,
        "total": total,
        "source": db.get_meta("collection_source"),
        "ollama_ok": llm.is_available(),
        "ollama_model": settings.ollama_model,
    }
    ctx.update(extra)
    return ctx


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _home_context(request))


@app.post("/import", response_class=HTMLResponse)
async def import_collection(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8-sig", errors="ignore")
    rows, errors = manabox.parse_manabox_csv(text)
    db.replace_collection(rows, source=f"ManaBox: {file.filename}")

    distinct, total = db.collection_count()
    return templates.TemplateResponse(
        request,
        "index.html",
        _home_context(
            request,
            import_done=True,
            imported_rows=len(rows),
            import_errors=errors[:20],
            import_error_count=len(errors),
        ),
    )


@app.get("/collection", response_class=HTMLResponse)
def collection(request: Request):
    cards = db.collection_names()
    return templates.TemplateResponse(
        request,
        "collection.html",
        {"cards": cards, "distinct": len(cards), "total": sum(q for _, _, q in cards)},
    )


@app.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request, wish: str = Form("")):
    parsed = await run_in_threadpool(intent.parse_intent, wish)
    distinct, _total = db.collection_count()
    if distinct == 0:
        return templates.TemplateResponse(
            request,
            "results.html",
            {"wish": wish, "intent": parsed, "data": None, "empty_collection": True},
        )

    data = await run_in_threadpool(analysis.analyze, parsed)
    return templates.TemplateResponse(
        request,
        "results.html",
        {"wish": wish, "intent": parsed, "data": data, "empty_collection": False},
    )
