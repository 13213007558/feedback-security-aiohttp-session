aiohttp_session
===============
.. image:: https://github.com/aio-libs/aiohttp-session/actions/workflows/ci.yaml/badge.svg?branch=master
    :target: https://github.com/aio-libs/aiohttp-session/actions/workflows/ci.yaml
.. image:: https://codecov.io/github/aio-libs/aiohttp-session/coverage.svg?branch=master
    :target: https://codecov.io/github/aio-libs/aiohttp-session
.. image:: https://readthedocs.org/projects/aiohttp-session/badge/?version=latest
    :target: https://aiohttp-session.readthedocs.io/
.. image:: https://img.shields.io/pypi/v/aiohttp-session.svg
    :target: https://pypi.python.org/pypi/aiohttp-session

The library provides sessions for `aiohttp.web`__.

.. _aiohttp_web: https://aiohttp.readthedocs.io/en/latest/web.html

__ aiohttp_web_

Usage
-----

The library allows us to store user-specific data into a session object.

The session object has a dict-like interface (operations like
``session[key] = value``, ``value = session[key]`` etc. are present).


Before processing the session in a web-handler, you have to register the
*session middleware* in ``aiohttp.web.Application``.

A trivial usage example:

.. code:: python

    import time
    from cryptography import fernet
    from aiohttp import web
    from aiohttp_session import setup, get_session
    from aiohttp_session.cookie_storage import EncryptedCookieStorage


    async def handler(request):
        session = await get_session(request)
        last_visit = session['last_visit'] if 'last_visit' in session else None
        session['last_visit'] = time.time()
        text = 'Last visited: {}'.format(last_visit)
        return web.Response(text=text)


    def make_app():
        app = web.Application()
        fernet_key = fernet.Fernet.generate_key()
        f = fernet.Fernet(fernet_key)
        setup(app, EncryptedCookieStorage(f))
        app.router.add_get('/', handler)
        return app


    web.run_app(make_app())


All storages use an HTTP Cookie named ``AIOHTTP_SESSION`` for storing
data. This can be modified by passing the keyword argument ``cookie_name`` to
the storage class of your choice.

Available session storages are:

* ``aiohttp_session.SimpleCookieStorage()`` -- keeps session data as a
  plain JSON string in the cookie body. Use the storage only for testing
  purposes, it's very non-secure.

* ``aiohttp_session.cookie_storage.EncryptedCookieStorage(secret_key)``
  -- stores the session data into a cookie as ``SimpleCookieStorage`` but
  encodes it via AES cipher. ``secrect_key`` is a ``bytes`` key for AES
  encryption/decryption, the length should be 32 bytes.

  Requires ``cryptography`` library::

      $ pip install aiohttp_session[secure]

* ``aiohttp_session.redis_storage.RedisStorage(redis_pool)`` -- stores
  JSON encoded data in *redis*, keeping only the redis key (a random UUID) in
  the cookie. ``redis_pool`` is a ``redis`` object, created by
  ``await aioredis.from_url(...)`` call.

      $ pip install aiohttp_session[aioredis]


One-time confirmation challenges
--------------------------------

Sensitive operations guarded by a one-time confirmation challenge can
use the optional ``aiohttp_session.challenge`` module.  The challenge
identifier remains an ordinary session key; the claim/completion state
is kept in a store so concurrent requests cannot both consume the
challenge and a failed session save cannot reopen a consumed one::

    from aiohttp_session.challenge import MemoryChallengeStore, run_session_challenge

    store = MemoryChallengeStore()

    async def confirm(request):
        session = await get_session(request)
        result = await run_session_challenge(
            session, store, "confirm_challenge",
            claimant=request["user_id"],
            operation=perform_sensitive_operation,
        )
        if not result.authorized:
            raise web.HTTPConflict()
        return web.Response(text="confirmed")

The in-memory store is for a single process; multi-process deployments
need an ``AbstractChallengeStore`` implementation backed by an atomic
shared primitive.

Developing
----------

Install for local development::

    $ make setup

Run linters::

    $ make lint

Run tests::

    $ make test


Third party extensions
----------------------

* `aiohttp_session_mongo
  <https://github.com/alexpantyukhin/aiohttp-session-mongo>`_

* `aiohttp_session_dynamodb
  <https://github.com/alexpantyukhin/aiohttp-session-dynamodb>`_

* `aiohttp_session_firestore
  <https://github.com/dcgudeman/aiohttp-session-firestore>`_


License
-------

``aiohttp_session`` is offered under the Apache 2 license.
