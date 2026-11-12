"""Additional focused catalog subcategories.

The base catalog keeps its stable identifiers; this module adds narrower
buyer-facing shelves and explicit title evidence for deterministic validation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtraSubcategory:
    code: str
    category_code: str
    title: str
    description: str
    query_routes: tuple[str, ...]
    keywords: tuple[str, ...]
    product_types: tuple[str, ...]
    exclude_terms: tuple[str, ...] = ()


def _extra(code: str, category: str, title: str, description: str,
           routes: tuple[str, ...], keywords: tuple[str, ...],
           product_types: tuple[str, ...], exclude_terms: tuple[str, ...] = ()) -> ExtraSubcategory:
    return ExtraSubcategory(
        code, category, title, description, routes, keywords,
        product_types, exclude_terms,
    )


EXTRA_SUBCATEGORIES: tuple[ExtraSubcategory, ...] = (
    # Electronics: split the broad audio shelf into clear product types.
    _extra("headphones", "electronics", "Наушники", "Проводные и беспроводные наушники",
           ("wireless headphones earbuds", "over ear headphones", "in ear bluetooth earphones"),
           ("наушники", "гарнитура", "вкладыши"),
           ("headphones", "earbuds", "earphones", "наушники", "гарнитура")),
    _extra("bluetooth_speakers", "electronics", "Bluetooth-колонки", "Портативные и домашние Bluetooth-колонки",
           ("portable bluetooth speakers", "wireless bluetooth speaker", "waterproof bluetooth speaker"),
           ("bluetooth колонка", "портативная колонка", "беспроводная колонка"),
           ("bluetooth speaker", "portable speaker", "wireless speaker", "bluetooth колонка", "портативная колонка")),
    _extra("microphones", "electronics", "Микрофоны", "Микрофоны для компьютера, записи и стриминга",
           ("USB microphones", "wireless lavalier microphone", "desktop condenser microphone"),
           ("микрофон", "петличный микрофон", "usb микрофон"),
           ("microphone", "lavalier mic", "USB mic", "микрофон", "петличный микрофон")),
    # Home: focused shelves for the previously broad cleaning and lighting topics.
    _extra("robot_vacuums", "home", "Роботы-пылесосы", "Роботизированная уборка пола",
           ("robot vacuum cleaners", "robot vacuum mop", "robotic floor cleaner"),
           ("робот пылесос", "робот мойщик пола", "робот для уборки"),
           ("robot vacuum", "robotic vacuum", "робот пылесос", "робот мойщик пола"),
           ("drain", "pipe", "sewer", "plumbing", "fireplace", "ash bucket")),
    _extra("window_cleaners", "home", "Очистители окон", "Роботы и устройства для мытья окон и стекла",
           ("window cleaning robots", "electric window cleaner", "window vacuum cleaner"),
           ("мойщик окон", "робот мойщик окон", "очиститель стекол"),
           ("window cleaning robot", "window cleaner", "window vacuum", "glass cleaning robot", "мойщик окон", "робот мойщик окон"),
           ("drain", "pipe", "sewer", "plumbing", "toilet", "shower")),
    _extra("table_lamps", "home", "Настольные лампы", "Рабочие, прикроватные и дизайнерские настольные лампы",
           ("desk table lamps", "bedside table lamps", "modern designer desk lamp"),
           ("настольная лампа", "лампа на стол", "прикроватная лампа"),
           ("desk lamp", "table lamp", "bedside lamp", "reading lamp", "настольная лампа", "прикроватная лампа")),
    # Auto
    _extra("dash_cameras", "auto", "Видеорегистраторы", "Автомобильные видеорегистраторы и камеры записи",
           ("car dash cameras", "4k dashboard camera", "front rear dash cam"),
           ("видеорегистратор", "автомобильная камера записи", "автомобильный регистратор"),
           ("dash cam", "dashboard camera", "car DVR", "видеорегистратор")),
    _extra("tire_inflators", "auto", "Компрессоры и насосы", "Автомобильные компрессоры и насосы для шин",
           ("car tire inflators", "12v portable air compressor", "portable tire pump"),
           ("автокомпрессор", "насос для шин", "компрессор для шин"),
           ("tire inflator", "air compressor", "tire pump", "автокомпрессор", "насос для шин")),
    _extra("car_lighting", "auto", "Автомобильное освещение", "Фары, лампы и светодиодная подсветка автомобиля",
           ("automotive LED headlights", "car LED bulbs", "car interior ambient lights"),
           ("автомобильная лампа", "светодиодная фара", "подсветка салона"),
           ("car headlight", "automotive LED bulb", "car light", "автомобильная лампа", "светодиодная фара")),
    # Computers
    _extra("monitors", "computers", "Мониторы и дисплеи", "Компьютерные мониторы и портативные экраны",
           ("computer monitors", "portable monitors USB C", "gaming monitors"),
           ("монитор", "портативный монитор", "экран для компьютера"),
           ("computer monitor", "portable monitor", "gaming monitor", "монитор", "портативный монитор")),
    _extra("webcams", "computers", "Веб-камеры", "Камеры для видеосвязи, стриминга и конференций",
           ("USB webcams", "4k computer webcam", "web camera microphone"),
           ("вебкамера", "веб камера", "камера для компьютера"),
           ("webcam", "web camera", "computer camera", "вебкамера", "веб камера")),
    _extra("network_equipment", "computers", "Сетевое оборудование", "Роутеры, сетевые адаптеры и Wi-Fi оборудование",
           ("wifi routers", "network adapters ethernet", "wireless access points mesh wifi switches"),
           ("wifi роутер", "сетевой адаптер", "точка доступа"),
           ("wifi router", "wireless router", "network adapter", "ethernet adapter", "access point", "wifi repeater", "wifi extender", "mesh wifi", "network switch", "network card", "wifi роутер", "сетевой адаптер", "точка доступа", "усилитель wifi", "коммутатор сети", "сетевой адаптер ethernet")),
    # Games
    _extra("pc_controllers", "games", "Контроллеры для ПК", "Геймпады и джойстики для компьютерных игр",
           ("PC game controllers", "wired wireless PC gamepad", "computer joystick controller"),
           ("геймпад для пк", "контроллер для компьютера", "джойстик для пк"),
           ("PC gamepad", "PC controller", "computer gamepad", "wireless PC controller", "wired PC controller", "USB gamepad", "PC joystick", "геймпад для пк", "джойстик для пк", "беспроводной геймпад для пк"),
           ("router", "wifi", "remote control", "TV remote")),
    _extra("console_controllers", "games", "Контроллеры для приставок", "Геймпады для игровых консолей",
           ("PlayStation Xbox Nintendo Switch gamepad controllers", "wireless wired gamepad controllers", "console racing wheel arcade joystick"),
           ("геймпад для приставки", "контроллер playstation", "контроллер xbox"),
           ("PlayStation controller", "PS5 controller", "PS4 controller", "Xbox controller", "Switch controller", "Joy-Con controller", "console gamepad", "wireless gamepad", "wired gamepad", "gaming controller", "arcade joystick", "racing wheel", "flight stick", "геймпад для приставки", "беспроводной геймпад", "контроллер для ps5", "контроллер для xbox", "контроллер для switch", "игровой руль", "джойстик для приставки")),
    _extra("retro_gaming", "games", "Ретро-игры и приставки", "Портативные ретро-консоли и игровые устройства",
           ("retro handheld game consoles", "classic mini game console", "retro arcade game console"),
           ("ретро приставка", "портативная игровая консоль", "ретро игровая консоль"),
           ("retro game console", "handheld game console", "mini game console", "ретро приставка", "портативная игровая консоль")),
    # Clothing
    _extra("watches", "clothing", "Часы", "Наручные и электронные часы",
           ("wrist watches", "quartz wristwatch", "fashion watch men women"),
           ("наручные часы", "кварцевые часы", "часы наручные"),
           ("wristwatch", "wrist watch", "quartz watch", "наручные часы", "кварцевые часы")),
    _extra("jewelry", "clothing", "Украшения", "Бижутерия и украшения для повседневного образа",
           ("fashion jewelry", "necklaces earrings bracelets", "rings jewelry accessories"),
           ("украшения", "серьги", "колье", "браслет", "кольцо"),
           ("necklace", "earrings", "bracelet", "fashion ring", "серьги", "колье", "браслет", "кольцо")),
    _extra("hats_caps", "clothing", "Головные уборы", "Кепки, шапки, панамы и другие головные уборы",
           ("baseball caps hats", "beanies winter hats", "bucket hats"),
           ("кепка", "шапка", "панама", "бейсболка"),
           ("baseball cap", "beanie", "bucket hat", "hat", "кепка", "шапка", "панама")),
    # Beauty
    _extra("massage_devices", "beauty", "Массажёры", "Электрические и ручные массажёры для домашнего ухода",
           ("electric massage devices", "facial body massagers", "neck shoulder massager"),
           ("массажер", "массажёр", "массажер для лица"),
           ("massager", "massage device", "facial massager", "neck massager",
            "shoulder massager", "back massager", "foot massager", "leg massager",
            "hand massager", "eye massager", "scalp massager", "body massager",
            "massage gun", "massage chair", "facial massage roller",
            "face massage roller", "anti cellulite massager",
            "массажер", "массажёр", "массажер для шеи", "массажер для плеч",
            "массажер для спины", "массажер для ног", "массажер для рук",
            "массажный пистолет", "роликовый массажер")),
    _extra("beauty_appliances", "beauty", "Приборы для красоты", "Электрические приборы для домашнего ухода",
           ("beauty care appliances", "facial cleansing device", "skin care beauty machine"),
           ("прибор для ухода", "аппарат для лица", "очиститель кожи"),
           ("beauty device", "facial cleansing device", "skin care device", "аппарат для лица", "очиститель кожи")),
    _extra("epilators", "beauty", "Эпиляторы и триммеры", "Приборы для удаления и подравнивания волос",
           ("electric epilators", "hair removal trimmers", "body hair trimmer"),
           ("эпилятор", "триммер для тела", "прибор для эпиляции"),
           ("epilator", "hair removal device", "body trimmer", "эпилятор", "триммер для тела")),
    # Tools
    _extra("welding_equipment", "tools", "Сварочное оборудование", "Сварочные аппараты и принадлежности для сварки",
           ("welding machines", "MIG TIG MMA welding equipment", "welding accessories"),
           ("сварочный аппарат", "сварочный инвертор", "оборудование для сварки"),
           ("welding machine", "welding inverter", "MIG welder", "TIG welder",
            "MMA welder", "MIG welding machine", "TIG welding machine",
            "MMA welding machine", "plasma cutter", "multi process welder",
            "spot welder", "laser welder", "сварочный аппарат", "сварочный инвертор"),
           ("welding table", "welding helmet", "welding gloves", "dent puller",
            "car dent repair", "paintless dent repair")),
    _extra("work_lights", "tools", "Рабочее освещение", "Переносные фонари и светильники для мастерской",
           ("workshop LED work lights", "portable work lights", "rechargeable inspection lamp"),
           ("рабочий фонарь", "светильник для мастерской", "инспекционная лампа"),
           ("work light", "inspection light", "workshop lamp", "рабочий фонарь", "инспекционная лампа")),
    _extra("woodworking_tools", "tools", "Инструменты для дерева", "Инструменты для обработки и столярных работ",
           ("woodworking hand tools", "wood carving chisels planes", "woodworking clamps jigs measuring tools"),
           ("столярный инструмент", "инструмент для дерева", "резцы по дереву"),
           ("woodworking tool", "wood carving tool", "wood chisel", "wood carving knife", "woodworking clamp", "woodworking hand plane", "hand plane", "wood rasp", "wood file", "carving gouge", "wood router bit", "woodworking jig", "drill guide for wood", "столярный инструмент", "резцы по дереву", "стамеска по дереву", "нож для резьбы по дереву", "струбцина столярная", "рубанок", "рашпиль по дереву", "напильник по дереву", "фреза по дереву", "кондуктор для сверления дерева")),
    # Sport
    _extra("sportswear", "sport", "Спортивная одежда", "Одежда и экипировка для тренировок",
           ("sportswear workout clothes", "running training apparel", "gym clothing"),
           ("спортивная одежда", "одежда для тренировок", "одежда спортивная"),
           ("sportswear", "workout clothes", "running apparel", "спортивная одежда", "одежда для тренировок")),
    _extra("water_sports", "sport", "Водный спорт", "Аксессуары для плавания и водных видов спорта",
           ("swimming accessories", "snorkeling diving gear fins masks", "water sports life jackets dry bags swimming gear"),
           ("товары для плавания", "маска для плавания", "снаряжение для плавания"),
           ("swimming goggles", "snorkeling mask", "diving gear", "swim fins", "snorkel tube", "life jacket", "dry bag", "wetsuit", "swim cap", "swimming kickboard", "inflatable buoy", "underwater flashlight", "paddle board", "товары для плавания", "маска для плавания", "ласты для плавания", "трубка для плавания", "спасательный жилет", "гермомешок", "гидрокостюм", "шапочка для плавания", "доска для плавания", "буй для плавания", "подводный фонарь")),
    _extra("sports_electronics", "sport", "Спортивная электроника", "Спортивные часы, датчики и электронные трекеры",
           ("sports electronics trackers", "heart rate monitors chest straps sports watches", "bike computers GPS cycling sensors"),
           ("спортивный трекер", "пульсометр", "велокомпьютер"),
           ("heart rate monitor", "heart rate chest strap", "chest strap heart rate monitor", "heart rate sensor", "sports tracker", "fitness tracker", "GPS sports watch", "running watch", "smart sports watch", "bike computer", "cycling cadence sensor", "speed sensor", "cadence sensor", "pedometer", "датчик сердечного ритма", "нагрудный пульсометр", "нагрудный ремень пульсометра", "пульсометр", "спортивные часы", "фитнес трекер", "велокомпьютер", "датчик каденса", "датчик скорости велосипеда", "шагомер")),
    # Pets
    _extra("pet_beds", "pets", "Лежанки и домики", "Места отдыха и домики для домашних животных",
           ("pet beds houses", "cat dog beds", "pet caves sleeping mats"),
           ("лежанка для животных", "домик для кошки", "спальное место питомца"),
           ("pet bed", "cat bed", "dog bed", "pet house", "лежанка для животных", "домик для кошки")),
    _extra("pet_toys", "pets", "Игрушки для животных", "Игрушки и интерактивные развлечения для питомцев",
           ("interactive pet toys", "cat toys dog toys", "automatic pet toys"),
           ("игрушки для кошек", "игрушки для собак", "игрушки для питомцев"),
           ("cat toy", "dog toy", "interactive pet toy", "automatic pet toy", "игрушки для кошек", "игрушки для собак")),
    _extra("pet_training", "pets", "Дрессировка и обучение", "Аксессуары для обучения и безопасного поведения питомцев",
           ("pet training accessories", "dog training clickers whistles target sticks", "pet agility equipment treat pouches training aids"),
           ("кликер для дрессировки", "аксессуары для дрессировки", "дрессировка собак"),
           ("training clicker", "dog whistle", "training whistle", "target stick", "dog agility tunnel", "dog agility hurdle", "treat pouch", "training dummy", "dog training bell", "pet training mat", "кликер для дрессировки", "свисток для собак", "свисток для дрессировки", "мишень для дрессировки", "туннель для аджилити", "барьер для аджилити", "сумка для лакомств", "апортировочный снаряд", "колокольчик для дрессировки", "коврик для дрессировки")),
    # Kids
    _extra("baby_feeding", "kids", "Кормление малышей", "Бутылочки, поильники, посуда и аксессуары для кормления",
           ("baby feeding bottles", "baby cups plates utensils", "infant feeding accessories"),
           ("детская бутылочка", "поильник", "посуда для кормления"),
           ("baby bottle", "sippy cup", "baby feeding set", "детская бутылочка", "поильник")),
    _extra("baby_monitors", "kids", "Радионяни и видеоняни", "Устройства наблюдения и связи с ребёнком",
           ("baby monitors", "video baby monitor", "audio baby monitor"),
           ("радионяня", "видеоняня", "монитор для младенца"),
           ("baby monitor", "video baby monitor", "audio baby monitor", "радионяня", "видеоняня")),
    _extra("kids_transport", "kids", "Детский транспорт", "Самокаты, беговелы и аксессуары для детского транспорта",
           ("kids scooters balance bikes", "children ride on toys", "baby stroller accessories"),
           ("детский самокат", "беговел", "детский транспорт"),
           ("kids scooter", "balance bike", "ride on toy", "детский самокат", "беговел")),
    # Garden
    _extra("lawn_care", "garden", "Уход за газоном", "Инструменты и устройства для стрижки и ухода за газоном",
           ("lawn care tools", "grass trimmers lawn mowers", "lawn edging tools"),
           ("газонокосилка", "триммер для травы", "инструменты для газона"),
           ("lawn mower", "grass trimmer", "weed trimmer", "газонокосилка", "триммер для травы")),
    _extra("garden_power_tools", "garden", "Садовая техника", "Электрические инструменты для работы в саду",
           ("cordless garden power tools", "electric pruning shears", "garden chainsaw"),
           ("садовый электроинструмент", "электросекатор", "садовая цепная пила"),
           ("electric pruning shears", "garden chainsaw", "garden power tool", "электросекатор", "садовая цепная пила")),
    _extra("outdoor_furniture", "garden", "Садовая мебель", "Мебель и комфортные решения для отдыха на участке",
           ("outdoor garden furniture", "patio chairs tables benches", "garden swings loungers outdoor seating"),
           ("садовая мебель", "уличный стол", "садовое кресло"),
           ("garden furniture", "patio chair", "outdoor table", "garden bench", "outdoor sofa", "garden lounge chair", "patio set", "outdoor dining set", "садовая мебель", "уличный стол", "садовое кресло", "садовая скамейка", "уличный диван", "комплект мебели для террасы", "обеденный комплект для сада")),
)
