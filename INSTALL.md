# Установка: Open WebUI DOCX Anonymizer

Инструкция для человека **или для ИИ-агента**. Каждый шаг — отдельная
команда с ожидаемым результатом. Если результат не совпал — остановись и
разберись, не иди дальше.

Целевая установка: хост с Open WebUI (в нашем случае — Home Assistant,
`192.168.1.4`), Docker, root-доступ по SSH.

---

## Что ставится

Три части, все на одном хосте:

| Часть | Что это | Где лежит |
|---|---|---|
| Ядро | upstream `argmaster-dev/docx-anonymizer` (распознавание ПДн) | `/opt/docx-anon-service/repo` |
| Модель | ONNX-модель распознавания ФИО/организаций (~30 МБ) | `/opt/docx-anon-service/ner` |
| Сервис | HTTP-сервис обезличивания, порт `8971`, слушает только localhost | контейнер `docx-anon-service-8971` |
| Фильтр | функция Open WebUI `docx_anonymizer` | в БД Open WebUI |

Ядро и модель **не входят в Docker-образ** — они тянутся при установке и
монтируются read-only. Поэтому обновление ядра не требует пересборки образа.

---

## Требования

На хосте с Open WebUI:

- `docker` (проверка: `docker version`)
- `git`, `curl`, `unzip` (в контейнере `node:22-alpine` их нет, поэтому
  скачивание ядра и модели идёт с хоста)
- интернет до `github.com` и `ghcr.io`
- ~1.5 ГБ свободного места (ядро с зависимостями ~600 МБ, модель 30 МБ,
  образ с LibreOffice ~500 МБ)

Версия ядра фиксирована: **v0.4.0**. В v0.5.0 upstream поменял API ядра,
и фильтр с ним не работает. Не меняй `CORE_VERSION` без проверки.

---

## Шаг 1. Получить репозиторий

```bash
git clone https://github.com/ZhenyaRUS39/owui-docx-anonymizer.git
cd owui-docx-anonymizer
```

Ожидаемо: каталог с `README.md`, `Dockerfile`, `references/`, `scripts/`.

Проверка:

```bash
ls references/ scripts/ && head -3 Dockerfile
```

---

## Шаг 2. Задать доступ к хосту

Скрипт ходит на хост по SSH. Нужен root-пароль:

```bash
export HA_ROOT_PASSWORD='<root-пароль хоста>'
export HA_HOST='192.168.1.4'     # если хост другой — поменяй
```

Если пароль лежит в `/config/.hermes/secrets/ha_ssh.json` (формат
`{"host": ..., "user": "root", "password": ...}`) — скрипт возьмёт его сам.

Проверка доступа:

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh -o StrictHostKeyChecking=no \
  root@"$HA_HOST" 'docker ps --format "{{.Names}}" | grep -i open-webui'
```

Ожидаемо: имя контейнера Open WebUI (например `app_4696b1a1_open-webui`).
Если пусто — Open WebUI не на этом хосте, проверь `HA_HOST`.

### Образ сервиса: доступ к реестру

Образ `ghcr.io/zhenyarus39/owui-docx-anonymizer-service:latest` **приватный**.
Если на хосте нет доступа к реестру, скрипт сам соберёт образ локально
(на 5–10 минут дольше) — установка всё равно пройдёт.

Чтобы тянуть готовый образ, залогинься на хосте:

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'echo "<gh-токен>" | docker login ghcr.io -u ZhenyaRUS39 --password-stdin'
```

Токен — GitHub PAT с правом `read:packages` (или `write:packages`).
Без логина скрипт перейдёт к локальной сборке — это не ошибка.

---

## Шаг 3. Установить

```bash
bash scripts/deploy_openwebui.sh
```

Скрипт идемпотентный: повторный запуск обновляет, а не ломает.

Что он делает по шагам (каждый шаг печатается в лог):

1. `1/6` — клонирует ядро **на теге v0.4.0** в `/opt/docx-anon-service/repo`,
   ставит зависимости (`npm ci`), скачивает модель NER в `/opt/docx-anon-service/ner`.
2. `2/6` — копирует сервис и обёртку в `/opt/docx-anon-service/anon`.
3. `3/6` — собирает образ с LibreOffice (нужен для `.doc`) и поднимает
   контейнер `docx-anon-service-8971` с `--network host`,
   `--restart unless-stopped`, токеном сервиса и `DOCX_ANON_HOST=127.0.0.1`.
4. `4/6` — создаёт admin API-ключ Open WebUI и включает `auth.enable_api_keys`.
5. `5/6` — регистрирует Filter `docx_anonymizer` (создаёт или обновляет).
6. `6/6` — ставит значения Valves, перезапускает контейнер Open WebUI
   (сбрасывает кэш скомпилированной функции) и гоняет живую проверку.

Ожидаемый финал:

```
=== ГОТОВО
Проверка: curl http://127.0.0.1:8971/health
Фильтр: docx_anonymizer (active + global)
```

Если скрипт упал — читай его вывод, в нём номер шага. Частые причины:

