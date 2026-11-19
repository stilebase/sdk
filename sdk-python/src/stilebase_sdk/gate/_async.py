"""The gate for async sites (ASGI: FastAPI, Starlette, Litestar…): the calls to Stilebase with httpx's AsyncClient,
the settings refreshed and the captures sent in background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any

import httpx

from ._core import (
    CAPTURE_QUEUE,
    CAPTURE_TIMEOUT,
    PASS,
    SETTINGS_TIMEOUT,
    UNUSABLE,
    Answer,
    Capture,
    Copy,
    Decision,
    Engine,
    GateOptions,
    Hold,
    Pass,
    Settings,
    capture_body,
    decide_body,
    delivery,
    json_body,
    read_answer,
    remember_key,
    too_large,
    unknown_payment,
)
from ._request import GateRequest


class AsyncGate(Engine):
    """`await gate.check(request)` says what to do with a request; `await gate.settle(...)` settles a held answer;
    `gate.capture(...)` sends a copy. None of them raises."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "async", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.AsyncClient | None = None
        self._settings_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._asking: dict[str, asyncio.Future[Answer]] = {}  # same remember key, no payment: one question at a time
        self._captures: asyncio.Queue[dict[str, Any]] | None = None
        self._capturing: asyncio.Task[None] | None = None

    async def check(self, request: GateRequest) -> Decision:
        if not self.active:
            return PASS
        try:
            settings = await self._settings()
            decision = self.before_asking(request, settings)
            if decision is not None or settings is None:
                return decision or PASS
            body = decide_body(request, settings, self.options.assume_https)
            if request.payment is not None:
                answer = await self._ask("decide", body, settings.settle_timeout)
                return self.after_asking(request, settings, body, answer)
            if request.method.upper() not in ("GET", "HEAD"):  # only reads are alike enough to share an answer
                return self.after_asking(request, settings, body, await self._ask("decide", body, settings.timeout))
            key = f"{request.method.upper()} {remember_key(request, settings)}"
            asking = self._asking.get(key)
            if asking is not None:  # another request like this one is being asked about: its answer is ours
                try:
                    answer = await asyncio.wait_for(asyncio.shield(asking), settings.timeout)
                except Exception:
                    answer = UNUSABLE
                if not answer.usable:
                    return PASS
                if isinstance(answer.decision, Pass) and answer.decision.capture is not None:
                    # A capture is for the request it was asked about, never for the ones that shared its answer.
                    shared = Pass(answer.decision.response_headers, answer.decision.hold, None)
                    answer = Answer(True, shared, answer.remember)
                return self.after_asking(request, settings, body, answer)
            future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
            self._asking[key] = future
            answer = UNUSABLE
            try:
                answer = await self._ask("decide", body, settings.timeout)
            finally:
                self._asking.pop(key, None)
                if not future.done():
                    future.set_result(answer)
            return self.after_asking(request, settings, body, answer)
        except Exception as error:
            self.report(error)
            return unknown_payment() if request.payment is not None else PASS

    async def settle(self, hold: Hold, status: int, body: bytes) -> Decision:
        """The site answered a paid request (`body` its whole body, held): settle it (SPEC.md, section 7). `Pass`: send
        the held answer with its `response_headers`; anything else: send that instead."""
        if len(body) > hold.max_bytes:
            return too_large()
        try:
            settings = self.settings or Settings()
            url, data, headers = self.signed_post("settle", {**hold.body, "delivery": delivery(status, body)})
            request = self.http.post(url, content=data, headers=headers, timeout=settings.settle_timeout)
            answer = await asyncio.wait_for(request, settings.settle_timeout)
            return self.settled(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)
            return unknown_payment()

    def capture(self, order: Capture, status: int, headers: Sequence[tuple[str, str]], copy: Copy, ttfb_ms: int) -> None:
        """Send a copy of the site's answer in the background (SPEC.md, section 9): one at a time, at most
        CAPTURE_QUEUE waiting, the rest and any failure dropped."""
        try:
            payload = capture_body(order.token, status, headers, copy.body, copy.size, copy.truncated, ttfb_ms)
            if self._captures is None:
                self._captures = asyncio.Queue(CAPTURE_QUEUE)
            self._captures.put_nowait(payload)
            if self._capturing is None or self._capturing.done():
                self._capturing = self._spawn(self._send_captures())
        except asyncio.QueueFull:
            pass
        except Exception as error:
            self.report(error)

    async def aclose(self) -> None:
        """Wait for background work, then close the HTTP client (e.g. at shutdown, or in tests)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks))
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self.options.transport, follow_redirects=False)
        return self._http

    async def _ask(self, name: str, body: dict[str, Any], budget: float) -> Answer:
        """One call to Stilebase within `budget` seconds, connecting included; anything wrong is UNUSABLE."""
        try:
            url, data, headers = self.signed_post(name, body)
            answer = await asyncio.wait_for(self.http.post(url, content=data, headers=headers, timeout=budget), budget)
            return read_answer(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)
            return UNUSABLE

    async def _settings(self) -> Settings | None:
        if not self.settings_at:
            await asyncio.wait({self._load()}, timeout=SETTINGS_TIMEOUT)  # this request's deadline, never the load's
        elif self.settings_due():
            self._load()
        return self.settings

    # The load runs as a task of its own: a request waiting on it that is cancelled (its client gone) never cancels it.
    def _load(self) -> asyncio.Task[None]:
        if self._settings_task is None or self._settings_task.done():
            self._settings_task = self._spawn(self._load_settings())
        return self._settings_task

    async def _load_settings(self) -> None:
        try:
            answer = await self.http.get(
                self.api + self.path("settings"), headers=self.settings_headers(), timeout=SETTINGS_TIMEOUT
            )
            self.take_settings(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.settings_at = self.options.now()
            self.report(error)  # keep the last good settings, if any

    async def _send_captures(self) -> None:
        assert self._captures is not None
        while not self._captures.empty():
            payload = self._captures.get_nowait()
            try:
                url, data, headers = self.signed_post("captures", payload)
                request = self.http.post(url, content=data, headers=headers, timeout=CAPTURE_TIMEOUT)
                await asyncio.wait_for(request, CAPTURE_TIMEOUT)
            except Exception as error:
                self.report(error)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)  # keep a reference until it's done, or it may be collected mid-flight
        task.add_done_callback(self._tasks.discard)
        return task
