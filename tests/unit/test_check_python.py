"""The check that ``make install`` runs before creating the .venv (scripts/check_python.py)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import ModuleType

from src.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "check_python.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_python", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


def _without(*missing: str) -> object:
    return lambda name: name not in missing


def test_a_complete_python_passes() -> None:
    assert check.problems((3, 12), _without()) == []
    assert check.problems((3, 13), _without()) == []


def test_python_without_ssl_says_how_to_fix_it() -> None:
    """The tester's case: pip only said "No matching distribution found for pydantic"."""
    [(problem, fix)] = check.problems((3, 12), _without("_ssl"))
    assert "ssl" in problem and "PYTHON=/usr/bin/python3" in fix


def test_debian_python_without_ensurepip_names_the_package() -> None:
    [(problem, fix)] = check.problems((3, 12), _without("ensurepip"))
    assert "venv" in problem and "sudo apt install python3.12-venv" in fix


def test_old_python_is_refused_with_every_problem_listed() -> None:
    found = check.problems((3, 11), _without("_ssl", "ensurepip"))
    assert len(found) == 3
    assert "3.12" in found[0][0] and "python3.11-venv" in found[2][1]


def test_the_script_accepts_the_interpreter_that_runs_the_tests() -> None:
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)  # noqa: S603
    assert done.returncode == 0, done.stderr
    assert "ok" in done.stdout
