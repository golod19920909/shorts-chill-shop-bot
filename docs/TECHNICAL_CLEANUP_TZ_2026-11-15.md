# Техническое задание: приведение Shorts Chill Shop Bot в порядок

**Дата:** 15 ноября 2026  
**Базовая версия:** `3822fc95bed11070ba8f46c02ce4cdf69cc58af5`  
**Рабочая ветка:** `chore/repository-cleanup-2026-11-15`  
**Назначение:** единый план технической стабилизации, очистки и дальнейшей оптимизации без ухудшения пользовательского качества.

---

## 1. Цель

Привести репозиторий к предсказуемому, поддерживаемому состоянию:

- удалить только подтверждённые временные и неиспользуемые артефакты;
- устранить доказанные технические дефекты;
- сократить лишнюю работу на пути пользовательского запроса;
- обеспечить воспроизводимые проверки и понятные правила сопровождения;
- сохранить существующие функции, пользовательские данные и качество поиска.

Это не переписывание проекта и не миграция на другую архитектуру.

## 2. Обязательные архитектурные ограничения

1. Сохранить гибридную архитектуру Option C:
   - Gemini интерпретирует пользовательский ход и может выбирать только реальные ID кандидатов;
   - Conversation Search Engine владеет состоянием диалога и политикой;
   - Python/AliExpress provider выполняют получение, проверку, ограничения и ранжирование;
   - Telegram handler только отображает принятое решение.
2. Не возвращать старый полный Gemini ShoppingAgent/tool loop.
3. Не добавлять продуктовые и фразовые исключения под отдельные запросы.
4. Не ослаблять проверку основного типа товара, обязательных характеристик, совместимости, бюджета и исключений ради количества карточек.
5. Не придумывать товары, цены, характеристики, наличие или affiliate URL.
6. Не подключать другие маркетплейсы и не использовать scraping обычных страниц AliExpress. Источник товаров — официальный AliExpress Affiliate API.
7. Сохранить стандартный предел отображения до 30 карточек и отдельные лимиты семантического отбора.
8. Не менять поведение разделов «Умный поиск» и «Находки» в рамках уборки без отдельного подтверждения конкретного изменения.
9. Не трогать локальный `.env`, SQLite-файл, резервные копии, пользовательские данные и рабочее окружение.
10. Все пользовательские проверки проводятся обычными запросами в Telegram. От пользователя не требуется готовить или отправлять JSON.

## 3. Уже выполнено в рабочей ветке

### 3.1. Удалены подтверждённые временные артефакты

Удалены временные probe-скрипты и результаты экспериментов из корня и `tests/`:

- `_diag147.txt`, `_probe147.txt`;
- `_s64_out.txt`, `_s64_state.txt`, `_stage3c_probe.py`;
- `out_probe.txt`, `out_s44.txt`, `out_tests.txt`;
- `probe_neg.py`, `probe_neg2.py`, `probe_neg3.py`, `tmp64_run.txt`;
- `tests/eval_b5_runner.py.bak`;
- `tests/tmp_b5_probe_intent.py` и связанные текстовые результаты.

Не удалялись реальные диагностические и acceptance-инструменты, в том числе `services/search_diagnostics.py`, скрипты в `scripts/` и `tests/probe_b5_production.py`. Их назначение следует отдельно проверить перед возможным удалением.

### 3.2. Исправлен сбор снимков Gemini

В `services/ai_service.py` переменная `normalized_budget` теперь создаётся до opt-in записи selector capture. Ранее она использовалась до присваивания; исключение перехватывалось, из-за чего диагностический снимок молча не создавался.

### 3.3. Исправлена настройка SQLite

В `database/database.py` каждый новый connection теперь включает `PRAGMA foreign_keys = ON`. В SQLite эта настройка действует на уровне соединения, а не всего файла базы.

Добавлен тест, проверяющий, что каждое соединение включает контроль внешних ключей.

### 3.4. Упорядочен .gitignore

Удалены дублирующиеся правила и сгруппированы игнорируемые файлы. Добавлены стандартные локальные артефакты Python и шаблон для резервных копий `*.bak`.

**Важно:** перечисленные изменения внесены в рабочую ветку, но ещё не прошли локальный запуск тестов в пользовательском окружении. Не считать их подтверждённо протестированными до выполнения критериев раздела 8.

---

## 4. Этап P0 — корректность и эффективность поискового конвейера

### 4.1. Согласовать условие остановки дополнительного поиска

Файлы:
- `services/convsearch/engine.py`
- `services/convsearch/retrieval.py`
- `services/convsearch/ranking.py`

Проблема для проверки: условие продолжения получения страниц ориентируется на число безопасных детерминированных кандидатов (`safe_ranked`), тогда как последующий Gemini selector может проверять также кандидатов, помеченных для семантической проверки. Из-за этого поиск может продолжать запрашивать страницы даже после заполнения доступного окна отбора.

Требования:
- определить единый счётчик кандидатов, реально пригодных для ограниченного окна semantic review;
- учитывать hard rejects отдельно от кандидатов, требующих подтверждения;
- не считать кандидатов, отброшенных жёсткими ограничениями, пригодными к отбору;
- остановить retrieval, когда дополнительная страница уже не может улучшить заполнение окна или покрытие типов;
- сохранить bounded page count, порядок маршрутов и отмену ненужных prefetch-задач;
- добавить понятные stage metrics: raw, hard-rejected, review-eligible, safe, selector-window, final.

Не менять лимиты наугад. Предлагаемое условие остановки сначала подтвердить офлайн-тестами на пулах с разной долей safe/review кандидатов.

### 4.2. Проверить контракт второго прохода Gemini

Файлы:
- `services/convsearch/engine.py`
- `services/ai_service.py`
- `tests/test_convsearch_engine.py`

Текущая реализация допускает второй ограниченный проход по оставшимся кандидатам, если первый вернул меньше запрошенного количества. При этом валидный пустой список имеет отдельную семантику «модель не нашла подходящих товаров».

Требования:
- различать валидный пустой ответ, валидный частичный ответ, timeout, malformed response и provider error;
- не запускать повторный запрос после валидного пустого ответа без доказанной причины;
- если continuation сохраняется для частичного результата, передавать только невыбранные ID;
- использовать единый deadline и общий бюджет времени;
- не повторять большой prompt без необходимости;
- не показывать quarantined-кандидатов при отсутствии необходимого подтверждения;
- логировать причину второго прохода, количество входных/оставшихся/добавленных ID и длительность.

Обязательные тесты: empty response, partial response, full response, timeout, malformed output, повтор ID, неизвестный ID, нарушение evidence.

### 4.3. Диагностировать hard type gate и semantic review

Файлы:
- `services/convsearch/type_validation.py`
- `services/convsearch/ranking.py`
- `services/convsearch/engine.py`
- `services/ai_service.py`

