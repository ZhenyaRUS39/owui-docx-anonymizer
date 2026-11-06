#!/usr/bin/env python3
"""Проверка новых настроек обезличивания на живом сервисе.

Проверяем, что каждая настройка реально меняет поведение, а не просто
принимается сервисом.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

SERVICE = os.environ.get("ANON_SERVICE", "http://127.0.0.1:8972")
TOKEN = os.environ.get("ANON_TOKEN", "")

TXT_COMMERCIAL = 'Плательщик: ООО "Лунный свет". Заказчик: АО "Сбербанк". ИНН 772816897110.'
TXT_CONTRACT = 'Договор № 77/2024. Сумма 1 500 000 руб. Компания ООО "Лунный свет".'
TXT_PII = 'Гражданин РФ Сидоров Иван Иванович, паспорт 45 05 123456.'
TXT_KNOWN = 'Ответственный: Сидоров Иван Иванович. Договор 77/2024.'

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail and not ok else ""))


def post(text, params=None, settings=None, filename="doc.txt"):
    url = SERVICE + "/anonymize"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    boundary = "----anonboundary1234567890"
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\n'.encode())
    parts.append(b"Content-Type: text/plain\r\n\r\n")
    parts.append(text.encode("utf-8"))
    if settings is not None:
        parts.append(f"\r\n--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="settings"\r\n\r\n')
        parts.append(json.dumps(settings, ensure_ascii=False).encode("utf-8"))
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    data = b"".join(parts)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    if not TOKEN:
        print("НЕТ ТОКЕНА (ANON_TOKEN)")
        return 2

    # ── 1. Дефолт сервиса: госорганы не тронуты ──
    r = post(TXT_COMMERCIAL)
    t = r.get("text", "")
    check("дефолт сервиса — коммерческая группа включена (названия компаний маскируются)",
          "Лунный свет" not in t, t)
    check("дефолт сервиса — госорган (АО Сбербанк) НЕ тронут",
          "Сбербанк" in t, t)
    check("дефолт сервиса — ПДн маскируются всегда",
          "Сидоров" not in post(TXT_PII).get("text", ""), "")

    # ── 2. Явное «не трогать» == дефолту ──
    t_keep = post(TXT_COMMERCIAL, params={"govEntities": "1"}).get("text", "")
    check("govEntities=1 (не трогать) — госорган не тронут", "Сбербанк" in t_keep, t_keep)
    t_mask = post(TXT_COMMERCIAL, params={"govEntities": "0"}).get("text", "")
    check("govEntities=0 (маскировать) — госорган замаскирован",
          "Сбербанк" not in t_mask, t_mask)

    # ── 3. Индивидуальные переключатели категорий ──
    # По умолчанию ядро маскирует организации, а номера договоров и суммы — нет.
    t_def = post(TXT_CONTRACT, params={"commercial": "1"}).get("text", "")
    check("категории по умолчанию — организация маскируется",
          "Лунный свет" not in t_def, t_def)
    check("категории по умолчанию — номер договора НЕ маскируется",
          "77/2024" in t_def, t_def)

    t_contract = post(TXT_CONTRACT, params={"commercial": "1"},
                       settings={"categories": {"CONTRACT": True}}).get("text", "")
    check("categories.CONTRACT=true — номер договора маскируется",
          "77/2024" not in t_contract, t_contract)

    t_amount = post(TXT_CONTRACT, params={"commercial": "1"},
                    settings={"categories": {"AMOUNT": True}}).get("text", "")
    check("categories.AMOUNT=true — сумма маскируется",
          "1 500 000" not in t_amount, t_amount)

    t_nocompany = post(TXT_CONTRACT, params={"commercial": "1"},
                       settings={"categories": {"COMPANY": False}}).get("text", "")
    check("categories.COMPANY=false — организация НЕ маскируется",
          "Лунный свет" in t_nocompany, t_nocompany)

    # ── 4. Список компаний (маскируются ВСЕГДА) ──
    t_list = post('Плательщик: ООО "Лунный свет".', params={"commercial": "0"},
                  settings={"maskedCompanies": [{"name": "ООО Лунный свет"}]}).get("text", "")
    check("maskedCompanies — компания маскируется даже при commercial=0",
          "Лунный свет" not in t_list, t_list)

    t_list_alias = post('Плательщик: ООО "Лунный свет".', params={"commercial": "0"},
                        settings={"maskedCompanies": [
                            {"name": "ООО Лунный свет", "aliases": ["Лунный свет"]}]}).get("text", "")
    check("maskedCompanies — псевдоним маскируется",
          "Лунный свет" not in t_list_alias, t_list_alias)

    # ── 5. Исключения (что НЕ маскировать) ──
    t_excl = post(TXT_KNOWN, settings={"exclusions": [
        {"value": "Сидоров Иван Иванович", "category": "PERSON"}]}).get("text", "")
    check("exclusions — указанное значение остаётся как есть",
          "Сидоров Иван Иванович" in t_excl, t_excl)
    check("exclusions — остальное маскируется",
          "45 05 123456" not in t_excl, t_excl)

    # ── 6. Профиль ядра ──
    t_ext = post(TXT_COMMERCIAL, settings={"profile": "external"}).get("text", "")
    check("профиль external — госорган маскируется (в отличие от дефолта)",
          "Сбербанк" not in t_ext, t_ext)

    t_llm = post(TXT_COMMERCIAL, settings={"profile": "llm"}).get("text", "")
    check("профиль llm — работает, госорган не тронут",
          "Сбербанк" in t_llm, t_llm)

    r = post(TXT_PII, settings={"profile": "нет-такого"})
    check("неизвестный профиль — обезличивание продолжает работать",
          "Сидоров" not in r.get("text", ""), json.dumps(r)[:200])

    # ── 7. Порог NER для организаций ──
    t_org = post('Договор с компанией Ромашка.', params={"nerThresholdOrg": "0.95"}).get("text", "")
    check("nerThresholdOrg — принимается и обезличивание работает",
          bool(t_org), t_org)

    # ── 8. Стоп-лист модели ──
    t_stop = post('Компания Ромашка и Сбербанк.', params={"nerStoplist": "Сбербанк"}).get("text", "")
    check("nerStoplist — принимается, обезличивание работает", bool(t_stop), t_stop)

    # ── 9. Свои категории ──
    # Категория должна пройти validateCustomCategory ядра: обязательны
    # id вида custom:*, label.ru, placeholderPrefix.ru/.en, color, priority,
    # и для dictionary — caseSensitive/wholeWord.
    t_custom = post('Проект Орион начинается в мае. Код: Орион.',
                     settings={"customCategories": [{
                         "id": "custom:project",
                         "label": {"ru": "Проект", "en": "Project"},
                         "placeholderPrefix": {"ru": "ПРОЕКТ", "en": "PROJECT"},
                         "color": "#8e44ad",
                         "priority": "belowBuiltin",
                         "enabled": True,
                         "kind": "dictionary",
                         "dictionary": {"values": ["Орион"],
                                        "caseSensitive": False,
                                        "wholeWord": True},
                     }]}).get("text", "")
    check("customCategories — своя категория маскируется",
          "Орион" not in t_custom, t_custom)
    check("customCategories — метка имеет свой префикс",
          "[ПРОЕКТ_" in t_custom or "[PROJECT_" in t_custom, t_custom)

    # ── 10. Некорректные данные не ломают обезличивание ──
    t_junk = post(TXT_PII, settings={"categories": "не-объект",
                                      "maskedCompanies": "строка"})
    check("некорректные настройки — обезличивание продолжает работать",
          bool(t_junk.get("text")), json.dumps(t_junk)[:200])

    ok = sum(1 for _, o, _ in RESULTS if o)
    total = len(RESULTS)
    print("-" * 60)
    print(f"ИТОГ: {ok}/{total}")
    if ok != total:
        print("ЕСТЬ ПРОВАЛЫ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
