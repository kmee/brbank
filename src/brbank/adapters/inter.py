"""Banco Inter, API de Cobranca v3 (boleto com Pix, mTLS, OAuth2 por escopo).

Portado de `l10n_br_payment_boleto_inter` (kmee-odoo-addons). Diferencas
deliberadas: sem dados ficticios no lugar de campo ausente (o Odoo mandava
`teste@teste.com` e DDD 31), e o cancelamento usa o motivo pedido pelo
chamador em vez de fixar SUBSTITUICAO.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado, Validacao
from brbank.mascara import mascarar
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

URL_PRODUCAO = "https://cdpj.partners.bancointer.com.br"
URL_SANDBOX = "https://cdpj-sandbox.partners.uatinter.co"
VALOR_MINIMO = Decimal("2.50")
DIAS_AGENDA_PADRAO = 60

ESCOPOS = {
    "cobranca_add": "boleto-cobranca.write",
    "cobranca_cancel": "boleto-cobranca.write",
    "cobranca_get": "boleto-cobranca.read",
}

TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM_PROCESSAMENTO": StatusCanonico.PENDENTE_REGISTRO,
        "A_RECEBER": StatusCanonico.REGISTRADO,
        "ATRASADO": StatusCanonico.REGISTRADO,
        "PROTESTO": StatusCanonico.REGISTRADO,
        "PAGO": StatusCanonico.LIQUIDADO,
        "RECEBIDO": StatusCanonico.LIQUIDADO,
        "MARCADO_RECEBIDO": StatusCanonico.LIQUIDADO,
        "CANCELADO": StatusCanonico.BAIXADO,
        "EXPIRADO": StatusCanonico.BAIXADO,
        # O modulo Odoo ja tratava VENCIDO como encerrado, mantido por paridade.
        "VENCIDO": StatusCanonico.BAIXADO,
        "FALHA_EMISSAO": StatusCanonico.REJEITADO,
    },
)


@dataclass
class InterConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    conta_corrente: str | None = None
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = None

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    return None if valor is None else Decimal(str(valor))


def _pessoa(pessoa: Pessoa, *, papel: str, com_email: bool = False) -> dict:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao(f"{papel} sem endereco: o Inter exige endereco completo")
    dados: dict[str, Any] = {
        "cpfCnpj": pessoa.documento,
        "tipoPessoa": pessoa.tipo,
        "nome": pessoa.nome,
        "endereco": endereco.logradouro,
        "numero": endereco.numero,
        "bairro": endereco.bairro,
        "cidade": endereco.cidade,
        "uf": endereco.uf,
        "cep": endereco.cep,
    }
    if endereco.complemento:
        dados["complemento"] = endereco.complemento
    if com_email and pessoa.email:
        dados["email"] = pessoa.email
    return dados


class InterAdapter(BankAdapter):
    banco: ClassVar[str] = "inter"
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.SO_HIBRIDO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
        | Capacidade.EMISSAO_ASSINCRONA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: InterConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=f"{config.url_base}/oauth/v2/token",
            client_id=config.client_id,
            client_secret=config.client_secret,
            escopos=ESCOPOS,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            ssl_context=ssl_context,
            autenticador=auth,
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    def _cabecalhos(self) -> dict[str, str]:
        if self._config.conta_corrente:
            return {"x-conta-corrente": self._config.conta_corrente}
        return {}

    @staticmethod
    def _montar_payload(titulo: Titulo) -> dict[str, Any]:
        if titulo.valor < VALOR_MINIMO:
            raise Validacao(f"valor minimo do boleto Inter e R$ {VALOR_MINIMO}")
        payload: dict[str, Any] = {
            "seuNumero": titulo.seu_numero,
            "valorNominal": _numero(titulo.valor),
            "dataVencimento": titulo.vencimento.isoformat(),
            "numDiasAgenda": (
                DIAS_AGENDA_PADRAO
                if titulo.dias_baixa_automatica is None
                else titulo.dias_baixa_automatica
            ),
            "pagador": _pessoa(titulo.pagador, papel="pagador", com_email=True),
        }
        if titulo.beneficiario:
            payload["beneficiarioFinal"] = _pessoa(
                titulo.beneficiario, papel="beneficiario"
            )
        enc = titulo.encargos
        if enc.abatimento is not None:
            payload["valorAbatimento"] = _numero(enc.abatimento)
        if enc.desconto_percentual is not None:
            payload["desconto"] = {
                "codigo": "PERCENTUALDATAINFORMADA",
                "taxa": _numero(enc.desconto_percentual),
                "quantidadeDias": enc.desconto_dias_antes,
            }
        if enc.multa_percentual is not None:
            payload["multa"] = {
                "codigo": "PERCENTUAL",
                "taxa": _numero(enc.multa_percentual),
            }
        if enc.juros_mensal_percentual is not None:
            payload["mora"] = {
                "codigo": "TAXAMENSAL",
                "taxa": _numero(enc.juros_mensal_percentual),
            }
        if titulo.mensagens:
            payload["mensagem"] = {
                f"linha{i}": linha for i, linha in enumerate(titulo.mensagens[:5], 1)
            }
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Inter nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao("resposta do Inter fora do formato", status_http=200)
        return dados

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            "cobranca/v3/cobrancas",
            operacao="registrar",
            escopo="cobranca_add",
            json=self._montar_payload(titulo),
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        id_banco = dados.get("codigoSolicitacao")
        if not id_banco:
            raise Validacao(
                "resposta do Inter sem codigoSolicitacao",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        # O Inter confirma so o recebimento do pedido: o registro termina depois.
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.PENDENTE_REGISTRO,
            seu_numero=titulo.seu_numero,
            resposta_bruta=dados,
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "GET",
            f"cobranca/v3/cobrancas/{id_banco}",
            operacao="consultar",
            escopo="cobranca_get",
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        cobranca = dados.get("cobranca") or {}
        boleto = dados.get("boleto") or {}
        pix = dados.get("pix") or {}
        status = self.tabela_status.traduzir(cobranca.get("situacao"))
        liquidado = status is StatusCanonico.LIQUIDADO
        data_situacao = cobranca.get("dataSituacao")
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=cobranca.get("seuNumero"),
            nosso_numero=boleto.get("nossoNumero"),
            linha_digitavel=boleto.get("linhaDigitavel"),
            codigo_barras=boleto.get("codigoBarras"),
            pix_copia_cola=pix.get("pixCopiaECola"),
            txid=pix.get("txid"),
            valor_pago=_dinheiro(cobranca.get("valorTotalRecebido")),
            data_pagamento=(
                date.fromisoformat(data_situacao[:10])
                if liquidado and data_situacao
                else None
            ),
            resposta_bruta=dados,
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        raise NaoSuportado("inter: instrucoes ainda nao implementadas")

    async def baixar_titulo(
        self, id_banco: str, motivo: str = "APEDIDODOCLIENTE"
    ) -> TituloRegistrado:
        await self._transporte.requisitar(
            "POST",
            f"cobranca/v3/cobrancas/{id_banco}/cancelar",
            operacao="baixar",
            escopo="cobranca_cancel",
            json={"motivoCancelamento": motivo},
            cabecalhos=self._cabecalhos(),
        )
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        resp = await self._transporte.requisitar(
            "GET",
            f"cobranca/v3/cobrancas/{id_banco}/pdf",
            operacao="pdf",
            escopo="cobranca_get",
            cabecalhos=self._cabecalhos(),
        )
        # A API responde ora JSON com o PDF em base64, ora o binario.
        if resp.content.startswith(b"%PDF"):
            return resp.content
        try:
            pdf = base64.b64decode(self._json(resp).get("pdf") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Validacao("PDF do Inter corrompido") from exc
        if not pdf.startswith(b"%PDF"):
            raise Validacao("resposta do Inter nao contem PDF")
        return pdf

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook de cobranca do Inter: lista de situacoes por codigoSolicitacao.

        Sem assinatura: o chamador deve reconsultar antes de dar como pago.
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Inter nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        eventos = []
        for item in itens:
            if not isinstance(item, dict) or not item.get("codigoSolicitacao"):
                raise Validacao("webhook do Inter sem codigoSolicitacao")
            momento = item.get("dataHoraSituacao")
            eventos.append(
                EventoCanonico(
                    banco=self.banco,
                    referencia=item["codigoSolicitacao"],
                    tipo=self.tabela_status.traduzir(item.get("situacao")),
                    ocorrido_em=datetime.fromisoformat(momento) if momento else None,
                    valor_pago=_dinheiro(item.get("valorTotalRecebido")),
                    resposta_bruta=mascarar(item),
                )
            )
        return eventos
