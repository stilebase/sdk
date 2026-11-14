"""The gate for sync sites (WSGI: Flask, Django, Pyramid…): `decide`'s questions answered with httpx's Client, reports
and refreshes on one background thread with a bounded queue (dropped when full, never blocking a request)."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

import httpx

from ._core import (
    CONFIG_TIMEOUT,
    PASS,
    REPORT_TIMEOUT,
    REQUIREMENTS_TIMEOUT,
    SETTLE_TIMEOUT,
    Decision,
    Effect,
    Engine,
    Flow,
    GateOptions,
    GetConfig,
    GetRanges,
    GetRequirements,
    Report,
    Settle,
    json_body,
)
from ._detect import GateRequest, NetworkIndex

BACKGROUND_QUEUE_SIZE = 1000


class SyncGate(Engine):
    """`gate.check(request)` tells you what to do with a request. Never raises. Safe to share between threads."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "sync", **kwargs: Any) -> None:
        if options is not None and kwargs:
            raise TypeError(f"SyncGate takes GateOptions or keywords, not both (got {', '.join(kwargs)} too)")
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._ranges_lock = threading.Lock()
        self._queue: queue.Queue[Callable[[], Any]] = queue.Queue(BACKGROUND_QUEUE_SIZE)
        self._worker: threading.Thread | None = None
        self._pending = 0
        self._pending_lock = threading.Condition()

    def check(self, request: GateRequest) -> Decision:
        if self.site is None:
            return PASS
        try:
            return self._run(self.flow(request))
        except Exception as error:
            self.report(error)
            return PASS

    def wait(self, timeout: float = 10.0) -> None:
        """Wait for background reports and refreshes to finish (tests, shutdown)."""
        with self._pending_lock:
            self._pending_lock.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        self.wait()
        if self._http is not None:
            self._http.close()
            self._http = None

    def _run(self, flow: Flow) -> Decision:
        value: Any = None
        error: Exception | None = None
        while True:
            try:
                effect = flow.throw(error) if error is not None else flow.send(value)
            except StopIteration as stop:
                result: Decision = stop.value
                return result
            value, error = None, None
            try:
                value = self._perform(effect)
            except Exception as exc:
                error = exc

    def _perform(self, effect: Effect) -> Any:
        if isinstance(effect, GetConfig):
            if not self.config_tried:
                self._load_config()
            elif self.config_due() and self.begin_refresh("config"):
                self._refresh(self._load_config, "config")
            return self.config
        if isinstance(effect, GetRanges):
            if self.ranges is None:
                self._load_ranges()
            elif self.ranges_due() and self.begin_refresh("ranges"):
                self._refresh(self._load_ranges, "ranges")
            return self.ranges if self.ranges is not None else NetworkIndex([])
        if isinstance(effect, GetRequirements):
            cached = self.cached_requirements(effect.query)
            if cached is not None:
                return cached
            answer = self.http.get(f"{self.api}/v1/x402/requirements?{effect.query}", timeout=REQUIREMENTS_TIMEOUT)
            body = json_body(answer.content) if answer.status_code == 200 else None
            self.store_requirements(effect.query, answer.status_code, body, answer.headers.get("cache-control", ""))
            return answer.status_code, body
        if isinstance(effect, Report):
            path, payload = effect.path, effect.payload
            self._background(lambda: self._post(path, payload, REPORT_TIMEOUT))
            return None
        if isinstance(effect, Settle):
            answer = self._post("/v1/x402/settle", effect.payload, SETTLE_TIMEOUT)
            return answer.status_code, json_body(answer.content)
        raise TypeError(f"Unknown effect {effect!r}")

    @property
    def http(self) -> httpx.Client:
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(transport=self.options.transport, follow_redirects=False)
            return self._http

    def _load_config(self) -> None:
        if not self._config_lock.acquire(blocking=not self.config_tried):
            return  # a refresh is already running
        try:
            if self.config_tried and not self.config_due():
                return  # another thread loaded them while this one waited
            answer = self.http.get(self.api + self.config_path(), headers=self.config_headers(), timeout=CONFIG_TIMEOUT)
            self.take_config(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)  # keep the last good settings, if any
        finally:
            self.config_done()
            self._config_lock.release()

    def _load_ranges(self) -> None:
        if not self._ranges_lock.acquire(blocking=self.ranges is None):
            return
        try:
            if self.ranges is not None and not self.ranges_due():
                return
            answer = self.http.get(f"{self.api}/v1/x402/agent-ranges", timeout=REQUIREMENTS_TIMEOUT)
            self.take_ranges(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)
        finally:
            self.ranges_done()
            self._ranges_lock.release()

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        body, headers = self.signed_post(path, payload)
        return self.http.post(self.api + path, content=body, headers=headers, timeout=timeout)

    def _refresh(self, work: Callable[[], Any], name: str) -> None:
        """A settings or address-list refresh, on its own short-lived thread: never queued behind visit reports, so
        a pause or a rotated key reaches the gate even while Stilebase is slow to take reports (one per kind at a
        time, begin_refresh)."""

        def run() -> None:
            try:
                work()
            except Exception as error:
                self.report(error)
            finally:
                self.end_refresh(name)
                with self._pending_lock:
                    self._pending -= 1
                    self._pending_lock.notify_all()

        with self._pending_lock:
            self._pending += 1
        try:
            threading.Thread(target=run, name=f"stilebase-sdk-{name}", daemon=True).start()
        except Exception as error:  # no thread to be had: undo, so the next due request tries again
            self.end_refresh(name)
            with self._pending_lock:
                self._pending -= 1
                self._pending_lock.notify_all()
            self.report(error)

    def _background(self, work: Callable[[], Any]) -> None:
        """A visit report: one worker thread, a bounded queue, dropped when full."""
        with self._pending_lock:
            if self._worker is None or not self._worker.is_alive():
                worker = threading.Thread(target=self._drain, name="stilebase-sdk", daemon=True)
                try:
                    worker.start()
                except Exception as error:  # no thread to be had: this report is dropped, the next one tries again
                    self.report(error)
                    return
                self._worker = worker
            try:
                self._queue.put_nowait(work)
            except queue.Full:
                return  # statistics can wait; requests never do
            self._pending += 1

    def _drain(self) -> None:
        while True:
            work = self._queue.get()
            try:
                work()
            except Exception as error:
                self.report(error)
            finally:
                with self._pending_lock:
                    self._pending -= 1
                    self._pending_lock.notify_all()
