from keyboards.products import (
    CB_PREFIX_FAVORITE_BUY,
    LABEL_BUY,
    favorites_list_keyboard,
)
from models.product import Marketplace, Product


def _product(*, affiliate_url=None):
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="123456",
        title="Test product",
        product_url="https://www.aliexpress.com/item/123456.html",
        affiliate_url=affiliate_url,
    )


def test_favorites_offer_buy_only_for_a_real_affiliate_url():
    product = _product(affiliate_url="https://s.click.aliexpress.com/e/test")
    keyboard = favorites_list_keyboard([{"index": 0, "product": product}])
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    buy = next(button for button in buttons if button.text == LABEL_BUY)
    assert buy.callback_data == f"{CB_PREFIX_FAVORITE_BUY}:aliexpress:123456"
    assert buy.url is None


def test_favorites_do_not_offer_buy_without_affiliate_url():
    keyboard = favorites_list_keyboard([{"index": 0, "product": _product()}])
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert all(button.text != LABEL_BUY for button in buttons)


def test_favorites_buy_callback_uses_stable_identity_not_list_index():
    product = _product(affiliate_url="https://s.click.aliexpress.com/e/test")
    keyboard = favorites_list_keyboard([{"index": 7, "product": product}])
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    buy = next(button for button in buttons if button.text == LABEL_BUY)
    assert buy.callback_data.endswith(":aliexpress:123456")
    assert ":7" not in buy.callback_data
