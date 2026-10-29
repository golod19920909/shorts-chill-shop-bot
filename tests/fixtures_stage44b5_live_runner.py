# -*- coding: utf-8 -*-
"""Stage 44-B5 — LIVE Gemini capture runner (owner-authorized).

This is the runner deliberately left unimplemented in Stage 44-B4. It is
isolated test tooling (NOT production code) and reuses every Stage 44-B4
safety mechanism unchanged:

* ``CapturingAIService`` (production ``AIService`` subclass, ``_generate``
  overridden to route through an injectable transport + call budget),
* ``CallBudget`` (hard cap on ACTUAL provider attempts, incl. fallback),
* ``JsonlCaptureStore`` (append-only, truncated-line recovery, strict resume),
* ``CaptureLock`` (no concurrent runners).

The ONLY difference from B4 is the transport: ``LiveGeminiTransport`` calls
the REAL ``google.genai`` client. Every attempt is counted; quota/rate-limit
raises ``QuotaStop`` and halts the run; billing/auth/transport failures also
halt (see ``_FATAL_MARKERS``). Raw model text is preserved verbatim before
any parsing. No key/secret is ever logged.

Run (explicit, never via an ordinary offline test):
    python tests/fixtures_stage44b5_live_runner.py --cap 260
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import load_settings
from services.ai_service import _classify_gemini_error
from tests.fixtures_stage44b4_capture_runner import (
    CallBudget,
    CaptureLock,
    CaptureRecord,
    CapturingAIService,
    JsonlCaptureStore,
    QuotaStop,
)
from tests.fixtures_stage44_dataset import GOLD_DATASET

#: Error markers that must HALT the capture (never retried, never fallback).
_FATAL_MARKERS = (
    "billing", "permission_denied", "unauthenticated", "invalid api key",
    "api_key_invalid", "insufficient_quota", "credit", "401", "403",
)


class LiveGeminiTransport:
    """Real Gemini transport. Counts every attempt; never logs the key."""

    def __init__(self, api_key: str) -> None:
        from google import genai  # imported here so offline tests never touch it
        self._client = genai.Client(api_key=api_key)
        self.calls: list[dict] = []
        self.provider_kind = "live"

    async def generate(self, model: str, contents):
        self.calls.append({"model": model})
        try:
            response = await self._client.aio.models.generate_content(
                model=model, contents=contents
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if any(m in message for m in _FATAL_MARKERS):
                # billing/auth/transport: halt, do not retry, do not fallback.
                raise QuotaStop(f"fatal provider error: {_classify_gemini_error(exc)}") from exc
            raise
        return getattr(response, "text", None)


async def run_capture(cap: int, out_path: Path, lock_path: Path) -> dict:
    """Capture both paths for all 65 gold queries, resuming safely."""
    settings = load_settings()
    if not settings.is_gemini_configured:
        raise SystemExit("GEMINI_API_KEY is not configured; refusing live capture")

    store = JsonlCaptureStore(out_path)
    lock = CaptureLock(lock_path)
    budget = CallBudget(cap)
    transport = LiveGeminiTransport(settings.gemini_api_key)
    service = CapturingAIService(
        transport, budget,
        fast_model=settings.gemini_fast_model,
        fallback_model=settings.gemini_fallback_model,
    )

    done = store.completed_keys()
    summary = {"captured": 0, "skipped_resume": 0, "fallback": 0,
               "ai": 0, "stopped_reason": None, "attempts": 0}

    with lock:  # raises if another runner holds it
        for gold in GOLD_DATASET:
            for path, method in (
                ("parse_query", service.capture_parse_query),
                ("parse_query_intent", service.capture_parse_query_intent),
            ):
                key = (gold["id"], path)
                if key in done:
                    summary["skipped_resume"] += 1
                    continue
                if budget.remaining <= 0:
                    summary["stopped_reason"] = "budget_cap_reached"
                    break
                try:
                    record: CaptureRecord = await method(gold)
                except QuotaStop as exc:
                    summary["stopped_reason"] = f"quota/fatal: {exc}"
                    break
                except Exception as exc:  # noqa: BLE001 - one bad query never ends the run
                    summary.setdefault("errors", []).append(
                        f"#{gold['id']} {path}: {type(exc).__name__}")
                    continue
                store.append(record)         # persist each completed record
                summary["captured"] += 1
                if record.app_marked_ai:
                    summary["ai"] += 1
                else:
                    summary["fallback"] += 1
            if summary["stopped_reason"]:
                break

    summary["attempts"] = budget.used
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 44-B5 live Gemini capture")
    parser.add_argument("--cap", type=int, default=260)
    parser.add_argument("--out", default="tmp_stage44b5_capture.jsonl")
    parser.add_argument("--lock", default="tmp_stage44b5_capture.lock")
    args = parser.parse_args()
    summary = asyncio.run(run_capture(args.cap, Path(args.out), Path(args.lock)))
    print("CAPTURE SUMMARY:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
