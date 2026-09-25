# django-swr-memoize

[![CI](https://github.com/Morpheus-Tribe/django-swr-memoize/actions/workflows/ci.yml/badge.svg)](https://github.com/Morpheus-Tribe/django-swr-memoize/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/django-swr-memoize.svg)](https://pypi.org/project/django-swr-memoize/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-swr-memoize.svg)](https://pypi.org/project/django-swr-memoize/)
[![Django versions](https://img.shields.io/pypi/djversions/django-swr-memoize.svg)](https://pypi.org/project/django-swr-memoize/)
[![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

Stale-while-revalidate memoization for Django. The API is the same as
[django-memoize](https://pypi.org/project/django-memoize/), but only the very
first call for a key ever waits for the function.

## Why

With django-memoize, when a cached value expires, the next caller runs the
function and waits for it. If the function takes 9 s and its value expires
hourly, someone waits 9 s every hour. Under load it's worse: every caller that
arrives during those 9 s misses the cache too, and they all run the function at
the same time.

django-swr-memoize serves the old value straight away, recomputes it in a
background thread, and the next caller gets the new value. As long as a key is
read often enough, nobody ever waits.

## Installation

```bash
pip install django-swr-memoize
```

It uses Django's configured cache (`django.core.cache.cache`), so there's
nothing to add to `INSTALLED_APPS` and no broker to run. For the lock that keeps
refreshes to one per key, the cache has to be shared between processes:
database, Redis or Memcached, not `LocMemCache`.

## Usage

```python
from swr_memoize import delete_memoized, memoize


@memoize(timeout=3600)
def pool_state(assignment_id: int) -> dict:
    ...  # slow


pool_state(743)              # first call ever: computes and waits
pool_state(743)              # served from the cache
delete_memoized(pool_state)  # forget every value; the next call waits again
```

### How old a value may be

How a call behaves depends on how old the cached value is:

| Age of the cached value | What the caller gets |
|---|---|
| younger than `fresh_for` | the cached value |
| between `fresh_for` and `max_age` | the cached value at once; a background thread recomputes it for the next caller |
| older than `max_age`, or never computed | a newly computed value (the caller waits) |

- **`timeout`** means what it means in django-memoize: no value is ever served
  older than this. It's another name for `max_age`.
- **`fresh_for`** defaults to half of `max_age`. With `timeout=3600`, a value is
  served as it is for 30 minutes, then served and refreshed for up to 60
  minutes. A key read at least once per `timeout / 2` never makes anyone wait,
  and each key is recomputed at most twice per `timeout`.
- **Pass `fresh_for=` to choose a different split.** `fresh_for == max_age`
  behaves exactly like django-memoize.

| Parameter | Default | Meaning |
|---|---|---|
| `timeout` / `max_age` | the cache's default timeout | a value older than this is never served; `None` keeps values forever |
| `fresh_for` | half of `max_age` | a value younger than this is served without refreshing it |
| `make_name` | `None` | maps the function name to the one used in the key |
| `unless` | `None` | a callable; when it returns `True` the cache is bypassed |

### Background refreshes

- **One at a time per key, across processes.** A lock taken with `cache.add`
  means only one process refreshes a key at once, so 40 gunicorn workers still
  make one refresh.
- **Refreshes are threads, not tasks.** If a process dies mid-refresh, the stale
  value keeps being served, and the lock expires after
  `Memoizer(refresh_lock_timeout=300)` seconds.
- **A failed refresh is logged and changes nothing.** The stale value stays
  until `max_age`, and the next caller past `fresh_for` retries.
- **Database connections are closed.** The refresh thread closes its own Django
  database connections when it finishes.

## Moving from django-memoize

These behave the same as in django-memoize:

- `memoize(timeout, make_name, unless)`
- `delete_memoized(f, *args, **kwargs)` and `delete_memoized_verhash(f)`
- the `Memoizer` class
- the `uncached`, `cache_timeout`, `make_cache_key` and `delete_memoized`
  attributes on the decorated function

To move a function over, change its import from `memoize` to `swr_memoize`.
Both libraries can be installed side by side, since this one stores its
entries under its own `swr_memoize` key prefix. That lets you move functions
over one at a time.

## Supported versions

Every combination below runs the full test suite in
[CI](https://github.com/Morpheus-Tribe/django-swr-memoize/actions/workflows/ci.yml)
on every push and pull request, and weekly.

| Django | Python |
|---|---|
| 4.2 | 3.10, 3.11, 3.12 |
| 5.2 | 3.10, 3.11, 3.12, 3.13, 3.14 |
| 6.0 | 3.12, 3.13, 3.14 |
| 6.1 | 3.12, 3.13, 3.14 |

## Development

```bash
git clone https://github.com/Morpheus-Tribe/django-swr-memoize
cd django-swr-memoize
uv run --with pytest --with django python -m pytest -v
```

To test against one particular Python and Django, as CI does:

```bash
uv run --isolated --python 3.12 --with pytest --with "Django~=5.2.0" python -m pytest -v
```

The tests are in `tests/test_memoize.py`. They cover:

- fresh, stale and expired values
- a single refresh under concurrent callers
- a failed refresh
- both forms of `delete_memoized`
- instance methods, `unless` and `uncached`

A fake clock drives the timing, so the whole suite runs in well under a second.

## Releasing

Update `__version__` in `src/swr_memoize/__init__.py`, then create a GitHub
release. The `Publish` workflow builds the package and uploads it to PyPI
through trusted publishing, so no API token is stored anywhere.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
