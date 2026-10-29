"""Diagnostics of the AliExpress client: latest-call error state + 405 class.

No network: every response is an ``httpx.MockTransport`` answer. The rules
under test (both observed during live acceptance):

* ``last_error_class`` describes the LATEST call only - a success after a
  failure must clear it, otherwise ``/admin`` keeps reporting an error that
  has already been recovered from;
* the documented empty result (``resp_code=405`` + "The result is empty") is
  a real answer to a real query: counted as a success, tracked separately as
  ``empty_results`` and never as an API failure;
* every OTHER non-200 code - and every transport/signature failure - stays
  a visible error.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from services.marketplaces.aliexpress_client import (
    AliExpressApiError,
    AliExpressClient,
    is_empty_result,
)

CREDENTIALS = MarketplaceCredentials("key", "secret")


def make_client(handler) -> AliExpressClient:
    """A client whose HTTP layer is fully replaced by a mock."""

    return AliExpressClient(
        CREDENTIALS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def json_handler(payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def empty_result_payload(
    code: str = "405", message: str = "The result is empty"
) -> dict:
    """The documented affiliate shape for a non-200 business answer."""

    return {
        "aliexpress_affiliate_product_query_response": {
            "resp_result": {
                "resp_code": code,
                "resp_msg": message,
                "result": {"products": {}},
            }
        }
    }


def ok_payload(code: str = "200") -> dict:
    return {
        "aliexpress_affiliate_product_query_response": {
            "resp_result": {
                "resp_code": code,
                "result": {"products": {"product": []}},
            }
        }
    }


def call(client: AliExpressClient) -> dict:
    return asyncio.run(client.call("aliexpress_affiliate_product_query", {}))


class EmptyResultClassificationTests(unittest.TestCase):
    """Only the documented combination counts as an empty result."""

    def test_documented_empty_result_is_recognized(self) -> None:
        self.assertTrue(is_empty_result(405, "The result is empty"))
        self.assertTrue(is_empty_result("405", "  the result is empty  "))

    def test_a_bare_code_is_never_enough(self) -> None:
        # 405 alone can also be an auth/permission failure, so the message has
        # to confirm the empty meaning before it is treated as one.
        self.assertFalse(is_empty_result(405, "Invalid signature"))
        self.assertFalse(is_empty_result(405, ""))
        self.assertFalse(is_empty_result(405, None))

    def test_other_codes_are_never_empty_results(self) -> None:
        for code in ("200", "500", "403", "400", ""):
            with self.subTest(code=code):
                self.assertFalse(is_empty_result(code, "The result is empty"))



class ErrorStateResetTests(unittest.TestCase):
    """``last_error_class`` must always describe the latest call."""

    def test_success_clears_a_previous_error(self) -> None:
        client = make_client(
            json_handler({"error_response": {"msg": "boom", "code": 500}})
        )
        with self.assertRaises(AliExpressApiError):
            call(client)
        self.assertIsNotNone(client.last_error_class)
        self.assertEqual(client.calls_err, 1)

        # A healthy call afterwards must clear the state completely.
        client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(json_handler(ok_payload()))
        )
        result = call(client)

        self.assertIsNone(client.last_error_class)
        self.assertEqual(result.get("resp_code"), "200")
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 1)

    def test_connection_failure_records_a_class(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = make_client(boom)
        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(client.last_error_class, "ConnectError")
        self.assertIsNone(client.last_ok_at)
        self.assertEqual(client.calls_err, 1)
    def test_envelope_error_response_is_recorded_as_a_failure(self) -> None:
        # `error_response` is the TOP envelope for signature/auth rejections.
        # It is not a result, so it must count as a failure - otherwise a dead
        # app key would look like a perfectly healthy client in /admin.
        client = make_client(
            json_handler(
                {"error_response": {"msg": "Invalid signature", "code": 25}}
            )
        )

        with self.assertRaises(AliExpressApiError) as caught:
            call(client)

        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.empty_results, 0)
        self.assertIn("25", client.last_error_class)
        self.assertEqual(caught.exception.code, "25")

    def test_non_json_answer_is_recorded_as_a_failure(self) -> None:
        def html(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>gateway</html>")

        client = make_client(html)
        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.last_error_class, "NonJsonResponse")


    def test_http_status_error_records_the_status(self) -> None:
        client = make_client(
            json_handler({"error_response": {"msg": "nope"}}, status=503)
        )
        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(client.last_error_class, "HTTP 503")
        self.assertEqual(client.calls_err, 1)

    def test_business_error_records_the_resp_code(self) -> None:
        client = make_client(
            json_handler(empty_result_payload(code="500", message="System error"))
        )
        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(client.last_error_class, "resp_code=500")
        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.empty_results, 0)


class EmptyResultIsNotAFailureTests(unittest.TestCase):
    """An empty answer counts as a successful call, not as an error."""

    def test_documented_empty_result_is_a_successful_call(self) -> None:
        client = make_client(json_handler(empty_result_payload()))

        result = call(client)

        self.assertEqual(result, {})
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 0)
        self.assertEqual(client.empty_results, 1)
        self.assertIsNotNone(client.last_empty_at)
        self.assertIsNone(client.last_error_class)

    def test_empty_result_clears_an_earlier_failure(self) -> None:
        client = make_client(
            json_handler({"error_response": {"msg": "boom", "code": 500}})
        )
        with self.assertRaises(AliExpressApiError):
            call(client)

        client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(json_handler(empty_result_payload()))
        )
        call(client)

        self.assertIsNone(client.last_error_class)
        self.assertEqual(client.empty_results, 1)
        self.assertEqual(client.calls_err, 1)

    def test_405_with_a_different_message_stays_an_error(self) -> None:
        # Suppressing every 405 would hide a real authentication/signature
        # problem, which is exactly what must stay visible in /admin.
        client = make_client(
            json_handler(empty_result_payload(message="Invalid app key"))
        )

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.empty_results, 0)
        self.assertEqual(client.last_error_class, "resp_code=405")


class AdminApiDiagnosticsTests(unittest.TestCase):
    """The /admin API screen must show empty answers apart from failures."""

    def _screen(self, client) -> str:
        import asyncio

        from handlers import admin
        from tests.test_ai_menu_handler import FakeContext

        class Provider:
            display_name = "AliExpress"
            is_configured = True

            def __init__(self, inner):
                self._client = inner

        class SearchService:
            def __init__(self, providers):
                self.providers = providers

        context = FakeContext(
            {"search_service": SearchService([Provider(client)])}
        )
        text, _keyboard = asyncio.run(admin._screen_api(context, "7", 0))
        return text

    def _client(self, **overrides):
        from services.marketplaces.aliexpress_client import AliExpressClient

        class FakeClient(AliExpressClient):
            def __init__(self):
                self.calls_ok = 0
                self.calls_err = 0
                self.empty_results = 0
                self.last_ok_at = None
                self.last_error_class = None
                for name, value in overrides.items():
                    setattr(self, name, value)

        return FakeClient()

    def test_empty_results_are_reported_separately_from_errors(self) -> None:
        text = self._screen(
            self._client(
                calls_ok=5,
                calls_err=2,
                empty_results=3,
                last_ok_at="2026-09-25 10:00:00",
                last_error_class="HTTP 500",
            )
        )

        self.assertIn("Успешных вызовов: 5", text)
        self.assertIn("Ошибок: 2", text)
        self.assertIn("Пустых результатов: 3", text)
        self.assertIn("HTTP 500", text)

    def test_a_healthy_client_shows_no_error_class(self) -> None:
        text = self._screen(
            self._client(
                calls_ok=7,
                calls_err=0,
                empty_results=1,
                last_ok_at="2026-09-25 10:00:00",
            )
        )

        self.assertIn("Ошибок: 0", text)
        self.assertIn("Пустых результатов: 1", text)
        self.assertIn("Последний класс ошибки: —", text)


if __name__ == "__main__":
    unittest.main()

