"""Conversational search engine (adapted from anything-conversational-search).

The single search authority of the bot. One engine turn: interpret the
message → update the ledger → build the retrieval strategy → retrieve via
the AliExpress adapter → validate (hard product-type gate) → rank → return
one ``SearchDecision`` to the Telegram layer.
"""

from services.convsearch.contract import Outcome, SearchDecision
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState, MessageIntent

__all__ = [
    "ConversationSearchEngine",
    "SearchDecision",
    "Outcome",
    "ConversationEngineState",
    "MessageIntent",
]
