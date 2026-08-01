"""Scryfall-style search syntax: parse a query string, match it against cards.

Powers the advanced filters of the "Ma collection" page. The free-text box
accepts the same formalism as scryfall.com (``t:creature mv<=3 -o:sacrifice
(c:r or c:g)``), and the structured filter controls (type, rarity, mana value,
oracle text…) are *compiled into that same syntax* — so there is a single
filter engine to reason about and the panel doubles as a way to learn the
query language.

Only the subset of Scryfall's operators that the local card cache can answer
is implemented (see ``_KEYS``). An unknown key raises ``QueryError`` instead of
silently matching everything, so a typo surfaces as a message rather than as a
wrong result set. Keys that only make sense server-side on Scryfall
(``lang:``, ``game:``, ``unique:``…) are accepted and ignored, so a query
pasted from scryfall.com still runs.

Collection-specific keys (``qty``, ``deck``, ``total``) read from the ``extra``
mapping the caller passes to ``Query.match`` — owned quantities are not part of
a Scryfall card object. ``extra`` also overrides the price and the set code
(``eur``, ``price``, ``is:priced``, ``s:``) when the caller knows which
printing is actually owned, so the query box filters on exactly the numbers the
collection page displays.
"""
import difflib
import operator
import re

from . import commanders, scryfall

_QUOTES = "\"'"

_CMP = {
    ":": operator.eq, "=": operator.eq, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}

# Comparable rarity ladder — lets `r>=rare` mean what it says on Scryfall.
_RARITY_ORDER = ["common", "uncommon", "rare", "special", "mythic", "bonus"]
_RARITY_ALIASES = {"c": "common", "u": "uncommon", "r": "rare", "m": "mythic",
                   "s": "special", "b": "bonus", "mythic rare": "mythic"}

_COLOR_LETTERS = {"w": "W", "u": "U", "b": "B", "r": "R", "g": "G"}
_COLOR_ALIASES = {
    "white": "W", "blue": "U", "black": "B", "red": "R", "green": "G",
    "colorless": "", "c": "",
    "azorius": "WU", "dimir": "UB", "rakdos": "BR", "gruul": "RG", "selesnya": "GW",
    "orzhov": "WB", "izzet": "UR", "golgari": "BG", "boros": "RW", "simic": "GU",
    "bant": "GWU", "esper": "WUB", "grixis": "UBR", "jund": "BRG", "naya": "RGW",
    "abzan": "WBG", "jeskai": "URW", "sultai": "BGU", "mardu": "RWB", "temur": "GUR",
    "chaos": "UBRG", "aggression": "WBRG", "altruism": "WURG", "growth": "WUBG",
    "artifice": "WUBR", "wubrg": "WUBRG", "rainbow": "WUBRG", "penta": "WUBRG",
}

_PERMANENT_TYPES = ("creature", "artifact", "enchantment", "land",
                    "planeswalker", "battle")

# Keys that don't change *which* cards match here: the local cache holds one
# canonical English paper printing per card, so a printing/locale selector from
# a pasted scryfall.com query is a no-op rather than an error. Anything that
# would genuinely narrow the result set is NOT in this list — it must either be
# implemented or rejected, never silently dropped.
_IGNORED_KEYS = {"unique", "lang", "language", "game", "prefer", "include",
                 "display"}
_ORDER_KEYS = {"order", "sort"}
_DIR_KEYS = {"direction", "dir"}

# Keys answerable without any Scryfall data — the card's name and what the
# collection itself knows (see ``_Term.match``).
_CARDLESS_KEYS = {"name", "n", "qty", "nb", "deck", "total"}
_CARDLESS_IS = {"indeck", "spare", "unresolved"}


class QueryError(ValueError):
    """A query the user typed cannot be understood (message is user-facing)."""


# --- AST -----------------------------------------------------------------

class _Node:
    def match(self, card: dict, extra: dict) -> bool:  # pragma: no cover
        raise NotImplementedError


class _All(_Node):
    def match(self, card, extra):
        return True


class _And(_Node):
    def __init__(self, parts):
        self.parts = parts

    def match(self, card, extra):
        return all(p.match(card, extra) for p in self.parts)


class _Or(_Node):
    def __init__(self, parts):
        self.parts = parts

    def match(self, card, extra):
        return any(p.match(card, extra) for p in self.parts)


