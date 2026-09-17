"""The Telegram Bot API over plain HTTPS, rate-limited.

No bot framework: the runner needs five methods, and httpx is already in the
image. Long polling (`getUpdates`) rather than a webhook, because the runner
sits on a home machine with no public address.

RATE LIMITS. Telegram allows about 30 messages a second across all chats and
about one a second into any one chat; beyond that it answers 429 with a
`retry_after`. Every send goes through one throttle that keeps under both, and
a 429 is honoured and retried rather than dropped.

THE TOKEN is a password. It is part of every request URL, so neither URLs nor
raw exception messages are ever logged: errors pass through `_redact` first.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

import httpx

log = logging.getLogger("runner.telegram")

GLOBAL_PER_SECOND = 25
CHAT_MIN_INTERVAL = 1.05


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.code = code
        self.description = description


class Throttle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._global: list[float] = []
        self._chats: dict[int, float] = {}

    def wait(self, chat_id: int | None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._global = [t for t in self._global if now - t < 1.0]
                delay = 0.0
                if len(self._global) >= GLOBAL_PER_SECOND:
                    delay = 1.0 - (now - self._global[0])
                if chat_id is not None:
                    delay = max(delay, CHAT_MIN_INTERVAL - (now - self._chats.get(chat_id, 0.0)))
                if delay <= 0:
                    self._global.append(now)
                    if chat_id is not None:
                        self._chats[chat_id] = now
                    return
            time.sleep(min(delay, 1.0))


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self._http = httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0))
        self.throttle = Throttle()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def call(self, method: str, retries: int = 3, **params: Any) -> Any:
        for attempt in range(retries + 1):
            try:
                response = self._http.post(self._base + method, json=params)
                body = response.json()
            except (httpx.HTTPError, ValueError) as err:
                if attempt == retries:
                    raise TelegramError(method, None, self._redact(str(err))) from None
                time.sleep(2**attempt)
                continue
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after and attempt < retries:
                log.warning("telegram rate limit on %s: waiting %ss", method, retry_after)
                time.sleep(float(retry_after) + 0.5)
                continue
            raise TelegramError(method, code, self._redact(str(body.get("description"))))
        raise TelegramError(method, None, "retries exhausted")

    # --- methods ------------------------------------------------------------------

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            retries=0,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def send(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> dict[str, Any]:
        self.throttle.wait(chat_id)
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.throttle.wait(chat_id)
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        try:
            self.call("editMessageText", **params)
        except TelegramError as err:
            # Pressing a button that re-renders the same page is not an error.
            if "message is not modified" not in err.description:
                raise

    def answer(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.call("answerCallbackQuery", retries=0, callback_query_id=callback_id, text=text)
        except TelegramError:
            # An expired callback (older than ~15 min) cannot be answered; harmless.
            pass

    def set_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )

    def me(self) -> dict[str, Any]:
        return self.call("getMe")

    def close(self) -> None:
        self._http.close()
