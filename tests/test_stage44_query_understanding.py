# -*- coding: utf-8 -*-
"""Stage 44-B1 — OFFLINE deterministic query-understanding baseline harness.

READ-ONLY evaluation. It exercises ONLY the existing offline interfaces:

  * ``AIService(api_key=None).parse_query(text)``  -> deterministic SearchRequest
  * ``query_intent.fallback_intent(text)``         -> deterministic StructuredQueryIntent
  * ``query_intent.apply_intent(request, intent)`` -> final SearchRequest
  * ``query_intent.parse_query_intent(text, FakeAI)`` for PLUMBING only (mocked)

NO production file is modified. NO network / Gemini / AliExpress call is made:
the parser path is constructed with ``api_key=None`` (local fallback parser) and
the plumbing path uses a local ``FakeAI`` whose ``_generate`` returns canned
strings. Run:

    python -m pytest tests/test_stage44_query_understanding.py -q -s

The harness prints a human summary and writes a machine-readable JSON report to
``tmp_stage44_report.json`` (gitignored scratch, no secrets / no user data).
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

from services.ai_service import AIService
from services.query_intent import (
    StructuredQueryIntent,
    apply_intent,
    fallback_intent,
    normalize_intent,
    parse_query_intent,
    reset_ai_state,
)

from tests.fixtures_stage44_dataset import GOLD_CORRECTIONS, GOLD_DATASET

REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "tmp_stage44_report.json")


def _norm(value):
    """Case/ё-insensitive normalization for tolerant field comparison."""
    if value is None:
        return ""
    return " ".join(str(value).lower().replace("ё", "е").split())


def _run(coro):
    """Run one coroutine to completion (offline; parse_query is async)."""
    return asyncio.run(coro)


def _contains(needle, haystack):
    """True when ``needle`` (normalized) is a token-substring of ``haystack``."""
    n = _norm(needle)
    if not n:
        return True
    return n in _norm(haystack)


def _list_f1(gold, actual):
    """Token/string-level precision, recall, F1 between two string lists."""
    gset = {_norm(x) for x in gold if _norm(x)}
    aset = {_norm(x) for x in actual if _norm(x)}
    if not gset and not aset:
        return 1.0, 1.0, 1.0
    tp = len(gset & aset)
    precision = tp / len(aset) if aset else (1.0 if not gset else 0.0)
    recall = tp / len(gset) if gset else 1.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return precision, recall, f1


class FakeAI:
    """Offline stand-in for ``AIService`` exposing only ``_generate``.

    Returns a canned string (or raises) and records call count, so plumbing
    tests prove a real model is NEVER invoked.
    """

    def __init__(self, result=None, *, exc=None):
        self._client = object()  # non-None => parse_query_intent will try AI
        self.fast_model = "fake-model"
        self.last_model = None
        self.calls = 0
        self._result = result
        self._exc = exc

    async def _generate(self, contents, *, models=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


def _intent_to_lists(intent):
    """Flatten a StructuredQueryIntent into the comparable gold fields."""
    return {
        "product_type": intent.product_type,
        "required_attributes": list(intent.required_attributes),
        "optional_attributes": list(intent.optional_attributes),
        "excluded_product_types": list(intent.excluded_product_types),
        "compatibility": list(intent.compatibility),
        "brand": intent.brand,
        "budget": {"max": intent.budget.max, "min": intent.budget.min,
                   "currency": intent.budget.currency},
    }


def evaluate_query(gold):
    """Run the deterministic path for one gold query and score it field-by-field."""
    text = gold["query"]
    parsed = _run(AIService(api_key=None).parse_query(text))  # deterministic SearchRequest
    intent = fallback_intent(text)                       # deterministic intent
    intent_lists = _intent_to_lists(intent)
    final = apply_intent(parsed, intent)                 # final SearchRequest

    record = {
        "id": gold["id"], "category": gold["category"], "kind": gold["kind"],
        "query": text,
        "expected": {
            "product_type": gold["product_type"],
            "required_attributes": gold["required_attributes"],
            "optional_attributes": gold["optional_attributes"],
            "excluded_product_types": gold["excluded_product_types"],
            "compatibility": gold["compatibility"],
            "brand": gold["brand"], "budget": gold["budget"],
        },
        "actual_parser": {
            "normalized_query": parsed.normalized_query,
            "max_price": parsed.max_price, "min_price": parsed.min_price,
            "currency": parsed.currency,
        },
        "actual_fallback_intent": intent_lists,
        "final_request": {
            "normalized_query": final.normalized_query,
            "max_price": final.max_price, "currency": final.currency,
            "intent_product_type": final.intent_product_type,
            "intent_required_attributes": list(final.intent_required_attributes),
            "intent_excluded_product_types": list(final.intent_excluded_product_types),
            "intent_compatibility": list(final.intent_compatibility),
        },
        "fields": {}, "missing_expected": [], "unexpected_extracted": [],
        "unmeasurable": ["intended_use", "provenance", "optional_attributes"],
        "original_query_preserved": final.original_query == text,
        "explanation": "",
    }

    # -- product intent (only where gold marks it applicable) ---------------
    if gold["product_intent_applicable"]:
        exp_pt, act_pt = gold["product_type"], (final.intent_product_type or "")
        ok = _contains(exp_pt, act_pt) or _contains(act_pt, exp_pt)
        record["fields"]["product_type"] = {
            "expected": exp_pt, "actual": act_pt, "ok": ok}
        if not ok:
            record["missing_expected"].append(f"product_type={exp_pt!r}")
    else:
        leaked = final.intent_product_type
        record["fields"]["product_type_must_be_empty"] = {
            "expected": None, "actual": leaked, "ok": leaked is None}
        if leaked is not None:
            record["unexpected_extracted"].append(f"product_type={leaked!r}")

    # -- mandatory attributes (P/R/F1) --------------------------------------
    gold_req, act_req = gold["required_attributes"], final.intent_required_attributes
    for a in gold_req:
        if not any(_contains(a, x) or _contains(x, a) for x in act_req):
            record["missing_expected"].append(f"required={a!r}")
    p, r, f1 = _list_f1(gold_req, act_req)
    record["fields"]["required_attributes"] = {
        "expected": gold_req, "actual": act_req,
        "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3)}

    # -- exclusions (recall + scope) ----------------------------------------
    gold_excl, act_excl = gold["excluded_product_types"], final.intent_excluded_product_types
    excl_hit = [e for e in gold_excl
                if any(_contains(e, x) or _contains(x, e) for x in act_excl)]
    for e in gold_excl:
        if e not in excl_hit:
            record["missing_expected"].append(f"excluded={e!r}")
    for pos in gold.get("positive_from_negation", []):
        if any(_contains(pos, x) for x in act_excl):
            record["unexpected_extracted"].append(
                f"excluded_positive={pos!r} (scope error)")
    record["fields"]["excluded_product_types"] = {
        "expected": gold_excl, "actual": act_excl,
        "recall": round(len(excl_hit) / len(gold_excl), 3) if gold_excl else None,
        "scope_ok": (not any(
            _contains(pos, x) for pos in gold.get("positive_from_negation", [])
            for x in act_excl)) if act_excl else None}

    # -- compatibility -------------------------------------------------------
    gold_compat, act_compat = gold["compatibility"], final.intent_compatibility
    for c in gold_compat:
        if not any(_contains(c, x) or _contains(x, c) for x in act_compat):
            record["missing_expected"].append(f"compatibility={c!r}")
    cp, cr, cf1 = _list_f1(gold_compat, act_compat)
    record["fields"]["compatibility"] = {
        "expected": gold_compat, "actual": act_compat,
        "precision": round(cp, 3), "recall": round(cr, 3), "f1": round(cf1, 3)}

    # -- numeric/unit preservation ------------------------------------------
    gold_num = gold.get("numeric_specs", [])
    num_hit = [s for s in gold_num if any(_contains(s, x) for x in act_req)]
    record["fields"]["numeric_specs"] = {
        "expected": gold_num, "actual": act_req,
        "preserved": [s for s in gold_num if s in num_hit],
        "recall": round(len(num_hit) / len(gold_num), 3) if gold_num else None}
    for s in gold_num:
        if s not in num_hit:
            record["missing_expected"].append(f"numeric={s!r}")

    # -- budget (only where gold provides one) ------------------------------
    gb = gold["budget"]
    if gb["max"] is not None or gb["currency"] is not None:
        ok_budget = (final.max_price == gb["max"]
                     and (gb["currency"] is None
                          or _norm(final.currency) == _norm(gb["currency"])))
        record["fields"]["budget"] = {
            "expected": gb,
            "actual": {"max": final.max_price, "currency": final.currency},
            "ok": ok_budget}
        if not ok_budget:
            record["missing_expected"].append(
                f"budget=max:{gb['max']},cur:{gb['currency']}")

    if record["missing_expected"]:
        record["explanation"] = ("deterministic parser lacks: "
                                 + "; ".join(record["missing_expected"]))
    return record

def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def aggregate_metrics(records):
    """Fold per-query records into macro metrics (MEASURED, not thresholds)."""
    pt_cases = [r for r in records if "product_type" in r["fields"]]
    pt_acc = _mean([1.0 if r["fields"]["product_type"]["ok"] else 0.0
                    for r in pt_cases])
    must_empty = [r for r in records if "product_type_must_be_empty" in r["fields"]]
    no_invent = _mean([1.0 if r["fields"]["product_type_must_be_empty"]["ok"] else 0.0
                       for r in must_empty])
    req_p = _mean([r["fields"]["required_attributes"]["precision"] for r in records])
    req_r = _mean([r["fields"]["required_attributes"]["recall"] for r in records])
    req_f1 = _mean([r["fields"]["required_attributes"]["f1"] for r in records])
    excl_cases = [r for r in records
                  if r["fields"]["excluded_product_types"]["recall"] is not None]
    excl_recall = _mean([r["fields"]["excluded_product_types"]["recall"]
                         for r in excl_cases])
    scope_cases = [r for r in records
                   if r["fields"]["excluded_product_types"]["scope_ok"] is not None]
    scope_ok = _mean([1.0 if r["fields"]["excluded_product_types"]["scope_ok"] else 0.0
                      for r in scope_cases]) if scope_cases else None
    compat_f1 = _mean([r["fields"]["compatibility"]["f1"] for r in records])
    num_cases = [r for r in records
                 if r["fields"]["numeric_specs"]["recall"] is not None]
    num_recall = _mean([r["fields"]["numeric_specs"]["recall"] for r in num_cases])
    budget_cases = [r for r in records if "budget" in r["fields"]]
    budget_acc = _mean([1.0 if r["fields"]["budget"]["ok"] else 0.0
                        for r in budget_cases]) if budget_cases else None
    orig_pres = _mean([1.0 if r["original_query_preserved"] else 0.0
                       for r in records])
    lost = 0
    for r in records:
        fi, fr = r["actual_fallback_intent"], r["final_request"]
        for field, final_field in (("required_attributes", "intent_required_attributes"),
                                   ("excluded_product_types", "intent_excluded_product_types"),
                                   ("compatibility", "intent_compatibility")):
            for item in fi[field]:
                if _norm(item) not in {_norm(x) for x in fr[final_field]}:
                    lost += 1
    return {
        "queries": len(records),
        "product_intent_accuracy": pt_acc,
        "product_intent_applicable_cases": len(pt_cases),
        "no_invention_rate": no_invent,
        "must_be_empty_cases": len(must_empty),
        "required_precision": req_p, "required_recall": req_r, "required_f1": req_f1,
        "exclusion_recall": excl_recall, "exclusion_scope_accuracy": scope_ok,
        "compatibility_f1": compat_f1, "numeric_unit_recall": num_recall,
        "budget_accuracy": budget_acc, "original_query_preservation": orig_pres,
        "information_lost_items": lost,
        "unmeasurable_fields": ["intended_use", "provenance",
                                "optional_attributes(AI-only)"],
    }


def _print_summary(metrics, records):
    print("\n" + "=" * 72)
    print("STAGE 44-B1 — OFFLINE deterministic query-understanding baseline")
    print("=" * 72)
    print(f"queries evaluated : {metrics['queries']}")
    print(f"product-intent accuracy (applicable={metrics['product_intent_applicable_cases']}) : "
          f"{metrics['product_intent_accuracy']}")
    print(f"no-invention rate (must-be-empty={metrics['must_be_empty_cases']}) : "
          f"{metrics['no_invention_rate']}")
    print(f"required  P/R/F1  : {metrics['required_precision']} / "
          f"{metrics['required_recall']} / {metrics['required_f1']}")
    print(f"exclusion recall  : {metrics['exclusion_recall']}   "
          f"scope accuracy: {metrics['exclusion_scope_accuracy']}")
    print(f"compatibility F1  : {metrics['compatibility_f1']}")
    print(f"numeric/unit recall: {metrics['numeric_unit_recall']}")
    print(f"budget accuracy   : {metrics['budget_accuracy']}")
    print(f"original-query preservation: {metrics['original_query_preservation']}")
    print(f"information lost (intent->final): {metrics['information_lost_items']} items")
    print(f"UNMEASURABLE fields: {metrics['unmeasurable_fields']}")
    print("-" * 72)
    worst = sorted(records, key=lambda r: len(r["missing_expected"]), reverse=True)[:8]
    print("Biggest gaps (top 8 by missing expected constraints):")
    for r in worst:
        if r["missing_expected"]:
            print(f"  #{r['id']:>2} [{r['category']}] {r['query'][:48]!r}")
            print(f"       missing: {', '.join(r['missing_expected'])}")
            if r["unexpected_extracted"]:
                print(f"       unexpected: {', '.join(r['unexpected_extracted'])}")
    print("=" * 72 + "\n")


class OfflineDeterministicBaseline(unittest.TestCase):
    """Runs the deterministic path over all 65 gold queries; measures, no asserts."""

    def test_baseline_writes_report(self):
        records = [evaluate_query(gold) for gold in GOLD_DATASET]
        metrics = aggregate_metrics(records)
        report = {
            "stage": "44-B1",
            "mode": "offline-deterministic",
            "network": "none",
            "gold_corrections": GOLD_CORRECTIONS,
            "metrics": metrics,
            "records": records,
        }
        with open(REPORT_PATH, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        _print_summary(metrics, records)

        # Structural sanity (NOT performance thresholds):
        self.assertEqual(len(records), 65)
        self.assertTrue(all(r["original_query_preserved"] for r in records),
                        "original_query must never be rewritten")
        # The deterministic parser must never be invoked with a real client:
        self.assertFalse(AIService(api_key=None).is_available)


class NoNetworkGuard(unittest.TestCase):
    """Prove the harness never reaches a real AI/marketplace client."""

    def test_parser_is_constructed_without_a_key(self):
        service = AIService(api_key=None)
        self.assertIsNone(service._client)
        self.assertFalse(service.is_available)

    def test_fallback_intent_needs_no_ai(self):
        intent = fallback_intent("повербанк 20000 мАч")
        self.assertIsInstance(intent, StructuredQueryIntent)


class StructuredOutputPlumbing(unittest.TestCase):
    """Layer 2: synthetic model responses through the REAL contracts (no live model).

    Verifies parsing, schema validation, normalization, apply_intent survival,
    and fallback-on-error — the plumbing the deterministic baseline cannot show.
    """

    def setUp(self):
        reset_ai_state()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_ai_response_is_parsed_and_validated(self):
        ai = FakeAI(result='{"product_type":"зарядка","brand":"Apple",'
                           '"required_attributes":["USB-C"],'
                           '"compatibility":["iPhone 15"],"budget":{"max":500}}')
        intent, meta = self._run(parse_query_intent("зарядка для iPhone 15", ai))
        self.assertEqual(meta.source, "ai")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.required_attributes, ["USB-C"])
        self.assertEqual(ai.calls, 1)

    def test_unknown_keys_are_dropped_not_injected(self):
        ai = FakeAI(result='{"product_type":"мышь","invented":"x",'
                           '"products":[{"title":"fake"}]}')
        intent, _ = self._run(parse_query_intent("мышь", ai))
        self.assertEqual(intent.product_type, "мышь")
        self.assertNotIn("invented", intent.model_dump())
        self.assertNotIn("products", intent.model_dump())

    def test_malformed_json_falls_back(self):
        ai = FakeAI(result="this is not json at all")
        intent, meta = self._run(parse_query_intent("наушники", ai))
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "invalid_json")

    def test_none_response_opens_circuit_and_falls_back(self):
        ai = FakeAI(result=None)
        intent, meta = self._run(parse_query_intent("наушники", ai))
        self.assertEqual(meta.source, "fallback")
        self.assertIsNotNone(meta.error)

    def test_exception_is_swallowed_and_falls_back(self):
        ai = FakeAI(exc=RuntimeError("boom"))
        intent, meta = self._run(parse_query_intent("наушники", ai))
        self.assertEqual(meta.source, "fallback")
        self.assertTrue(meta.error.startswith("ai_exception"))

    def test_apply_intent_preserves_validated_constraints(self):
        # A validated intent must survive into the final SearchRequest unchanged.
        intent = normalize_intent({
            "product_type": "ssd", "required_attributes": ["1 ТБ", "NVMe"],
            "compatibility": ["ноутбука"], "excluded_product_types": ["адаптер"],
            "budget": {"max": 2000, "currency": "UAH"}})
        base = self._run(AIService(api_key=None).parse_query("ssd 1 ТБ для ноутбука"))
        final = apply_intent(base, intent)
        self.assertEqual(final.intent_product_type, "ssd")
        self.assertIn("1 ТБ", final.intent_required_attributes)
        self.assertIn("NVMe", final.intent_required_attributes)
        self.assertIn("ноутбука", final.intent_compatibility)
        self.assertIn("адаптер", final.intent_excluded_product_types)
        self.assertEqual(final.max_price, 2000)
        self.assertEqual(final.currency, "UAH")
        # original_query is never overwritten
        self.assertEqual(final.original_query, base.original_query)


if __name__ == "__main__":
    unittest.main(verbosity=2)



