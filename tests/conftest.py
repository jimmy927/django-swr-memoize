from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import django
import pytest
from django.conf import settings

settings.configure(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    DATABASES={},
    INSTALLED_APPS=[],
)
django.setup()

from django.core.cache import cache  # noqa: E402

from swr_memoize import REFRESH_THREAD_NAME  # noqa: E402


class Clock:
    """Stands in for time.time, which both the library and LocMemCache read."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    cache.clear()
    fake = Clock()
    monkeypatch.setattr(time, "time", fake)
    yield fake
    join_refreshes()


def join_refreshes() -> None:
    for thread in threading.enumerate():
        if thread.name == REFRESH_THREAD_NAME:
            thread.join(timeout=5)
