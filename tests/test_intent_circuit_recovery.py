# -*- coding: utf-8 -*-
"""Intent circuit breaker: bounded cooldown + controlled recovery.

Offline: ``_generate`` is a local fake and the breaker's monotonic clock is
patched, so no test sleeps for the real cooldown and no Gemini call is ever
made.

Covered:

- a transient failure opens a bounded cooldown (deterministic fallback is
  served, no AI request per message while failing);
- recovery after the cooldown through exactly ONE probe;
- a failed probe re-opens the cooldown (still no retry loop);
- a timeout / unrelated exception never disabling intent for the process;
- concurrent requests sharing a single probe (no retry storm);
- the deterministic fallback staying functional while the circuit is open.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from services import query_intent
from services.query_intent import (
    INTENT_AI_STATS,
    INTENT_CIRCUIT_COOLDOWN_SECONDS,
    is_circuit_open,
    parse_query_intent,
    reset_ai_state,
)

GOOD_INTENT = json.dumps({"product_type": "зарядка"})


def run(coro):
    return asyncio.run(coro)


def parse(text, ai=None):
    """Parse through the real public entry point (``ai`` may be a mock)."""

    return run(parse_query_intent(text, ai))


class FakeAI:
    """Minimal ``AIService`` stand-in: scripted answer, counted calls."""

    def __init__(self, result=None, *, exc=None, delay: float = 0.0) -> None:
        self._client = object()
        self.fast_model = "gemini-test"
        self.last_model = None
        self.calls = 0
        self._result = result
        self._exc = exc
        self._delay = delay

    async def _generate(self, contents, *, models=None):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._result


class _ClockedTestCase(unittest.TestCase):
    """Base class with a controllable clock for the breaker."""

    def setUp(self) -> None:
        reset_ai_state()
        self._real_clock = query_intent._clock
        self.now = 1_000.0
        query_intent._clock = lambda: self.now

    def tearDown(self) -> None:
        query_intent._clock = self._real_clock
        reset_ai_state()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def advance_past_cooldown(self) -> None:
        self.advance(INTENT_CIRCUIT_COOLDOWN_SECONDS + 1.0)


class CircuitCooldownTests(_ClockedTestCase):
    def test_failure_opens_a_bounded_cooldown_without_extra_calls(self) -> None:
        ai = FakeAI(result=None)  # hard failure -> circuit opens
        _, meta1 = parse("наушники", ai)
        self.assertEqual(meta1.error, "ai_returned_none")
        self.assertTrue(is_circuit_open())
        self.assertEqual(INTENT_AI_STATS["circuit_opens"], 1)

        # While the cooldown lasts: deterministic fallback, ZERO AI requests
        # (the old behaviour issued none too, but it never recovered).
        for query in ("зарядка для iPhone", "до 3000 грн", "SSD для ноутбука"):
            intent, meta = parse(query, ai)
            self.assertEqual(meta.source, "fallback")
            self.assertEqual(meta.error, "circuit_open")
        self.assertEqual(ai.calls, 1)
        self.assertEqual(INTENT_AI_STATS["attempted"], 1)

    def test_deterministic_fallback_still_works_while_open(self) -> None:
        parse("наушники", FakeAI(result=None))
        intent, meta = parse(
            "зарядка для iPhone 15 Pro до 2000 грн", FakeAI(result=None)
        )
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.budget.max, 2000)

    def test_recovery_after_the_cooldown_uses_one_probe(self) -> None:
        failing = FakeAI(result=None)
        parse("наушники", failing)
        self.assertEqual(failing.calls, 1)

        self.advance_past_cooldown()
        healthy = FakeAI(result=GOOD_INTENT)
        intent, meta = parse("зарядка для iPhone", healthy)

        self.assertEqual(healthy.calls, 1)  # exactly one recovery probe
        self.assertEqual(meta.source, "ai")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertFalse(is_circuit_open())

        # Circuit fully closed again: the next message is an ordinary call.
        _, meta2 = parse("SSD для ноутбука", healthy)
        self.assertEqual(meta2.source, "ai")
        self.assertEqual(healthy.calls, 2)

    def test_failed_probe_reopens_the_cooldown_still_without_storm(self) -> None:
        failing = FakeAI(result=None)
        parse("наушники", failing)
        self.assertEqual(failing.calls, 1)

        self.advance_past_cooldown()
        _, meta = parse("зарядка", failing)  # probe: still failing
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(failing.calls, 2)
        self.assertTrue(is_circuit_open())
        self.assertEqual(INTENT_AI_STATS["circuit_opens"], 2)

        # Still inside the new cooldown: no further AI request.
        self.advance(INTENT_CIRCUIT_COOLDOWN_SECONDS - 1.0)
        parse("SSD", failing)
        self.assertEqual(failing.calls, 2)

        # The next window allows exactly one more probe.
        self.advance(1.0)
        parse("флешка", failing)
        self.assertEqual(failing.calls, 3)

    def test_transient_exception_does_not_disable_intent_forever(self) -> None:
        # A timeout (or any unrelated transient exception) opens the circuit,
        # but the cooldown guarantees AI intent is offered again later.
        flaky = FakeAI(exc=TimeoutError("simulated timeout"))
        _, meta = parse("наушники", flaky)
        self.assertTrue(meta.error.startswith("ai_exception:"), meta.error)
        self.assertTrue(is_circuit_open())

        self.advance_past_cooldown()
        healthy = FakeAI(result=GOOD_INTENT)
        intent, meta = parse("зарядка для iPhone", healthy)
        self.assertEqual(meta.source, "ai")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertFalse(is_circuit_open())

    def test_concurrent_requests_share_a_single_probe(self) -> None:
        # Open the circuit first, then let the cooldown expire.
        parse("наушники", FakeAI(result=None))
        self.advance_past_cooldown()

        async def scenario():
            ai = FakeAI(result=GOOD_INTENT, delay=0.05)
            queries = ["наушники", "зарядка", "чехол", "SSD", "флешка"]
            results = await asyncio.gather(
                *(parse_query_intent(q, ai) for q in queries)
            )
            return ai, results

        ai, results = run(scenario())

        # ONE probe request for five concurrent messages: no retry storm.
        self.assertEqual(ai.calls, 1)
        sources = [meta.source for _, meta in results]
        self.assertEqual(sources.count("ai"), 1)
        self.assertEqual(sources.count("fallback"), 4)
        for _, meta in results:
            if meta.source == "fallback":
                self.assertEqual(meta.error, "circuit_open")


if __name__ == "__main__":
    unittest.main()