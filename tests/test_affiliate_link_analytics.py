from keyboards.products import CB_PREFIX_BUY, LABEL_BUY, browse_card_keyboard
from models.product import Marketplace, Product
from services.analytics import EVENT_AFFILIATE_LINK_REQUESTED, EVENT_LABELS, KNOWN_EVENTS


def _product(*, affiliate_url=None):
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="123456",
        title="Test product",
        product_url="https://www.aliexpress.com/item/123456.html",
        affiliate_url=affiliate_url,
    )


def test_buy_button_is_callback_for_a_real_affiliate_url():
    keyboard = browse_card_keyboard("abc123", 0, 1, _product(affiliate_url="https://s.click.aliexpress.com/e/test"))
    button = keyboard.inline_keyboard[0][0]
    assert button.text == LABEL_BUY
    assert button.callback_data == f"{CB_PREFIX_BUY}:abc123:0"
    assert button.url is None


def test_buy_button_is_absent_without_affiliate_url():
    keyboard = browse_card_keyboard("abc123", 0, 1, _product())
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert all(button.text != LABEL_BUY for button in buttons)


def test_affiliate_link_request_is_a_distinct_honestly_labelled_event():
    assert EVENT_AFFILIATE_LINK_REQUESTED in KNOWN_EVENTS
    assert "не подтверждённые открытия" in EVENT_LABELS[EVENT_AFFILIATE_LINK_REQUESTED]

