import json
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    ConflictContract,
    ConflictError,
    SimpleCookieStorage,
    get_session,
    session_middleware,
)
from aiohttp_session.memory_storage import MemoryStorage

CONTRACT = ConflictContract(permission_fields=frozenset({"permissions"}))
KEY = "session-key"


def make_storage(**kwargs: Any) -> MemoryStorage:
    kwargs.setdefault("conflict_contract", CONTRACT)
    return MemoryStorage(**kwargs)


def make_request(cookie: str | None = KEY) -> web.Request:
    headers = {"Cookie": f"AIOHTTP_SESSION={cookie}"} if cookie else {}
    return make_mocked_request("GET", "/", headers=headers)


def seed(storage: MemoryStorage, data: dict[str, Any], key: str = KEY) -> None:
    record = {"created": int(time.time()), "session": data}
    storage._store[storage._make_key(key)] = (1, json.dumps(record))


def stored_session(storage: MemoryStorage, key: str = KEY) -> dict[str, Any]:
    _, data_str = storage._store[storage._make_key(key)]
    return json.loads(data_str)["session"]


class BumpingEncoder:
    """Encoder that simulates a concurrent save on every attempt.

    Each time the storage encodes a merged record (which happens between
    the version read and the conditional write), this encoder bumps the
    stored version, forcing the conditional write to fail.
    """

    def __init__(self, storage: MemoryStorage, key: str = KEY) -> None:
        self._storage = storage
        self._store_key = storage._make_key(key)
        self.budget = 0
        self.bumps = 0

    def __call__(self, data: object) -> str:
        if self.budget > 0:
            self.budget -= 1
            version, data_str = self._storage._store[self._store_key]
            self._storage._store[self._store_key] = (version + 1, data_str)
            self.bumps += 1
        return json.dumps(data)


async def test_contract_rejected_without_conditional_writes() -> None:
    assert not SimpleCookieStorage.supports_conditional_writes
    with pytest.raises(RuntimeError, match="conditional writes"):
        SimpleCookieStorage(conflict_contract=CONTRACT)


async def test_contract_accepted_with_conditional_writes() -> None:
    storage = make_storage()
    assert storage.supports_conditional_writes
    assert storage.conflict_contract is CONTRACT


async def test_contract_rejects_negative_retries() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ConflictContract(max_retries=-1)


async def test_new_session_roundtrip() -> None:
    storage = make_storage()
    response = web.Response()
    session = await storage.new_session()
    session["user"] = "alice"
    await storage.save_session(make_request(None), response, session)

    morsel = response.cookies["AIOHTTP_SESSION"]
    assert morsel.value
    reloaded = await storage.load_session(make_request(morsel.value))
    assert not reloaded.new
    assert dict(reloaded) == {"user": "alice"}


async def test_empty_session_deletes_record() -> None:
    storage = make_storage()
    seed(storage, {"user": "alice"})
    session = await storage.load_session(make_request())
    session.invalidate()
    response = web.Response()
    await storage.save_session(make_request(), response, session)
    assert storage._store == {}
    assert response.cookies["AIOHTTP_SESSION"].value == ""


async def test_late_save_does_not_restore_revoked_permission() -> None:
    storage = make_storage()
    seed(
        storage,
        {
            "permissions": {"read": True, "write": True},
            "theme": "light",
            "lang": "en",
        },
    )
    # Request A narrows the authorization scope.
    session_a = await storage.load_session(make_request())
    # Request B loads the same state before A saves.
    session_b = await storage.load_session(make_request())

    del session_a["permissions"]["write"]
    await storage.save_session(make_request(), web.Response(), session_a)

    # B's stale save must not resurrect the deleted permission, but its
    # non-conflicting preference update must survive.
    session_b["theme"] = "dark"
    await storage.save_session(make_request(), web.Response(), session_b)

    # The next request observes the full merged state.
    session_c = await storage.load_session(make_request())
    assert dict(session_c) == {
        "permissions": {"read": True},
        "theme": "dark",
        "lang": "en",
    }


async def test_field_deletion_not_resurrected_by_stale_save() -> None:
    storage = make_storage()
    seed(storage, {"token": "abc", "lang": "en", "theme": "light"})
    session_a = await storage.load_session(make_request())
    session_b = await storage.load_session(make_request())

    del session_a["token"]
    await storage.save_session(make_request(), web.Response(), session_a)

    session_b["lang"] = "fr"
    await storage.save_session(make_request(), web.Response(), session_b)

    session_c = await storage.load_session(make_request())
    assert dict(session_c) == {"lang": "fr", "theme": "light"}


