"""Confere, antes de criar o .venv, se este Python serve para instalar o projeto.

``make install`` roda este arquivo com o Python escolhido (``PYTHON``, padrão ``python3``). Ele usa só a
biblioteca padrão e sintaxe de Pythons antigos, porque o trabalho dele é recusar um Python que não serve
com uma mensagem que diga o que fazer, em vez do erro enganoso do pip ("No matching distribution found").

Casos vistos numa máquina limpa: Python compilado sem ``ssl`` (pyenv/asdf sem ``libssl-dev``), em que o
pip não alcança o PyPI; e o Python do Debian/Ubuntu sem ``ensurepip``, que vem no pacote ``python3.X-venv``.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable

MINIMUM = (3, 12)
DOCKER = "Sem mexer no Python da máquina: docker compose up --build (veja o README)."


def _installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def problems(version: tuple[int, int], found: Callable[[str], bool] = _installed) -> list[tuple[str, str]]:
    """(problema, como corrigir) para cada requisito que falta; vazio quando o Python serve."""
    major, minor = version
    out: list[tuple[str, str]] = []
    if version < MINIMUM:
        out.append(
            (
                f"Python {major}.{minor}: o projeto pede {MINIMUM[0]}.{MINIMUM[1]} ou mais novo.",
                "Instale o Python 3.12 (Ubuntu 24.04 já traz; Mac: brew install python@3.12; outros: pyenv) "
                "e rode: make install PYTHON=python3.12",
            )
        )
    if not found("_ssl"):
        out.append(
            (
                "Este Python foi compilado sem o módulo ssl, então o pip não consegue baixar nada do PyPI.",
                "Use outro Python, por exemplo o do sistema: make install PYTHON=/usr/bin/python3. "
                "Com pyenv/asdf, reinstale depois de instalar a libssl: "
                "sudo apt install libssl-dev && pyenv install --force 3.12",
            )
        )
    if not found("venv") or not found("ensurepip"):
        out.append(
            (
                "Este Python não consegue criar ambientes virtuais (falta o venv/ensurepip).",
                f"No Debian/Ubuntu: sudo apt install python{major}.{minor}-venv",
            )
        )
    return out


def main() -> int:
    found = problems((sys.version_info[0], sys.version_info[1]))
    if not found:
        print(f"Python {sys.version.split()[0]} em {sys.executable}: ok")
        return 0
    print(f"\nO Python em {sys.executable} não serve para instalar o projeto:\n", file=sys.stderr)
    for problem, fix in found:
        print(f"  - {problem}\n    Como resolver: {fix}\n", file=sys.stderr)
    print(f"  {DOCKER}\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
