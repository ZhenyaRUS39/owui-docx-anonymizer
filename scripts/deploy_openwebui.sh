#!/usr/bin/env bash
# Установка обезличивания документов в Open WebUI.
#
#   bash scripts/deploy_openwebui.sh
#
# Что ставится (три части):
#   1. ядро upstream (argmaster-dev/docx-anonymizer) + модель NER → /opt/docx-anon-service
#   2. HTTP-сервис обезличивания → контейнер docx-anon-service (node:22-alpine, host-network)
#   3. Filter Open WebUI (docx_anonymizer) — обезличивает вложения в inlet, восстанавливает в outlet
#
# Требуется: SSH-доступ к хосту с Open WebUI (root), docker, интернет на хосте.
# Скрипт идемпотентный: повторный запуск обновляет, а не ломает.
#
# Переменные окружения (все необязательные, кроме HA_HOST при нестандартном хосте):
#   HA_HOST=192.168.1.4          хост с Open WebUI
#   HA_USER=root                 пользователь SSH
#   HA_ROOT_PASSWORD=...         пароль root (если нет /config/.hermes/secrets/ha_ssh.json)
#   BASE=/opt/docx-anon-service   каталог установки на хосте
#   PORT=8971                    порт сервиса обезличивания
#   OWUI_CONTAINER=...           контейнер Open WebUI (определяется автоматически)
#   OWUI_URL=http://127.0.0.1:8080  адрес Open WebUI внутри хоста
#   FILTER_ID=docx_anonymizer    id функции-фильтра
#   SKIP_LIVE_CHECK=1            не гонять живую проверку в конце
#   LIVE_SRC=<каталог>           каталог со скриптами живой проверки. Если они есть
#                                (в скилле агента) — скрипт запустит проверку после
#                                установки; если нет — проверка пропускается.
set -euo pipefail

HA_HOST="${HA_HOST:-192.168.1.4}"
HA_USER="${HA_USER:-root}"
BASE="${BASE:-/opt/docx-anon-service}"
PORT="${PORT:-8971}"
FILTER_ID="${FILTER_ID:-docx_anonymizer}"
OWUI_URL="${OWUI_URL:-http://127.0.0.1:8080}"
# Имя контейнера привязано к порту: иначе вторая установка (для теста, на
# другом порту) пересоздаст контейнер первой и уронит работающий сервис.
SERVICE_NAME="${SERVICE_NAME:-docx-anon-service-$PORT}"

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NER_ASSET="${NER_ASSET:-docx-anonymizer-ner-rubert-tiny2-ner-ruen-1.1.0.zip}"

