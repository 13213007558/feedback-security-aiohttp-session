import json
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web

from . import AbstractStorage, Session

try:
    from redis import VERSION as REDIS_VERSION, asyncio as aioredis
except ImportError:  # pragma: no cover
    try:
        import aioredis  # type: ignore[import-not-found, no-redef]
    except ImportError:
        aioredis = None  # type: ignore[assignment]
    else:
        import warnings

        warnings.warn(
            "aioredis library is deprecated, please replace with redis.",
            DeprecationWarning,
            stacklevel=1,
        )
        REDIS_VERSION = (4, 3)


class RedisStorage(AbstractStorage):
    """Redis storage"""

    def __init__(
        self,
        redis_pool: "aioredis.Redis",
        *,
        cookie_name: str = "AIOHTTP_SESSION",
        domain: str | None = None,
        max_age: int | None = None,
        path: str = "/",
        secure: bool | None = None,
        httponly: bool = True,
        samesite: str | None = None,
        key_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        encoder: Callable[[object], str] = json.dumps,
        decoder: Callable[[str], Any] = json.loads,
        absolute_max_age: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            cookie_name=cookie_name,
            domain=domain,
            max_age=max_age,
            path=path,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            encoder=encoder,
            decoder=decoder,
            absolute_max_age=absolute_max_age,
            clock=clock,
        )
        if aioredis is None:
            raise RuntimeError("Please install redis")
        # May have installed aioredis separately (without aiohttp-session[aioredis]).
        if REDIS_VERSION < (4, 3):
            raise RuntimeError("redis<4.3 is not supported")
        self._key_factory = key_factory
        if not isinstance(redis_pool, aioredis.Redis):
            raise TypeError(f"Expected redis.asyncio.Redis got {type(redis_pool)}")
        self._redis = redis_pool

    def _storage_key(self, key: Any) -> str:
        return self.cookie_name + "_" + str(key)

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return self._new_session()
        else:
            key = str(cookie)
            data_bytes = await self._redis.get(self._storage_key(key))
            if data_bytes is None:
                return self._new_session()
            data_str = data_bytes.decode("utf-8")
            try:
                data = self._decoder(data_str)
            except ValueError:
                data = None
            return self._load_session(key, data)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        ttl = self._ttl(session)
        if self._should_expire(session) or ttl == 0:
            if session.identity is not None:
                await self._storage_delete(session.identity)
            self.save_cookie(response, "")
            return

        key = session.identity
        if key is None:
            key = self._key_factory()
            self.save_cookie(response, key, max_age=ttl)
        else:
            if session.empty:
                self.save_cookie(response, "", max_age=ttl)
            else:
                key = str(key)
                self.save_cookie(response, key, max_age=ttl)

        data_str = self._encoder(self._get_session_data(session))
        await self._redis.set(
            self._storage_key(key),
            data_str,
            ex=ttl,
        )

    async def _storage_delete(self, key: Any) -> None:
        await self._redis.delete(self._storage_key(key))