class _Not(_Node):
    def __init__(self, node):
        self.node = node

    def match(self, card, extra):
        return not self.node.match(card, extra)


class _Term(_Node):
    """One ``key op value`` filter, with its handler already resolved."""

    def __init__(self, key, op, value, regex=None):
        self.key = key
        self.op = op
        self.value = value
        self.regex = regex
        self.handler = _KEYS[key]
        self.needs_card = (
            key not in _CARDLESS_KEYS
            and not (key in ("is", "not", "has") and value.lower() in _CARDLESS_IS)
        )

    def match(self, card, extra):
        # A card the local cache hasn't resolved yet carries nothing but its
        # name: any filter over card data must report "no" rather than fake an
        # answer from missing fields (an absent colour identity is a subset of
        # everything, which would make `id<=u` match every unresolved card).
        if self.needs_card and not extra.get("resolved", True):
            return False
        return self.handler(self, card, extra)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<{self.key}{self.op}{self.value}>"


# --- Card accessors ------------------------------------------------------

def _joined(card: dict, field: str) -> str:
    """A card's ``field``, joined across faces when there is no top-level value
    (double-faced / split / adventure cards)."""
    direct = card.get(field)
    if direct:
        return direct
    faces = card.get("card_faces") or []
    return " // ".join(f.get(field, "") for f in faces if f.get(field))


def _numbers(card: dict, field: str):
    """Every numeric value of ``field`` on the card, faces included.

    A card matches a numeric filter when ANY face does, so ``pow>=5`` finds a
    creature whose back face is the big one. ``*``-valued power/toughness is
    skipped rather than guessed at.
    """
    out = []
    for src in [card] + list(card.get("card_faces") or []):
        raw = src.get(field)
        if raw is None:
            continue
        try:
            out.append(float(str(raw).replace("+", "")))
        except ValueError:
            continue
    return out


def _colors(card: dict) -> set[str]:
    direct = card.get("colors")
    if direct is not None:
        return set(direct)
    out: set[str] = set()
    for face in card.get("card_faces") or []:
        out |= set(face.get("colors") or [])
    return out


def _price(card: dict, field: str):
    val = (card.get("prices") or {}).get(field)
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def _eur(card: dict, extra: dict):
    """The EUR price ``eur:``/``price:``/``is:priced`` filter on.

    ``extra["unit_price"]`` is what the collection page DISPLAYS for the row —
    the printing actually owned, foil copies at foil price, averaged when a
    name is owned in several editions. Filtering on the card object's own price
    instead would let the query box and the displayed price disagree.
    """
    val = extra.get("unit_price")
    return _price(card, "eur") if val is None else float(val)


# --- Value coercion (parse time, so errors point at the query) -----------

def _as_number(term: _Term) -> float:
    try:
        return float(term.value.replace(",", "."))
    except ValueError:
        raise QueryError(
            f"« {term.key}{term.op}{term.value} » attend un nombre."
        ) from None


def _as_colors(value: str) -> set[str] | None:
    """Parse a colour value to a WUBRG set, or None if it isn't one."""
    key = value.strip().lower()
    if key in _COLOR_ALIASES:
        return set(_COLOR_ALIASES[key])
    if key and all(ch in _COLOR_LETTERS or ch == "c" for ch in key):
        return {_COLOR_LETTERS[ch] for ch in key if ch in _COLOR_LETTERS}
    return None


# --- Handlers ------------------------------------------------------------

def _text_result(term: _Term, haystack: str) -> bool:
    """Shared text semantics: ``:`` substring, ``=`` whole value, ``/re/``."""
    if term.regex is not None:
        return term.regex.search(haystack) is not None
    needle = term.value.lower()
    hay = haystack.lower()
    if term.op in ("=", "!="):
        found = hay.strip() == needle
    elif term.op == ":":
        found = needle in hay
    else:
        raise QueryError(f"L'opérateur « {term.op} » ne s'applique pas à « {term.key} ».")
    return not found if term.op == "!=" else found


def _text_handler(field):
    def handle(term, card, extra):
        return _text_result(term, _joined(card, field))
    return handle


def _h_name(term, card, extra):
    return _text_result(term, card.get("name") or "")


