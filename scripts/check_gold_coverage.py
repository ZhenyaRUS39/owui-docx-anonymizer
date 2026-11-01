#!/usr/bin/env python3
"""Проверка полноты обезличивания против gold-разметки.

Для каждого документа с `.gold.json`:
  * какие сущности gold ожидались найти (category + value);
  * какие находки сделал движок (карта замен);
  * полнота: доля gold-сущностей, найденных движком;
  * утечки: значения, которые остались в обезличенном файле;
  * ложные срабатывания: находки, которых не было в gold (оценка сверху — носит
    информационный характер, потому что gold неполон по построению).

Код возврата: 0 — полнота по всем gold-сущностям 100 % и утечек нет; 1 — есть проблемы.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docx_text import docx_text  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import find_repo, find_fixtures, find_model  # noqa: E402



def normalize(s: str) -> str:
    """Нормализация для сравнения: регистр, пробелы, пунктуация."""
    s = (s or "").lower().replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,.;:()«»\"'")


def gold_entities(gold: dict):
    out = []
    for e in gold.get("entities", []):
        cat = e.get("category")
        val = e.get("value")
        if not cat or not val:
            continue
        out.append((cat, val))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(Path(find_fixtures()) / "heldout"))
    ap.add_argument("--map-dir", required=True, help="каталог с картами замен (*.map.json)")
    ap.add_argument("--min-length", type=int, default=4)
    args = ap.parse_args()

    fx = Path(args.fixtures)
    map_dir = Path(args.map_dir)
    rows = []
    total_gold = total_found = 0
    total_leaks = 0

    for gold_path in sorted(fx.glob("*/*.gold.json")):
        docx = gold_path.with_suffix("").with_suffix(".docx")
        if not docx.is_file():
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        expected = gold_entities(gold)
        if not expected:
            continue

        # карта замен этого документа
        stem = docx.stem
        maps = sorted(map_dir.glob(f"{stem}.map.json"))
        if not maps:
            rows.append((gold_path.parent.name + "/" + stem, len(expected), 0, 0, ["нет карты замен"], []))
            total_gold += len(expected)
            continue
        mapping = json.loads(maps[-1].read_text(encoding="utf-8"))
        entries = mapping.get("entries", [])
        found = {(e.get("category"), e.get("value")) for e in entries}

        # текст обезличенного файла
        anon = map_dir / f"{stem}.anon.docx"
        if not anon.is_file():
            anon = map_dir / f"{stem}.anon"
        # формат — по сигнатуре файла, а не по расширению: .anon тоже docx,
        # и при чтении как текст утечки были бы незаметны
        if anon.read_bytes()[:4] == b"PK\x03\x04":
            anon_text = docx_text(anon)
        else:
            anon_text = anon.read_text(encoding="utf-8", errors="replace")

        missed = []
        for cat, val in expected:
            total_gold += 1
            hit = (cat, val) in found
            # или значение найдено по нормализованному совпадению
            if not hit:
                nv = normalize(val)
                hit = any(fc == cat and normalize(fv) == nv for fc, fv in found)
            if hit:
                total_found += 1
            else:
                missed.append(f"{cat}:{val}")

        # утечки: значения gold, оставшиеся в обезличенном файле
        leaks = []
        for cat, val in expected:
            if len(val) < args.min_length:
                continue
            if val in anon_text:
                leaks.append(f"{cat}:{val}")

        # ложные срабатывания (информационно)
        gold_norm = {(c, normalize(v)) for c, v in expected}
        extra = [f"{e.get('category')}:{e.get('value')}" for e in entries if (e.get("category"), normalize(e.get("value") or "")) not in gold_norm]

        total_leaks += len(leaks)
        rows.append((gold_path.parent.name + "/" + stem, len(expected), len(expected) - len(missed), len(missed), missed, leaks, extra))

    print("=" * 100)
    print("ПОЛНОТА ОБЕЗЛИЧИВАНИЯ ПРОТИВ GOLD-РАЗМЕТКИ")
    print("=" * 100)
    for row in rows:
        name, exp, found_n, missed_n, missed, leaks = row[0], row[1], row[2], row[3], row[4], row[5]
        extra = row[6] if len(row) > 6 else []
        status = "OK  " if (missed_n == 0 and not leaks) else "WARN"
        print(f"[{status}] {name}: gold={exp}, найдено={found_n}, пропущено={missed_n}, утечек={len(leaks)}, лишних находок={len(extra)}")
        for m in missed[:4]:
            print(f"         пропущено: {m}")
        for lk in leaks[:4]:
            print(f"         УТЕЧКА: {lk}")
    print("-" * 100)
    print(f"ИТОГО по gold: найдено {total_found}/{total_gold} ({100*total_found/max(total_gold,1):.1f} %), утечек {total_leaks}")
    print("=" * 100)
    return 0 if (total_found == total_gold and total_leaks == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
