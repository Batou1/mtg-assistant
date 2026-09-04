"""Magic Comprehensive Rules: download, parse, store, search, keep current.

The rules tab answers questions by *citing* the official Comprehensive Rules
(CR), so the app needs the document itself, not the model's memory of it.
Wizards publishes it on https://magic.wizards.com/en/rules in three formats;
the ``.txt`` one is the machine-readable source: one numbered rule per line
(``100.1.``, ``100.1a``), ``Example:`` lines attached to the rule above, then a
glossary. We parse it into the ``rules`` / ``rules_glossary`` tables and keep
an in-memory index for keyword search (the corpus is ~1 MB: no FTS needed).

Freshness: the document changes with most set releases, and an answer citing
a superseded rule is worse than no answer. The scheduler re-reads the rules
page every ``rules_refresh_days`` (monthly by default) and re-imports when the
linked file changed; the page shows the effective date and a manual refresh
button. Without network (or before the first import) the tab still renders,
with an explicit "rules not loaded" notice — the app never blocks on this.
"""
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime

import httpx

from . import db
from .config import settings

logger = logging.getLogger(__name__)

# Rule identifiers as they appear in the document and in answers.
_RE_SECTION = re.compile(r"^(\d)\.\s+(\S.*)$")
_RE_CHAPTER = re.compile(r"^(\d{3})\.\s+(\S.*)$")
_RE_RULE = re.compile(r"^(\d{3}\.\d+)\.\s+(.*)$")
_RE_SUBRULE = re.compile(r"^(\d{3}\.\d+[a-z])\s+(.*)$")
_RE_EXAMPLE = re.compile(r"^Example:\s*(.*)$")
_RE_EFFECTIVE = re.compile(r"effective as of ([A-Za-z]+ \d{1,2}, \d{4})")
# The TXT link on the rules page: the file name carries a space ("MagicCompRules
# 20260819.txt"), sometimes encoded, sometimes not.
_RE_TXT_URL = re.compile(
    r"https?://media\.wizards\.com/[^\"'<>]*?MagicCompRules[^\"'<>]*?\.txt", re.IGNORECASE
)
# A rule number inside free text: "702.19b", "702.19", optionally prefixed.
NUMBER_RE = re.compile(r"\b(\d{3}\.\d{1,3}[a-z]?)\b")
# "rule 702" but not the "702" of "rule 702.19c" (a rule citation, above).
CHAPTER_REF_RE = re.compile(r"\b(?:rule|règle|section|chapitre)\s+(\d{3})(?!\d)(?!\.\d)", re.IGNORECASE)

_STOPWORDS = {
    # English
    "the", "and", "for", "that", "this", "with", "from", "are", "you", "your",
    "its", "has", "have", "can", "not", "may", "any", "all", "one", "does", "what",
    "when", "how", "which", "into", "onto", "than", "then", "there", "their", "they",
    "them", "been", "being", "will", "would", "about", "each", "other", "some",
    "rule", "rules", "magic", "card", "cards",
    # French (questions arrive in French; the model is told to search in
    # English, but the key-free fallback searches the raw question)
    "les", "des", "une", "est", "que", "qui", "quoi", "pour", "avec", "dans",
    "sur", "pas", "peut", "peux", "mon", "mes", "ses", "son", "cette", "ces",
    "comment", "quand", "elle", "ils", "elles", "nous", "vous", "mais", "donc",
    "carte", "cartes", "règle", "règles", "regle", "regles", "moi", "lui",
}
_META_SOURCE = "rules_source_url"
_META_EFFECTIVE = "rules_effective_date"
_META_SYNCED = "rules_synced_at"
_META_CHECKED = "rules_checked_at"
_POLL_INTERVAL_SECONDS = 3600

_MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre",
]


# --- Parsing -------------------------------------------------------------

