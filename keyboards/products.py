"""Product-card keyboards and callback-data helpers.

The user sees ONE product card at a time. For a single product the card
offers:

    🛒 Купить
    ❤️ В избранное | 🔗 Поделиться
    🔔 Следить за ценой | 🔕 Не отслеживать (once tracking is active)
    ◀️ Назад | ➡️ Следующий

``🛒 Купить`` exists only when the marketplace really returned an
affiliate link for this product: this project never sends the user to a
plain marketplace page behind a Buy button, and never invents an affiliate
URL. Buy is a URL button: one press opens the product's exact affiliate
URL right in the browser, with no bot round-trip and no extra message.
(The flip side of Telegram's design: the bot receives no signal about
URL-button presses, so a Buy click cannot be recorded as analytics.)

There is deliberately NO "🔍 Сравнить" button on cards: a comparison of
one product is meaningless, and the keyboard must stay small.

Callback data must stay under Telegram's 64-byte limit, so handlers keep
the current result set in memory under a short ``session_id`` and the
buttons reference ``(session_id, index)`` instead of real product ids.

The share button uses Telegram's own share link
(``https://t.me/share/url?url=...&text=...``, built by
``utils.formatting.build_share_url``): it opens the chat picker with the
product text and a working link pre-filled, and needs no bot inline mode.
No home-made deep link is invented.
"""

from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models.product import Product
from keyboards.limits import UrlBudget
from keyboards.main import CB_BACK, LABEL_HOME
from utils.formatting import (
    format_price,
    is_openable_url,
    share_button_url,
)

CB_PREFIX_FAVORITE = "pfav"
CB_PREFIX_TRACK = "ptrack"
CB_PREFIX_FAVORITE_REMOVE = "fdel"
CB_PREFIX_TRACK_STOP = "tstop"

#: ``ptun:<session_id>:<index>`` - stop tracking THIS product right from
#: the card (the tracking screen keeps its own ``tstop:<id>`` flow).
CB_PREFIX_CARD_UNTRACK = "ptun"

#: ``pnav:<session_id>:<index>`` - move to another product of the same
#: cached result set. The target index is absolute, so a button that was
#: left over from an earlier card can never move the user to a wrong item.
CB_PREFIX_NAV = "pnav"

LABEL_BUY = "\U0001F6D2 Купить"
LABEL_FAVORITE = "\u2764\uFE0F В избранное"
LABEL_SHARE = "\U0001F517 Поделиться"
LABEL_TRACK = "\U0001F514 Следить за ценой"
LABEL_UNTRACK = "\U0001F515 Не отслеживать"
LABEL_REMOVE = "\U0001F5D1 Удалить"
LABEL_STOP = "\u23F9 Остановить"
LABEL_CARD_BACK = "\u25C0\uFE0F Назад"
LABEL_CARD_NEXT = "\u27A1\uFE0F Следующий"
LABEL_CARD_RESTART = "\U0001F501 Сначала"

#: The 🤖 AI button exists ONLY on a product card (never in the main menu):
#: it opens the AI menu of the product the user is looking at.
LABEL_AI = "\U0001F916 AI"
LABEL_AI_ADVICE = "\u2B50 Стоит ли покупать?"
LABEL_AI_CHEAPER = "\U0001F4B0 Найти дешевле"
LABEL_AI_SIMILAR = "\U0001F50E Найти похожий"
LABEL_AI_BETTER = "\U0001F3AF Подобрать лучше"
LABEL_AI_QUESTION = "\U0001F4AC Задать свой вопрос"

#: ``pai:<session_id>:<index>`` - open the AI menu on the current card.
CB_PREFIX_AI = "pai"
#: ``paia:<action>:<session_id>:<index>`` - run one AI action.
CB_PREFIX_AI_ACTION = "paia"

#: The AI menu actions, in the order they are displayed. Every one of them
#: reasons only over the real ``Product`` the card shows - see
#: ``services.product_assistant``.
AI_ACTIONS: tuple[tuple[str, str], ...] = (
    ("advice", LABEL_AI_ADVICE),
    ("cheaper", LABEL_AI_CHEAPER),
    ("similar", LABEL_AI_SIMILAR),
    ("better", LABEL_AI_BETTER),
    ("question", LABEL_AI_QUESTION),
)

#: The card actions, in display order - asserted by tests. ``Купить`` is
#: absent for a product without an affiliate link (affiliate-only rule);
#: the tracking button toggles with the user's tracking state.
CARD_ACTION_LABELS = (LABEL_BUY, LABEL_FAVORITE, LABEL_TRACK, LABEL_SHARE)

#: The navigation row of a card, in display order.
CARD_NAV_LABELS = (LABEL_CARD_BACK, LABEL_CARD_NEXT)



