"""Polite GET of HTML/text pages for the scraped Commander-variant sources.

PDHREC (Pauper Commander) and MTGTop8 (Duel Commander) have no JSON API: their
pages are server-rendered HTML (MTGTop8's deck export is plain text). This is
the shared fetch: browser-like headers (both sit behind CDNs that reject bare
clients), throttle after every call, backoff with jitter on blocking/transient
statuses — the same politeness contract as ``edhrec._get_with_retry``.
"""
import random
import time

import httpx

from .config import settings

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
}

_RETRY_STATUS = {403, 429, 500, 502, 503, 504}

#: Returned instead of a body for a page that does not exist (404).
NOT_FOUND = "_not_found"


def get_text(client: httpx.Client, url: str, *, encoding: str | None = None,
             attempts: int = 4) -> str | None:
    """Body of ``url``, ``NOT_FOUND`` for a 404, or None when every attempt failed.

    ``encoding`` overrides the declared charset (MTGTop8 serves ISO-8859-1).
    """
    delay = max(settings.request_delay, 0.25)
    for attempt in range(attempts):
        try:
            resp = client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
        except httpx.RequestError:
            time.sleep(delay + random.uniform(0, 0.3))
            delay = min(delay * 2, 8)
            continue
        if resp.status_code == 404:
            time.sleep(settings.request_delay)
            return NOT_FOUND
        if resp.status_code in _RETRY_STATUS:
            if attempt == attempts - 1:
                break
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else delay
            time.sleep(wait + random.uniform(0, 0.4))
            delay = min(delay * 2, 8)
            continue
        if resp.status_code >= 400:
            return None
        time.sleep(settings.request_delay + random.uniform(0, 0.25))
        if encoding:
            return resp.content.decode(encoding, errors="replace")
        return resp.text
    return None
