from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.config import PROJECT_ROOT

SOURCE_DB = PROJECT_ROOT / "data" / "anexo_desafio_1.db"
DICTIONARY = PROJECT_ROOT / "src" / "semantic" / "dictionary.yaml"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private copy of the challenge database; tests assert it is never modified."""
    target = tmp_path_factory.mktemp("db") / "anexo_desafio_1.db"
    shutil.copy(SOURCE_DB, target)
    before = file_sha256(target)
    yield target
    assert file_sha256(target) == before, "the database file was modified during the tests"
