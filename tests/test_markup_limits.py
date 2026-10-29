"""Live-verified reply-markup budget: keyboards must never be refused.

Telegram rejects a WHOLE message with "Reply markup is too long" when the
long URL buttons of one keyboard exceed a shared budget (measured live:
a sum of 11 011 chars passes, 11 894 fails; URLs <= 1000 chars are free).
The real favorites screen summed ~23 900 and pressing ❤️ Избранное then
did NOTHING - the edit and the fresh send were both refused.

These tests pin the budget logic of every keyboard that renders product
URLs and the favorites handler's slim-keyboard fallback.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from telegram.error import BadRequest

from handlers import favorites
from keyboards.limits import (
    LONG_URL_BUDGET,
    LONG_URL_FREE,
    UrlBudget,
    url_cost,
)
from keyboards.main import CB_BACK
from keyboards.products import (
    CB_PREFIX_FAVORITE_REMOVE,
    browse_card_keyboard,
    favorites_list_keyboard,
    tracked_list_keyboard,
)
from models.product import Marketplace, Product


def run(coro):
    return asyncio.run(coro)


def long_url(seed: str, length: int = 1011) -> str:
    """An affiliate-shaped URL just over the free threshold."""

    prefix = f"https://s.click.aliexpress.com/e/promo?p={seed}&"
    return prefix + "a" * max(0, length - len(prefix))


def make_product(product_id: str, url: str, title: str = "Товар") -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=url,
        affiliate_url=url,
        price=10.0,
        currency="USD",
    )


def counted_urls(markup) -> list[str]:
    """Every URL of the markup that costs budget (> LONG_URL_FREE chars)."""

    return [
        button.url
        for row in markup.inline_keyboard
        for button in row
        if getattr(button, "url", None) and url_cost(button.url) > 0
    ]


def url_budget_sum(markup) -> int:
    return sum(url_cost(url) for url in counted_urls(markup))


def callback_datas(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if getattr(button, "callback_data", None)
    ]


class UrlBudgetUnitTests(unittest.TestCase):
    def test_short_urls_are_free(self) -> None:
        budget = UrlBudget()
        before = budget.remaining
        self.assertTrue(budget.try_take("https://example.com/p?x=1"))
        self.assertEqual(budget.remaining, before)

    def test_urls_above_the_threshold_consume_the_budget(self) -> None:
        budget = UrlBudget()
        url = long_url("x")
        self.assertGreater(len(url), LONG_URL_FREE)
        self.assertTrue(budget.try_take(url))
        self.assertEqual(budget.remaining, LONG_URL_BUDGET - len(url))

    def test_an_unaffordable_url_is_refused_not_truncated(self) -> None:
        budget = UrlBudget(budget=100)
        self.assertFalse(budget.try_take(long_url("x", 1011)))
        self.assertEqual(budget.remaining, 100)

    def test_url_cost_matches_the_threshold(self) -> None:
        exactly_free = "https://e.x/" + "a" * (LONG_URL_FREE - len("https://e.x/"))
        self.assertEqual(len(exactly_free), LONG_URL_FREE)
        self.assertEqual(url_cost(None), 0)
        self.assertEqual(url_cost(exactly_free), 0)
        self.assertEqual(url_cost(exactly_free + "a"), LONG_URL_FREE + 1)


class FavoritesKeyboardBudgetTests(unittest.TestCase):
    def build(self, count: int, url) -> list:
        items = [
            {"index": i, "product": make_product(str(i + 1), url(i + 1))}
            for i in range(count)
        ]
        return favorites_list_keyboard(items)

    def test_realistic_screen_stays_inside_the_verified_budget(self) -> None:
        # The live failure: 6 affiliate links + 6 share links ≈ 23 900 chars.
        markup = self.build(6, lambda i: long_url(f"p{i}"))
        self.assertLessEqual(url_budget_sum(markup), LONG_URL_BUDGET)

    def test_every_product_stays_visible_and_removable(self) -> None:
        markup = self.build(12, lambda i: long_url(f"p{i}"))
        removes = [
            data
            for data in callback_datas(markup)
            if data.startswith(f"{CB_PREFIX_FAVORITE_REMOVE}:")
        ]
        self.assertEqual(len(removes), 12)
        self.assertIn(CB_BACK, callback_datas(markup))

    def test_at_least_one_open_link_survives_a_full_screen(self) -> None:
        markup = self.build(12, lambda i: long_url(f"p{i}"))
        self.assertTrue(counted_urls(markup))

    def test_short_urls_render_every_button(self) -> None:
        # Budgets must never drop anything while the screen is small.
        markup = self.build(10, lambda i: f"https://www.aliexpress.com/item/{i}.html")
        urls = [
            button.url
            for row in markup.inline_keyboard
            for button in row
            if getattr(button, "url", None)
        ]
        # 10 open + 10 share buttons
        self.assertEqual(len(urls), 20)
        self.assertEqual(url_budget_sum(markup), 0)


class TrackedListKeyboardBudgetTests(unittest.TestCase):
    def test_long_urls_never_exceed_the_budget(self) -> None:
        items = [
            {
                "tracked_id": i + 1,
                "title": f"Товар {i + 1}",
                "url": long_url(f"t{i}"),
                "price": 10.0,
                "currency": "USD",
            }
            for i in range(15)
        ]
        markup = tracked_list_keyboard(items)
        self.assertLessEqual(url_budget_sum(markup), LONG_URL_BUDGET)
        # Every product still has its stop button and a way home.
        stops = [d for d in callback_datas(markup) if d.startswith("tstop:")]
        self.assertEqual(len(stops), 15)
        self.assertIn(CB_BACK, callback_datas(markup))

    def test_short_urls_render_every_open_button(self) -> None:
        items = [
            {
                "tracked_id": i + 1,
                "title": f"Товар {i + 1}",
                "url": f"https://www.aliexpress.com/item/{i + 1}.html",
                "price": 10.0,
                "currency": "USD",
            }
            for i in range(8)
        ]
        markup = tracked_list_keyboard(items)
        urls = [
            button.url
            for row in markup.inline_keyboard
            for button in row
            if getattr(button, "url", None)
        ]
        self.assertEqual(len(urls), 8)


class CardKeyboardBudgetTests(unittest.TestCase):
    def test_buy_is_kept_and_share_is_dropped_first(self) -> None:
        product = make_product("1", long_url("card", length=7000))
        markup = browse_card_keyboard("s", 0, 3, product)
        urls = counted_urls(markup)
        self.assertLessEqual(url_budget_sum(markup), LONG_URL_BUDGET)
        # The buy link (the product itself) survives; the share link is the
        # one dropped when the budget is tight.
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], product.affiliate_url)
        # Navigation and home are callback buttons - never budgeted.
        self.assertIn(CB_BACK, callback_datas(markup))

    def test_normal_card_keeps_both_links(self) -> None:
        product = make_product("1", long_url("card", length=1011))
        markup = browse_card_keyboard("s", 1, 3, product)
        self.assertEqual(len(counted_urls(markup)), 2)


class FakeMessage:
    def __init__(self, chat_id: int = 47) -> None:
        self.chat = type("Chat", (), {"id": chat_id})()
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeQuery:
    """Callback whose first edit may be refused as oversized."""

    def __init__(self, fail_first_edit: bool = False, fail_all_edits: bool = False) -> None:
        self.data = "menu:favorites"
        self.message = FakeMessage()
        self.edit_calls: list[tuple[str, dict]] = []
        self.answers: list[tuple[str | None, bool]] = []
        self.fail_first_edit = fail_first_edit
        self.fail_all_edits = fail_all_edits

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edit_calls.append((text, kwargs))
        if self.fail_all_edits or (self.fail_first_edit and len(self.edit_calls) == 1):
            raise BadRequest("Reply markup is too long")


class FakeBot:
    """Fresh sends refused as oversized on the first attempt only."""

    def __init__(self, fail_first: bool) -> None:
        self.calls: list[dict] = []
        self.fail_first = fail_first

    async def send_message(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            raise BadRequest("Reply markup is too long")


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeChat:
    id = 47


class FakeUpdate:
    def __init__(self, callback_query) -> None:
        self.callback_query = callback_query
        self.effective_message = None
        self.effective_chat = FakeChat()
        self.effective_user = FakeUser()


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, database, bot) -> None:
        self.bot = bot
        self.user_data: dict = {}
        self.application = FakeApplication({"database": database})


class FakeDatabase:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def get_or_create_user(self, telegram_id, username, first_name) -> int:
        return 1

    async def get_favorites(self, user_id) -> list[dict]:
        return [dict(row) for row in self.rows]

    async def record_event(self, event_type: str, **kwargs) -> None:
        return None


def favorite_row(product: Product) -> dict:
    payload = {
        "marketplace": product.marketplace.value,
        "product_id": product.product_id,
        "title": product.title,
        "product_url": product.product_url,
        "price": product.price,
        "currency": product.currency,
    }
    return {
        "marketplace": product.marketplace.value,
        "product_id": product.product_id,
        "product_json": json.dumps(payload, ensure_ascii=False),
    }


class FavoritesOversizedMarkupFallbackTests(unittest.TestCase):
    """Even if Telegram refuses the screen anyway, the user gets it."""

    def setUp(self) -> None:
        product = make_product("1", long_url("fav"))
        self.database = FakeDatabase([favorite_row(product)])

    def test_edit_refusal_retries_with_the_small_keyboard(self) -> None:
        query = FakeQuery(fail_first_edit=True)
        context = FakeContext(self.database, FakeBot(fail_first=False))
        update = FakeUpdate(query)

        run(favorites.show_favorites(update, context))

        self.assertEqual(len(query.edit_calls), 2)
        first = query.edit_calls[0][1].get("reply_markup")
        second = query.edit_calls[1][1].get("reply_markup")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(
            json.dumps(first.to_dict()), json.dumps(second.to_dict())
        )
        self.assertLessEqual(url_budget_sum(second), LONG_URL_BUDGET)

    def test_send_refusal_still_delivers_the_screen(self) -> None:
        # Every edit refused AND the first fresh send refused: the slim
        # retry must land and the stale message must then be removed.
        query = FakeQuery(fail_all_edits=True)
        bot = FakeBot(fail_first=True)
        context = FakeContext(self.database, bot)
        update = FakeUpdate(query)

        run(favorites.show_favorites(update, context))

        # 1st send refused, slim send delivered.
        self.assertEqual(len(bot.calls), 2)
        slim = bot.calls[1].get("reply_markup")
        self.assertIsNotNone(slim)
        self.assertLessEqual(url_budget_sum(slim), LONG_URL_BUDGET)
        self.assertTrue(query.message.deleted)


if __name__ == "__main__":
    unittest.main()

