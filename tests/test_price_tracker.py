"""Tests for the real automatic price-tracking scheduler."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from services.price_tracker import (
    DEFAULT_MIN_CHANGE_PERCENT,
    PRICE_TRACKING_JOB_NAME,
    PriceChange,
    PriceTracker,
    format_notification,
    price_tracking_job,
    schedule_price_tracking,
)


class StubProduct:
    """Duck-typed stand-in for models.product.Product."""

    def __init__(self, price=None, currency="USD", affiliate_url=None, title="T"):
        self.price = price
        self.currency = currency
        self.affiliate_url = affiliate_url
        self.title = title


class StubProvider:
    """Maps product_id -> product | None | Exception."""

    def __init__(self, mapping):
        self._mapping = mapping
        #: every ``currency`` the scheduler requested, in call order
        self.requested_currencies: list = []

    async def get_product(self, product_id, currency=None):
        self.requested_currencies.append(currency)
        item = self._mapping.get(product_id)
        if isinstance(item, Exception):
            raise item
        return item


class StubDatabase:
    """In-memory stand-in with the exact scheduler-facing API."""

    def __init__(self, rows):
        self._rows = rows
        self.history = []
        self.observations = []
        self.events = []

    async def get_active_tracked_with_telegram(self):
        return [dict(r) for r in self._rows]

    async def record_event(self, event_type, **kwargs):
        self.events.append({"type": event_type, **kwargs})

    async def record_price_history(
        self, tracked_id, price, shipping_cost=None, currency=None
    ):
        self.history.append((tracked_id, price, currency))

    async def update_tracked_observation(self, tracked_id, new_price, currency):
        self.observations.append((tracked_id, new_price, currency))
        for row in self._rows:
            if row["id"] == tracked_id:
                row["last_checked_price"] = new_price
                row["currency"] = currency


class NotifierSpy:
    def __init__(self):
        #: (chat_id, text, buy_url) triples - buy_url is the inline button
        self.calls = []

    async def __call__(self, chat_id, text, buy_url=None):
        self.calls.append((chat_id, text, buy_url))


def make_row(**overrides):
    row = {
        "id": 1,
        "user_id": 10,
        "product_id": "1001",
        "title": "Наушники",
        "product_url": "https://www.aliexpress.com/item/1001.html",
        "last_checked_price": 10.0,
        "currency": "USD",
        "telegram_id": 555,
        # Columns added by the enriched scheduler join:
        "user_currency": "USD",
        "notify_price_changes": 1,
    }
    row.update(overrides)
    return row


class PriceTrackerUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_observation_sets_baseline_silently(self):
        db = StubDatabase([make_row(last_checked_price=None)])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=10.0)}), notifier
        )
        changes = await tracker.check_all()
        self.assertEqual(changes, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [(1, 10.0, "USD")])
        self.assertEqual(db.observations, [(1, 10.0, "USD")])

    async def test_unchanged_price_records_nothing(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=10.0)}), notifier
        )
        changes = await tracker.check_all()
        self.assertEqual(changes, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [])
        self.assertEqual(db.observations, [])

    async def test_small_change_recorded_but_silent(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=10.05)}), notifier
        )
        changes = await tracker.check_all()
        self.assertEqual(changes, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [(1, 10.05, "USD")])

    async def test_meaningful_change_notifies(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=8.0)}), notifier
        )
        changes = await tracker.check_all()
        self.assertEqual(len(changes), 1)
        chat_id, text, buy_url = notifier.calls[0]
        self.assertEqual(chat_id, 555)
        self.assertEqual(text, format_notification(changes[0]))
        self.assertIn("8.00", text)
        self.assertIn("Наушники", text)
        # Inline button target: plain product URL (no affiliate on stub)
        self.assertEqual(
            buy_url, "https://www.aliexpress.com/item/1001.html"
        )
        # No raw URL in the notification text itself (spec §59)
        self.assertNotIn("http", text)
        self.assertEqual(changes[0].percent, -20.0)

    async def test_identical_repeat_does_not_spam(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=8.0)}), notifier
        )
        await tracker.check_all()
        await tracker.check_all()
        self.assertEqual(len(notifier.calls), 1)

    async def test_unavailable_product_skipped_entry_stays_active(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(db, StubProvider({"1001": None}), notifier)
        self.assertEqual(await tracker.check_all(), [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [])

    async def test_malformed_price_skipped(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=None)}), notifier
        )
        self.assertEqual(await tracker.check_all(), [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [])

    async def test_provider_error_does_not_break_batch(self):
        db = StubDatabase(
            [make_row(id=1, product_id="BAD"), make_row(id=2, product_id="GOOD")]
        )
        notifier = NotifierSpy()
        provider = StubProvider(
            {"BAD": RuntimeError("boom"), "GOOD": StubProduct(price=5.0)}
        )
        tracker = PriceTracker(db, provider, notifier)
        changes = await tracker.check_all()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].tracked_product_id, 2)
        self.assertEqual(len(notifier.calls), 1)

    async def test_currency_mismatch_never_notifies_and_rebases(self):
        """Spec regression: UAH baseline vs USD observation must be silent.

        The bot must NOT compute a percentage, must NOT claim the price
        rose/fell, must record the observation and rebase the baseline.
        """
        db = StubDatabase([make_row(currency="UAH", last_checked_price=29932.47)])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db,
            StubProvider({"1001": StubProduct(price=610.75, currency="USD")}),
            notifier,
        )
        changes = await tracker.check_all()

        self.assertEqual(changes, [])
        self.assertEqual(notifier.calls, [])
        # The observation is recorded and becomes the new baseline.
        self.assertEqual(db.history, [(1, 610.75, "USD")])
        self.assertEqual(db.observations, [(1, 610.75, "USD")])
        # ...and a diagnostic event is persisted for the admin panel.
        self.assertTrue(
            any(e["type"] == "currency_mismatch" for e in db.events)
        )

    async def test_scheduler_requests_the_users_persisted_currency(self):
        """Spec §28: the scheduler must obtain the price in the user currency."""
        db = StubDatabase([make_row(user_currency="EUR")])
        provider = StubProvider({"1001": StubProduct(price=10.0)})
        tracker = PriceTracker(db, provider, NotifierSpy())
        await tracker.check_all()
        self.assertEqual(provider.requested_currencies, ["EUR"])

    async def test_notification_disabled_records_but_stays_silent(self):
        """Spec §36: user disabled notifications -> observation recorded."""
        db = StubDatabase([make_row(notify_price_changes=0)])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=8.0)}), notifier
        )
        changes = await tracker.check_all()
        self.assertEqual(changes, [])
        self.assertEqual(notifier.calls, [])
        self.assertEqual(db.history, [(1, 8.0, "USD")])
        self.assertEqual(db.observations, [(1, 8.0, "USD")])

    async def test_notification_uses_inline_buy_button(self):
        """Spec §59: raw URLs are gone; the buy URL travels separately."""
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=8.0)}), notifier
        )
        await tracker.check_all()
        chat_id, text, buy_url = notifier.calls[0]
        self.assertNotIn("http", text)
        self.assertNotIn("Купить", text)
        # Plain product URL fallback (no affiliate on the stub product)
        self.assertEqual(buy_url, "https://www.aliexpress.com/item/1001.html")

        db2 = StubDatabase([make_row()])
        notifier2 = NotifierSpy()
        tracker2 = PriceTracker(
            db2,
            StubProvider(
                {
                    "1001": StubProduct(
                        price=8.0,
                        affiliate_url="https://s.click.aliexpress.com/e?af=1",
                    )
                }
            ),
            notifier2,
        )
        await tracker2.check_all()
        _, text2, buy_url2 = notifier2.calls[0]
        self.assertNotIn("http", text2)
        # Affiliate URL preferred when the API returned one
        self.assertEqual(buy_url2, "https://s.click.aliexpress.com/e?af=1")

    async def test_percent_without_baseline_and_threshold_constant(self):
        change = PriceChange(
            tracked_product_id=1, user_id=1, telegram_id=1, product_id="1",
            title="t", old_price=None, new_price=5.0,
            old_currency=None, new_currency="USD", affiliate_url=None,
        )
        self.assertIsNone(change.percent)
        self.assertEqual(DEFAULT_MIN_CHANGE_PERCENT, 1.0)

    def test_percent_is_never_computed_across_currencies(self):
        change = PriceChange(
            tracked_product_id=1, user_id=1, telegram_id=1, product_id="1",
            title="t", old_price=29932.47, new_price=610.75,
            old_currency="UAH", new_currency="USD", affiliate_url=None,
        )
        self.assertTrue(change.currency_changed)
        self.assertIsNone(change.percent)
        # ...and the formatter shows no percentage either (defense in depth)
        text = format_notification(change)
        self.assertNotIn("%", text)
        self.assertNotIn("снизилась", text)
        self.assertNotIn("-98", text)

    def test_buy_url_falls_back_to_product_url(self):
        change = PriceChange(
            tracked_product_id=1, user_id=1, telegram_id=1, product_id="1",
            title="t", old_price=10.0, new_price=8.0,
            old_currency="USD", new_currency="USD", affiliate_url=None,
            product_url="https://www.aliexpress.com/item/1.html",
        )
        self.assertEqual(change.buy_url, "https://www.aliexpress.com/item/1.html")
        with_affiliate = PriceChange(
            tracked_product_id=1, user_id=1, telegram_id=1, product_id="1",
            title="t", old_price=10.0, new_price=8.0,
            old_currency="USD", new_currency="USD",
            affiliate_url="https://s.click.aliexpress.com/e?af=9",
            product_url="https://www.aliexpress.com/item/1.html",
        )
        self.assertEqual(with_affiliate.buy_url, "https://s.click.aliexpress.com/e?af=9")


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _app(jq, interval):
        return SimpleNamespace(
            job_queue=jq,
            bot_data={"settings": SimpleNamespace(tracking_interval_minutes=interval)},
        )

    def test_schedule_registers_job(self):
        class FakeJQ:
            def __init__(self):
                self.calls = []

            def run_repeating(self, callback, interval, first, name):
                self.calls.append((callback, interval, first, name))

        jq = FakeJQ()
        self.assertTrue(schedule_price_tracking(self._app(jq, 60)))
        callback, interval, _first, name = jq.calls[0]
        self.assertIs(callback, price_tracking_job)
        self.assertEqual(interval, 3600)
        self.assertEqual(name, PRICE_TRACKING_JOB_NAME)

    def test_schedule_disabled_on_zero(self):
        class NoJQ:
            def run_repeating(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("must not schedule")

        self.assertFalse(schedule_price_tracking(self._app(NoJQ(), 0)))

    def test_schedule_disabled_without_job_queue(self):
        app = SimpleNamespace(
            job_queue=None,
            bot_data={"settings": SimpleNamespace(tracking_interval_minutes=60)},
        )
        self.assertFalse(schedule_price_tracking(app))

    async def test_job_callback_uses_tracker(self):
        db = StubDatabase([make_row()])
        notifier = NotifierSpy()
        tracker = PriceTracker(
            db, StubProvider({"1001": StubProduct(price=8.0)}), notifier
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"price_tracker": tracker})
        )
        await price_tracking_job(context)
        self.assertEqual(len(notifier.calls), 1)

    async def test_job_callback_without_tracker_is_noop(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={}))
        await price_tracking_job(context)  # must not raise


class RealDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end over the real SQLite layer (no fake DB)."""

    async def asyncSetUp(self):
        import os
        import tempfile

        from database.database import Database

        self._tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self._tmp, "t.db"))
        await self.db.init_db()
        self.user_id = await self.db.get_or_create_user(777001)

    async def asyncTearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_full_cycle_with_real_database(self):
        # add_tracked_product seeds last_checked_price = initial (10.0),
        # so the first cycle with 8.0 is a real -20% change.
        await self.db.add_tracked_product(
            self.user_id, "aliexpress", "1005001", "Тестовый товар",
            "https://www.aliexpress.com/item/1005001.html", 10.0, "USD",
        )
        rows = await self.db.get_active_tracked_with_telegram()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["telegram_id"], 777001)
        tracked_id = rows[0]["id"]

        notifier = NotifierSpy()
        provider = StubProvider({"1005001": StubProduct(price=8.0)})
        tracker = PriceTracker(self.db, provider, notifier)

        await tracker.check_all()  # 10.0 -> 8.0: notify
        await tracker.check_all()  # unchanged: silence
        provider._mapping["1005001"] = StubProduct(price=6.0)
        await tracker.check_all()  # 8.0 -> 6.0: notify

        self.assertEqual(len(notifier.calls), 2)
        self.assertIn("8.00", notifier.calls[0][1])
        self.assertIn("6.00", notifier.calls[1][1])
        self.assertEqual(notifier.calls[0][0], 777001)
        # The scheduler requested the user's persisted currency (here: none
        # set -> None, honestly forwarded).
        self.assertEqual(provider.requested_currencies, [None, None, None])

        rows = await self.db.get_active_tracked_with_telegram()
        self.assertEqual(rows[0]["last_checked_price"], 6.0)
        history = await self.db.get_price_history(tracked_id, limit=10)
        self.assertEqual(len(history), 2)

        # Tracking can be stopped and then nothing is checked anymore.
        await self.db.deactivate_tracked_product(tracked_id, self.user_id)
        self.assertEqual(await self.db.get_active_tracked_with_telegram(), [])
        self.assertEqual(await tracker.check_all(), [])
        self.assertEqual(len(notifier.calls), 2)

    async def test_manual_currency_change_invalidates_baseline(self):
        """Spec §30: after a currency switch the old baseline is dropped."""
        await self.db.add_tracked_product(
            self.user_id, "aliexpress", "1005002", "Товар",
            "https://www.aliexpress.com/item/1005002.html", 10.0, "UAH",
        )
        rows = await self.db.get_active_tracked_with_telegram()
        self.assertEqual(rows[0]["last_checked_price"], 10.0)

        await self.db.invalidate_tracking_baselines(self.user_id)

        rows = await self.db.get_active_tracked_with_telegram()
        self.assertIsNone(rows[0]["last_checked_price"])
        # initial_price keeps the original observation for display
        self.assertEqual(rows[0]["initial_price"], 10.0)

    async def test_join_carries_user_currency_and_notify_preference(self):
        """Spec §28/§36: the scheduler row must expose both columns."""
        await self.db.update_user_region(self.user_id, currency="EUR")
        await self.db.add_tracked_product(
            self.user_id, "aliexpress", "1005003", "Товар",
            "https://www.aliexpress.com/item/1005003.html", 10.0, "USD",
        )
        rows = await self.db.get_active_tracked_with_telegram()
        self.assertEqual(rows[0]["user_currency"], "EUR")
        self.assertEqual(rows[0]["notify_price_changes"], 1)

