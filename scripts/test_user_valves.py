# -*- coding: utf-8 -*-
"""Проверка пользовательских параметров (Filter.UserValves) и приоритета.

Что проверяем:

  1. Filter.UserValves объявлен и содержит те же 33 поля, что и Valves.
  2. У каждого выпадающего списка первой опцией идёт «Как у администратора»
     — наследование глобальной настройки.
  3. Приоритет: явное значение пользователя → глобальная настройка →
     значение по умолчанию ядра.
  4. Незаданное пользователем значение НЕ перекрывает глобальное:
     «Как у администратора» и пустая строка = не задано.
  5. Пользовательские значения не протекают между запросами: экземпляр
     фильтра общий для всех пользователей, состояние — только на время хука.
  6. Технические параметры (SERVICE_URL, TIMEOUT, WORKDIR, …) тоже
     перекрываются пользователем.

Запуск:  python3 scripts/test_user_valves.py
"""
import asyncio
import importlib.util
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
FILTER_PY = HERE.parent / "references" / "openwebui-filter.py"

AS_ADMIN = "Как у администратора"

_passed = 0
_failed = 0


def ok(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"[OK  ] {what}")
    else:
        _failed += 1
        print(f"[FAIL] {what}")


def load():
    spec = importlib.util.spec_from_file_location("anon_filter", FILTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load()
    F = mod.Filter

    # ── 1. наличие и состав UserValves ──────────────────────────────────
    ok(hasattr(F, "UserValves"), "Filter.UserValves объявлен")

    user_props = F.UserValves.schema()["properties"]
    admin_props = F.Valves.schema()["properties"]
    ok(len(user_props) == 33, f"в UserValves 33 поля (получено {len(user_props)})")
    ok(set(user_props) == set(admin_props), "поля UserValves совпадают с Valves")

    # ── 2. «Как у администратора» — первая опция каждого списка ─────────
    selects = {n: p for n, p in user_props.items()
               if (p.get("input") or {}).get("type") == "select"}
    ok(len(selects) > 0, f"в UserValves есть выпадающие списки ({len(selects)})")
    bad = [n for n, p in selects.items()
           if not (p["input"].get("options") or [])
           or p["input"]["options"][0].get("value") != AS_ADMIN]
    ok(not bad, f"первая опция во всех списках — «{AS_ADMIN}» (нарушения: {bad})")

    # значения списков совпадают с админскими (плюс опция наследования)
    mismatch = []
    for n, p in selects.items():
        uvals = [o["value"] for o in p["input"]["options"]]
        a_in = admin_props[n].get("input") or {}
        if not a_in.get("options"):
            # Админская форма — переключатель (bool): в пользовательской
            # это список Включено/Выключено + наследование.
            if uvals != [AS_ADMIN, "Включено", "Выключено"]:
                mismatch.append(n)
            continue
        avals = [o["value"] for o in a_in["options"]]
        if uvals != [AS_ADMIN] + avals:
            mismatch.append(n)
    ok(not mismatch, f"опции UserValves = админские + наследование (расхождения: {mismatch})")

    # Числа в пользовательской форме принимают пустое значение («не задано»),
    # иначе фронт подставил бы дефолт схемы и молча перекрыл настройку
    # администратора.
    int_fields = [n for n, p in user_props.items()
                  if "integer" in json.dumps(p.get("anyOf", []))]
    ok(int_fields, f"числовые поля распознаются ({int_fields})")
    for n in int_fields:
        ok(user_props[n].get("default") == "",
           f"{n}: пустое значение = «как у администратора»")

    # ── 3. приоритет: пользователь → админ → умолчание ────────────────
    f = F()
    f.valves = F.Valves(MODE="Звёздочки", TIMEOUT=300, FAIL_CLOSED=True,
                         SERVICE_URL="http://127.0.0.1:8971", WORKDIR="/tmp/adm")
    f._apply_user_valves({"valves": F.UserValves(MODE="Метки", TIMEOUT=600,
                                                 FAIL_CLOSED="Выключено",
                                                 SERVICE_URL="http://127.0.0.1:9999",
                                                 WORKDIR="/tmp/usr")})
    ok(f._valve("MODE") == "Метки", "явное значение пользователя побеждает глобальное")
    ok(f._valve("TIMEOUT") == 600, "пользователь перекрывает TIMEOUT")
    ok(f._fail_closed() is False, "пользователь перекрывает FAIL_CLOSED")
    ok(f._valve("SERVICE_URL") == "http://127.0.0.1:9999", "пользователь перекрывает SERVICE_URL")
    ok(f._valve("WORKDIR") == "/tmp/usr", "пользователь перекрывает WORKDIR")
    ok(f._valve("CATEGORY_AMOUNT") == "Как в сервисе",
       "незаданный пользователем параметр наследует глобальный")
    mod._EFFECTIVE_VALVES.set(None)

    # ── 4. «Как у администратора» и пустая строка = не задано ───────────
    f2 = F()
    f2.valves = F.Valves(MODE="Звёздочки", MASKED_COMPANIES="ООО Ромашка",
                          NER_THRESHOLD="0.85")
    f2._apply_user_valves({"valves": F.UserValves(MODE=AS_ADMIN,
                                                   MASKED_COMPANIES="",
                                                   NER_THRESHOLD="0.95",
                                                   FAIL_CLOSED=AS_ADMIN)})
    ok(f2._valve("MODE") == "Звёздочки",
       "«Как у администратора» наследует глобальное значение")
    ok(f2._valve("MASKED_COMPANIES") == "ООО Ромашка",
       "пустая строка наследует глобальный список")
    ok(f2._valve("NER_THRESHOLD") == "0.95",
       "явное значение пользователя действует (NER_THRESHOLD)")
    ok(f2._fail_closed() is True,
       "«Как у администратора» наследует глобальный FAIL_CLOSED")
    mod._EFFECTIVE_VALVES.set(None)

    # флаг FAIL_CLOSED: «Включено»/«Выключено» в пользовательской форме
    f6 = F()
    f6.valves = F.Valves(FAIL_CLOSED=True)
    f6._apply_user_valves({"valves": F.UserValves(FAIL_CLOSED="Выключено")})
    ok(f6._fail_closed() is False, "«Выключено» в пользовательской форме даёт False")
    f6._apply_user_valves({"valves": F.UserValves(FAIL_CLOSED="Включено")})
    ok(f6._fail_closed() is True, "«Включено» в пользовательской форме даёт True")
    mod._EFFECTIVE_VALVES.set(None)

    # ── 5. без пользовательских значений поведение прежнее ─────────────
    f3 = F()
    f3.valves = F.Valves(MODE="Метки", TIMEOUT=300)
    f3._apply_user_valves({"valves": F.UserValves()})
    ok(f3._valve("MODE") == "Метки", "без переопределений берётся глобальное значение")
    ok(f3._valve("TIMEOUT") == 300, "технический параметр без переопределений прежний")
    f3._apply_user_valves({"valves": None})
    ok(f3._valve("MODE") == "Метки", "отсутствие user-valves не ломает чтение")
    ok(f3._valve("TIMEOUT") == 300, "отсутствие user-valves не ломает техпараметры")
    mod._EFFECTIVE_VALVES.set(None)

    # ── 6. изоляция между запросами ─────────────────────────────────────
    f4 = F()
    f4.valves = F.Valves(MODE="Метки")

    async def hook(user_valves):
        token = f4._apply_user_valves({"valves": user_valves})
        try:
            return f4._valve("MODE")
        finally:
            f4._reset_user_valves(token)

    a = asyncio.run(hook(F.UserValves(MODE="Удалить")))
    b = asyncio.run(hook(F.UserValves()))
    ok(a == "Удалить", "в хуке действует пользовательское значение")
    ok(b == "Метки", "после хука значение не утекает в следующий запрос")
    ok(mod._EFFECTIVE_VALVES.get() is None, "состояние сброшено после хука")

    # два «пользователя» подряд: значения не смешиваются
    u1 = asyncio.run(hook(F.UserValves(MODE="Звёздочки")))
    u2 = asyncio.run(hook(F.UserValves(MODE="Метки")))
    ok(u1 == "Звёздочки" and u2 == "Метки", "значения разных пользователей не смешиваются")

    # ── 7. _service_params собирает параметры из эффективных значений ───
    f5 = F()
    f5.valves = F.Valves(MODE="Как в сервисе", COMMERCIAL="Как в сервисе",
                          NER_ENABLED="Включено")
    params, _extra = f5._service_params()
    ok(params.get("nerEnabled") == "1", "админское значение доходит до параметров сервиса")
    ok("mode" not in params, "незаданный админом параметр не передаётся сервису")
    ok("commercial" not in params, "«Как в сервисе» не превращается в явный флаг")

    f5._apply_user_valves({"valves": F.UserValves(MODE="Звёздочки",
                                                   COMMERCIAL="Включено",
                                                   NER_ENABLED="Выключено")})
    params, extra = f5._service_params()
    ok(params.get("mode") == "mask", "пользовательский MODE дошёл до параметров сервиса")
    ok(params.get("commercial") == "1", "пользовательский COMMERCIAL дошёл до параметров сервиса")
    ok(params.get("nerEnabled") == "0", "пользовательский NER_ENABLED дошёл до параметров сервиса")
    mod._EFFECTIVE_VALVES.set(None)

    print(f"\nИТОГ: {_passed}/{_passed + _failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
