"""DDA (Debito Direto Autorizado): boletos registrados contra o CNPJ da empresa.

E o lado de *pagar* do banco: o adapter lista os boletos que os fornecedores
emitiram contra a empresa (pull) e avisa dos novos pelo webhook. Nada aqui
paga: pagar e outro dominio (ordem de pagamento por arquivo ou, depois,
`AdapterPagamento`).

Segue o padrao de `recorrencia.py`: contrato opcional ao lado de
`BankAdapter`, com modelo, evento e capacidade proprios. O chamador descobre o
suporte por `isinstance(adapter, AdapterDda)` e por `suporta_dda(...)`.
Os metodos nao suportados levantam `NaoSuportado`.

O boleto DDA nao tem nosso numero nem titulo previo no Odoo: a identidade e a
dupla (CNPJ do pagador, linha digitavel normalizada).
"""

import hashlib
import json
import re
from abc import ABC
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Flag, auto
from typing import Annotated, Any, ClassVar

from pydantic import BeforeValidator, Field, field_validator

from brbank._compat import StrEnum
from brbank.erros import NaoSuportado
from brbank.instrumento import (  # noqa: F401 (continuam importaveis daqui)
    codigo_barras_da_linha,
    normalizar_linha_digitavel,
)
from brbank.modelos import Valor, _mascarar_bruta, _Modelo

_CENTAVO = Decimal("0.01")
_PAGADOR = re.compile(r"[A-Z0-9]{14}")


class CapacidadeDda(Flag):
    NENHUMA = 0
    ATIVACAO = auto()
    CONSULTA = auto()
    RESUMO = auto()
    ALTERACAO = auto()
    WEBHOOK = auto()


class StatusDda(StrEnum):
    ABERTO = "ABERTO"
    PAGO = "PAGO"
    CANCELADO = "CANCELADO"
    BAIXADO = "BAIXADO"
    RECUSADO = "RECUSADO"


class TipoEventoDda(StrEnum):
    DEBITO_NOVO = "DEBITO_NOVO"
    DEBITO_ALTERADO = "DEBITO_ALTERADO"
    DEBITO_PAGO = "DEBITO_PAGO"
    DDA_ATIVADO = "DDA_ATIVADO"
    DDA_DESATIVADO = "DDA_DESATIVADO"


# --- modelos -----------------------------------------------------------------


def _normalizar_pagador(valor: Any) -> Any:
    if isinstance(valor, str):
        return re.sub(r"[^0-9A-Za-z]", "", valor).upper()
    return valor


class DebitoDda(_Modelo):
    """Um boleto a pagar visto pelo DDA. PII fora do repr."""

    banco: str
    id_banco: str
    # CNPJ da empresa (14 caracteres, alfanumerico aceito); so o formato e conferido.
    cnpj_pagador: Annotated[str, BeforeValidator(_normalizar_pagador)] = Field(
        repr=False
    )
    linha_digitavel: str = Field(repr=False)
    codigo_barras: str | None = Field(default=None, repr=False)
    beneficiario_documento: str = Field(repr=False)
    beneficiario_nome: str = Field(repr=False)
    beneficiario_fantasia: str | None = Field(default=None, repr=False)
    sacador_documento: str | None = Field(default=None, repr=False)
    sacador_nome: str | None = Field(default=None, repr=False)
    banco_emissor: str | None = None
    numero_documento: str | None = Field(default=None, repr=False)
    valor: Valor = Field(gt=0)
    desconto: Valor | None = Field(default=None, ge=0)
    juros: Valor | None = Field(default=None, ge=0)
    multa: Valor | None = Field(default=None, ge=0)
    vencimento: date | None = None
    data_limite: date | None = None
    status: StatusDda = StatusDda.ABERTO
    atualizado_em: datetime | None = None
    # Ja mascarada: a biblioteca nunca guarda PII em claro.
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)

    @field_validator("cnpj_pagador")
    @classmethod
    def _formato_pagador(cls, v: str) -> str:
        if not _PAGADOR.fullmatch(v):
            raise ValueError("cnpj_pagador deve ter 14 caracteres (A-Z, 0-9)")
        return v

    @field_validator("linha_digitavel")
    @classmethod
    def _linha(cls, v: str) -> str:
        # Aqui so forma (digitos e tamanho): o banco e a fonte, e recusar o boleto
        # por DV perderia a divida. Quem quiser conferir usa a funcao com DV.
        return normalizar_linha_digitavel(v, validar_dv=False)

    @field_validator("beneficiario_documento")
    @classmethod
    def _beneficiario(cls, v: str) -> str:
        doc = re.sub(r"[^0-9A-Za-z]", "", v).upper()
        if not doc:
            raise ValueError("beneficiario_documento vazio")
        return doc

    @field_validator("valor", "desconto", "juros", "multa")
    @classmethod
    def _centavos(cls, v: Decimal | None) -> Decimal | None:
        return None if v is None else v.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


