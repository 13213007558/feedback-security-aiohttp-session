import base64
import json
from collections.abc import Callable
from typing import Any

from aiohttp import web
from cryptography import fernet
from cryptography.fernet import InvalidToken

from . import (
    AbstractStorage,
    LimitExceededHandler,
    Session,
    default_limit_exceeded_handler,
)
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
        max_blob_size: int | None = None,
        max_session_depth: int | None = None,
        max_session_containers: int | None = None,
        on_limit_exceeded: LimitExceededHandler = default_limit_exceeded_handler,
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
            max_blob_size=max_blob_size,
            max_session_depth=max_session_depth,
            max_session_containers=max_session_containers,
            on_limit_exceeded=on_limit_exceeded,
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
            return Session(None, data=None, new=True, max_age=self.max_age)

        rejected = await self._reject_oversized_blob(request, cookie)
        if rejected is not None:
            return rejected

        try:
            plaintext = self._fernet.decrypt(
                cookie.encode("utf-8"), ttl=self.max_age
            ).decode("utf-8")
        except InvalidToken:
            log.warning(
                "Cannot decrypt cookie value, " "create a new fresh session"
            )
            return Session(None, data=None, new=True, max_age=self.max_age)

        return await self._decode_session_text(request, plaintext)

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        if session.empty:
            return self.save_cookie(response, "", max_age=session.max_age)

        cookie_data = self._encoder(self._get_session_data(session)).encode("utf-8")
        self.save_cookie(
            response,
            self._fernet.encrypt(cookie_data).decode("utf-8"),
            max_age=session.max_age,
        )
