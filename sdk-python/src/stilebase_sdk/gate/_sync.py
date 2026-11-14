"""The gate for sync sites (WSGI: Flask, Django…): `decide`'s questions answered with httpx's Client, refreshes and visit
reports on background threads."""

from __future__ import annotations

import queue
import threading
from typing import Any

import httpx

from ._core import (
    CONFIG_TIMEOUT,
    MAX_PENDING_REPORTS,
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
from ._detect import GateRequest


class SyncGate(Engine):
    """`gate.check(request)` tells you what to do with a request. Never raises. Safe to share between threads."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "sync", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http = httpx.Client(transport=self.options.transport, follow_redirects=False)
        self._config_lock = threading.Lock()
        self._ranges_lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._reports: queue.Queue[dict[str, Any]] = queue.Queue(MAX_PENDING_REPORTS)
        self._worker: threading.Thread | None = None
        self._busy = threading.Condition()
        self._pending = 0

    def check(self, request: GateRequest) -> Decision:
        if self.site is None:
            return PASS
        try:
            return self._run(self.flow(request))
        except Exception as error:
            self.report(error)
            return PASS

    def wait(self, timeout: float = 10.0) -> None:
        """Wait for background refreshes and reports to finish (tests, shutdown)."""
        with self._busy:
            self._busy.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        self.wait()
        self._http.close()

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
                self.report(exc)
                error = exc

    def _perform(self, effect: Effect) -> Any:
        if isinstance(effect, GetConfig):
            if not self.config_at:
                self._load_config(wait=True)
            elif self.config_due():
                self._in_background("config", self._load_config)
            return self.config
        if isinstance(effect, GetRanges):
            if self.ranges is None:
                self._load_ranges(wait=True)
            elif self.ranges_due():
                self._in_background("ranges", self._load_ranges)
            return self.ranges or []
        if isinstance(effect, GetRequirements):
            cached = self.cached_requirements(effect.query)
            if cached is not None:
                return cached
            answer = self._http.get(f"{self.api}/v1/x402/requirements?{effect.query}", timeout=REQUIREMENTS_TIMEOUT)
            body = json_body(answer.content) if answer.status_code == 200 else None
            self.store_requirements(effect.query, answer.status_code, body, answer.headers.get("cache-control", ""))
            return answer.status_code, body
        if isinstance(effect, Report):
            self._report_visit(effect.payload)
            return None
        if isinstance(effect, Settle):
            body, headers = self.signed_post("/v1/x402/settle", effect.payload)
            answer = self._http.post(f"{self.api}/v1/x402/settle", content=body, headers=headers, timeout=SETTLE_TIMEOUT)
            return answer.status_code, json_body(answer.content)
        raise TypeError(f"Unknown effect {effect!r}")

    def _load_config(self, wait: bool = False) -> None:
        # The first load: requests arriving meanwhile wait for it (at most its timeout) instead of loading again.
        if not self._config_lock.acquire(blocking=wait, timeout=CONFIG_TIMEOUT + 0.5 if wait else -1):
            return
        try:
            if wait and self.config_at:
                return  # another request loaded them while this one waited
            answer = self._http.get(self.api + self.config_path(), headers=self.config_headers(), timeout=CONFIG_TIMEOUT)
            self.take_config(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.config_at = self.options.now()
            self.report(error)  # keep the last good settings, if any
        finally:
            self._config_lock.release()

    def _load_ranges(self, wait: bool = False) -> None:
        if not self._ranges_lock.acquire(blocking=wait, timeout=REQUIREMENTS_TIMEOUT + 0.5 if wait else -1):
            return
        try:
            if wait and self.ranges is not None:
                return
            answer = self._http.get(f"{self.api}/v1/x402/agent-ranges", timeout=REQUIREMENTS_TIMEOUT)
            self.take_ranges(answer.status_code, json_body(answer.content))
        except Exception as error:
            if self.ranges is None:
                self.ranges = []
            self.ranges_at = self.options.now()
            self.report(error)
        finally:
            self._ranges_lock.release()

    def _in_background(self, name: str, work: Any) -> None:
        """A refresh on a short-lived thread of its own, one per kind at a time: never queued behind visit reports."""
        with self._busy:
            if name in self._refreshing:
                return
            self._refreshing.add(name)
            self._pending += 1

        def run() -> None:
            try:
                work()
            finally:
                with self._busy:
                    self._refreshing.discard(name)
                    self._pending -= 1
                    self._busy.notify_all()

        try:
            threading.Thread(target=run, name=f"stilebase-sdk-{name}", daemon=True).start()
        except Exception as error:  # no thread to be had: undo, so the next due request tries again
            with self._busy:
                self._refreshing.discard(name)
                self._pending -= 1
                self._busy.notify_all()
            self.report(error)

    def _report_visit(self, payload: dict[str, Any]) -> None:
        """One worker thread posts the visits; dropped when too many are waiting (Stilebase slow)."""
        with self._busy:
            try:
                self._reports.put_nowait(payload)
            except queue.Full:
                return
            self._pending += 1
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="stilebase-sdk", daemon=True)
                try:
                    self._worker.start()
                except Exception as error:  # no thread to be had: statistics wait for the next visit's try
                    self.report(error)

    def _drain(self) -> None:
        while True:
            payload = self._reports.get()
            try:
                body, headers = self.signed_post("/v1/x402/visits", payload)
                self._http.post(f"{self.api}/v1/x402/visits", content=body, headers=headers, timeout=REPORT_TIMEOUT)
            except Exception as error:
                self.report(error)
            finally:
                with self._busy:
                    self._pending -= 1
                    self._busy.notify_all()
