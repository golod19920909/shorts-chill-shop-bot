# -*- coding: utf-8 -*-
"""Stage 44-B4 — offline safety tests for the capture mechanism.

Every test is OFFLINE: a ``FakeTransport`` supplies synthetic responses and a
``CapturingAIService`` is built with the ``_OFFLINE_CLIENT`` sentinel, so no
real Gemini client, no AliExpress, and no network path exists. Synthetic
fixtures are NEVER represented as genuine Gemini evidence. Run ONLY:

    python -m pytest tests/test_stage44b4_capture_safety.py -q
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from services.query_intent import reset_ai_state

from tests.fixtures_stage44b4_capture_runner import (
    BudgetExceeded,
    CallBudget,
    CaptureLock,
    CaptureRecord,
    CapturingAIService,
    FakeTransport,
    JsonlCaptureStore,
    QuotaStop,
    run_live_capture,
    _OFFLINE_CLIENT,
)
from tests.fixtures_stage44b4_replay import ReplayRejected, replay
from tests.fixtures_stage44_dataset import GOLD_DATASET


def run(coro):
    return asyncio.run(coro)


def _service(responses=None, cap=1000, provider_kind="synthetic"):
    transport = FakeTransport(responses or {}, provider_kind=provider_kind)
    return CapturingAIService(transport, CallBudget(cap)), transport


VALID_QUERY_JSON = json.dumps({
    "original_query": "x", "normalized_query": "power bank",
    "keywords": ["power", "bank"], "max_price": None})
VALID_INTENT_JSON = json.dumps({
    "product_type": "повербанк", "brand": None, "model": None,
    "generation": None, "required_attributes": [], "optional_attributes": [],
    "compatibility": [], "budget": {"min": None, "max": None, "currency": None},
    "excluded_product_types": []})


class ProviderIsolation(unittest.TestCase):
    def setUp(self):
        reset_ai_state()

    def test_no_real_client_and_available(self):
        service, _ = _service()
        # The client is the OFFLINE sentinel object, never a genai.Client.
        self.assertIs(service._client, _OFFLINE_CLIENT)
        self.assertTrue(service.is_available)
        self.assertIsNone(service._api_key)          # no credential was ever set
        # The sentinel is a bare object(), so it has no genai client surface.
        self.assertFalse(hasattr(service._client, "aio"))

    def test_all_traffic_goes_through_fake_transport(self):
        service, transport = _service({"gemini-fast": [VALID_QUERY_JSON]})
        run(service.capture_parse_query(GOLD_DATASET[0]))
        self.assertEqual(len(transport.calls), 1)   # exactly one fake call
        self.assertEqual(transport.calls[0]["model"], "gemini-fast")

    def test_live_runner_is_blocked(self):
        with self.assertRaises(QuotaStop):
            run_live_capture()


class CallBudgetAndFallback(unittest.TestCase):
    def setUp(self):
        reset_ai_state()

    def test_budget_permits_up_to_cap(self):
        budget = CallBudget(cap=2)
        budget.check(); budget.record()
        budget.check(); budget.record()
        self.assertEqual(budget.used, 2)

    def test_attempt_refused_before_exceeding_cap(self):
        budget = CallBudget(cap=1)
        budget.record()
        with self.assertRaises(BudgetExceeded):
            budget.check()
        self.assertEqual(budget.used, 1)   # never incremented past cap

    def test_fallback_attempt_counted_separately(self):
        # primary -> model-unavailable (404) -> fallback returns text: 2 attempts.
        service, transport = _service({
            "gemini-fast": [RuntimeError("404 model not found")],
            "gemini-fallback": [VALID_QUERY_JSON]}, cap=10)
        rec = run(service.capture_parse_query(GOLD_DATASET[0]))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(rec.attempt_count, 2)
        self.assertEqual(service.budget.used, 2)

    def test_quota_error_stops_and_is_not_retried(self):
        service, transport = _service({
            "gemini-fast": [RuntimeError("429 resource_exhausted quota")],
            "gemini-fallback": [VALID_QUERY_JSON]}, cap=10)
        with self.assertRaises(QuotaStop):
            run(service.capture_parse_query(GOLD_DATASET[0]))
        # Only the primary was attempted; the fallback was NOT tried on quota.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(service.budget.used, 1)

    def test_budget_blocks_a_provider_attempt(self):
        service, transport = _service({"gemini-fast": [VALID_QUERY_JSON]}, cap=0)
        with self.assertRaises(BudgetExceeded):
            run(service.capture_parse_query(GOLD_DATASET[0]))
        self.assertEqual(len(transport.calls), 0)   # refused before any call


class Provenance(unittest.TestCase):
    def setUp(self):
        reset_ai_state()

    def _record(self, **kw):
        base = dict(gold_id=1, query="компрессор", path="parse_query",
                    raw_response=VALID_QUERY_JSON,
                    parsed_output={"normalized_query": "air compressor"},
                    source="ai", provider_kind="live")
        base.update(kw)
        return CaptureRecord(**base)

    def test_synthetic_fixture_is_not_a_genuine_capture(self):
        rec = self._record(provider_kind="synthetic")
        self.assertTrue(rec.app_marked_ai)        # app-side flag is set...
        self.assertFalse(rec.replay_eligible)     # ...but NOT genuine (synthetic)

    def test_fallback_record_is_ineligible(self):
        rec = self._record(source="fallback", raw_response=None,
                           parsed_output=None)
        self.assertFalse(rec.app_marked_ai)
        self.assertFalse(rec.replay_eligible)

    def test_genuine_live_record_is_eligible(self):
        rec = self._record(provider_kind="live")
        self.assertTrue(rec.replay_eligible)
        self.assertTrue(rec.validate()[0])

    def test_response_without_parse_fails_validation(self):
        rec = self._record(parsed_output=None)
        ok, reason = rec.validate()
        self.assertFalse(ok)
        self.assertIn("not parsed", reason)


class PersistenceResumeLock(unittest.TestCase):
    def setUp(self):
        reset_ai_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _genuine(self, gid, path="parse_query"):
        return CaptureRecord(
            gold_id=gid, query="q", path=path, raw_response=VALID_QUERY_JSON,
            parsed_output={"normalized_query": "x"}, source="ai",
            provider_kind="live", attempt_count=1)

    def test_new_file_created_without_overwriting(self):
        store = JsonlCaptureStore(self.dir / "cap.jsonl")
        store.append(self._genuine(1))
        store.append(self._genuine(2))
        records, problems = store.load()
        self.assertEqual(len(records), 2)
        self.assertEqual(problems, [])

    def test_truncated_final_line_keeps_earlier_records(self):
        path = self.dir / "cap.jsonl"
        store = JsonlCaptureStore(path)
        store.append(self._genuine(1))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"gold_id": 2, "query": "q", "pa')  # truncated, no newline
        records, problems = store.load()
        self.assertEqual(len(records), 1)          # only the valid one survives
        self.assertEqual(len(problems), 1)         # truncated line reported

    def test_resume_skips_only_complete_ai_records(self):
        store = JsonlCaptureStore(self.dir / "cap.jsonl")
        store.append(self._genuine(1))
        store.append(CaptureRecord(gold_id=2, query="q", path="parse_query",
                                   source="fallback", provider_kind="live"))
        done = store.completed_keys()
        self.assertIn((1, "parse_query"), done)     # complete AI -> skip
        self.assertNotIn((2, "parse_query"), done)  # fallback -> NOT skipped

    def test_dedup_by_id_and_path(self):
        store = JsonlCaptureStore(self.dir / "cap.jsonl")
        store.append(self._genuine(1, "parse_query"))
        store.append(self._genuine(1, "parse_query_intent"))
        records, _ = store.load()
        keys = {(r.gold_id, r.path) for r in records}
        self.assertEqual(len(keys), 2)              # distinct paths, not deduped away

    def test_lock_blocks_concurrent_runner(self):
        lock_path = self.dir / "cap.lock"
        first = CaptureLock(lock_path)
        self.assertTrue(first.acquire())
        second = CaptureLock(lock_path)
        self.assertFalse(second.acquire())          # concurrent runner blocked
        first.release()
        self.assertTrue(CaptureLock(lock_path).acquire())  # free again

    def test_dataset_version_mismatch_rejected(self):
        rec = CaptureRecord(gold_id=1, query="q", path="parse_query",
                            raw_response=VALID_QUERY_JSON, parsed_output={"x": 1},
                            source="ai", provider_kind="live",
                            dataset_version="other")
        ok, reason = rec.validate()
        self.assertFalse(ok)
        self.assertIn("version", reason)


class ReplayBoundary(unittest.TestCase):
    def setUp(self):
        reset_ai_state()

    def _genuine(self, **kw):
        base = dict(gold_id=1, query="компрессор", path="parse_query",
                    raw_response=VALID_QUERY_JSON,
                    parsed_output={"normalized_query": "air compressor"},
                    source="ai", provider_kind="live")
        base.update(kw)
        return CaptureRecord(**base)

    def test_replay_preserves_query_and_no_live_client(self):
        out = replay(self._genuine())
        self.assertEqual(out["original_query"], "компрессор")  # exact gold query

    def test_replay_rejects_fallback_records(self):
        with self.assertRaises(ReplayRejected):
            replay(self._genuine(source="fallback", raw_response=None,
                                 parsed_output=None))

    def test_replay_rejects_synthetic_records(self):
        with self.assertRaises(ReplayRejected):
            replay(self._genuine(provider_kind="synthetic"))

    def test_replay_rejects_query_id_text_mismatch(self):
        # A record whose query text does not match the gold id it claims.
        rec = self._genuine(gold_id=99, query="does-not-match-gold-99")
        gold_by_id = {g["id"]: g["query"] for g in GOLD_DATASET}
        self.assertNotEqual(gold_by_id.get(rec.gold_id), rec.query)
        with self.assertRaises(ReplayRejected):
            replay(rec)

    def test_intent_path_replay_produces_applied_request(self):
        rec = self._genuine(path="parse_query_intent",
                            raw_response=VALID_INTENT_JSON)
        out = replay(rec)
        self.assertIn("intent", out)
        self.assertIn("final_request", out)
        self.assertEqual(out["final_request"]["original_query"], "компрессор")


class DatasetReuse(unittest.TestCase):
    def test_gold_dataset_loaded_from_stage44b1_fixture(self):
        # The 65-query set is imported, not copied: same object identity.
        from tests.fixtures_stage44_dataset import GOLD_DATASET as SRC
        self.assertIs(GOLD_DATASET, SRC)
        self.assertEqual(len(GOLD_DATASET), 65)
        self.assertEqual({g["id"] for g in GOLD_DATASET}, set(range(1, 66)))


if __name__ == "__main__":
    unittest.main(verbosity=2)



