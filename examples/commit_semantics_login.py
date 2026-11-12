"""Login flow built on the session commit-semantics contract.

The point of this example is showing that a session save can fail at three
different stages and that the application's reaction differs per stage:

* :class:`SessionNotSavedError` -- nothing was persisted. The whole request
  (including the login side effects) may be retried.
* :class:`SessionPersistedError` -- the identity already exists in the
  storage backend, only the response cookie was lost. Login side effects
  such as billing an event or sending a welcome e-mail must NOT run again.
* :class:`SessionResponseLockedError` -- the response headers were already
  sent. The request cannot be repaired; only log and surface a 500.

Run with::

    python examples/commit_semantics_login.py
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

from aiohttp import web

from aiohttp_session import (
    CommitStage,
    SessionError,
    SessionNotSavedError,
    SessionPersistedError,
    SessionResponseLockedError,
    SimpleCookieStorage,
    get_session,
    new_session,
    session_middleware,
)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Pretend credentials store.
USERS = {"alice": "wonderland"}

# Non-idempotent side-effect ledgers. In a real application these would be
# rows in an operational database (audit table, mail queue, ...).
audit_log: list[str] = []
emails_sent: list[str] = []

# Identities for which the post-login effects already ran. This is the
# idempotency guard that makes a retry safe after a partial failure.
processed_identities: set[str] = set()


async def emit_login_side_effects(identity: str, username: str) -> None:
    """Run one-shot login effects, de-duplicated by session identity.

    The identity is generated before the session is saved and doubles as the
    idempotency key. When the storage reports a partial failure
    (``PERSISTED`` stage) the same committed identity is handed back, so a
    second invocation becomes a no-op instead of sending a duplicate welcome
    e-mail. A ``NOT_SAVED`` failure never reaches this function at all, so a
    client retry can perform the effects exactly once on the successful
    commit.
    """
    if identity in processed_identities:
        audit_log.append(f"duplicate suppressed for {identity}")
        return
    processed_identities.add(identity)
    audit_log.append(f"login audit for {username} ({identity})")
    emails_sent.append(f"welcome for {username} ({identity})")


def _error_payload(error: SessionError) -> dict[str, str]:
    return {
        "stage": error.stage.value,
        "retryable": str(error.retryable),
        "persisted": str(error.persisted),
        "response_mutable": str(error.response_mutable),
        "detail": str(error),
    }


@web.middleware
async def session_error_middleware(
    request: web.Request, handler: _Handler
) -> web.StreamResponse:
    """Translate typed session failures into deliberate HTTP responses.

    This middleware is registered *outside* the session middleware, so it
    sees the errors the session layer raises after the handler returned.
    """
    try:
        return await handler(request)
    except SessionNotSavedError as exc:
        # Nothing durable happened: the client may retry the request. Because
        # the identity was never committed, emit_login_side_effects() has no
        # record of it and the legitimate retry performs the effects once.
        return web.json_response(
            {"error": "session_not_saved", **_error_payload(exc)},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    except SessionPersistedError as exc:
        # The login already happened server-side. Mark the identity as
        # processed so no retry can re-run the side effects, and ask the
        # client to start a fresh request instead of blindly re-posting.
        identity = exc.identity
        if identity is not None:
            processed_identities.add(identity)
        return web.json_response(
            {"error": "session_persisted_cookie_lost", **_error_payload(exc)},
            status=HTTPStatus.CONFLICT,
        )
    except SessionResponseLockedError as exc:
        # The response is already on the wire (e.g. streaming started). There
        # is nothing the application can do except log and fail visibly.
        request.app["locked_failures"] += 1
        return web.json_response(
            {"error": "response_already_committed", **_error_payload(exc)},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


async def login(request: web.Request) -> web.Response:
    form = await request.post()
    username = str(form.get("name", ""))
    password = str(form.get("password", ""))
    if USERS.get(username) != password:
        return web.Response(text="invalid credentials", status=HTTPStatus.FORBIDDEN)

    # Always start a fresh session on login (session fixation guard).
    session = await new_session(request)
    # A stable identity generated up front doubles as the idempotency key for
    # the side effects below, including across retries of this very request.
    identity = f"login-{session.created}-{username}"
    session.set_new_identity(identity)
    session["user"] = username
    request["login_identity"] = identity
    request["login_username"] = username

    # No side effects here: the session middleware saves the session after
    # the handler returns, and the post-commit middleware below only runs the
    # one-shot effects once the storage commit has succeeded.
    raise web.HTTPFound("/profile")


@web.middleware
async def post_commit_side_effects(
    request: web.Request, handler: _Handler
) -> web.StreamResponse:
    """Run login effects only after the session save has fully committed.

    Positioned between the error middleware and the session middleware:

    * a :class:`SessionNotSavedError` propagates outward before any effect
      runs, so a retry stays clean;
    * a :class:`SessionPersistedError` means the identity is durable -- the
      effects run (or get de-duplicated) without needing the response cookie;
    * on full success the effects run once the inner chain (which performs
      the save) returns, including when the login view signals a redirect by
      raising :class:`aiohttp.web.HTTPFound`.
    """
    try:
        response = await handler(request)
    except SessionPersistedError as exc:
        identity = exc.identity or request.get("login_identity")
        if identity is not None:
            await emit_login_side_effects(
                identity, str(request.get("login_username", ""))
            )
        raise
    except web.HTTPException:
        # The session middleware has already saved the session by the time a
        # handler-raised HTTPException reaches here, so the commit succeeded.
        identity = request.get("login_identity")
        if identity is not None:
            await emit_login_side_effects(
                identity, str(request.get("login_username", ""))
            )
        raise

    identity = request.get("login_identity")
    if identity is not None:
        await emit_login_side_effects(
            identity, str(request.get("login_username", ""))
        )
    return response


async def profile(request: web.Request) -> web.Response:
    session = await get_session(request)
    user = session.get("user")
    if user is None:
        raise web.HTTPFound("/login")
    return web.json_response(
        {
            "user": user,
            "stage": CommitStage.COMMITTED.value,
            "audit_entries": len(audit_log),
            "emails_sent": len(emails_sent),
        }
    )


async def login_page(request: web.Request) -> web.Response:
    return web.Response(
        text=(
            '<form method="post" action="/login">'
            '<input name="name"><input name="password" type="password">'
            '<button>login</button></form>'
        ),
        content_type="text/html",
    )


def make_app() -> web.Application:
    app = web.Application(
        middlewares=[
            # Outermost: classify the typed errors into HTTP responses.
            session_error_middleware,
            # Runs one-shot effects once the storage commit is known.
            post_commit_side_effects,
            # Innermost: persists the session and raises the typed errors.
            session_middleware(SimpleCookieStorage()),
        ]
    )
    app["locked_failures"] = 0
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login)
    app.router.add_get("/profile", profile)
    return app


if __name__ == "__main__":
    web.run_app(make_app())