def parse(text: str) -> dict:
    """Parse the CR ``.txt`` into rules + glossary (document order).

    Returns ``{"effective": str|None, "rules": [(number, kind, chapter, text,
    examples)], "glossary": [(term, definition)]}``. ``kind`` is ``section``
    (``1``), ``chapter`` (``100``) or ``rule`` (``100.1``, ``100.1a``);
    ``chapter`` is the title of the enclosing chapter ("Keyword Abilities").

    The document opens with a table of contents that repeats every section and
    chapter heading: those are merged by number, and the glossary only starts
    at the "Glossary" heading that FOLLOWS the numbered rules (the TOC has one
    too). Wrapped lines (rare) are folded into the rule above them.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    m = _RE_EFFECTIVE.search(text)
    effective = _iso_date(m.group(1)) if m else None

    rules: list[list] = []          # [number, kind, chapter, text, examples]
    seen: dict[str, list] = {}
    glossary: list[tuple[str, str]] = []
    chapter_title = ""
    current: list | None = None      # last rule row, for examples/continuations
    saw_rule = False
    in_glossary = False
    entry: list[str] = []

    def flush_entry():
        if len(entry) >= 2:
            glossary.append((entry[0].strip(), "\n".join(entry[1:]).strip()))
        elif len(entry) == 1 and glossary:
            # A lone line inside the glossary: continuation of the previous
            # definition (a wrapped line), keep the corpus intact.
            term, definition = glossary[-1]
            glossary[-1] = (term, f"{definition}\n{entry[0].strip()}".strip())
        entry.clear()

    for raw in text.split("\n"):
        line = raw.strip()
        if in_glossary:
            if line == "Credits":
                flush_entry()
                break
            if not line:
                flush_entry()
            else:
                entry.append(line)
            continue
        if not line:
            continue
        if line == "Glossary" and saw_rule:
            in_glossary = True
            current = None
            continue

        m = _RE_SUBRULE.match(line) or _RE_RULE.match(line)
        if m:
            number, body = m.group(1), m.group(2).strip()
            row = [number, "rule", chapter_title, body, ""]
            if number in seen:            # duplicated line: keep the first
                current = seen[number]
                continue
            seen[number] = row
            rules.append(row)
            current = row
            saw_rule = True
            continue
        m = _RE_CHAPTER.match(line)
        if m:
            number, title = m.group(1), m.group(2).strip()
            chapter_title = title
            if number not in seen:
                row = [number, "chapter", title, title, ""]
                seen[number] = row
                rules.append(row)
            current = None
            continue
        m = _RE_SECTION.match(line)
        if m:
            number, title = m.group(1), m.group(2).strip()
            if number not in seen:
                row = [number, "section", title, title, ""]
                seen[number] = row
                rules.append(row)
            current = None
            continue
        m = _RE_EXAMPLE.match(line)
        if m and current is not None:
            current[4] = (current[4] + "\n" + m.group(1).strip()).strip()
            continue
        if current is not None and current[1] == "rule":
            # Wrapped continuation of the rule (or of its last example).
            if current[4]:
                current[4] = current[4] + " " + line
            else:
                current[3] = current[3] + " " + line

    return {
        "effective": effective,
        "rules": [tuple(r) for r in rules],
        "glossary": glossary,
    }


def _iso_date(us_date: str) -> str | None:
    """'August 7, 2026' -> '2026-08-07' (the document is always in English)."""
    try:
        return datetime.strptime(us_date, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def format_date_fr(iso: str | None) -> str:
    """'2026-08-07' -> '7 août 2026' for the UI (falls back to the input)."""
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {_MONTHS_FR[d.month - 1]} {d.year}"


# --- In-memory index -----------------------------------------------------
# Loaded lazily from SQLite and dropped whenever the corpus is replaced. Every
# rule carries a token bag (its own text, weighted with the words of its parent
# rule and chapter) so "trample damage" ranks 702.19b — whose parent is the
# heading "Trample" — above rules that merely mention damage.

class _Index:
    def __init__(self, rows: list[dict], glossary: list[dict]):
        self.rows = rows
        self.by_number: dict[str, dict] = {r["number"]: r for r in rows}
        self.glossary = glossary
        self.tokens: dict[str, Counter] = {}
        self.folded: dict[str, str] = {}
        for r in rows:
            if r["kind"] != "rule":
                continue
            bag = Counter(tokenize(r["text"]))
            for tok in tokenize(r["examples"]):
                bag[tok] += 1
            parent = self.by_number.get(parent_number(r["number"]))
            if parent is not None and parent["kind"] == "rule":
                for tok in set(tokenize(parent["text"][:120])):
                    bag[tok] += 2
            for tok in set(tokenize(r["chapter"])):
                bag[tok] += 1
            self.tokens[r["number"]] = bag
            self.folded[r["number"]] = fold(r["text"])


_index: _Index | None = None


def _get_index() -> _Index:
    global _index
    if _index is None:
        _index = _Index(db.all_rules(), db.all_glossary())
    return _index


def invalidate() -> None:
    global _index
    _index = None


def fold(text: str) -> str:
    """Lowercase ASCII (accents stripped, curly quotes flattened)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("’", "'").lower()


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def tokenize(text: str) -> list[str]:
    out = []
    for tok in _TOKEN_RE.findall(fold(text)):
        tok = tok.split("'")[0]
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        out.append(_stem(tok))
    return out


