"""Static parent catalogue and currency preferences.

Parent categories are navigation domains; concrete buyer-facing shelves live
in config.subcategories. Domains without a validated live assortment remain
out of the visible keyboard until their shelves pass the live quality gate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    """One user-facing parent category."""

    code: str
    title: str
    query: str
    keywords: tuple[str, ...] = ()


CATEGORIES: tuple[Category, ...] = (
    Category("smartphones", "📱 Смартфоны и аксессуары", "smartphones mobile phones",
             ("смартфон", "телефон", "чехол для телефона", "защитное стекло")),
    Category("computers", "💻 Компьютеры и офис", "computer office equipment",
             ("ноутбук", "клавиатура", "монитор", "ssd", "мышь", "роутер",
              "вебкамера", "докстанция", "процессор", "принтер", "микрофон")),
    Category("electronics", "🔌 Электроника и гаджеты", "consumer electronics gadgets",
             ("наушники", "кабель", "зарядка", "повербанк", "колонка",
              "гарнитура", "умные часы", "адаптер", "умный дом", "трекер")),
    Category("home", "🏠 Дом, кухня и быт", "home kitchen household",
             ("дом", "кухня", "посуда", "быт", "хранение", "органайзер",
              "полотенце", "светильник", "термос", "подушка", "пылесос")),
    Category("tools", "🛠 Инструменты и ремонт", "tools repair workshop",
             ("инструмент", "дрель", "отвёртка", "шуруповерт", "молоток",
              "пила", "ключ", "мультитул", "сварочный аппарат")),
    Category("auto", "🚗 Авто и мото", "automotive motorcycle",
             ("авто", "машина", "видеорегистратор", "держатель", "камера",
              "прикуриватель", "коврик", "компрессор", "мото")),
    Category("sport", "🏕 Спорт, туризм и отдых", "sport fitness tourism camping",
             ("спорт", "фитнес", "туризм", "гантели", "палатка", "фонарь",
              "велосипед", "рыбалка", "треккинг")),
    Category("beauty", "💄 Красота и личный уход", "beauty personal care",
             ("косметика", "крем", "макияж", "маска", "шампунь", "помада",
              "тушь", "сыворотка", "расчёска", "бритва", "массажёр")),
    Category("kids", "👶 Детские товары", "baby kids products",
             ("коляска", "подгузники", "бутылочка", "радионяня",
              "детская безопасность", "школьный")),
    Category("pets", "🐾 Товары для животных", "pet supplies",
             ("зоотовары", "корм", "поводок", "когтеточка", "миска",
              "переноска", "наполнитель", "шлейка", "лежанка", "игрушка")),
    Category("clothing", "👕 Одежда и обувь", "clothing shoes bags",
             ("одежда", "футболка", "сумка", "джинсы", "куртка", "ремень",
              "шапка", "кошелёк", "кроссовки", "спортивная одежда")),
    Category("gaming", "🎮 Игровые устройства", "gaming consoles controllers VR",
             ("игра", "геймпад", "консоль", "джойстик", "руль", "vr")),
    Category("hobbies", "🧩 Игрушки, хобби и творчество", "toys hobbies crafts",
             ("игрушка", "конструктор", "творчество", "рисование",
              "моделирование", "настольная игра", "головоломка")),
    Category("watches", "⌚ Часы и украшения", "watches jewelry",
             ("наручные часы", "часы", "украшения", "серьги", "браслет", "кольцо")),
    Category("garden", "🌳 Сад и дача", "garden lawn irrigation",
             ("сад", "дача", "теплица", "семена", "лейка", "секатор",
              "шланг", "газон", "удобрение", "садовая техника")),
)

REMOVED_CATEGORY_CODES: frozenset[str] = frozenset({"gifts"})

CURRENCIES: tuple[str, ...] = (
    "UAH", "USD", "RUB", "BYN", "EUR", "KZT", "UZS", "PLN", "GBP",
)

CURRENCY_FLAGS: dict[str, str] = {
    "UAH": "\U0001F1FA\U0001F1E6",
    "USD": "\U0001F1FA\U0001F1F8",
    "RUB": "\U0001F1F7\U0001F1FA",
    "BYN": "\U0001F1E7\U0001F1FE",
    "EUR": "\U0001F1EA\U0001F1FA",
    "KZT": "\U0001F1F0\U0001F1FF",
    "UZS": "\U0001F1FA\U0001F1FF",
    "PLN": "\U0001F1F5\U0001F1F1",
    "GBP": "\U0001F1EC\U0001F1E7",
}

_CATEGORIES_BY_CODE = {category.code: category for category in CATEGORIES}


def get_category(code: str) -> Category | None:
    return _CATEGORIES_BY_CODE.get(code)


def is_supported_currency(code: str | None) -> bool:
    return bool(code) and code.upper() in CURRENCIES


def currency_label(code: str | None) -> str:
    return code.upper() if code else "не указана"
