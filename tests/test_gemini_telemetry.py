"""Focused tests for the measurement-only Gemini instrumentation.

Covers: usage_metadata extraction (complete/partial/missing), failure and
timeout recording without behavior change, per-turn attempt counting via the
context-scoped collector, concurrency isolation, agent-loop attempt counting
without duplication, and that existing failure/return behavior is preserved.
"""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from services import gemini_telemetry
from services.ai_service import AIService
from services.gemini_telemetry import (
    GeminiAttempt,
    extract_usage,
    record_attempt,
    turn_scope,
)


def _response(text="ok", **tokens):
    """A fake SDK response: usage fields present only when passed."""
    return types.SimpleNamespace(text=text, usage_metadata=types.SimpleNamespace(**tokens))


class _FakeModels:
    """Callable fake for ``client.aio.models.generate_content``."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def generate_content(self, **kwargs):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ai_with(results):
    """An AIService whose fake client issues the given results in order."""
    ai = AIService(api_key="test-key-123", model="gemini-test")
    fake = _FakeModels(results)
    ai._client = types.SimpleNamespace(aio=types.SimpleNamespace(models=fake))
    return ai, fake


class ExtractUsageTests(unittest.TestCase):
    def test_complete_metadata(self):
        response = _response(
            prompt_token_count=10,
            candidates_token_count=20,
            total_token_count=30,
            cached_content_token_count=0,
            thoughts_token_count=5,
        )
        self.assertEqual(
            extract_usage(response),
            {
                "prompt_token_count": 10,
                "candidates_token_count": 20,
                "total_token_count": 30,
                "cached_content_token_count": 0,
                "thoughts_token_count": 5,
            },
        )

    def test_missing_metadata(self):
        self.assertIsNone(extract_usage(_response()))
        self.assertIsNone(extract_usage(types.SimpleNamespace(text="x")))
        self.assertIsNone(extract_usage(None))

    def test_partial_metadata(self):
        response = _response(prompt_token_count=7, total_token_count=None)
        self.assertEqual(extract_usage(response), {"prompt_token_count": 7})

    def test_non_integer_values_dropped(self):
        response = _response(prompt_token_count="12", candidates_token_count=1.5)
        self.assertIsNone(extract_usage(response))


class TurnScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_summary_counts_and_partial_flag(self):
        with turn_scope("t") as usage:
            record_attempt(
                operation="generate", model="m", duration_ms=10.0,
                outcome="ok", response=_response(prompt_token_count=10),
            )
            record_attempt(
                operation="generate", model="m2", duration_ms=5.0,
                outcome="error", error=True,
            )
        self.assertEqual(len(usage.attempts), 2)
        self.assertTrue(usage.tokens_partial)
        self.assertIn("attempts=2", usage.summary())
        self.assertIn("partial=true", usage.summary())

    def test_success_attempt_with_metadata_not_partial(self):
        with turn_scope("t") as usage:
            record_attempt(
                operation="generate", model="m", duration_ms=1.0,
                outcome="ok", response=_response(prompt_token_count=3),
            )
        self.assertFalse(usage.tokens_partial)
        self.assertEqual(usage.totals().get("prompt_token_count"), 3)


class _GenerateInstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_attempts(self):
        ai, _ = _ai_with(
            [_response(text="  first  ", prompt_token_count=11,
                       candidates_token_count=2, total_token_count=13)]
        )
        with turn_scope("search") as usage:
            raw = await ai._generate("prompt", models=("m1",))
        self.assertEqual(raw, "first")
        self.assertEqual(len(usage.attempts), 1)
        attempt = usage.attempts[0]
        self.assertEqual(attempt.operation, "generate")
        self.assertEqual(attempt.outcome, "ok")
        self.assertEqual(attempt.tokens["total_token_count"], 13)

    async def test_primary_failure_then_fallback_success_counts_two_attempts(self):
        class _ModelUnavailable(Exception):
            status_code = 503  # classifies as kind=model -> fallback attempted

        ai, fake = _ai_with(
            [
                _ModelUnavailable("503 unavailable"),
                _response(text="ok", prompt_token_count=1, total_token_count=1),
            ]
        )
        with turn_scope("search") as usage:
            raw = await ai._generate("prompt", models=("primary",), operation="parse_query")
        self.assertEqual(raw, "ok")
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(usage.attempts), 2)
        self.assertEqual(
            [a.model for a in usage.attempts], ["primary", "gemini-3.1-flash-lite"]
        )
        self.assertEqual(usage.attempts[0].outcome, "error")
        self.assertEqual(usage.attempts[0].operation, "parse_query")
        self.assertTrue(usage.tokens_partial)

    async def test_timeout_recorded_without_behavior_change(self):
        async def hang(**kwargs):
            await asyncio.sleep(5)

        ai, _ = _ai_with([])
        ai._client = types.SimpleNamespace(
            aio=types.SimpleNamespace(
                models=types.SimpleNamespace(generate_content=hang)
            )
        )
        ai.timeout_seconds = 0.05
        with turn_scope("search") as usage:
            raw = await ai._generate("prompt", models=("m1",))
        self.assertIsNone(raw)
        self.assertEqual(len(usage.attempts), 1)
        self.assertEqual(usage.attempts[0].outcome, "timeout")
        self.assertIsNone(usage.attempts[0].tokens)

    async def test_empty_response_records_empty_outcome(self):
        empty = _response(prompt_token_count=1, total_token_count=2)
        empty.text = None
        ai, _ = _ai_with([empty])
        with turn_scope("search") as usage:
            raw = await ai._generate("prompt", models=("m1",))
        self.assertIsNone(raw)  # unchanged behavior: empty answer -> None
        self.assertEqual(usage.attempts[0].outcome, "empty")
        self.assertEqual(usage.attempts[0].tokens, {"prompt_token_count": 1,
                                                   "total_token_count": 2})

    def test_no_scope_still_logs(self):
        # Outside any scope the attempt is only logged, never aggregated.
        record_attempt(
            operation="generate", model="m", duration_ms=1.0, outcome="ok",
            response=_response(prompt_token_count=1),
        )


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_searches_isolated(self):
        ai, _ = _ai_with(
            [
                _response(text="a1", prompt_token_count=10, total_token_count=10),
                _response(text="b1", prompt_token_count=100, total_token_count=100),
                _response(text="a2", prompt_token_count=1, total_token_count=1),
                _response(text="b2", prompt_token_count=200, total_token_count=200),
            ]
        )

        async def search(tag):
            with turn_scope(tag) as usage:
                await ai._generate("p", models=("m",))
                await asyncio.sleep(0.01)
                await ai._generate("p", models=("m",))
                return usage

        a, b = await asyncio.gather(search("a"), search("b"))
        self.assertEqual(sum(t["prompt_token_count"] for t in [x.tokens for x in a.attempts]), 11)
        self.assertEqual(sum(t["prompt_token_count"] for t in [x.tokens for x in b.attempts]), 300)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def _run_loop(self, ai, results):
        from services.agent_loop import run_agent_loop

        async def run_tool(name, args, ctx, search_fn=None):
            return types.SimpleNamespace(response={})

        return run_agent_loop(
            ai, "hello", types.SimpleNamespace(shown_products=[]),
            max_rounds=1, search_fn=None, run_tool=run_tool,
        )

    async def test_agent_rounds_counted_once_per_round(self):
        # Round 1: a tool call; round 2: final text. Two remote attempts.
        ai, fake = _ai_with([])

        call_part = types.SimpleNamespace(
            function_call=types.SimpleNamespace(name="get_shown_products", args={}, id="1")
        )
        final = _response(text="done", prompt_token_count=5, total_token_count=7)
        call = types.SimpleNamespace(
            text=None,
            candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=[call_part]))],
        )
        final.candidates = []
        ai._client = types.SimpleNamespace(
            aio=types.SimpleNamespace(
                models=types.SimpleNamespace(
                    generate_content=AsyncMock(side_effect=[call, final])
                )
            )
        )
        with turn_scope("agent") as usage:
            result = await self._run_loop(ai, None)
        self.assertEqual(result["text"], "done")
        self.assertEqual(len(usage.attempts), 2)  # two rounds, counted once each
        self.assertTrue(all(a.operation == "agent_round" for a in usage.attempts))
        self.assertEqual(usage.attempts[1].tokens.get("total_token_count"), 7)

    async def test_agent_failure_recorded_with_timeout_outcome(self):
        ai, _ = _ai_with([])

        async def hang(**kwargs):
            await asyncio.sleep(5)

        ai._client = types.SimpleNamespace(
            aio=types.SimpleNamespace(
                models=types.SimpleNamespace(generate_content=hang)
            )
        )
        ai.timeout_seconds = 0.05
        with turn_scope("agent") as usage:
            result = await self._run_loop(ai, None)
        self.assertTrue(result["failed"])
        self.assertEqual(len(usage.attempts), 1)
        self.assertEqual(usage.attempts[0].outcome, "timeout")


if __name__ == "__main__":
    unittest.main()