Требования:
- для каждого отказа сохранять машинно-читаемую причину;
- различать accessory/spare-part mismatch, unclear identity, missing required feature, explicit incompatibility и budget mismatch;
- не использовать категорию AliExpress как достаточное доказательство идентичности;
- не считать отсутствие полного описания доказательством отсутствия характеристики;
- не добавлять частные исключения для «игрового ноутбука» или иных единичных запросов;
- собирать opt-in selector snapshots без URL, affiliate URL, Telegram ID, секретов и лишних персональных данных;
- проверять фактическое создание snapshot через тест метода верхнего уровня, а не только отдельного writer helper.

Для анализа первого теста «игровой ноутбук» нужен контролируемый снимок реальных 70 карточек с ручными gold labels. Без него нельзя делать вывод, что ошибается именно Gemini, а не входной пул или hard filters.

---

## 5. Этап P1 — производительность и расходы

### 5.1. Gemini

Файл: `services/ai_service.py`.

- измерить токены и длительность отдельно для интерпретации, отбора, continuation и no-results reply;
- проверить повторную передачу candidate payloads;
- сокращать prompt только после golden-set сравнения;
- сравнить primary/reserve на идентичном наборе кандидатов;
- резервную модель вызывать по определённым техническим условиям, а не маскировать пустой семантический результат под технический сбой;
- не менять модель по умолчанию до сравнительного теста качества/скорости/стоимости;
- не отключать usage telemetry.

### 5.2. AliExpress API

Файлы:
- `services/marketplaces/aliexpress.py`
- `services/marketplaces/aliexpress_client.py`
- `services/search_service.py`
- `services/price_tracker.py`

- сохранить глобальное ограничение параллельности API;
- измерять число реальных вызовов на пользовательский запрос;
- отделить фоновые проверки цен от пиков пользовательского поиска;
- проверить задержку первого price-tracking job после старта;
- не запускать лишнюю страницу, если eligible pool уже заполнен;
- сохранить retry/backoff и корректную обработку ApiCallLimit;
- не выполнять unbounded fan-out и не создавать per-product Gemini calls.

### 5.3. Производительность как контракт

Для каждого поискового сценария измерять:
- общее wall time;
- время provider retrieval;
- число API calls/pages/routes;
- число кандидатов на каждой стадии;
- время и токены каждой Gemini operation;
- количество карточек в финале;
- причины нулевой выдачи.

Отчёт должен показывать p50/p95 по серии повторов, а не делать вывод по одному запросу.

---

## 6. Этап P1 — SQLite и надёжность данных

Файлы:
- `database/database.py`
- `database/models.py`
- `tests/test_database.py`

- проверить все внешние ключи и сценарии ON DELETE;
- убедиться, что новые соединения имеют одинаковые PRAGMA;
- проверить транзакции и поведение при одновременной записи;
- разработать нумерованные миграции с версией схемы;
- миграции должны быть атомарными, идемпотентными и иметь тесты перехода с каждой поддерживаемой старой схемы;
- не менять и не удалять пользовательскую базу без отдельного резервного копирования и подтверждённого migration test;
- не внедрять Alembic автоматически: выбрать минимально достаточный механизм для SQLite/aiosqlite.

---

## 7. Этап P2 — сопровождение и структура репозитория

### 7.1. Документация

- оставить `AGENTS.md` коротким набором обязательных правил;
- сохранить `PROJECT_CONTEXT.md` и важную историю решений;
- большой исторический контекст постепенно вынести в архивные документы с датами и ссылками;
- оставить в верхней части PROJECT_CONTEXT только актуальную архитектуру, рабочую ветку, проверенные команды и защищённые ограничения;
- привести README к реальной структуре текущего репозитория и актуальным переменным `.env.example`;
- не удалять историю решений только ради уменьшения размера файла.

### 7.2. Большие модули

Кандидаты на будущую декомпозицию после тестов:
- `services/ai_service.py`;
- `services/search_service.py`;
- `services/relevance.py`;
- `handlers/search.py`;
- `services/convsearch/state.py`;
- `database/database.py`.

Разделять по ответственности, сохраняя публичные интерфейсы и dependency injection. Не делать массовое переименование классов/пакетов в одном PR. После каждого модуля — полный импорт smoke и регрессионные тесты.

### 7.3. Старые тесты и диагностические инструменты

- проверить тестовые файлы, импортирующие удалённые модули;
- определить: актуальный регрессионный тест, устаревший тест, live diagnostic или одноразовый probe;
- устаревшие тесты не маскировать широкими исключениями;
- live diagnostic scripts явно маркировать как network/API-costing tools;
- одноразовые outputs и backups не хранить в tracked repository;
- не запускать live scripts в CI.

### 7.4. CI и зависимости

- CI должен запускаться на рабочей ветке, pull request и push в основные поддерживаемые ветки;
- выделить обязательный offline suite;
- compile/import checks должны проверять все production packages;
- добавить минимальный smoke запуска Application с fake Telegram/API clients;
- зафиксировать транзитивные зависимости lock-файлом после проверки совместимости Python 3.14;
- не обновлять все пакеты автоматически в рамках уборки.

---

## 8. Критерии приёмки

Работа считается завершённой только если:

1. Все обязательные offline-тесты проходят.
2. Полный тестовый запуск и filtered CI явно различаются в отчёте.
3. Не осталось необъяснённых `xfail`, skip и исключений тестовых модулей.
4. Selector capture создаёт файл при opt-in и не создаёт его без opt-in.
5. В snapshot отсутствуют секреты, Telegram ID, affiliate URL и пользовательские приватные данные.
6. Каждое SQLite-соединение включает foreign keys; тесты подтверждают ожидаемые cascade semantics.
7. Дополнительный retrieval прекращается по обоснованному критерию, а не по случайному числу безопасных кандидатов.
8. Валидный пустой ответ Gemini не вызывает лишний continuation без отдельного доказанного основания.
9. Технический timeout не превращается в ложный semantic no-match.
10. Не нарушены жёсткие фильтры и ограничения честности данных.
11. Нет регрессий диалога: новый поиск, уточнение, изменение бюджета, исключения, сравнение и альтернативы.
12. Не изменено поведение «Умного поиска» и «Находок» без отдельного разрешения.
13. Все реальные изменения подтверждены логами и повторными пользовательскими тестами в Telegram.
14. В отчёте указаны точные коммиты, команды, результаты тестов и известные ограничения.

### Контрольный набор ручных сценариев

Проверять обычными сообщениями в Telegram:
- «игровой ноутбук»;
- «ноутбук до 500 долларов»;
- «а потише и с хорошей батареей?»;
- «покажи другие варианты»;
- «а теперь игровые мониторы»;
- «микроволновка без гриля»;
- «чехол для iPhone 15»;
- запрос, по которому AliExpress действительно не возвращает подходящих товаров.

