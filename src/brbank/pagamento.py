"""Pagamento pela API do banco: a empresa paga boletos, guias e Pix a partir da conta.

E o outro lado do DDA: o DDA lista o que a empresa deve, este dominio paga.
Segue o padrao de `dda.py` e `recorrencia.py`: contrato opcional, com modelo,
capacidade e estado proprios; o chamador descobre o suporte por
`isinstance(adapter, AdapterPagamento)` e `suporta_pagamento(...)`.

Pagamento so existe em lote (decisao 9 do PRD-DDA): o banco aceita ou recusa
cada linha, o estado anda por linha e de forma assincrona, e liberar e um passo
proprio (a aprovacao do lote). Um lote tem um so tipo de pagamento, porque os
bancos separam as rotas por tipo.

Execucao duravel: o numero do lote pode ser reservado antes do envio
(`reservar_numero_lote`), para que o chamador o grave antes de chamar o banco;
um timeout no envio e resultado desconhecido e se resolve consultando o lote por
esse numero, nunca reenviando.

Lote local (`CapacidadePagamento.LOTE_LOCAL`): bancos cuja API paga um por vez
(Pix copia e cola do Sicoob, por exemplo) tambem implementam `pagar_lote`, mas o
lote so existe do lado de ca. O adapter envia linha a linha, o numero do lote e
gerado localmente, nao ha `consultar_lote` nem liberacao, e o estado de cada
linha se acompanha por `consultar_pagamento` (ou webhook). Se uma linha fica sem
resposta (timeout no envio de um pagamento que executa na hora), ela volta
`INDETERMINADO` e as seguintes nao sao enviadas (`RECUSADO`, motivo
`NAO_ENVIADO`): o chamador confere antes de reenviar.
"""

import re
from abc import ABC
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Flag, auto
from typing import Annotated, Any, ClassVar

from pydantic import BeforeValidator, Field, field_validator, model_validator

from brbank._compat import StrEnum
from brbank.erros import NaoSuportado
from brbank.instrumento import ler_brcode
from brbank.modelos import Valor, _mascarar_bruta, _Modelo

_CENTAVO = Decimal("0.01")


class CapacidadePagamento(Flag):
    NENHUMA = 0
    BOLETO = auto()
    GUIA = auto()
    PIX = auto()
    TRANSFERENCIA = auto()
    RESERVA_NUMERO = auto()
    LIBERACAO = auto()
    CANCELAMENTO = auto()
    WEBHOOK = auto()
    # Pix copia e cola (BR Code, QR estatico ou dinamico).
    PIX_QRCODE = auto()
    # O banco paga um por vez: lote local, sem consultar_lote (ver docstring).
    LOTE_LOCAL = auto()


class TipoPagamento(StrEnum):
    BOLETO = "BOLETO"
    # Concessionaria e tributo com codigo de barras (arrecadacao, comeca com 8).
    GUIA = "GUIA"
    PIX = "PIX"
    TRANSFERENCIA = "TRANSFERENCIA"
    # Pix copia e cola: o BR Code vai inteiro ao banco, que le e valida o QR.
    PIX_QRCODE = "PIX_QRCODE"


class StatusPagamento(StrEnum):
    # Aceito pelo banco, aguardando liberacao ou a data.
    ACEITO = "ACEITO"
    RECUSADO = "RECUSADO"
    AGENDADO = "AGENDADO"
    EM_PROCESSAMENTO = "EM_PROCESSAMENTO"
    PAGO = "PAGO"
    REJEITADO = "REJEITADO"
    CANCELADO = "CANCELADO"
    DEVOLVIDO = "DEVOLVIDO"
    # Sem resposta no envio de um pagamento que executa na hora: pode ter sido
    # pago. Conferir (consulta, extrato ou webhook) antes de reenviar.
    INDETERMINADO = "INDETERMINADO"


class StatusLote(StrEnum):
    ACEITO = "ACEITO"
    ACEITO_PARCIAL = "ACEITO_PARCIAL"
    RECUSADO = "RECUSADO"
    PENDENTE_LIBERACAO = "PENDENTE_LIBERACAO"
    EM_PROCESSAMENTO = "EM_PROCESSAMENTO"
    PROCESSADO = "PROCESSADO"
    REJEITADO = "REJEITADO"


class TipoChavePix(StrEnum):
    TELEFONE = "TELEFONE"
    EMAIL = "EMAIL"
    DOCUMENTO = "DOCUMENTO"
    ALEATORIA = "ALEATORIA"


def _so_alfanumerico(valor: Any) -> Any:
    if isinstance(valor, str):
        return re.sub(r"[^0-9A-Za-z]", "", valor).upper()
    return valor


Documento = Annotated[str, BeforeValidator(_so_alfanumerico)]