def ai_menu_keyboard(
    session_id: str,
    index: int,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """The 🤖 AI menu of ONE product card (see ``handlers.ai_assistant``).

    Actions first, then navigation: ``◀️ Назад`` restores the product card
    itself (``pnav`` re-renders it, so the AI button comes back), and
    ``🏠 Главное меню`` is always available - the same two exits every
    other screen of this bot offers.
    """

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                label,
                callback_data=f"{CB_PREFIX_AI_ACTION}:{action}:{session_id}:{index}",
            )
        ]
        for action, label in AI_ACTIONS
    ]
    nav: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            LABEL_CARD_BACK,
            callback_data=back_callback or f"{CB_PREFIX_NAV}:{session_id}:{index}",
        )
    ]
    rows.append(nav)
    rows.append([InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)])
    return InlineKeyboardMarkup(rows)


def _short(text: str, limit: int = 28) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def browse_card_keyboard(
    session_id: str,
    index: int,
    count: int,
    product: Product,
    *,
    back_callback: str | None = None,
    tracking: bool = False,
) -> InlineKeyboardMarkup:
    """One-card browsing keyboard: actions + prev/next navigation.

    This is the keyboard of the single card a user browses through a whole
    feed (search / popular / category / photo). The same Telegram
    message is edited in place, so navigation callbacks carry the absolute
    target index: a stale button can never show the wrong product.

    ``🛒 Купить`` is a URL button that opens this product's exact
    affiliate URL right in the browser (one press, no bot round-trip),
    and it exists only when the provider really returned an affiliate
    URL (affiliate-only rule). The tracking row reflects the user's
    current state (``🔔`` to start, ``🔕`` to stop) inside this same
    card. ``◀️ Назад`` is always present: on any product but the first it
    moves to the previous product, and on the FIRST one it leaves the feed
    for the real screen the card was opened from - ``back_callback`` when
    the caller supplies one, the main menu otherwise (there is no summary
    screen to go back to).
    """

    rows: list[list[InlineKeyboardButton]] = []
    # Telegram rejects the WHOLE keyboard when the long URL buttons of one
    # message exceed a shared budget (see keyboards.limits): 🛒 Купить is
    # taken first, the share link is dropped first when the budget is tight.
    # Callback buttons are never budgeted.
    budget = UrlBudget()

    if (
        product.has_affiliate_url
        and is_openable_url(product.affiliate_url)
        and budget.try_take(product.affiliate_url)
    ):
        # URL button: one press opens this product's exact affiliate URL
        # in the browser. No bot round-trip, no re-sent card, no extra
        # message - and the URL is the one the API returned for exactly
        # this (session_id, index) product.
        #
        # ``is_openable_url`` is not cosmetic: Telegram refuses the WHOLE
        # message when one button URL is empty or relative ("Text buttons
        # are not allowed" / "URL host is empty", verified live), which
        # would hide the product entirely.
        rows.append(
            [
                InlineKeyboardButton(LABEL_BUY, url=product.affiliate_url)
            ]
        )

    action_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            LABEL_FAVORITE, callback_data=f"{CB_PREFIX_FAVORITE}:{session_id}:{index}"
        )
    ]
    share_url = share_button_url(product)
    if share_url is not None and budget.try_take(share_url):
        action_row.append(InlineKeyboardButton(LABEL_SHARE, url=share_url))
    rows.append(action_row)

    if tracking:
        rows.append(
            [
                InlineKeyboardButton(
                    LABEL_UNTRACK,
                    callback_data=f"{CB_PREFIX_CARD_UNTRACK}:{session_id}:{index}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    LABEL_TRACK,
                    callback_data=f"{CB_PREFIX_TRACK}:{session_id}:{index}",
                )
            ]
        )

    # §7: the AI menu lives on the card itself, right above navigation.
    rows.append(
        [
            InlineKeyboardButton(
                LABEL_AI, callback_data=f"{CB_PREFIX_AI}:{session_id}:{index}"
            )
        ]
    )

    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(
            InlineKeyboardButton(
                LABEL_CARD_BACK,
                callback_data=f"{CB_PREFIX_NAV}:{session_id}:{index - 1}",
            )
        )
    else:
        # First product of the feed: "Назад" leaves the feed for the real
        # screen the card was opened from - the caller's own screen when
        # it supplied one, the main menu otherwise.
        nav.append(
            InlineKeyboardButton(LABEL_CARD_BACK, callback_data=back_callback or CB_BACK)
        )
    if index < count - 1:
        nav.append(
            InlineKeyboardButton(
                LABEL_CARD_NEXT,
                callback_data=f"{CB_PREFIX_NAV}:{session_id}:{index + 1}",
            )
        )
    elif count > 1:
        nav.append(
            InlineKeyboardButton(
                LABEL_CARD_RESTART, callback_data=f"{CB_PREFIX_NAV}:{session_id}:0"
            )
        )
    rows.append(nav)

    # Spec: every product card must reach the main menu directly, even
    # when the card is a PHOTO message (the handler falls back to a
    # fresh message when Telegram cannot edit media into text).
    rows.append([InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)])

    return InlineKeyboardMarkup(rows)


