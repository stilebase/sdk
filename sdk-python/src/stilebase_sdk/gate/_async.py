"""The gate for async sites (ASGI: FastAPI, Starlette, Litestar…): `decide`'s questions answered with httpx's
AsyncClient, refreshes and visit reports in background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
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


class AsyncGate(Engine):
    """`await gate.check(request)` tells you what to do with a request. Never raises."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "async", **kwargs: Any) -> None:
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.AsyncClient | None = None
        self._config_task: asyncio.Task[None] | None = None
        self._ranges_task: asyncio.Task[None] | None = None
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
                self.report(exc)
                error = exc

    async def _perform(self, effect: Effect) -> Any:
        if isinstance(effect, GetConfig):
            if not self.config_at:
                await self._wait(self._config_load(), CONFIG_TIMEOUT + 0.5)
            elif self.config_due() and (self._config_task is None or self._config_task.done()):
                self._config_load()
            return self.config
        if isinstance(effect, GetRanges):
            if self.ranges is None:
                await self._wait(self._ranges_load(), REQUIREMENTS_TIMEOUT + 0.5)
            elif self.ranges_due() and (self._ranges_task is None or self._ranges_task.done()):
                self._ranges_load()
            return self.ranges or []
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
                self._spawn(self._report(effect.payload))
            return None
        if isinstance(effect, Settle):
            body, headers = self.signed_post("/v1/x402/settle", effect.payload)
            answer = await self.http.post(f"{self.api}/v1/x402/settle", content=body, headers=headers, timeout=SETTLE_TIMEOUT)
            return answer.status_code, json_body(answer.content)
        raise TypeError(f"Unknown effect {effect!r}")

    # Each load runs as a task of its own: a request waiting on it that is cancelled (its client gone) never cancels it.
    def _config_load(self) -> asyncio.Task[None]:
        if self._config_task is None or self._config_task.done():
            self._config_task = self._spawn(self._load_config())
        return self._config_task

    def _ranges_load(self) -> asyncio.Task[None]:
        if self._ranges_task is None or self._ranges_task.done():
            self._ranges_task = self._spawn(self._load_ranges())
        return self._ranges_task

    @staticmethod
    async def _wait(task: asyncio.Task[None], timeout: float) -> None:
        """This request's own deadline for a load; asyncio.wait never cancels the load nor raises when it ends."""
        await asyncio.wait({task}, timeout=timeout)

    async def _load_config(self) -> None:
        try:
            answer = await self.http.get(self.api + self.config_path(), headers=self.config_headers(), timeout=CONFIG_TIMEOUT)
            self.take_config(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.config_at = self.options.now()
            self.report(error)  # keep the last good settings, if any

    async def _load_ranges(self) -> None:
        try:
            answer = await self.http.get(f"{self.api}/v1/x402/agent-ranges", timeout=REQUIREMENTS_TIMEOUT)
            self.take_ranges(answer.status_code, json_body(answer.content))
        except Exception as error:
            if self.ranges is None:
                self.ranges = []
            self.ranges_at = self.options.now()
            self.report(error)

    async def _report(self, payload: dict[str, Any]) -> None:
        try:
            body, headers = self.signed_post("/v1/x402/visits", payload)
            await self.http.post(f"{self.api}/v1/x402/visits", content=body, headers=headers, timeout=REPORT_TIMEOUT)
        except Exception as error:
            self.report(error)
        finally:
            self._reports -= 1

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)  # keep a reference until it's done, or it may be collected mid-flight
        task.add_done_callback(self._tasks.discard)
        return task
