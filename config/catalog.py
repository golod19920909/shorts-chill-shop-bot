"""Static catalog used by the user interface: categories and currencies.

This module contains NO product data and NO marketplace knowledge. It is
only the fixed list of options the bot offers to the user:

- ``CATEGORIES``  - the compact category tree shown under " Категории".
  Each category carries an English ``query`` because marketplace search
  APIs work with search phrases, not with our internal category codes.
- ``CURRENCIES``  - currencies the user may prefer. There is deliberately
  no country selector: the bot cannot know the user's country, Telegram
  does not provide it, and the project forbids IP geolocation.

Everything here is plain data, so extending the catalog is a one-line
change and never requires touching handlers or services.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    """One user-facing category."""

    code: str
    title: str
    query: str
    keywords: tuple[str, ...] = ()


#: The initial category set. Deliberately compact - natural-language
#: search remains the primary discovery mechanism, categories are just a
#: second way in.
#:
#: Two fields have different jobs and are never mixed up:
#: * ``query``    - the phrase sent to AliExpress (English, because that is
#:   how the marketplace's own search index is written);
#: * ``keywords`` - Russian words the relevance filter (``services.relevance``)
#:   compares against the Russian product titles this project asks for.
#:   They must stay GENEROUS: a category whose keywords are too narrow would
#:   hide legitimate products. A wrong product (a religious cross inside
#:   "Электроника") matches none of them, which is exactly the point.
CATEGORIES: tuple[Category, ...] = (
    Category("electronics", "📱 Электроника", "consumer electronics gadgets",
             ("наушники", "смартфон", "кабель", "зарядка", "повербанк",
              "колонка", "гарнитура", "умные часы", "телефон", "адаптер",
              "мышь", "клавиатура", "роутер", "микрофон", "вебкамера")),
    Category("home", "🏠 Дом и быт", "home kitchen storage",
             ("дом", "кухня", "посуда", "быт", "хранение", "органайзер",
              "полотенце", "светильник", "термос", "подушка")),
    Category("auto", "🚗 Авто", "car accessories interior",
             ("авто", "машина", "видеорегистратор", "держатель", "камера",
              "прикуриватель", "коврик", "щитки", "амортизатор", "чехол")),
    Category("computers", "\U0001F4BB Компьютеры", "computer laptop accessories",
             ("ноутбук", "клавиатура", "монитор", "ssd", "мышь", "роутер",
              "вебкамера", "докстанция", "процессор", "флешка")),
    Category("games", "🎮 Игры и развлечения", "gaming accessories console",
             ("игры", "геймпад", "консоль", "джойстик", "руль", "стик",
              "кресло", "мышь", "наушники", "коврик")),
    Category("clothing", "\U0001F455 Одежда и аксессуары", "clothing accessories fashion",
             ("одежда", "футболка", "сумка", "часы", "джинсы", "куртка",
              "ремень", "шапка", "кошелёк", "кроссовки")),
    Category("beauty", "💄 Красота и уход", "beauty skincare cosmetics",
             ("косметика", "крем", "макияж", "маска", "шампунь", "помада",
              "тушь", "сыворотка", "расчёска", "бритва")),
    Category("tools", "🔧 Инструменты", "hand tools hardware",
             ("инструмент", "дрель", "отвёртка", "шуруповерт", "молоток",
              "пила", "ключ", "стремянка", "мультитул", "набор")),
    Category("sport", "🏋️ Спорт и отдых", "sport fitness outdoor",
             ("спорт", "фитнес", "туризм", "гантели", "скакалка", "палатка",
              "фонарь", "бутылка", "велосипед", "коврик")),
    Category("pets", "🐾 Для животных", "pet supplies accessories",
             ("зоотовары", "корм", "поводок", "когтеточка", "миска",
              "переноска", "наполнитель", "шлейка", "лежанка", "игрушка")),
    Category("kids", "👶 Для детей", "baby kids toys",
             ("игрушка", "коляска", "подгузники", "бутылочка", "развивающая",
              "пазл", "конструктор", "комбинезон", "радионяня", "детская")),
    Category("garden", "🌳 Сад и дача", "garden outdoor plants",
             ("сад", "дача", "теплица", "семена", "лейка", "секатор",
              "шланг", "газон", "удобрение", "фонарь")),
)

#: Categories that were removed by product decision and must not come back
#: through the API or the UI: the generic "gifts" bucket produced
#: irrelevant feeds (random novelties instead of a coherent category).
REMOVED_CATEGORY_CODES: frozenset[str] = frozenset({"gifts"})

#: Currencies offered as a user preference, in the EXACT display order of
#: the final currency screen (UAH first, per spec). Not a conversion
#: table - the bot never converts prices, it only passes the preference to
#: providers whose API documents that currency (see
#: ``services/marketplaces/aliexpress_client.py``).
CURRENCIES: tuple[str, ...] = (
    "UAH",
    "USD",
    "RUB",
    "BYN",
    "EUR",
    "KZT",
    "UZS",
    "PLN",
    "GBP",
)

#: Telegram flag emoji per currency, used on the currency screen buttons.
#: Insertion order of this dict IS ``CURRENCIES`` (both follow the spec's
#: screen layout), and every flag key is a supported currency code.
CURRENCY_FLAGS: dict[str, str] = {
    "UAH": "\U0001F1FA\U0001F1E6",  # 🇺🇦
    "USD": "\U0001F1FA\U0001F1F8",  # 🇺🇸
    "RUB": "\U0001F1F7\U0001F1FA",  # 🇷🇺
    "BYN": "\U0001F1E7\U0001F1FE",  # 🇧🇾
    "EUR": "\U0001F1EA\U0001F1FA",  # 🇪🇺
    "KZT": "\U0001F1F0\U0001F1FF",  # 🇰🇿
    "UZS": "\U0001F1FA\U0001F1FF",  # 🇺🇿
    "PLN": "\U0001F1F5\U0001F1F1",  # 🇵🇱
    "GBP": "\U0001F1EC\U0001F1E7",  # 🇬🇧
}

_CATEGORIES_BY_CODE = {category.code: category for category in CATEGORIES}


def get_category(code: str) -> Category | None:
    """Look up a category by its code, or ``None`` if unknown."""

    return _CATEGORIES_BY_CODE.get(code)


def is_supported_currency(code: str | None) -> bool:
    """Whether a currency code is offered by the catalog."""

    return bool(code) and code.upper() in CURRENCIES


def currency_label(code: str | None) -> str:
    """User-facing currency label; "not specified" when unset."""

    return code.upper() if code else "не указана"