def favorites_list_keyboard(items: Sequence[dict]) -> InlineKeyboardMarkup:
    """Favorites screen: open, share or remove a saved product.

    ``items`` entries are ``{"index": int, "product": Product}`` as
    prepared by the favorites handler.

    A button is emitted ONLY when its URL can really be opened. Telegram
    refuses an entire message when one inline button carries an empty or
    relative URL (``Text buttons are not allowed in the inline keyboard`` /
    ``URL host is empty`` - verified live against the Bot API), and a saved
    snapshot may genuinely have no link: it was stored by an older release,
    or the marketplace returned a product without one. Such an item simply
    gets no link buttons - it is still listed and can still be deleted,
    which is exactly why the screen must not fail.

    ``🗑 Удалить`` carries the PRODUCT IDENTITY (marketplace +
    product_id), never a list position: two favorites screens can be open
    at once and the older one's cached list is then stale, so an
    index-based button would delete whatever now sits at that position.
    An identity-based callback deletes exactly the product the button was
    rendered for - even after the cache was replaced, even after a bot
    restart (the cache is only consulted to enrich analytics).

    The LAST row is always ``🏠 Главное меню``: the favorites screen is a
    menu section, and without that row a non-empty list offered only «🔗
    Поделиться» (a URL, so no bot round-trip) and «🗑 Удалить» - the
    dialog could only be left by deleting saved products. A screen that
    cannot be closed is a dead end, so the way home is part of the list
    itself and matches the single button the empty state shows.
    """

    rows: list[list[InlineKeyboardButton]] = []
    # One shared long-URL budget for the WHOLE screen (see keyboards.limits):
    # the open links come first, the share link is dropped first, and the 🗑
    # / home rows are callback buttons that are never budgeted - a saved
    # product always stays visible and removable.
    budget = UrlBudget()
    for item in items:
        index = item["index"]
        product: Product = item["product"]
        marketplace = str(getattr(product.marketplace, "value", product.marketplace))

        url = product.best_url if is_openable_url(product.best_url) else None
        if url is not None and budget.try_take(url):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{index + 1}. {_short(product.title, 30)}", url=url
                    )
                ]
            )

        action_row: list[InlineKeyboardButton] = []
        share_url = share_button_url(product)
        if share_url is not None and budget.try_take(share_url):
            action_row.append(InlineKeyboardButton(LABEL_SHARE, url=share_url))
        action_row.append(
            InlineKeyboardButton(
                LABEL_REMOVE,
                callback_data=(
                    f"{CB_PREFIX_FAVORITE_REMOVE}:{marketplace}:{product.product_id}"
                ),
            )
        )
        rows.append(action_row)

    # §UX: a screen must always be closable. Same single way home the empty
    # favorites state shows, so both states of the section end the dialog the
    # same way - and deleting products is never the only exit.
    rows.append([InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)])

    return InlineKeyboardMarkup(rows)


def tracked_list_keyboard(items: Sequence[dict]) -> InlineKeyboardMarkup:
    """Price tracking screen: open the product or stop tracking it.

    ``items`` entries are
    ``{"tracked_id": int, "title": str, "url": str | None,
    "price": float | None, "currency": str | None}`` - plain data read
    from the ``tracked_products`` table.

    Like the favorites list, the price-tracking screen always ends with
    ``🏠 Главное меню``: a list a user can only leave by stopping a
    tracked product would be a dead end.
    """

    rows: list[list[InlineKeyboardButton]] = []
    # Same shared long-URL budget as the favorites list (keyboards.limits):
    # a tracked product whose open link no longer fits falls back to its
    # inert callback twin instead of breaking the whole screen; ⛔ and home
    # are callback buttons and are always rendered.
    budget = UrlBudget()
    for item in items:
        label = (
            f"{_short(item.get('title') or 'Товар', 24)} — "
            f"{format_price(item.get('price'), item.get('currency'))}"
        )

        openable = is_openable_url(item.get("url")) and budget.try_take(item.get("url"))
        row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(label, url=item["url"])
            if openable
            else InlineKeyboardButton(label, callback_data=f"noop:{item['tracked_id']}"),
            InlineKeyboardButton(
                LABEL_STOP,
                callback_data=f"{CB_PREFIX_TRACK_STOP}:{item['tracked_id']}",
            ),
        ]
        rows.append(row)

    rows.append([InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)])

    return InlineKeyboardMarkup(rows)