class ContaBancaria(_Modelo):
    """Conta de debito ou de credito. Agencia e conta sem DV; DV a parte."""

    agencia: str = Field(pattern=r"^\d{1,5}$")
    conta: str = Field(pattern=r"^\d{1,13}$", repr=False)
    digito: str = Field(pattern=r"^[0-9Xx]$", repr=False)
    # Banco de destino (ISPB de 8 digitos ou COMPE de 3); vazio na conta de debito.
    ispb: str | None = Field(default=None, pattern=r"^\d{8}$")
    compe: str | None = Field(default=None, pattern=r"^\d{3}$")
    poupanca: bool = False


class DestinoPix(_Modelo):
    """Chave Pix ou dados bancarios (um dos dois)."""

    tipo_chave: TipoChavePix | None = None
    chave: str | None = Field(default=None, repr=False)
    conta: ContaBancaria | None = None
    documento: Documento | None = Field(default=None, repr=False)
    # Nome do favorecido: o Inter o exige no Pix por dados bancarios.
    nome: str | None = Field(default=None, max_length=140, repr=False)

    @model_validator(mode="after")
    def _chave_ou_conta(self) -> "DestinoPix":
        if (self.chave is None) == (self.conta is None):
            raise ValueError("destino Pix exige chave ou conta (um dos dois)")
        if self.chave is not None and self.tipo_chave is None:
            raise ValueError("chave Pix exige tipo_chave")
        if self.conta is not None and not (self.conta.ispb or self.conta.compe):
            raise ValueError("Pix por conta exige ispb ou compe do banco de destino")
        return self


class Pagamento(_Modelo):
    """Uma linha do lote. PII fora do repr."""

    chave_idempotencia: str = Field(min_length=1, max_length=64)
    tipo: TipoPagamento
    valor: Valor = Field(gt=0)
    data: date
    descricao: str | None = Field(default=None, max_length=40)
    # BOLETO e GUIA: codigo de barras de 44 digitos (nunca a linha digitavel).
    codigo_barras: str | None = Field(default=None, pattern=r"^\d{44}$", repr=False)
    beneficiario_documento: Documento | None = Field(default=None, repr=False)
    valor_nominal: Valor | None = Field(default=None, gt=0)
    desconto: Valor | None = Field(default=None, ge=0)
    juros_multa: Valor | None = Field(default=None, ge=0)
    nosso_numero: str | None = Field(default=None, repr=False)
    # Vencimento do titulo (boleto, guia, QR de cobranca com vencimento). Opcional:
    # o Inter o exige no boleto (sem ele, sai do fator de vencimento do codigo).
    vencimento: date | None = None
    # PIX
    pix: DestinoPix | None = None
    # TRANSFERENCIA
    conta_credito: ContaBancaria | None = None
    favorecido_documento: Documento | None = Field(default=None, repr=False)
    # PIX_QRCODE: o Pix copia e cola inteiro. `valor` e o valor original (confere
    # com o do QR quando ele traz valor); com `pagar_com_juros_multa` o banco
    # soma os encargos do QR de cobranca com vencimento ate a data do pagamento.
    brcode: str | None = Field(default=None, repr=False)
    pagar_com_juros_multa: bool = False

    @field_validator("valor", "valor_nominal", "desconto", "juros_multa")
    @classmethod
    def _centavos(cls, v: Decimal | None) -> Decimal | None:
        return None if v is None else v.quantize(_CENTAVO, rounding=ROUND_HALF_UP)

    @field_validator("brcode")
    @classmethod
    def _brcode_confere(cls, v: str | None) -> str | None:
        # Confere CRC e formato; ValueError vira ValidationError.
        return None if v is None else ler_brcode(v).payload

    @model_validator(mode="after")
    def _campos_do_tipo(self) -> "Pagamento":
        t = self.tipo
        if t in (TipoPagamento.BOLETO, TipoPagamento.GUIA) and not self.codigo_barras:
            raise ValueError(f"pagamento {t.value} exige codigo_barras")
        if t is TipoPagamento.BOLETO and not self.beneficiario_documento:
            raise ValueError("pagamento de boleto exige beneficiario_documento")
        if t is TipoPagamento.GUIA and self.codigo_barras[0] != "8":
            raise ValueError("guia de arrecadacao comeca com 8")
        if t is TipoPagamento.BOLETO and self.codigo_barras[0] == "8":
            raise ValueError("codigo de barras com 8 na frente e guia, nao boleto")
        if t is TipoPagamento.PIX and self.pix is None:
            raise ValueError("pagamento Pix exige pix")
        if t is TipoPagamento.TRANSFERENCIA and (
            self.conta_credito is None or not self.favorecido_documento
        ):
            raise ValueError("transferencia exige conta_credito e favorecido_documento")
        if (self.brcode is not None) != (t is TipoPagamento.PIX_QRCODE):
            raise ValueError("brcode vai so no pagamento PIX_QRCODE, e ele o exige")
        if self.brcode is not None:
            lido = ler_brcode(self.brcode)
            # QR estatico com valor: o valor e fixo. No dinamico vale o do JSON
            # assinado no PSP do recebedor, que so o banco pagador le e valida.
            if not lido.dinamico and lido.valor not in (None, self.valor):
                raise ValueError("valor diferente do valor fixo do QR estatico")
        return self