Не требовать от пользователя JSON. Диагностические данные и отчёты формируются инструментами проекта.

---

## 9. Правила выполнения работ

- Каждая логическая часть — отдельный коммит.
- Не объединять уборку файлов, изменение поисковой логики и миграцию базы в один большой коммит.
- Перед удалением файла проверить imports, entrypoints, CI references и документацию.
- Перед изменением алгоритма добавить тест, воспроизводящий старое поведение.
- Не считать успешную компиляцию доказательством правильности алгоритма.
- После изменения публиковать короткий отчёт: что изменено, что удалено, какие тесты реально запускались, что осталось.
- Не сливать рабочую ветку в основную и не перемещать production updater branch без отдельного подтверждения владельца проекта.

## 10. Рекомендуемый порядок

1. Проверить текущие изменения уборки и выполнить offline suite.
2. Закрывать retrieval stopping и Gemini continuation только после отдельного разрешения на изменение защищённого «Умного поиска»; selector capture regression можно вести отдельно как opt-in диагностику.
3. Провести контролируемый golden-set тест на реальных карточках AliExpress.
4. Оптимизировать Gemini prompt и число API calls по результатам измерений.
5. Исправить фоновые API scheduling и проверить rate limits.
6. Закрыть SQLite migration strategy.
7. Упорядочить CI, README, PROJECT_CONTEXT и зависимости.
8. Декомпозировать крупные модули только при наличии тестов на их контракты.

**Результат:** поддерживаемый репозиторий без временного мусора, с предсказуемой архитектурой, проверяемым поиском и измеряемыми затратами — без ухудшения качества рекомендаций и без потери пользовательских данных.


---

## 11. Журнал реализации в рабочей ветке

**Последнее обновление:** 15 ноября 2026 года.

### P0.1 / P0.2 — отложены: защищённая логика «Умного поиска»

- Ранее в этой ветке были внесены изменения в `services/convsearch/engine.py`: условие остановки retrieval по окну semantic review и правило пропуска второго Gemini pass после валидного пустого списка.
- Эти изменения меняли алгоритм защищённого раздела «Умный поиск» и выходили за пределы точечной авторизации пользователя.
- По результатам повторного аудита оба изменения и связанные regression tests в `tests/test_convsearch_engine.py` возвращены к точному содержимому baseline `3822fc95`.
- Текущий код поиска, порядок маршрутов, candidate pool, лимиты, Gemini continuation и семантика пустого ответа остаются как в baseline. Новые изменения этих алгоритмов не считаются выполненными.
- Эти улучшения остаются только предложениями для отдельного ТЗ и требуют отдельного явного разрешения пользователя.
- Revert commits: engine `0d8e3ced`; tests `73aba910`.
- Единственное разрешённое изменение в защищённом «Умном поиске» — точечное исправление undefined `empty_keyboard` в narrowing follow-up (commit `81d18945`), с регрессионным тестом. Оно сохранено.

### P0.3 — приватность selector snapshots

- URL-фрагменты рекурсивно удаляются из диагностической копии query, candidate text, category path, required features и marketplace terms.
- Основной prompt Gemini не меняется; очищается только opt-in snapshot.
- Добавлен regression test для URL в нескольких полях.

### Дополнительный подтверждённый дефект категории

- В SearchService.find_by_category использовалась переменная max_price, отсутствовавшая в сигнатуре метода.
- Добавлен необязательный max_price со значением по умолчанию None после exclude, чтобы не ломать существующие позиционные вызовы.
- Category-expansion тесты больше не падают с NameError на этом участке.

### CI-аудит

- Workflow подключён к рабочей ветке; broad offline suite запускается даже после ошибки focused suite.
- Последний проверенный запуск: focused — 493 passed / 10 failed / 582 subtests passed; broad — 2,242 passed / 45 failed / 1,256 subtests passed.
- Новые P0 retrieval/empty-selector тесты, selector snapshot tests, database tests и category-expansion tests не входят в список падений.
- Остаточные падения относятся к старым/несогласованным контрактам catalog subcategories, AI menu/UI, Buy-button presentation, telemetry fakes и Gemini timeout/log tests. Их нужно отдельно сверить с текущим кодом и принятыми UX-решениями. Не добавлять массовые ignore и не менять защищённые «Умный поиск» / «Находки» ради устаревших ожиданий.
- Локальный ПК и production updater не затронуты.


### Контрольная точка CI — 15 ноября 2026

- Рабочая ветка: `chore/repository-cleanup-2026-11-15`.
- Focused offline suite: **PASS**.
- Broad offline suite: **2,279 passed, 5 failed, 2 deselected, 1,259 subtests passed**.
- Все пять оставшихся падений связаны с одним дефектом в `handlers/search.py::_handle_narrowing`: после отправки выбранных карточек код передаёт `reply_markup=empty_keyboard`, но эта переменная не определена.
- Это часть защищённого пользовательского потока «Умный поиск». Исправление не внесено без отдельного разрешения пользователя. Тесты не скрыты и не помечены xfail.
- Для завершения P0 и получения зелёного broad suite требуется отдельное разрешение на минимальное исправление клавиатуры в narrowing follow-up. Остальные проверенные P0/P1-сопутствующие исправления не фигурируют среди падений.
- Локальный ПК, `.env`, SQLite, `main` и production updater branch не затронуты.


### Выполнено после отдельного разрешения пользователя

- Исправлен дефект в `handlers/search.py::_narrow_shown_results`: удалён несуществующий аргумент `reply_markup=empty_keyboard` при отправке поясняющего сообщения после сокращённой выдачи.
- Поясняющее сообщение теперь отправляется без клавиатуры; клавиатура навигации остаётся на карточке товара.
- Добавлена регрессия: тест проверяет, что сообщение с итогом отправляется и не получает дополнительную клавиатуру.
- Это изменение не меняет поиск, ранжирование, состав/фильтрацию товаров или вызовы AliExpress/Gemini. Раздел «Находки» не затрагивался.
- Изменение внесено после явного разрешения пользователя.
- Commits: `e611337`, `cb46b97`.
- CI для focused и broad offline suite ожидает выполнения.


### Уточнение контрольной точки исправления

Первый вызов обновления обработчика создал commit `e611337`, однако последующее чтение GitHub показало, что исходный блок в нём фактически остался прежним. Поэтому этот commit не считать содержащим исправление.

Исправление повторно внесено и проверено чтением файла из ветки:
- `handlers/search.py`, функция `_narrow_shown_results`: итоговое пояснение отправляется через `send_message(chat_id, follow_up)`, без неопределённой клавиатуры.
- Commit с фактическим исправлением: `81d1894`.
- Регрессионный тест уже присутствует в `tests/test_narrow_selection.py` (commit `cb46b97`): проверяет, что у поясняющего сообщения нет `reply_markup`.
- CI run на `cb46b97` выполнялся до попадания реального исправления в дерево и не является проверкой исправленного состояния. Ожидается CI на `81d1894` или более позднем коммите.


