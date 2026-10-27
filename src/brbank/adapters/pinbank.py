"""PinBank, API CashIn de boleto (OAuth2 password, payload cifrado AES-CBC).

Portado de `l10n_br_payment_boleto_pinbank` (kmee-odoo-addons, branch 16.0; hoje
`l10n_br_payment_pinbank`).
Diferencas deliberadas em relacao ao modulo Odoo:

- A referencia do titulo e o `NossoNumero` devolvido pelo banco (`id_banco`),
  nunca o identificador do chamador.
- Payload sem a chave esperada vira `Validacao`, nunca `KeyError`.
- Centavos por `Decimal`, sem `int(x * 100)` de float.
- A consulta e a fonte da verdade: a baixa devolve o estado consultado, e o
  registro e sempre `PENDENTE_REGISTRO` (como o `last_action=CREATED` do Odoo).
- A chave de idempotencia evita o segundo POST do mesmo titulo no processo.
  A garantia entre processos e do `Orquestrador` (repositorio de chaves).
- Sem dados ficticios: o que o titulo nao traz nao e enviado.

Duvidas em aberto, nao inventadas (ver `docs/pinbank-duvidas.md`): unidade de
Juros e Multa, desconto e abatimento, numero e complemento do endereco, campo
de idempotencia do lado do banco, PDF fora da geracao, webhook.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2Password
from brbank.capacidades import Capacidade
from brbank.envelope import EnvelopeAesCbc
from brbank.erros import NaoSuportado, Validacao
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte

URL_PRODUCAO = "https://pinbank.com.br/services/api"
URL_TESTE = "https://dev.pinbank.com.br/services/api"

ENDPOINT_TOKEN = "/token"
ENDPOINT_GERAR = "/CashIn/GerarBoletoEncrypted"
ENDPOINT_BAIXAR = "/CashIn/SolicitarBaixaBoletoEncrypted"
ENDPOINT_CONSULTAR = "/CashIn/ConsultarBoletoEncrypted"

TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "PENDENTE": StatusCanonico.PENDENTE_REGISTRO,
        "REGISTRADO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        # No modulo Odoo BAIXA cancela a transacao.
        "BAIXA": StatusCanonico.BAIXADO,
    },
)

_CENTAVO = Decimal("0.01")


@dataclass
class PinbankConfig:
    conta: str
    usuario: str
    # Senha do usuario e tambem a chave AES do payload (16, 24 ou 32 bytes
    # em UTF-8), como no modulo Odoo.
    senha: str = field(repr=False)
    codigo_canal: int
    codigo_cliente: int
    origem_requisicao: str
    ambiente: str = "producao"
    requisicoes_por_segundo: float | None = None

    @property
    def url_base(self) -> str:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente PinBank invalido: {self.ambiente!r}")
        return URL_TESTE if self.ambiente == "sandbox" else URL_PRODUCAO


def _centavos(valor: Decimal) -> int:
    return int((valor.quantize(_CENTAVO) * 100).to_integral_value())


def _de_centavos(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return (Decimal(str(valor)) / 100).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do PinBank") from exc


def _numero(texto: str, campo: str) -> int:
    if not texto.isdigit():
        raise Validacao(f"PinBank exige {campo} numerico")
    return int(texto)


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return datetime.strptime(str(texto)[:8], "%Y%m%d").date()
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do PinBank") from exc


def _sacado(pessoa: Pessoa) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao("pagador sem endereco: o PinBank exige o endereco")
    return {
        "CpfCnpj": _numero(pessoa.documento, "CPF/CNPJ"),
        "Nome": pessoa.nome,
        "Endereco": endereco.logradouro,
        "Bairro": endereco.bairro,
        "Cidade": endereco.cidade,
        "Cep": int(endereco.cep),
        "Uf": endereco.uf,
    }


class PinbankAdapter(BankAdapter):
    banco: ClassVar[str] = "pinbank"
    capacidades: ClassVar[Capacidade] = Capacidade.BAIXA | Capacidade.EMISSAO_ASSINCRONA
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: PinbankConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        auth = OAuth2Password(
            conta=config.conta,
            url_token=f"{config.url_base}{ENDPOINT_TOKEN}",
            usuario=config.usuario,
            senha=config.senha,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            autenticador=auth,
            envelope=EnvelopeAesCbc(config.senha.encode("utf-8")),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )
        self._registrados: dict[str, TituloRegistrado] = {}
        self._travas: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._transporte.aclose()

    def _cabecalhos(self) -> dict[str, str]:
        return {
            "RequestOrigin": self._config.origem_requisicao,
            "UserName": self._config.usuario,
        }

    def _chaves_conta(self) -> dict[str, int]:
        return {
            "CodigoCanal": self._config.codigo_canal,
            "CodigoCliente": self._config.codigo_cliente,
        }

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        enc = titulo.encargos
        # Unidade de Juros/Multa/desconto nao confirmada (o Odoo multiplica por
        # 10000 sem documentar a base): recusar e melhor que cobrar errado.
        for nome, valor in (
            ("multa", enc.multa_percentual),
            ("juros", enc.juros_mensal_percentual),
            ("desconto", enc.desconto_percentual),
            ("abatimento", enc.abatimento),
            ("baixa automatica", titulo.dias_baixa_automatica),
        ):
            if valor is not None:
                raise Validacao(
                    f"PinBank: {nome} ainda nao suportado pelo adapter "
                    "(contrato do campo em aberto)"
                )
        dados: dict[str, Any] = {
            **self._chaves_conta(),
            "DataVencimento": titulo.vencimento.strftime("%Y%m%d"),
            "Valor": _centavos(titulo.valor),
            "DadosSacado": _sacado(titulo.pagador),
            "RetornarBase64": True,
            "Instrucoes": " ".join(titulo.mensagens),
        }
        if titulo.pagador.email:
            dados["Email"] = titulo.pagador.email
        return {"Data": dados}

    @staticmethod
    def _dados(resp: httpx.Response) -> dict[str, Any]:
        """Extrai `Data` da resposta decifrada; ausencia e Validacao."""
        try:
            corpo = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do PinBank nao e JSON", status_http=resp.status_code
            ) from exc
        dados = corpo.get("Data") if isinstance(corpo, dict) else None
        if not isinstance(dados, dict) or not dados:
            raise Validacao(
                "resposta do PinBank sem Data",
                status_http=resp.status_code,
                resposta_bruta=corpo,
            )
        return dados

    async def _chamar(
        self, endpoint: str, operacao: str, payload: dict[str, Any]
    ) -> httpx.Response:
        return await self._transporte.requisitar(
            "POST",
            endpoint,
            operacao=operacao,
            json=payload,
            cabecalhos=self._cabecalhos(),
        )

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        chave = titulo.chave_idempotencia
        trava = self._travas.setdefault(chave, asyncio.Lock())
        async with trava:
            if chave in self._registrados:
                return self._registrados[chave]
            resp = await self._chamar(
                ENDPOINT_GERAR, "registrar", self._montar_payload(titulo)
            )
            dados = self._dados(resp)
            nosso_numero = dados.get("NossoNumero")
            if not nosso_numero:
                raise Validacao(
                    "resposta do PinBank sem NossoNumero",
                    status_http=resp.status_code,
                    resposta_bruta=dados,
                )
            registrado = TituloRegistrado(
                banco=self.banco,
                id_banco=str(nosso_numero),
                status=StatusCanonico.PENDENTE_REGISTRO,
                seu_numero=titulo.seu_numero,
                nosso_numero=str(nosso_numero),
                linha_digitavel=dados.get("LinhaDigitavel"),
                codigo_barras=dados.get("CodigoBarras"),
                resposta_bruta=dados,
            )
            self._registrados[chave] = registrado
            return registrado

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._chamar(
            ENDPOINT_CONSULTAR,
            "consultar",
            {"Data": {**self._chaves_conta(), "NossoNumero": id_banco}},
        )
        dados = self._dados(resp)
        status = self.tabela_status.traduzir(dados.get("Status"))
        liquidado = status is StatusCanonico.LIQUIDADO
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            nosso_numero=dados.get("NossoNumero") or id_banco,
            linha_digitavel=dados.get("LinhaDigitavel"),
            codigo_barras=dados.get("CodigoBarras"),
            valor_pago=_de_centavos(dados.get("ValorPago")) if liquidado else None,
            data_pagamento=_data(dados.get("DataPagamento")) if liquidado else None,
            resposta_bruta=dados,
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        raise NaoSuportado("pinbank: instrucoes nao implementadas")

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._chamar(
            ENDPOINT_BAIXAR,
            "baixar",
            {"Data": {**self._chaves_conta(), "NossoNumero": id_banco}},
        )
        # A consulta e a fonte da verdade: a baixa pode ainda nao ter sido
        # processada, e entao o estado devolvido nao sera BAIXADO.
        return await self.consultar_titulo(id_banco)

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        # O modulo Odoo so consulta (cron); nao ha contrato de webhook.
        raise NaoSuportado("pinbank: sem webhook, use polling por consultar_titulo")
