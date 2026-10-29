# -*- coding: utf-8 -*-
"""G25.2-D Stage 19B (addendum) -- strict resp_code + deepcopy checks.

New file; does NOT modify tests/test_stage19b_offline_runner.py.
Covers the §3.2 gap found during validation: resp_code MUST exist and
equal 200; missing/non-200 codes must fail loudly. Also covers §3.9:
preserved payload must be a full deep copy, not a shallow dict().
Strictly OFFLINE: no network/AI/production changes.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "tmp_g252d_stage19b_runner_strict",
        ROOT / "tmp_g252d_stage19b_runner.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()


def _valid_node() -> dict:
    return {"resp_code": 200, "resp_msg": "ok",
            "result": {"products": {"product": [
                {"product_id": "7", "product_title": "t",
                 "product_detail_url": "https://x/7"}]}}}


class StrictRespCode(unittest.TestCase):
    def test_missing_resp_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"result": {"products": {}}})

    def test_none_resp_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"resp_code": None,
                                      "result": {"products": {}}})

    def test_non_200_resp_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_response({"resp_code": 405,
                                      "result": {"products": {}}})

    def test_string_200_accepted(self) -> None:
        gate = runner.validate_response(dict(_valid_node(),
                                             resp_code="200"))
        self.assertEqual(gate["outcome"], "needs-parse")

    def test_int_200_accepted(self) -> None:
        gate = runner.validate_response(_valid_node())
        self.assertEqual(gate["outcome"], "needs-parse")

    def test_missing_code_never_reaches_parser(self) -> None:
        class P:
            calls = 0

            def parse_products(self, payload):
                type(self).calls += 1
                return []
        with self.assertRaises(ValueError):
            runner.handle_payload(P(), {"result": {"products": {}}})
        self.assertEqual(P.calls, 0)


class DeepPreserve(unittest.TestCase):
    def test_preserved_is_deep_copy(self) -> None:
        class P:
            def parse_products(self, payload):
                from services.marketplaces.aliexpress import (
                    AliExpressProvider)
                from config.settings import MarketplaceCredentials
                return AliExpressProvider(
                    MarketplaceCredentials("k", "s")).parse_products(payload)
        node = _valid_node()
        res = runner.handle_payload(P(), node)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["preserved"], node)
        node["result"]["products"]["product"][0]["product_id"] = "MUT"
        self.assertNotEqual(res["preserved"]["result"]["products"]
                            ["product"][0]["product_id"], "MUT")


if __name__ == "__main__":
    unittest.main()
