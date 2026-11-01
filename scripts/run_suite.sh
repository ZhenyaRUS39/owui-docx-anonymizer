#!/usr/bin/env bash
# Полный прогон скилла docx-anonymizer.
#
#   ./run_suite.sh                 — все наборы тестов
#   ./run_suite.sh pipeline        — только основной pipeline
#   ./run_suite.sh extended edge   — выборочно
#
# Пути определяются автоматически (scripts/paths.py): checkout upstream и каталог
# модели NER ищутся в /tmp/docx-anonymizer, /opt/docx-anonymizer и др.
# Переопределение: REPO=... NER=... ./run_suite.sh
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-$(python3 -c "import sys;sys.path.insert(0,'$SKILL_DIR/scripts');from paths import find_repo;print(find_repo())")}"
FIXTURES="${FIXTURES:-$REPO/tests/fixtures}"
NER="${NER:-$(python3 -c "import sys;sys.path.insert(0,'$SKILL_DIR/scripts');from paths import find_model;print(find_model())")}"

cd "$SKILL_DIR" || exit 2

want() { [ $# -eq 0 ] && return 0; for s in "$@"; do [ "$s" = "$1" ] && return 0; done; return 1; }

SUITES="${*:-pipeline extended edge owui mutation}"

rc=0
for s in $SUITES; do
  echo
  echo "############################################################"
  echo "# $s"
  echo "############################################################"
  case "$s" in
    pipeline)
      python3 scripts/test_pipeline.py --repo "$REPO" --fixtures "$FIXTURES" \
        --script "$SKILL_DIR/scripts/anonymize.mjs" ${NER:+--ner "$NER"} \
        --with-genres --with-pdf --limit 61 || rc=1
      ;;
    extended)
      python3 scripts/test_extended.py || rc=1
      ;;
    edge)
      python3 scripts/test_edge_cases.py || rc=1
      ;;
    owui)
      python3 scripts/test_openwebui_filter.py --repo "$REPO" --fixtures "$FIXTURES" \
        --script "$SKILL_DIR/scripts/anonymize.mjs" ${NER:+--ner "$NER"} || rc=1
      ;;
    mutation)
      python3 scripts/mutation_check.py || rc=1
      python3 scripts/mutation_check_b.py || rc=1
      ;;
  esac
done

echo
echo "=============================================================="
if [ $rc -eq 0 ]; then echo "ВСЕ НАБОРЫ ПРОЙДЕНЫ"; else echo "ЕСТЬ ПРОВАЛЫ"; fi
echo "=============================================================="
exit $rc