def _h_oracle(term, card, extra):
    text = _joined(card, "oracle_text")
    # Scryfall's `~` stands for the card's own name inside its rules text, so
    # `o:"~ enters"` works across cards. Normalising the text (name -> ~) is
    # equivalent to expanding the needle, and costs one replace instead of a
    # rebuilt term.
    if term.regex is None and "~" in term.value:
        name = commanders.front_name(card) if card.get("name") else ""
        if name:
            text = text.replace(name, "~")
    return _text_result(term, text)


def _h_keyword(term, card, extra):
    kws = {k.lower() for k in (card.get("keywords") or [])}
    if term.regex is not None:
        return any(term.regex.search(k) for k in kws)
    found = term.value.lower() in kws
    return not found if term.op == "!=" else found


def _h_set(term, card, extra):
    # ``extra["set_code"]`` is the printing the collection page shows and
    # prices; without it, `s:tor` would miss a Torment copy whose canonical
    # Scryfall entry points at another set.
    code = (extra.get("set_code") or card.get("set") or "").lower()
    found = code == term.value.lower()
    return not found if term.op == "!=" else found


def _h_rarity(term, card, extra):
    want = _RARITY_ALIASES.get(term.value.lower(), term.value.lower())
    if want not in _RARITY_ORDER:
        raise QueryError(f"Rareté inconnue : « {term.value} ».")
    have = (card.get("rarity") or "").lower()
    if have not in _RARITY_ORDER:
        return False
    return _CMP[term.op](_RARITY_ORDER.index(have), _RARITY_ORDER.index(want))


def _color_result(have: set[str], want: set[str], op: str, subset_default: bool) -> bool:
    """Set semantics shared by ``c:`` and ``id:``.

    ``:`` means "at least these colours" for the card's own colours but
    "within these colours" for a colour identity (``subset_default``) — the
    same asymmetry Scryfall has, and the one that makes ``id:wu`` the natural
    way to ask "what fits my Azorius commander?".
    """
    if op == "=":
        return have == want
    if op == "!=":
        return have != want
    if op == ":":
        op = "<=" if subset_default else ">="
    if op == ">=":
        return want <= have
    if op == "<=":
        return have <= want
    if op == ">":
        return want < have
    return have < want


def _color_handler(getter, subset_default):
    def handle(term, card, extra):
        have = getter(card)
        value = term.value.lower()
        if value in ("m", "multi", "multicolor", "multicolored"):
            found = len(have) >= 2
            return not found if term.op == "!=" else found
        if value in ("mono", "monocolor", "monocolored"):
            found = len(have) == 1
            return not found if term.op == "!=" else found
        want = _as_colors(value)
        if want is None:
            try:
                count = float(value)
            except ValueError:
                raise QueryError(f"Couleur inconnue : « {term.value} ».") from None
            return _CMP[term.op](len(have), count)
        return _color_result(have, want, term.op, subset_default)
    return handle


def _number_handler(getter):
    def handle(term, card, extra):
        values = getter(card, extra)
        if not values:
            return False
        target = _as_number(term)
        cmp = _CMP[term.op]
        return any(cmp(v, target) for v in values)
    return handle


def _mana_symbols(cost: str) -> tuple[dict[str, int], int]:
    """(symbol counts, generic total) for a mana cost string like ``{2}{G}{G}``."""
    counts: dict[str, int] = {}
    generic = 0
    for sym in re.findall(r"\{([^}]*)\}", cost.upper()):
        if sym.isdigit():
            generic += int(sym)
        elif sym == "X":
            counts["X"] = counts.get("X", 0) + 1
        else:
            counts[sym] = counts.get(sym, 0) + 1
    return counts, generic


def _h_mana(term, card, extra):
    """``m:{G}{U}`` — the cost carries at least these symbols; ``m=`` exact."""
    value = term.value.upper()
    if "{" not in value:
        value = "".join(f"{{{ch}}}" for ch in value if not ch.isspace())
    want, want_generic = _mana_symbols(value)
    have, have_generic = _mana_symbols(scryfall.mana_cost(card))
    if term.op in ("=", "!="):
        found = have == want and have_generic == want_generic
        return not found if term.op == "!=" else found
    found = have_generic >= want_generic and all(
        have.get(sym, 0) >= n for sym, n in want.items()
    )
    return found


def _h_legal(term, card, extra):
    fmt = term.value.lower()
    status = (card.get("legalities") or {}).get(fmt)
    if term.key == "banned":
        return status == "banned"
    if term.key == "restricted":
        return status == "restricted"
    return status in ("legal", "restricted")


