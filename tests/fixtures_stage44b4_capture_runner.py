# -*- coding: utf-8 -*-
"""Stage 44-B4 — OFFLINE capture mechanism (no network, no live runner here).

This module prepares a SAFE, RESUMABLE Gemini-capture mechanism for a FUTURE,
separately-authorized live run over the 65 Stage 44-B1 gold queries through
BOTH ``AIService.parse_query`` and ``parse_query_intent``.

Design (all offline-safe):
* ``CapturingAIService`` SUBCLASSES the production ``AIService`` and overrides
  ONLY ``_generate`` — the single provider chokepoint (every AI call in the
  project funnels through it). It NEVER initializes a real Gemini client:
  ``_client`` is set to the ``_OFFLINE_CLIENT`` sentinel, so ``is_available``
  is True but no ``google.genai`` import or network call can occur. Production
  files are untouched; ``parse_query``/``parse_query_intent``/``normalize_intent``
  /``_to_search_request``/``apply_intent`` are reused unchanged.
* The overridden ``_generate`` MIRRORS the production model chain
  (primary -> fallback) and error routing (quota/model/other) but delegates the
  actual request to an injectable ``transport`` and accounts every attempt.
* A ``CallBudget`` counts each REAL provider attempt (fallback attempts
  included), is checked BEFORE each attempt, and refuses to exceed a hard cap.
* ``CaptureRecord`` keeps four provenance concepts SEPARATE: response received,
  parsed OK, app-marked-AI, and complete+eligible. A record is a GENUINE AI
  capture only when ``replay_eligible AND provider_kind == "live"`` — a synthetic
  test fixture can be replay-eligible but is NEVER a genuine capture.

The LIVE runner is intentionally NOT implemented here; ``run_live_capture``
raises and requires separate owner approval.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.ai_service import AIService, _classify_gemini_error
from services.query_intent import apply_intent, parse_query_intent

#: Non-None sentinel so ``is_available`` is True without a real client.
_OFFLINE_CLIENT = object()


class BudgetExceeded(RuntimeError):
    """Raised when a provider attempt would exceed the configured hard cap."""


class QuotaStop(RuntimeError):
    """Raised to stop the whole capture on a quota/rate-limit error (no retry)."""


class CaptureError(RuntimeError):
    """Raised for unusable capture inputs (bad record, id/text mismatch)."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallBudget:
    """Centralised provider-attempt accounting.

    Counts each ACTUAL provider request attempt (fallback-model attempts and
    any retries included) — never a logical parse operation. ``check`` refuses
    to start an attempt that would exceed ``cap``; ``record`` counts one.
    """

    def __init__(self, cap: int) -> None:
        if cap < 0:
            raise ValueError("cap must be >= 0")
        self.cap = int(cap)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def check(self) -> None:
        """Refuse to start another attempt once the cap is reached."""
        if self.used >= self.cap:
            raise BudgetExceeded(
                f"provider-call budget exhausted (cap={self.cap}, used={self.used})"
            )

    def record(self, n: int = 1) -> None:
        self.used += int(n)


