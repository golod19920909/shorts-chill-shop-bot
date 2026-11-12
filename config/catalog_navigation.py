"""User-facing catalog navigation taxonomy.

This module is deliberately separate from config.catalog and
config.subcategories: it changes only the catalog menu hierarchy and labels.
The original Subcategory objects (and their search/category semantics) remain
untouched for Smart Search, photo search and other protected flows.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from config.subcategories import Subcategory, get_all_subcategories


@dataclass(frozen=True)
class CatalogCategory:
    code: str
    title: str
    query: str
    keywords: tuple[str, ...] = ()


CATEGORIES: tuple[CatalogCategory, ...] = (
    CatalogCategory("smartphones", "📱 Смартфоны и аксессуары", "smartphones mobile phones",
                    ("смартфон", "телефон", "чехол для телефона", "защитное стекло")),
    CatalogCategory("computers", "💻 Компьютеры и офис", "computer office equipment",
                    ("ноутбук", "клавиатура", "монитор", "ssd", "мышь", "роутер",
                     "вебкамера", "докстанция", "процессор", "принтер", "микрофон")),
    CatalogCategory("electronics", "🔌 Электроника и гаджеты", "consumer electronics gadgets",
                    ("наушники", "кабель", "зарядка", "повербанк", "колонка",
                     "гарнитура", "умные часы", "адаптер", "умный дом", "трекер")),
    CatalogCategory("home", "🏠 Дом, кухня и быт", "home kitchen household",
                    ("дом", "кухня", "посуда", "быт", "хранение", "органайзер",
                     "полотенце", "светильник", "термос", "подушка", "пылесос")),
    CatalogCategory("tools", "🛠 Инструменты и ремонт", "tools repair workshop",
                    ("инструмент", "дрель", "отвёртка", "шуруповерт", "молоток",
                     "пила", "ключ", "мультитул", "сварочный аппарат")),
    CatalogCategory("auto", "🚗 Авто и мото", "automotive motorcycle",
                    ("авто", "машина", "видеорегистратор", "держатель", "камера",
                     "прикуриватель", "коврик", "компрессор", "мото")),
    CatalogCategory("sport", "🏕 Спорт, туризм и отдых", "sport fitness tourism camping",
                    ("спорт", "фитнес", "туризм", "гантели", "палатка", "фонарь",
                     "велосипед", "рыбалка", "треккинг")),
    CatalogCategory("beauty", "💄 Красота и личный уход", "beauty personal care",
                    ("косметика", "крем", "макияж", "маска", "шампунь", "помада",
                     "тушь", "сыворотка", "расчёска", "бритва", "массажёр")),
    CatalogCategory("kids", "👶 Детские товары", "baby kids products",
                    ("коляска", "подгузники", "бутылочка", "радионяня",
                     "детская безопасность", "школьный")),
    CatalogCategory("pets", "🐾 Товары для животных", "pet supplies",
                    ("зоотовары", "корм", "поводок", "когтеточка", "миска",
                     "переноска", "наполнитель", "шлейка", "лежанка", "игрушка")),
    CatalogCategory("clothing", "👕 Одежда и обувь", "clothing shoes bags",
                    ("одежда", "футболка", "сумка", "джинсы", "куртка", "ремень",
                     "шапка", "кошелёк", "кроссовки", "спортивная одежда")),
    CatalogCategory("gaming", "🎮 Игры и игровые устройства", "gaming consoles controllers VR",
                    ("игра", "геймпад", "консоль", "джойстик", "руль", "vr")),
    CatalogCategory("hobbies", "🧩 Игрушки, хобби и творчество", "toys hobbies crafts",
                    ("игрушка", "конструктор", "творчество", "рисование",
                     "моделирование", "настольная игра", "головоломка")),
    CatalogCategory("watches", "⌚ Часы и украшения", "watches jewelry",
                    ("наручные часы", "часы", "украшения", "серьги", "браслет", "кольцо")),
    CatalogCategory("garden", "🌳 Сад и дача", "garden lawn irrigation",
                    ("сад", "дача", "теплица", "семена", "лейка", "секатор",
                     "шланг", "газон", "удобрение", "садовая техника")),
)

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "smartphones": "Смартфоны и аксессуары к ним",
    "computers": "Компьютерная техника, офис и оборудование для записи",
    "electronics": "Устройства, электроника и повседневные гаджеты",
    "home": "Практичные вещи для дома, кухни и быта",
    "tools": "Инструменты для ремонта, мастерской и дома",
    "auto": "Автоэлектроника, обслуживание и аксессуары",
    "sport": "Фитнес, туризм, кемпинг и спортивное оборудование",
    "beauty": "Устройства и аксессуары для красоты и личного ухода",
    "kids": "Детские товары, безопасность и обучение",
    "pets": "Товары и аксессуары для домашних питомцев",
    "clothing": "Одежда, обувь и повседневные аксессуары",
    "gaming": "Игровые устройства, контроллеры и VR",
    "hobbies": "Игрушки, конструкторы, хобби и творчество",
    "watches": "Наручные часы, украшения и бижутерия",
    "garden": "Садовые товары, техника и уход за участком",
}

SHELF_PARENT_BY_CODE: dict[str, str] = {
    "smart_home": "electronics",
    "portable_gadgets": "electronics",
    "charging": "electronics",
    "audio": "electronics",
    "cables_adapters": "electronics",
    "kitchen": "home",
    "storage": "home",
    "cleaning": "home",
    "home_lighting": "home",
    "bathroom": "home",
    "car_interior": "auto",
    "car_electronics": "auto",
    "car_charging": "auto",
    "car_care": "auto",
    "car_tools": "auto",
    "keyboards_mice": "computers",
    "laptop_accessories": "computers",
    "usb_hubs": "computers",
    "pc_cooling": "computers",
    "computer_components": "computers",
    "console_accessories": "gaming",
    "controllers": "gaming",
    "gaming_peripherals": "computers",
    "streaming": "computers",
    "vr": "gaming",
    "mens_clothing": "clothing",
    "womens_clothing": "clothing",
    "shoes": "clothing",
    "bags": "clothing",
    "fashion_accessories": "clothing",
    "hair_care": "beauty",
    "skin_care_tools": "beauty",
    "manicure": "beauty",
    "makeup_tools": "beauty",
    "personal_care": "beauty",
    "hand_tools": "tools",
    "power_tools": "tools",
    "measuring_tools": "tools",
    "workshop_storage": "tools",
    "fasteners": "tools",
    "fitness": "sport",
    "camping": "sport",
    "cycling": "sport",
    "fishing": "sport",
    "outdoor": "sport",
    "dog_supplies": "pets",
    "cat_supplies": "pets",
    "pet_feeding": "pets",
    "pet_grooming": "pets",
    "pet_travel": "pets",
    "toys": "hobbies",
    "learning": "kids",
    "creative": "hobbies",
    "kids_safety": "kids",
    "school": "kids",
    "watering": "garden",
    "garden_tools": "garden",
    "plant_growing": "garden",
    "garden_lighting": "garden",
    "outdoor_leisure": "garden",
    "headphones": "electronics",
    "bluetooth_speakers": "electronics",
    "microphones": "computers",
    "robot_vacuums": "home",
    "window_cleaners": "home",
    "table_lamps": "home",
    "dash_cameras": "auto",
    "tire_inflators": "auto",
    "car_lighting": "auto",
    "monitors": "computers",
    "webcams": "computers",
    "network_equipment": "computers",
    "pc_controllers": "gaming",
    "console_controllers": "gaming",
    "retro_gaming": "gaming",
    "watches": "watches",
    "jewelry": "watches",
    "hats_caps": "clothing",
    "massage_devices": "beauty",
    "beauty_appliances": "beauty",
    "epilators": "beauty",
    "welding_equipment": "tools",
    "work_lights": "tools",
    "woodworking_tools": "tools",
    "sportswear": "clothing",
    "water_sports": "sport",
    "sports_electronics": "sport",
    "pet_beds": "pets",
    "pet_toys": "pets",
    "pet_training": "pets",
    "baby_feeding": "kids",
    "baby_monitors": "kids",
    "kids_transport": "kids",
    "lawn_care": "garden",
    "garden_power_tools": "garden",
    "outdoor_furniture": "garden",
}

# Merged broad/duplicate shelves and shelves failing the latest live quality
# threshold are not displayed. Their original Subcategory objects remain
# available to the existing search code and historical callbacks.
DISABLED_UNTIL_LIVE_VALIDATION_CODES: frozenset[str] = frozenset({
    "kitchen", "bathroom", "car_interior", "car_electronics",
    "computer_components", "console_accessories", "vr", "hair_care",
    "personal_care", "power_tools", "workshop_storage", "fitness",
    "camping", "fishing", "kids_safety", "plant_growing",
    "outdoor_leisure", "beauty_appliances", "epilators", "water_sports",
    "baby_feeding", "streaming", "skin_care_tools",
})
HIDDEN_SUBCATEGORY_CODES: frozenset[str] = frozenset({
    "portable_gadgets", "audio", "table_lamps", "dash_cameras",
    "pc_controllers", "console_controllers", "microphones",
    "gaming_peripherals", "dog_supplies", "cat_supplies", "outdoor",
    "woodworking_tools", "outdoor_furniture",
}) | DISABLED_UNTIL_LIVE_VALIDATION_CODES

SHELF_TITLE_OVERRIDES: dict[str, str] = {
    "laptop_accessories": "Подставки и док-станции для ноутбуков",
    "car_care": "Автошампуни и полировка",
    "cycling": "Велофонари",
    "school": "Школьные пеналы",
    "garden_tools": "Секаторы и садовые инструменты",
    "sports_electronics": "Велокомпьютеры и пульсометры",
    "pet_training": "Кликеры для дрессировки",
    "network_equipment": "Роутеры и сетевые адаптеры",
    "garden_power_tools": "Электросекаторы",
}


def get_catalog_category(code: str) -> CatalogCategory | None:
    return next((item for item in CATEGORIES if item.code == code), None)


def get_catalog_subcategories(category_code: str) -> tuple[Subcategory, ...]:
    values: list[Subcategory] = []
    for item in get_all_subcategories():
        parent = SHELF_PARENT_BY_CODE.get(item.code, item.category_code)
        if parent != category_code or item.code in HIDDEN_SUBCATEGORY_CODES:
            continue
        values.append(
            replace(
                item,
                category_code=parent,
                title=SHELF_TITLE_OVERRIDES.get(item.code, item.title),
            )
        )
    return tuple(values)
