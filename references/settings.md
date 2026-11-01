# Настройки ядра docx-anonymizer — полный справочник

Источник: `src/core/settings.js` (`defaultSettings()`, `VALIDATORS`, `MIGRATIONS`) и `src/core/categories.js` (`BUILTIN_CATEGORIES`).
Схема настроек: версия 3 (миграция 2 → 3 — политика маскирования 0.4).

## Группы категорий

| Группа | Категории | Поведение |
|---|---|---|
| `pii` | PASSPORT, SNILS, CARD, EMAIL, PHONE, BIRTHDATE, PERSON, ADDRESS | маскируются **всегда**, выключить нельзя (`ALWAYS_ON_IDS`) |
| `ids` | INN, KPP, OGRN, ACCOUNT, IBAN, SWIFT, BIK, TAXID, SSN, URL | маскируются **всегда** |
| `commercial` | COMPANY, CONTRACT, AMOUNT | по переключателю `commercial`, **по умолчанию выключено** |

Политика 0.4: названия организаций не маскируются ни в одном профиле (миграция 2 → 3 принудительно ставит `pii`/`ids` в true и оставляет `commercial` выключенным).

## Поля Settings

| Поле | Тип | По умолчанию | Валидатор | Смысл |
|---|---|---|---|---|
| `schemaVersion` | int | 3 | целое | версия схемы; миграции 0→1→2→3 |
| `interceptEnabled` | bool | `false` | bool | перехват файлов в браузере (в Node не нужно) |
| `siteMode` | str | `allowlist` | `all`/`allowlist` | режим allowlist/blocklist |
| `allowlist` | str[] | 5 сайтов (см. ниже) | массив origin-паттернов | сайты, где перехват разрешён |
| `blocklist` | str[] | `[]` | массив origin-паттернов | запрещённые сайты (приоритетнее allowlist) |
| `categories` | obj{str:bool} | по группам | все значения bool | переключатели внутри `commercial` (для `pii`/`ids` всегда true) |
| `commercial` | bool | `false` | bool | группа «Коммерческая информация» |
| `maskedCompanies` | obj[] | `[]` | ≤200, структура `{name, aliases[]}` | компании, маскируемые **всегда**, даже при `commercial: false` |
| `orgPolicyHintPending` | bool | `false` | bool | разовая подсказка «организации теперь по желанию» (не экспортируется) |
| `mode` | str | `placeholder` | `placeholder`/`mask`/`delete` | режим замены |
| `language` | str | `auto` | `auto`/`ru`/`en`/`both` | язык детекторов |
| `placeholderLanguage` | str | `document` | `document`/`ru`/`en` | язык плейсхолдеров |
| `nerEnabled` | bool | `true` | bool | использовать модель NER |
| `nerThreshold` | float | `0.7` | 0..1 | порог для PER/LOC |
| `nerThresholdOrg` | float | `0.85` | 0..1 | порог для ORG (выше — иначе ложные организации) |
| `nerStoplist` | str[] | `[]` | ≤500, каждый ≤80 симв. | стоп-лист модельных организаций |
| `nerModel` | str | `''` | id пакета | id установленного пакета модели (не экспортируется) |
| `bundledModelSha` | str | `''` | hex64 | sha256 встроенного пакета, уже обработанного профилем (не экспортируется) |
| `bundledModelError` | str | `''` | `[A-Z_]{1,40}` | код последней неудачной автоустановки модели (не экспортируется) |
| `keepMetadata` | bool | `false` | bool | оставлять метаданные docProps (по умолчанию вычищаются) |
| `stripEmbedded` | bool | `false` | bool | вырезать встроенные OLE-объекты (встроенные объекты не обезличиваются) |
| `maxFileSizeMB` | int | `50` | 1..100 | лимит размера файла |
| `uiLanguage` | str | `auto` | `auto`/`ru`/`en` | язык интерфейса расширения |
| `exclusions` | obj[] | `[]` | `{value, category}` | что не маскировать |
| `govEntities` | bool | `true` | bool | распознавать государственные организации |
| `dictNames` | bool | `true` | bool | словарный детектор ФИО третьих лиц |
| `panelPosition` | obj | `{}` | объект | положение панели на странице |
| `hintMappingShown` | bool | `false` | bool | подсказка о ключе восстановления показана (не экспортируется) |
| `customCategories` | obj[] | `[]` | ≤100 | свои категории |
| `profiles` | obj[] | `llm`, `external` | массив | профили настроек |
| `activeProfile` | str | `llm` | строка | активный профиль |
| `batchOutput` | str | `folder` | `folder`/`zip`/`files` | пакетная выгрузка в браузере |
| `outputSuffix` | str | `.anon` | ≤32, без `\/:*?"<>\|` | суффикс обезличенного файла |
| `pdfMode` | str | `markdown` | `markdown`/`text`/`off` | что уходит в чат из PDF |

## allowlist по умолчанию

```
https://chatgpt.com
https://claude.ai
https://gemini.google.com
https://chat.deepseek.com
https://www.perplexity.ai
```

Свой домен добавляется через `chrome.permissions.request` — доступ к сайтам выдаётся вручную, при установке расширение не просит доступ ни к одному сайту. Паттерн поддерживает `https://*.example.com`.

## Встроенные профили

| id | Имя | Что меняет |
|---|---|---|
| `llm` | «Для LLM» | значения по умолчанию: `commercial: false`, `pii`/`ids` включены |
| `external` | «Для внешней передачи» | все категории включены, `govEntities: false`, `stripEmbedded: true` |

Профиль — набор overrides поверх дефолта; поля `categories`, `commercial`, `mode`, `language`, `placeholderLanguage`, `keepMetadata`, `stripEmbedded`, `govEntities`, `dictNames`, `nerThreshold`, `nerThresholdOrg` — переопределяемые.

## Уровни уверенности (влияют на приоритет)

`validated` (0) > `labeled` (1) > `kb` (2) > `ner` (3) > `weak` (4).

Каждая категория задаёт `allowedLevels` — кандидаты других уровней ядром отбрасываются. Пример: `PASSPORT` принимает только `labeled`, `PERSON` — `labeled`, `ner`, `weak`, `COMPANY` — `labeled`, `ner`, `kb`.

Источники (`source`): `rule` (регулярки с валидаторами), `label` (метки вида «ИНН:»), `kb` (база знаний сторон), `user` (добавлено вручную), `ner` (модель), `list` (список компаний).

`source: 'user'` и `source: 'list'` маскируются **даже при выключенной категории** — это осознанное исключение из политики.

## Миграции

| Переход | Что делает |
|---|---|
| 0 → 1 | проставляет `schemaVersion: 1` |
| 1 → 2 | порог NER 0.6 → 0.7 (значение пользователя не затирается) |
| 2 → 3 | `pii`/`ids` принудительно включаются и в настройках, и в профилях; `commercial` остаётся выключенным; выставляется `orgPolicyHintPending` |

## Применение в Node

```js
import { defaultSettings, migrateSettings } from 'docx-anonymizer/src/core/index.js';

const s = { ...defaultSettings(), commercial: true, mode: 'placeholder' };
```

`migrateSettings(obj)` — применить миграции к сохранённому объекту (например, из `chrome.storage.local`).

## Профили расширения в браузере

Два встроенных + свои. Настройки хранятся в `chrome.storage.local` под ключом `settings` (обёртка — `src/shared/settings-store.js`, единственное место чтения хранилища). Карта замен **не** хранится в `chrome.storage` — она живёт в памяти воркера offscreen; сохраняется вручную через меню «Ещё» панели (JSON-файл с ключом восстановления).