class PaginaDebitos(_Modelo):
    itens: tuple[DebitoDda, ...] = ()
    cursor_proximo: str | None = None


class ResumoDda(_Modelo):
    quantidade: int = Field(ge=0)
    valor_total: Valor = Field(ge=0)
    por_status: dict[StatusDda, int] = Field(default_factory=dict)


class EventoDda(_Modelo):
    banco: str
    id_evento: str = Field(min_length=1)
    tipo: TipoEventoDda
    cnpj_pagador: str | None = Field(default=None, repr=False)
    debitos: tuple[DebitoDda, ...] = ()
    ocorrido_em: datetime | None = None
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = Field(default_factory=dict)

    @property
    def chave_dedup(self) -> tuple[str, str]:
        return (self.banco, self.id_evento)


def hash_corpo(corpo: Any) -> str:
    """Id estavel de um evento sem id proprio: hash do corpo normalizado."""
    texto = json.dumps(corpo, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(texto.encode()).hexdigest()[:32]


# --- contrato ---------------------------------------------------------------


class AdapterDda(ABC):
    """DDA no banco. Implementado junto com `BankAdapter` (ou sozinho, num agregador).

    Cada metodo exige a capacidade correspondente; a implementacao padrao
    levanta `NaoSuportado`, e o adapter sobrescreve o que declara.
    """

    capacidades_dda: ClassVar[CapacidadeDda] = CapacidadeDda.NENHUMA
    banco: ClassVar[str]

    def suporta_dda(self, capacidade: CapacidadeDda) -> bool:
        return capacidade in self.capacidades_dda

    def exigir_dda(self, capacidade: CapacidadeDda) -> None:
        if not self.suporta_dda(capacidade):
            nome = getattr(self, "banco", type(self).__name__)
            raise NaoSuportado(f"{nome} nao declara DDA {capacidade.name}")

    async def ativar_dda(self, cnpj: str) -> None:
        self.exigir_dda(CapacidadeDda.ATIVACAO)
        raise NaoSuportado("ativar_dda nao implementado")

    async def desativar_dda(self, cnpj: str) -> None:
        self.exigir_dda(CapacidadeDda.ATIVACAO)
        raise NaoSuportado("desativar_dda nao implementado")

    async def listar_debitos(
        self,
        cnpj: str,
        desde: date | None = None,
        cursor: str | None = None,
    ) -> PaginaDebitos:
        """Uma pagina de boletos. `cursor_proximo` None indica a ultima."""
        self.exigir_dda(CapacidadeDda.CONSULTA)
        raise NaoSuportado("listar_debitos nao implementado")

    async def resumo_debitos(self, cnpj: str) -> ResumoDda:
        self.exigir_dda(CapacidadeDda.RESUMO)
        raise NaoSuportado("resumo_debitos nao implementado")

    async def alterar_debito(
        self, cnpj: str, id_banco: str, **campos: Any
    ) -> DebitoDda:
        self.exigir_dda(CapacidadeDda.ALTERACAO)
        raise NaoSuportado("alterar_debito nao implementado")

    def parse_webhook_dda(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoDda]:
        """Eventos de DDA do webhook. Os que nao sao de DDA viram lista vazia."""
        self.exigir_dda(CapacidadeDda.WEBHOOK)
        raise NaoSuportado("parse_webhook_dda nao implementado")
