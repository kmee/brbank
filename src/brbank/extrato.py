"""Extrato da conta corrente pela API do banco: lancamentos para a conciliacao.

Segue o padrao de `dda.py` e `pagamento.py`: contrato opcional, com modelo e
capacidade proprios; o chamador descobre o suporte por
`isinstance(adapter, AdapterExtrato)`. No Odoo, o consumidor natural e um
provider do `account_statement_import_online` (OCA/bank-statement-import).

Identidade do lancamento: `id_lancamento` e um hash dos campos que o banco nao
muda depois (data, valor, sinal, historico, lote, documento). O id unico da
transacao do banco, quando existe, vai em `id_transacao`: no BB ele so e gerado
em D-1, e usa-lo como identidade faria a mesma linha mudar de id no dia
seguinte e entrar duplicada na importacao.
"""

from abc import ABC
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Flag, auto
from typing import Annotated, Any, ClassVar

from pydantic import BeforeValidator, Field, field_validator

from brbank._compat import StrEnum
from brbank.erros import NaoSuportado
from brbank.modelos import Valor, _mascarar_bruta, _Modelo
from brbank.pagamento import ContaBancaria

_CENTAVO = Decimal("0.01")


class CapacidadeExtrato(Flag):
    NENHUMA = 0
    LANCAMENTOS = auto()
    SALDO = auto()


class Sinal(StrEnum):
    CREDITO = "C"
    DEBITO = "D"


class TipoSaldo(StrEnum):
    ANTERIOR = "ANTERIOR"
    FINAL = "FINAL"


class LancamentoExtrato(_Modelo):
    """Um movimento da conta. Valor sempre positivo; o sentido vem de `sinal`."""

    banco: str
    id_lancamento: str = Field(min_length=1)
    id_transacao: str | None = None
    data: date
    valor: Valor = Field(gt=0)
    sinal: Sinal
    descricao: str
    historico: str | None = None
    documento: str | None = None
    complemento: str | None = Field(default=None, repr=False)
    contrapartida_documento: str | None = Field(default=None, repr=False)
    contrapartida_banco: str | None = None
    contrapartida_agencia: str | None = Field(default=None, repr=False)
    contrapartida_conta: str | None = Field(default=None, repr=False)
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)

    @field_validator("valor")
    @classmethod
    def _centavos(cls, v: Decimal) -> Decimal:
        return v.quantize(_CENTAVO, rounding=ROUND_HALF_UP)

    @property
    def valor_assinado(self) -> Decimal:
        return self.valor if self.sinal is Sinal.CREDITO else -self.valor


class SaldoExtrato(_Modelo):
    data: date
    tipo: TipoSaldo
    # Com sinal: negativo quando a conta esta devedora.
    valor: Valor


class PaginaExtrato(_Modelo):
    lancamentos: tuple[LancamentoExtrato, ...] = ()
    saldos: tuple[SaldoExtrato, ...] = ()
    cursor_proximo: str | None = None


class AdapterExtrato(ABC):
    """Extrato no banco. A implementacao padrao levanta `NaoSuportado`."""

    capacidades_extrato: ClassVar[CapacidadeExtrato] = CapacidadeExtrato.NENHUMA
    banco: ClassVar[str]

    def suporta_extrato(self, capacidade: CapacidadeExtrato) -> bool:
        return capacidade in self.capacidades_extrato

    def exigir_extrato(self, capacidade: CapacidadeExtrato) -> None:
        if not self.suporta_extrato(capacidade):
            nome = getattr(self, "banco", type(self).__name__)
            raise NaoSuportado(f"{nome} nao declara extrato {capacidade.name}")

    async def listar_lancamentos(
        self,
        conta: ContaBancaria,
        desde: date,
        ate: date,
        cursor: str | None = None,
    ) -> PaginaExtrato:
        """Uma pagina do periodo. `cursor_proximo` None indica a ultima."""
        self.exigir_extrato(CapacidadeExtrato.LANCAMENTOS)
        raise NaoSuportado("listar_lancamentos nao implementado")
