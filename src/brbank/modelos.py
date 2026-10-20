"""Modelo canonico (B1). Dinheiro e Decimal; PII fica fora do repr."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from brbank._compat import StrEnum
from brbank.documento import normalizar_documento, tipo_documento
from brbank.mascara import mascarar
from brbank.status import StatusCanonico

_CENTAVO = Decimal("0.01")


def _sem_float(valor: Any) -> Any:
    if isinstance(valor, float):
        raise ValueError("use Decimal ou str, nunca float")
    return valor


Valor = Annotated[Decimal, BeforeValidator(_sem_float)]


def _mascarar_bruta(valor: Any) -> Any:
    return mascarar(valor) if isinstance(valor, dict) else valor


class _Modelo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Endereco(_Modelo):
    logradouro: str = Field(repr=False)
    numero: str = Field(default="SN", repr=False)
    complemento: str = Field(default="", repr=False)
    bairro: str = Field(repr=False)
    cidade: str = Field(repr=False)
    uf: str = Field(min_length=2, max_length=2)
    cep: str = Field(repr=False)

    @field_validator("uf")
    @classmethod
    def _uf(cls, v: str) -> str:
        return v.upper()

    @field_validator("cep")
    @classmethod
    def _cep(cls, v: str) -> str:
        digitos = "".join(c for c in v if c.isdigit())
        if len(digitos) != 8:
            raise ValueError("CEP deve ter 8 digitos")
        return digitos


class Pessoa(_Modelo):
    nome: str = Field(min_length=1, repr=False)
    documento: str = Field(repr=False)
    endereco: Endereco | None = Field(default=None, repr=False)
    email: str | None = Field(default=None, repr=False)

    @field_validator("documento")
    @classmethod
    def _documento(cls, v: str) -> str:
        return normalizar_documento(v)

    @property
    def tipo(self) -> str:
        return tipo_documento(self.documento)


class Encargos(_Modelo):
    """Percentuais em unidade de percentual (2 = 2%)."""

    multa_percentual: Valor | None = Field(default=None, ge=0)
    juros_mensal_percentual: Valor | None = Field(default=None, ge=0)
    desconto_percentual: Valor | None = Field(default=None, ge=0)
    desconto_dias_antes: int | None = Field(default=None, ge=0)
    abatimento: Valor | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _desconto_completo(self) -> "Encargos":
        if (self.desconto_percentual is None) != (self.desconto_dias_antes is None):
            raise ValueError("desconto exige percentual e dias antes juntos")
        return self


class Titulo(_Modelo):
    chave_idempotencia: str = Field(min_length=1)
    seu_numero: str = Field(min_length=1)
    valor: Valor = Field(gt=0)
    vencimento: date
    pagador: Pessoa
    beneficiario: Pessoa | None = None
    encargos: Encargos = Field(default_factory=Encargos)
    mensagens: tuple[str, ...] = ()
    dias_baixa_automatica: int | None = Field(default=None, ge=0)

    @field_validator("valor")
    @classmethod
    def _centavos(cls, v: Decimal) -> Decimal:
        return v.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


class TituloRegistrado(_Modelo):
    banco: str
    id_banco: str
    status: StatusCanonico
    seu_numero: str | None = None
    nosso_numero: str | None = None
    linha_digitavel: str | None = None
    codigo_barras: str | None = None
    pix_copia_cola: str | None = Field(default=None, repr=False)
    txid: str | None = None
    valor_pago: Valor | None = None
    data_pagamento: date | None = None
    # Ja mascarada: a biblioteca nunca guarda PII em claro.
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = {}


class TipoInstrucao(StrEnum):
    PRORROGACAO = "PRORROGACAO"
    ABATIMENTO = "ABATIMENTO"
    PROTESTO = "PROTESTO"
    NEGATIVACAO = "NEGATIVACAO"
    BAIXA = "BAIXA"


class Instrucao(_Modelo):
    chave_idempotencia: str = Field(min_length=1)
    tipo: TipoInstrucao
    nova_data_vencimento: date | None = None
    valor: Valor | None = Field(default=None, gt=0)


class EventoCanonico(_Modelo):
    banco: str
    referencia: str
    tipo: StatusCanonico
    ocorrido_em: datetime | None = None
    valor_pago: Valor | None = None
    resposta_bruta: Annotated[
        dict[str, Any], BeforeValidator(_mascarar_bruta), Field(repr=False)
    ] = {}

    @property
    def chave_dedup(self) -> tuple[str, str, StatusCanonico]:
        return (self.banco, self.referencia, self.tipo)
