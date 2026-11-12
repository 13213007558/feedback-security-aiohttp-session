.. _aiohttp-session-reference:


===========
 Reference
===========

.. module:: aiohttp_session
.. currentmodule:: aiohttp_session
.. highlight:: python


Public functions
================

.. function:: get_session(request)

   A :ref:`coroutine<coroutine>` for getting session instance from
   request object.

   See example below in :ref:`Session<aiohttp-session-session>`
   section for :func:`get_session` usage.

.. function:: new_session(request)

   A :ref:`coroutine<coroutine>` for getting a new session regardless
   of whether a cookie exists.

   .. warning::

      Always use :func:`new_session` instead of :func:`get_session`
      in your login views to guard against Session Fixation attacks!

   Example usage::

      from aiohttp_session import new_session

      async def handler(request):
          session = await new_session(request)
          session.new == True # This will always be True

.. function:: session_middleware(storage)

   Session middleware factory.

   Create session middleware to pass into
   :class:`aiohttp.web.Application` constructor.

   *storage* is a session storage instance (object used to store
   session data into cookies, Redis, database etc., class is derived
   from :class:`AbstractStorage`).

   .. seealso:: :ref:`aiohttp-session-storage`

   .. note:: :func:`setup` is new-fashion way for library setup.

.. function:: setup(app, storage)

   Setup session support for given *app*.

   The function is shortcut for::

      app.middlewares.append(session_middleware(storage))

   *app* is :class:`aiohttp.web.Application` instance.

   *storage* is a session storage instance (object used to store
   session data into cookies, Redis, database etc., class is derived
   from :class:`AbstractStorage`).

   .. seealso:: :ref:`aiohttp-session-storage`


.. _aiohttp-session-commit-semantics:

Save failures and commit semantics
==================================

A changed session is saved by the middleware after the handler returns.
The save goes through three observable stages, reported by
:class:`CommitStage`:

1. :pyattr:`CommitStage.NOT_SAVED` -- the change exists only in the
   in-memory :class:`Session`; the storage backend was never reached or
   refused the write. Nothing durable happened, so the request is safe to
   retry.

2. :pyattr:`CommitStage.PERSISTED` -- the backend committed the change (a
   Redis/Memcached record exists, or the cookie payload was produced) but
   the response could not be updated with the matching cookie. The
   server-side identity already exists, so non-idempotent login side
   effects (audit entries, welcome e-mails, ...) must not run again.

3. :pyattr:`CommitStage.COMMITTED` -- the response was already prepared
   (headers sent, e.g. a streaming response) and cannot carry a session
   cookie at all. The request cannot be repaired.

Every failure inherits from :class:`SessionError`, which itself is a
:class:`RuntimeError` (so existing ``except RuntimeError`` code keeps
working). Inspect :attr:`SessionError.stage`,
:attr:`SessionError.retryable`, :attr:`SessionError.persisted` and
:attr:`SessionError.response_mutable` instead of catching every failure
as a generic save error.

.. class:: CommitStage

   Enumeration of how far a session change got through the save
   pipeline. Members: ``NOT_SAVED``, ``PERSISTED``, ``COMMITTED``.

.. class:: SessionError(message=None)

   Base class of every session failure. Attributes:

   * ``stage`` -- the :class:`CommitStage` reached when it failed;
   * ``retryable`` -- ``True`` while nothing durable was committed;
   * ``persisted`` -- ``True`` once the backend accepted the change;
   * ``response_mutable`` -- ``True`` while the response may still carry
     the session cookie.

.. class:: SessionNotSavedError(message=None)

   The change never reached the storage backend. ``stage`` is
   ``NOT_SAVED`` and ``retryable`` is ``True``.

.. class:: SessionPersistedError(message=None, *, identity=None)

   The backend stored the change but the response could not be updated.
   ``stage`` is ``PERSISTED`` and ``retryable`` is ``False``. The
   optional *identity* is the storage key that was committed, suitable
   as an idempotency key for post-login side effects.

.. class:: SessionResponseLockedError(message=None)

   The response was already prepared and cannot carry a cookie. ``stage``
   is ``COMMITTED``, ``retryable`` is ``False`` and
   ``response_mutable`` is ``False``.