def _stem(tok: str) -> str:
    """Crude English stemmer: enough to match "blocking"/"blocked"/"blocks"."""
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(tok) - len(suffix) >= 4 and tok.endswith(suffix):
            base = tok[: -len(suffix)]
            return base + "y" if suffix == "ies" else base
    return tok


def normalize_number(raw: str) -> str:
    """'CR 702.19B.' / 'rule 702.19b' / '702.19b' -> '702.19b'."""
    s = (raw or "").strip().lower()
    s = re.sub(r"^(?:cr|rule|règle|regle|section|chapitre)\s*", "", s)
    s = s.strip(" .")
    return s


def parent_number(number: str) -> str | None:
    """702.19b -> 702.19 ; 702.19 -> 702 ; 702 -> 7 ; 7 -> None."""
    if re.fullmatch(r"\d{3}\.\d+[a-z]", number):
        return number[:-1]
    if re.fullmatch(r"\d{3}\.\d+", number):
        return number.split(".")[0]
    if re.fullmatch(r"\d{3}", number):
        return number[0]
    return None


def is_loaded() -> bool:
    return bool(_get_index().rows)


def exists(number: str) -> bool:
    return normalize_number(number) in _get_index().by_number


def get_rule(number: str) -> dict | None:
    """A rule with its context: chapter, parent rule, direct children.

    For a chapter number ("702") the children are its main rules (not the
    subrules), so the drawer can list a chapter without drowning the reader.
    """
    idx = _get_index()
    num = normalize_number(number)
    row = idx.by_number.get(num)
    if row is None:
        return None
    parent = idx.by_number.get(parent_number(num) or "")
    chapter = idx.by_number.get(num[:3]) if len(num) >= 3 else None
    children = []
    for r in idx.rows:
        if r["number"] == num:
            continue
        if parent_number(r["number"]) == num:
            children.append(r)
    return {
        "rule": row,
        "parent": parent,
        "chapter": chapter if chapter is not row else None,
        "children": children,
    }


