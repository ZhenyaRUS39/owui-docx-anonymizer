# -*- coding: utf-8 -*-
"""Общий поиск локального checkout upstream-репозитория.

Пути в README (/opt/docx-anonymizer) рассчитаны на развёрнутый инстанс. На
рабочей машине checkout может лежать в /tmp или в ~/.cache — ищем сами, чтобы
тесты запускались без ручных флагов.
"""
import os
from pathlib import Path

CANDIDATES = [
    os.environ.get("DOCX_ANON_REPO", ""),
    "/opt/docx-anonymizer",
    "/tmp/docx-anonymizer",
    "/config/docx-anonymizer",
    str(Path.home() / "docx-anonymizer"),
    str(Path.home() / ".cache" / "docx-anonymizer"),
]


def find_repo(default: str = "/opt/docx-anonymizer") -> str:
    """Первый существующий checkout upstream (по src/core/index.js)."""
    for c in CANDIDATES:
        if c and (Path(c) / "src" / "core").is_dir():
            return c
    return default


def find_fixtures(repo: str = None, default: str = "/opt/docx-anonymizer/tests/fixtures") -> str:
    repo = repo or find_repo()
    p = Path(repo) / "tests" / "fixtures"
    return str(p) if p.is_dir() else default


def find_model(explicit: str = "") -> str:
    """Каталог модели NER: явный путь, переменная окружения или известные места."""
    if explicit:
        return explicit
    env = os.environ.get("DOCX_ANON_NER", "")
    if env:
        return env
    for c in ["/tmp/npk", "/opt/docx-anon-model", "/tmp/np", "/tmp/docx-anpk"]:
        if Path(c).is_dir():
            return c
    return ""
