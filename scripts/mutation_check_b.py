# -*- coding: utf-8 -*-
"""Мутационная проверка части B (Open WebUI-фильтр): тест обязан падать.

Мутации бьют по реальному коду references/openwebui-filter.py и запускают
scripts/test_pipeline.py — если мутированный фильтр всё ещё проходит тест,
значит тест недостаточно строгий.

Мутации (по новой архитектуре: фильтр ходит в HTTP-сервис, хуки async):
  1. файлы-документы НЕ вычищаются из тела запроса → B3/B4 должны упасть
  2. обезличенный текст НЕ подмешивается в сообщение → B2 (мало текста)
  3. карта замен НЕ сохраняется → B5
  4. outlet НЕ восстанавливает метки → B5
"""
import shutil
import subprocess
import sys
from pathlib import Path

SK = Path(__file__).resolve().parent.parent
FILTER = SK / "references" / "openwebui-filter.py"
BACKUP = Path("/tmp/filter_backup.py")


def run(limit=3, ner="/tmp/npk"):
    cmd = [
        sys.executable,
        str(SK / "scripts" / "test_pipeline.py"),
        "--repo", "/tmp/docx-anonymizer",
        "--fixtures", "/tmp/docx-anonymizer/tests/fixtures",
        "--script", str(SK / "scripts" / "anonymize.mjs"),
        "--ner", ner,
        "--limit", str(limit),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SK)).stdout
    line = [x for x in out.splitlines() if x.startswith("ИТОГ")]
    return line[0] if line else "НЕТ ИТОГА"


def mutate(name, old, new):
    s = FILTER.read_text(encoding="utf-8")
    assert old in s, "паттерн мутации не найден: " + name
    FILTER.write_text(s.replace(old, new, 1), encoding="utf-8")


def restore():
    shutil.copyfile(BACKUP, FILTER)


MUTATIONS = [
    (
        "файлы-документы НЕ вычищаются из тела",
        "        body[\"files\"] = kept_files",
        "        body[\"files\"] = kept_files + list(items)  # мутация: оригиналы остаются",
    ),
    (
        "обезличенный текст НЕ подмешивается в сообщение",
        "                last[\"content\"] = content + \"\\n\\n\" + \"\\n\\n\".join(anon_blocks)",
        "                pass  # мутация: текст не подмешан",
    ),
    (
        "карта замен НЕ сохраняется",
        "        self._save_map(key, {\"files\": maps, \"created\": time.time()})",
        "        pass  # мутация: карта не сохранена",
    ),
    (
        "outlet НЕ восстанавливает метки",
        "                content = self._restore(content, mapping)",
        "                pass  # мутация: без восстановления",
    ),
    (
        "оригинал остаётся доступен по id файла",
        "            await self._scrub_file_record(file_id, text)",
        "            pass  # мутация: запись файла не заменена",
    ),
    (
        "блок <attached_files> остаётся в теле запроса",
        "                content = _strip_attached_files(content)",
        "                pass  # мутация: блок не убран",
    ),
]

print("=" * 74)
print("МУТАЦИОННАЯ ПРОВЕРКА части B (Open WebUI-фильтр)")
print("=" * 74)

shutil.copyfile(FILTER, BACKUP)
control = run()
print("контроль (фильтр не сломан):", control)

failures = []
for name, old, new in MUTATIONS:
    restore()
    mutate(name, old, new)
    res = run()
    caught = not res.startswith("ИТОГ: 3/3")
    print("  %-48s %-12s %s" % (name, res, "OK — поймал" if caught else "ПРОВАЛ — тест не заметил"))
    if not caught:
        failures.append(name)

restore()
print("после восстановления:", run())
print("=" * 74)
print("ИТОГ мутаций: поймано %d из %d" % (len(MUTATIONS) - len(failures), len(MUTATIONS)))
sys.exit(1 if failures else 0)
