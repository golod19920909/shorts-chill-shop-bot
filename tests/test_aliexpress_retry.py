"""Retry policy of the AliExpress client (documented transient only).

No network: every answer is an ``httpx.MockTransport`` handler that can
count how many times it was actually called. The rules under test:

* timeouts, transport failures and 5xx gateway answers are retried a
  bounded number of times (``max_retries``, default 1) with backoff;
* rate-limit answers (HTTP 429), business errors (``error_response``,
  non-200 ``resp_code``) and malformed payloads are NEVER retried;
* a retry that fixes the call counts as a success (``calls_err == 0``),
  while a call that exhausts the budget counts as ONE error;
* retries are visible in diagnostics via ``retries_spent``.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from services.marketplaces.aliexpress_client import (
    AliExpressApiError,
    AliExpressClient,
)

CREDENTIALS = MarketplaceCredentials("key", "secret")


def ok_payload() -> dict:
    return {
        "aliexpress_affiliate_product_query_response": {
            "resp_result": {
                "resp_code": "200",
                "result": {"products": {"product": []}},
            }
        }
    }


def error_response_payload() -> dict:
    return {"error_response": {"msg": "Invalid signature", "code": 25}}


class CountingTransport:
    """Handler sequence: pops one scripted response per attempt."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            status, payload = step
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json=step)


def make_client(script: list, **kwargs) -> tuple[AliExpressClient, CountingTransport]:
    transport = CountingTransport(script)
    kwargs.setdefault("retry_backoff", 0.0)  # never sleep in tests
    client = AliExpressClient(
        CREDENTIALS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
        **kwargs,
    )
    return client, transport


def call(client: AliExpressClient) -> dict:
    return asyncio.run(client.call("aliexpress_affiliate.product.query", {}))


class TransientRetryTests(unittest.TestCase):
    def test_timeout_then_success_is_recovered(self) -> None:
        client, transport = make_client(
            [httpx.TimeoutException("late", request=None), ok_payload()]
        )

        call(client)

        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 0)
        self.assertEqual(client.retries_spent, 1)
        self.assertIsNone(client.last_error_class)

    def test_gateway_5xx_then_success_is_recovered(self) -> None:
        client, transport = make_client([(503, {}), ok_payload()])

        call(client)

        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 0)
        self.assertEqual(client.retries_spent, 1)

    def test_connect_error_then_success_is_recovered(self) -> None:
        client, transport = make_client(
            [httpx.ConnectError("down", request=None), ok_payload()]
        )

        call(client)

        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 0)

    def test_budget_is_bounded_to_one_retry_by_default(self) -> None:
        # Always failing: exactly ONE retry, then a single visible error.
        client, transport = make_client([httpx.TimeoutException("late", request=None)])

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 2)
        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.calls_ok, 0)
        self.assertEqual(client.retries_spent, 1)
        self.assertEqual(client.last_error_class, "Timeout")

    def test_max_retries_is_configurable(self) -> None:
        client, transport = make_client(
            [httpx.TimeoutException("late", request=None)],
            max_retries=3,
        )

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 4)  # 1 try + 3 retries
        self.assertEqual(client.retries_spent, 3)
        self.assertEqual(client.calls_err, 1)

    def test_max_retries_zero_disables_retrying(self) -> None:
        client, transport = make_client(
            [httpx.TimeoutException("late", request=None)],
            max_retries=0,
        )

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.retries_spent, 0)
        self.assertEqual(client.calls_err, 1)


class NonRetryableTests(unittest.TestCase):
    def test_rate_limit_429_is_never_retried(self) -> None:
        # Hiding a rate-limit problem behind retries would break the
        # documented "rate limits must stay visible" rule.
        client, transport = make_client([(429, {})])

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.retries_spent, 0)
        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.last_error_class, "HTTP 429")

    def test_business_error_is_never_retried(self) -> None:
        client, transport = make_client([error_response_payload()])

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.retries_spent, 0)
        self.assertEqual(client.calls_err, 1)

    def test_unexpected_payload_is_never_retried(self) -> None:
        # A JSON document that is not an object at all.
        client, transport = make_client([["totally", "unexpected"]])

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(transport.calls, 1)
        self.assertEqual(client.retries_spent, 0)
        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.last_error_class, "UnexpectedPayload")


    def test_non_json_body_is_never_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, text="<html>gateway</html>")

        client = AliExpressClient(
            CREDENTIALS,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            retry_backoff=0.0,
        )

        with self.assertRaises(AliExpressApiError):
            call(client)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(client.retries_spent, 0)
        self.assertEqual(client.calls_err, 1)
        self.assertEqual(client.last_error_class, "NonJsonResponse")

    def test_error_class_is_reset_by_a_clean_call_after_failures(self) -> None:
        # A call that fails after exhausting its budget leaves a class...
        client, _transport = make_client([httpx.TimeoutException("late", request=None)])
        with self.assertRaises(AliExpressApiError):
            call(client)
        self.assertEqual(client.last_error_class, "Timeout")

        # ...but the next clean call must clear it (admin screen rule).
        client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=ok_payload())
            )
        )
        call(client)
        self.assertIsNone(client.last_error_class)
        self.assertEqual(client.calls_ok, 1)
        self.assertEqual(client.calls_err, 1)


if __name__ == "__main__":
    unittest.main()

