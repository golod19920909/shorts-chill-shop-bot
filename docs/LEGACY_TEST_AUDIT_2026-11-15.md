# Аудит модулей, исключённых из broad offline CI

**Дата:** 15 ноября 2026 года  
**Ветка:** `chore/repository-cleanup-2026-11-15`

## Назначение и границы

Этот документ фиксирует первичную классификацию модулей, которые явно исключены из broad offline pytest-команды в `.github/workflows/conversational-regression.yml`.

Это **не** утверждение, что все перечисленные тесты устарели. Проверка выполнена по импортам, названиям тестов и текущей архитектурной карте. Полный запуск каждого исключённого модуля и сверка каждого assertion с актуальным контрактом ещё не выполнены. Поэтому в рамках этой уборки тесты не удаляются, не получают blanket `xfail` и не возвращаются в обязательный CI без отдельной проверки.

## Классификация

| Модуль | Наблюдение по исходнику | Предварительный статус |
|---|---|---|
| `test_agent_service.py` | Импортирует `services.agent_service`, старый Agent/tool-loop API и `ConversationState`. | Подтверждённый кандидат на архивирование/переписывание под Option C; старый production-модуль не является текущей точкой входа. |
| `test_b3_routing_and_failures.py` | Импортирует `services.chat_router`, `ChatAction` и fixtures из старого `test_stage47_conversational.py`. | Legacy routing contract; не считать актуальным тестом authoritative engine без переписывания. |
| `test_b6_conversational_flow.py` | Использует старые chat-router и handler flow fixtures. | Legacy flow contract; требует переписывания на `ConversationSearchEngine`, прежде чем возвращать в обязательный CI. |
| `test_conversation_integration.py` | Интеграционные сценарии построены вокруг старого chat-router / ProductAssistant маршрута. | Переходный интеграционный тест; его бизнес-сценарии могут быть полезны, но wiring больше не является авторитетным. |
| `test_stage19b_offline_runner.py` | Тестирует отдельный исторический acceptance runner через динамическую загрузку файла. | Исторический диагностический harness; сначала проверить, поддерживается ли сам runner. |
| `test_stage19b_strict_addendum.py` | Проверяет внутренние функции того же исторического runner. | Исторический диагностический harness; не удалять без проверки зависимости runner. |
| `test_stage19c_offline.py` | Проверяет отдельный исторический offline acceptance script и его сетевые ограничения. | Исторический диагностический harness; проверить актуальность script entrypoint. |
| `test_stage21_classifier.py` | Проверяет исторический classifier/labeling script через динамическую загрузку. | Исторический диагностический harness; не относится автоматически к текущему conversational engine. |
| `test_stage47_conversational.py` | Импортирует `services.chat_router`, `services.conversation` и старый routing API. | Legacy routing suite; отдельные сценарии можно перенести в engine tests, но старый contract не должен управлять production behavior. |
| `test_stage49_followups.py` | Тестирует `services.chat_router` и `services.conversation.resolve_request`. | Legacy follow-up suite; требует переноса сценариев на текущий engine, а не механического включения. |
| `test_variant_b_conversation.py` | Небольшой набор тестов старого `chat_router`. | Legacy routing contract. |
| `test_discovery_acceptance.py` | Использует текущие database, handlers, keyboards и acceptance-сценарии пользовательского UI. | **Не подтверждён как устаревший.** Нужен отдельный разбор конкретных падений и текущих UX-контрактов; не удалять. |
| `test_gemini_telemetry.py` | Смешивает актуальные telemetry/instrumentation tests и исторический `AgentLoopTests`. | **Частично возвращён в focused CI:** пять текущих классов выполняются отдельно; старый AgentLoop-класс остаётся исключённым. |
| `test_ai_service.py` | Большой смешанный модуль текущего `AIService`; отдельные классы уже запускаются в focused suite, весь файл исключён из broad suite. | **Смешанный модуль, не stale целиком.** Разделить по операциям/контрактам; не включать или исключать весь файл одним решением. |
| `test_conversation.py` | Проверяет старый `services.conversation` state/merge helper. | Legacy helper contract; сверить оставшиеся call sites и перенести только действующие общие функции. |
| `test_stage3a_merge_feature_protection.py` | Тестирует merge constraints в старом `services.conversation.resolve_request`. | Legacy merge contract; не должен подменять тесты ledger/slot provenance текущего engine. |
| `test_stage3c_merge_constraint_protection.py` | Тестирует старый `deterministic_merge` / `resolve_request`. | Legacy merge contract; требует переноса актуальных инвариантов на текущий state ledger, если они ещё применимы. |
| `test_latency_and_failures.py` | Смешивает актуальные `services.convsearch.engine` и старые `chat_router` / `conversation` пути. | **Смешанный модуль.** Выделить engine instrumentation tests отдельно; не считать весь файл stale. |
| `test_result_limit_and_followup.py` | Использует текущие handlers, SearchService и text-matching для лимита карточек. | **Не подтверждён как устаревший.** Сценарии могут затрагивать защищённый «Умный поиск»; не менять assertions или production без отдельного разрешения. |
| `test_search_intent.py` | Тестирует текущий `query_intent`, но также старый `services.conversation` resolver. | **Смешанный модуль.** Отделить действующий intent contract от legacy merge path. |
| `test_session_isolation.py` | Использует текущие database, handlers, discovery и карточные callbacks; проверяет разделение состояния поиска и ленты. | **Не подтверждён как устаревший.** Включает взаимодействие защищённых потоков; не менять без отдельного разрешения. |

## Выводы

1. Нельзя называть все исключённые модули «устаревшими»: несколько тестируют действующие production-сервисы и пользовательские контракты.
2. Наиболее явный legacy-кластер — старый `chat_router` / `services.conversation` / Agent-tool-loop. Его тесты нельзя механически использовать как источник требований для Option C.
3. Смешанные тестовые файлы следует разделять по ответственности: текущие engine/telemetry/UI проверки отдельно от legacy merge/routing сценариев.
4. `test_discovery_acceptance.py`, `test_gemini_telemetry.py`, `test_result_limit_and_followup.py` и `test_session_isolation.py` требуют приоритетной адресной проверки, а не удаления.
5. Сценарии, затрагивающие «Умный поиск» и «✨ Находки», остаются защищёнными. Текущая узкая авторизация пользователя касается только исправления `empty_keyboard` в narrowing follow-up и не расширяется этим аудитом.

## Следующие безопасные действия

- Запускать исключённые модули по одному в изолированной CI-диагностике и сохранять полный traceback.
- Для каждого падения определять, устарел ли сам assertion, тестовая fixture или production behavior.
- Переносить полезные сценарии в тесты authoritative engine / текущих сервисов, не переписывая поведение под старые тесты.
- Возвращать в обязательный broad CI только модули, которые прошли отдельную проверку.
- До завершения аудита явно называть текущий broad CI **configured/filtered offline suite**, а не полным unfiltered suite.


### Follow-up после targeted CI

`test_gemini_telemetry.py` теперь частично запускается в focused CI. Первый запуск выявил устаревшее ожидание в timeout test: реализация делает один reserve retry, поэтому записывает две попытки. Ожидание исправлено на две попытки (primary + reserve); production-код не менялся. Проверка исправления ожидает повторного CI.


### Test hygiene follow-up

The broad-suite log showed that `BuyCallbackTests.test_products_without_affiliate_url_have_no_buy_button` had been declared async without any await under `unittest.TestCase`, so unittest did not await it and its assertions were not actually executed. It is now synchronous. This is test-only; CI run [#1945](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43742653098) for commit `ff8b668d` passed, and the warning disappeared from the broad suite.