| Сообщение | Причина | Что делать |
|---|---|---|
| `не нашёл контейнер Open WebUI` | Open WebUI не на этом хосте | проверь `HA_HOST` |
| `МОДЕЛЬ НЕ СКАЧАЛАСЬ` | нет доступа в интернет или релиз upstream переименован | проверь `curl -sIL https://github.com/argmaster-dev/docx-anonymizer/releases/latest` |
| `auth failed` в логах сервиса | токен сервиса не совпал с тем, что в Valves | перезапусти скрипт установки (он пересоздаст токен) |
| `valves: 422` | Open WebUI не успел увидеть новый API-ключ (WAL) | подожди 10 с и повтори |

---

## Шаг 4. Проверить

### Сервис отвечает

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'curl -s http://127.0.0.1:8971/health'
```

Ожидаемо:

```json
{"ok":true,"ner":true,"repo":"/srv/repo","ner_dir":"/srv/ner",...}
```

`"ner":true` — модель распознавания загрузилась. `"ok":false` — сервис
не поднялся, смотри `docker logs docx-anon-service-8971`.

### Фильтр зарегистрирован

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'docker exec <контейнер-open-webui> python3 -c "
import sqlite3, json
con = sqlite3.connect(\"/app/backend/data/webui.db\")
r = con.execute(\"select is_active, is_global, valves from function where id=?\", (\"docx_anonymizer\",)).fetchone()
print(\"active=%s global=%s\" % (r[0], r[1]))
print(json.loads(r[2]))
"'
```

Ожидаемо: `active=1 global=1`, в Valves есть `SERVICE_URL` и `SERVICE_TOKEN`.

### Что реально уходит в модель

Фильтр пишет **аудит-лог** — точный текст запроса к модели:

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'docker exec <контейнер-open-webui> tail -f /tmp/docx-anon/model_input.log'
```

Отправь в чат документ с персональными данными. В логе должно быть:

```
<anonymized_document name="doc.docx">
Гражданин РФ [ФИО_1], паспорт [ПАСПОРТ_1]
</anonymized_document>
```

Исходных значений нет. Если они есть — фильтр не активен или не global.

### Сквозной тест

В Open WebUI: Админ → Functions → `docx_anonymizer` → иконка Valves →
все значения «Как в сервисе» → Save. Затем перезапусти контейнер Open WebUI
(значения фильтра кэшируются в процессе):

```bash
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'docker restart <контейнер-open-webui>'
```

Прикрепи документ с ФИО и спроси: «Перечисли все ФИО из приложенного
документа». Модель ответит метками (`[ФИО_1]`), а ты увидишь настоящие
значения — они восстанавливаются на выходе.

---

## Шаг 5. Настроить переключатели (по желанию)

Админ → Functions → `docx_anonymizer` → иконка Valves. Полный список и
смысл — в [README](README.md#настройка-переключатели).

Два правила, без которых настройка не сработает:

1. После смены значения **перезапусти контейнер Open WebUI** — фильтр
   кэширует Valves в процессе.
2. При установке через скрипт значения оператора **не сбрасываются**:
   скрипт читает текущие значения и мержит с ними.

Задать значения при установке из командной строки:

```bash
COMMERCIAL=Выключено MODE=Звёздочки bash scripts/deploy_openwebui.sh
```

---

## Обновление

```bash
cd owui-docx-anonymizer
git pull
bash scripts/deploy_openwebui.sh
```

Скрипт сам перезальёт код сервиса и фильтра, перезапустит контейнеры и
прогонит живую проверку. Ядро обновится, только если сменился
`CORE_VERSION` в скрипте.

---

## Откат

```bash
# убрать контейнер сервиса
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'docker rm -f docx-anon-service-8971'

# убрать фильтр из Open WebUI
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" \
  'docker exec <контейнер-open-webui> python3 -c "
import sqlite3
con = sqlite3.connect(\"/app/backend/data/webui.db\")
con.execute(\"delete from function where id=?\", (\"docx_anonymizer\",))
con.commit()
"'

# убрать файлы
sshpass -p "$HA_ROOT_PASSWORD" ssh root@"$HA_HOST" 'rm -rf /opt/docx-anon-service'
```

---

## Сборка образа вручную

Образ публикуется в GHCR автоматически при пуше тега `v*`:

```bash
docker pull ghcr.io/zhenyarus39/owui-docx-anonymizer-service:latest
```

Собрать самому:

```bash
docker build -t owui-docx-anonymizer-service:0.1.0 .
```

В образе: `node:22-alpine` + LibreOffice Writer + код сервиса.
Ядро и модель — нет, они монтируются при запуске.

---

## Если делает ИИ-агент

Порядок действий (не менять):

1. Прочитай `README.md` (что это, форматы, переключатели) и этот файл
   (установка).
2. Проверь доступ: `docker ps` на хосте с Open WebUI.
3. Склонируй репозиторий и запусти `bash scripts/deploy_openwebui.sh`.
4. Дождись `=== ГОТОВО`. Скрипт сам гоняет живую проверку — если он зелёный,
   установка удалась.
5. Проверь `curl http://127.0.0.1:8971/health` на хосте: `"ok":true`.
6. Проверь аудит-лог: `docker exec <owui> tail /tmp/docx-anon/model_input.log` —
   в нём должны быть метки, а не исходные значения.
7. **Не меняй `CORE_VERSION`** без проверки: v0.5.0 несовместим с фильтром.
8. **Не выкладывай в репозиторий тесты, фикстуры и секреты.** В репозитории
   живёт только плагин и рантайм; ключи и токены — в Valves на сервере.
