"""B4: in-memory search diagnostics - outcomes, providers, latency, scripts.

Purpose: make the EXISTING search pipeline measurable for the B5 quality
review without changing any search behaviour. Everything here is
observation only: no query interpretation, no ranking, no provider policy.

What is recorded (aggregates only, never user data):

* final outcome of every ``SearchService.find_products`` call, classified
  into one of the :data:`OUTCOMES` labels (see :func:`classify_outcome`);
* stage-level result counts (raw -> after relevance -> after explicit
  constraints -> final) as running totals, so an empty result can be
  attributed to the stage that actually emptied it;
* per-provider reliability: attempts, successful calls, EMPTY successful
  calls, errors, timeouts and per-call latency (bounded samples);
* the search-level latency of ``find_products`` itself (bounded samples);
* the PARSING path of each handler search (AI vs deterministic fallback
  plus parse/intent durations) - a fallback is a parsing-path metric, NOT
  a search failure;
* a cross-writing-system split of raw candidate titles (same script as the
  query / different script / unknown) - a lightweight proxy for
  cross-language retrieval, see :meth:`SearchDiagnostics.record_search`.

Privacy and operational safety (hard rules):

* NO raw queries, product titles, user ids, chat ids, tokens or API keys
  are ever stored or logged here - only counts, labels, modes and
  durations;
* every latency list is a bounded ``deque`` (:data:`MAX_LATENCY_SAMPLES`);
  provider stats are keyed by display name (bounded by the number of
  registered providers); everything else is an integer counter;
* all public record methods are fail-safe: a bug in diagnostics must
  NEVER break a search (:func:`safe_record` and the guards inside each
  method swallow diagnostics errors and only make diagnostics quieter);
* lifecycle: the registry lives in process memory for the process'
  lifetime. It is NOT persisted, NOT shared between processes and NOT
  sent anywhere. :meth:`SearchDiagnostics.reset` clears it (tests,
  operator diagnostics). Consecutive-process totals therefore start from
  zero; the admin AI counters (``AIService.parse_ok`` etc.) and
  ``INTENT_AI_STATS`` remain the long-lived counters they always were -
  this module deliberately does not duplicate their AI-call semantics,
  it only snapshots ``INTENT_AI_STATS`` into the registry snapshot.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outcome labels (diagnostic categories, not a public API enum)
# ---------------------------------------------------------------------------

#: The search completed and produced results for the user.
OUTCOME_SUCCESS_WITH_RESULTS = "SUCCESS_WITH_RESULTS"
#: Provider calls completed successfully but returned no products at all.
OUTCOME_SUCCESS_EMPTY_RAW = "SUCCESS_EMPTY_RAW"
#: Raw products WERE retrieved, but relevance filtering removed all of them.
OUTCOME_EMPTY_AFTER_RELEVANCE = "EMPTY_AFTER_RELEVANCE"
#: Candidates existed and were relevant, but explicit user constraints
#: (price, rating, orders, ...) removed all of them.
OUTCOME_EMPTY_AFTER_CONSTRAINTS = "EMPTY_AFTER_CONSTRAINTS"
#: At least one provider call failed while another completed (with results
#: or at least with candidates). The label wins over the empty-stage labels
#: because provider unreliability is the more actionable diagnosis; the
#: stage counters still preserve how far the surviving pool got.
OUTCOME_PROVIDER_PARTIAL_FAILURE = "PROVIDER_PARTIAL_FAILURE"
#: No provider completed the search (all failed or none was configured).
#: Exactly the condition ``handlers.search._is_total_provider_failure``
#: uses for the honest "search unavailable" message (B3).
OUTCOME_PROVIDER_TOTAL_FAILURE = "PROVIDER_TOTAL_FAILURE"
#: Parsing/intent produced NOTHING to search with (empty effective query).
#: A normal deterministic FALLBACK is deliberately NOT this: fallback still
#: yields a real request and is recorded as a parsing-path metric only.
OUTCOME_PARSE_OR_INTENT_FAILURE = "PARSE_OR_INTENT_FAILURE"
#: Anything that cannot be reliably classified (e.g. no provider was ever
#: registered, so "empty raw" would be a lie).
OUTCOME_OTHER_OR_UNKNOWN = "OTHER_OR_UNKNOWN"

#: Every label, in reporting order.
OUTCOMES: tuple[str, ...] = (
    OUTCOME_SUCCESS_WITH_RESULTS,
    OUTCOME_SUCCESS_EMPTY_RAW,
    OUTCOME_EMPTY_AFTER_RELEVANCE,
    OUTCOME_EMPTY_AFTER_CONSTRAINTS,
    OUTCOME_PROVIDER_PARTIAL_FAILURE,
    OUTCOME_PROVIDER_TOTAL_FAILURE,
    OUTCOME_PARSE_OR_INTENT_FAILURE,
    OUTCOME_OTHER_OR_UNKNOWN,
)

#: How many latency observations each bounded sample list keeps. 500 searches
#: are enough for a meaningful p50/p95 in a B5 batch while the memory cost
#: stays negligible and never grows at runtime.
MAX_LATENCY_SAMPLES = 500



def classify_outcome(outcome: Any, *, query_empty: bool = False) -> str:
    """Classify one finished ``SearchOutcome`` into a B4 diagnostic label.

    Pure function over fields the pipeline already tracks - it never
    re-runs a filter and never infers a cause from the final list alone:

    1. some provider FAILED and some answered -> PARTIAL failure
       (results may still exist; stage counters keep the details);
    2. NO provider answered and some failed/was unconfigured -> TOTAL
       failure (the B3 "search unavailable" condition, verbatim);
    3. final products exist -> SUCCESS_WITH_RESULTS;
    4. the effective query was empty -> PARSE_OR_INTENT_FAILURE (a
       deterministic fallback that produced a request is NOT this);
    5. otherwise the empty is attributed to the FIRST stage that emptied
       it: raw==0 -> SUCCESS_EMPTY_RAW, relevance==0 -> EMPTY_AFTER_
       RELEVANCE, constraints==0 -> EMPTY_AFTER_CONSTRAINTS;
    6. everything else -> OTHER_OR_UNKNOWN (safe fallback: e.g. no
       provider was ever registered, so "the providers answered empty"
       cannot be claimed).
    """

    final = len(getattr(outcome, "products", ()) or ())
    raw = int(getattr(outcome, "products_before_filtering", 0) or 0)
    dropped = int(getattr(outcome, "products_dropped_as_irrelevant", 0) or 0)
    after_constraints = int(getattr(outcome, "products_after_constraints", 0) or 0)
    searched = list(getattr(outcome, "searched_marketplaces", ()) or ())
    failed = list(getattr(outcome, "failed_marketplaces", ()) or ())
    unavailable = list(getattr(outcome, "unavailable_marketplaces", ()) or ())

    if searched and failed:
        return OUTCOME_PROVIDER_PARTIAL_FAILURE
    if not searched and (failed or unavailable):
        return OUTCOME_PROVIDER_TOTAL_FAILURE
    if final > 0:
        return OUTCOME_SUCCESS_WITH_RESULTS
    if query_empty:
        return OUTCOME_PARSE_OR_INTENT_FAILURE
    if not searched:
        # Nothing ever answered and nothing is known to have failed: an
        # "empty answer" label would claim more than the pipeline knows.
        return OUTCOME_OTHER_OR_UNKNOWN
    if raw <= 0:
        return OUTCOME_SUCCESS_EMPTY_RAW
    if raw - dropped <= 0:
        return OUTCOME_EMPTY_AFTER_RELEVANCE
    if after_constraints <= 0:
        return OUTCOME_EMPTY_AFTER_CONSTRAINTS
    return OUTCOME_OTHER_OR_UNKNOWN


def safe_record(call: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Run one diagnostics recording call; never let it raise."""

    try:
        call(*args, **kwargs)
    except Exception:  # noqa: BLE001 - diagnostics must never break a search
        logger.debug("search diagnostics record failed", exc_info=True)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class _ProviderStats:
    """Counters and bounded latency samples of ONE provider display name."""

    __slots__ = (
        "attempts", "successes", "empty_successes", "errors", "timeouts",
        "latencies_ms",
    )

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.empty_successes = 0
        self.errors = 0
        self.timeouts = 0
        self.latencies_ms: deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "empty_successes": self.empty_successes,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "latencies_ms": list(self.latencies_ms),
        }


