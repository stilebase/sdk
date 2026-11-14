"""The gate for async sites (ASGI: FastAPI, Starlette, Litestar…): `decide`'s questions answered with httpx's
AsyncClient, reports and refreshes in background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine
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
# Visit reports get a client of their own, with a few connections: while Stilebase is slow to take them, they never
# hold the connections a price lookup or a settlement needs (one that can't get one would pass, or fail, the request).
REPORT_CONNECTIONS = 10


class AsyncGate(Engine):
    """`await gate.check(request)` tells you what to do with a request. Never raises."""

    def __init__(self, options: GateOptions | None = None, adapter: str = "async", **kwargs: Any) -> None:
        if options is not None and kwargs:
            raise TypeError(f"AsyncGate takes GateOptions or keywords, not both (got {', '.join(kwargs)} too)")
        super().__init__(options or GateOptions(**kwargs), adapter)
        self._http: httpx.AsyncClient | None = None
        self._reports_http: httpx.AsyncClient | None = None
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
        """Wait for background reports, then close the HTTP clients (e.g. at shutdown, or in tests). A gate used
        after this opens them again; close it again then."""
        try:
            for _ in range(3):  # a few rounds, not "until none": requests still arriving keep adding reports
                if not self._tasks:
                    break
                await asyncio.wait(set(self._tasks))  # never cancels them, even if this is cancelled (a shutdown timeout)
        finally:
            clients = [c for c in (self._http, self._reports_http) if c is not None]
            self._http = self._reports_http = None
            pending = {task for task in self._tasks if not task.done()}
            if pending:
                # Work still running (a shutdown that won't wait): the clients close once it's done, never under it.
                self._spawn(self._close_after(clients, pending))
            else:
                await self._close(clients)

    async def _close_after(self, clients: list[httpx.AsyncClient], tasks: set[asyncio.Task[Any]]) -> None:
        try:
            await asyncio.wait(tasks)
        finally:  # cancelled too (the loop torn down): the clients still close
            await self._close(clients)

    async def _close(self, clients: list[httpx.AsyncClient]) -> None:
        for client in clients:
            try:
                await client.aclose()
            except Exception as error:  # one client that fails to close never keeps the other open
                self.report(error)

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
                await self._wait(self._config_load(), CONFIG_TIMEOUT + 0.5)
            elif self.config_due() and self.begin_refresh("config"):
                self._background(self._config_load(), refresh="config")
            return self.config
        if isinstance(effect, GetRanges):
            if self.ranges is None:
                await self._wait(self._ranges_load(), REQUIREMENTS_TIMEOUT + 0.5)
            elif self.ranges_due() and self.begin_refresh("ranges"):
                self._background(self._ranges_load(), refresh="ranges")
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
            # Stilebase slow: statistics can wait, memory can't grow without end.
            if self._reports < MAX_PENDING_REPORTS:
                self._reports += 1
                self._background(self._report(effect.path, effect.payload, self._reports_client()))
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

    # The one load of each kind in flight, as a task of its own: a request waiting on it that is cancelled (its client
    # gone) never cancels the load, so the gate never ends up having "tried" with nothing.
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
        """Wait for a load, at most `timeout`: this request's own deadline, whatever happens to the load."""
        # asyncio.wait neither cancels the load nor raises when it ends (cancelled, failed or late): a CancelledError
        # here is this request's own, and goes on up.
        await asyncio.wait({task}, timeout=timeout)

    async def _load_config(self) -> None:
        try:
            answer = await self.http.get(self.api + self.config_path(), headers=self.config_headers(), timeout=CONFIG_TIMEOUT)
            self.take_config(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)  # keep the last good settings, if any
        finally:
            self.config_done()

    async def _load_ranges(self) -> None:
        try:
            answer = await self.http.get(f"{self.api}/v1/x402/agent-ranges", timeout=REQUIREMENTS_TIMEOUT)
            self.take_ranges(answer.status_code, json_body(answer.content))
        except Exception as error:
            self.report(error)
        finally:
            self.ranges_done()

    def _reports_client(self) -> httpx.AsyncClient:
        """The reports' client, taken when a report is started: one started before aclose() closes with that client."""
        if self._reports_http is None:
            limits = httpx.Limits(max_connections=REPORT_CONNECTIONS)
            self._reports_http = httpx.AsyncClient(transport=self.options.transport, follow_redirects=False, limits=limits)
        return self._reports_http

    async def _report(self, path: str, payload: dict[str, Any], client: httpx.AsyncClient) -> None:
        try:
            await self._post(path, payload, REPORT_TIMEOUT, client)
        finally:
            self._reports -= 1

    async def _post(
        self, path: str, payload: dict[str, Any], timeout: float, client: httpx.AsyncClient | None = None
    ) -> httpx.Response:
        body, headers = self.signed_post(path, payload)
        return await (client or self.http).post(self.api + path, content=body, headers=headers, timeout=timeout)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)  # keep a reference until it's done, or it may be collected mid-flight
        task.add_done_callback(self._tasks.discard)
        return task

    def _background(self, work: Awaitable[Any], refresh: str | None = None) -> None:
        async def quietly() -> None:
            try:
                await work
            except Exception as error:
                self.report(error)
            finally:
                if refresh:
                    self.end_refresh(refresh)

        self._spawn(quietly())