A typical application installs an outer middleware that maps the typed
errors to distinct responses and keeps login side effects idempotent via
the session identity. A complete, runnable version lives in
``examples/commit_semantics_login.py``; the skeleton is::

   from aiohttp import web
   from aiohttp_session import (
       session_middleware, SessionNotSavedError,
       SessionPersistedError, SessionResponseLockedError,
   )

   @web.middleware
   async def session_error_middleware(request, handler):
       try:
           return await handler(request)
       except SessionNotSavedError:
           # nothing was written: the client may simply retry
           raise web.HTTPServiceUnavailable(text="retry the login")
       except SessionPersistedError as exc:
           # identity already stored: do NOT re-run login effects,
           # ask for a fresh request instead
           mark_effects_done(exc.identity)
           raise web.HTTPConflict(text="session saved, sign in again")
       except SessionResponseLockedError:
           # headers already sent: only logging can help
           raise web.HTTPInternalServerError()

   app = web.Application(middlewares=[
       session_error_middleware,
       session_middleware(storage),
   ])


.. _aiohttp-session-session:

Session
=======

.. class:: Session

   Client's session, a namespace that is valid for some period of
   continual activity that can be used to represent a user's
   interaction with a web application.

   .. warning::

      Never create :class:`Session` instances by hands, retieve those
      by :func:`get_session` call.

   The :class:`Session` is a :class:`MutableMapping`, thus it supports
   all dictionary methods, along with some extra attributes and
   methods::

      from aiohttp_session import get_session

      async def handler(request):
          session = await get_session(request)
          session['key1'] = 'value 1'
          assert 'key2' in session
          assert session['key2'] == 'value 2'
          # ...

   .. attribute:: created

      Creation UNIX TIMESTAMP, the value returned by :func:`time.time`
      for very first access to the session object.

   .. attribute:: identity

      Client's identity. It may be cookie name or database
      key. Read-only property. For change use :func:`Session.set_new_identity`.

   .. attribute:: new

      A boolean. If new is ``True``, this session is new. Otherwise,
      it has been constituted from data that was already serialized.

   .. method:: changed()

      Call this when you mutate a mutable value in the session
      namespace. See the note below for details on when, and why
      you should call this.

      .. note::

         Keys and values of session data must be JSON serializable
         when using one of the included storage backends. This
         means, typically, that they are instances of basic types of
         objects, such as strings, lists, dictionaries, tuples,
         integers, etc. If you place an object in a session data key
         or value that is not JSON serializable, an error will be raised
         when the session is serialized.

         If you place a mutable value (for example, a list or a
         dictionary) in a session object, and you subsequently mutate
         that value, you must call the changed() method of the session
         object. In this case, the session has no way to know that is
         was modified. However, when you modify a session object
         directly, such as setting a value (i.e., ``__setitem__``), or
         removing a key (e.g., ``del`` or ``pop``), the session will
         automatically know that it needs to re-serialize its data,
         thus calling :meth:`changed` is unnecessary. There is no harm
         in calling :meth:`changed` in either case, so when in doubt,
         call it after you've changed sessioning data.

   .. method:: invalidate()

      Call this when you want to invalidate the session (dump all
      data, and -- perhaps -- set a clearing cookie).

   .. method:: set_new_identity(identity)

      Call this when you want to change the :py:attr:`identity`.

      .. warning::

         Never change :py:attr:`identity` of a session which is not new.

.. _aiohttp-session-storage:

Session storages
================

:mod:`aiohttp_session` uses storages to save/load persistend session data.

Abstract Storage
----------------

All storages should be derived from :class:`AbstractStorage` and
implement both :meth:`~AbstractStorage.load_session` and
:meth:`~AbstractStorage.save_session` methods.

