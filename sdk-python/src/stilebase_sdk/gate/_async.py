"""The gate for async sites (ASGI: FastAPI, Starlette, Litestar…): `decide`'s questions answered with httpx's
AsyncClient, reports and refreshes in background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
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

MAX_PENDING_REPORTS = 1000


class AsyncGate(Engine):
    """`await gate.check(request)` tells you what to do with a request. Never raises."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "async", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.AsyncClient | None = None
        self._config_lock: asyncio.Lock | None = None
        self._ranges_lock: asyncio.Lock | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reports = 0

    async def check(self, request: GateRequest) -> Decision:
        if self.site is None:
            return PASS
        try:
            return await self._run(self.flow(request))
        except Exception as error:
            self.report(error)
            return PASS

    async def aclose(self) -> None:
        """Wait for background reports, then close the HTTP client (e.g. at shutdown, or in tests)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _run(self, flow: Flow) -> Decision:
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
                value = await self._perform(effect)
            except Exception as exc:
                error = exc

    async def _perform(self, effect: Effect) -> Any:
        if isinstance(effect, GetConfig):
            if not self.config_tried:
                await self._load_config()
            elif self.config_due() and self.begin_refresh("config"):
                self._background(self._load_config(), refresh="config")
            return self.config
        if isinstance(effect, GetRanges):
            if self.ranges is None:
                await self._load_ranges()
            elif self.ranges_due() and self.begin_refresh("ranges"):
                self._background(self._load_ranges(), refresh="ranges")
            return self.ranges if self.ranges is not None else NetworkIndex([])
        if isinstance(effect, GetRequirements):
            cached = self.cached_requirements(effect.query)
            if cached is not None:
                return cached
            answer = await self.http.get(f"{self.api}/v1/x402/requirements?{effect.query}", timeout=REQUIREMENTS_TIMEOUT)
            body = json_body(answer.content) if answer.status_code == 200 else None
            self.store_requirements(effect.query, answer.status_code, body, answer.headers.get("cache-control", ""))
            return answer.status_code, body
        if isinstance(effect, Report):
            if self._reports < MAX_PENDING_REPORTS:  # Stilebase slow: statistics can wait, memory can't grow without end
                self._reports += 1
                self._background(self._report(effect.path, effect.payload))
            return None
        if isinstance(effect, Settle):
            answer = await self._post("/v1/x402/settle", effect.payload, SETTLE_TIMEOUT)
            return answer.status_code, json_body(answer.content)
        raise TypeError(f"Unknown effect {effect!r}")

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self.options.transport, follow_redirects=False)
        return self._http

    async def _load_config(self) -> None:
        self._config_lock = self._config_lock or asyncio.Lock()
        if self._config_lock.locked():  # another request is loading them: wait for it, don't load twice
            async with self._config_lock:
                return
        async with self._config_lock:
            try:
                answer = await self.http.get(self.api + self.config_path(), headers=self.config_headers(), timeout=CONFIG_TIMEOUT)
                self.take_config(answer.status_code, json_body(answer.content))
            except Exception as error:
                self.report(error)  # keep the last good settings, if any
            finally:
                self.config_done()

    async def _load_ranges(self) -> None:
        self._ranges_lock = self._ranges_lock or asyncio.Lock()
        if self._ranges_lock.locked():
            async with self._ranges_lock:
                return
        async with self._ranges_lock:
            try:
                answer = await self.http.get(f"{self.api}/v1/x402/agent-ranges", timeout=REQUIREMENTS_TIMEOUT)
                self.take_ranges(answer.status_code, json_body(answer.content))
            except Exception as error:
                self.report(error)
            finally:
                self.ranges_done()

    async def _report(self, path: str, payload: dict[str, Any]) -> None:
        try:
            await self._post(path, payload, REPORT_TIMEOUT)
        finally:
            self._reports -= 1

    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        body, headers = self.signed_post(path, payload)
        return await self.http.post(self.api + path, content=body, headers=headers, timeout=timeout)

    def _background(self, work: Coroutine[Any, Any, Any], refresh: str | None = None) -> None:
        async def quietly() -> None:
            try:
                await work
            except Exception as error:
                self.report(error)
            finally:
                if refresh:
                    self.end_refresh(refresh)

        task = asyncio.get_running_loop().create_task(quietly())
        self._tasks.add(task)  # keep a reference until it's done, or it may be collected mid-flight
        task.add_done_callback(self._tasks.discard)
