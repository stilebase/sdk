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
    NOT_ASKED,
    PASS,
    SETTINGS_TIMEOUT,
    UNUSABLE,
    Answer,
    Asking,
    Capture,
    Copy,
    Decision,
    Engine,
    GateOptions,
    Hold,
    Settings,
    capture_body,
    decide_body,
    delivery,
    holdable,
    json_body,
    not_payable,
    read_answer,
    unknown_payment,
)
from ._http import LIMITS, budget, kept
from ._request import GateRequest


class AsyncGate(Engine):
    """`await gate.check(request)` says what to do with a request; `await gate.settle(...)` settles a held answer;
    `gate.capture(...)` sends a copy. None of them raises."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "async", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.AsyncClient | None = None
        self._settings_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        # GET/HEAD alike, no payment: one question at a time (the future, and when it was asked)
        self._asking: dict[str, tuple[asyncio.Future[Answer], float]] = {}
        self._captures: asyncio.Queue[dict[str, Any]] | None = None
        self._capturing: asyncio.Task[None] | None = None

    async def check(self, request: GateRequest) -> Decision:
        if not self.active:
            return PASS
        asking: Asking | None = None
        answered = False
        try:
            settings = await self._settings()
            before = self.before_asking(request, settings)
            if not isinstance(before, Asking) or settings is None:
                return before if not isinstance(before, Asking) else PASS
            asking = before
            body = decide_body(request, settings, self.options.assume_https)
            payment = request.payment is not None
            if payment or request.method.upper() not in ("GET", "HEAD") or asking.probe:
                # Only reads without a payment share an answer; a probe asks alone.
                answer = await self._ask("decide", body, settings.settle_timeout if payment else settings.timeout)
                answered = True
                return self.after_asking(request, settings, body, answer, asking)
            key = f"{request.method.upper()} {asking.key}"
            loop = asyncio.get_running_loop()
            shared = self._asking.get(key)
            if shared is not None and loop.time() - shared[1] > settings.timeout:
                # Waiting longer than its budget: the runtime may have cancelled it. Forgotten; this one asks again.
                self._asking.pop(key, None)
                shared = None
            if shared is not None:  # another request like this one is being asked about: its answer is ours
                self.release(asking)  # waiting on another's decision: this one asks nothing, counts nothing
                asking = None
                try:
                    answer = await asyncio.wait_for(asyncio.shield(shared[0]), settings.timeout)
                except asyncio.TimeoutError:
                    answer = UNUSABLE
                return self.after_asking(request, settings, body, answer, None) if answer.usable else PASS
            future: asyncio.Future[Answer] = loop.create_future()
            self._asking[key] = (future, loop.time())
            answer = UNUSABLE  # what waiters get if this request is cancelled: they pass, and nothing is counted
            try:
                answer = await self._ask("decide", body, settings.timeout)
                answered = True
            finally:
                if self._asking.get(key, (None, 0.0))[0] is future:
                    self._asking.pop(key, None)
                if not future.done():
                    future.set_result(answer)
            return self.after_asking(request, settings, body, answer, asking)
        except Exception as error:
            self.report(error)
            return unknown_payment() if request.payment is not None else PASS
        finally:
            if not answered:  # ended without Stilebase's answer (cancelled, a fault here): counts nothing
                self.release(asking)

    async def settle(self, hold: Hold, status: int, body: bytes) -> Decision:
        """The site answered a paid request (`body` its whole body, held): settle it (SPEC.md, section 7). `Pass`: send
        the held answer with its `response_headers`; anything else: send that instead."""
        if len(body) > hold.max_bytes or not holdable(status):
            return not_payable()
        try:
            settings = self.settings or Settings()
            url, data, headers = self.signed_post("settle", {**hold.body, "delivery": delivery(status, body)})
            request = self.http.post(url, content=data, headers=headers, timeout=budget(settings.settle_timeout))
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
            self._http = httpx.AsyncClient(transport=self.options.transport, follow_redirects=False, limits=LIMITS)
        return self._http

    async def _ask(self, name: str, body: dict[str, Any], seconds: float) -> Answer:
        """One call to Stilebase within `seconds`, connecting included; anything wrong is UNUSABLE. Past its budget the
        call isn't cancelled: it runs on in the background (at most KEEP_CONNECTING), keeping its connection."""
        try:
            url, data, headers = self.signed_post(name, body)
            call = asyncio.get_running_loop().create_task(
                self.http.post(url, content=data, headers=headers, timeout=kept(seconds))
            )
            self._tasks.add(call)
            call.add_done_callback(_settled_quietly)
            call.add_done_callback(self._tasks.discard)
            done, _ = await asyncio.wait({call}, timeout=seconds)  # a request cancelled meanwhile never cancels the call
            if not done:
                self.report(TimeoutError(f"Stilebase didn't answer within {round(seconds * 1000)} ms"))
                return UNUSABLE
            answer = call.result()
            return read_answer(answer.status_code, json_body(answer.content))
        except httpx.PoolTimeout as error:
            self.report(error)
            return NOT_ASKED  # waiting for a free connection is the gate's own wait: the breaker ignores it
        except Exception as error:
            self.report(error)
            return UNUSABLE

    async def _settings(self) -> Settings | None:
        if self.settings_at is None:
            await asyncio.wait({self._load()}, timeout=SETTINGS_TIMEOUT + 0.1)  # this request's deadline, never the load's
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
            fetch = self.http.get(self.api + self.path("settings"), headers=self.settings_headers(), timeout=SETTINGS_TIMEOUT)
            answer = await asyncio.wait_for(fetch, SETTINGS_TIMEOUT)  # 3 s in all, however slowly it arrives
            self.take_settings(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.settings_at = self.options.clock()
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


def _settled_quietly(task: asyncio.Task[Any]) -> None:
    """A call the gate stopped waiting for may fail later: its error is the gate's to ignore (it was reported, or it
    answered after its request had gone on)."""
    if not task.cancelled():
        task.exception()
