"""Offline end-to-end smoke of the discovery flows (no network, no Telegram).

Drives the REAL handlers through the fakes from ``tests.test_handlers_ui`` and
checks the card-first contract of the whole UX:

    python smoke_ux_flows.py     # prints SMOKE OK / SMOKE FAILED

Covers: text search, discovery/«✨ Находки» feed, the «🎟 Промокоды» section (one
configured URL button + legacy tombstone), photo search (bounded refinement),
the legacy "next page" button and the tracking status.
Never runs from the test suite - it is a manual, one-command check.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import keyboards.main as keyboards_main
import keyboards.products as keyboards_products
from config.promo import PROMO_REFERRAL_URL
from handlers import discovery
from handlers import promo_codes
from handlers import search as search_handlers
from handlers import settings as settings_handlers
from handlers.start import dispatch_menu
from models.search_request import SearchRequest
from services.search_service import SearchOutcome
from tests.test_handlers_ui import (
    FakeMessage,
    FakeQuery,
    FakeUpdate,
    FakeUser,
    callbacks_of,
    labels_of,
    make_context,
    make_outcome,
    make_product,
)

#: Strings a card-first bot must never show on a product screen.
FORBIDDEN = ("Найдено товаров", "Показать ещё", "Страница", "товары 6-7")
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    verdict = "OK  " if condition else "FAIL"
    print(f"{verdict} {name}{'' if condition else ' :: ' + detail}")
    if not condition:
        FAILURES.append(name)


def clean(text: str) -> bool:
    return not any(bad in text for bad in FORBIDDEN)


def smoke_text_search() -> None:
    context = make_context()
    asyncio.run(
        search_handlers.send_search_results(context, 47, make_outcome(count=5))
    )
    card = context.bot.texts[0]
    labels = labels_of(context.bot.keyboards[0])
    check("search: exactly one message", len(context.bot.messages) == 1)
    check(
        "search: card starts with the product (no query echo)",
        card.startswith("Товар 0") and "товар</b>" not in card,
        card[:60],
    )
    check("search: no counter/page/list markers", clean(card), card[:120])
    check("search: card actions", "❤️ В избранное" in labels, str(labels))
    check("search: next walks the feed", "➡️ Следующий" in labels, str(labels))
    check(
        "search: home button wired",
        "menu:back" in callbacks_of(context.bot.keyboards[0]),
    )


def smoke_discovery_feed() -> None:
    context = make_context()
    asyncio.run(
        search_handlers.send_search_results(
            context,
            47,
            make_outcome(count=4),
            header="✨ <b>Находки</b>",
            back_callback="menu:back",
        )
    )
    card = context.bot.texts[0]
    callbacks = callbacks_of(context.bot.keyboards[0])
    check("discovery: feed name on the card", card.startswith("✨ <b>Находки</b>"))
    check("discovery: no counter/page markers", clean(card), card[:120])
    check("discovery: back leaves the feed", "menu:back" in callbacks, str(callbacks))


def smoke_promo_codes() -> None:
    """«🎟 Промокоды»: ONE screen with ONE configured destination."""

    context = make_context()
    query = FakeQuery("menu:pcodes")
    asyncio.run(
        dispatch_menu("menu:pcodes", FakeUpdate(callback_query=query), context)
    )
    check("promo: one screen rendered", len(query.edits) == 1, str(query.edits))
    if query.edits:
        text, kwargs = query.edits[-1]
        rows = kwargs["reply_markup"].inline_keyboard
        urls = [button.url for row in rows for button in row if button.url]
        check(
            "promo: exactly the configured URL",
            urls == [PROMO_REFERRAL_URL],
            str(urls),
        )
        check(
            "promo: no campaign list on the screen",
            clean(text) and "кампани" not in text.lower(),
            text[:120],
        )
        check(
            "promo: home button wired",
            "menu:back" in callbacks_of(rows),
            str(callbacks_of(rows)),
        )

    legacy = FakeQuery("pcamp:0")
    asyncio.run(
        promo_codes.promo_legacy_callback(FakeUpdate(callback_query=legacy), context)
    )
    check(
        "promo: legacy campaign press answered once",
        len(legacy.answers) == 1,
        str(legacy.answers),
    )
    if legacy.edits:
        text, kwargs = legacy.edits[-1]
        urls = [
            button.url
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.url
        ]
        check(
            "promo: legacy press shows the current URL button",
            urls == [PROMO_REFERRAL_URL],
            str(urls),
        )


class _Vision:
    is_available = True

    async def describe_image_query(self, image_bytes: bytes) -> str:
        return "синий кожаный рюкзак с молнией и карманами"


class _SearchService:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def find_products(self, request, *, exclude=None, broaden=True) -> SearchOutcome:
        self.queries.append(request.original_query)
        products = [] if len(self.queries) == 1 else [make_product(0)]
        return SearchOutcome(request=request, products=products)

    async def find_products_from_text(
        self, text: str, currency=None, exclude=None
    ) -> SearchOutcome:
        self.queries.append(text)
        products = [] if len(self.queries) == 1 else [make_product(0)]
        return SearchOutcome(
            request=SearchRequest(original_query=text), products=products
        )


class _PhotoFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"image-bytes")


class _Photo:
    file_size = 1024

    async def get_file(self) -> _PhotoFile:
        return _PhotoFile()


class _PhotoMessage(FakeMessage):
    def __init__(self) -> None:
        super().__init__()
        self.photo = [_Photo()]
        self.text = ""


def smoke_photo_search() -> None:
    search_service = _SearchService()
    context = make_context(ai_service=_Vision(), search_service=search_service)
    message = _PhotoMessage()
    asyncio.run(
        discovery.photo_message(FakeUpdate(message=message, user=FakeUser()), context)
    )
    check(
        "photo: recognised query first (cleaned, whole words)",
        search_service.queries[:1]
        == ["синий кожаный рюкзак молнией карманами"],
        str(search_service.queries),
    )
    check(
        "photo: bounded refinement (<= MAX_PHOTO_ATTEMPTS)",
        len(search_service.queries) <= discovery.MAX_PHOTO_ATTEMPTS,
        str(search_service.queries),
    )
    check(
        "photo: second attempt is a grounded shorter phrase",
        len(search_service.queries) < 2
        or search_service.queries[1] == "синий кожаный рюкзак молнией",
        str(search_service.queries),
    )
    check("photo: card shown after the retry", bool(context.bot.messages))
    if context.bot.messages:
        card = context.bot.texts[-1]
        check("photo: no counter/list markers", clean(card), card[:200])
        check("photo: no generated query on the card", "Запрос:" not in card, card[:200])


def smoke_legacy_next_button() -> None:
    context = make_context()
    session_id = asyncio.run(
        search_handlers.send_search_results(context, 47, make_outcome(count=3))
    )
    query = FakeQuery(f"next:{session_id}")
    asyncio.run(
        search_handlers.next_page_callback(FakeUpdate(callback_query=query), context)
    )
    check("legacy next: honest answer", bool(query.answers), str(query.answers))
    if query.answers:
        check("legacy next: points at the card", "Следующий" in query.answers[0])
    check("legacy next: sends nothing new", len(context.bot.messages) == 1)


def smoke_tracking_status() -> None:
    context = make_context(
        settings=SimpleNamespace(tracking_interval_minutes=1440),
        price_tracker=object(),
    )
    line = settings_handlers.tracking_status_line(context)
    check("tracking: 24h reads as days", "раз в 1 дн." in line, line)
    check("tracking: minutes never leak", "1440" not in line, line)


def smoke_dead_screens_are_gone() -> None:
    removed = (
        "product_card_keyboard",
        "products_summary_rows",
        "products_summary_keyboard",
    )
    check(
        "cleanup: list/summary keyboards deleted",
        not any(hasattr(keyboards_products, name) for name in removed),
        str([name for name in removed if hasattr(keyboards_products, name)]),
    )
    check(
        "cleanup: pagination keyboard deleted",
        not hasattr(keyboards_main, "pagination_keyboard")
        and not hasattr(keyboards_main, "LABEL_NEXT"),
    )


def main() -> int:
    smoke_text_search()
    smoke_discovery_feed()
    smoke_promo_codes()
    smoke_photo_search()
    smoke_legacy_next_button()
    smoke_tracking_status()
    smoke_dead_screens_are_gone()
    print("SMOKE OK" if not FAILURES else f"SMOKE FAILED: {FAILURES}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())

