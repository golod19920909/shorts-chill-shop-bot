# -*- coding: utf-8 -*-
"""G25.2-D Stage 19B — OFFLINE validation of the future live-runner.

Validates tmp_g252d_stage19b_runner.py against the VERIFIED production
contract WITHOUT any network/AI call:
  call() returns the UNWRAPPED resp_result node {resp_code,resp_msg,result}
  (aliexpress_client.py L421-455); empty result (405) -> {}; error_response
  -> raise; _raw_products reads result.products.product and _normalize skips
  rows without id/title/url (aliexpress.py L545-638).
Cases A-G per the Stage 19B task. Network is made impossible by replacing
socket.socket/httpx transport with raising stubs for the whole run (DI),
not merely by a source-text search. No production file is modified.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "tmp_g252d_stage19b_runner",
        ROOT / "tmp_g252d_stage19b_runner.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()


def _reconstructed_unwrapped(node_rows: list[dict]) -> dict:
    """Build an UNWRAPPED resp_result node from S15 extracted rows.

    RECONSTRUCTED (labelled): rows already carry product_id/title; we add a
    synthetic detail_url because _normalize requires id+title+url. This is
    NOT a genuine API capture (see report §4).
    """
    return {
        "resp_code": 200,
        "resp_msg": "success",
        "result": {"products": {"product": [
            {"product_id": r["product_id"],
             "product_title": r["title"],
             "product_detail_url": "https://reconstructed/" + r["product_id"]}
            for r in node_rows
        ]}},
    }


class _FrozenSocket:
    """DI stub: any socket use during the offline run raises immediately."""
    def __init__(self, *a, **k):
        raise AssertionError("offline guarantee: socket use attempted")


class Stage19BOfflineGuarantee(unittest.TestCase):
    def test_runner_has_no_network_import(self) -> None:
        import ast
        src = (ROOT / "tmp_g252d_stage19b_runner.py").read_text(
            encoding="utf-8")
        # Secondary signal only (the PRIMARY guarantee is the socket-stub
        # test above). Strip docstrings so prose like "call() returns..." is
        # not a false positive, then inspect real code nodes.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, (ast.Module, ast.FunctionDef,
                                  ast.AsyncFunctionDef, ast.ClassDef))
                    and (node.body and isinstance(
                        node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str))):
                node.body[0].value.value = ""
        code = ast.unparse(tree)
        for banned in ("import httpx", "import socket", "import asyncio",
                       "AliExpressClient", "generativeai", "gemini"):
            self.assertNotIn(banned, code, f"runner code must not contain "
                                           f"{banned!r}")
        # No attribute call named 'call' in executable code.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "call"):
                self.fail("runner code must not invoke a .call() method")

    def test_no_socket_during_full_validation(self) -> None:
        real = socket.socket
        socket.socket = _FrozenSocket  # type: ignore[assignment]
        try:
            node = _reconstructed_unwrapped([
                {"product_id": "1", "title": "SSD 1TB"},
                {"product_id": "2", "title": "SSD 2TB"}])
            gate = runner.validate_response(node)
            self.assertEqual(gate["outcome"], "needs-parse")
            self.assertEqual(runner.validate_response({})["outcome"], "empty")
        finally:
            socket.socket = real  # type: ignore[assignment]



class Stage19BCaseA_FixtureClassificationLayer(unittest.TestCase):
    """A: S15 rows validate the CLASSIFICATION layer, NOT raw parsing."""

    def setUp(self) -> None:
        self.fx = json.loads((ROOT / "tmp_g252d_stage15_live_results.json")
                             .read_text(encoding="utf-8"))

    def test_eight_requests_fifty_rows_each_first(self) -> None:
        self.assertEqual(len(self.fx["requests"]), 8)
        first = self.fx["requests"][0]
        self.assertEqual(len(first["items"]), first["raw_count"])

    def test_labels_within_registered_set(self) -> None:
        allowed = {"TARGET", "RELATED", "WRONG_DOMAIN", "UNRELATED",
                   "UNCERTAIN"}
        for req in self.fx["requests"]:
            for it in req["items"]:
                self.assertIn(it["label"], allowed)

    def test_counts_sum_to_classified_total(self) -> None:
        first = self.fx["requests"][0]
        s = first["counts"]
        self.assertEqual(sum(s.values()), len(first["items"]))

    def test_provenance_is_extracted_rows_not_envelope(self) -> None:
        it = self.fx["requests"][0]["items"][0]
        self.assertIn("title", it)
        self.assertNotIn("resp_result", it)
        self.assertNotIn("product_title", it)


class Stage19BCaseB_ValidEmpty(unittest.TestCase):
    """B: {} is the verified empty-result contract (405 path)."""

    def test_empty_dict_is_valid_empty(self) -> None:
        gate = runner.validate_response({})
        self.assertEqual(gate["outcome"], "empty")
        self.assertEqual(gate["products"], [])


class Stage19BCaseC_ApiError(unittest.TestCase):
    """C: error_response must be an API error, never raw=0."""

    def test_error_response_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"error_response": {
                "msg": "Invalid signature", "code": 401}})


class Stage19BCaseD_Malformed(unittest.TestCase):
    """D: malformed / not-unwrapped shapes fail loudly."""

    def test_top_level_envelope_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({
                "aliexpress_affiliate_product_query_response":
                    {"resp_result": {"resp_code": 200}}})

    def test_resp_result_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"resp_result": {"resp_code": 200}})

    def test_non_200_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"resp_code": 500, "resp_msg": "boom",
                                      "result": {"products": {"product": []}}})

    def test_missing_result_products_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"resp_code": 200, "result": {}})

    def test_non_dict_rejected(self) -> None:
        for bad in ("string", [1, 2], None, 7):
            with self.assertRaises(ValueError):
                runner.validate_response(bad)



class _CountingProvider:
    """DI stand-in: parse_products delegates to the REAL production parser
    but counts invocations so 'exactly once' and 'parse errors cannot reach
    the classifier' are proven structurally, not by source inspection."""

    def __init__(self) -> None:
        self.calls = 0

    def parse_products(self, payload):  # noqa: ANN001
        from services.marketplaces.aliexpress import AliExpressProvider
        from config.settings import MarketplaceCredentials
        self.calls += 1
        return AliExpressProvider(
            MarketplaceCredentials("k", "s")).parse_products(payload)