class LotePagamento(_Modelo):
    chave_idempotencia: str = Field(min_length=1, max_length=64)
    tipo: TipoPagamento
    conta_debito: ContaBancaria
    pagamentos: tuple[Pagamento, ...] = Field(min_length=1)
    # Numero reservado no banco antes do envio (ver `reservar_numero_lote`).
    numero: str | None = None

    @model_validator(mode="after")
    def _um_tipo_e_chaves_unicas(self) -> "LotePagamento":
        if any(p.tipo is not self.tipo for p in self.pagamentos):
            raise ValueError("lote tem um so tipo de pagamento")
        chaves = [p.chave_idempotencia for p in self.pagamentos]
        if len(set(chaves)) != len(chaves):
            raise ValueError("chave_idempotencia repetida no lote")
        return self

    @property
    def valor_total(self) -> Decimal:
        return sum((p.valor for p in self.pagamentos), Decimal(0))


class PagamentoRegistrado(_Modelo):
    banco: str
    id_banco: str
    status: StatusPagamento
    chave_idempotencia: str | None = None
    valor: Valor | None = None
    data: date | None = None
    # Codigos do banco quando recusado, rejeitado ou devolvido.
    motivos: tuple[str, ...] = ()
    autenticacao: str | None = None
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)


class LoteRegistrado(_Modelo):
    banco: str
    id_banco: str
    tipo: TipoPagamento
    status: StatusLote
    pagamentos: tuple[PagamentoRegistrado, ...] = ()
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)

    @property
    def aceitos(self) -> tuple[PagamentoRegistrado, ...]:
        return tuple(
            p for p in self.pagamentos if p.status is not StatusPagamento.RECUSADO
        )


def status_lote_local(pagamentos: tuple[PagamentoRegistrado, ...]) -> StatusLote:
    """Estado de um lote local (LOTE_LOCAL) a partir do estado das linhas."""
    if not pagamentos:
        raise ValueError("lote sem linhas")
    recusados = sum(p.status is StatusPagamento.RECUSADO for p in pagamentos)
    if recusados == len(pagamentos):
        return StatusLote.RECUSADO
    if recusados:
        return StatusLote.ACEITO_PARCIAL
    finais = (
        StatusPagamento.PAGO,
        StatusPagamento.REJEITADO,
        StatusPagamento.CANCELADO,
        StatusPagamento.DEVOLVIDO,
    )
    if all(p.status in finais for p in pagamentos):
        return StatusLote.PROCESSADO
    return StatusLote.EM_PROCESSAMENTO


# --- contrato ---------------------------------------------------------------


class AdapterPagamento(ABC):
    """Pagamento pela API do banco. Cada metodo exige a capacidade correspondente;
    a implementacao padrao levanta `NaoSuportado`."""

    capacidades_pagamento: ClassVar[CapacidadePagamento] = CapacidadePagamento.NENHUMA
    banco: ClassVar[str]

    def suporta_pagamento(self, capacidade: CapacidadePagamento) -> bool:
        return capacidade in self.capacidades_pagamento

    def exigir_pagamento(self, capacidade: CapacidadePagamento) -> None:
        if not self.suporta_pagamento(capacidade):
            nome = getattr(self, "banco", type(self).__name__)
            raise NaoSuportado(f"{nome} nao declara pagamento {capacidade.name}")

    async def reservar_numero_lote(self) -> str:
        self.exigir_pagamento(CapacidadePagamento.RESERVA_NUMERO)
        raise NaoSuportado("reservar_numero_lote nao implementado")

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Envia o lote. O banco devolve aceite ou recusa por linha; nada e pago
        antes de `liberar_lote` quando o banco declara LIBERACAO."""
        raise NaoSuportado("pagar_lote nao implementado")

    async def consultar_lote(
        self, id_banco: str, tipo: TipoPagamento
    ) -> LoteRegistrado:
        raise NaoSuportado("consultar_lote nao implementado")

    async def liberar_lote(self, id_banco: str) -> None:
        self.exigir_pagamento(CapacidadePagamento.LIBERACAO)
        raise NaoSuportado("liberar_lote nao implementado")

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        raise NaoSuportado("consultar_pagamento nao implementado")

    async def cancelar_pagamentos(
        self, conta_debito: ContaBancaria, ids_banco: tuple[str, ...]
    ) -> None:
        """Cancela pagamentos ainda nao liberados."""
        self.exigir_pagamento(CapacidadePagamento.CANCELAMENTO)
        raise NaoSuportado("cancelar_pagamentos nao implementado")

    def parse_webhook_pagamento(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[PagamentoRegistrado]:
        self.exigir_pagamento(CapacidadePagamento.WEBHOOK)
        raise NaoSuportado("parse_webhook_pagamento nao implementado")
