"""Status canonico e tabela versionada de status por adapter (B9)."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from brbank._compat import StrEnum
from brbank.erros import Validacao


class StatusCanonico(StrEnum):
    PENDENTE_REGISTRO = "PENDENTE_REGISTRO"
    REGISTRADO = "REGISTRADO"
    LIQUIDADO = "LIQUIDADO"
    BAIXADO = "BAIXADO"
    REJEITADO = "REJEITADO"


@dataclass(frozen=True)
class TabelaStatus:
    """Mapa codigo do banco -> status canonico, com versao.

    Codigo desconhecido levanta erro: assumir um status por padrao poderia
    liquidar ou baixar um titulo por engano.
    """

    versao: str
    mapa: Mapping[str, StatusCanonico]

    @classmethod
    def criar(cls, versao: str, mapa: dict[str, StatusCanonico]) -> "TabelaStatus":
        return cls(versao, MappingProxyType(dict(mapa)))

    def traduzir(self, codigo: str | None) -> StatusCanonico:
        try:
            return self.mapa[(codigo or "").upper()]
        except KeyError:
            raise Validacao(
                f"status {codigo!r} desconhecido na tabela v{self.versao}"
            ) from None