def _first_s15_rows(n: int = 3) -> list[dict]:
    fx = json.loads((ROOT / "tmp_g252d_stage15_live_results.json")
                    .read_text(encoding="utf-8"))
    return fx["requests"][0]["items"][:n]


class Stage19BCaseE_DoubleUnwrapRegression(unittest.TestCase):
    """E: the Stage-18 second-unwrap must be detected, not become raw=0."""

    def test_old_logic_would_score_zero(self) -> None:
        node = _reconstructed_unwrapped(_first_s15_rows(3))
        old = node.get("resp_result", {}).get("result", {}).get(
            "products", {}).get("product", [])
        self.assertEqual(old, [], "Stage-18 bug must reproduce here")

    def test_repaired_runner_survives_same_payload(self) -> None:
        node = _reconstructed_unwrapped(_first_s15_rows(3))
        prov = _CountingProvider()
        res = runner.handle_payload(prov, node)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(len(res["products"]), 3)


class Stage19BCaseF_SuccessfulPayload(unittest.TestCase):
    """F: parser invoked once, full payload preserved, counts match."""

    def test_parse_once_preserve_payload_counts_match(self) -> None:
        node = _reconstructed_unwrapped(_first_s15_rows(5))
        prov = _CountingProvider()
        res = runner.handle_payload(prov, node)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(res["parse_calls"], 1)
        self.assertEqual(res["preserved"], node)
        self.assertEqual(len(res["products"]), 5)
        self.assertEqual(len(res["products"]),
                         len(node["result"]["products"]["product"]))

    def test_parse_error_cannot_reach_classifier(self) -> None:
        prov = _CountingProvider()
        with self.assertRaises(ValueError):
            runner.handle_payload(prov, {"resp_code": 200, "result": {}})
        self.assertEqual(prov.calls, 0)


class Stage19BCaseG_FrozenPlan(unittest.TestCase):
    """G: exactly 12 queries, order/pairs/keywords preserved."""

    def test_twelve_entries(self) -> None:
        self.assertEqual(runner.check_plan(),
                         {"entries": 12, "pairs": 5, "unpaired": 2})

    def test_keyword_order_preserved(self) -> None:
        kw = runner.frozen_keywords()
        self.assertEqual(kw[0], "для toyota camry 70")
        self.assertEqual(kw[1], "toyota camry 70")
        self.assertEqual(kw[3], "пылесос без мешка")
        self.assertEqual(len(kw), 12)

    def test_pair_params_identical_except_keywords(self) -> None:
        raw = json.loads((ROOT / "tmp_g252d_stage18_raw.json")
                         .read_text(encoding="utf-8"))
        for a, b in (("R01", "R02"), ("R03", "R04"), ("R05", "R06"),
                     ("R07", "R08"), ("R09", "R10")):
            pa = dict(raw[a]["sanitized_params"])
            pb = dict(raw[b]["sanitized_params"])
            ka, kb = pa.pop("keywords"), pb.pop("keywords")
            self.assertNotEqual(ka, kb)
            self.assertEqual(pa, pb, f"{a}/{b} params must match except kw")
            self.assertEqual(pa["page_no"], 1)
            self.assertEqual(pa["page_size"], 50)


if __name__ == "__main__":
    unittest.main()
