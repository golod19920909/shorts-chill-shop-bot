"""Real automatic price tracking for tracked products.

``bot/__init__.py`` wires this into the application JobQueue. Design
decisions (documented, nothing invented):

* Interval: ``Settings.tracking_interval_minutes``
  (``TRACKING_INTERVAL_MINUTES``). ``0`` disables automation - the UI
  then honestly reports manual tracking.
* Cycle: load active tracked rows -> fetch each product through the same
  provider used by search -> record every new price in ``price_history``
  -> notify on meaningful changes.
* Meaningful = moved by >= ``DEFAULT_MIN_CHANGE_PERCENT`` (1%). Smaller
  moves are recorded but stay silent (anti-spam threshold).
* First observation only sets the baseline: history is written, no
  notification.
* Identical API answers produce no history row and no notification.
* Unavailable/malformed data (``None`` product, ``None`` price) is
  skipped; the entry stays active for the next cycle. Each row is
  checked in its own try/except so one failure never breaks the batch.
* Notifications compare prices only inside ONE currency: a currency
  change (or an unknown currency) silently rebases the baseline and
  records a diagnostic - never a misleading percentage alert.
* The user's notification preference (`users.notify_price_changes`)
  is honoured: disabled means the observation is recorded but no
  message is sent.
* Notifications carry an inline "🛒 Купить" URL button (affiliate URL
  when real, plain product URL as the truthful fallback) instead of a
  raw link in the text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

#: Minimum relative price move (percent of the old price) that triggers
#: a notification. Smaller moves are recorded in history but stay silent.
DEFAULT_MIN_CHANGE_PERCENT = 1.0

#: Name of the repeating PTB job.
PRICE_TRACKING_JOB_NAME = "price_tracking"

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass(frozen=True)
class PriceChange:
    """One meaningful observed price movement of a tracked product."""

    tracked_product_id: int
    user_id: int
    telegram_id: int | None
    product_id: str
    title: str
    old_price: float | None
    new_price: float
    old_currency: str | None
    new_currency: str | None
    affiliate_url: str | None
    #: Plain marketplace URL - the truthful fallback for the inline
    #: "🛒 Купить" button when no affiliate URL exists.
    product_url: str | None = None

    @property
    def percent(self) -> float | None:
        """Relative change in percent, or ``None`` without a baseline.

        Never computed across currencies: a UAH→USD "change" is not a
        change, and such notifications are suppressed entirely upstream
        (see ``PriceTracker._check_one``).
        """

        if self.currency_changed:
            return None
        if self.old_price is None or self.old_price <= 0:
            return None
        return round((self.new_price - self.old_price) / self.old_price * 100, 1)

    @property
    def currency_changed(self) -> bool:
        return (
            self.old_currency is not None
            and self.new_currency is not None
            and self.old_currency != self.new_currency
        )

    @property
    def buy_url(self) -> str | None:
        """Affiliate URL when real, else the plain product page."""

        return self.affiliate_url or self.product_url or None


def _money(amount: float | None, currency: str | None) -> str:
    value = f"{amount:.2f}" if amount is not None else "?"
    return f"{value} {currency}" if currency else value


def format_notification(change: PriceChange) -> str:
    """Human-readable price alert. Plain text, only real data.

    Never renders a percentage when the currencies differ (spec: no
    misleading price-change claims) - such changes are not notified at
    all by the tracker; the guard here is defense in depth.
    """

    if change.old_price is not None and change.new_price < change.old_price:
        arrow = "📉"
        headline = "Цена снизилась"
    else:
        arrow = "📈"
        headline = "Цена изменилась"

    lines = [f"{arrow} {headline}", "", change.title or "Товар", ""]
    lines.append(f"Было: {_money(change.old_price, change.old_currency)}")
    lines.append(f"Стало: {_money(change.new_price, change.new_currency)}")
    percent = change.percent
    if percent is not None:
        lines.append(f"Изменение: {percent:+.1f}%")
    if change.currency_changed:
        # Defensive: the tracker suppresses cross-currency notifications,
        # but if one ever reaches this formatter no percentage is shown.
        lines = [
            "ℹ️ Изменение цены",
            "",
            change.title or "Товар",
            "",
            f"Было: {_money(change.old_price, change.old_currency)}",
            f"Стало: {_money(change.new_price, change.new_currency)}",
            f"Валюта изменилась: {change.old_currency} -> {change.new_currency}",
        ]
    return "\n".join(lines)


class PriceTracker:
    """Checks every active tracked product and reports real changes."""

    def __init__(
        self,
        database: Any,
        provider: Any,
        notifier: Notifier,
        *,
        min_change_percent: float = DEFAULT_MIN_CHANGE_PERCENT,
    ) -> None:
        self._database = database
        self._provider = provider
        self._notifier = notifier
        self._min_change_percent = min_change_percent

        #: Safe run diagnostics for the admin "Отслеживание" screen (§44):
        #: counters and timestamps only - no user data.
        self.last_run_at: str | None = None
        self.runs = 0
        self.rows_checked = 0
        self.changes_found = 0
        self.mismatches = 0
        self.notified = 0
        self.notify_skipped = 0
        self.row_errors = 0

    async def check_all(self) -> list[PriceChange]:
        """One full cycle. Returns the meaningful changes it detected."""

        from datetime import datetime, timezone

        rows = await self._database.get_active_tracked_with_telegram()
        changes: list[PriceChange] = []
        self.runs += 1
        self.rows_checked += len(rows)
        self.last_run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            try:
                change = await self._check_one(dict(row))
            except Exception:  # noqa: BLE001 - one bad row never stops the batch
                logger.exception("Price tracking failed for row %s", row.get("id"))
                self.row_errors += 1
                continue
            if change is None:
                continue
            # The user's notification preference is authoritative: the
            # observation is already recorded, only the message is skipped.
            if not int(row.get("notify_price_changes", 1) or 0):
                logger.info(
                    "Price change for row %s recorded; notification disabled "
                    "by the user",
                    row.get("id"),
                )
                self.notify_skipped += 1
                continue
            changes.append(change)
            await self._notify(change)
            self.notified += 1
        self.changes_found += len(changes)
        return changes

    async def _check_one(self, row: dict[str, Any]) -> PriceChange | None:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            return None

        # The scheduler obtains the price in the USER's persisted
        # currency; the provider forwards it only when the API
        # documents target_currency for this method.
        preferred_currency = str(row.get("user_currency") or "").strip() or None
        product = await self._provider.get_product(
            product_id, currency=preferred_currency
        )
        if product is None:
            # Unavailable right now (removed / rate limited / transient).
            # Keep the entry active; the next cycle retries. Nothing is
            # invented here.
            return None

        new_price = getattr(product, "price", None)
        if new_price is None:
            return None
        try:
            new_price = float(new_price)
        except (TypeError, ValueError):
            return None
        if new_price < 0:
            return None

        tracked_id = int(row.get("id"))
        new_currency = getattr(product, "currency", None) or row.get("currency")
        old_price = row.get("last_checked_price")
        old_currency = row.get("currency")
        if old_price is not None:
            try:
                old_price = float(old_price)
            except (TypeError, ValueError):
                old_price = None

        if old_price is None:
            # First observation: baseline only, silent.
            await self._database.record_price_history(
                tracked_id, new_price, currency=new_currency
            )
            await self._database.update_tracked_observation(
                tracked_id, new_price, new_currency
            )
            return None

        # CRITICAL (currency mismatch safety): numbers from different
        # currencies are NEVER compared. A mismatch (or an unknown
        # currency on either side) records the observation, REBASES the
        # baseline onto the new price+currency and sends NO
        # notification - no percentage, no "снизилась/выросла" claim.
        mismatch = (
            old_currency is not None
            and new_currency is not None
            and old_currency != new_currency
        )
        not_comparable = old_currency is None or new_currency is None
        if mismatch or not_comparable:
            await self._database.record_price_history(
                tracked_id, new_price, currency=new_currency
            )
            await self._database.update_tracked_observation(
                tracked_id, new_price, new_currency
            )
            await self._record_mismatch(
                row,
                reason=(
                    f"{old_currency}->{new_currency}" if mismatch else "unknown"
                ),
            )
            self.mismatches += 1
            logger.info(
                "Price observation for row %s not comparable (%s); baseline "
                "rebased to %s %s, no notification sent",
                tracked_id,
                "currency mismatch" if mismatch else "unknown currency",
                new_price,
                new_currency,
            )
            return None

        if abs(new_price - old_price) < 1e-9:
            # Identical answer: no history row, no notification.
            return None

        await self._database.record_price_history(
            tracked_id, new_price, currency=new_currency
        )
        await self._database.update_tracked_observation(
            tracked_id, new_price, new_currency
        )

        change = PriceChange(
            tracked_product_id=tracked_id,
            user_id=int(row.get("user_id") or 0),
            telegram_id=row.get("telegram_id"),
            product_id=product_id,
            title=str(row.get("title") or getattr(product, "title", "") or ""),
            old_price=old_price,
            new_price=new_price,
            old_currency=old_currency,
            new_currency=new_currency,
            affiliate_url=getattr(product, "affiliate_url", None) or None,
            product_url=str(row.get("product_url") or "") or None,
        )
        percent = change.percent
        if percent is None or abs(percent) < self._min_change_percent:
            return None  # recorded, but below the documented threshold
        return change

    async def _record_mismatch(self, row: dict[str, Any], *, reason: str) -> None:
        """Persist a currency-mismatch diagnostic (admin panel reads it)."""

        try:
            await self._database.record_event(
                "currency_mismatch",
                user_id=int(row.get("user_id") or 0) or None,
                product_id=str(row.get("product_id") or "") or None,
                metadata={"reason": reason},
            )
        except Exception:  # noqa: BLE001 - diagnostics never break a cycle
            logger.debug("Could not record currency_mismatch event", exc_info=True)

    async def _notify(self, change: PriceChange) -> None:
        telegram_id = change.telegram_id
        if telegram_id is None:
            logger.warning(
                "Price change for row %s has no telegram_id; skip notify",
                change.tracked_product_id,
            )
            return
        try:
            # Inline "🛒 Купить" button: affiliate URL preferred, plain
            # product URL as truthful fallback, no raw link in the text.
            await self._notifier(
                int(telegram_id), format_notification(change), change.buy_url
            )
        except Exception:  # noqa: BLE001 - a failed notification is logged only
            logger.exception(
                "Failed to deliver price notification for row %s",
                change.tracked_product_id,
            )


def build_price_tracker(
    database: Any, search_service: Any, bot: Any
) -> PriceTracker | None:
    """Wire the tracker to the first configured provider of the service.

    Returns ``None`` when no marketplace provider is configured - the
    bot then reports manual tracking instead of faking automation.
    """

    provider = None
    for candidate in getattr(search_service, "providers", None) or []:
        if getattr(candidate, "is_configured", False):
            provider = candidate
            break
    if provider is None:
        return None

    async def _notify(chat_id: int, text: str, buy_url: str | None = None) -> None:
        # Spec: concise notification + inline "🛒 Купить" button instead
        # of a raw URL in the text.
        reply_markup = None
        if buy_url:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛒 Купить", url=buy_url)]]
            )
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    return PriceTracker(database, provider, _notify)


async def price_tracking_job(context: Any) -> None:
    """PTB job callback. Never raises; never blocks polling."""

    tracker = context.application.bot_data.get("price_tracker")
    if tracker is None:
        return
    try:
        await tracker.check_all()
    except Exception:  # noqa: BLE001 - the scheduler must survive anything
        logger.exception("Price tracking cycle failed")


def schedule_price_tracking(
    application: Any,
    *,
    interval_minutes: int | None = None,
    first_delay_seconds: float = 30.0,
) -> bool:
    """Register the repeating price-tracking job.

    Returns ``True`` when scheduled. ``False`` (with a logged reason)
    when tracking is disabled via settings or the JobQueue extra is not
    installed - the UI then reports manual tracking, not fake automation.
    """

    job_queue = getattr(application, "job_queue", None)
    if interval_minutes is None:
        settings = application.bot_data.get("settings")
        interval_minutes = int(
            getattr(settings, "tracking_interval_minutes", 1440) or 0
        )
    if interval_minutes <= 0:
        logger.info("Automatic price tracking disabled (TRACKING_INTERVAL_MINUTES=0)")
        return False
    if job_queue is None:
        logger.warning(
            "JobQueue unavailable (install python-telegram-bot[job-queue]); "
            "automatic price tracking is OFF"
        )
        return False
    job_queue.run_repeating(
        price_tracking_job,
        interval=interval_minutes * 60,
        first=first_delay_seconds,
        name=PRICE_TRACKING_JOB_NAME,
    )
    logger.info("Automatic price tracking scheduled every %s min", interval_minutes)
    return True