_IS_CHECKS = {
    "commander": lambda c, x: commanders.is_commander(c),
    "legendary": lambda c, x: "legendary" in _joined(c, "type_line").lower(),
    "permanent": lambda c, x: any(
        t in _joined(c, "type_line").lower() for t in _PERMANENT_TYPES
    ),
    "spell": lambda c, x: "land" not in _joined(c, "type_line").lower(),
    "vanilla": lambda c, x: not _joined(c, "oracle_text").strip(),
    "dfc": lambda c, x: bool(c.get("card_faces")),
    "split": lambda c, x: c.get("layout") == "split",
    "flip": lambda c, x: c.get("layout") == "flip",
    "transform": lambda c, x: c.get("layout") == "transform",
    "mdfc": lambda c, x: c.get("layout") == "modal_dfc",
    "adventure": lambda c, x: c.get("layout") == "adventure",
    "hybrid": lambda c, x: "/" in scryfall.mana_cost(c).replace("//", ""),
    "phyrexian": lambda c, x: "/P" in scryfall.mana_cost(c).upper(),
    "multicolor": lambda c, x: len(_colors(c)) >= 2,
    "colorless": lambda c, x: not _colors(c),
    "mono": lambda c, x: len(_colors(c)) == 1,
    "reserved": lambda c, x: bool(c.get("reserved")),
    "promo": lambda c, x: bool(c.get("promo")),
    "reprint": lambda c, x: bool(c.get("reprint")),
    "fullart": lambda c, x: bool(c.get("full_art")),
    "textless": lambda c, x: bool(c.get("textless")),
    "digital": lambda c, x: not scryfall.is_paper(c),
    "funny": lambda c, x: c.get("set_type") == "funny",
    "priced": lambda c, x: _eur(c, x) is not None,
    # Collection-side predicates (see app/collection.py for `extra`).
    "indeck": lambda c, x: x.get("deck_qty", 0) > 0,
    "spare": lambda c, x: x.get("qty", 0) - x.get("deck_qty", 0) > 0,
    "unresolved": lambda c, x: not x.get("resolved", True),
}


def _h_is(term, card, extra):
    value = term.value.lower()
    check = _IS_CHECKS.get(value)
    if check is None:
        raise QueryError(f"Critère « {term.key}:{term.value} » inconnu.")
    found = check(card, extra)
    return not found if term.key == "not" else found


def _h_year(term, card, extra):
    released = card.get("released_at") or ""
    if len(released) < 4 or not released[:4].isdigit():
        return False
    return _CMP[term.op](float(released[:4]), _as_number(term))


def _extra_number(field):
    return lambda card, extra: [float(extra.get(field, 0) or 0)]


# key -> handler. Every alias is listed explicitly so an unknown key is a
# genuine typo (Scryfall's own aliases included).
_KEYS = {
    "name": _h_name, "n": _h_name,
    "t": _text_handler("type_line"), "type": _text_handler("type_line"),
    "o": _h_oracle, "oracle": _h_oracle, "fo": _h_oracle, "text": _h_oracle,
    "ft": _text_handler("flavor_text"), "flavor": _text_handler("flavor_text"),
    "a": _text_handler("artist"), "artist": _text_handler("artist"),
    "layout": _text_handler("layout"),
    "border": _text_handler("border_color"),
    "frame": _text_handler("frame"),
    "watermark": _text_handler("watermark"),
    "st": _text_handler("set_type"), "settype": _text_handler("set_type"),
    "kw": _h_keyword, "keyword": _h_keyword,
    "s": _h_set, "set": _h_set, "e": _h_set, "edition": _h_set,
    "r": _h_rarity, "rarity": _h_rarity,
    "c": _color_handler(_colors, False),
    "color": _color_handler(_colors, False),
    "colors": _color_handler(_colors, False),
    "id": _color_handler(lambda c: set(c.get("color_identity") or []), True),
    "identity": _color_handler(lambda c: set(c.get("color_identity") or []), True),
    "ci": _color_handler(lambda c: set(c.get("color_identity") or []), True),
    "mv": _number_handler(lambda c, x: _numbers(c, "cmc")),
    "cmc": _number_handler(lambda c, x: _numbers(c, "cmc")),
    "manavalue": _number_handler(lambda c, x: _numbers(c, "cmc")),
    "pow": _number_handler(lambda c, x: _numbers(c, "power")),
    "power": _number_handler(lambda c, x: _numbers(c, "power")),
    "tou": _number_handler(lambda c, x: _numbers(c, "toughness")),
    "toughness": _number_handler(lambda c, x: _numbers(c, "toughness")),
    "loy": _number_handler(lambda c, x: _numbers(c, "loyalty")),
    "loyalty": _number_handler(lambda c, x: _numbers(c, "loyalty")),
    "eur": _number_handler(lambda c, x: [p for p in [_eur(c, x)] if p is not None]),
    "price": _number_handler(lambda c, x: [p for p in [_eur(c, x)] if p is not None]),
    "usd": _number_handler(lambda c, x: [p for p in [_price(c, "usd")] if p is not None]),
    "m": _h_mana, "mana": _h_mana,
    "f": _h_legal, "format": _h_legal, "legal": _h_legal,
    "banned": _h_legal, "restricted": _h_legal,
    "is": _h_is, "not": _h_is, "has": _h_is,
    "year": _h_year,
    # Collection-specific.
    "qty": _number_handler(_extra_number("qty")),
    "nb": _number_handler(_extra_number("qty")),
    "deck": _number_handler(_extra_number("deck_qty")),
    "total": _number_handler(_extra_number("line_total")),
}

