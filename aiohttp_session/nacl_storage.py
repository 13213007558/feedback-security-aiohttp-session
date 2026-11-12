import binascii
import json
from collections.abc import Callable
from typing import Any

import nacl.exceptions
import nacl.secret
import nacl.utils
from aiohttp import web
from nacl.encoding import Base64Encoder

from . import AbstractStorage, Session
from .log import log


class NaClCookieStorage(AbstractStorage):
    """NaCl Encrypted JSON storage."""

    def __init__(
        self,
        secret_key: bytes,
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

        self._secretbox = nacl.secret.SecretBox(secret_key)

    def empty_session(self) -> Session:
        return self._new_session()

    async def load_session(self, request: web.Request) -> Session:
        cookie = self.load_cookie(request)
        if cookie is None:
            return self.empty_session()
        else:
            try:
                data = self._decoder(
                    self._secretbox.decrypt(
                        cookie.encode("utf-8"), encoder=Base64Encoder
                    ).decode("utf-8")
                )
                return self._load_session(None, data)
            except (binascii.Error, nacl.exceptions.CryptoError):
                log.warning(
                    "Cannot decrypt cookie value, " "create a new fresh session"
                )
                return self.empty_session()

    async def save_session(
        self, request: web.Request, response: web.StreamResponse, session: Session
    ) -> None:
        if session.empty or self._should_expire(session):
            return self.save_cookie(response, "")

        cookie_data = self._encoder(self._get_session_data(session)).encode("utf-8")
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
        self.save_cookie(
            response,
            self._secretbox.encrypt(cookie_data, nonce, encoder=Base64Encoder).decode(
                "utf-8"
            ),
            max_age=session.cookie_max_age,
        )
