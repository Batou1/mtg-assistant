"""Shared test guards.

Two things made the suite flaky (about one run in three failed at fixture
setup with ``sqlite3.OperationalError: database is locked`` from ``get_conn``):

* Modules bind ``settings`` at import time (``from .config import settings``),
  so a fixture that reloads ``app.config`` with ``MTG_BULK_AUTO_REFRESH=0`` but
  not ``app.bulk_data`` leaves the scheduler looking at a *stale* settings
  object — and re-importing ``app.main`` then started a real ``bulk-data-refresh``
  thread that downloaded Scryfall exports and wrote its ``meta`` rows into
  whichever throwaway database the *next* test had just created.  Pinning the
  variable here, before any ``app`` import, means every reload sees it.
* Every chat turn schedules a player-profile refresh in a daemon thread; a test
  that returned before it finished left it writing into the next test's fresh
  database.  Two connections racing on ``PRAGMA journal_mode=WAL`` for a brand
  new file fail immediately (SQLite skips the busy handler for that lock).

The autouse fixture below joins whatever threads a test started, and fails the
*leaking* test by name rather than letting an unrelated later test blow up.
"""
import os
import threading

import pytest

# Master switch for background network threads (bulk Scryfall refresh and the
# monthly Comprehensive Rules check). Must be set before app.config is first
# imported; individual fixtures may still set it explicitly.
os.environ["MTG_BULK_AUTO_REFRESH"] = "0"

_JOIN_TIMEOUT_SECONDS = 15.0


@pytest.fixture(autouse=True)
def _no_thread_leaks(request):
    """Fail a test that leaves background threads running past its teardown."""
    before = set(threading.enumerate())
    yield
    started = [t for t in threading.enumerate() if t not in before]
    for t in started:
        t.join(_JOIN_TIMEOUT_SECONDS)
    alive = [t.name for t in started if t.is_alive()]
    if alive:
        pytest.fail(
            f"{request.node.nodeid} leaked background thread(s) still running after "
            f"{_JOIN_TIMEOUT_SECONDS:.0f}s: {alive}. Join or disable them in the test/fixture "
            "(they would otherwise write into the next test's database)."
        )