say() { printf '\n=== %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# --- SSH -------------------------------------------------------------------
# Хост отдаёт root-пароль, а не ключ: ssh без askpass не отработает.
setup_askpass() {
  local pass_file=/tmp/ssh_pass_ha.sh
  local pass=""
  if [ -f /config/.hermes/secrets/ha_ssh.json ]; then
    pass=$(python3 -c "import json;print(json.load(open('/config/.hermes/secrets/ha_ssh.json')).get('password',''))" 2>/dev/null || echo "")
  fi
  [ -z "$pass" ] && pass=${HA_ROOT_PASSWORD:-root}
  printf '#!/bin/sh\necho "%s"\n' "$pass" > "$pass_file"
  chmod +x "$pass_file"
  export SSH_ASKPASS="$pass_file"
  export SSH_ASKPASS_REQUIRE=force
  export SSH_ASKPASS_REQUIRE=force
}

ssh_ha() {
  ssh -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/config/.hermes/secrets/n100_known_hosts \
      -o ConnectTimeout=10 \
      -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      "$HA_USER@$HA_HOST" "$@"
}

setup_askpass

# --- 0. находим контейнер Open WebUI --------------------------------------
say "0/6 контейнер Open WebUI"
OWUI_CONTAINER="${OWUI_CONTAINER:-$(ssh_ha "docker ps --format '{{.Names}}' | grep -i 'open-webui' | head -1")}"
[ -n "$OWUI_CONTAINER" ] || die "не нашёл контейнер Open WebUI на $HA_HOST (задай OWUI_CONTAINER)"
echo "контейнер: $OWUI_CONTAINER"

# --- 1. ядро upstream и модель NER -----------------------------------------
say "1/6 ядро upstream и модель NER → $BASE"
ssh_ha "set -e
mkdir -p $BASE
if [ ! -d $BASE/repo/.git ]; then
  rm -rf $BASE/repo
  git clone --depth 1 https://github.com/argmaster-dev/docx-anonymizer.git $BASE/repo
fi
if [ ! -d $BASE/repo/node_modules ]; then
  docker run --rm -v $BASE/repo:/w -w /w node:22-alpine sh -c 'npm ci --no-audit --no-fund'
fi
if [ ! -f $BASE/ner/model.onnx ]; then
  mkdir -p $BASE/ner
  # Качаем НА ХОСТЕ (там есть curl/wget), распаковываем в контейнере:
  # в alpine curl нет, а ставить его — лишняя зависимость.
  curl -sL -o $BASE/ner/np.zip https://github.com/argmaster-dev/docx-anonymizer/releases/latest/download/$NER_ASSET
  docker run --rm -v $BASE/ner:/w alpine sh -c 'cd /w && unzip -o -q np.zip && rm -f np.zip'
  test -f $BASE/ner/model.onnx || { echo 'МОДЕЛЬ НЕ СКАЧАЛАСЬ'; exit 1; }
fi
echo 'ядро и модель готовы'
ls $BASE/repo/node_modules | wc -l"

# --- 2. сервис и обёртка ----------------------------------------------------
say "2/6 сервис и обёртка в $BASE/anon"
ssh_ha "mkdir -p $BASE/anon/scripts $BASE/state"
cat "$SKILL_DIR/references/anon_service.mjs" | ssh_ha "cat > $BASE/anon/anon_service.mjs"
cat "$SKILL_DIR/references/office_extract.mjs" | ssh_ha "cat > $BASE/anon/office_extract.mjs"
cat "$SKILL_DIR/scripts/anonymize.mjs" | ssh_ha "cat > $BASE/anon/scripts/anonymize.mjs"

# --- 3. контейнер сервиса ---------------------------------------------------
# Токен сервиса: сервис обрабатывает ПДн. Если его слушают не только на
# localhost — токен обязателен (сервис сам не стартует без него).
SERVICE_TOKEN="${SERVICE_TOKEN:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
say "3/6 контейнер $SERVICE_NAME"
# LibreOffice нужен для разбора .doc (бинарный Word 97–2003): больше никто
# в этом окружении .doc не читает. Ставим в образ сервиса, чтобы был
# доступен и при пересоздании контейнера.
SERVICE_IMAGE="${SERVICE_IMAGE:-node:22-alpine}"
if [ "$SERVICE_IMAGE" = "node:22-alpine" ]; then
  say "     сборка образа с LibreOffice (нужен для .doc)"
  ssh_ha "docker rm -f ${SERVICE_NAME}-img 2>/dev/null || true"
  ssh_ha "docker run --name ${SERVICE_NAME}-img $SERVICE_IMAGE sh -c \"apk add --no-cache libreoffice-writer >/dev/null 2>&1 && echo INSTALLED\"" || true
  ssh_ha "docker commit ${SERVICE_NAME}-img ${SERVICE_NAME}-img:lo 2>/dev/null || true"
  ssh_ha "docker rm -f ${SERVICE_NAME}-img 2>/dev/null || true"
  if ssh_ha "docker image inspect ${SERVICE_NAME}-img:lo >/dev/null 2>&1"; then
    SERVICE_IMAGE="${SERVICE_NAME}-img:lo"
    say "     образ $SERVICE_IMAGE"
  else
    say "     LibreOffice не установился — .doc будет блокироваться"
  fi
fi
ssh_ha "docker rm -f $SERVICE_NAME 2>/dev/null || true
docker run -d --name $SERVICE_NAME \
  --network host --restart unless-stopped \
  -v $BASE/repo:/srv/repo:ro -v $BASE/ner:/srv/ner:ro \
  -v $BASE/anon:/srv/anon:ro -v $BASE/state:/srv/state \
  -e DOCX_ANON_REPO=/srv/repo -e DOCX_ANON_NER=/srv/ner \
  -e DOCX_ANON_WRAPPER=/srv/anon/scripts/anonymize.mjs \
  -e DOCX_ANON_WORKDIR=/srv/state -e DOCX_ANON_PORT=$PORT \
  -e DOCX_ANON_TOKEN=$SERVICE_TOKEN -e DOCX_ANON_HOST=127.0.0.1 \
  -w /srv/anon $SERVICE_IMAGE node /srv/anon/anon_service.mjs"
sleep 5
HEALTH=$(ssh_ha "curl -s -m 10 http://127.0.0.1:$PORT/health")
echo "$HEALTH"
echo "$HEALTH" | grep -q '"ok":true' || die "сервис не поднялся: $HEALTH"

# --- 4. admin API-ключ ------------------------------------------------------
# Ключ генерируем здесь, в репозитории: в скрипте и в репозитории скилла
# никаких постоянных ключей лежать не должно.
say "4/6 admin API-ключ + auth.enable_api_keys"
API_KEY="${API_KEY:-sk-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
python3 - "$API_KEY" <<'PY' > /tmp/owui_mkkey.py
import sys
key = sys.argv[1]
print(f'''import json, sqlite3, sys, time, uuid
DB = "/app/backend/data/webui.db"
KEY = {key!r}
con = sqlite3.connect(DB); cur = con.cursor()
row = cur.execute("select id, email, role from user where role='admin' order by created_at limit 1").fetchone()
if not row:
    print("NO_ADMIN"); sys.exit(3)
user_id, email, role = row
now = int(time.time() * 1000)
old = cur.execute("select id from api_key where key=?", (KEY,)).fetchone()
if old:
    kid = old[0]
else:
    kid = str(uuid.uuid4())
    cur.execute("insert into api_key (id,user_id,key,data,expires_at,last_used_at,created_at,updated_at) values (?,?,?,?,?,?,?,?)",
                (kid, user_id, KEY, json.dumps({{"name":"docx-anonymizer-deploy"}}), None, None, now, now))
cur.execute("insert into config (key,value,updated_at) values (?,?,?) on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
            ("auth.enable_api_keys", "true", now))
con.commit(); con.close()
print(json.dumps({{"ok": True, "role": role, "email": email}}))''')
PY
ssh_ha "docker exec -i $OWUI_CONTAINER python3 - '$API_KEY'" < /tmp/owui_mkkey.py
rm -f /tmp/owui_mkkey.py

# --- 5. регистрация Filter --------------------------------------------------
say "5/6 регистрация Filter $FILTER_ID"
python3 - "$FILTER_ID" "$SKILL_DIR" <<'PY' > /tmp/owui_filter_payload.json
import json, sys
fid, skill_dir = sys.argv[1], sys.argv[2]
src = open(f"{skill_dir}/references/openwebui-filter.py", encoding="utf-8").read()
print(json.dumps({
    "id": fid,
    "name": "DOCX Anonymizer",
    "content": src,
    "meta": {"description": "Обезличивает вложения (docx/pdf/txt) перед отправкой в модель. Локально, без сети."},
}, ensure_ascii=False))
PY
cat /tmp/owui_filter_payload.json | ssh_ha "cat > /tmp/owui_filter_payload.json"
rm -f /tmp/owui_filter_payload.json

owui_api() {  # метод путь [data-файл]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    ssh_ha "curl -s -o /dev/null -w '%{http_code}' -X $method -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' --data-binary @$data $OWUI_URL$path"
  else
    ssh_ha "curl -s -o /dev/null -w '%{http_code}' -X $method -H 'Authorization: Bearer $API_KEY' $OWUI_URL$path"
  fi
}
owui_api_get() {  # путь -> json
  ssh_ha "curl -s -H 'Authorization: Bearer $API_KEY' $OWUI_URL$1"
}

# create — только если функции ещё нет; update — если есть. Идемпотентно.
EXISTS=$(owui_api GET "/api/v1/functions/id/$FILTER_ID")
if [ "$EXISTS" = "200" ]; then
  echo "функция уже есть — обновляем"
  ssh_ha "cat /tmp/owui_filter_payload.json | curl -s -o /dev/null -w 'update: %{http_code}\n' -X POST -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' --data-binary @- $OWUI_URL/api/v1/functions/id/$FILTER_ID/update"
else
  echo "создаём"
  ssh_ha "cat /tmp/owui_filter_payload.json | curl -s -o /dev/null -w 'create: %{http_code}\n' -X POST -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' --data-binary @- $OWUI_URL/api/v1/functions/create"
fi

# global — тоже переключатель: включаем, только если ещё не глобальный.
GLOBAL=$(owui_api_get "/api/v1/functions/id/$FILTER_ID" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if d.get("is_global") else 0)' 2>/dev/null || echo 0)
if [ "$GLOBAL" != "1" ]; then
  owui_api POST "/api/v1/functions/id/$FILTER_ID/toggle/global" || true
  echo "включили global"
else
  echo "фильтр уже глобальный"
fi

# ВАЖНО: toggle ПЕРЕКЛЮЧАЕТ is_active, а не включает. Если вызвать его на уже
# активном фильтре — он ВЫКЛЮЧИТСЯ и перестанет работать (is_active=0),
# а get_active_filter_ids его не отдаст. Поэтому включаем только если выключен.
ACTIVE=$(owui_api_get "/api/v1/functions/id/$FILTER_ID" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if d.get("is_active") else 0)' 2>/dev/null || echo 0)
if [ "$ACTIVE" != "1" ]; then
  owui_api POST "/api/v1/functions/id/$FILTER_ID/toggle" || true
  echo "включили фильтр"
else
  echo "фильтр уже активен"
fi
owui_api_get "/api/v1/functions/id/$FILTER_ID" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("active=", d.get("is_active"), "global=", d.get("is_global"), "type=", d.get("type"))'

# Значения Valves: токен сервиса обезличивания. Без него фильтр не дойдёт
# до сервиса (если тот слушает не только localhost).
python3 -c "import json,sys;print(json.dumps({'SERVICE_TOKEN':sys.argv[1],'SERVICE_URL':sys.argv[2]}))" \
  "$SERVICE_TOKEN" "http://127.0.0.1:$PORT" > /tmp/owui_valves.json
cat /tmp/owui_valves.json | ssh_ha "cat > /tmp/owui_valves.json"
rm -f /tmp/owui_valves.json
ssh_ha "curl -s -o /dev/null -w 'valves: %{http_code}\n' -X POST -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' --data-binary @/tmp/owui_valves.json $OWUI_URL/api/v1/functions/id/$FILTER_ID/valves/update"

# --- ГОТОВО -----------------------------------------------------------------
# Backend компилирует функцию и кэширует её на весь процесс: правка в БД
# видна в function.content, но работающий процесс продолжит исполнять старый
# код. Поэтому после обновления контейнер перезапускается.
if [ "${SKIP_RESTART:-0}" != "1" ]; then
  say "перезапуск $OWUI_CONTAINER (сброс кэша скомпилированной функции)"
  ssh_ha "docker restart $OWUI_CONTAINER" || true
  # Ждём не фиксированное время, а готовность: Open WebUI поднимается дольше
  # 12 секунд, и живая проверка внутри деплоя падала с «Expecting value»
  # (API ещё не отвечал). Ожидаем /health до 180 секунд.
  OWUI_READY=0
  for _ in $(seq 1 60); do
    if ssh_ha "curl -s -m 5 -o /dev/null -w '%{http_code}' ${OWUI_URL}/health 2>/dev/null" 2>/dev/null | grep -q '200'; then
      OWUI_READY=1
      break
    fi
    sleep 3
  done
  if [ "$OWUI_READY" != "1" ]; then
    die "Open WebUI не поднялся после рестарта (health не ответил)"
  fi
  say "Open WebUI готов"
fi

say "ГОТОВО"
echo "Проверка: curl http://127.0.0.1:$PORT/health"
echo "Фильтр: $FILTER_ID (active + global)"

# Живая проверка (6 сквозных сценариев) — скрипты лежат не здесь, а рядом
# с этим репозиторием (в скилле агента). Если их нет — пропускаем.
LIVE_SRC="${LIVE_SRC:-}"
if [ -z "$LIVE_SRC" ] && [ -f "$SKILL_DIR/scripts/test_live_deployed.py" ]; then
  LIVE_SRC="$SKILL_DIR"
fi
if [ -n "$LIVE_SRC" ] && [ -f "$LIVE_SRC/scripts/test_live_deployed.py" ]; then
  say "живая проверка фильтра"
  cat "$LIVE_SRC/scripts/test_live_deployed.py" | ssh_ha "cat > /tmp/test_live_deployed.py"
  cat "$LIVE_SRC/scripts/live_fixtures.py" | ssh_ha "cat > /tmp/live_fixtures.py"
  cat "$LIVE_SRC/scripts/fixtures/live_check.pdf" | ssh_ha "cat > /tmp/live_check.pdf"
  ssh_ha "docker cp /tmp/test_live_deployed.py $OWUI_CONTAINER:/tmp/test_live_deployed.py
docker cp /tmp/live_fixtures.py $OWUI_CONTAINER:/tmp/live_fixtures.py
docker exec $OWUI_CONTAINER mkdir -p /tmp/fixtures
docker cp /tmp/live_check.pdf $OWUI_CONTAINER:/tmp/fixtures/live_check.pdf
docker exec $OWUI_CONTAINER sh -c 'printf %s \"$API_KEY\" > /tmp/owui_key && chmod 600 /tmp/owui_key'
docker exec $OWUI_CONTAINER timeout 300 python3 /tmp/test_live_deployed.py" || true
else
  echo
  echo "Проверить, что обезличивание работает, можно загрузить документ в чат:"
  echo "  — модель должна видеть метки ([ФИО_1], [ПАСПОРТ_1]), а не исходные значения;"
  echo "  — в ответе пользователю значения должны быть восстановлены."
fi