async def test_nested_permission_deletion_wins_over_stale_grant() -> None:
    storage = make_storage()
    seed(storage, {"permissions": {"read": True, "admin": True}, "lang": "en"})
    session_a = await storage.load_session(make_request())
    session_b = await storage.load_session(make_request())

    del session_a["permissions"]["admin"]
    await storage.save_session(make_request(), web.Response(), session_a)

    # B still sees the old snapshot and re-grants the revoked permission.
    session_b["permissions"]["admin"] = True
    session_b["permissions"]["audit"] = True
    await storage.save_session(make_request(), web.Response(), session_b)

    session_c = await storage.load_session(make_request())
    # The revoked permission stays revoked; the unrelated new grant and
    # the untouched permission are kept.
    assert session_c["permissions"] == {"read": True, "audit": True}
    assert session_c["lang"] == "en"


async def test_whole_permission_field_deletion_wins() -> None:
    storage = make_storage()
    seed(storage, {"permissions": {"read": True}, "lang": "en"})
    session_a = await storage.load_session(make_request())
    session_b = await storage.load_session(make_request())

    del session_a["permissions"]
    await storage.save_session(make_request(), web.Response(), session_a)

    session_b["permissions"]["write"] = True
    await storage.save_session(make_request(), web.Response(), session_b)

    session_c = await storage.load_session(make_request())
    assert dict(session_c) == {"lang": "en"}


async def test_single_conflict_is_retried_and_merged() -> None:
    storage = make_storage()
    seed(storage, {"lang": "en", "theme": "light"})
    encoder = BumpingEncoder(storage)
    storage._encoder = encoder

    session_a = await storage.load_session(make_request())
    session_b = await storage.load_session(make_request())
    session_a["theme"] = "dark"
    await storage.save_session(make_request(), web.Response(), session_a)

    # One concurrent save lands between B's read and its conditional write.
    encoder.budget = 1
    session_b["lang"] = "fr"
    await storage.save_session(make_request(), web.Response(), session_b)

    assert encoder.bumps == 1
    assert stored_session(storage) == {"lang": "fr", "theme": "dark"}


async def test_consecutive_conflicts_raise_conflict_error() -> None:
    storage = MemoryStorage(
        conflict_contract=ConflictContract(
            permission_fields=frozenset({"permissions"}), max_retries=2
        )
    )
    seed(storage, {"lang": "en"})
    encoder = BumpingEncoder(storage)
    encoder.budget = 10  # every attempt loses the race
    storage._encoder = encoder

    session = await storage.load_session(make_request())
    session["lang"] = "fr"
    with pytest.raises(ConflictError, match="conflict"):
        await storage.save_session(make_request(), web.Response(), session)

    # Every attempt (initial + 2 retries) lost the race...
    assert encoder.bumps == 3
    # ...and the stored data was never overwritten.
    assert stored_session(storage) == {"lang": "en"}


async def test_encoding_failure_leaves_stored_state_untouched() -> None:
    def bad_encoder(data: object) -> str:
        raise ValueError("cannot encode")

    storage = MemoryStorage(conflict_contract=CONTRACT, encoder=bad_encoder)
    seed(storage, {"permissions": {"read": True}, "lang": "en"})
    version_before = storage._store[storage._make_key(KEY)][0]

    session = await storage.load_session(make_request())
    session["lang"] = "fr"
    with pytest.raises(ValueError, match="cannot encode"):
        await storage.save_session(make_request(), web.Response(), session)

    assert storage._store[storage._make_key(KEY)][0] == version_before
    # The next request still observes the original state.
    reloaded = await storage.load_session(make_request())
    assert dict(reloaded) == {"permissions": {"read": True}, "lang": "en"}


async def test_without_contract_save_overwrites() -> None:
    storage = MemoryStorage()
    seed(storage, {"lang": "en", "theme": "light"})
    session_a = await storage.load_session(make_request())
    session_b = await storage.load_session(make_request())

    del session_a["theme"]
    await storage.save_session(make_request(), web.Response(), session_a)
    session_b["lang"] = "fr"
    await storage.save_session(make_request(), web.Response(), session_b)

    # Plain last-write-wins: the stale save restores the deleted field.
    assert stored_session(storage) == {"lang": "fr", "theme": "light"}


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
async def test_middleware_roundtrip() -> None:
    storage = make_storage()
    middleware = session_middleware(storage)

    async def setter(request: web.Request) -> web.Response:
        session = await get_session(request)
        session["permissions"] = {"read": True}
        session["theme"] = "dark"
        return web.Response(text="ok")

    response = await middleware(make_request(None), setter)
    cookie = response.cookies["AIOHTTP_SESSION"].value
    assert cookie

    async def getter(request: web.Request) -> web.Response:
        session = await get_session(request)
        return web.json_response(dict(session))

    response = await middleware(make_request(cookie), getter)
    assert json.loads(response.text) == {
        "permissions": {"read": True},
        "theme": "dark",
    }
