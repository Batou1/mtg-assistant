"""Presentation helpers for rendering free-form LLM text (game plans,
archetype strategy) in templates."""
import re

_MD_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")


def paragraphs(text: str | None) -> list[str]:
    """Split free-form LLM text into paragraphs for readable rendering — one
    <p> per paragraph instead of one wall of text with collapsed newlines.

    Also strips Markdown a model might add despite being asked for plain text
    (## headings, **bold**), since this is rendered as plain text, not through
    a Markdown renderer, so the raw syntax would otherwise show up literally.
    """
    if not text:
        return []
    cleaned = _MD_HEADING_RE.sub("", text)
    cleaned = _MD_BOLD_RE.sub(r"\1", cleaned)
    parts = re.split(r"\n\s*\n", cleaned.strip())
    return [" ".join(p.split()) for p in parts if p.strip()]
