import base64
import json
from collections.abc import Callable
from typing import Any

from aiohttp import web
from cryptography import fernet
from cryptography.fernet import InvalidToken

from . import AbstractStorage, Session
from .log import log


class EncryptedCookieStorage(AbstractStorage):
    """Encrypted JSON storage."""

    def __init__(
        self,
        secret_key: str | bytes | bytearray | fernet.Fernet,
        *,
        cookie_name: str = "AIOHTTP_SESSION",
        domain: str | None = None,
        max_age: int | None = None,
        path: str = "/",
        secure: bool | None = None,
        httponly: bool = True,
        samesite: str | None = None,
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

        if isinstance(secret_key, fernet.Fernet):
            self._fernet = secret_key
        else:
            if isinstance(secret_key, (bytes, bytearray)):
                secret_key = base64.urlsafe_b64encode(secret_key)
            self._fernet = fernet.Fernet(secret_key)

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return self._new_session()
        else:
            try:
                # The Fernet token TTL alone would expire the whole
                # cookie on every save, losing the absolute deadline.
                # Session-level limits are enforced from the payload.
                data = self._decoder(
                    self._fernet.decrypt(cookie.encode("utf-8")).decode("utf-8")
                )
                return self._load_session(None, data)
            except InvalidToken:
                log.warning(
                    "Cannot decrypt cookie value, " "create a new fresh session"
                )
                return self._new_session()

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        if session.empty or self._should_expire(session):
            return self.save_cookie(response, "")

        cookie_data = self._encoder(self._get_session_data(session)).encode("utf-8")
        self.save_cookie(
            response,
            self._fernet.encrypt(cookie_data).decode("utf-8"),
            max_age=session.cookie_max_age,
        )