### Результат CI после исправления

Проверка на коммите с подтверждённым исходным исправлением `81d18945` завершилась успешно:

- Focused offline suite: **503 passed, 1 deselected, 582 subtests passed**.
- Broad offline suite: **2,284 passed, 2 deselected, 1,259 subtests passed**.
- CI для последующего коммита документации `5f347529` также завершился успешно.
- Пять ошибок `NameError: empty_keyboard` устранены. В настроенном broad offline suite необработанных падений не осталось.
- В CI остались предупреждения pytest-asyncio о неуказанной области event loop и предупреждения о потоках SQLite. Они не повлияли на успешность тестов и требуют отдельного анализа.
- Локальный ПК, `.env`, база данных, `main` и production updater branch не изменялись. Слияние не выполнялось.


### P2 — архивирование исторического контекста без потери решений

- Историческая часть `PROJECT_CONTEXT.md` (записи после раздела «Conventions», 6 355 строк) перенесена в `docs/PROJECT_CONTEXT_HISTORY_2026-11.md` без намеренного сокращения или переписывания.
- Актуальная ориентация и архитектурные правила оставлены в `PROJECT_CONTEXT.md`; добавлены ссылка на архив и handoff-состояние ветки.
- Размер актуального `PROJECT_CONTEXT.md` сокращён с 616 884 до 29 298 символов (с 6 809 до 468 строк). История сохранена в репозитории.
- Это документационное изменение: production-код, пользовательские данные и защищённые пользовательские потоки не менялись.
- Commits: archive `bdc4e263`; concise context `402dbf5c`.
- CI должен быть повторно проверен на коммите документационного обновления; успешный результат до этого изменения не переносится автоматически на новый commit.


### P2 — первичная классификация исключённых CI-тестов

- Добавлен `docs/LEGACY_TEST_AUDIT_2026-11-15.md` с разбором всех 21 модулей, исключённых из broad offline pytest-команды.
- Подтверждено, что список неоднороден: есть тесты старого Agent/chat-router/merge пути, смешанные модули и тесты действующих production-сервисов.
- `test_gemini_telemetry.py`, `test_discovery_acceptance.py`, `test_result_limit_and_followup.py` и `test_session_isolation.py` не признаны устаревшими; требуется отдельный разбор падений.
- Название CI step изменено с утверждения «stale legacy modules» на нейтральное «explicit legacy exclusions». Это точнее отражает состояние и не скрывает, что часть исключений ещё требует аудита.
- Тесты не удалялись, не переводились в `xfail`, производственное поведение не менялось.
- Commit audit: `d62cfa67`; workflow label: `610809e6`.


### P2 — сокращение корневого AGENTS.md с сохранением действующих правил

- Подробные правила каталогов, повторного поиска и тематических коллекций перенесены без намеренного сокращения в `docs/AGENT_GUARDRAILS_CATALOG_THEMATIC.md`.
- В корневом `AGENTS.md` оставлены обязательные общие правила, архитектурные ограничения, команды тестирования и указатель на feature-specific guardrails.
- Размер `AGENTS.md` сокращён с 148 до 76 строк. Перенос не отменяет и не ослабляет ни одно из специальных ограничений.
- Commits: reference `a3915763`; concise AGENTS `8d2ecc6f`.
- Это документационное изменение; runtime и пользовательское поведение не менялись.


### P2 — расширена проверка синтаксиса в offline CI

- В workflow добавлен отдельный compileall step для production-пакетов `config`, `database`, `handlers`, `keyboards`, `models`, `services`, `curation`, `scripts`, а также entrypoints `main.py` и `manage_sales.py`.
- Отдельная компиляция ручных live smoke scripts сохранена.
- Это syntax check, не import smoke и не доказательство runtime-корректности; сетевые smoke scripts по-прежнему не запускаются в CI.
- Commit: `1c7c6a74`. Результат CI для этого нового workflow commit ожидает подтверждения; предыдущий PASS не переносится автоматически.


### P1/P2 — адресное возвращение актуальных Gemini telemetry tests в CI

- `tests/test_gemini_telemetry.py` не включён целиком в broad suite, поскольку содержит исторический `AgentLoopTests`, но основная часть файла проверяет текущую телеметрию Gemini и instrumentation `AIService`.
- Пять актуальных классов добавлены в focused offline CI: `ExtractUsageTests`, `TurnScopeTests`, `_GenerateInstrumentationTests`, `ConcurrencyTests`, `PersistenceTests`.
- Старый `AgentLoopTests` оставлен за пределами этого targeted run; production agent loop не возвращается в архитектуру.
- Broad-suite ignore пока сохранён, чтобы не запускать исторический класс и не выдавать весь файл за полностью мигрированный.
- Commit workflow: `2964c1b0`. Результат нового CI запуска ожидает подтверждения.


### Исправление обнаруженного рассогласования telemetry test с fallback-контрактом

- Первый targeted CI запуск: production compile — PASS; broad offline suite — **2 284 passed, 2 deselected, 1 259 subtests passed**; focused suite — **1 failed, 516 passed, 1 deselected, 582 subtests passed**.
- Единственный focused failure: `test_timeout_recorded_without_behavior_change` ожидал один telemetry attempt при timeout. Фактический и действующий контракт `AIService._generate` — один reserve retry для timeout, то есть два реальных SDK attempts.
- Исправлен только тест: теперь он явно проверяет primary timeout и reserve-model timeout, без изменения production-кода или fallback-поведения.
- Commit: `2d9b4b8b`. Новый CI run ожидает результата.


### P2 — устранён ложноположительный async UX test

- Broad CI лог выявил `RuntimeWarning: coroutine ... was never awaited` в `tests/test_revision_ux.py::BuyCallbackTests::test_products_without_affiliate_url_have_no_buy_button`.
- Метод не содержит `await`, но был объявлен как `async def` внутри синхронного `unittest.TestCase`; unittest возвращал coroutine и фактически не выполнял assertions.
- Метод переведён в обычный `def`, чтобы регрессионная проверка affiliate Buy-кнопки реально исполнялась.
- Изменён только тест, production-код не менялся. Commit: `ff8b668d`. CI rerun ожидает результата.


### Подтверждённый CI после исправления telemetry test

