#!/usr/bin/env python3
"""Проверка утечек: ищет исходные значения из карты замен в обезличенном файле.

Метод совпадает с upstream (tests/integration/support/pipeline.js, findLeaks):
значение ищется во ВСЕХ XML-частях пакета в трёх представлениях — сырой XML,
XML с раскрытыми сущностями, XML без тегов. Регистр не учитывается.
Формат файла определяется по сигнатуре (PK\x03\x04), а не по расширению:
переименованный .anon — это тот же docx.

Использование:
    python3 check_leaks.py --map договор.map.json --anon договор.anon.docx
    python3 check_leaks.py --map договор.map.json --anon договор.anon.md

Код возврата: 0 — утечек нет, 1 — есть утечки, 2 — ошибка входных данных.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docx_text import docx_text, find_leaks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка утечек ПДн в обезличенном документе")
    ap.add_argument("--map", required=True, help="карта замен (mapping.json)")
    ap.add_argument("--anon", required=True, help="обезличенный файл")
    ap.add_argument("--min-length", type=int, default=4, help="минимальная длина значения (короче — ложные срабатывания)")
    ap.add_argument("--quiet", action="store_true", help="только итог")
    args = ap.parse_args()

    map_path = Path(args.map)
    anon_path = Path(args.anon)
    if not map_path.exists():
        print(f"карта не найдена: {map_path}", file=sys.stderr)
        return 2
    if not anon_path.exists():
        print(f"файл не найден: {anon_path}", file=sys.stderr)
        return 2

    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    entries = mapping.get("entries") or []
    if not entries:
        print("в карте нет записей — нечего проверять")
        return 0

    # значения, которые должны были быть заменены
    values = []
    for e in entries:
        for key in ("value", "original"):
            v = (e.get(key) or "").strip()
            if v and v not in values:
                values.append(v)

    leaks = find_leaks(anon_path, values, min_length=args.min_length)

    if not leaks:
        if not args.quiet:
            print(f"утечек нет: проверено {len(values)} значений в {anon_path.name}")
        return 0

    print(f"ОБНАРУЖЕНЫ УТЕЧКИ: {len(leaks)}")
    for l in sorted(set(leaks)):
        print(f"  {l}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
