from __future__ import annotations

import threading

import pytest
from conftest import Clock, join_refreshes

from swr_memoize import delete_memoized, memoize


class Counter:
    """A function body that returns how often it has run."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.calls


def test_a_value_is_computed_once_and_then_served() -> None:
    body = Counter()

    @memoize(timeout=100)
    def f() -> int:
        return body()

    assert (f(), f(), f()) == (1, 1, 1)
    assert body.calls == 1


def test_arguments_key_the_cache_however_they_are_passed() -> None:
    body = Counter()

    @memoize(timeout=100)
    def f(a: int, b: int = 2) -> tuple[int, int, int]:
        return a, b, body()

    assert f(1) == f(1, 2) == f(a=1, b=2) == (1, 2, 1)
    assert f(2) == (2, 2, 2)


def test_a_stale_value_is_served_at_once_and_refreshed_in_the_background(
    clock: Clock,
) -> None:
    body = Counter()

    @memoize(timeout=100)
    def f() -> int:
        return body()

    assert f() == 1
    clock.advance(60)  # past fresh_for (50), inside max_age (100)

    assert f() == 1
    join_refreshes()
    assert f() == 2


def test_a_value_older_than_max_age_is_never_served(clock: Clock) -> None:
    body = Counter()

    @memoize(timeout=100)
    def f() -> int:
        return body()

    assert f() == 1
    clock.advance(101)

    assert f() == 2


def test_only_one_refresh_runs_however_many_callers_find_the_value_stale(
    clock: Clock,
) -> None:
    started = threading.Event()
    release = threading.Event()
    body = Counter()

    @memoize(timeout=100)
    def f() -> int:
        n = body()
        if n > 1:
            started.set()
            release.wait(timeout=5)
        return n

    f()
    clock.advance(60)
    for _ in range(10):
        assert f() == 1
    started.wait(timeout=5)
    release.set()
    join_refreshes()

    assert body.calls == 2
    assert f() == 2


def test_a_failed_refresh_keeps_the_stale_value_and_the_next_caller_retries(
    clock: Clock,
) -> None:
    outcomes = iter([1, RuntimeError("backend down"), 3])

    @memoize(timeout=100)
    def f() -> int:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    f()
    clock.advance(60)
    assert f() == 1
    join_refreshes()

    assert f() == 1  # still stale; this call retries the refresh
    join_refreshes()
    assert f() == 3


def test_fresh_for_equal_to_max_age_behaves_like_django_memoize(clock: Clock) -> None:
    body = Counter()

    @memoize(timeout=100, fresh_for=100)
    def f() -> int:
        return body()

    f()
    clock.advance(99)
    assert f() == 1
    join_refreshes()
    assert body.calls == 1


def test_fresh_for_defaults_to_half_of_timeout() -> None:
    @memoize(timeout=3600)
    def f() -> None:
        return None

    assert (f.fresh_for, f.cache_timeout) == (1800, 3600)  # type: ignore[attr-defined]


def test_timeout_and_max_age_are_one_setting() -> None:
    with pytest.raises(TypeError):
        memoize(timeout=10, max_age=10)


def test_fresh_for_cannot_exceed_max_age() -> None:
    with pytest.raises(ValueError):
        memoize(max_age=10, fresh_for=11)


def test_delete_memoized_forgets_every_value_of_the_function() -> None:
    body = Counter()

    @memoize(timeout=100)
    def f(a: int) -> int:
        return body()

    f(1), f(2)
    delete_memoized(f)

    assert (f(1), f(2)) == (3, 4)


def test_delete_memoized_with_arguments_forgets_only_that_value() -> None:
    body = Counter()

    @memoize(timeout=100)
    def f(a: int) -> int:
        return body()

    f(1), f(2)
    delete_memoized(f, 1)

    assert (f(1), f(2)) == (3, 2)


def test_methods_are_cached_per_instance() -> None:
    body = Counter()

    class Thing:
        def __init__(self, pk: int) -> None:
            self.pk = pk

        def __repr__(self) -> str:
            return f"Thing({self.pk})"

        @memoize(timeout=100)
        def value(self) -> int:
            return body()

    one, two = Thing(1), Thing(2)
    assert (one.value(), two.value(), one.value()) == (1, 2, 1)


def test_unless_bypasses_the_cache() -> None:
    body = Counter()

    @memoize(timeout=100, unless=lambda: True)
    def f() -> int:
        return body()

    assert (f(), f()) == (1, 2)


def test_the_original_function_stays_reachable() -> None:
    body = Counter()

    @memoize(timeout=100)
    def f() -> int:
        return body()

    f()
    assert f.uncached() == 2  # type: ignore[attr-defined]
