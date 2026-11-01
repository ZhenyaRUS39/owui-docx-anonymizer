# Open WebUI DOCX Anonymizer

Серверный плагин для [Open WebUI](https://github.com/open-webui/open-webui): обезличивает
вложенные документы **до** того, как они попадут в модель, и восстанавливает оригинальные
значения в ответе, который видит пользователь.

Работает как обычный Open WebUI **Filter** (серверная функция) — никаких расширений
браузера, никакого трафика наружу: ядро и модель распознавания персональных данных
живут на том же хосте.

```
┌──────────────┐   вложение        ┌────────────────┐   метки      ┌──────────┐
│  Open WebUI  │ ─────────────────► │  docx-anon-    │ ───────────► │  LLM     │
│  (фильтр)    │                    │  service :8971 │              │ (ПДн нет)│
│              │ ◄───────────────── │  ядро upstream │  значения    └──────────┘
│  ответ + ПДн │   восстановление   │  + модель NER  │  (outlet)
└──────────────┘                    └────────────────┘
```

## Что это делает

| Шаг | Что происходит |
|---|---|
| `inlet` | Документ читается, обезличивается (ФИО, паспорт, адреса, ИНН, счета и т. д.), исходный файл вычищается из запроса, в текст подставляется обезличенная версия с метками вида `[ФИО_1]`, `[ПАСПОРТ_1]` |
| модель | Видит **только метки**. Исходных значений в запросе нет — это видно в аудит-логе |
| `outlet` | Метки в ответе модели заменяются исходными значениями (с учётом русской морфологии: «его супруга [ФИО_3]» → «Белоусова») |

Поддерживаются: `docx`, `docm`, `pdf`, `txt`, `md`.

Не поддерживаются и **блокируются** (fail-closed): `xlsx`, `pptx`, `doc`, `odt` и прочее —
обезличить нечем, а персональные данные внутри возможны. Медиа (`png`, `jpg`, `mp3`, `mp4`)
проходят как есть и обычные чаты не ломаются.

## Требования

- Open WebUI (проверено на `v0.12.0`)
- Хост с docker и интернетом (SSH-доступ нужен для установки)
- Node.js в контейнере сервиса (ставится через образ `node:22-alpine`)

Ядро обезличивания — [argmaster-dev/docx-anonymizer](https://github.com/argmaster-dev/docx-anonymizer)
(MIT). Этот репозиторий — обёртка: HTTP-сервис вокруг ядра и Filter для Open WebUI.

## Установка

```bash
git clone <this-repo> && cd <this-repo>
bash scripts/deploy_openwebui.sh
```

Скрипт идемпотентный — его можно запускать повторно, он обновляет, а не ломает.

Он делает следующее:

1. клонирует ядро upstream в `/opt/docx-anon-service/repo` и ставит зависимости (`npm ci`);
2. скачивает модель распознавания персональных данных (ONNX, ~30 МБ) в `/opt/docx-anon-service/ner`;
3. поднимает контейнер `docx-anon-service` (`node:22-alpine`, `--network host`,
   `--restart unless-stopped`) с HTTP-сервисом на порту `8971`;
4. создаёт admin API-ключ Open WebUI и регистрирует Filter `docx_anonymizer`;
5. включает Filter и делает его глобальным;
6. прогоняет живую проверку из 6 сценариев.

Ожидаемый финал:

```
=== ГОТОВО
Фильтр: docx_anonymizer (active + global)
=== 6/6 живая проверка фильтра
[OK  ] L1 DOCX с ПДн
[OK  ] L2 PDF с ПДн
[OK  ] L3 битый docx (fail-closed)
[OK  ] L4 .xlsx (формат не обезличивается)
[OK  ] L5 картинка (обычный чат не ломается)
[OK  ] L6 чат без вложений
ИТОГ: 6/6
```

### Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `HA_HOST` | `192.168.1.4` | хост с Open WebUI |
| `HA_USER` | `root` | пользователь SSH |
| `HA_ROOT_PASSWORD` | — | пароль root, если его нет в `/config/.hermes/secrets/ha_ssh.json` |
| `BASE` | `/opt/docx-anon-service` | каталог установки на хосте |
| `PORT` | `8971` | порт сервиса обезличивания |
| `OWUI_CONTAINER` | определяется автоматически | контейнер Open WebUI |
| `OWUI_URL` | `http://127.0.0.1:8080` | адрес Open WebUI внутри хоста |
| `FILTER_ID` | `docx_anonymizer` | id функции-фильтра |
| `SKIP_LIVE_CHECK` | `0` | `1` — не гонять живую проверку в конце |

### Ручная установка

1. `git clone --depth 1 https://github.com/argmaster-dev/docx-anonymizer.git /opt/docx-anon-service/repo`
   и `npm ci` внутри (там `onnxruntime-web` и `pdfjs-dist` — без них не работает ни NER, ни PDF).
2. Модель NER: zip из [релизов upstream](https://github.com/argmaster-dev/docx-anonymizer/releases)
   в `/opt/docx-anon-service/ner` (нужен файл `model.onnx`).
3. `references/anon_service.mjs` → `/opt/docx-anon-service/anon/`,
   `scripts/anonymize.mjs` → `/opt/docx-anon-service/anon/scripts/`.
4. Контейнер:

   ```bash
   docker run -d --name docx-anon-service-8971 \
     --network host --restart unless-stopped \
     -v /opt/docx-anon-service/repo:/srv/repo:ro \
     -v /opt/docx-anon-service/ner:/srv/ner:ro \
     -v /opt/docx-anon-service/anon:/srv/anon:ro \
     -v /opt/docx-anon-service/state:/srv/state \
     -e DOCX_ANON_REPO=/srv/repo -e DOCX_ANON_NER=/srv/ner \
     -e DOCX_ANON_WRAPPER=/srv/anon/scripts/anonymize.mjs \
     -e DOCX_ANON_WORKDIR=/srv/state -e DOCX_ANON_PORT=8971 \
     -w /srv/anon node:22-alpine node /srv/anon/anon_service.mjs
   ```

5. Filter: `references/openwebui-filter.py` — как функция с id `docx_anonymizer`,
   `is_active=true`, `is_global=true`. Значения `Valves` — дефолтные
   (`SERVICE_URL=http://127.0.0.1:8971`): сервис доступен по localhost, потому что оба
   контейнера в host-network.

Проверка:

```bash
curl -s http://127.0.0.1:8971/health
# {"ok":true,"ner":true,"repo":"/srv/repo",...}
```

## Проверка

### Живой стенд (6 сквозных сценариев)

```bash
bash scripts/deploy_openwebui.sh          # в конце гоняет эту проверку
```

| Сценарий | Ожидание |
|---|---|
| L1 DOCX с ПДн | модель получила метки, ПДн нет; в чате значения вернулись |
| L2 PDF с ПДн | то же |
| L3 битый docx | документ не выпущен к модели, пользователь предупреждён |
| L4 `.xlsx` | документ не выпущен (формат не обезличивается) |
| L5 картинка | обычный чат работает |
| L6 без вложений | обычный чат работает, меток нет |

### Браузерная проверка

```bash
OWUI_URL=http://<host>:8080 \
OWUI_EMAIL=<логин> OWUI_PASSWORD=<пароль> \
timeout 275 node scripts/browser_check.mjs /path/to/document.docx
```

Chromium → логин → вложение → вопрос → проверка DOM ответа, перехваченного запроса
и блока `<attached_files>`. Таймаут 275 с — не меньше: ответ модели занимает 20–60 с.

### Локальные тесты

```bash
bash scripts/run_suite.sh              # всё
bash scripts/run_suite.sh pipeline     # выборочно
```

| Набор | Кейсов | Что проверяет |
|---|---|---|
| `test_pipeline.py` | 61 | обезличивание → подстановка в запрос → восстановление, на корпусе из upstream |
| `test_extended.py` | 18 | PDF, TXT/MD/DOCM, режимы mask/delete, идемпотентность |
| `test_edge_cases.py` | 8 | корректный отказ: пароль, скан, `.xlsx`, битый zip, нет файла |
| `test_openwebui_filter.py` | 10 | Filter на моках Open WebUI: файлы вычищены → модель получила текст → outlet восстановил |
| `mutation_check.py` | 3 мутации | тест обязан падать на сломанном коде |
| `mutation_check_b.py` | 6 мутаций | то же для Filter |

Тесты требуют checkout upstream-ядра и каталог модели NER — пути ищутся сами
(`scripts/paths.py`), можно задать через `DOCX_ANON_REPO` и `DOCX_ANON_NER`.

## Устройство

```
references/openwebui-filter.py   Filter Open WebUI: inlet (обезличить) + outlet (восстановить)
references/anon_service.mjs      HTTP-сервис: /health, /anonymize, /restore
references/settings.md           справка по настройкам ядра
scripts/anonymize.mjs            CLI-обёртка над ядром (anonymize / restore)
scripts/deploy_openwebui.sh      установка и обновление на хосте
scripts/test_live_deployed.py    6 сквозных сценариев на живом стенде
scripts/browser_check.mjs        проверка через настоящий браузер
scripts/run_suite.sh              прогон всех наборов
scripts/owui_mock/               минимальные моки Open WebUI для тестов
docs/DEVELOPMENT.md          журнал разработки: как устроен Filter, подводные камни Open WebUI, результаты тестов
```

### Что лежит в `body` в момент `inlet`

`body["files"]` — список вложений, `body["messages"]` — сообщения, `body["metadata"]` —
метаданные чата. Содержимого файла там **нет**: обезличивать нужно самому —
скачать, обработать, подменить.

Имя параметра хука — `__user__`, не `user` (в шаблоне Filter в UI Open WebUI — баг шаблона).

### Порядок работы в Open WebUI

`middleware.py` добавляет блок `<attached_files>` с id файла **до** `inlet`.
По этому id модель может вызвать встроенный инструмент `view_file` и прочитать
оригинал. Поэтому Filter не только чистит `body["files"]`, но и:

1. вырезает блок `<attached_files>` из текста сообщения;
2. перезаписывает `files.data['content']` обезличенным текстом.

Без этого ПДн утекли бы через `view_file`, RAG и поиск по коллекции.

## Лицензия

MIT — см. [LICENSE](LICENSE).

Ядро обезличивания — [argmaster-dev/docx-anonymizer](https://github.com/argmaster-dev/docx-anonymizer)
(Copyright (c) 2026 argmaster-dev, MIT): используется как внешняя зависимость и
подключается на этапе установки. Тестовые фикстуры (`scripts/fixtures/live_check.pdf`)
взяты из его репозитория — MIT, Copyright (c) 2026 argmaster-dev.

Сторонние компоненты, подключаемые ядром, распространяются на условиях своих лицензий:
ONNX Runtime Web (MIT), pdf.js (Apache-2.0), словари и модель распознавания —
см. `NOTICE` в репозитории upstream.
