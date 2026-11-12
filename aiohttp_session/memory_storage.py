"""In-memory session storage with conditional (compare-and-swap) writes."""

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web

from . import AbstractStorage, Session, SessionData
from .conflict import ConflictContract, ConflictError, merge_session_data


class MemoryStorage(AbstractStorage):
    """In-memory session storage.

    Every stored record carries a version and all writes are conditional
    on the version read at the start of the save, so a concurrent save is
    always detected.  When a :class:`ConflictContract` is configured the
    conflicting save is re-merged against the freshest stored state (with
    deletion-wins semantics for permission fields and field-level
    last-writer-wins for everything else) and retried, instead of
    silently overwriting the other request's update.
    """

    supports_conditional_writes = True

    def __init__(
        self,
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
        conflict_contract: ConflictContract | None = None,
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
            conflict_contract=conflict_contract,
        )
        self._key_factory = key_factory
        # storage key -> (version, encoded session data)
        self._store: dict[str, tuple[int, str]] = {}
        self._lock = asyncio.Lock()

    def _make_key(self, identity: str) -> str:
        return self.cookie_name + "_" + identity

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        track_changes = self._conflict_contract is not None
        if cookie is None:
            return Session(
                None,
                data=None,
                new=True,
                max_age=self.max_age,
                track_changes=track_changes,
            )
        key = str(cookie)
        async with self._lock:
            record = self._store.get(self._make_key(key))
        if record is None:
            return Session(
                None,
                data=None,
                new=True,
                max_age=self.max_age,
                track_changes=track_changes,
            )
        _, data_str = record
        try:
            data = self._decoder(data_str)
        except ValueError:
            data = None
        return Session(
            key,
            data=data,
            new=False,
            max_age=self.max_age,
            track_changes=track_changes,
        )

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        key = session.identity
        if key is None:
            key = self._key_factory()
            self.save_cookie(response, key, max_age=session.max_age)
            data_str = self._encoder(self._get_session_data(session))
            async with self._lock:
                version, _ = self._store.get(self._make_key(key), (0, ""))
                self._store[self._make_key(key)] = (version + 1, data_str)
            return

        store_key = self._make_key(str(key))
        if session.empty:
            self.save_cookie(response, "", max_age=session.max_age)
            async with self._lock:
                self._store.pop(store_key, None)
            return

        self.save_cookie(response, str(key), max_age=session.max_age)
        contract = self._conflict_contract
        if contract is None:
            data_str = self._encoder(self._get_session_data(session))
            async with self._lock:
                version, _ = self._store.get(store_key, (0, ""))
                self._store[store_key] = (version + 1, data_str)
            return

        await self._save_with_merge(store_key, session, contract)

    async def _save_with_merge(
        self, store_key: str, session: Session, contract: ConflictContract
    ) -> None:
        local_data = self._get_session_data(session)
        base = session._snapshot if session._snapshot is not None else {}
        for _ in range(contract.max_retries + 1):
            async with self._lock:
                record = self._store.get(store_key)
            if record is None:
                version = 0
                stored_data: SessionData = {}
            else:
                version, stored_str = record
                try:
                    stored_data = self._decoder(stored_str)
                except ValueError:
                    stored_data = {}
            merged_session = merge_session_data(
                contract,
                base=base,
                local=local_data.get("session", {}),
                stored=stored_data.get("session", {}) if stored_data else {},
            )
            created = local_data.get("created")
            if stored_data:
                created = stored_data.get("created", created)
            # Encoding happens before any write: an encoder failure
            # aborts the save and leaves the stored record untouched.
            data_str = self._encoder({"created": created, "session": merged_session})
            async with self._lock:
                current = self._store.get(store_key)
                current_version = current[0] if current is not None else 0
                if current_version != version:
                    continue  # lost the race: re-read, re-merge and retry
                self._store[store_key] = (version + 1, data_str)
                return
        raise ConflictError(
            "Could not save session after {} conflict(s); the stored "
            "version kept changing, aborted instead of overwriting "
            "concurrent updates".format(contract.max_retries + 1)
        )
