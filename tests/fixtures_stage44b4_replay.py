# -*- coding: utf-8 -*-
"""Stage 44-B4 — OFFLINE replay adapter for validated capture records.

Feeds a captured ``raw_response`` back through the REAL parsing/normalization/
application functions — WITHOUT any network or live client — so a future
AI-path baseline can be scored offline from genuine captures.

It NEVER:
* initializes a Gemini client,
* contacts AliExpress,
* or treats a fallback / synthetic / mismatched record as AI evidence.

It exercises, per path, the exact production helpers:
* ``parse_query``        -> ``AIService._extract_json_object`` + ``_to_search_request``
* ``parse_query_intent`` -> ``query_intent._extract_json`` + ``normalize_intent``
                             + ``apply_intent``
"""

from __future__ import annotations

from typing import Any

from services.ai_service import AIService
from services.query_intent import _extract_json, apply_intent, normalize_intent

from tests.fixtures_stage44b4_capture_runner import CaptureRecord, CaptureError
from tests.fixtures_stage44_dataset import GOLD_DATASET

#: gold_id -> exact query text, so a record's claimed id and text must agree.
_GOLD_QUERY_BY_ID = {entry["id"]: entry["query"] for entry in GOLD_DATASET}


class ReplayRejected(Exception):
    """Raised when a record is not eligible for genuine AI-path replay."""


def _require_eligible(record: CaptureRecord) -> None:
    if not record.replay_eligible:
        raise ReplayRejected(
            f"record not eligible for AI replay "
            f"(received={record.response_received}, parsed={record.parsed_ok}, "
            f"ai={record.app_marked_ai}, provider={record.provider_kind})"
        )
    if not record.validate()[0]:
        raise ReplayRejected(f"record failed validation: {record.validate()[1]}")
    # The claimed gold id must exist AND its stored query text must match the
    # gold dataset exactly — a mismatched id/text is never replayed.
    expected_query = _GOLD_QUERY_BY_ID.get(record.gold_id)
    if expected_query is None:
        raise ReplayRejected(f"unknown gold_id {record.gold_id}")
    if record.query != expected_query:
        raise ReplayRejected(
            f"query/id mismatch: record text does not match gold #{record.gold_id}"
        )


def replay_parse_query(record: CaptureRecord) -> dict[str, Any]:
    """Re-derive a SearchRequest from the captured raw response, offline."""
    _require_eligible(record)
    payload = AIService._extract_json_object(record.raw_response or "")
    if payload is None:
        raise ReplayRejected("captured raw response is not valid JSON")
    request = AIService._to_search_request(payload, fallback_text=record.query)
    return request.model_dump()


def replay_parse_query_intent(record: CaptureRecord) -> dict[str, Any]:
    """Re-derive StructuredQueryIntent + applied SearchRequest, offline."""
    _require_eligible(record)
    raw = _extract_json(record.raw_response or "")
    if raw is None:
        raise ReplayRejected("captured raw response is not valid JSON")
    intent = normalize_intent(raw)
    base = AIService._fallback_parse(record.query)  # original query preserved
    final = apply_intent(base, intent)
    return {"intent": intent.model_dump(), "final_request": final.model_dump()}


def replay(record: CaptureRecord) -> dict[str, Any]:
    """Dispatch by path. Query text must equal the record's own query (never
    re-sent); a mismatched gold id/text is rejected before any parsing."""
    if record.path == "parse_query":
        return replay_parse_query(record)
    if record.path == "parse_query_intent":
        return replay_parse_query_intent(record)
    raise CaptureError(f"unknown path: {record.path!r}")