.. class:: AbstractStorage(cookie_name="AIOHTTP_SESSION", *, \
                           domain=None, max_age=None, path='/', \
                           secure=None, httponly=True, samesite=None, \
                           encoder=json.dumps, decoder=json.loads)

   Base class for session storage implementations.

   It uses HTTP cookie for storing at least the key for session data, but
   some implementations may save all session info into cookies.

   *cookie_name* -- name of cookie used for saving session data.

   *domain* -- cookie's domain, :class:`str` or ``None``.

   *max_age* -- cookie's max age, :class:`int` or ``None``.

   *path* -- cookie's path, :class:`str` or ``None``.

   *secure* -- cookie's secure flag, :class:`bool` or ``None`` (the
   same as ``False``).

   *httponly* -- cookie's http-only flag, :class:`bool` or ``None`` (the
   same as ``False``).

   *samesite* -- cookie's samesite flag, :class:`str` or ``None``

   *encoder* -- session serializer.  A callable with the following
   signature: `def encode(param: Any) -> str: ...`.  Default is
   :func:`json.dumps`.

   *decoder* -- session deserializer.  A callable with the following
   signature: `def decode(param: str) -> Any: ...`.  Default is
   :func:`json.loads`.

   .. versionadded:: 2.3

      Added *encoder* and *decoder* parameters.

   .. attribute:: max_age

      Maximum age for session data, :class:`int` seconds or ``None``
      for "session cookie" which last until you close your browser.

   .. attribute:: cookie_name

      Name of cookie used for saving session data.

   .. attribute:: cookie_params

      :class:`dict` of cookie params: *domain*, *max_age*, *path*,
      *secure*, *httponly* and *samesite*.

   .. attribute:: encoder

      The JSON serializer that will be used to dump session cookie data.

      .. versionadded:: 2.3

   .. attribute:: decoder

      The JSON deserializer that will be used to load session cookie data.

      .. versionadded:: 2.3

   .. method:: new_session()

      A :ref:`coroutine<coroutine>` for getting a new session regardless
      of whether a cookie exists.

      Return :class:`Session` instance.

   .. method:: load_session(request)

      An *abstract* :ref:`coroutine<coroutine>`, called by internal
      machinery for retrieving :class:`Session` object for given
      *request* (:class:`aiohttp.web.Request` instance).

      Return :class:`Session` instance.

   .. method:: save_session(request, response, session)

      An *abstract* :ref:`coroutine<coroutine>`, called by internal
      machinery for storing *session* (:class:`Session`) instance for
      given *request* (:class:`aiohttp.web.Request`) using *response*
      (:class:`aiohttp.web.StreamResponse` or descendants).

      Implementations follow a two-phase commit order and classify
      failures with the :ref:`typed session errors
      <aiohttp-session-commit-semantics>`: persist to the backend first,
      then mutate *response*; raise
      :class:`~aiohttp_session.SessionNotSavedError` when the backend
      commit failed and :class:`~aiohttp_session.SessionPersistedError`
      when it succeeded but the cookie could not be attached. The
      middleware raises
      :class:`~aiohttp_session.SessionResponseLockedError` itself when
      *response* is already prepared, and wraps any unclassified
      exception as :class:`~aiohttp_session.SessionNotSavedError`.

   .. method:: load_cookie(request)

      A helper for loading cookie (:class:`http.cookies.SimpleCookie`
      instance) from *request* (:class:`aiohttp.web.Request`).

   .. method:: save_cookie(response, cookie_data, *, max_age=None)

      A helper for saving *cookie_data* (:class:`str`) into *response*
      (:class:`aiohttp.web.StreamResponse` or descendants).

      *max_age* is cookie lifetime given from session. Storage default
      is used if the value is ``None``.

Simple Storage
--------------

For testing purposes there is :class:`SimpleCookieStorage`. It stores
session data as unencrypted and unsigned JSON data in browser cookies,
so it's totally insecure.

.. warning:: Never use this storage on production!!! It's highly insecure!!!

To use the storage you should push it into
:func:`session_middleware`::

   aiohttp_session.setup(app, aiohttp_session.SimpleCookieStorage())

.. class:: SimpleCookieStorage(*, \
                               cookie_name="AIOHTTP_SESSION", \
                               domain=None, max_age=None, path='/', \
                               secure=None, httponly=True, samesite=None, \
                               encoder=json.dumps, decoder=json.loads)

   Create unencrypted cookie storage.

   The class is inherited from :class:`AbstractStorage`.

   Parameters are the same as for :class:`AbstractStorage`
   constructor.


.. module:: aiohttp_session.cookie_storage
.. currentmodule:: aiohttp_session.cookie_storage


Cookie Storage
--------------

The storage that saves session data in HTTP cookies as
:class:`~cryptography.fernet.Fernet` encrypted data.

To use the storage you should push it into
:func:`~aiohttp_session.session_middleware`::

   app = aiohttp.web.Application(middlewares=[
       aiohttp_session.cookie_storage.EncryptedCookieStorage(
           b'Thirty  two  length  bytes  key.')])