class SearchDiagnostics:
    """Process-wide, in-memory, bounded search diagnostics registry.

    Lifecycle: created at import, lives for the process, cleared only by
    :meth:`reset`. Never persisted - a restart starts from zero by design
    (see the module docstring).
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Drop every counter and sample (tests / operator diagnostics)."""

        self.searches_total = 0
        self.outcome_counts: Counter[str] = Counter()
        #: outcome counts per SearchMode value - bounded by the mode enum.
        self.outcomes_by_mode: dict[str, Counter[str]] = {}
        self.partial_failure_searches = 0
        self.total_failure_searches = 0

        # Running totals of the stage-level result counts.
        self.raw_results_total = 0
        self.after_relevance_total = 0
        self.after_constraints_total = 0
        self.final_results_total = 0

        # Bounded latency samples (milliseconds).
        self.search_latencies_ms: deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self.parse_latencies_ms: deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self.intent_latencies_ms: deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)

        # Parsing path per handler search (fallback != failure).
        self.intent_ai_searches = 0
        self.intent_fallback_searches = 0

        # Cross-writing-system split of RAW candidate titles (a lightweight
        # cross-language proxy; see record_search).
        self.titles_same_script_total = 0
        self.titles_other_script_total = 0
        self.titles_unknown_script_total = 0

        self._providers: dict[str, _ProviderStats] = {}

    # -- providers ----------------------------------------------------------

    def record_provider_call(
        self,
        display_name: str,
        *,
        latency_ms: float,
        rows: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        """One real provider request completed (successfully or not).

        ``rows`` is the number of raw listings the provider returned for a
        successful call; ``error`` classifies a failed call (a timeout is
        counted separately from other errors). No extra provider call is
        ever made for instrumentation.
        """

        stats = self._providers.setdefault(str(display_name), _ProviderStats())
        stats.attempts += 1
        if latency_ms is not None:
            stats.latencies_ms.append(round(float(latency_ms), 3))
        if error is not None:
            if isinstance(error, TimeoutError):  # includes asyncio's alias
                stats.timeouts += 1
            else:
                stats.errors += 1
            return
        stats.successes += 1
        if rows == 0:
            stats.empty_successes += 1

    # -- whole searches -----------------------------------------------------

    def record_search(
        self,
        outcome: Any,
        *,
        label: str,
        duration_ms: float,
        query_empty: bool = False,
    ) -> None:
        """One finished ``find_products`` call with its classified label."""

        self.searches_total += 1
        self.outcome_counts[label] += 1
        request = getattr(outcome, "request", None)
        mode = getattr(request, "mode", None)
        mode_value = getattr(mode, "value", None) or "unknown"
        self.outcomes_by_mode.setdefault(str(mode_value), Counter())[label] += 1

        if label == OUTCOME_PROVIDER_PARTIAL_FAILURE:
            self.partial_failure_searches += 1
        elif label == OUTCOME_PROVIDER_TOTAL_FAILURE:
            self.total_failure_searches += 1

        raw = int(getattr(outcome, "products_before_filtering", 0) or 0)
        dropped = int(getattr(outcome, "products_dropped_as_irrelevant", 0) or 0)
        after_constraints = int(getattr(outcome, "products_after_constraints", 0) or 0)
        final = len(getattr(outcome, "products", ()) or ())
        self.raw_results_total += raw
        self.after_relevance_total += max(0, raw - dropped)
        self.after_constraints_total += max(0, after_constraints)
        self.final_results_total += final

        self.titles_same_script_total += int(
            getattr(outcome, "titles_same_script", 0) or 0)
        self.titles_other_script_total += int(
            getattr(outcome, "titles_other_script", 0) or 0)
        self.titles_unknown_script_total += int(
            getattr(outcome, "titles_unknown_script", 0) or 0)

        if duration_ms is not None:
            self.search_latencies_ms.append(round(float(duration_ms), 3))

    # -- parsing path -------------------------------------------------------

    def record_parse_path(
        self,
        source: str | None,
        *,
        parse_ms: float | None = None,
        intent_ms: float | None = None,
    ) -> None:
        """One handler search took the AI (``"ai"``) or fallback parse path.

        A deterministic fallback is NORMAL behaviour (no key, quota, circuit
        open) and is counted here as a parsing-path metric - it never marks
        the search itself as failed.
        """

        if source == "ai":
            self.intent_ai_searches += 1
        else:
            self.intent_fallback_searches += 1
        if parse_ms is not None:
            self.parse_latencies_ms.append(round(float(parse_ms), 3))
        if intent_ms is not None:
            self.intent_latencies_ms.append(round(float(intent_ms), 3))

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """A plain-dict copy for tests, the admin screen and the B5 review.

        Latency samples are included as full (bounded) lists so percentiles
        are computed from ACTUAL observations, never invented.
        """

        from services.query_intent import INTENT_AI_STATS

        total = self.searches_total
        return {
            "searches_total": total,
            "outcome_counts": dict(self.outcome_counts),
            "outcome_rates": {
                label: (count / total if total else 0.0)
                for label, count in self.outcome_counts.items()
            },
            "outcomes_by_mode": {
                mode: dict(counts)
                for mode, counts in self.outcomes_by_mode.items()
            },
            "partial_failure_searches": self.partial_failure_searches,
            "total_failure_searches": self.total_failure_searches,
            "stage_totals": {
                "raw": self.raw_results_total,
                "after_relevance": self.after_relevance_total,
                "after_constraints": self.after_constraints_total,
                "final": self.final_results_total,
            },
            "search_latencies_ms": list(self.search_latencies_ms),
            "parse_latencies_ms": list(self.parse_latencies_ms),
            "intent_latencies_ms": list(self.intent_latencies_ms),
            "intent_ai_searches": self.intent_ai_searches,
            "intent_fallback_searches": self.intent_fallback_searches,
            "title_scripts": {
                "same_as_query": self.titles_same_script_total,
                "different_from_query": self.titles_other_script_total,
                "unknown": self.titles_unknown_script_total,
            },
            "providers": {
                name: stats.snapshot()
                for name, stats in sorted(self._providers.items())
            },
            # Reused, not duplicated: the long-lived AI-call counters of the
            # intent layer, copied so one snapshot answers one question.
            "intent_ai_stats": dict(INTENT_AI_STATS),
        }


#: The one process-wide registry (mirrors the ``INTENT_AI_STATS`` convention).
SEARCH_DIAGNOSTICS = SearchDiagnostics()


def record_parse_path(
    source: str | None,
    *,
    parse_ms: float | None = None,
    intent_ms: float | None = None,
) -> None:
    """Fail-safe module-level convenience wrapper for the handlers layer."""

    safe_record(
        SEARCH_DIAGNOSTICS.record_parse_path, source,
        parse_ms=parse_ms, intent_ms=intent_ms,
    )