#: Human-readable list of supported keys, for the UI's syntax help.
SUPPORTED_KEYS = sorted(_KEYS)


# --- Tokenizer / parser --------------------------------------------------

def _read_raw(text: str, i: int) -> tuple[str, int]:
    """Read one whitespace-delimited chunk, treating quoted and ``/regex/``
    spans as opaque so ``o:"draw a card"`` stays one token."""
    start = i
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            i += 1
            while i < n and text[i] != ch:
                i += 1
            i = min(i + 1, n)
            continue
        if ch == "/" and (i == start or text[i - 1] in ":=<>!"):
            i += 1
            while i < n and text[i] != "/":
                i += 1
            i = min(i + 1, n)
            continue
        if ch.isspace() or ch in "()":
            break
        i += 1
    return text[start:i], i


def _tokenize(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            toks.append((ch, ""))
            i += 1
            continue
        raw, nxt = _read_raw(text, i)
        i = nxt if nxt > i else i + 1
        if not raw:
            continue
        upper = raw.upper()
        if upper in ("OR", "AND"):
            toks.append((upper, ""))
        elif raw == "-":
            toks.append(("NOT", ""))
        else:
            toks.append(("TERM", raw))
    return toks


_TERM_RE = re.compile(r"^([A-Za-z]+)(!=|>=|<=|[:=<>])(.*)$", re.S)


def _unquote(value: str) -> tuple[str, bool]:
    val = value.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in _QUOTES:
        return val[1:-1], False
    if len(val) >= 2 and val[0] == "/" and val[-1] == "/":
        return val[1:-1], True
    return val, False


def _build_term(raw: str, options: dict) -> _Node:
    negated = False
    while raw[:1] == "-":
        negated = not negated
        raw = raw[1:]
    if not raw:
        raise QueryError("Un « - » isolé n'est pas un filtre.")

    exact = raw[0] == "!"
    if exact:
        raw = raw[1:]

    match = _TERM_RE.match(raw)
    if match:
        key, op, value = match.group(1).lower(), match.group(2), match.group(3)
    else:
        key, op, value = "name", ("=" if exact else ":"), raw

    value, is_regex = _unquote(value)
    if key in _ORDER_KEYS:
        options["order"] = value.lower()
        return _All()
    if key in _DIR_KEYS:
        options["direction"] = value.lower()
        return _All()
    if key in _IGNORED_KEYS:
        return _All()
    if key not in _KEYS:
        close = difflib.get_close_matches(key, SUPPORTED_KEYS, n=1, cutoff=0.6)
        hint = f" Vouliez-vous dire « {close[0]}: » ?" if close else ""
        raise QueryError(
            f"Filtre inconnu : « {key} ».{hint} Voir l'aide sur la syntaxe."
        )
    if not value:
        raise QueryError(f"Le filtre « {key}{op} » attend une valeur.")

    regex = None
    if is_regex:
        try:
            regex = re.compile(value, re.I)
        except re.error as exc:
            raise QueryError(f"Expression régulière invalide : {exc}") from None

    term = _Term(key, op, value, regex)
    _validate(term)
    return _Not(term) if negated else term


def _validate(term: _Term) -> None:
    """Reject at parse time what would otherwise fail on every single card."""
    if term.op not in _CMP:
        raise QueryError(f"Opérateur inconnu : « {term.op} ».")
    # Numeric filters coerce their value on every card; do it once here so a
    # bad value names the offending filter instead of matching nothing.
    if term.key in ("mv", "cmc", "manavalue", "pow", "power", "tou", "toughness",
                    "loy", "loyalty", "eur", "usd", "price", "year",
                    "qty", "nb", "deck", "total"):
        _as_number(term)
    if term.key in ("r", "rarity"):
        want = _RARITY_ALIASES.get(term.value.lower(), term.value.lower())
        if want not in _RARITY_ORDER:
            raise QueryError(f"Rareté inconnue : « {term.value} ».")
    if term.key in ("is", "not", "has") and term.value.lower() not in _IS_CHECKS:
        raise QueryError(
            f"Critère « {term.key}:{term.value} » inconnu. Exemples : "
            "is:commander, is:legendary, is:indeck, is:spare."
        )


class _Parser:
    def __init__(self, toks, options):
        self.toks = toks
        self.options = options
        self.pos = 0

    def peek(self):
        return self.toks[self.pos][0] if self.pos < len(self.toks) else None

    def take(self):
        if self.pos >= len(self.toks):
            raise QueryError("Requête incomplète.")
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> _Node:
        node = self.parse_or()
        if self.pos < len(self.toks):
            raise QueryError("Parenthèse fermante en trop.")
        return node

    def parse_or(self) -> _Node:
        parts = [self.parse_and()]
        while self.peek() == "OR":
            self.take()
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else _Or(parts)

    def parse_and(self) -> _Node:
        parts: list[_Node] = []
        while True:
            kind = self.peek()
            if kind == "AND":
                self.take()
                continue
            if kind in ("TERM", "NOT", "("):
                parts.append(self.parse_unary())
                continue
            break
        if not parts:
            raise QueryError("Requête incomplète.")
        return parts[0] if len(parts) == 1 else _And(parts)

    def parse_unary(self) -> _Node:
        kind, raw = self.take()
        if kind == "NOT":
            return _Not(self.parse_unary())
        if kind == "(":
            node = self.parse_or()
            if self.peek() != ")":
                raise QueryError("Parenthèse non fermée.")
            self.take()
            return node
        return _build_term(raw, self.options)


class Query:
    """A parsed query. ``order``/``direction`` carry an inline ``order:`` term."""

    def __init__(self, node: _Node, options: dict | None = None):
        self._node = node
        options = options or {}
        self.order = options.get("order")
        self.direction = options.get("direction")

    def match(self, card: dict | None, extra: dict | None = None) -> bool:
        return self._node.match(card or {}, extra or {})

    def __call__(self, card, extra=None):
        return self.match(card, extra)


def parse(text: str) -> Query:
    """Parse ``text`` into a :class:`Query`. Raises :class:`QueryError`.

    An empty query matches everything — the collection page's default view.
    """
    toks = _tokenize(text or "")
    if not toks:
        return Query(_All())
    options: dict = {}
    node = _Parser(toks, options).parse()
    return Query(node, options)


def needs_grouping(text: str) -> bool:
    """True if ANDing extra terms onto ``text`` requires parenthesising it.

    Only a top-level ``or`` changes meaning when terms are appended (``a or b``
    plus ``c`` would bind ``c`` to ``b`` alone). Everything else is already a
    conjunction, so it is left alone and a query built up filter by filter stays
    readable instead of accumulating nested parentheses.
    """
    depth = 0
    for kind, _raw in _tokenize(text or ""):
        if kind == "(":
            depth += 1
        elif kind == ")":
            depth = max(0, depth - 1)
        elif kind == "OR" and depth == 0:
            return True
    return False


def quote(value: str) -> str:
    """Quote ``value`` for embedding in a query, if it needs it.

    Used by the structured filter panel, which composes its controls into a
    query string rather than filtering through a second code path.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if any(ch.isspace() or ch in "()\"'" for ch in value):
        return '"' + value.replace('"', "") + '"'
    return value