.. class:: EncryptedCookieStorage(secret_key, *, \
                                  cookie_name="AIOHTTP_SESSION", \
                                  domain=None, max_age=None, path='/', \
                                  secure=None, httponly=True, samesite=None, \
                                  encoder=json.dumps, decoder=json.loads)

   Create encryted cookies storage.

   The class is inherited from :class:`~aiohttp_session.AbstractStorage`.

   *secret_key* is :class:`bytes` secret key with length of 32, used
   for encoding or base-64 encoded :class:`str` one.

   Other parameters are the same as for
   :class:`~aiohttp_session.AbstractStorage` constructor.

   .. note::

      For key generation use
      :meth:`cryptography.fernet.Fernet.generate_key` method.


.. module:: aiohttp_session.cookie_storage
.. currentmodule:: aiohttp_session.cookie_storage


NaCl Storage
--------------

The storage that saves session data in HTTP cookies as
:class:`~nacl.secret.SecretBox` encrypted data.

To use the storage you should push it into
:func:`~aiohttp_session.session_middleware`::

   app = aiohttp.web.Application(middlewares=[
       aiohttp_session.nacl_storage.NaClCookieStorage(
           b'Thirty  two  length  bytes  key.'])

.. class:: NaClCookieStorage(secret_key, *, \
                                  cookie_name="AIOHTTP_SESSION", \
                                  domain=None, max_age=None, path='/', \
                                  secure=None, httponly=True, samesite=None, \
                                  encoder=json.dumps, decoder=json.loads)

   Create encryted cookies storage.

   The class is inherited from :class:`~aiohttp_session.AbstractStorage`.

   *secret_key* is :class:`bytes` secret key with length of 32, used
   for encoding.

   Other parameters are the same as for
   :class:`~aiohttp_session.AbstractStorage` constructor.


.. module:: aiohttp_session.redis_storage
.. currentmodule:: aiohttp_session.redis_storage


Redis Storage
-------------

The storage that stores session data in Redis database and
keeps only Redis keys (UUIDs actually) in HTTP cookies.

It operates with Redis database via :class:`redis.asyncio.Redis`.

To use the storage you need setup it first::

   redis = await aioredis.from_url("redis://127.0.0.1:6379")
   storage = aiohttp_session.redis_storage.RedisStorage(redis)
   aiohttp_session.setup(app, storage)


.. class:: RedisStorage(redis_pool, *, \
                        cookie_name="AIOHTTP_SESSION", \
                        domain=None, max_age=None, path='/', \
                        secure=None, httponly=True, samesite=None, \
                        key_factory=lambda: uuid.uuid4().hex, \
                        encoder=json.dumps, decoder=json.loads)

   Create Redis storage for user session data.

   The class is inherited from :class:`~aiohttp_session.AbstractStorage`.

   *redis_pool* is a :class:`~redis.asyncio.Redis` which should be
   created by :func:`~redis.asyncio.from_url` call, e.g.::

      redis = await aioredis.from_url("redis://localhost:6379")
      storage = aiohttp_session.redis_storage.RedisStorage(redis)

   Other parameters are the same as for
   :class:`~aiohttp_session.AbstractStorage` constructor.


Memcached Storage
-----------------

The storage that stores session data in Memcached and
keeps only keys (UUIDs actually) in HTTP cookies.

It operates with Memcached database via :class:`aiomcache.Client`.

To use the storage you need setup it first::

   mc = aiomcache.Client('localhost', 11211)
   storage = aiohttp_session.memcached_storage.Client(mc)
   aiohttp_session.setup(app, storage)

.. versionadded:: 1.2

.. class:: MemcachedStorage(memcached_conn, *, \
                            cookie_name="AIOHTTP_SESSION", \
                            domain=None, max_age=None, path='/', \
                            secure=None, httponly=True, samesite=None, \
                            key_factory=lambda: uuid.uuid4().hex, \
                            encoder=json.dumps, decoder=json.loads)

   Create Memcached storage for user session data.

   The class is inherited from :class:`~aiohttp_session.AbstractStorage`.

   *memcached_conn* is a :class:`~aiomcache.Client` instance::

      mc = await aiomcache.Client('localhost', 6379)
      storage = aiohttp_session.memcached_storage.MemcachedStorage(mc)

   Other parameters are the same as for
   :class:`~aiohttp_session.AbstractStorage` constructor.
