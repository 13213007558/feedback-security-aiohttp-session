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


.. _aiohttp-session-challenge:

One-time session challenges
===========================

``aiohttp_session.challenge`` is an optional module for sensitive
operations guarded by a one-time confirmation challenge.  The challenge
identifier lives in the session as an ordinary key; the authoritative
one-time state lives in a separate challenge store so that concurrent
requests cannot both consume it and so that a failed session save
cannot make a consumed challenge available again.

.. code-block:: python

   from aiohttp_session import get_session
   from aiohttp_session.challenge import (
       MemoryChallengeStore,
       RetryPolicy,
       run_session_challenge,
   )

   challenge_store = MemoryChallengeStore()

   async def confirm(request):
       session = await get_session(request)
       result = await run_session_challenge(
           session,
           challenge_store,
           "confirm_challenge",
           claimant=request["user_id"],
           operation=perform_sensitive_operation,
           retry_policy=RetryPolicy.NEVER,
       )
       if not result.authorized:
           raise web.HTTPConflict(text="challenge is not available")
       return web.Response(text="confirmed")

The challenge identifier is issued and stored under a normal session key,
for example::

   challenge_id = await issue_session_challenge(
       session, challenge_store, "confirm_challenge", ttl=300
   )

No other session key changes behaviour.  Existing session storages
continue to work unchanged; applications that do not need one-time
challenge semantics do not need this module.

State transitions are::

   AVAILABLE --claim--> CLAIMED --complete(succeeded)--> COMPLETED
                                 |
                                 +--complete(failed)--> FAILED
                                                    or RETRYABLE

``COMPLETED`` and ``FAILED`` are terminal.  The retry policy is fixed
on the first successful claim.  ``RetryPolicy.NEVER`` makes an
operation failure terminal for every request.  With
``RetryPolicy.SAME_CLAIMANT`` a failure makes the challenge
``RETRYABLE`` for the original claimant only; it is never restored to
``AVAILABLE`` and another request cannot use it.  A later request
cannot loosen the policy chosen by the first claim.

Completion must be recorded before the session is saved.  If the
session save fails after a completion, the challenge remains
``COMPLETED`` or ``FAILED``; a stale session value that still contains
the identifier is denied by the store.

.. class:: MemoryChallengeStore(*, clock=None, token_factory=None)

   In-memory implementation for a single process.  Transitions are
   serialized with a thread lock, so they are atomic for coroutines in
   one event loop and for threads in one process.  It does not
   coordinate separate processes or hosts.  Multi-process deployments
   must provide their own :class:`AbstractChallengeStore` backed by an
   atomic shared primitive such as a Redis Lua script or a
   compare-and-set database operation.

   Pass one instance to every request; a new store per request would
   defeat the one-time guarantee.  Discarded identifiers remain
   reserved as tombstones for the lifetime of the store.

.. class:: AbstractChallengeStore

   Optional storage contract for challenge state.  Implementations must
   make claim and completion atomic compare-and-set operations.
   Loading a record, checking it in application code, and saving it
   back is not a valid implementation.

   .. method:: issue_challenge(challenge_id, *, ttl=None)

      Create an AVAILABLE challenge.  Return ``True`` on success.
      Never reset an existing challenge, whatever its state.

   .. method:: claim_challenge(challenge_id, *, claimant, retry_policy=RetryPolicy.NEVER)

      Atomically claim an AVAILABLE challenge for *claimant*.  At most
      one caller receives a granted result.  The retry policy is
      recorded by the first successful claim.

   .. method:: complete_challenge(challenge_id, claim_token, *, outcome)

      Record the operation outcome.  A missing or mismatched claim
      token never changes state.  See :class:`CompletionResult`.

   .. method:: snapshot(challenge_id)

      Return diagnostic state, or ``None`` for an unknown challenge.
      Do not use a snapshot for authorization decisions.

   .. method:: discard_challenge(challenge_id)

      Remove a challenge during explicit cleanup.  Never use this as
      failure handling to make a consumed challenge available again.

.. class:: ClaimResult

   Result of a claim.  ``granted`` is true at most once per
   AVAILABLE challenge (or for a permitted same-claimant retry).
   ``claim_token`` is present only for a granted claim and must not be
   exposed to other requests.

.. class:: CompletionResult

   Result of a completion.  ``completed`` says whether the outcome was
   recorded.  ``state`` is the resulting :class:`ChallengeState`
   (``COMPLETED``, ``FAILED``, or ``RETRYABLE``).  A denied completion
   reports a :class:`CompletionDeniedReason` and leaves state unchanged.

.. class:: ChallengeExecution

   Explicit execution result.  ``authorized`` distinguishes a denied
   claim from an operation that legitimately returned ``None``.
   ``state`` is the challenge state after completion (``None`` when
   unauthorized).  Check ``state`` when the application must treat a
   completion the store refused to record as a hard failure.

.. class:: ChallengeState

   ``AVAILABLE``, ``CLAIMED``, ``RETRYABLE``, ``COMPLETED`` or
   ``FAILED``.

.. class:: RetryPolicy

   ``NEVER`` (default) or ``SAME_CLAIMANT``.

.. function:: claim_session_challenge(session, store, key, *, claimant, retry_policy=RetryPolicy.NEVER)

   Read the challenge identifier stored under the ordinary session
   *key* and claim it.  Return a :class:`ChallengeLease`, or ``None``
   when the key is missing or the claim is denied.  The session mapping
   is not mutated.

.. function:: complete_session_challenge(lease, session, key, outcome)

   Record the outcome first, then remove the ordinary session key when
   the challenge is terminal.  A ``RETRYABLE`` challenge keeps the key
   for the same claimant.  The returned :class:`CompletionResult` makes
   the final state explicit.

.. function:: run_session_challenge(session, store, key, *, claimant, operation, retry_policy=RetryPolicy.NEVER)

   Claim, run *operation*, and complete.  Return a
   :class:`ChallengeExecution`; ``authorized`` is false when the claim
   is denied, without executing *operation*.  If the operation raises,
   the challenge is marked failed before the exception propagates.
   A cancellation is not treated as an operation failure.

.. function:: issue_session_challenge(session, store, key, *, challenge_id=None, ttl=None)

   Issue a new challenge and store its identifier under the ordinary
   session *key*.  Return the identifier, or ``None`` when the key is
   already occupied or the store rejects it.

.. function:: challenge_expired(result)

   Return whether a denied claim or completion was caused by challenge
   expiry.


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
