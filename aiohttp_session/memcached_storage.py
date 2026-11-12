import json
import uuid
from collections.abc import Callable
from typing import Any

import aiomcache
from aiohttp import web

from . import AbstractStorage, Session


class MemcachedStorage(AbstractStorage):
    """Memcached storage"""

    def __init__(
        self,
        memcached_conn: aiomcache.Client,
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
        self._key_factory = key_factory
        self.conn = memcached_conn

    def _storage_key(self, key: Any) -> bytes:
        return (self.cookie_name + "_" + str(key)).encode("utf-8")

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return self._new_session()
        else:
            key = str(cookie)
            data_b = await self.conn.get(self._storage_key(key))
            if data_b is None:
                return self._new_session()
            data = data_b.decode("utf-8")
            try:
                sess_data = self._decoder(data)
            except ValueError:
                sess_data = None
            return self._load_session(key, sess_data)

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

        data = self._encoder(self._get_session_data(session))
        # https://github.com/memcached/memcached/wiki/Programming#expiration
        # "Expiration times can be set from 0, meaning "never expire", to
        # 30 days. Any time higher than 30 days is interpreted as a Unix
        # timestamp date. If you want to expire an object on January 1st of
        # next year, this is how you do that."
        if ttl is None:
            expire = 0
        elif ttl > 30 * 24 * 60 * 60:
            expire = self._now() + ttl
        else:
            expire = ttl
        await self.conn.set(
            self._storage_key(key), data.encode("utf-8"), exptime=expire
        )

    async def _storage_delete(self, key: Any) -> None:
        await self.conn.delete(self._storage_key(key))
