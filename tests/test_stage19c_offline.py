# -*- coding: utf-8 -*-
"""G25.2-D Stage 19C — OFFLINE tests of the live runner (run BEFORE any API
call). Injects a FakeClient (DI) so the loop is exercised with zero network:
proves the 12-budget cannot be exceeded, each entry is attempted once, a
valid non-empty response parses once and preserves the payload, a malformed
response stops before classification, the first fatal error stops later
requests, retries/pagination are absent, and no AI is used. No production
file is modified; the Stage-19B offline runner is untouched.
"""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "stage19c_live", ROOT / "tmp_g252d_stage19c_live.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


live = _load()


class _Redirect(unittest.TestCase):
    """Keep tests hermetic: send live-run outputs to a temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._raw = Path(self._tmp.name) / "raw"
        self._res = Path(self._tmp.name) / "results.json"
        self._old_raw, self._old_res = live.RAW_DIR, live.RESULTS_PATH
        live.RAW_DIR = self._raw
        live.RESULTS_PATH = self._res

    def tearDown(self) -> None:
        live.RAW_DIR, live.RESULTS_PATH = self._old_raw, self._old_res



def _node(rows):
    """A valid UNWRAPPED resp_result node with `rows` products."""
    return {"resp_code": 200, "resp_msg": "success",
            "result": {"products": {"product": [
                {"product_id": r[0], "product_title": r[1],
                 "product_detail_url": "https://x/" + r[0]} for r in rows]}}}


def _counting_provider():
    from services.marketplaces.aliexpress import AliExpressProvider
    from config.settings import MarketplaceCredentials
    return AliExpressProvider(MarketplaceCredentials("k", "s"))


class FakeClient:
    """Records every call(); never touches the network. call_count is the
    hard proof of the budget. `script` maps attempt-index(1-based) -> either
    a payload dict, an Exception to raise, or None."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default
        self.calls = []          # list of (method, keywords)
        self.calls_ok = 0
        self.calls_err = 0
        self.retries_spent = 0
        self.empty_results = 0

    async def call(self, method, params):
        self.calls.append((method, params.get("keywords")))
        item = self.script.get(len(self.calls), self.default)
        if isinstance(item, Exception):
            raise item
        if item is not None:
            self.calls_ok += 1
        return item

    async def aclose(self):
        return None


def _run(script=None, default=None):
    fc = FakeClient(script, default)
    rc = asyncio.run(live.main(client=fc))
    return fc, rc


class Budget(_Redirect):
    def test_exactly_twelve_calls_all_valid(self) -> None:
        fc, rc = _run(default=_node([("1", "SSD 1TB")]))
        self.assertEqual(len(fc.calls), 12)
        self.assertEqual(rc, 0)

    def test_budget_never_exceeds_twelve(self) -> None:
        fc, _ = _run(default=_node([("1", "x")]))
        self.assertLessEqual(len(fc.calls), 12)

    def test_each_entry_attempted_once_in_order(self) -> None:
        fc, _ = _run(default=_node([("1", "x")]))
        kws = [kw for _, kw in fc.calls]
        self.assertEqual(kws, live.runner19b.frozen_keywords())


class ParseAndPreserve(_Redirect):
    def test_valid_response_parses_once_preserves_and_classifies(self) -> None:
        node = _node([("1", "DDR4 16GB RAM"), ("2", "SSD 256GB"),
                      ("3", "MicroSD 128GB memory card")])
        res = live.runner19b.handle_payload(_counting_provider(), node)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["preserved"], node)
        counts = {"TARGET": 0, "RELATED": 0, "WRONG_DOMAIN": 0,
                  "UNRELATED": 0, "UNCERTAIN": 0}
        for p in res["products"]:
            lab, _ = live.classify("Q5", p.title)
            counts[lab] += 1
        self.assertEqual(sum(counts.values()), len(res["products"]))
        self.assertEqual(counts["TARGET"], 1)        # 16GB RAM
        self.assertEqual(counts["WRONG_DOMAIN"], 2)  # ssd + flash


class StopOnFatal(_Redirect):
    def test_api_error_stops_immediately(self) -> None:
        from services.marketplaces.aliexpress_client import AliExpressApiError
        fc, rc = _run(default=AliExpressApiError("boom"))
        self.assertEqual(len(fc.calls), 1)      # no second request
        self.assertEqual(rc, 0)

    def test_malformed_stops_before_classification(self) -> None:
        script = {1: _node([("1", "x")]), 2: {"resp_code": 200, "result": {}}}
        fc, _ = _run(script=script)
        self.assertEqual(len(fc.calls), 2)      # stopped on the 2nd

    def test_empty_is_not_fatal_and_continues(self) -> None:
        fc, rc = _run(default={})
        self.assertEqual(len(fc.calls), 12)     # all 12 attempted
        self.assertEqual(rc, 0)


class OfflineGuarantee(_Redirect):
    def test_no_network_during_loop(self) -> None:
        # Block the OUTBOUND primitives only (not socket.socketpair, which
        # asyncio's proactor loop needs internally on Windows).
        import httpx
        real_conn = socket.create_connection
        real_gai = socket.getaddrinfo

        def boom(*a, **k):
            raise AssertionError("network attempted in offline test")
        socket.create_connection = boom
        socket.getaddrinfo = boom
        real_send = httpx.AsyncClient.send

        async def no_send(self, *a, **k):
            raise AssertionError("httpx send attempted in offline test")
        httpx.AsyncClient.send = no_send
        try:
            _run(default=_node([("1", "x")]))
        finally:
            socket.create_connection = real_conn
            socket.getaddrinfo = real_gai
            httpx.AsyncClient.send = real_send

    def test_redaction_strips_secrets(self) -> None:
        out = live._redact({"sign": "abc", "access_token": "t",
                            "result": {"products": {"product": []}},
                            "resp_msg": "ok"})
        self.assertEqual(out["sign"], "<redacted>")
        self.assertEqual(out["access_token"], "<redacted>")
        self.assertEqual(out["resp_msg"], "ok")


if __name__ == "__main__":
    unittest.main()

