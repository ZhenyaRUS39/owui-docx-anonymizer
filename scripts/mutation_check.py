# -*- coding: utf-8 -*-
"""Мутационная проверка теста: убеждаемся, что он ЛОВИТ поломки.

Правило: если сломанный код даёт "10/10", тест бесполезен.
"""
import shutil
import subprocess
import sys
from pathlib import Path

SK = Path(__file__).resolve().parent.parent
SCRIPT = SK / "scripts" / "anonymize.mjs"
BACKUP = Path("/tmp/anonymize_backup.mjs")

APPLY = "  const res = applyDecisions(doc, analysis.groups, {"
APPLY_BAD = "  const res = applyDecisions(doc, [], {"
RESTORE = "      out = restoreDocx(bytes, mapping, { decline: !flag('--no-decline') });"
RESTORE_BAD = "      out = { bytes, unresolved: [], declined: [], skipped: [], changedParts: [], tokens: [] };"


def run(limit=3, ner="/tmp/npk"):
    cmd = [
        sys.executable,
        str(SK / "scripts" / "test_pipeline.py"),
        "--repo", "/tmp/docx-anonymizer",
        "--fixtures", "/tmp/docx-anonymizer/tests/fixtures",
        "--script", str(SCRIPT),
        "--ner", ner,
        "--limit", str(limit),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SK)).stdout
    line = [x for x in out.splitlines() if x.startswith("ИТОГ")]
    return line[0] if line else "НЕТ ИТОГА"


def mutate(name, old, new):
    s = SCRIPT.read_text(encoding="utf-8")
    assert old in s, "паттерн мутации не найден: " + name
    SCRIPT.write_text(s.replace(old, new, 1), encoding="utf-8")


def restore():
    shutil.copyfile(BACKUP, SCRIPT)


MUTATIONS = [
    ("карта замен не пишется", "  if (mapPath) {", "  if (false && mapPath) {"),
    ("ничего не заменяем (applyDecisions пустой)", APPLY, APPLY_BAD),
    ("восстановление ничего не делает", RESTORE, RESTORE_BAD),
]

print("=" * 74)
print("МУТАЦИОННАЯ ПРОВЕРКА: тест обязан падать на сломанном коде")
print("=" * 74)

shutil.copyfile(SCRIPT, BACKUP)
print("контроль (код не сломан):", run())

failures = []
for name, old, new in MUTATIONS:
    restore()
    mutate(name, old, new)
    res = run()
    caught = not (res.startswith("ИТОГ: 3/3") or res.startswith("ИТОГ: 10/10"))
    print("  %-42s %-12s %s" % (name, res, "OK — поймал" if caught else "ПРОВАЛ — тест не заметил"))
    if not caught:
        failures.append(name)

restore()
print("после восстановления:", run())
print("=" * 74)
print("ИТОГ мутаций: поймано %d из %d" % (len(MUTATIONS) - len(failures), len(MUTATIONS)))
sys.exit(1 if failures else 0)
