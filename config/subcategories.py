"""Curated subcategories for the Telegram visual catalog.

This is static catalog configuration only: no product data and no provider
calls. Each route tuple is bounded to three phrases. The routes are initial
retrieval hypotheses and must pass live AliExpress relevance checks before
being enabled by the shared SearchService.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Subcategory:
    code: str
    category_code: str
    title: str
    description: str
    query_routes: tuple[str, ...]
    keywords: tuple[str, ...]


CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "electronics": "Гаджеты, устройства и полезная электроника",
    "home": "Практичные вещи для дома, кухни и хранения",
    "auto": "Аксессуары и полезные устройства для автомобиля",
    "computers": "Периферия, комплектующие и аксессуары",
    "games": "Игровые устройства, аксессуары и развлечения",
    "clothing": "Одежда, обувь, сумки и аксессуары",
    "beauty": "Устройства и аксессуары для ухода",
    "tools": "Инструменты для ремонта, мастерской и дома",
    "sport": "Фитнес, туризм и активный отдых",
    "pets": "Всё необходимое для домашних питомцев",
    "kids": "Игрушки, развитие и полезные детские товары",
    "garden": "Всё для сада, участка и дачного отдыха",
}


def _item(code: str, category: str, title: str, description: str,
          routes: tuple[str, ...], keywords: tuple[str, ...]) -> Subcategory:
    return Subcategory(code, category, title, description, routes, keywords)


SUBCATEGORIES: tuple[Subcategory, ...] = (
    # Электроника
    _item("smart_home", "electronics", "Умный дом", "Автоматизация, датчики и управление",
          ("smart home devices", "smart home sensors", "smart switches plugs"),
          ("умный дом", "умная розетка", "датчик движения", "умный выключатель")),
    _item("portable_gadgets", "electronics", "Портативные гаджеты", "Компактные электронные устройства",
          ("portable electronic gadgets", "mini electronic devices", "handheld gadgets"),
          ("гаджет", "портативный", "мини устройство", "электронный")),
    _item("charging", "electronics", "Зарядка и питание", "Зарядные устройства и аккумуляторы",
          ("phone chargers power banks", "USB charging adapters", "portable battery packs"),
          ("зарядное устройство", "зарядка", "повербанк", "аккумулятор")),
    _item("audio", "electronics", "Аудиотехника", "Наушники, колонки и беспроводной звук",
          ("wireless headphones bluetooth speakers", "portable bluetooth speakers", "wireless earbuds"),
          ("наушники", "колонка", "гарнитура", "микрофон")),
    _item("cables_adapters", "electronics", "Кабели и адаптеры", "Кабели, переходники и USB-аксессуары",
          ("electronic cables adapters", "USB hubs adapters", "charging data cables"),
          ("кабель", "переходник", "адаптер", "разъём")),

    # Дом и быт
    _item("kitchen", "home", "Кухня", "Посуда и кухонные принадлежности",
          ("kitchen tools utensils", "kitchen gadgets accessories", "cookware kitchen organizers"),
          ("кухня", "кухонный", "посуда", "принадлежности")),
    _item("storage", "home", "Хранение и порядок", "Органайзеры, контейнеры и системы хранения",
          ("home storage organizers", "storage boxes containers", "closet drawer organizers"),
          ("органайзер", "хранение", "контейнер", "коробка", "полка")),
    _item("cleaning", "home", "Уборка и чистота", "Инвентарь и аксессуары для уборки",
          ("home cleaning tools", "cleaning brushes mops", "household cleaning accessories"),
          ("уборка", "щётка", "швабра", "салфетка", "чистка")),
    _item("home_lighting", "home", "Освещение", "Светильники и подсветка помещений",
          ("indoor home lighting", "LED room lights", "desk bedside lamps"),
          ("светильник", "лампа", "освещение", "подсветка")),
    _item("bathroom", "home", "Ванная комната", "Аксессуары и решения для ванной",
          ("bathroom accessories organizers", "bathroom storage", "shower accessories"),
          ("ванная", "душ", "держатель", "мыльница", "дозатор")),

    # Авто
    _item("car_interior", "auto", "Аксессуары в салон", "Органайзеры, держатели и полезные мелочи",
          ("car interior accessories", "car organizers holders", "car seat accessories"),
          ("салон", "держатель", "органайзер", "коврик")),
    _item("car_electronics", "auto", "Автоэлектроника", "Электронные устройства для автомобиля",
          ("car electronics devices", "automotive electronic accessories", "car diagnostic devices"),
          ("автоэлектроника", "видеорегистратор", "камера", "диагностика")),
    _item("car_charging", "auto", "Зарядка в автомобиль", "Автомобильные зарядки и адаптеры",
          ("car chargers USB adapters", "12V car power adapters", "wireless car chargers"),
          ("автомобильная зарядка", "прикуриватель", "зарядное устройство")),
    _item("car_care", "auto", "Уход за автомобилем", "Инструменты для ухода и очистки",
          ("car care cleaning accessories", "car detailing tools", "car wash accessories"),
          ("уход за автомобилем", "мойка", "полировка", "очистка")),
    _item("car_tools", "auto", "Инструменты для авто", "Инструменты для ремонта и обслуживания",
          ("automotive repair tools", "car mechanic hand tools", "vehicle maintenance tools"),
          ("автомобильный инструмент", "ремонт", "ключ", "съёмник")),

    # Компьютеры
    _item("keyboards_mice", "computers", "Клавиатуры и мыши", "Периферия для работы и игр",
          ("computer keyboards mice", "wireless keyboard mouse", "mechanical keyboards"),
          ("клавиатура", "мышь", "компьютерная мышь")),
    _item("laptop_accessories", "computers", "Аксессуары для ноутбуков", "Подставки, док-станции и дополнения",
          ("laptop accessories stands docks", "laptop docking stations", "notebook cooling stands"),
          ("ноутбук", "подставка", "докстанция", "охлаждение")),
    _item("usb_hubs", "computers", "USB-хабы и адаптеры", "Разветвители портов и переходники",
          ("USB hubs computer adapters", "USB C docking adapters", "computer port adapters"),
          ("usb хаб", "разветвитель", "переходник", "адаптер")),
    _item("pc_cooling", "computers", "Охлаждение компьютера", "Вентиляторы, кулеры и охлаждение",
          ("PC cooling fans", "computer CPU cooling", "laptop cooling accessories"),
          ("кулер", "вентилятор", "охлаждение", "радиатор")),
    _item("computer_components", "computers", "Комплектующие", "Компоненты для сборки и модернизации ПК",
          ("computer PC components", "desktop PC parts", "computer internal components"),
          ("комплектующие", "ssd", "оперативная память", "процессор")),

    # Игры и развлечения
    _item("console_accessories", "games", "Аксессуары для консолей", "Принадлежности для игровых приставок",
          ("gaming console accessories", "game console accessories", "console controller accessories"),
          ("консоль", "приставка", "аксессуар", "зарядная станция")),
    _item("controllers", "games", "Геймпады и контроллеры", "Устройства игрового управления",
          ("game controllers gamepads", "wireless gaming controllers", "joystick controllers"),
          ("геймпад", "джойстик", "контроллер", "руль")),
    _item("gaming_peripherals", "games", "Игровая периферия", "Клавиатуры, мыши, коврики и гарнитуры",
          ("gaming keyboard mouse headset", "gaming mouse pads", "gaming headsets"),
          ("игровая клавиатура", "игровая мышь", "гарнитура", "коврик")),
    _item("streaming", "games", "Стриминг и запись", "Микрофоны, свет и оборудование для трансляций",
          ("streaming equipment accessories", "USB microphones webcams", "streaming lights capture devices"),
          ("стриминг", "микрофон", "вебкамера", "свет")),
    _item("vr", "games", "VR-аксессуары", "Аксессуары для виртуальной реальности",
          ("virtual reality VR accessories", "VR headset accessories", "VR controllers accessories"),
          ("vr", "виртуальная реальность", "шлем", "контроллер")),

    # Одежда и аксессуары
    _item("mens_clothing", "clothing", "Мужская одежда", "Повседневная одежда и базовые вещи",
          ("mens clothing casual wear", "men shirts jackets pants", "mens everyday fashion"),
          ("мужская одежда", "футболка", "рубашка", "куртка")),
    _item("womens_clothing", "clothing", "Женская одежда", "Повседневная одежда и модные вещи",
          ("womens clothing casual wear", "women dresses tops jackets", "womens everyday fashion"),
          ("женская одежда", "платье", "блузка", "юбка")),
    _item("shoes", "clothing", "Обувь", "Обувь для повседневной носки и отдыха",
          ("casual shoes sneakers footwear", "womens shoes", "mens shoes"),
          ("обувь", "кроссовки", "ботинки", "сандалии")),
    _item("bags", "clothing", "Сумки и рюкзаки", "Сумки, рюкзаки и дорожные аксессуары",
          ("bags backpacks travel bags", "crossbody bags wallets", "everyday backpacks"),
          ("сумка", "рюкзак", "кошелёк", "дорожная сумка")),
    _item("fashion_accessories", "clothing", "Аксессуары", "Часы, ремни, украшения и дополнения",
          ("fashion accessories watches belts", "jewelry accessories", "hats scarves fashion"),
          ("аксессуар", "часы", "ремень", "украшение")),

    # Красота и уход
    _item("hair_care", "beauty", "Уход за волосами", "Инструменты и аксессуары для укладки",
          ("hair care styling tools", "hair brushes accessories", "hair styling devices"),
          ("волосы", "расчёска", "фен", "плойка")),
    _item("skin_care_tools", "beauty", "Уход за кожей", "Аксессуары и устройства для домашнего ухода",
          ("skin care beauty tools", "facial cleansing devices", "face care accessories"),
          ("уход за кожей", "массажёр", "очищение", "лицо")),
    _item("manicure", "beauty", "Маникюр и педикюр", "Инструменты и принадлежности для ногтей",
          ("manicure pedicure tools", "nail art accessories", "nail care equipment"),
          ("маникюр", "педикюр", "ногти", "фрезер")),
    _item("makeup_tools", "beauty", "Инструменты для макияжа", "Кисти, спонжи и аксессуары",
          ("makeup tools brushes sponges", "cosmetic makeup accessories", "makeup organizers"),
          ("макияж", "кисти", "спонж", "косметичка")),
    _item("personal_care", "beauty", "Личный уход", "Приборы и аксессуары повседневного ухода",
          ("personal care grooming devices", "electric grooming accessories", "personal hygiene tools"),
          ("бритва", "триммер", "уход", "гигиена")),

    # Инструменты
    _item("hand_tools", "tools", "Ручные инструменты", "Ключи, отвёртки, плоскогубцы и наборы",
          ("hand tools screwdriver wrench pliers", "mechanic hand tool sets", "precision hand tools"),
          ("инструмент", "отвёртка", "ключ", "плоскогубцы")),
    _item("power_tools", "tools", "Электроинструменты", "Инструменты для сверления, резки и обработки",
          ("power tools drills saws", "cordless power tools", "electric workshop tools"),
          ("дрель", "шуруповёрт", "пила", "шлифмашина")),
    _item("measuring_tools", "tools", "Измерительные приборы", "Уровни, рулетки, тестеры и приборы",
          ("measuring tools laser levels", "digital measuring instruments", "multimeters testers"),
          ("измерительный прибор", "уровень", "рулетка", "мультиметр")),
    _item("workshop_storage", "tools", "Мастерская и хранение", "Организация рабочего места и инструмента",
          ("tool storage workshop organizers", "tool boxes bags organizers", "workbench accessories"),
          ("хранение инструмента", "ящик", "органайзер", "мастерская")),
    _item("fasteners", "tools", "Крепёж и расходники", "Крепёжные элементы и материалы для ремонта",
          ("hardware fasteners screws bolts", "repair consumables hardware", "anchors nuts washers"),
          ("крепёж", "саморез", "винт", "болт")),

    # Спорт и отдых
    _item("fitness", "sport", "Фитнес и тренировки", "Инвентарь для домашних и спортивных тренировок",
          ("fitness training equipment", "home workout accessories", "strength training accessories"),
          ("фитнес", "тренировка", "гантели", "эспандер")),
    _item("camping", "sport", "Туризм и кемпинг", "Снаряжение для походов и отдыха на природе",
          ("camping hiking equipment", "outdoor camping accessories", "hiking gear"),
          ("туризм", "кемпинг", "палатка", "спальник")),
    _item("cycling", "sport", "Велосипедные аксессуары", "Оснащение и принадлежности для велосипеда",
          ("bicycle accessories", "bike repair tools accessories", "cycling lights bags"),
          ("велосипед", "велофонарь", "насос", "велосумка")),
    _item("fishing", "sport", "Рыбалка", "Снасти и принадлежности для рыбалки",
          ("fishing tackle accessories", "fishing tools equipment", "fishing storage gear"),
          ("рыбалка", "удочка", "катушка", "приманка")),
    _item("outdoor", "sport", "Активный отдых", "Аксессуары для прогулок и отдыха",
          ("outdoor recreation accessories", "outdoor sports gear", "travel outdoor accessories"),
          ("отдых", "активный отдых", "туристический", "прогулка")),

    # Для животных
    _item("dog_supplies", "pets", "Для собак", "Аксессуары для прогулок, игр и ухода",
          ("dog supplies accessories", "dog walking harness leash", "dog toys grooming"),
          ("собака", "поводок", "шлейка", "игрушка для собак")),
    _item("cat_supplies", "pets", "Для кошек", "Игрушки, когтеточки и аксессуары",
          ("cat supplies accessories", "cat toys scratching posts", "cat beds feeders"),
          ("кошка", "когтеточка", "игрушка для кошек", "лежанка")),
    _item("pet_feeding", "pets", "Кормушки и поилки", "Миски, автоматические кормушки и поилки",
          ("pet feeders water fountains", "automatic pet feeders", "pet bowls accessories"),
          ("кормушка", "поилка", "миска", "фонтанчик")),
    _item("pet_grooming", "pets", "Уход и гигиена", "Щётки и инструменты для ухода за питомцами",
          ("pet grooming hygiene supplies", "pet brushes nail clippers", "pet cleaning accessories"),
          ("уход за животными", "щётка", "когтерез", "гигиена")),
    _item("pet_travel", "pets", "Прогулки и перевозка", "Переноски, сумки и прогулочные аксессуары",
          ("pet carriers travel accessories", "pet walking accessories", "pet travel bags"),
          ("переноска", "сумка для животных", "поводок", "шлейка")),

    # Для детей
    _item("toys", "kids", "Игрушки", "Игрушки для игр и досуга",
          ("kids toys educational toys", "children play toys", "creative toys for kids"),
          ("игрушка", "детская игрушка", "кукла", "машинка")),
    _item("learning", "kids", "Развитие и обучение", "Развивающие игры и обучающие материалы",
          ("educational toys learning games", "kids learning activities", "early learning toys"),
          ("развивающий", "обучающий", "пазл", "алфавит")),
    _item("creative", "kids", "Творчество", "Наборы для рисования, лепки и рукоделия",
          ("kids art craft supplies", "children drawing painting sets", "creative activity kits"),
          ("творчество", "рисование", "лепка", "краски")),
    _item("kids_safety", "kids", "Детская безопасность", "Защитные аксессуары и товары для дома",
          ("baby child safety products", "childproof home safety", "baby safety accessories"),
          ("детская безопасность", "защита", "блокиратор", "замок")),
    _item("school", "kids", "Школьные принадлежности", "Канцелярия и полезные вещи для учёбы",
          ("school supplies stationery", "school bags pencil cases", "student desk accessories"),
          ("школьный", "канцелярия", "пенал", "рюкзак")),

    # Сад и дача
    _item("watering", "garden", "Полив и орошение", "Шланги, распылители и системы полива",
          ("garden watering irrigation", "garden hoses sprinklers", "drip irrigation systems"),
          ("полив", "шланг", "распылитель", "орошение")),
    _item("garden_tools", "garden", "Садовые инструменты", "Инструменты для ухода за участком",
          ("garden hand tools", "pruning shears garden tools", "gardening tool sets"),
          ("садовый инструмент", "секатор", "лопата", "грабли")),
    _item("plant_growing", "garden", "Выращивание растений", "Товары для рассады, теплиц и растений",
          ("plant growing gardening supplies", "greenhouse seedling accessories", "plant pots trays"),
          ("рассада", "теплица", "семена", "горшок")),
    _item("garden_lighting", "garden", "Освещение участка", "Уличные светильники и садовая подсветка",
          ("outdoor garden lighting", "solar garden lights", "landscape pathway lights"),
          ("садовое освещение", "уличный светильник", "солнечный фонарь")),
    _item("outdoor_leisure", "garden", "Отдых на даче", "Аксессуары для отдыха и комфорта на участке",
          ("garden outdoor leisure accessories", "patio camping garden accessories", "outdoor furniture accessories"),
          ("дача", "отдых", "гамак", "садовая мебель")),
)


_SUBCATEGORIES_BY_CODE = {item.code: item for item in SUBCATEGORIES}
_SUBCATEGORIES_BY_CATEGORY = {
    code: tuple(item for item in SUBCATEGORIES if item.category_code == code)
    for code in CATEGORY_DESCRIPTIONS
}


def get_subcategory(code: str) -> Subcategory | None:
    return _SUBCATEGORIES_BY_CODE.get(code)


def get_subcategories(category_code: str) -> tuple[Subcategory, ...]:
    return _SUBCATEGORIES_BY_CATEGORY.get(category_code, ())


def get_all_subcategories() -> tuple[Subcategory, ...]:
    return SUBCATEGORIES
