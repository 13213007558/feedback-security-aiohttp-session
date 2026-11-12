"""Deterministic tests for session payload resource limits.

Only small, hand-built synthetic payloads are used: nested containers, a
handful of sibling containers and a multi-byte string. There are no large
or randomized payloads. The tests assert whether the decoder was invoked
and what the storage actually persists after a rejection.
"""

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_session import (
    LimitKind,
    LimitViolation,
    Session,
    SimpleCookieStorage,
    get_session,
    measure_json_complexity,
    session_middleware,
)
from aiohttp_session.cookie_storage import EncryptedCookieStorage

# The library stores the storage under a plain string request key; running
# the real middleware against a mocked request emits aiohttp's advisory
# NotAppKeyWarning, which is unrelated to the behaviour under test.
pytestmark = pytest.mark.filterwarnings(
    "ignore:It is recommended to use web.RequestKey:Warning"
)


def make_request(cookie_value: str | None = None) -> web.Request:
    headers: dict[str, str] = {}
    if cookie_value is not None:
        headers["Cookie"] = "AIOHTTP_SESSION=" + cookie_value
    return make_mocked_request("GET", "/", headers=headers)


def encode(session: dict[str, Any]) -> str:
    return json.dumps({"created": 1, "session": session})


class RecordingDecoder:
    """Decoder that records every payload it was asked to decode."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> Any:
        self.calls.append(text)
        return json.loads(text)


class RecordingPolicy:
    def __init__(self) -> None:
        self.violations: list[LimitViolation] = []

    async def __call__(
        self, request: web.Request, storage: Any, violation: LimitViolation
    ) -> None:
        self.violations.append(violation)


def nested(value: Any, depth: int) -> Any:
    """Build a value nested *depth* container levels deep."""
    for _ in range(depth):
        value = [value]
    return value


# ---------------------------------------------------------------------------
# measure_json_complexity: small, fully deterministic structures
# ---------------------------------------------------------------------------


def test_measure_scalar() -> None:
    assert measure_json_complexity(1) == (1, 0)
    assert measure_json_complexity("x") == (1, 0)


def test_measure_empty_container() -> None:
    assert measure_json_complexity([]) == (1, 1)
    assert measure_json_complexity({}) == (1, 1)


def test_measure_depth_and_container_count() -> None:
    # {"session": {"a": [[1]], "b": [2, 3]}}
    value = {"session": {"a": [[1]], "b": [2, 3]}}
    depth, containers = measure_json_complexity(value)
    assert depth == 5
    assert containers == 5


def test_measure_is_iterative_and_bounded() -> None:
    # Far deeper than the Python recursion limit would allow recursively.
    value = nested(1, 5000)
    depth, containers = measure_json_complexity(value)
    assert depth == 5001
    assert containers == 5000


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_blob_size": 0},
        {"max_session_depth": -1},
        {"max_session_containers": 0},
    ),
)
def test_limits_must_be_positive(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SimpleCookieStorage(**kwargs)


def test_limits_disabled_by_default() -> None:
    storage = SimpleCookieStorage()
    assert storage.max_blob_size is None
    assert storage.max_session_depth is None
    assert storage.max_session_containers is None


# ---------------------------------------------------------------------------
# Byte budget: the decoder must never see an oversized transport payload
# ---------------------------------------------------------------------------


async def test_blob_size_under_limit_decodes_normally() -> None:
    payload = encode({"a": 1})
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder, max_blob_size=len(payload), on_limit_exceeded=policy
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == [payload]
    assert policy.violations == []
    assert not session.new
    assert session["a"] == 1


async def test_blob_size_over_limit_skips_decoder() -> None:
    payload = encode({"a": 1})
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_blob_size=len(payload) - 1,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == []
    assert policy.violations == [
        LimitViolation(LimitKind.BYTES, len(payload) - 1, len(payload))
    ]
    assert session.new
    assert dict(session) == {}
    assert not session._changed


async def test_blob_size_counts_utf8_expansion_not_characters() -> None:
    # "é" is two UTF-8 bytes but one character: the boundary is measured in
    # transport bytes, so a one-character-per-byte budget must reject it.
    payload = json.dumps(
        {"created": 1, "session": {"k": "é"}}, ensure_ascii=False
    )
    assert len(payload) < len(payload.encode("utf-8"))
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_blob_size=len(payload),
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == []
    assert policy.violations[0].kind is LimitKind.BYTES
    assert policy.violations[0].observed == len(payload.encode("utf-8"))
    assert session.new and dict(session) == {}


# ---------------------------------------------------------------------------
# Structural limits: depth and container count, at exact boundaries
# ---------------------------------------------------------------------------


async def test_depth_at_limit_is_accepted() -> None:
    inner = nested(1, 2)
    payload = encode({"a": inner})
    limit_depth = measure_json_complexity(json.loads(payload))[0]
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_session_depth=limit_depth,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == [payload]
    assert policy.violations == []
    assert not session.new


async def test_depth_over_limit_is_rejected_after_decoding() -> None:
    inner = nested(1, 3)
    payload = encode({"a": inner})
    observed_depth = measure_json_complexity(json.loads(payload))[0]
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_session_depth=observed_depth - 1,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    # Structural checks happen on the decoded value, so the decoder ran once.
    assert decoder.calls == [payload]
    assert policy.violations == [
        LimitViolation(LimitKind.DEPTH, observed_depth - 1, observed_depth)
    ]
    assert session.new and dict(session) == {}


async def test_container_count_over_limit_is_rejected() -> None:
    # Four small sibling containers, all within a shallow structure.
    payload = encode({"groups": [[1], [2], [3], [4]]})
    _, observed_containers = measure_json_complexity(json.loads(payload))
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_session_containers=observed_containers - 1,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == [payload]
    assert policy.violations == [
        LimitViolation(
            LimitKind.CONTAINERS, observed_containers - 1, observed_containers
        )
    ]
    assert session.new and dict(session) == {}


async def test_container_count_at_limit_is_accepted() -> None:
    payload = encode({"groups": [[1], [2]]})
    _, observed_containers = measure_json_complexity(json.loads(payload))
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_session_containers=observed_containers,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(payload))

    assert decoder.calls == [payload]
    assert policy.violations == []
    assert not session.new


async def test_byte_limit_takes_precedence_over_structure() -> None:
    inner = nested(1, 4)
    payload = encode({"a": inner})
    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        decoder=decoder,
        max_blob_size=len(payload) - 1,
        max_session_depth=1,
        on_limit_exceeded=policy,
    )

    await storage.load_session(make_request(payload))

    assert decoder.calls == []
    assert [v.kind for v in policy.violations] == [LimitKind.BYTES]


# ---------------------------------------------------------------------------
# Rejected payloads must never be written back, through the middleware
# ---------------------------------------------------------------------------


async def test_rejected_payload_is_not_saved_back() -> None:
    payload = encode({"secret": "do-not-persist"})
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(
        max_blob_size=len(payload) - 1, on_limit_exceeded=policy
    )
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.StreamResponse:
        session = await get_session(request)
        # The handler sees an empty fresh session, never the rejected data.
        assert session.new
        assert dict(session) == {}
        return web.Response(text="ok")

    response = await middleware(make_request(payload), handler)

    assert response.status == 200
    assert "Set-Cookie" not in response.headers
    assert [v.kind for v in policy.violations] == [LimitKind.BYTES]


async def test_default_policy_rejects_oversized_blob_with_413() -> None:
    payload = encode({"a": 1})
    storage = SimpleCookieStorage(max_blob_size=len(payload) - 1)
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.StreamResponse:
        await get_session(request)
        return web.Response(text="ok")

    with pytest.raises(web.HTTPRequestEntityTooLarge) as exc_info:
        await middleware(make_request(payload), handler)
    assert exc_info.value.status_code == 413


async def test_default_policy_rejects_too_deep_payload_with_400() -> None:
    payload = encode({"a": nested(1, 3)})
    observed_depth = measure_json_complexity(json.loads(payload))[0]
    storage = SimpleCookieStorage(max_session_depth=observed_depth - 1)
    middleware = session_middleware(storage)

    async def handler(request: web.Request) -> web.StreamResponse:
        await get_session(request)
        return web.Response(text="ok")

    with pytest.raises(web.HTTPBadRequest) as exc_info:
        await middleware(make_request(payload), handler)
    assert exc_info.value.status_code == 400


async def test_middleware_wires_policy_onto_storage() -> None:
    policy = RecordingPolicy()
    storage = SimpleCookieStorage(max_blob_size=1)
    session_middleware(storage, on_limit_exceeded=policy)
    assert storage.on_limit_exceeded is policy


# ---------------------------------------------------------------------------
# Encrypted cookie storage: the byte budget applies to the encrypted blob,
# before Fernet decryption and therefore before the JSON decoder runs.
# ---------------------------------------------------------------------------


async def test_encrypted_blob_over_limit_skips_decoder_and_decrypt(
    fernet_key: bytes = b"0123456789abcdef0123456789abcdef",
) -> None:
    fernet_storage = EncryptedCookieStorage(fernet_key)
    plaintext = encode({"a": 1})
    encrypted = fernet_storage._fernet.encrypt(plaintext.encode("utf-8")).decode(
        "utf-8"
    )

    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    limited = EncryptedCookieStorage(
        fernet_key,
        decoder=decoder,
        max_blob_size=len(encrypted) - 1,
        on_limit_exceeded=policy,
    )

    session = await limited.load_session(make_request(encrypted))

    assert decoder.calls == []
    assert policy.violations[0].kind is LimitKind.BYTES
    assert policy.violations[0].observed == len(encrypted.encode("utf-8"))
    assert session.new and dict(session) == {}


async def test_encrypted_valid_payload_still_loads(
    fernet_key: bytes = b"0123456789abcdef0123456789abcdef",
) -> None:
    storage = EncryptedCookieStorage(fernet_key)
    encrypted = storage._fernet.encrypt(
        encode({"a": 1}).encode("utf-8")
    ).decode("utf-8")

    session = await storage.load_session(make_request(encrypted))

    assert isinstance(session, Session)
    assert not session.new
    assert session["a"] == 1


async def test_encrypted_payload_depth_limit_runs_after_decrypt(
    fernet_key: bytes = b"0123456789abcdef0123456789abcdef",
) -> None:
    builder = EncryptedCookieStorage(fernet_key)
    deep = nested(1, 5)
    encrypted = builder._fernet.encrypt(
        encode({"a": deep}).encode("utf-8")
    ).decode("utf-8")

    decoder = RecordingDecoder()
    policy = RecordingPolicy()
    storage = EncryptedCookieStorage(
        fernet_key,
        decoder=decoder,
        max_session_depth=3,
        on_limit_exceeded=policy,
    )

    session = await storage.load_session(make_request(encrypted))

    # Decryption and JSON decoding succeeded; the structural check then
    # discards the decoded payload rather than persisting it.
    assert len(decoder.calls) == 1
    assert policy.violations == [
        LimitViolation(LimitKind.DEPTH, 3, measure_json_complexity(
            json.loads(decoder.calls[0])
        )[0])
    ]
    assert session.new and dict(session) == {}
