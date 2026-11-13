"""Editorial thematic shelves, independent from the ordinary category tree.

All product identities are explicit title aliases and are still subject to
the shared principal-product gate. Budget tiers require a real USD price.
Thematic shelves use their own history scope and are not mixed into the
ordinary catalog's subcategory list or live 96-shelf report.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemeShelf:
    code: str
    title: str
    description: str
    category_code: str
    query_routes: tuple[str, ...]
    keywords: tuple[str, ...]
    product_types: tuple[str, ...]
    exclude_terms: tuple[str, ...] = ()
    max_price: float | None = None
    # English/Russian aliases for the same shopper-facing type share one bucket.
    product_type_groups: tuple[tuple[str, ...], ...] = ()


THEMATIC_COLLECTIONS: tuple[dict[str, str], ...] = (
    {
        "code": "unusual_devices",
        "title": "Необычные полезные устройства",
        "description": "Необычные механизмы и устройства, которые помогают решать повседневные задачи",
    },
    {
        "code": "smart_home",
        "title": "Умные решения для дома",
        "description": "Устройства автоматизации, датчики и управление домашними системами",
    },
    {
        "code": "little_known_gadgets",
        "title": "Гаджеты, о которых мало кто знает",
        "description": "Менее очевидные устройства и технологии с конкретным назначением",
    },
    {
        "code": "budget_finds",
        "title": "Полезные мелочи до определённого бюджета",
        "description": "Практичные небольшие товары с подтверждённой ценой в долларах",
    },
)


THEME_SHELVES: tuple[ThemeShelf, ...] = (
    ThemeShelf(
        "theme_unusual_devices", "Необычные полезные устройства",
        "Необычные механизмы и устройства для решения повседневных задач",
        "home",
        ("automatic jar opener", "electric bag sealer", "electric cleaning brush"),
        ("automatic jar opener", "electric bag sealer", "automatic toothpaste dispenser",
         "self stirring mug", "electric lint remover", "motion sensor night light",
         "portable door lock", "digital measuring spoon", "electric cleaning brush"),
        ("automatic jar opener", "electric bag sealer", "mini heat sealer",
         "automatic toothpaste dispenser", "toothpaste dispenser", "self stirring mug",
         "electric lint remover", "motion sensor light", "portable door lock",
         "digital measuring spoon", "electric cleaning brush", "automatic soap dispenser",
         "автоматическая открывалка", "запайщик пакетов", "мини запайщик",
         "автоматический дозатор зубной пасты", "самомешалка кружка",
         "электрическая щётка для уборки", "электрощетка для уборки",
         "автоматический дозатор мыла", "датчик движения светильник"),
        ("alcohol", "wine", "cigarette", "tobacco", "weapon", "ammunition",
         "лекарство", "табак", "сигарет", "оружие", "боеприпас"),
        product_type_groups=(
            ("automatic jar opener", "автоматическая открывалка"),
            ("electric bag sealer", "mini heat sealer", "запайщик пакетов", "мини запайщик"),
            ("automatic toothpaste dispenser", "toothpaste dispenser", "автоматический дозатор зубной пасты"),
            ("self stirring mug", "самомешалка кружка"),
            ("electric lint remover",),
            ("motion sensor light", "датчик движения светильник"),
            ("portable door lock",),
            ("digital measuring spoon",),
            ("electric cleaning brush", "электрическая щётка для уборки", "электрощетка для уборки"),
            ("automatic soap dispenser", "автоматический дозатор мыла"),
        ),
    ),
    ThemeShelf(
        "theme_smart_home", "Умные решения для дома",
        "Датчики, управление и автоматизация бытовых систем",
        "electronics",
        ("smart plug", "smart switch", "motion sensor"),
        ("smart plug", "smart switch", "motion sensor", "door sensor",
         "temperature humidity sensor", "smart home hub"),
        ("smart plug", "smart switch", "motion sensor", "door sensor",
         "window sensor", "water leak detector", "smart home hub",
         "smart thermostat", "smart curtain motor", "smart lighting controller",
         "wifi relay switch", "zigbee gateway", "smart radiator valve",
         "умная розетка", "умный выключатель", "датчик движения",
         "датчик открытия двери", "датчик протечки воды", "датчик температуры",
         "датчик влажности", "шлюз умного дома", "мотор для штор",
         "умный термостат", "контроллер освещения"),
        ("replacement part", "remote control only", "switch cover",
         "запасная часть", "пульт отдельно", "накладка выключателя"),
        product_type_groups=(
            ("smart plug", "умная розетка"),
            ("smart switch", "wifi relay switch", "умный выключатель"),
            ("motion sensor", "датчик движения"),
            ("door sensor", "window sensor", "датчик открытия двери"),
            ("water leak detector", "датчик протечки воды"),
            ("temperature humidity sensor", "датчик температуры", "датчик влажности"),
            ("smart home hub", "zigbee gateway", "шлюз умного дома"),
            ("smart thermostat", "умный термостат"),
            ("smart curtain motor", "мотор для штор"),
            ("smart lighting controller", "контроллер освещения"),
            ("smart radiator valve",),
        ),
    ),
    ThemeShelf(
        "theme_little_known_gadgets", "Гаджеты, о которых мало кто знает",
        "Нишевые устройства и технологии, которые редко встречаются в обычных подборках",
        "electronics",
        ("digital microscope", "usb tester", "endoscope camera"),
        ("digital microscope", "usb tester", "endoscope camera", "air quality monitor"),
        ("digital microscope", "usb tester", "usb power meter", "wifi endoscope camera",
         "endoscope camera", "thermal imaging camera", "infrared thermometer",
         "air quality monitor", "bluetooth item tracker", "portable label printer",
         "digital caliper", "laser distance meter", "smart tag tracker",
         "usb c tester", "digital microscope", "мини микроскоп цифровой",
         "тестер usb", "измеритель usb", "камера эндоскоп",
         "тепловизор", "инфракрасный термометр", "анализатор качества воздуха",
         "bluetooth трекер", "принтер этикеток", "цифровой штангенциркуль",
         "лазерный дальномер", "метка трекер"),
        ("phone case", "screen protector", "ordinary charging cable",
         "чехол телефона", "защитное стекло", "обычный кабель зарядки"),
        product_type_groups=(
            ("digital microscope", "мини микроскоп цифровой"),
            ("usb tester", "usb power meter", "usb c tester", "тестер usb", "измеритель usb"),
            ("wifi endoscope camera", "endoscope camera", "камера эндоскоп"),
            ("thermal imaging camera", "тепловизор"),
            ("infrared thermometer", "инфракрасный термометр"),
            ("air quality monitor", "анализатор качества воздуха"),
            ("bluetooth item tracker", "smart tag tracker", "bluetooth трекер", "метка трекер"),
            ("portable label printer", "принтер этикеток"),
            ("digital caliper", "цифровой штангенциркуль"),
            ("laser distance meter", "лазерный дальномер"),
        ),
    ),
    ThemeShelf(
        "theme_budget_5", "Полезные мелочи до $5",
        "Только товары с подтверждённой ценой не выше 5 USD",
        "home",
        ("phone stand", "cable organizer", "silicone cable tie"),
        ("phone stand", "cable organizer", "cleaning brush", "adhesive hook"),
        ("phone stand", "cable organizer", "silicone cable tie", "cleaning brush",
         "screen cleaning kit", "magnetic hook", "adhesive hook", "door stopper",
         "sink strainer", "measuring spoon", "keychain flashlight", "plant clips",
         "zipper repair kit", "eyeglass holder", "bag sealer", "travel bottle",
         "mini screwdriver set", "phone holder", "drawer organizer",
         "подставка для телефона", "органайзер для кабеля", "стяжка для кабеля",
         "щётка для уборки", "набор для чистки экрана", "магнитный крючок",
         "самоклеящийся крючок", "ограничитель двери", "сетка для раковины",
         "мерная ложка", "фонарик брелок", "клипсы для растений",
         "ремкомплект молнии", "держатель очков", "запайщик пакетов",
         "дорожный флакон", "набор мини отверток", "органайзер для ящика"),
        ("bundle of 100", "wholesale lot", "replacement part only",
         "оптовая партия", "набор 100 штук", "запасная часть"),
        5.0,
        product_type_groups=(
            ("phone stand", "phone holder", "подставка для телефона"),
            ("cable organizer", "органайзер для кабеля"),
            ("silicone cable tie", "стяжка для кабеля"),
            ("cleaning brush", "щётка для уборки"),
            ("screen cleaning kit", "набор для чистки экрана"),
            ("magnetic hook", "магнитный крючок"),
            ("adhesive hook", "самоклеящийся крючок"),
            ("door stopper", "ограничитель двери"),
            ("sink strainer", "сетка для раковины"),
            ("measuring spoon", "мерная ложка"),
            ("keychain flashlight", "фонарик брелок"),
            ("plant clips", "клипсы для растений"),
            ("zipper repair kit", "ремкомплект молнии"),
            ("eyeglass holder", "держатель очков"),
            ("bag sealer", "запайщик пакетов"),
            ("travel bottle", "дорожный флакон"),
            ("mini screwdriver set", "набор мини отверток"),
            ("drawer organizer", "органайзер для ящика"),
        ),
    ),
    ThemeShelf(
        "theme_budget_10", "Полезные мелочи до $10",
        "Только товары с подтверждённой ценой не выше 10 USD",
        "home",
        ("phone stand", "kitchen timer", "mini flashlight"),
        ("phone stand", "cable organizer", "kitchen timer", "travel organizer"),
        ("phone stand", "cable organizer", "kitchen timer", "travel organizer",
         "reusable lint roller", "silicone spatula", "foldable storage box",
         "led night light", "carabiner", "magnetic screwdriver", "mini flashlight",
         "sink organizer", "portable soap dispenser", "key holder",
         "подставка для телефона", "органайзер для кабеля", "кухонный таймер",
         "дорожный органайзер", "многоразовый ролик для одежды",
         "силиконовая лопатка", "складной контейнер", "ночник led",
         "карабин", "магнитная отвертка", "мини фонарь", "органайзер для раковины",
         "дорожный дозатор мыла", "ключница"),
        ("bundle of 100", "wholesale lot", "replacement part only",
         "оптовая партия", "набор 100 штук", "запасная часть"),
        10.0,
        product_type_groups=(
            ("phone stand", "подставка для телефона"),
            ("cable organizer", "органайзер для кабеля"),
            ("kitchen timer", "кухонный таймер"),
            ("travel organizer", "дорожный органайзер"),
            ("reusable lint roller", "многоразовый ролик для одежды"),
            ("silicone spatula", "силиконовая лопатка"),
            ("foldable storage box", "складной контейнер"),
            ("led night light", "ночник led"),
            ("carabiner", "карабин"),
            ("magnetic screwdriver", "магнитная отвертка"),
            ("mini flashlight", "мини фонарь"),
            ("sink organizer", "органайзер для раковины"),
            ("portable soap dispenser", "дорожный дозатор мыла"),
            ("key holder", "ключница"),
        ),
    ),
    ThemeShelf(
        "theme_budget_20", "Полезные мелочи до $20",
        "Только товары с подтверждённой ценой не выше 20 USD",
        "home",
        ("mini tool kit", "rechargeable night light", "usb desk fan"),
        ("mini tool kit", "portable organizer", "rechargeable night light"),
        ("mini tool kit", "rechargeable night light", "digital kitchen timer",
         "portable organizer", "electric lint remover", "usb desk fan",
         "motion sensor light", "compact measuring tool", "foldable phone stand",
         "набор мини инструментов", "аккумуляторный ночник", "цифровой кухонный таймер",
         "портативный органайзер", "электрическая машинка для удаления катышков",
         "настольный usb вентилятор", "светильник с датчиком движения",
         "компактный измерительный инструмент", "складная подставка для телефона"),
        ("bundle of 100", "wholesale lot", "replacement part only",
         "оптовая партия", "набор 100 штук", "запасная часть"),
        20.0,
        product_type_groups=(
            ("mini tool kit", "набор мини инструментов"),
            ("rechargeable night light", "аккумуляторный ночник"),
            ("digital kitchen timer", "цифровой кухонный таймер"),
            ("portable organizer", "портативный органайзер"),
            ("electric lint remover", "электрическая машинка для удаления катышков"),
            ("usb desk fan", "настольный usb вентилятор"),
            ("motion sensor light", "светильник с датчиком движения"),
            ("compact measuring tool", "компактный измерительный инструмент"),
            ("foldable phone stand", "складная подставка для телефона"),
        ),
    ),
)


BUDGET_TIERS: tuple[tuple[str, str], ...] = (
    ("theme_budget_5", "До $5"),
    ("theme_budget_10", "До $10"),
    ("theme_budget_20", "До $20"),
)


THEME_MAX_PRICE_BY_CODE: dict[str, float] = {
    shelf.code: shelf.max_price
    for shelf in THEME_SHELVES
    if shelf.max_price is not None
}
