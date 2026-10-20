"""Interface abstrata dos bancos (B2)."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar

from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado
from brbank.modelos import EventoCanonico, Instrucao, Titulo, TituloRegistrado
from brbank.status import TabelaStatus


class BankAdapter(ABC):
    banco: ClassVar[str]
    capacidades: ClassVar[Capacidade]
    tabela_status: ClassVar[TabelaStatus]
    # So vira True depois de rodar contra o ambiente real do banco.
    validado_sandbox: ClassVar[bool] = False

    def suporta(self, capacidade: Capacidade) -> bool:
        return capacidade in self.capacidades

    def exigir(self, capacidade: Capacidade) -> None:
        if not self.suporta(capacidade):
            raise NaoSuportado(f"{self.banco} nao declara {capacidade.name}")

    @abstractmethod
    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado: ...

    @abstractmethod
    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado: ...

    @abstractmethod
    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado: ...

    @abstractmethod
    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado: ...

    @abstractmethod
    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Converte o webhook em eventos. Nao confirma pagamento: reconsulte."""

    async def obter_pdf(self, id_banco: str) -> bytes:
        """PDF do boleto. Adapters com a capacidade PDF sobrescrevem."""
        self.exigir(Capacidade.PDF)
        raise NaoSuportado(f"{self.banco} declara PDF mas nao implementa obter_pdf")

    async def aclose(self) -> None:
        return None
