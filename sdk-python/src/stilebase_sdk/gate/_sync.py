"""The gate for sync sites (WSGI: Flask, Django…): the calls to Stilebase with httpx's Client, the settings refreshed
and the captures sent on background threads.

Unlike the async gate, concurrent requests like each other aren't folded into one question: each thread asks on its
own (a WSGI server's threads would wait on each other's locks, inside their own budget, for little gain)."""

from __future__ import annotations

import queue
import threading
from collections.abc import Sequence
from typing import Any

import httpx

from ._core import (
    CAPTURE_QUEUE,
    CAPTURE_TIMEOUT,
    LIMITS,
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
from ._request import GateRequest


class SyncGate(Engine):
    """`gate.check(request)` says what to do with a request; `gate.settle(...)` settles a held answer; `gate.capture(...)`
    sends a copy. None of them raises. Safe to share between threads."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "sync", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http = httpx.Client(transport=self.options.transport, follow_redirects=False, limits=LIMITS)
        self._settings_lock = threading.Lock()
        self._refreshing = False
        self._captures: queue.Queue[dict[str, Any]] = queue.Queue(CAPTURE_QUEUE)
        self._capturing = False
        self._state = threading.Lock()
        self._busy = threading.Condition()
        self._pending = 0

    def check(self, request: GateRequest) -> Decision:
        if not self.active:
            return PASS
        asking: Asking | None = None
        answered = False
        try:
            settings = self._settings()
            before = self.before_asking(request, settings)
            if not isinstance(before, Asking) or settings is None:
                return before if not isinstance(before, Asking) else PASS
            asking = before
            body = decide_body(request, settings, self.options.assume_https)
            budget = settings.settle_timeout if request.payment is not None else settings.timeout
            answer = self._ask("decide", body, budget)
            answered = True
            return self.after_asking(request, settings, body, answer, asking)
        except Exception as error:
            self.report(error)
            return unknown_payment() if request.payment is not None else PASS
        finally:
            if not answered:  # ended without Stilebase's answer (a fault here): counts nothing
                self.release(asking)

    def settle(self, hold: Hold, status: int, body: bytes) -> Decision:
        """The site answered a paid request (`body` its whole body, held): settle it (SPEC.md, section 7). `Pass`: send
        the held answer with its `response_headers`; anything else: send that instead."""
        if len(body) > hold.max_bytes or not holdable(status):
            return not_payable()
        try:
            settings = self.settings or Settings()
            url, data, headers = self.signed_post("settle", {**hold.body, "delivery": delivery(status, body)})
            answer = self._http.post(url, content=data, headers=headers, timeout=settings.settle_timeout)
            return self.settled(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)
            return unknown_payment()

    def capture(self, order: Capture, status: int, headers: Sequence[tuple[str, str]], copy: Copy, ttfb_ms: int) -> None:
        """Send a copy of the site's answer in the background (SPEC.md, section 9): one at a time, at most
        CAPTURE_QUEUE waiting, the rest and any failure dropped."""
        try:
            payload = capture_body(order.token, status, headers, copy.body, copy.size, copy.truncated, ttfb_ms)
            self._captures.put_nowait(payload)
            with self._state:
                if self._capturing:
                    return
                self._capturing = True
            self._background(self._send_captures)
        except queue.Full:
            pass
        except Exception as error:
            self.report(error)

    def wait(self, timeout: float = 10.0) -> None:
        """Wait for background refreshes and captures to finish (tests, shutdown)."""
        with self._busy:
            self._busy.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        self.wait()
        self._http.close()

    def _ask(self, name: str, body: dict[str, Any], budget: float) -> Answer:
        """One call to Stilebase within `budget` seconds for each step (connect, write, read); anything wrong is
        UNUSABLE."""
        try:
            url, data, headers = self.signed_post(name, body)
            answer = self._http.post(url, content=data, headers=headers, timeout=budget)
            return read_answer(answer.status_code, json_body(answer.content))
        except httpx.PoolTimeout as error:
            self.report(error)
            return NOT_ASKED  # waiting for a free connection is the gate's own wait: the breaker ignores it
        except Exception as error:
            self.report(error)
            return UNUSABLE

    def _settings(self) -> Settings | None:
        if self.settings_at is None:
            if self._settings_lock.acquire(timeout=SETTINGS_TIMEOUT):  # one thread loads; the others wait for it
                try:
                    if self.settings_at is None:
                        self._load_settings()
                finally:
                    self._settings_lock.release()
        elif self.settings_due():
            with self._state:
                if self._refreshing:
                    return self.settings
                self._refreshing = True
            self._background(self._refresh)
        return self.settings

    def _refresh(self) -> None:
        try:
            self._load_settings()
        finally:
            with self._state:
                self._refreshing = False

    def _load_settings(self) -> None:
        try:
            # httpx's sync client has no overall deadline: its steps share the 3 s, so the whole load stays near it (a
            # read that trickles in byte by byte can still run longer: TODO.md).
            timeout = httpx.Timeout(connect=1.0, write=0.5, read=1.0, pool=0.5)
            answer = self._http.get(self.api + self.path("settings"), headers=self.settings_headers(), timeout=timeout)
            self.take_settings(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.settings_at = self.options.clock()
            self.report(error)  # keep the last good settings, if any

    def _send_captures(self) -> None:
        while True:
            with self._state:  # checked and released together: a copy queued meanwhile is never left behind
                try:
                    payload = self._captures.get_nowait()
                except queue.Empty:
                    self._capturing = False
                    return
            try:
                url, data, headers = self.signed_post("captures", payload)
                self._http.post(url, content=data, headers=headers, timeout=CAPTURE_TIMEOUT)
            except Exception as error:
                self.report(error)

    def _background(self, work: Any) -> None:
        with self._busy:
            self._pending += 1

        def run() -> None:
            try:
                work()
            except Exception as error:
                self.report(error)
            finally:
                with self._busy:
                    self._pending -= 1
                    self._busy.notify_all()

        threading.Thread(target=run, daemon=True, name="stilebase-gate").start()