def search(query: str, limit: int | None = None) -> list[dict]:
    """Keyword search over the numbered rules, best matches first.

    A query that *is* a rule number (or a prefix like "702.19") returns that
    rule and its subrules directly. Otherwise rules are scored by the query
    tokens they contain (prefix matches count for long tokens) with a bonus
    when the whole phrase appears verbatim.
    """
    idx = _get_index()
    limit = limit or settings.rules_search_limit
    q = (query or "").strip()
    if not q:
        return []
    num = normalize_number(q)
    if re.fullmatch(r"\d{3}(?:\.\d+[a-z]?)?", num) and num in idx.by_number:
        out = [idx.by_number[num]] + [
            r for r in idx.rows if r["number"] != num and r["number"].startswith(num)
            and r["kind"] == "rule"
        ]
        return [dict(r, score=None) for r in out[:limit]]

    qtokens = list(dict.fromkeys(tokenize(q)))
    phrase = fold(q)
    if not qtokens:
        return []
    scored: list[tuple[float, int, dict]] = []
    for pos, r in enumerate(idx.rows):
        bag = idx.tokens.get(r["number"])
        if not bag:
            continue
        score = 0.0
        matched = 0
        for qt in qtokens:
            hit = bag.get(qt, 0)
            if not hit and len(qt) >= 5:
                hit = sum(c for t, c in bag.items() if t.startswith(qt) or qt.startswith(t) and len(t) >= 5)
            if hit:
                matched += 1
                score += 1.0 + min(hit, 4) * 0.25
        if not matched:
            continue
        # Rules matching every query word beat rules matching one word a lot.
        score += matched * matched
        if len(qtokens) > 1 and phrase in idx.folded.get(r["number"], ""):
            score += 5
        # Slight preference for short, definitional rules over long lists.
        score -= min(len(r["text"]), 1200) / 2400
        scored.append((score, pos, r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [dict(r, score=round(s, 2)) for s, _p, r in scored[:limit]]


def lookup_glossary(term: str, limit: int = 6) -> list[dict]:
    """Glossary entries whose term contains (or starts with) the query."""
    idx = _get_index()
    q = fold((term or "").strip())
    if not q:
        return []
    exact = [g for g in idx.glossary if fold(g["term"]) == q]
    starts = [g for g in idx.glossary if fold(g["term"]).startswith(q) and g not in exact]
    contains = [g for g in idx.glossary if q in fold(g["term"])
                and g not in exact and g not in starts]
    return (exact + starts + contains)[:limit]


# --- Storage / refresh ---------------------------------------------------

def store(parsed: dict, source: str) -> int:
    """Persist a parsed document and stamp its provenance. Returns rule count."""
    db.replace_rules(parsed["rules"], parsed["glossary"])
    now = str(time.time())
    db.set_meta(_META_SOURCE, source)
    db.set_meta(_META_EFFECTIVE, parsed.get("effective") or "")
    db.set_meta(_META_SYNCED, now)
    db.set_meta(_META_CHECKED, now)
    invalidate()
    count = sum(1 for r in parsed["rules"] if r[1] == "rule")
    logger.info("rules: imported %d rules from %s (effective %s)",
                count, source, parsed.get("effective"))
    return count


def find_txt_url(html: str) -> str | None:
    """The current CR ``.txt`` link on the rules page, URL-encoded."""
    m = _RE_TXT_URL.search(html or "")
    if not m:
        return None
    return m.group(0).replace(" ", "%20")


def refresh(force: bool = False) -> dict:
    """Check the rules page and import the linked document if it changed.

    Returns ``{"status": "current"|"updated"|"error", ...}``. Errors (no
    network, page layout changed) are reported, not raised, and leave the
    stored corpus untouched — the scheduler retries on its next pass.
    """
    try:
        with httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=30,
                          follow_redirects=True) as client:
            page = client.get(settings.rules_page_url)
            page.raise_for_status()
            url = find_txt_url(page.text)
            if not url:
                logger.warning("rules: no .txt link found on %s", settings.rules_page_url)
                return {"status": "error", "error": "lien .txt introuvable sur la page des règles"}
            if not force and url == db.get_meta(_META_SOURCE) and db.rules_count():
                db.set_meta(_META_CHECKED, str(time.time()))
                return {"status": "current", "source": url}
            doc = client.get(url)
            doc.raise_for_status()
            text = doc.content.decode("utf-8-sig", errors="replace")
    except httpx.HTTPError as exc:
        logger.warning("rules: refresh failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    parsed = parse(text)
    if not parsed["rules"]:
        return {"status": "error", "error": "document illisible (aucune règle trouvée)"}
    count = store(parsed, url)
    return {"status": "updated", "source": url, "rules": count,
            "effective": parsed.get("effective")}


def import_file(path: str) -> dict:
    """Import a locally downloaded CR ``.txt`` (offline bootstrap)."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8-sig", errors="replace")
    parsed = parse(text)
    if not parsed["rules"]:
        return {"status": "error", "error": "document illisible (aucune règle trouvée)"}
    count = store(parsed, f"file:{os.path.basename(path)}")
    return {"status": "updated", "source": path, "rules": count,
            "effective": parsed.get("effective")}


def status() -> dict:
    """What the UI shows: effective date, last check, staleness."""
    count = db.rules_count()
    checked = db.get_meta(_META_CHECKED)
    synced = db.get_meta(_META_SYNCED)
    checked_at = float(checked) if checked else None
    effective = db.get_meta(_META_EFFECTIVE) or None
    return {
        "loaded": count > 0,
        "count": count,
        "effective": effective,
        "effective_fr": format_date_fr(effective),
        "source": db.get_meta(_META_SOURCE),
        "synced_at": float(synced) if synced else None,
        "checked_at": checked_at,
        "checked_days_ago": (
            int((time.time() - checked_at) // 86400) if checked_at else None
        ),
        "stale": needs_check(),
    }


def needs_check() -> bool:
    """No corpus yet, or the last look at the rules page is older than the
    refresh window."""
    if not db.rules_count():
        return True
    checked = db.get_meta(_META_CHECKED)
    if not checked:
        return True
    return (time.time() - float(checked)) >= settings.rules_refresh_days * 86400


def run_scheduler_loop() -> None:
    """Blocking loop for a daemon thread: check when due, then poll hourly."""
    while True:
        try:
            if needs_check():
                refresh()
        except Exception:
            logger.exception("rules: scheduled refresh failed")
        time.sleep(_POLL_INTERVAL_SECONDS)


def start_background_refresh() -> None:
    if not (settings.bulk_auto_refresh and settings.rules_auto_refresh):
        return
    import threading

    threading.Thread(target=run_scheduler_loop, daemon=True, name="rules-refresh").start()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db.init_db()
    if len(sys.argv) > 1:
        print(import_file(sys.argv[1]))
    else:
        print(refresh(force=True))