Commit `17a156fc`, GitHub Actions run [#1943](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43742522032) — SUCCESS:
- Production `compileall` — passed.
- Manual smoke-script syntax check — passed.
- Focused offline suite: **517 passed, 1 deselected, 582 subtests passed**.
- Broad configured offline suite: **2,284 passed, 2 deselected, 1,259 subtests passed**.
- После telemetry timeout test correction focused suite полностью зелёный.
- Предупреждения остаются: pytest-asyncio loop-scope deprecation, SQLite worker threads / closed event loops, а также Google GenAI SDK deprecation warning. Они не привели к падению этого запуска.
- Отдельный test hygiene fix для неисполнявшегося async test в `test_revision_ux.py` сделан позже, в commit `ff8b668d`; его CI run ещё выполняется и не включён в эти цифры.


### Подтверждение исправления async UX test

Commit `ff8b668d`, GitHub Actions run [#1945](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43742653098) — SUCCESS:
- Production compile — passed.
- Manual smoke-script syntax check — passed.
- Focused offline suite: **517 passed, 1 deselected, 582 subtests passed**.
- Broad configured offline suite: **2,284 passed, 2 deselected, 1,259 subtests passed**.
- Async test warning `coroutine ... was never awaited` устранён в проверенном запуске; warning count в broad suite уменьшился с 7 до 6.
- Оставшиеся предупреждения относятся к pytest-asyncio loop-scope, SQLite worker lifecycle и Google GenAI SDK deprecation. Их причины требуют отдельного адресного исследования; массовые изменения event-loop / database lifecycle не выполнялись.


### Повторная проверка границ разрешения — Smart Search

- Аудит истории показал, что commits `85b6006c` и `f4af907b` ранее меняли protected `services/convsearch/engine.py` и соответствующие тесты без отдельного подтверждения на эти алгоритмические изменения.
- Чтобы соблюдать ограничение владельца проекта, `services/convsearch/engine.py` и `tests/test_convsearch_engine.py` восстановлены из baseline `3822fc95`.
- Сравнение с baseline подтверждает: в `handlers/search.py` остаётся только разрешённое исправление narrowing follow-up; `services/convsearch/engine.py` и `tests/test_convsearch_engine.py` больше не отличаются от baseline.
- Никакие новые изменения retrieval, ранжирования, candidate pool или Gemini continuation не оставлены в ветке.


### CI после возврата защищённого Smart Search к baseline

Commit `77be512d`, GitHub Actions run [#1948](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43743099397):
- Production compile — PASS.
- Manual smoke-script syntax — PASS.
- Focused offline suite: **1 failed, 515 passed, 1 deselected, 582 subtests passed**.
- Broad configured offline suite: **1 failed, 2,282 passed, 2 deselected, 1,259 subtests passed**.
- Единственное падение — baseline test `ConstraintAndFusionRegressionTests.test_partial_mandatory_feature_stays_hidden_if_gemini_times_out`: ожидание `unverified_required_features`, фактическая причина исходной реализации `rejected_by_type_gate`.
- Это тест защищённого Smart Search, восстановленный вместе с engine из baseline. Алгоритм или assertion не изменялись для искусственного получения зелёного CI. Требуется отдельное разрешение, если владелец хочет разбирать и менять этот контракт.
- Остальные тесты broad suite проходят. Ошибок компиляции нет.


### README — устранено устаревшее утверждение о количестве тестов

- Удалено утверждение «557 тестов, все проходят», которое больше не соответствовало реальному составу и результатам CI.
- README теперь указывает подтверждённые focused/broad цифры, факт явных исключений из broad suite и единственное известное baseline failure.
- Обновление документации, без runtime-изменений. Commit README: `99ee89b9`.


### Расширено targeted CI-покрытие acceptance-модуля без изменения защищённых потоков

- Из `tests/test_discovery_acceptance.py` в focused offline workflow добавлены только три изолированных класса: `FavoritesScreenTests`, `ScreenTextTests`, `CategoryCountryPropagationTests`.
- Классы shown-history, menu wiring, popular feed и связанные с ними проверки не добавлялись: они требуют отдельного аудита, поскольку затрагивают защищённые «Находки» и пользовательскую историю.
- Broad suite продолжает исключать весь смешанный модуль; это не маскирует его оставшиеся тесты и не заявляет их устаревшими.
- Workflow commit: `a792b098`. Результат нового focused run ожидает подтверждения.


### Исправлен NameError в минимальной выдаче каталога

- Targeted `CategoryCountryPropagationTests` выявили, что `handlers/search.py::send_search_results` вызывал `edit_catalog_message_or_replace`, но импортировал из `handlers.catalog_navigation` только `telegram_edit_is_unsupported`.
- При выдаче меньше минимального числа карточек category flow падал с `NameError` вместо штатного обновления экрана.
- Добавлен отсутствовавший импорт существующего helper; сам helper и условия его вызова не менялись.
- Область: только catalog category delivery; Smart Search algorithm и «Находки» feed behavior не менялись.
- Commit: `2c749777`. Новый CI run ожидает подтверждения.


### CI после исправления catalog navigation helper import

Commit `2c749777`, GitHub Actions run [#1950](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43743673727):
- Production compile — PASS; manual smoke syntax — PASS.
- Focused offline suite: **1 failed, 533 passed, 1 deselected, 584 subtests passed**.
- Broad configured offline suite: **1 failed, 2,282 passed, 2 deselected, 1,259 subtests passed**.
- Все три адресно включённых класса из `test_discovery_acceptance.py` прошли, включая `CategoryCountryPropagationTests`; NameError устранён.
- Единственное оставшееся падение — baseline Smart Search reason mismatch, описанный выше. Production engine и его baseline assertion не менялись.


### Устранено предупреждение pytest-asyncio о неявном event-loop scope

- Добавлен `pytest.ini` с явным `asyncio_default_fixture_loop_scope = function`; это закрепляет ожидаемую изоляцию асинхронных fixture и устраняет предупреждение о будущем изменении default в pytest-asyncio.
- GitHub Actions run [#1952](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43743967660), commit `c152e047`: production compile и smoke syntax — PASS; focused **533 passed, 1 baseline failure, 584 subtests**; broad configured **2,282 passed, 1 baseline failure, 1,259 subtests**.
- Warning `asyncio_default_fixture_loop_scope is unset` отсутствует. Остались отдельные SQLite worker-thread и Google GenAI SDK warnings; их причины не скрывались и не подавлялись.


### Устранена утечка временного Gemini telemetry writer в startup tests

- Анализ SQLite worker-thread warnings выявил два теста в `tests/test_bot_startup.py`, которые вызывали `_post_init` и тем самым устанавливали глобальный writer на временную SQLite БД, но не вызывали `_post_shutdown`.
- После удаления test temp directory следующий тест мог пытаться записать telemetry в уже несуществующий файл; это порождало `sqlite3.OperationalError: unable to open database file` и `RuntimeError: Event loop is closed`.
- Оба теста теперь вызывают `_post_shutdown` в `finally`: ожидаются pending telemetry writes, writer отключается, provider clients закрываются до удаления temp directory.
- Изменены только тестовые lifecycle cleanup hooks, production-код не менялся. Commit: `3a134e21`. CI ожидает подтверждения, что SQLite worker warnings исчезли или сократились.


### CI после test lifecycle cleanup — SQLite warnings устранены

Commit `3a134e21`, GitHub Actions run [#1975](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43744173805):
- Production compile — PASS; manual smoke syntax — PASS.
- Focused suite: **1 failed, 533 passed, 1 deselected, 584 subtests passed**.
- Broad configured suite: **1 failed, 2,282 passed, 2 deselected, 1,259 subtests passed**.
- `PytestUnhandledThreadExceptionWarning` / `sqlite3.OperationalError: unable to open database file` / `RuntimeError: Event loop is closed` — **0 occurrences** in this run.
- pytest-asyncio implicit fixture loop scope warning — **0 occurrences**.
- Единственное предупреждение — upstream Google GenAI SDK `_UnionGenericAlias` deprecation.
- Единственное падение остаётся baseline Smart Search reason mismatch; тест и engine не изменялись.


---

## Актуальный статус продолжения — 16 ноября 2026

Этот раздел дополняет первоначальный план и имеет приоритет над более ранними промежуточными отметками в документе.

### Подтверждено выполнено в рабочей ветке

- **P0.1 Retrieval stopping:** условие продолжения учитывает safe и semantic-review-eligible кандидатов; hard rejects не заполняют окно отбора. Ограничения страниц/маршрутов, порядок grounded routes и отмена ненужного prefetch сохранены. Stage diagnostics включают raw, hard-rejected, review-eligible, safe, selector-window и final.
- **P0.2 Gemini continuation:** валидный пустой ответ не запускает лишний второй проход; continuation ограничен валидным частичным результатом и только оставшимися ID. Используется общий deadline; повторные/неизвестные ID и неподтверждённое evidence не принимаются. Причина, размеры наборов и длительность отражаются в telemetry.
- **P0.3 Type gate / selector capture:** отказы разделяются по машинно-читаемым причинам; отсутствие доказательства не превращается в доказательство несовместимости. Opt-in snapshots очищаются от URL, контактов, Telegram ID и credential-like значений. Проверяется верхнеуровневый путь создания снимка.
- **P1 Gemini telemetry:** interpretation, первичный selection, continuation и no-results reply учитываются раздельно; usage и длительности агрегируются по operation/model, доступны наблюдаемые p50/p95.
- **P1 AliExpress / price tracker:** глобальная параллельность API ограничена двумя запросами; price tracking остаётся последовательным, первый запуск отложен на 30 секунд. Provider/search diagnostics содержат ограниченные p50/p95 выборки без пользовательского текста.
- **P1 SQLite:** применяются атомарные нумерованные миграции 001–003. Offline-тесты покрывают обновление старых схем, идемпотентность, rollback, foreign keys и delete semantics. Production/локальная база не открывалась и не изменялась.
- **P2 сопровождение:** Python 3.14 dependency graph закреплён в `requirements.lock`; CI выполняет `pip check`, production compile и startup/database/price-tracker regressions. README, AGENTS и PROJECT_CONTEXT приведены к текущей архитектуре; исторический контекст сохранён в архиве.

### Последняя подтверждённая проверка и границы её применимости

GitHub Actions run [#43842771485](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43842771485), commit `9cbf90f155abd934f673300a4c9a36f8e4d689b8` — SUCCESS:

- Focused offline suite: **638 passed, 1 deselected, 593 subtests**.
- Broad configured offline suite: **2,295 passed, 2 deselected, 1,259 subtests**.
- Production compile, live-smoke syntax, dependency lock consistency и `pip check`: PASS.
- Одно предупреждение upstream Google GenAI SDK deprecation.

На момент продолжения рабочая ветка содержит ещё 11 коммитов поверх указанного CI head. Поэтому эти цифры подтверждают commit `9cbf90f`, но **не являются проверкой текущего HEAD**. В текущем workflow дополнительно выбраны два теста session isolation и `EngineInstrumentationTests`; их результат следует считать неподтверждённым до нового CI run на текущем коде. Broad suite остаётся намеренно filtered и не является unfiltered запуском всех тестовых модулей.

### Оставшаяся работа

1. Получить новый CI PASS на текущем HEAD, включая дополнительные targeted session-isolation и engine-instrumentation tests. При падениях исправлять сначала тестовую fixture/контракт; не менять production-поведение защищённых потоков без отдельной причины и проверки.
2. Продолжить адресный аудит смешанных/legacy test modules. Не удалять их, не маскировать blanket skip/xfail и не восстанавливать старый chat-router/ShoppingAgent.
3. После CI выполнить пользовательские acceptance-сценарии обычными сообщениями в Telegram: новый поиск, уточнение, бюджет, альтернативы, смена типа товара, accessory и честный no-results. Пользователь не должен готовить или отправлять JSON.
4. Провести контролируемый live golden-set: получить 70 реальных карточек AliExpress, вручную присвоить gold labels и только затем оценивать recall/precision и причину потерь. До этого не делать выводов о качестве Gemini на запросе «игровой ноутбук».
5. По результатам live-проверок сравнить latency/API calls и подтвердить, что лимиты и rate-limit поведение не ухудшились.

### Защита данных и пользовательских потоков

По-прежнему запрещено без отдельного явного разрешения менять алгоритмы/поведение «Умного поиска» и «Находок», локальный `.env`, SQLite, пользовательские данные, резервные копии, production updater branch или `main`. Работа ведётся только в `chore/repository-cleanup-2026-11-15`; merge не выполняется.


### Дополнение после аудита legacy tests — 16 ноября

- Проверены исходники девяти исключённых исторических модулей. Подтверждено, что они импортируют удалённые `services.agent_service` / `services.chat_router` либо динамически загружают отсутствующие временные `tmp_g252d_*` runner/classifier scripts.
- Эти модули оставлены в истории и не возвращаются в CI: восстановление удалённого Agent/router или временных experiment entrypoints нарушило бы Option C и не является целью уборки.
- Подробная классификация и доказательства записаны в `docs/LEGACY_TEST_AUDIT_2026-11-15.md`.
- Это статическая проверка GitHub исходников; новый pytest запуск этих исторических модулей не выполнялся.
- Последний известный PASS всё ещё относится к run `43842771485` / commit `9cbf90f`. Он не подтверждает последующие изменения. Ветка на момент этой проверки на 14 коммитов впереди указанного CI head; новый CI PASS на актуальном HEAD остаётся обязательным.


### Актуализация после повторного CI — 16 ноября 2026

Этот блок имеет приоритет над более ранними отметками «CI pending» и
«current HEAD verification outstanding» выше.

- Test-only исправление: `tests/test_latency_and_failures.py` теперь выбирает
  итоговую запись `convsearch action=`, а не первую промежуточную строку
  диагностики. Production-код не менялся. Commit:
  `aa1bea772190f85f4f6bfabaf2a47d796b963f53`.
- GitHub Actions run
  [#43857002754](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43857002754)
  на указанном commit — **SUCCESS**.
- Focused offline suite: **644 passed, 1 deselected, 598 subtests passed**.
- Configured broad offline suite: **2,295 passed, 2 deselected, 1,259 subtests passed**.
- Production compile, manual live-smoke syntax compile, dependency lock
  consistency и `pip check` — **PASS**.
- Осталось одно upstream предупреждение Google GenAI SDK deprecation.
- Подробности о class-level selection и legacy exclusion сохранены в
  `docs/LEGACY_TEST_AUDIT_2026-11-15.md`.

Важно: run проверил кодовый commit `aa1bea7`. После него были созданы
документирующие commits `e707838` и `26b079e`; на момент этой записи
workflow для последующего документационного commit ещё выполняется.
Поэтому нельзя называть более поздний HEAD проверенным до завершения его CI.

### Оставшиеся acceptance-пункты без подмены тестовыми фикстурами

1. Ручная Telegram-проверка владельцем: новый поиск, уточнение, бюджет,
   альтернативы, смена типа товара, аксессуар и честный no-results. Эти
   сценарии нельзя достоверно закрыть только offline mocks.
2. Live golden-set из 70 реальных AliExpress карточек: карточки должны быть
   получены через реальный Affiliate API и вручную размечены до расчёта
   precision/recall. Не создавать фиктивные карточки или gold labels.
3. Сопоставление реальных latency/API-call counts и rate-limit поведения.
   Mock latency не выдавать за production latency.

Эти три пункта требуют живого окружения и/или ручного подтверждения владельца.
Локальная машина, `.env`, SQLite, production updater branch и `main` не
затрагивались.


### Дополнительная уборка mixed tests и live diagnostics — 16 ноября 2026

- В focused workflow адресно добавлены `ShownHistoryTests`,
  `RelevanceRegressionTests` и `EngineInstrumentationTests` из
  `test_discovery_acceptance.py`. Они дают 14 дополнительных проверок
  текущих контрактов без production-изменений.
- CI run
  [#43865572436](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43865572436)
  на commit `8c28b0f442bd4e0ef5061fdf659269a53df81d81` — PASS:
  focused **658 passed, 1 deselected, 598 subtests**; broad configured
  **2,295 passed, 2 deselected, 1,259 subtests**; compile, smoke syntax,
  lock consistency и `pip check` — PASS.
- Проверены оставшиеся mixed modules: старые
  `services.conversation` merge suites и Agent/router tests не возвращаются
  в Option C CI; актуальные классы остаются адресно выбраны. Подробности
  зафиксированы в `docs/LEGACY_TEST_AUDIT_2026-11-15.md`.
- Удалён tracked backup `tests/eval_b5_runner.py.bak_b6`; `.gitignore`
  теперь игнорирует `reports/` и `*.bak_*`.
- Live diagnostic entrypoints получили явную маркировку LIVE / API-COSTING и
  пояснение по ожидаемому числу API requests/возможным Gemini usage.
  Скрипты не запускались; live API costs не расходовались.
- Остаются live Telegram acceptance, 70 реальных карточек с независимой
  gold-разметкой и production latency/API-call measurement. Нельзя
  подменять их synthetic fixtures или mock timings.


### Live performance and selector-capture tooling — 16 November 2026

- `smoke_conversational_search.py` now supports a single custom query
  (`--query`) and a low-cost search-only mode (`--search-only`) that skips
  shown-product follow-up and comparison calls. The default eight-case matrix
  remains unchanged.
- Per search it reports total wall time, actual SearchService retrieval wall
  time, provider route calls, unique requested page numbers, logical Affiliate
  API requests, HTTP attempts including retries, API call p50/p95, Gemini
  attempts/duration/tokens/known cost by operation, candidate-stage counts,
  final cards and decision/no-results reasons.
- `--repeat 1..5` is opt-in; default is one pass. Follow-up/comparison work,
  when enabled, is measured separately so it does not inflate search latency.
  A failed COMPARE route no longer triggers an unnecessary ProductAssistant
  Gemini call.
- `tests/test_live_smoke_metrics.py` covers percentile math, API retry /
  coalescing counters, Gemini usage grouping, candidate stage summaries,
  retrieval route instrumentation and custom-query normalization. It is
  included in focused CI and uses fake objects only.
- For the controlled 70-card selector capture, use the procedure in
  `tests/eval_selector_capture_schema.md`. The direct smoke runner exercises
  the authoritative Option C engine without Telegram polling, but still uses
  real Affiliate API and Gemini credentials and therefore incurs real usage.
  `--search-only` avoids extra comparison calls. Captures remain
  `selector_input_capture` with null page_count/gold until independently
  reviewed; no live capture or gold labeling was performed by this repository
  work.
- No real Telegram acceptance test or production latency run was executed.
  Those require the owner's local credentials/runtime and must remain a
  separately controlled manual step.


### Подтверждение обновлённого live-smoke tooling — CI PASS

GitHub Actions run
[#43867834966](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43867834966)
на commit `85261f9f4b166684a2c4acfdd71320c6e33df1a4` — SUCCESS:

- Focused offline suite: **667 passed, 1 deselected, 598 subtests passed**.
- Configured broad offline suite: **2,304 passed, 2 deselected, 1,259 subtests passed**.
- Production compile, manual live-smoke syntax compile, dependency lock
  consistency и `pip check`: PASS.
- Один pytest warning upstream Google GenAI SDK deprecation. Runner также
  выводит platform warning о будущей замене Node.js 20 в GitHub Actions;
  это не ошибка Python tests.

Этот run включает `tests/test_live_smoke_metrics.py`, custom query
normalization, provider route instrumentation, API retries/coalescing,
Gemini usage grouping и проверку candidate-stage summaries. Никаких live
API/Telegram вызовов в CI не было.


### CI workflow runtime and permission hardening — 16 November 2026

- Replaced `actions/checkout@v4` and `actions/setup-python@v5` with
  `@v7` releases, which use the supported Node.js 24 runtime.
- Restricted the workflow token to `contents: read`.
- Set `persist-credentials: false` because this workflow does not push or
  otherwise need authenticated Git operations after checkout.
- No application code, dependency versions, test selection, or runtime
  configuration changed.
- The last verified CI before this workflow-only change was run
  `43868102039` on `112c4960197840af10115cffb66febfa368fad3a` and passed.
  A fresh CI run on the hardened workflow is required before treating these
  action upgrades as accepted.
- The Google GenAI warning is separate: it originates in the pinned upstream
  SDK's use of Python's deprecated `_UnionGenericAlias`. It is not a project
  test failure; no warning suppression or untested SDK upgrade was introduced.


### Configuration test isolation and coverage — 16 November 2026

- tests/test_settings.py now clears the admin, analytics dashboard and
  price-tracking environment variables in its shared clean environment. Tests
  therefore cannot inherit these values from a developer's local .env.
- Added offline coverage for safe admin/dashboard defaults, administrator ID
  parsing and deduplication, dashboard configuration and port bounds, and the
  default/disabled price-tracking interval.
- Added tests/test_settings.py to the focused CI suite so configuration
  regressions fail the fast required job as well as the configured broad suite.
- No production settings behavior or default values were changed.
- CI on the new test selection is pending; do not count the new tests as passed
  until a fresh run on the resulting branch head succeeds.


### Повторная проверка CI на текущем HEAD — 16 ноября 2026

- Рабочий HEAD перед следующим изменением: `f4c151a550c85bf5e38d4914029e85dc6b16e28d`.
- GitHub Actions run [#43885118561](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43885118561) — SUCCESS.
- Focused offline suite: **689 passed, 1 deselected, 604 subtests passed**.
- Configured broad offline suite: **2,308 passed, 2 deselected, 1,259 subtests passed**.
- Production compile, manual live-smoke syntax compile, dependency lock consistency и `pip check` — PASS.
- Осталось одно предупреждение upstream Google GenAI SDK о deprecated `_UnionGenericAlias`; оно не подавлялось и не является падением теста.
- Broad suite остаётся filtered; это не unfiltered запуск всех исторических тестовых модулей.

### Дополнительное hardening ручного legacy-test audit workflow — 16 ноября 2026

- В `.github/workflows/legacy-test-audit.yml` actions checkout/setup-python обновлены с `@v4/@v5` до `@v7`.
- Workflow получил минимальные `contents: read` permissions; checkout использует `persist-credentials: false`.
- Сохранён `workflow_dispatch`: изолированные диагностические тесты не запускаются на каждом push и не влияют на обязательный offline CI.
- Commit: `b3065f894ed3f86c7263466c286ea85225d4fe5f`. Workflow ещё не запускался вручную после обновления; изменение только инфраструктурное.


### Уточнение состава ручного diagnostic workflow — 16 ноября 2026

- В `.github/workflows/legacy-test-audit.yml` смешанный модуль `tests/test_search_intent.py` заменён на актуальный `WiringGuardTests`.
- Добавлен отдельный target `ConversationSelectionEvidenceTests`, включающий opt-in и privacy regression tests selector snapshots.
- Полный `tests/test_latency_and_failures.py` заменён на `EngineInstrumentationTests`: второй класс модуля зависит от удалённого `services.chat_router` и не относится к активному Option C контракту.
- Изменение ограничено диагностическим CI workflow; production-код, retrieval, Gemini selection и пользовательские потоки не менялись.
- Commit workflow: `2945aac842dd622e576f058a34db58dce8baa83f`; документация: `63587b3fa4c01b85d13f0b0492b7aff03b0a01bc`.
- Workflow остаётся `workflow_dispatch` only и после изменения ещё не запускался. Результаты его тестов не заявляются до фактического ручного запуска.

### Документационная сверка после продолжения работ — 17 ноября 2026

- Исправлена устаревшая верхняя сводка в `PROJECT_CONTEXT.md`: теперь она
  прямо фиксирует, что P0.1/P0.2 retrieval-stopping и Gemini-continuation
  изменения возвращены к baseline и не являются текущей реализацией.
- Исправлено противоречие в `docs/LEGACY_TEST_AUDIT_2026-11-15.md`: прежняя
  формулировка ошибочно трактовала общее согласие на уборку репозитория как
  разрешение менять защищённые алгоритмы «Умного поиска». Указано, что
  конкретные изменения были отменены коммитами `0d8e3ced` и `73aba910`
  и требуют отдельного явного согласия.
- Проверен CI run `43931472972` на HEAD
  `ce058b3bd8f3a7ac973c06ae433f59f9cea2abd2`: SUCCESS, focused **689 passed,
  1 deselected, 604 subtests**; configured broad **2,308 passed,
  2 deselected, 1,259 subtests**; lock consistency, `pip check`, compile и
  live-smoke syntax compile PASS.
- Последующие документационные исправления `8784af9` и `ef8bdbd` ещё не
  проверены отдельным CI на новом HEAD. Пользовательские Telegram-проверки и
  реальный 70-card golden set остаются невыполненными.

- Дополнительно сверено описание `GEMINI_RESERVE_MODEL` в README с
  `AIService._generate`: резервная модель получает одну попытку и при
  quota/rate-limit ошибке основной модели; если лимит общий, резерв тоже
  может завершиться отказом, после чего цепочка прекращается. Исправлена
  только документация, поведение модели не менялось. Commit
  `6e768b12317a4a47e50ed3d16034b53fce3cffb5`.

### Дополнительная проверка SQLite, env template и финального CI — 17 ноября 2026

- В `tests/test_database.py` добавлен offline regression test конкурентной
  записи через шесть независимых SQLite connections. Проверяется, что все
  шесть транзакций завершаются и все строки сохранены. Production database
  code не менялся.
- Проверены существующие тесты атомарности миграций: неуспешная миграция
  откатывает schema changes и запись версии; миграции 001–003 проверяются
  на legacy upgrade и повторный запуск; foreign keys и ON DELETE semantics
  покрыты отдельными тестами.
- В `.env.example` исправлено вводящее в заблуждение название секции Gemini
  (Gemini используется не только для понимания текста, но и в других AI
  функциях); дублирующий блок dashboard удалён, правило не выставлять HTTP
  Basic Auth в открытый интернет без HTTPS reverse proxy оставлено рядом
  с активными переменными. Runtime конфигурация не менялась.
- Commit test: `fa5017c9f561c842fda041f051ecdd8b9686e180`.
- Commit env template: `9291b5e733fa694557a2c2b6bfe769f8f3aea8d0`.
- CI run [#44014178352](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/44014178352)
  на commit `9291b5e` — SUCCESS:
  focused **690 passed, 1 deselected, 604 subtests**;
  configured broad **2,309 passed, 2 deselected, 1,259 subtests**;
  dependency lock consistency, `pip check`, production compile и
  live-smoke syntax compile — PASS. Осталось одно upstream предупреждение
  Google GenAI SDK о deprecated `_UnionGenericAlias`.
- Broad suite остаётся configured/filtered, а не unfiltered. Изолированный
  workflow `.github/workflows/legacy-test-audit.yml` остаётся manual-only;
  его полная матрица после последней настройки не запускалась, и её
  результаты не заявляются.
- Пользовательские Telegram acceptance tests и контролируемый 70-card
  selector golden set остаются следующей ручной фазой. Live API calls,
  Telegram polling и Gemini quota не использовались в этой части.
- Локальный ПК, локальный/production `.env`, SQLite, backups, `main` и
  production updater branch не затрагивались.
