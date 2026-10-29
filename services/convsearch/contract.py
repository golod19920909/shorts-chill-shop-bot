"""The result contract between the conversational engine and Telegram.

The engine returns ONE structured decision per turn; the presentation layer
renders it and never re-runs search, re-filters, or overrides the decision.
Every outcome kind is honest and mutually exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from models.product import Product


class Outcome(str, Enum):
    """What the engine decided this turn should deliver to the user."""

    #: Eligible products exist and are delivered (possibly fewer than the cap).
    RESULTS = "results"
    #: The provider returned rows, but none qualified (type/constraints).
    NO_ELIGIBLE = "no_eligible"
    #: The provider answered with zero rows for the request.
    NO_CANDIDATES = "no_candidates"
    #: No provider answered / every provider failed — NOT an empty catalogue.
    PROVIDER_ERROR = "provider_error"
    #: The request is too vague to search meaningfully.
    CLARIFY = "clarify"
    #: Comparison / explanation over the SHOWN set; no new search needed.
    COMPARE = "compare"
    #: Engine-level input error (retrieval strategy could not be built).
    INSUFFICIENT_INFO = "insufficient_info"


@dataclass
class SearchDecision:
    """The engine's one decision for one user message."""

    outcome: Outcome
    #: Products for RESULTS (already validated, ranked, capped); empty otherwise.
    products: list[Product] = field(default_factory=list)
    #: The effective request the retrieval ran with (for cards/history).
    request: object | None = None
    #: Clarification question for CLARIFY, else None.
    question: str | None = None
    #: Whether this turn refined the active conversation.
    refined: bool = False
    #: Full traceability report (log only, never user-facing).
    report: dict = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.outcome is Outcome.RESULTS and bool(self.products)
