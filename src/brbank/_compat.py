"""Compatibilidade com Python 3.10 (Odoo 16.0 roda em 3.10).

So o que a biblioteca usa e o 3.10 nao tem: `enum.StrEnum` e o
`datetime.fromisoformat` que aceita `Z` e fracao de segundo com qualquer
numero de digitos (ambos chegaram no 3.11).
"""

import re
import sys
from datetime import datetime
from enum import Enum

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercitado so no 3.10

    class StrEnum(str, Enum):
        """Equivalente ao `enum.StrEnum` do 3.11: membro e texto."""

        def __str__(self) -> str:
            return str(self.value)

        def __format__(self, spec: str) -> str:
            return format(str(self.value), spec)


_FRACAO = re.compile(r"(\.\d+)(?=[+-]\d{2}:?\d{2}$|$)")


def _fracao_6(achado: re.Match[str]) -> str:
    digitos = achado.group(1)[1:7]
    return "." + digitos.ljust(6, "0")


def datetime_iso(texto: str) -> datetime:
    """`datetime.fromisoformat` com o comportamento do 3.11 nos casos de banco.

    Aceita sufixo `Z` e fracao de segundo de 1 a 9 digitos (os PSPs Pix mandam
    milissegundos, o 3.10 so aceita 3 ou 6 digitos e recusa `Z`).
    """
    if sys.version_info >= (3, 11):
        return datetime.fromisoformat(texto)
    normalizado = texto.strip()  # pragma: no cover - so no 3.10
    if normalizado.endswith(("Z", "z")):  # pragma: no cover
        normalizado = normalizado[:-1] + "+00:00"
    normalizado = _FRACAO.sub(_fracao_6, normalizado)  # pragma: no cover
    return datetime.fromisoformat(normalizado)  # pragma: no cover
