"""The gates (SyncGate, AsyncGate) against a fake Stilebase: forward, obey, fail open (SPEC.md)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import pytest

from stilebase_sdk.gate import AsyncGate, Capture, Copy, GateOptions, GateRequest, Hold, Pass, Respond, SyncGate
from tests.fake_stilebase import (
    API,
    CHROME,
    GPTBOT,
    KEY,
    PAID,
    PAYMENT,
    RECEIPT,
    REMEMBER,
    REQUIRED_402,
    SETTLE_AFTER,
    FakeStilebase,
    settings,
)

PERSON = GateRequest("GET", "/listings/2?x=1", (("User-Agent", CHROME), ("Cookie", "s=1; stilebase_pass=t")), ip="203.0.113.7")
AGENT = GateRequest("GET", "/listings/2", (("User-Agent", GPTBOT),), ip="20.15.240.70")
PAYING = GateRequest("GET", "/listings/2", (("User-Agent", GPTBOT), ("PAYMENT-SIGNATURE", PAYMENT)), ip="20.15.240.70")


class Clock:
    def __init__(self) -> None:
        self.at = 1_792_756_800.0

    def __call__(self) -> float:
        return self.at


def sync_gate(stilebase: FakeStilebase, clock: Clock | None = None, **options: Any) -> SyncGate:
    errors: list[Exception] = []
    tick = clock or Clock()
    gate = SyncGate(
        GateOptions(key=KEY, api_url=API, transport=stilebase.transport, on_error=errors.append, now=tick, clock=tick, **options)
    )
    gate.errors = errors  # type: ignore[attr-defined]
    return gate


def async_gate(stilebase: FakeStilebase, clock: Clock | None = None, **options: Any) -> AsyncGate:
    tick = clock or Clock()
    return AsyncGate(
        GateOptions(
            key=KEY, api_url=API, transport=stilebase.async_transport, on_error=lambda e: None, now=tick, clock=tick, **options
        )
    )


def test_without_a_key_or_disabled_everyone_passes_and_nothing_is_asked() -> None:
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    quiet = GateOptions(key="", api_url=API, transport=stilebase.transport, on_error=lambda e: None)
    assert SyncGate(quiet).check(AGENT) == Pass()
    disabled = sync_gate(stilebase, disabled=True)
    assert disabled.check(AGENT) == Pass() and stilebase.calls == []


def test_the_decision_is_asked_for_and_obeyed() -> None:
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    gate = sync_gate(stilebase)
    decision = gate.check(AGENT)
    assert isinstance(decision, Respond) and decision.status == 402 and decision.headers["cache-control"] == "no-store"
    [settings_call] = stilebase.to("settings")
    assert settings_call.headers["stilebase-client"] == "stilebase-sdk/0.2.0 gate/sync"
    [asked] = stilebase.to("decide")
    assert asked.body["request"] == {"method": "GET", "scheme": "https", "path": "/listings/2"}
    assert asked.body["client_ip"] == "20.15.240.70"


def test_a_person_is_remembered_and_never_sends_cookies() -> None:
    stilebase = FakeStilebase(decide=(200, REMEMBER))
    gate = sync_gate(stilebase)
    assert gate.check(PERSON) == Pass() and gate.check(PERSON) == Pass()
    [asked] = stilebase.to("decide")  # the second was remembered
    assert ["cookie", "s=1; stilebase_pass=t"] not in asked.body["headers"]
    assert asked.body["cookies"] == {"stilebase_pass": "t"} and asked.body["credentials"] is True


def test_a_payment_is_never_remembered_nor_skipped() -> None:
    stilebase = FakeStilebase(decide=(200, REMEMBER))
    gate = sync_gate(stilebase)
    gate.check(PAYING)
    gate.check(PAYING)
    static = GateRequest("GET", "/site.css", (("PAYMENT-SIGNATURE", PAYMENT),))
    gate.check(static)
    assert len(stilebase.to("decide")) == 3


def test_static_files_pass_without_asking() -> None:
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    gate = sync_gate(stilebase)
    assert gate.check(GateRequest("GET", "/assets/site.css", (("User-Agent", GPTBOT),))) == Pass()
    assert isinstance(gate.check(GateRequest("GET", "/premium/article;.css", (("User-Agent", GPTBOT),))), Respond)
    assert len(stilebase.to("decide")) == 1


@pytest.mark.parametrize(
    "stilebase",
    [
        FakeStilebase(down=True),
        FakeStilebase(decide=(500, {"action": "respond"})),
        FakeStilebase(raw_decide=b"not json"),
        FakeStilebase(decide=(200, {"action": "teleport"})),
    ],
    ids=["down", "500", "garbage", "unknown action"],
)
def test_anything_wrong_lets_people_through_and_payments_wait(stilebase: FakeStilebase) -> None:
    if stilebase.down:
        stilebase.down = False
        gate = sync_gate(stilebase)
        gate.check(AGENT)  # the settings first
        stilebase.down = True
    else:
        gate = sync_gate(stilebase)
    assert gate.check(AGENT) == Pass()
    held = gate.check(PAYING)
    assert isinstance(held, Respond) and (held.status, held.headers["retry-after"]) == (503, "5")


def test_settings_404_and_a_version_this_gate_cant_read_let_everyone_through() -> None:
    for answer in ((404, {"error": "gone"}), (200, settings(settings_version=2)), (200, settings(enabled=False))):
        stilebase = FakeStilebase(settings_answer=answer, decide=(200, REQUIRED_402))
        assert sync_gate(stilebase).check(AGENT) == Pass() and stilebase.to("decide") == []


def test_the_last_good_settings_are_kept_and_refreshed() -> None:
    clock = Clock()
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    gate = sync_gate(stilebase, clock)
    gate.check(AGENT)
    stilebase.settings_answer = (500, {})
    clock.at += 301
    gate.check(AGENT)
    gate.wait()
    assert len(stilebase.to("settings")) == 2 and gate.settings is not None  # a 5xx keeps the last good settings
    assert isinstance(gate.check(AGENT), Respond)


def test_the_breaker_opens_after_failures_in_a_row() -> None:
    clock = Clock()
    stilebase = FakeStilebase(decide=(500, {}))
    gate = sync_gate(stilebase, clock)
    for _ in range(5):
        gate.check(AGENT)
    assert len(stilebase.to("decide")) == 5
    assert gate.check(AGENT) == Pass() and len(stilebase.to("decide")) == 5  # open: nothing asked
    assert isinstance(gate.check(PAYING), Respond)  # a payment while open: 503
    clock.at += 31
    stilebase.decide = (200, REQUIRED_402)
    assert isinstance(gate.check(AGENT), Respond)  # asked again, and closed
    assert len(stilebase.to("decide")) == 6


def test_a_slow_answer_past_the_budget_lets_the_request_through() -> None:
    slow = FakeStilebase(settings_answer=(200, settings(timeout_ms=50)), decide=(200, REQUIRED_402), delay=0.3)

    async def run() -> Pass | Respond:
        gate = async_gate(slow)
        decision = await gate.check(AGENT)
        await gate.aclose()
        return decision

    assert asyncio.run(run()) == Pass()


def test_a_paid_answer_is_settled_with_its_delivery() -> None:
    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    gate = sync_gate(stilebase)
    decision = gate.check(PAYING)
    assert isinstance(decision, Pass) and decision.hold is not None
    released = gate.settle(decision.hold, 200, b"the page")
    assert released == Pass({"payment-response": RECEIPT})
    [settled] = stilebase.to("settle")
    assert settled.body["delivery"] == {"status": 200, "size": 8, "sha256": hashlib.sha256(b"the page").hexdigest()}
    assert settled.body["request"]["path"] == "/listings/2" and settled.body["headers"][1] == ["payment-signature", PAYMENT]


@pytest.mark.parametrize(
    ("answer", "status"),
    [((200, REQUIRED_402), 402), ((200, {"action": "unknown_payment", "retry_after": 9}), 503), ((502, {}), 503)],
)
def test_a_settlement_refused_or_unknown_withholds_the_page(answer: tuple[int, Any], status: int) -> None:
    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER), settle=answer)
    gate = sync_gate(stilebase)
    decision = gate.check(PAYING)
    assert isinstance(decision, Pass) and decision.hold is not None
    outcome = gate.settle(decision.hold, 200, b"the page")
    assert isinstance(outcome, Respond) and outcome.status == status


def test_too_large_to_hold_is_never_settled() -> None:
    stilebase = FakeStilebase()
    gate = sync_gate(stilebase)
    outcome = gate.settle(Hold({}, 4), 200, b"12345")
    assert isinstance(outcome, Respond) and outcome.status == 502 and stilebase.to("settle") == []


def test_a_capture_is_sent_in_the_background() -> None:
    stilebase = FakeStilebase()
    gate = sync_gate(stilebase)
    gate.check(AGENT)
    copy = Copy(4)
    copy.add(b"the page")
    gate.capture(Capture("tok", 4), 200, [("content-type", "text/html")], copy, 12)
    gate.wait()
    [sent] = stilebase.to("captures")
    assert sent.body == {
        "token": "tok",
        "status": 200,
        "headers": [["content-type", "text/html"]],
        "body": base64.b64encode(b"the ").decode(),
        "size": 8,
        "truncated": True,
        "ttfb_ms": 12,
    }


def test_the_async_gate_asks_once_for_requests_alike() -> None:
    async def run() -> int:
        stilebase = FakeStilebase(decide=(200, REMEMBER), delay=0.05)
        gate = async_gate(stilebase)
        await gate.check(AGENT)  # settings
        stilebase.calls.clear()
        alike = GateRequest("GET", "/other", (("User-Agent", CHROME),), ip="198.51.100.4")
        results = await asyncio.gather(*(gate.check(alike) for _ in range(5)))
        assert all(r == Pass() for r in results)
        await gate.aclose()
        return len(stilebase.to("decide"))

    assert asyncio.run(run()) == 1


def test_the_async_gate_settles_and_captures() -> None:
    async def run() -> None:
        stilebase = FakeStilebase(decide=(200, {**SETTLE_AFTER, "capture": {"token": "t", "max_bytes": 100}}))
        gate = async_gate(stilebase)
        decision = await gate.check(PAYING)
        assert isinstance(decision, Pass) and decision.hold is not None and decision.capture == Capture("t", 100)
        assert await gate.settle(decision.hold, 200, b"page") == Pass({"payment-response": RECEIPT})
        copy = Copy(100)
        copy.add(b"page")
        gate.capture(decision.capture, 200, [], copy, 1)
        await gate.aclose()
        assert len(stilebase.to("settle")) == 1 and len(stilebase.to("captures")) == 1

    asyncio.run(run())


def test_paid_headers_are_passed_on() -> None:
    stilebase = FakeStilebase(decide=(200, PAID))
    assert sync_gate(stilebase).check(AGENT) == Pass({"payment-response": RECEIPT})


# Review cycle 1


def test_every_call_carries_its_own_budget() -> None:
    async def run() -> None:
        stilebase = FakeStilebase(
            settings_answer=(200, settings(timeout_ms=250, settle_timeout_ms=7000)), decide=(200, SETTLE_AFTER)
        )
        gate = async_gate(stilebase)
        decision = await gate.check(PAYING)
        await gate.check(AGENT)
        assert isinstance(decision, Pass) and decision.hold is not None
        await gate.settle(decision.hold, 200, b"page")
        gate.capture(Capture("t", 10), 200, [], Copy(10), 1)
        await gate.aclose()
        assert stilebase.to("settings")[0].timeout["read"] == 3.0
        assert [c.timeout["read"] for c in stilebase.to("decide")] == [7.0, 0.25]  # a payment's budget, then a person's
        assert stilebase.to("settle")[0].timeout["read"] == 7.0  # never httpx's default 5 s
        assert stilebase.to("captures")[0].timeout["read"] == 10.0

    asyncio.run(run())


def test_settings_that_arent_json_keep_the_last_good_ones() -> None:
    clock = Clock()
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    gate = sync_gate(stilebase, clock)
    gate.check(AGENT)
    stilebase.raw_settings = b"<html>gateway timeout</html>"
    clock.at += 301
    gate.check(AGENT)
    gate.wait()
    assert gate.settings is not None and isinstance(gate.check(AGENT), Respond)
    stilebase.raw_settings = None
    stilebase.settings_answer = (200, settings(settings_version=9))  # a JSON object this version can't read: dropped
    clock.at += 301
    gate.check(AGENT)
    gate.wait()
    assert gate.settings is None and gate.check(AGENT) == Pass()


def test_the_settings_cap_what_a_gate_holds() -> None:
    stilebase = FakeStilebase(
        settings_answer=(200, settings(settle_after={"max_bytes": 100})),
        decide=(200, {"action": "pass", "settle_after": {"max_bytes": 5000}}),
    )
    decision = sync_gate(stilebase).check(PAYING)
    assert isinstance(decision, Pass) and decision.hold is not None and decision.hold.max_bytes == 100


def test_credentials_are_never_sent_whatever_the_settings_say() -> None:
    stilebase = FakeStilebase(settings_answer=(200, settings(strip_headers=["x-secret"])))
    request = GateRequest(
        "GET",
        "/",
        (("Cookie", "s=1"), ("Authorization", "Bearer t"), ("Proxy-Authorization", "p"), ("X-Secret", "x"), ("Accept", "*/*")),
    )
    sync_gate(stilebase).check(request)
    assert stilebase.to("decide")[0].body["headers"] == [["accept", "*/*"]]


def test_the_breaker_reopens_after_one_failure_and_closes_after_one_answer() -> None:
    clock = Clock()
    stilebase = FakeStilebase(decide=(500, {}))
    gate = sync_gate(stilebase, clock)
    for _ in range(5):
        gate.check(AGENT)
    clock.at += 31
    gate.check(AGENT)  # asked again, failed: open again at once (the count was kept)
    assert len(stilebase.to("decide")) == 6
    gate.check(AGENT)
    assert len(stilebase.to("decide")) == 6
    clock.at += 31
    stilebase.decide = (200, PAID)
    gate.check(AGENT)  # one usable answer closes it
    stilebase.decide = (500, {})
    gate.check(AGENT)  # one failure doesn't reopen it
    gate.check(AGENT)
    assert len(stilebase.to("decide")) == 9


@pytest.mark.parametrize(
    ("value", "disabled"), [(" 1 ", True), ("TRUE", True), ("yes", True), ("On", True), ("0", False), ("", False)]
)
def test_stilebase_disabled(monkeypatch: pytest.MonkeyPatch, value: str, disabled: bool) -> None:
    monkeypatch.setenv("STILEBASE_DISABLED", value)
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    gate = SyncGate(GateOptions(key=KEY, api_url=API, transport=stilebase.transport, on_error=lambda e: None))
    assert (gate.check(AGENT) == Pass()) is disabled


def test_only_a_plain_pass_is_remembered() -> None:
    for answer in (
        {**REMEMBER, "response_headers": {"x": "y"}},
        {**REMEMBER, "capture": {"token": "t", "max_bytes": 10}},
    ):
        stilebase = FakeStilebase(decide=(200, answer))
        gate = sync_gate(stilebase)
        gate.check(PERSON)
        gate.check(PERSON)
        assert len(stilebase.to("decide")) == 2


def test_only_reads_share_an_answer_and_never_its_capture() -> None:
    async def run() -> None:
        captured = {**REMEMBER, "capture": {"token": "t", "max_bytes": 10}, "response_headers": {"x": "y"}}
        stilebase = FakeStilebase(decide=(200, captured), delay=0.05)
        gate = async_gate(stilebase)
        await gate.check(AGENT)  # settings
        stilebase.calls.clear()
        read = GateRequest("GET", "/x", (("User-Agent", CHROME),), ip="198.51.100.4")
        results = await asyncio.gather(*(gate.check(read) for _ in range(3)))
        assert len(stilebase.to("decide")) == 1
        assert sorted(r.capture is not None for r in results if isinstance(r, Pass)) == [False, False, True]
        assert all(isinstance(r, Pass) and r.response_headers == {"x": "y"} for r in results)
        stilebase.calls.clear()
        posted = GateRequest("POST", "/x", (("User-Agent", CHROME),), ip="198.51.100.4")
        head = GateRequest("HEAD", "/x", (("User-Agent", CHROME),), ip="198.51.100.4")
        await asyncio.gather(gate.check(posted), gate.check(posted), gate.check(head), gate.check(read))
        assert len(stilebase.to("decide")) == 4  # POSTs alone; HEAD and GET apart (the method is in the key)
        await gate.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("settle_after", [None, True, "yes", 5, {"max_bytes": "10"}, {}])
def test_a_settle_after_that_isnt_an_object_is_unusable(settle_after: Any) -> None:
    stilebase = FakeStilebase(decide=(200, {"action": "pass", "settle_after": settle_after}))
    gate = sync_gate(stilebase)
    assert gate.check(AGENT) == Pass()
    held = gate.check(PAYING)
    assert isinstance(held, Respond) and held.status == 503


# Review cycle 2


def test_half_open_asks_exactly_one_request() -> None:
    async def run() -> None:
        clock = Clock()
        stilebase = FakeStilebase(decide=(500, {}), delay=0.05)
        gate = async_gate(stilebase, clock)
        for _ in range(5):
            await gate.check(AGENT)
        clock.at += 31  # half open
        stilebase.calls.clear()
        results = await asyncio.gather(gate.check(AGENT), gate.check(AGENT), gate.check(PAYING), gate.check(AGENT))
        assert len(stilebase.to("decide")) == 1  # the probe; the others passed (a payment: 503) as while open
        assert sum(isinstance(r, Respond) and r.status == 503 for r in results) == 1
        assert await gate.check(AGENT) == Pass() and len(stilebase.to("decide")) == 1  # its failure reopened it
        clock.at += 31
        stilebase.decide = (200, REQUIRED_402)
        await gate.check(AGENT)  # the probe's usable answer closes it
        await asyncio.gather(*(gate.check(AGENT) for _ in range(3)))  # closed: asked again (alike, so once)
        assert len(stilebase.to("decide")) == 3
        await gate.aclose()

    asyncio.run(run())


def test_a_shared_decision_waiting_past_its_budget_is_forgotten() -> None:
    async def run() -> None:
        stilebase = FakeStilebase(decide=(200, REQUIRED_402))
        gate = async_gate(stilebase)
        await gate.check(AGENT)  # settings
        stilebase.calls.clear()
        settings = gate.settings
        assert settings is not None
        from stilebase_sdk.gate._core import remember_key

        loop = asyncio.get_running_loop()
        stuck: asyncio.Future[Any] = loop.create_future()  # a leader the runtime cancelled: never answered
        gate._asking[f"GET {remember_key(AGENT, settings)}"] = (stuck, loop.time() - 10)
        decision = await gate.check(AGENT)
        assert isinstance(decision, Respond) and decision.status == 402 and len(stilebase.to("decide")) == 1
        await gate.aclose()

    asyncio.run(run())


# Review cycle 3


def test_a_cancelled_request_counts_nothing_for_itself_or_its_waiters() -> None:
    async def run() -> None:
        stilebase = FakeStilebase(decide=(200, REQUIRED_402), delay=0.5)
        gate = async_gate(stilebase)
        stilebase.delay = 0.0
        await gate.check(AGENT)  # settings
        stilebase.delay = 0.5
        leader = asyncio.create_task(gate.check(AGENT))
        await asyncio.sleep(0.05)
        waiter = asyncio.create_task(gate.check(AGENT))
        await asyncio.sleep(0.05)
        leader.cancel()
        assert await waiter == Pass()  # the leader's answer never came: the waiter passes
        assert gate.breaker._failures == 0  # and nothing was counted
        await gate.aclose()

    asyncio.run(run())


def test_a_cancelled_probe_frees_its_slot_without_counting() -> None:
    async def run() -> None:
        clock = Clock()
        stilebase = FakeStilebase(decide=(500, {}))
        gate = async_gate(stilebase, clock)
        for _ in range(5):
            await gate.check(AGENT)
        clock.at += 31  # half open
        stilebase.decide, stilebase.delay = (200, REQUIRED_402), 0.5
        probe = asyncio.create_task(gate.check(AGENT))
        await asyncio.sleep(0.05)
        probe.cancel()
        await asyncio.sleep(0)
        failures = gate.breaker._failures
        stilebase.delay = 0.0
        assert isinstance(await gate.check(AGENT), Respond)  # the next request probes, and closes it
        assert failures == 5 and gate.breaker._failures == 0
        await gate.aclose()

    asyncio.run(run())


def test_a_stale_probe_is_forgotten() -> None:
    from stilebase_sdk.gate._core import Breaker, Settings

    breaker, settings = Breaker(), Settings()
    first = breaker.admit(0, settings, "k", 0.3)
    assert first is not None
    for _ in range(5):
        breaker.record(False, 0, settings, first)
    probe = breaker.admit(31, settings, "k", 0.3)
    assert probe is not None and probe.probe
    assert breaker.admit(31.5, settings, "k", 0.3) is None  # still within the probe's budget plus a second
    newer = breaker.admit(32.5, settings, "k", 0.3)
    assert newer is not None and newer.probe  # forgotten: the next request probes
    breaker.record(False, 32.6, settings, probe)  # the old probe's late failure decides nothing
    breaker.record(True, 32.7, settings, newer)
    assert not breaker.is_open(32.8)


def test_the_first_settings_wait_is_bounded_in_all(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import stilebase_sdk.gate._async as driver

    monkeypatch.setattr(driver, "SETTINGS_TIMEOUT", 0.2)

    async def run() -> float:
        stilebase = FakeStilebase(decide=(200, REQUIRED_402), settings_delay=2.0)
        gate = async_gate(stilebase)
        started = time.monotonic()
        assert await gate.check(AGENT) == Pass()
        elapsed = time.monotonic() - started
        await gate.aclose()
        return elapsed

    assert asyncio.run(run()) < 1.0


def test_the_remember_key_is_computed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import stilebase_sdk.gate._core as core

    calls: list[object] = []
    real = core.remember_key

    def counted(request: GateRequest, settings: Any) -> str:
        calls.append(request)
        return real(request, settings)

    monkeypatch.setattr(core, "remember_key", counted)
    stilebase = FakeStilebase(decide=(200, REMEMBER))
    sync_gate(stilebase).check(PERSON)
    assert len(calls) == 1

    async def run() -> None:
        gate = async_gate(FakeStilebase(decide=(200, REMEMBER)))
        await gate.check(PERSON)
        await gate.aclose()

    calls.clear()
    asyncio.run(run())
    assert len(calls) == 1


def test_a_settle_after_in_a_settle_answer_is_ignored() -> None:
    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER), settle=(200, {**PAID, "settle_after": {"max_bytes": 5}}))
    gate = sync_gate(stilebase)
    decision = gate.check(PAYING)
    assert isinstance(decision, Pass) and decision.hold is not None
    assert gate.settle(decision.hold, 200, b"page") == Pass({"payment-response": RECEIPT})


def test_a_protocol_switch_is_never_settled() -> None:
    stilebase = FakeStilebase()
    outcome = sync_gate(stilebase).settle(Hold({}, 100), 101, b"")
    assert isinstance(outcome, Respond) and outcome.status == 502 and stilebase.to("settle") == []


# Final review


def test_waiting_for_a_free_connection_counts_nothing() -> None:
    import httpx

    def pool_full(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/settings"):
            return httpx.Response(200, json=settings())
        raise httpx.PoolTimeout("no free connection", request=request)

    gate = SyncGate(GateOptions(key=KEY, api_url=API, transport=httpx.MockTransport(pool_full), on_error=lambda e: None))
    for _ in range(20):
        assert gate.check(AGENT) == Pass()
    assert gate.breaker._failures == 0 and not gate.breaker.is_open(0)


def test_intervals_never_follow_the_wall_clock_back() -> None:
    class Back:
        """A wall clock that steps back an hour on every read."""

        def __init__(self) -> None:
            self.at = 1_792_756_800.0

        def __call__(self) -> float:
            self.at -= 3600
            return self.at

    mono = Clock()
    stilebase = FakeStilebase(decide=(500, {}))
    gate = SyncGate(
        GateOptions(key=KEY, api_url=API, transport=stilebase.transport, on_error=lambda e: None, now=Back(), clock=mono)
    )
    for _ in range(5):
        gate.check(AGENT)
    assert gate.check(AGENT) == Pass() and len(stilebase.to("decide")) == 5  # open, whatever the wall clock says
    mono.at += 31
    gate.check(AGENT)  # half open on the monotonic clock: asked
    assert len(stilebase.to("decide")) == 6
    stilebase.decide = (200, REMEMBER)
    mono.at += 31
    gate.check(PERSON)
    gate.check(PERSON)
    assert len(stilebase.to("decide")) == 7  # remembered on the monotonic clock
    mono.at += 601
    gate.check(PERSON)
    assert len(stilebase.to("decide")) == 8  # and forgotten on it


def test_a_probe_that_never_reached_stilebase_frees_its_slot() -> None:
    import httpx

    clock = Clock()
    full = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/settings"):
            return httpx.Response(200, json=settings())
        if full["on"]:
            raise httpx.PoolTimeout("no free connection", request=request)
        return httpx.Response(500, json={})

    gate = SyncGate(
        GateOptions(key=KEY, api_url=API, transport=httpx.MockTransport(handler), on_error=lambda e: None, now=clock, clock=clock)
    )
    for _ in range(5):
        gate.check(AGENT)
    clock.at += 31  # half open
    full["on"] = True
    assert gate.check(AGENT) == Pass()  # the probe waited for a connection: not Stilebase's failure
    assert gate.breaker._failures == 5 and not gate.breaker.is_open(clock.at)  # the slot is free again
    assert gate.breaker.admit(clock.at, gate.settings or settings_object(), "k", 0.3) is not None


def settings_object() -> Any:
    from stilebase_sdk.gate import Settings

    return Settings()


# The last round


def test_both_gates_get_the_one_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from stilebase_sdk.gate._http import LIMITS

    given: list[Any] = []

    class Recording(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            given.append(kwargs.get("limits"))
            super().__init__(**kwargs)

    class RecordingAsync(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            given.append(kwargs.get("limits"))
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", Recording)
    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsync)
    sync = SyncGate(GateOptions(key=KEY, api_url=API, on_error=lambda e: None))
    sync.close()

    async def run() -> None:
        gate = AsyncGate(GateOptions(key=KEY, api_url=API, on_error=lambda e: None))
        assert gate.http is not None
        await gate.aclose()

    asyncio.run(run())
    assert given == [LIMITS, LIMITS]
    assert (LIMITS.max_connections, LIMITS.max_keepalive_connections) == (1000, 100)


def test_requests_queued_for_a_connection_count_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real AsyncClient, a slow local server and one connection: the request holding it times out (Stilebase slow:
    one failure); the ones still waiting for the connection give up first, as pool waits, and count nothing."""
    import http.server
    import json
    import threading
    import time

    import httpx

    import stilebase_sdk.gate._async as driver

    class Slow(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._answer(json.dumps(settings(timeout_ms=300)).encode())

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", "0")))
            time.sleep(1.0)  # Stilebase too slow for the 300 ms budget
            self._answer(b'{"action":"pass"}')

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(driver, "LIMITS", httpx.Limits(max_connections=1, max_keepalive_connections=1))
    try:

        async def run() -> int:
            api = f"http://127.0.0.1:{server.server_address[1]}"
            gate = AsyncGate(GateOptions(key=KEY, api_url=api, on_error=lambda e: None))
            await gate.check(GateRequest("GET", "/static/x.css"))  # loads the settings (skipped path: nothing asked)
            many = [GateRequest("GET", f"/p/{i}", (("User-Agent", GPTBOT),)) for i in range(4)]
            await asyncio.gather(*(gate.check(r) for r in many))
            failures = gate.breaker._failures
            await gate.aclose()
            return failures

        assert asyncio.run(run()) == 1
    finally:
        server.shutdown()
