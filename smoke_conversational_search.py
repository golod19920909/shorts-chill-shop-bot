"""Live multi-category smoke test for the Option C shopping consultant.

Run manually from the project root (uses the local .env):

    .venv/Scripts/python.exe smoke_conversational_search.py

Requires both ALIEXPRESS_APP_KEY/SECRET and GEMINI_API_KEY. It runs four
broad Russian-language searches through the real ConversationSearchEngine,
AliExpress Affiliate API and Gemini, then asks a shown-products comparison
question without starting another marketplace search. It prints bounded
diagnostics and a few real listing titles; it never prints credentials or
affiliate URLs.

This is an intentionally small live smoke, not a benchmark and not a claim
that four queries represent the whole AliExpress catalogue. The broader
cross-category regression matrix remains in the offline test suite.
"""

from __future__ import annotations

import asyncio
import sys

from bot import build_search_service
from config.settings import load_settings
from services.convsearch.contract import Outcome
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState
from services.product_assistant import ProductAssistant


SEARCH_CASES = (
    ("power_tool", "аккумуляторный шуруповёрт"),
    ("portable_power", "портативный аккумулятор"),
    ("water_filter", "фильтр для воды под мойку"),
    ("security_camera", "уличная камера видеонаблюдения"),
)


def _sample_products(products) -> str:
    lines = []
    for product in products[:5]:
        price = (
            f"{product.price:g} {product.currency}"
            if product.price is not None and product.currency
            else "цена не указана"
        )
        lines.append(
            f"  - {product.title[:100]} | {price} | "
            f"affiliate={'yes' if product.affiliate_url else 'no'}"
        )
    return "\n".join(lines) if lines else "  - нет подходящих карточек"


async def main() -> int:
    settings = load_settings()
    if not settings.aliexpress.is_configured:
        print("SMOKE SKIPPED: ALIEXPRESS_APP_KEY/SECRET not configured")
        return 0
    if not settings.gemini_api_key:
        print("SMOKE SKIPPED: GEMINI_API_KEY not configured")
        return 0

    search_service, ai_service = build_search_service(settings)
    # Keep this manual check bounded: one marketplace page per grounded phrase.
    # The production page depth remains configured separately and is covered
    # by the offline multi-page route regressions plus smoke_aliexpress.py.
    search_service.max_search_pages = 1
    engine = ConversationSearchEngine()
    engine.bind_search_service(search_service)
    engine.bind_ai_service(ai_service)
    assistant = ProductAssistant(ai_service=ai_service, search_service=search_service)
    failures: list[str] = []

    try:
        for label, query in SEARCH_CASES:
            user_key = f"live-smoke-{label}"
            state = ConversationEngineState(user_key=user_key)
            print(f"\n=== {label}: {query} ===")
            try:
                decision = await engine.handle_message(
                    user_key,
                    query,
                    state=state,
                    currency="USD",
                    result_limit=30,
                )
            except Exception as exc:  # noqa: BLE001 - smoke must report a safe verdict
                failures.append(f"{label}: engine exception {type(exc).__name__}")
                print(f"FAIL: engine exception {type(exc).__name__}")
                continue

            selection = decision.report.get("ai_selection", "not-run")
            print(
                f"Outcome={decision.outcome.value}; products={len(decision.products)}; "
                f"AI-selection={selection}"
            )
            print(_sample_products(decision.products))
            if decision.outcome is not Outcome.RESULTS or not decision.products:
                failures.append(f"{label}: no result cards ({decision.outcome.value})")
                continue

            ids = [product.product_id for product in decision.products]
            if len(ids) != len(set(ids)):
                failures.append(f"{label}: duplicate product IDs in final result")
            if len(decision.products) > 30:
                failures.append(f"{label}: presentation cap exceeded")
            if any(not product.affiliate_url for product in decision.products):
                failures.append(f"{label}: one or more results lack affiliate links")
            if selection in {
                "timeout_fallback",
                "error_fallback",
                "unavailable_fallback",
                "turn_budget_exhausted",
            }:
                failures.append(f"{label}: Gemini selection did not complete ({selection})")

            # Simulate the handler's actual shown-product ledger before asking
            # the engine to interpret a follow-up about these cards.
            state.record_shown(ids)
            compare = await engine.handle_message(
                user_key,
                "Какой из них лучше?",
                state=state,
                shown_products=decision.products,
                currency="USD",
                result_limit=30,
            )
            print(f"Follow-up outcome={compare.outcome.value}")
            if compare.outcome is not Outcome.COMPARE:
                failures.append(f"{label}: shown-product question did not route to COMPARE")
                continue

            advice = await assistant.answer(
                "Какой из них лучше по рейтингу?",
                decision.products,
            )
            if advice is None or not advice.text.strip():
                failures.append(f"{label}: product comparison returned no answer")
                print("Comparison: no grounded answer available")
            else:
                print(f"Comparison: {advice.text[:500]}")

    finally:
        await search_service.aclose()

    print("\nSMOKE OK" if not failures else f"\nSMOKE FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
