"""Recorrencia (assinaturas): protocolo opcional, ao lado de `BankAdapter`.

Uma assinatura e outro objeto, com ciclo de vida proprio: quem a implementa e
quem gera e cobra cada ciclo sozinho (hoje o Asaas; o Pix Automatico do BACEN
e o segundo candidato). Cada cobranca gerada continua sendo um
`TituloRegistrado`, e chega ao chamador pelo webhook de titulos com
`EventoCanonico.id_recorrencia` preenchido.

O chamador descobre a capacidade por `Capacidade.RECORRENCIA` (e `CHECKOUT`
para o cartao pela pagina hospedada) e por `isinstance(adapter,
AdapterRecorrencia)`.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

from brbank._compat import StrEnum
from brbank.modelos import (
    Encargos,
    Pessoa,
    TituloRegistrado,
    Valor,
    _mascarar_bruta,
    _Modelo,
)


class Ciclo(StrEnum):
    SEMANAL = "SEMANAL"
    QUINZENAL = "QUINZENAL"
    MENSAL = "MENSAL"
    BIMESTRAL = "BIMESTRAL"
    TRIMESTRAL = "TRIMESTRAL"
    SEMESTRAL = "SEMESTRAL"
    ANUAL = "ANUAL"


class MeioCobranca(StrEnum):
    BOLETO = "BOLETO"
    PIX = "PIX"
    CARTAO = "CARTAO"
    # O pagador escolhe o meio na pagina hospedada de cada cobranca.
    INDEFINIDO = "INDEFINIDO"


class StatusAssinatura(StrEnum):
    ATIVA = "ATIVA"
    SUSPENSA = "SUSPENSA"
    ENCERRADA = "ENCERRADA"


class StatusCheckout(StrEnum):
    ATIVO = "ATIVO"
    PAGO = "PAGO"
    CANCELADO = "CANCELADO"
    EXPIRADO = "EXPIRADO"


class TipoEventoRecorrencia(StrEnum):
    CRIADA = "CRIADA"
    ALTERADA = "ALTERADA"
    SUSPENSA = "SUSPENSA"
    ENCERRADA = "ENCERRADA"
    CHECKOUT_PAGO = "CHECKOUT_PAGO"
    CHECKOUT_CANCELADO = "CHECKOUT_CANCELADO"
    CHECKOUT_EXPIRADO = "CHECKOUT_EXPIRADO"


class Assinatura(_Modelo):
    chave_idempotencia: str = Field(min_length=1)
    seu_numero: str = Field(min_length=1)
    pagador: Pessoa
    valor: Valor = Field(gt=0)
    ciclo: Ciclo
    primeiro_vencimento: date
    meio: MeioCobranca = MeioCobranca.INDEFINIDO
    data_fim: date | None = None
    maximo_cobrancas: int | None = Field(default=None, gt=0)
    encargos: Encargos = Field(default_factory=Encargos)
    mensagens: tuple[str, ...] = ()


class AlteracaoAssinatura(_Modelo):
    """So o que vem preenchido muda. `aplicar_nas_pendentes` leva a mudanca
    tambem as cobrancas ja geradas e ainda abertas."""

    valor: Valor | None = Field(default=None, gt=0)
    ciclo: Ciclo | None = None
    proximo_vencimento: date | None = None
    data_fim: date | None = None
    meio: MeioCobranca | None = None
    encargos: Encargos | None = None
    mensagens: tuple[str, ...] | None = None
    aplicar_nas_pendentes: bool = False


class AssinaturaRegistrada(_Modelo):
    banco: str
    id_banco: str
    status: StatusAssinatura
    seu_numero: str | None = None
    valor: Valor | None = None
    ciclo: Ciclo | None = None
    proximo_vencimento: date | None = None
    data_fim: date | None = None
    meio: MeioCobranca | None = None
    # Checkout que deu origem a assinatura, quando houve.
    id_checkout: str | None = None
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)


class Checkout(_Modelo):
    """Pagina hospedada em que o pagador informa o cartao; a assinatura nasce
    no gateway depois que ele conclui."""

    banco: str
    id_banco: str
    url: str = Field(repr=False)
    status: StatusCheckout
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)


class EventoRecorrencia(_Modelo):
    banco: str
    # Id da assinatura, ou do checkout nos eventos CHECKOUT_*.
    referencia: str
    tipo: TipoEventoRecorrencia
    ocorrido_em: datetime | None = None
    id_checkout: str | None = None
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)

    @property
    def chave_dedup(self) -> tuple[str, str, TipoEventoRecorrencia]:
        return (self.banco, self.referencia, self.tipo)


class AdapterRecorrencia(ABC):
    """Assinaturas no gateway. Implementado junto com `BankAdapter`."""

    @abstractmethod
    async def criar_assinatura(self, assinatura: Assinatura) -> AssinaturaRegistrada:
        """Cria a recorrencia. Repetir a mesma chave levanta `Duplicado`."""

    @abstractmethod
    async def consultar_assinatura(self, id_banco: str) -> AssinaturaRegistrada: ...

    @abstractmethod
    async def alterar_assinatura(
        self, id_banco: str, alteracao: AlteracaoAssinatura
    ) -> AssinaturaRegistrada: ...

    @abstractmethod
    async def suspender_assinatura(self, id_banco: str) -> AssinaturaRegistrada:
        """Para de gerar cobrancas; as ja geradas continuam."""

    @abstractmethod
    async def reativar_assinatura(
        self, id_banco: str, proximo_vencimento: date
    ) -> AssinaturaRegistrada: ...

    @abstractmethod
    async def encerrar_assinatura(self, id_banco: str) -> AssinaturaRegistrada:
        """Encerra de vez. O gateway pode remover as cobrancas ainda abertas."""

    @abstractmethod
    async def listar_cobrancas(self, id_banco: str) -> list[TituloRegistrado]:
        """Cobrancas ja geradas (as futuras ainda nao existem)."""

    @abstractmethod
    async def criar_checkout(
        self,
        assinatura: Assinatura,
        *,
        url_sucesso: str,
        url_cancelamento: str,
        url_expiracao: str | None = None,
        meios: Sequence[MeioCobranca] = (MeioCobranca.CARTAO,),
        minutos_validade: int = 60,
    ) -> Checkout:
        """Pagina hospedada para o pagador concluir a assinatura (cartao sem
        dado de cartao no chamador)."""

    @abstractmethod
    def parse_webhook_recorrencia(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoRecorrencia]:
        """Eventos de assinatura e de checkout. Os de cobranca continuam em
        `BankAdapter.parse_webhook`."""