class FakeTransport:
    """Offline transport: returns programmed text or raises, records every call.

    ``responses`` maps a model name to a list of outcomes (str to return, or an
    Exception instance to raise). ``provider_kind`` marks the origin so a
    synthetic fixture can never be mistaken for genuine Gemini output.
    """

    def __init__(self, responses: dict[str, list] | None = None,
                 *, provider_kind: str = "synthetic") -> None:
        self._responses = responses or {}
        self.calls: list[dict[str, Any]] = []
        self.provider_kind = provider_kind

    async def generate(self, model: str, contents: Any):
        self.calls.append({"model": model, "contents_len": len(str(contents))})
        queue = self._responses.get(model)
        if not queue:
            return None
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class CaptureRecord:
    """One capture unit for a single (gold_id, path). Four provenance flags kept
    deliberately separate so a record is never inflated into a genuine capture:

    * ``response_received`` — the provider returned non-empty text.
    * ``parsed_ok``         — that text parsed into the expected structure.
    * ``app_marked_ai``     — production code itself set ``source == "ai"``.
    * ``complete_and_eligible`` — all of the above AND provider is a real
      (``live``) provider AND the record passes validation.
    """

    gold_id: int
    query: str
    path: str                       # "parse_query" | "parse_query_intent"
    raw_response: str | None = None
    parsed_output: Any | None = None
    source: str = "unknown"         # ai | fallback | unavailable | error | ...
    error_category: str | None = None
    attempt_count: int = 0
    model_identifier: str | None = None
    captured_at: str = field(default_factory=_utcnow)
    provider_kind: str = "synthetic"  # "live" | "synthetic"
    dataset_version: str = "stage44b1-gold65"

    # -- derived provenance -------------------------------------------------
    @property
    def response_received(self) -> bool:
        return bool(self.raw_response and self.raw_response.strip())

    @property
    def parsed_ok(self) -> bool:
        return self.parsed_output is not None

    @property
    def app_marked_ai(self) -> bool:
        return self.source == "ai"

    @property
    def replay_eligible(self) -> bool:
        """Complete + genuine: response, parsed, app-marked-AI, LIVE provider."""
        return (self.response_received and self.parsed_ok
                and self.app_marked_ai and self.provider_kind == "live")

    def validate(self) -> tuple[bool, str]:
        """Strict completeness/provenance check used when resuming.

        Returns (ok, reason). A record passes only when it is internally
        consistent; ``replay_eligible`` additionally demands a live provider.
        """
        if not isinstance(self.gold_id, int):
            return False, "gold_id not int"
        if not self.query:
            return False, "empty query"
        if self.path not in ("parse_query", "parse_query_intent"):
            return False, "bad path"
        if self.dataset_version != "stage44b1-gold65":
            return False, "dataset version mismatch"
        if self.response_received and not self.parsed_ok:
            return False, "response received but not parsed"
        if self.app_marked_ai and not self.response_received:
            return False, "marked ai without a response"
        return True, "ok"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["provenance"] = {
            "response_received": self.response_received,
            "parsed_ok": self.parsed_ok,
            "app_marked_ai": self.app_marked_ai,
            "replay_eligible": self.replay_eligible,
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "CaptureRecord":
        data = json.loads(line)
        data.pop("provenance", None)  # derived, not stored input
        return cls(**data)


class CapturingAIService(AIService):
    """Production ``AIService`` with ``_generate`` overridden for capture.

    NO real client is ever built (``_client`` is the ``_OFFLINE_CLIENT``
    sentinel). Every provider attempt goes through ``transport`` and is counted
    by ``budget``. The model chain and error routing MIRROR production
    ``_generate``: quota stops immediately (no retry, no fallback), a
    model-unavailable error advances to the fallback model (a separate,
    countable attempt), anything else returns None.
    """

    def __init__(self, transport: FakeTransport, budget: CallBudget, *,
                 fast_model: str = "gemini-fast", fallback_model: str = "gemini-fallback") -> None:
        # Bypass AIService.__init__ (which would build a real client) and set
        # only the attributes the reused code paths read.
        self.model = fast_model
        self.fast_model = fast_model
        self.fallback_model = fallback_model
        self.timeout_seconds = 0.0
        self._api_key = None
        self._client = _OFFLINE_CLIENT      # non-None => is_available True
        self.transport = transport
        self.budget = budget
        self.last_model: str | None = None
        self.parse_ok = 0
        self.parse_fallback = 0
        self.generate_ok = 0
        self.generate_failed = 0
        self.attempts = 0               # every provider attempt, incl. fallback
        self.last_error: str | None = None

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def _generate(self, contents, *, models=None):
        chain = list(models or (self.fast_model,))
        if self.fallback_model and self.fallback_model not in chain:
            chain.append(self.fallback_model)
        for index, model in enumerate(chain):
            self.budget.check()               # refuse BEFORE the attempt
            self.budget.record()              # count THIS attempt (incl. fallback)
            self.attempts += 1                # dedicated per-attempt counter
            try:
                text = await self.transport.generate(model, contents)
            except Exception as exc:  # noqa: BLE001
                kind = _classify_gemini_error(exc)
                self.last_error = kind
                if kind == "quota":
                    self.generate_failed += 1
                    raise QuotaStop("quota/rate-limit reached; capture stopped") from exc
                if kind == "model" and index + 1 < len(chain):
                    continue                 # fallback attempt (already counted)
                self.generate_failed += 1
                return None
            if not text:
                self.generate_failed += 1
                return None
            self.generate_ok += 1
            self.last_model = model
            return str(text).strip()
        self.generate_failed += 1
        return None

    # -- capture entry points (reuse production parsing unchanged) ----------
    async def capture_parse_query(self, gold) -> CaptureRecord:
        """Run the real ``parse_query`` and record raw+parsed+source."""
        from services.ai_service import AIService as _Prod  # local alias
        real_generate = self._generate                      # bound BEFORE swap
        raw_holder: dict[str, str | None] = {"raw": None}

        async def _spy(contents, *, models=None):
            text = await real_generate(contents, models=models)
            raw_holder["raw"] = text
            return text

        # Route parse_query's own _generate through the spy so we capture the
        # exact raw text it consumed, without touching production code.
        self._generate = _spy  # type: ignore[assignment]
        try:
            request = await _Prod.parse_query(self, gold["query"])
        finally:
            self._generate = real_generate  # type: ignore[assignment]
        raw = raw_holder["raw"]
        source = "ai" if (raw and self.generate_ok and self.parse_ok) else "fallback"
        return CaptureRecord(
            gold_id=gold["id"], query=gold["query"], path="parse_query",
            raw_response=raw,
            parsed_output=request.model_dump(),
            source=source, error_category=self.last_error,
            attempt_count=self.attempts,
            model_identifier=self.last_model if source == "ai" else None,
            provider_kind=self.transport.provider_kind,
        )

    async def capture_parse_query_intent(self, gold) -> CaptureRecord:
        """Run the real ``parse_query_intent`` and record raw+parsed+meta."""
        real_generate = self._generate                      # bound BEFORE swap
        raw_holder: dict[str, str | None] = {"raw": None}

        async def _spy(contents, *, models=None):
            text = await real_generate(contents, models=models)
            raw_holder["raw"] = text
            return text

        self._generate = _spy  # type: ignore[assignment]
        try:
            from services.query_intent import reset_ai_state
            reset_ai_state()  # avoid a stale circuit breaker from another test
            intent, meta = await parse_query_intent(gold["query"], self)
        finally:
            self._generate = real_generate  # type: ignore[assignment]
        raw = raw_holder["raw"]
        return CaptureRecord(
            gold_id=gold["id"], query=gold["query"], path="parse_query_intent",
            raw_response=raw,
            parsed_output=intent.model_dump(),
            source=getattr(meta, "source", "unknown"),
            error_category=getattr(meta, "error", None),
            attempt_count=self.attempts,
            model_identifier=(getattr(meta, "model", None)
                              if getattr(meta, "source", None) == "ai" else None),
            provider_kind=self.transport.provider_kind,
        )


class JsonlCaptureStore:
    """Append-only JSONL persistence. Never overwrites or rewrites earlier data.

    * ``append`` writes one completed record and flushes, so an interruption
      loses at most the in-flight record.
    * ``load`` recovers a TRUNCATED final line (keeps all earlier valid records).
    * ``completed_keys`` returns ``(gold_id, path)`` pairs that pass STRICT
      validation (used to skip on resume); ineligible/malformed records are NOT
      skipped — they are reported for a future authorized re-run.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)

    def append(self, record: CaptureRecord) -> None:
        # "a" never truncates an existing file; flush+fsync for crash safety.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> tuple[list[CaptureRecord], list[str]]:
        """Return (valid_records, problems). A truncated/malformed final line is
        captured as a problem and dropped WITHOUT losing earlier valid records."""
        if not self.path.exists():
            return [], []
        records: list[CaptureRecord] = []
        problems: list[str] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(CaptureRecord.from_json(line))
                except Exception as exc:  # noqa: BLE001 - malformed/truncated line
                    problems.append(f"line {lineno}: {type(exc).__name__}")
        return records, problems

    def completed_keys(self) -> set[tuple[int, str]]:
        records, _ = self.load()
        done: set[tuple[int, str]] = set()
        for record in records:
            ok, _ = record.validate()
            if ok and record.app_marked_ai and record.response_received:
                done.add((record.gold_id, record.path))
        return done


class CaptureLock:
    """Filesystem lock preventing concurrent runners (offline-testable)."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._held = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self._held = True
        return True

    def release(self) -> None:
        if self._held and self.path.exists():
            self.path.unlink()
        self._held = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another capture runner holds the lock")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def run_live_capture(*args, **kwargs):  # pragma: no cover - intentionally blocked
    """The LIVE runner is NOT implemented in Stage 44-B4.

    Any real capture requires separate, explicit owner approval and an
    authorized network run. This stub exists so the symbol is importable but
    can never be triggered accidentally by an offline test.
    """
    raise QuotaStop(
        "live capture is disabled; separate owner authorization required"
    )




