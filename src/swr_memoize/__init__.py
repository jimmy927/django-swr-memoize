"""Stale-while-revalidate memoization for Django.

The API matches django-memoize (``memoize``, ``delete_memoized``,
``delete_memoized_verhash``, ``Memoizer``), and so do cache keys, versioning and
``delete_memoized``. What changes is what happens once a value gets old:

- younger than ``fresh_for``: served from the cache.
- older than ``fresh_for`` but younger than ``max_age``: served from the cache
  at once, and recomputed in a background thread for the next caller. Only one
  process refreshes a key at a time (a lock taken with ``cache.add``).
- older than ``max_age``, or never computed: computed now, and the caller waits.

``timeout`` keeps its django-memoize meaning — no value is ever served older
than it — and is another name for ``max_age``. ``fresh_for`` defaults to half of
it, so a key read at least once per ``timeout / 2`` never makes anyone wait.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar, cast

from django.conf import settings
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache
from django.db import connections
from django.utils.encoding import force_bytes

__version__ = "0.2.0"

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

#: ``fresh_for`` as a share of ``max_age`` when only ``timeout``/``max_age`` is given.
DEFAULT_FRESH_SHARE = 0.5

#: Name of the background threads, so tests and debuggers can find them.
REFRESH_THREAD_NAME = "swr-memoize-refresh"

#: ``on_miss`` default: a miss waits for the function, as in django-memoize.
WAIT = object()


class DefaultCacheObject:
    pass


DEFAULT_CACHE_OBJECT = DefaultCacheObject()


def function_namespace(
    f: Callable[..., Any], args: tuple[Any, ...] | None = None
) -> tuple[str, str | None]:
    """The function's namespace, and the instance's too for a bound method."""
    m_args = inspect.getfullargspec(f).args
    instance_token = None

    instance_self = getattr(f, "__self__", None)

    if instance_self and not inspect.isclass(instance_self):
        instance_token = repr(f.__self__)  # type: ignore[attr-defined]
    elif m_args and m_args[0] == "self" and args:
        instance_token = repr(args[0])

    module = f.__module__ or __name__
    name = f.__qualname__
    ns = f"{module}.{name}"
    ins = f"{module}.{name}.{instance_token}" if instance_token else None
    return ns, ins


class Memoizer:
    """Holds the cache, key prefix and refresh settings for a family of memoized functions."""

    def __init__(
        self,
        cache: BaseCache = default_cache,
        cache_prefix: str = "swr_memoize",
        default_cache_value: object = DEFAULT_CACHE_OBJECT,
        refresh_lock_timeout: int = 300,
    ) -> None:
        self.cache = cache
        self.cache_prefix = cache_prefix
        self.default_cache_value = default_cache_value
        #: How long a refresh lock is held at most. A refresh killed mid-flight
        #: (a worker restart) blocks the next one for no longer than this.
        self.refresh_lock_timeout = refresh_lock_timeout

    def get(self, key: str) -> Any:  # noqa: ANN401
        "Proxy function for internal cache object."
        return self.cache.get(key=key, default=self.default_cache_value)

    def set(
        self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT
    ) -> None:  # noqa: ANN401
        "Proxy function for internal cache object."
        self.cache.set(key=key, value=value, timeout=timeout)

    def add(
        self, key: str, value: Any, timeout: Any = DEFAULT_TIMEOUT
    ) -> bool:  # noqa: ANN401
        "Proxy function for internal cache object."
        return self.cache.add(key=key, value=value, timeout=timeout)

    def delete(self, key: str) -> None:
        "Proxy function for internal cache object."
        self.cache.delete(key=key)

    def delete_many(self, *keys: str) -> None:
        "Proxy function for internal cache object."
        self.cache.delete_many(keys=keys)

    def clear(self) -> None:
        "Proxy function for internal cache object."
        self.cache.clear()

    def get_many(self, *keys: str) -> list[Any]:
        "Proxy function for internal cache object."
        d = self.cache.get_many(keys=keys)
        return [d.get(key) for key in keys]

    def set_many(
        self, mapping: dict[str, Any], timeout: Any = DEFAULT_TIMEOUT
    ) -> None:  # noqa: ANN401
        "Proxy function for internal cache object."
        self.cache.set_many(data=mapping, timeout=timeout)

    def _memvname(self, funcname: str) -> str:
        return hashlib.md5(force_bytes(funcname)).hexdigest() + "_memver"

    def _memoize_make_version_hash(self) -> str:
        return uuid.uuid4().hex

    def _memoize_version(
        self,
        f: Callable[..., Any],
        args: tuple[Any, ...] | None = None,
        reset: bool = False,
        delete: bool = False,
    ) -> tuple[str, str | None]:
        """Updates the hash version associated with a memoized function or method.

        Version keys never expire. django-memoize gives them the function's
        timeout, so every value of the function becomes unreachable at once when
        it lapses; values refreshed in the background would be lost with it.
        """
        fname, instance_fname = function_namespace(f, args=args)
        version_key = self._memvname(fname)
        fetch_keys = [version_key]

        if instance_fname:
            fetch_keys.append(self._memvname(instance_fname))

        # Only delete the per-instance version key or per-function version
        # key but not both.
        if delete:
            self.delete(fetch_keys[-1])
            return fname, None

        version_data_list = self.get_many(*fetch_keys)
        dirty = False

        if version_data_list[0] is None:
            version_data_list[0] = self._memoize_make_version_hash()
            dirty = True

        if instance_fname and version_data_list[1] is None:
            version_data_list[1] = self._memoize_make_version_hash()
            dirty = True

        # Only reset the per-instance version or the per-function version
        # but not both.
        if reset:
            fetch_keys = fetch_keys[-1:]
            version_data_list = [self._memoize_make_version_hash()]
            dirty = True

        if dirty:
            self.set_many(dict(zip(fetch_keys, version_data_list)), timeout=None)

        return fname, "".join(version_data_list)

    def _memoize_make_cache_key(
        self, make_name: Callable[[str], str] | None = None
    ) -> Callable[..., str]:
        """Function used to create the cache_key for memoized functions."""

        def make_cache_key(f: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
            fname, version_data = self._memoize_version(f, args=args)

            #: this should have to be after version_data, so that it
            #: does not break the delete_memoized functionality.
            altfname = make_name(fname) if callable(make_name) else fname

            if callable(f):
                keyargs, keykwargs = self._memoize_kwargs_to_args(f, *args, **kwargs)
            else:
                keyargs, keykwargs = args, kwargs

            cache_key = hashlib.md5(
                force_bytes((altfname, keyargs, keykwargs))
            ).hexdigest()
            cache_key += version_data or ""

            if self.cache_prefix:
                cache_key = f"{self.cache_prefix}:{cache_key}"

            return cache_key

        return make_cache_key

    def _memoize_kwargs_to_args(
        self, f: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Orders arguments so ``f(1, b=2)`` and ``f(a=1, b=2)`` share a key."""
        new_args = []
        arg_num = 0
        argspec = inspect.getfullargspec(f)
        defaults = argspec.defaults or ()

        args_len = len(argspec.args)
        for i in range(args_len):
            if i == 0 and argspec.args[i] in ("self", "cls"):
                #: use the repr of the class instance, so instance methods
                #: are cached per instance
                arg = repr(args[0])
                arg_num += 1
            elif argspec.args[i] in kwargs:
                arg = kwargs.pop(argspec.args[i])
            elif arg_num < len(args):
                arg = args[arg_num]
                arg_num += 1
            elif abs(i - args_len) <= len(defaults):
                arg = defaults[i - args_len]
                arg_num += 1
            else:
                arg = None
                arg_num += 1

            new_args.append(arg)

        # If there are any missing varargs then
        # just append them since consistency of the key trumps order.
        if argspec.varargs and args_len < len(args):
            new_args.extend(args[args_len:])

        return tuple(new_args), kwargs

    def _resolve_ages(
        self, timeout: Any, fresh_for: float | None, max_age: Any  # noqa: ANN401
    ) -> tuple[float | None, float | None]:
        """(fresh_for, max_age) in seconds; None means forever."""
        if timeout is not DEFAULT_TIMEOUT and max_age is not DEFAULT_TIMEOUT:
            raise TypeError("timeout is another name for max_age; pass one of them")
        age = max_age if max_age is not DEFAULT_TIMEOUT else timeout
        if age is DEFAULT_TIMEOUT:
            age = self.cache.default_timeout
        if fresh_for is None:
            fresh_for = None if age is None else age * DEFAULT_FRESH_SHARE
        elif age is not None and fresh_for > age:
            raise ValueError(f"fresh_for ({fresh_for}) must not exceed max_age ({age})")
        return fresh_for, age

    def _refresh_lock_key(self, cache_key: str) -> str:
        return f"{cache_key}:refreshing"

    def _start_refresh(
        self,
        decorated_function: Callable[..., Any],
        f: Callable[..., Any],
        cache_key: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Compute ``cache_key`` in a background thread, unless another process already is."""
        lock_key = self._refresh_lock_key(cache_key)
        if not self.add(lock_key, True, timeout=self.refresh_lock_timeout):
            return

        def refresh() -> None:
            try:
                rv = f(*args, **kwargs)
                self.set(
                    cache_key,
                    (time.time(), rv),
                    timeout=decorated_function.cache_timeout,  # type: ignore[attr-defined]
                )
            except Exception:
                # A stale value stays served until max_age, a miss keeps
                # returning on_miss; the next such call retries.
                logger.exception(
                    f"swr_memoize: background refresh of {f.__qualname__} failed"
                )
            finally:
                self.delete(lock_key)
                # Django opens one connection per thread and only closes the
                # request thread's; without this each refresh leaks one.
                connections.close_all()

        threading.Thread(target=refresh, name=REFRESH_THREAD_NAME, daemon=True).start()

    def memoize(
        self,
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ANN401
        make_name: Callable[[str], str] | None = None,
        unless: Callable[[], bool] | None = None,
        *,
        fresh_for: float | None = None,
        max_age: Any = DEFAULT_TIMEOUT,  # noqa: ANN401
        on_miss: Any = WAIT,  # noqa: ANN401
    ) -> Callable[[F], F]:
        """Cache the result of a function, keyed on its arguments.

        Example::

            @memoize(timeout=3600)
            def big_foo(a, b):
                return a + b + random.randrange(0, 1000)

        :param timeout: Another name for ``max_age``; django-memoize's parameter.
        :param make_name: If set, maps the function name to the name used in the key.
        :param unless: If set and it returns True, the cache is bypassed entirely.
        :param fresh_for: Seconds a value is served without refreshing it.
                          Default: half of ``max_age``.
        :param max_age: Seconds after which a value is never served; the caller
                        waits for a new one. Default: the cache's default timeout.
                        ``None`` keeps values forever.
        :param on_miss: If set, a miss (never computed, or older than
                        ``max_age``) returns this at once and computes the value
                        in the background for the next caller, instead of
                        waiting for it.

        The decorated function carries ``uncached`` (the original function),
        ``wait_on_miss`` (the memoized function, but a miss waits for the value
        whatever ``on_miss`` says), ``cache_timeout`` (``max_age``),
        ``fresh_for``, ``make_cache_key`` and ``delete_memoized``.
        """
        resolved_fresh_for, resolved_max_age = self._resolve_ages(
            timeout, fresh_for, max_age
        )

        def memoize(f: F) -> F:
            def lookup(args: tuple[Any, ...], kwargs: dict[str, Any], wait: bool) -> Any:  # noqa: ANN401
                #: bypass cache
                if callable(unless) and unless() is True:
                    return f(*args, **kwargs)

                # try to fetch the function's return value from the cache
                try:
                    cache_key = decorated_function.make_cache_key(f, *args, **kwargs)
                    entry = self.get(cache_key)
                except Exception:
                    if settings.DEBUG:
                        raise
                    logger.exception("Exception possibly due to cache backend.")
                    return f(*args, **kwargs)

                if entry != self.default_cache_value:
                    computed_at, rv = entry
                    fresh = decorated_function.fresh_for
                    if fresh is not None and time.time() - computed_at >= fresh:
                        self._start_refresh(
                            decorated_function, f, cache_key, args, kwargs
                        )
                    return rv

                # a miss (never computed, or older than max_age)
                if not wait:
                    self._start_refresh(decorated_function, f, cache_key, args, kwargs)
                    return on_miss
                rv = f(*args, **kwargs)
                try:
                    self.set(
                        cache_key,
                        (time.time(), rv),
                        timeout=decorated_function.cache_timeout,
                    )
                except Exception:
                    if settings.DEBUG:
                        raise
                    logger.exception("Exception possibly due to cache backend.")
                return rv

            @functools.wraps(f)
            def decorated_function(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                return lookup(args, kwargs, wait=on_miss is WAIT)

            decorated_function.uncached = f  # type: ignore[attr-defined]
            decorated_function.wait_on_miss = lambda *args, **kwargs: lookup(  # type: ignore[attr-defined]
                args, kwargs, wait=True
            )
            decorated_function.cache_timeout = resolved_max_age  # type: ignore[attr-defined]
            decorated_function.fresh_for = resolved_fresh_for  # type: ignore[attr-defined]
            decorated_function.make_cache_key = self._memoize_make_cache_key(  # type: ignore[attr-defined]
                make_name
            )
            decorated_function.delete_memoized = lambda: self.delete_memoized(f)  # type: ignore[attr-defined]

            return cast(F, decorated_function)

        return memoize

    def delete_memoized(self, f: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Forget cached values of ``f``: all of them, or those for the given arguments.

        The next call recomputes and waits, as with django-memoize. Pass the
        memoized function itself, not its name.
        """
        if not callable(f):
            raise DeprecationWarning(
                "Deleting messages by relative name is no longer"
                " reliable, please switch to a function reference"
            )

        try:
            if not args and not kwargs:
                self._memoize_version(f, reset=True)
            else:
                cache_key = f.make_cache_key(f.uncached, *args, **kwargs)  # type: ignore[attr-defined]
                self.delete(cache_key)
        except Exception:
            if settings.DEBUG:
                raise
            logger.exception("Exception possibly due to cache backend.")

    def delete_memoized_verhash(self, f: Callable[..., Any], *args: Any) -> None:
        """Delete the version hash associated with the function."""
        if not callable(f):
            raise DeprecationWarning(
                "Deleting messages by relative name is no longer"
                " reliable, please use a function reference"
            )

        try:
            self._memoize_version(f, delete=True)
        except Exception:
            if settings.DEBUG:
                raise
            logger.exception("Exception possibly due to cache backend.")


# Memoizer instance
_memoizer = Memoizer()

# Public objects
memoize = _memoizer.memoize
delete_memoized = _memoizer.delete_memoized
delete_memoized_verhash = _memoizer.delete_memoized_verhash
