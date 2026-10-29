# -*- coding: utf-8 -*-
"""Gemini failure observability: classification + one safe log detail.

Fully offline: the Gemini "client" is a local stub and the exceptions are
``google.genai.errors`` instances built in-process (google-genai 2.24.0).
Nothing ever leaves the process and no real key exists anywhere.

Covered:

- classification of representative SDK exceptions (429 / 403 / 404 / 400 /
  5xx), timeouts, transport failures and unknown exceptions;
- structured status beating a message substring (no quota label without
  structured evidence);
- the logged detail fragment: model, exception class, HTTP status, API
  status, origin, classification and fallback decision - with the API key,
  prompt text and user text kept out of the log;
- unchanged fallback policy: quota stops, 404 tries the fallback model
  exactly once.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import httpx
from google.genai.errors import ClientError, ServerError

from services.ai_service import (
    AIService,
    _classify_gemini_error,
    _format_gemini_error,
    _sanitize_error_message,
)

#: Shape of a real Google API key, but obviously fake.
FAKE_KEY = "AIza" + "FakeKey0123456789FakeKey0123456789ab"

USER_TEXT = "секретный пользовательский запрос юзера"


def run(coro):
    return asyncio.run(coro)


def api_error(cls, code: int, status: str, message: str = "provider said no"):
    """Build a real ``google.genai.errors`` instance the way the SDK does."""

    return cls(code, {"error": {"status": status, "message": message}})


class _FakeModels:
    """Mimics ``client.aio.models``: each call pops the next scripted step."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    async def generate_content(self, *, model, contents):
        self.calls.append(model)
        if not self._script:
            raise AssertionError("unexpected extra Gemini call")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(text=step)


class _FakeClient:
    def __init__(self, script) -> None:
        self.aio = SimpleNamespace(models=_FakeModels(script))


def make_service(script, *, api_key=None, fast="gemini-test-fast",
                 fallback="gemini-test-fallback") -> AIService:
    """An ``AIService`` whose SDK calls are scripted - never sent."""

    service = AIService(
        api_key=None,
        model=fast,
        fast_model=fast,
        vision_model=fast,
        fallback_model=fallback,
    )
    service._api_key = api_key  # stored only for log redaction
    service._client = _FakeClient(script)
    return service


class ErrorClassificationTests(unittest.TestCase):
    def test_structured_quota_status_is_quota(self) -> None:
        error = api_error(ClientError, 429, "RESOURCE_EXHAUSTED")
        self.assertEqual(_classify_gemini_error(error), "quota")

    def test_permission_and_authentication_failures_are_auth(self) -> None:
        denied = api_error(ClientError, 403, "PERMISSION_DENIED")
        unauthenticated = api_error(ClientError, 401, "UNAUTHENTICATED")
        self.assertEqual(_classify_gemini_error(denied), "auth")
        self.assertEqual(_classify_gemini_error(unauthenticated), "auth")

    def test_model_unavailable_keeps_the_legacy_fallback_kind(self) -> None:
        # 404 NOT_FOUND and 503 UNAVAILABLE are the documented "this model
        # is unavailable" cases: they still advance to the fallback model.
        missing = api_error(ClientError, 404, "NOT_FOUND")
        unavailable = api_error(ServerError, 503, "UNAVAILABLE")
        self.assertEqual(_classify_gemini_error(missing), "model")
        self.assertEqual(_classify_gemini_error(unavailable), "model")

    def test_invalid_request_and_other_server_errors(self) -> None:
        invalid = api_error(ClientError, 400, "INVALID_ARGUMENT")
        server = api_error(ServerError, 500, "INTERNAL")
        self.assertEqual(_classify_gemini_error(invalid), "invalid")
        self.assertEqual(_classify_gemini_error(server), "server")

    def test_timeouts_and_transport_failures_are_distinguished(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        transport = httpx.ConnectError("connection refused", request=request)
        read_timeout = httpx.ReadTimeout("too slow", request=request)
        self.assertEqual(_classify_gemini_error(TimeoutError()), "timeout")
        self.assertEqual(_classify_gemini_error(read_timeout), "timeout")
        self.assertEqual(_classify_gemini_error(transport), "transport")

    def test_unknown_exception_stays_safely_classified(self) -> None:
        self.assertEqual(_classify_gemini_error(RuntimeError("boom")), "other")
        self.assertEqual(_classify_gemini_error(ValueError("nope")), "other")

    def test_nested_http_status_is_used_when_present(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )
        self.assertEqual(_classify_gemini_error(error), "quota")

    def test_markers_are_used_only_without_structured_status(self) -> None:
        # Legacy plain exceptions keep their historical classification.
        self.assertEqual(
            _classify_gemini_error(RuntimeError("429 resource_exhausted quota")),
            "quota",
        )
        self.assertEqual(
            _classify_gemini_error(RuntimeError("404 model not found")), "model"
        )

    def test_structured_status_beats_a_message_substring(self) -> None:
        # The message says "quota", the API says INVALID_ARGUMENT: structure
        # wins, so a substring can never fake a quota failure.
        error = api_error(
            ClientError, 400, "INVALID_ARGUMENT", "quota field is malformed"
        )
        self.assertEqual(_classify_gemini_error(error), "invalid")


class StructuredLoggingTests(unittest.TestCase):
    def test_quota_failure_logs_status_and_model_without_secrets(self) -> None:
        service = make_service(
            [api_error(ClientError, 429, "RESOURCE_EXHAUSTED",
                       f"quota exceeded for key={FAKE_KEY}")],
            api_key=FAKE_KEY,
        )
        with self.assertLogs("services.ai_service", level="WARNING") as captured:
            parsed = run(service.parse_query(USER_TEXT))
        joined = "\n".join(captured.output)

        self.assertIn("Gemini quota/rate limit reached", joined)
        self.assertIn("model=gemini-test-fast", joined)
        self.assertIn("class=ClientError", joined)
        self.assertIn("kind=quota", joined)
        self.assertIn("http=429", joined)
        self.assertIn("api_status=RESOURCE_EXHAUSTED", joined)
        self.assertIn("origin=api", joined)
        self.assertIn("fallback=no", joined)

        # Never the key, the prompt or the user's text.
        self.assertNotIn(FAKE_KEY, joined)
        self.assertNotIn("AIza", joined)
        self.assertNotIn(USER_TEXT, joined)
        self.assertNotIn("Ты помощник", joined)  # QUERY_PARSING_PROMPT

        # Policy unchanged: quota stops, the fallback model is not attempted.
        self.assertEqual(service._client.aio.models.calls, ["gemini-test-fast"])
        self.assertEqual(service.parse_fallback, 1)
        self.assertIsNotNone(parsed)  # deterministic parser still answered

    def test_model_unavailable_logs_and_tries_the_fallback_once(self) -> None:
        service = make_service(
            [api_error(ClientError, 404, "NOT_FOUND", "model not found"),
             '{"order": []}'],
        )
        with self.assertLogs("services.ai_service", level="WARNING") as captured:
            text = run(service._generate("standalone prompt"))
        joined = "\n".join(captured.output)

        self.assertIsNotNone(text)
        self.assertEqual(
            service._client.aio.models.calls,
            ["gemini-test-fast", "gemini-test-fallback"],
        )
        self.assertEqual(service.last_model, "gemini-test-fallback")
        self.assertIn("Gemini model gemini-test-fast is unavailable", joined)
        self.assertIn("class=ClientError", joined)
        self.assertIn("kind=model", joined)
        self.assertIn("http=404", joined)
        self.assertIn("fallback=yes", joined)

    def test_unknown_error_is_logged_with_full_exception_information(self) -> None:
        service = make_service([RuntimeError("boom"), "never-reached"])
        with self.assertLogs("services.ai_service", level="ERROR") as captured:
            text = run(service._generate("standalone prompt"))
        joined = "\n".join(captured.output)

        self.assertIsNone(text)
        self.assertEqual(service._client.aio.models.calls, ["gemini-test-fast"])
        self.assertIn(
            "Gemini request failed; continuing without AI assistance", joined
        )
        self.assertIn("class=RuntimeError", joined)
        self.assertIn("kind=other", joined)
        self.assertIn("origin=message", joined)
        self.assertIn("fallback=no", joined)
        self.assertIn("boom", joined)  # original message kept (sanitized)

    def test_timeout_is_logged_as_timeout_without_retrying(self) -> None:
        service = make_service([TimeoutError("too slow"), "never-reached"])
        with self.assertLogs("services.ai_service", level="ERROR") as captured:
            text = run(service._generate("standalone prompt"))
        joined = "\n".join(captured.output)

        self.assertIsNone(text)
        self.assertEqual(service._client.aio.models.calls, ["gemini-test-fast"])
        self.assertIn("kind=timeout", joined)
        self.assertIn("class=TimeoutError", joined)

    def test_detail_fragment_never_contains_request_contents(self) -> None:
        detail = _format_gemini_error(
            api_error(ClientError, 429, "RESOURCE_EXHAUSTED"),
            model="gemini-test-fast",
            kind="quota",
            will_fallback=False,
        )
        self.assertNotIn("standalone prompt", detail)
        self.assertNotIn("contents", detail)


class SanitizerTests(unittest.TestCase):
    def test_key_shapes_are_redacted(self) -> None:
        clean = _sanitize_error_message(
            f"bad key={FAKE_KEY} and AIzaAnotherKey0123456789abcdefghij"
        )
        self.assertNotIn(FAKE_KEY, clean)
        self.assertNotIn("AIzaAnotherKey0123456789abcdefghij", clean)
        self.assertIn("[REDACTED]", clean)

    def test_exact_api_key_is_removed_verbatim(self) -> None:
        clean = _sanitize_error_message(
            f"rejected value {FAKE_KEY} at index 7", api_key=FAKE_KEY
        )
        self.assertNotIn(FAKE_KEY, clean)
        self.assertIn("rejected value [REDACTED] at index 7", clean)

    def test_message_is_bounded(self) -> None:
        clean = _sanitize_error_message("x" * 5000, limit=300)
        self.assertEqual(len(clean), len("x" * 300 + "...[truncated]"))
        self.assertTrue(clean.endswith("...[truncated]"))


if __name__ == "__main__":
    unittest.main()