"""Safrapay (gateway do grupo Safra): boleto, Pix e webhook pela API de pagamentos v2.

Escrito a partir da documentacao do portal https://developers.safrapay.com.br (lida
em 2026-11-19; a documentacao vem aberta, sem login). Nada aqui rodou contra o
Safrapay: `validado_sandbox = False`. Contrato e duvidas em `docs/safrapay-duvidas.md`.

O que muda em relacao a um banco:

- O MerchantToken do lojista (Portal Safrapay, Configuracoes > Chave de Acesso) e
  trocado em `POST /v2/merchant/auth` por um JWT de 30 minutos. Sem mTLS.
- Uma cobranca (`charge`) leva uma transacao. O `id_banco` e o id da cobranca
  (UUID); o nosso numero e o `transactionId` do boleto.
- O meio sai da configuracao (`meio`): `BOLETO` ou `PIX`. O boleto nao traz QR Pix
  (sem hibrido) e o QR do Pix vale 30 minutos, sem vencimento (sem cobv).
- Nao ha chave de idempotencia na API: a `chave_idempotencia` vai em
  `merchantChargeId` e e consultada antes de criar; achada, vira `Duplicado`. O
  `POST` de emissao nao repete sozinho.
- Falha de validacao pode vir com HTTP 200 e `success: false`: manda o corpo.
- Multa, juros e desconto sao recusados: a documentacao v2 cita os objetos `fine`
  e `discount` sem unidade nem exemplo. O abatimento sai do valor cobrado.
- Boleto so pode ser cancelado a partir do dia seguinte a emissao (erro 195), e
  cancelar Pix pago e devolver o dinheiro: a baixa de cobranca paga e recusada.
- Webhook `Created`/`Updated` em PascalCase; a autenticidade e o MerchantToken em
  Base64 no `Authorization`, conferido quando `conferir_webhook` (padrao).
"""

import asyncio
import base64
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import ArmazemToken, ChaveParaToken
from brbank.capacidades import Capacidade
from brbank.erros import Duplicado, NaoSuportado, Validacao
from brbank.mascara import mascarar
from brbank.modelos import (
    CanalLiquidacao,
    EventoCanonico,
    Instrucao,
    MotivoBaixa,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import SEM_RETRY, LimitadorTaxa, PoliticaRetry, Transporte

URL_PRODUCAO = "https://payment.safrapay.com.br"
URL_SANDBOX = "https://payment-hml.safrapay.com.br"
MEIOS = ("BOLETO", "PIX")
_ORIGEM = {"BOLETO": 7, "PIX": 1}  # enum PaymentSource: 7 BankSlip, 1 Gateway
_BOLETO_MINIMO_CENTAVOS = 500
_MAX_INSTRUCOES = 255
# Erros que a consulta por `merchantChargeId` devolve quando nada coincide:
# 8 "Cobranca nao encontrada" e 0 "Cobranca ou transacao nao encontrada".
_NAO_ENCONTRADA = {0, 8}
_BOLETO_SO_NO_DIA_SEGUINTE = 195

# `transactionStatus`: nome na consulta e numero no webhook. Duas tabelas porque
# `Expired` muda de sentido: o QR Pix expirado nao recebe mais; o boleto vencido
# continua pagavel ate o emissor cancelar (`Canceled`, depois de
# `bankIssuerDaysToCancel`). Estados intermediarios (cancelamento e novo vencimento
# pendentes) ficam REGISTRADO: o titulo ainda pode ser pago.
_COMUM = {
    "PREAUTHORIZED": StatusCanonico.REGISTRADO,
    "1": StatusCanonico.REGISTRADO,
    "PENDING": StatusCanonico.PENDENTE_REGISTRO,
    "4": StatusCanonico.PENDENTE_REGISTRO,
    "PENDINGPAYMENT": StatusCanonico.REGISTRADO,
    "7": StatusCanonico.REGISTRADO,
    "PENDINGNEWDEADLINE": StatusCanonico.REGISTRADO,
    "11": StatusCanonico.REGISTRADO,
    "PENDINGCANCEL": StatusCanonico.REGISTRADO,
    "6": StatusCanonico.REGISTRADO,
    "PAID": StatusCanonico.LIQUIDADO,
    "8": StatusCanonico.LIQUIDADO,
    "CANCELED": StatusCanonico.BAIXADO,
    "5": StatusCanonico.BAIXADO,
    "DENIED": StatusCanonico.REJEITADO,
    "3": StatusCanonico.REJEITADO,
    "ERRORCREATION": StatusCanonico.REJEITADO,
    "9": StatusCanonico.REJEITADO,
    "TIMEOUT": StatusCanonico.REJEITADO,
    "12": StatusCanonico.REJEITADO,
}
TABELA_STATUS = TabelaStatus.criar(
    "2026.11",
    {**_COMUM, "EXPIRED": StatusCanonico.REGISTRADO, "10": StatusCanonico.REGISTRADO},
)
TABELA_STATUS_PIX = TabelaStatus.criar(
    "2026.11",
    {**_COMUM, "EXPIRED": StatusCanonico.BAIXADO, "10": StatusCanonico.BAIXADO},
)
# `paymentType`: texto na consulta, numero no webhook (4 Boleto, 8 Pix).
_TIPOS_BOLETO = {"4", "BOLETO", "BANKSLIP"}
_TIPOS_PIX = {"8", "PIX"}


@dataclass
class SafrapayConfig:
    """Estabelecimento no Safrapay.

    `merchant_token` e a chave do Portal Safrapay (`mk_...`), uma por ambiente.
    `meio` decide o que cada titulo vira: boleto (emissor configurado no cadastro
    do lojista: Bs2, Safra ou Santander) ou Pix com QR de 30 minutos.
    `conferir_webhook` exige, em cada notificacao, o `Authorization` com o
    MerchantToken em Base64, como o Safrapay envia.
    """

    conta: str
    merchant_token: str = field(repr=False)
    ambiente: str = "producao"
    meio: str = "BOLETO"
    conferir_webhook: bool = True
    dias_baixa_automatica: int | None = None
    requisicoes_por_segundo: float | None = None
    armazem: ArmazemToken | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"safrapay: ambiente invalido: {self.ambiente!r}")
        self.meio = (self.meio or "").upper()
        if self.meio not in MEIOS:
            raise Validacao(f"safrapay: meio deve ser um de {', '.join(MEIOS)}")
        if not self.merchant_token or not self.merchant_token.strip():
            raise Validacao("safrapay: merchant_token vazio")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


def _centavos(valor: Decimal) -> int:
    return int((valor * 100).to_integral_value())


def _reais(centavos: Any) -> Decimal | None:
    if centavos is None or isinstance(centavos, bool):
        return None
    try:
        return (Decimal(str(centavos)) / 100).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _momento(texto: Any) -> datetime | None:
    if not texto:
        return None
    try:
        return datetime_iso(str(texto))
    except ValueError:
        return None


def _id_cobranca(id_banco: str) -> str:
    """O id vai no caminho da URL: so UUID passa."""
    try:
        return str(uuid.UUID(str(id_banco)))
    except ValueError:
        raise Validacao(
            f"safrapay: id da cobranca {id_banco!r} nao e um UUID"
        ) from None


def _ler_json(bruta: Any) -> Any:
    if isinstance(bruta, str | bytes):
        try:
            return json.loads(bruta)
        except ValueError:
            return None
    return bruta


def _erros(bruta: Any) -> list[dict[str, Any]]:
    dados = _ler_json(bruta)
    if not isinstance(dados, dict):
        return []
    erros = dados.get("errors")
    return [e for e in erros if isinstance(e, dict)] if isinstance(erros, list) else []


def _codigos(bruta: Any) -> set[int]:
    codigos = set()
    for erro in _erros(bruta):
        try:
            codigos.add(int(erro.get("errorCode")))
        except (TypeError, ValueError):
            continue
    return codigos


def _descrever_erros(bruta: Any) -> str:
    """`errorCode: message` de cada item de `errors`, ou texto vazio."""
    partes = []
    for erro in _erros(bruta):
        codigo = str(
            erro.get("errorCode") if erro.get("errorCode") is not None else "?"
        )
        mensagem = str(erro.get("message") or "").strip()
        partes.append(f"{codigo}: {mensagem}" if mensagem else codigo)
    return "; ".join(partes)


def _tipo_pagamento(transacao: Mapping[str, Any]) -> str | None:
    """`BOLETO`, `PIX`, `OUTRO` (cartao e afins) ou None quando nao veio."""
    bruto = transacao.get("paymentType", transacao.get("PaymentType"))
    if bruto is None:
        return None
    texto = str(bruto).strip().upper()
    if texto in _TIPOS_BOLETO:
        return "BOLETO"
    if texto in _TIPOS_PIX:
        return "PIX"
    return "OUTRO"


def _cliente(pessoa: Pessoa, *, com_endereco: bool) -> dict[str, Any]:
    juridica = pessoa.tipo == "JURIDICA"
    dados: dict[str, Any] = {
        "name": pessoa.nome,
        "entityType": 2 if juridica else 1,
        "documentType": 2 if juridica else 1,
        "document": pessoa.documento,
    }
    if pessoa.email:
        dados["email"] = pessoa.email
    if com_endereco:
        endereco = pessoa.endereco
        if endereco is None:
            raise Validacao("safrapay: o boleto exige o endereco do pagador")
        dados["address"] = {
            "street": endereco.logradouro,
            "number": endereco.numero,
            "neighborhood": endereco.bairro,
            "city": endereco.cidade,
            "state": endereco.uf.upper(),
            "country": "BR",
            "zipCode": endereco.cep,
        }
        if endereco.complemento:
            dados["address"]["complement"] = endereco.complemento
    return dados


class SafrapayAdapter(BankAdapter):
    banco: ClassVar[str] = "safrapay"
    nome_exibicao: ClassVar[str] = "Safrapay (gateway do Safra)"
    capacidades: ClassVar[Capacidade] = (
        Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
        | Capacidade.PIX_COB
        | Capacidade.EMISSAO_ASSINCRONA
        | Capacidade.INSTRUCAO_ASSINCRONA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS
    instrucoes: ClassVar[frozenset[TipoInstrucao]] = frozenset(
        {TipoInstrucao.PRORROGACAO, TipoInstrucao.BAIXA}
    )

    @classmethod
    def instrucoes_da_config(
        cls, ajustes: Mapping[str, object]
    ) -> frozenset[TipoInstrucao] | None:
        """Cobranca Pix nao tem vencimento: so a baixa (cancelamento) vale."""
        if str(ajustes.get("meio") or "BOLETO").upper() == "PIX":
            return frozenset({TipoInstrucao.BAIXA})
        return cls.instrucoes

    def __init__(
        self,
        config: SafrapayConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            autenticador=ChaveParaToken(
                chave=config.merchant_token,
                conta=config.conta,
                url_token=f"{config.url_base}/v2/merchant/auth",
                armazem=config.armazem,
            ),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # --- infraestrutura ---------------------------------------------------

    async def _requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        operacao: str,
        json: Any = None,
        retry: PoliticaRetry | None = None,
    ) -> dict[str, Any]:
        """Requisicao com o corpo ja conferido: `success: false` vira `Validacao`
        mesmo com HTTP 200, e o erro do Safrapay sai legivel (`errorCode`)."""
        try:
            resp = await self._transporte.requisitar(
                metodo, caminho, operacao=operacao, json=json, retry=retry
            )
        except Validacao as erro:
            raise self._erro_legivel(operacao, erro.resposta_bruta, erro) from erro
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                f"safrapay: resposta de {operacao} nao e JSON",
                status_http=resp.status_code,
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                f"safrapay: resposta de {operacao} fora do formato",
                status_http=resp.status_code,
            )
        if dados.get("success") is False:
            raise self._erro_legivel(
                operacao, dados, Validacao("", status_http=resp.status_code)
            )
        return dados

    @staticmethod
    def _erro_legivel(operacao: str, bruta: Any, original: Validacao) -> Validacao:
        codigos = _codigos(bruta)
        detalhe = _descrever_erros(bruta)
        if _BOLETO_SO_NO_DIA_SEGUINTE in codigos:
            mensagem = (
                "safrapay: o boleto so pode ser cancelado a partir do dia seguinte "
                "a emissao (erro 195)"
            )
        elif detalhe:
            mensagem = f"safrapay recusou {operacao}: {detalhe}"
        elif original.mensagem:
            return original
        else:
            mensagem = f"safrapay recusou {operacao}"
        return Validacao(
            mensagem, status_http=original.status_http, resposta_bruta=bruta
        )

    @staticmethod
    def _cobranca(dados: Mapping[str, Any], operacao: str) -> dict[str, Any]:
        cobranca = dados.get("charge")
        if not isinstance(cobranca, dict) or not cobranca.get("id"):
            raise Validacao(
                f"safrapay: resposta de {operacao} sem a cobranca",
                resposta_bruta=dict(dados),
            )
        return cobranca

    @staticmethod
    def _transacao(cobranca: Mapping[str, Any]) -> dict[str, Any]:
        transacoes = cobranca.get("transactions") or cobranca.get("Transactions")
        if not isinstance(transacoes, list) or not transacoes:
            raise Validacao("safrapay: cobranca sem transacao", resposta_bruta=cobranca)
        transacao = transacoes[0]
        if not isinstance(transacao, dict):
            raise Validacao("safrapay: transacao fora do formato")
        return transacao

    def _meio(self, transacao: Mapping[str, Any]) -> str:
        meio = _tipo_pagamento(transacao) or self._config.meio
        if meio == "OUTRO":
            raise Validacao(
                "safrapay: cobranca de cartao fora deste adapter (so boleto e Pix)"
            )
        return meio

    @staticmethod
    def _tabela(meio: str) -> TabelaStatus:
        return TABELA_STATUS_PIX if meio == "PIX" else TABELA_STATUS

    def _montar_registrado(self, cobranca: dict[str, Any]) -> TituloRegistrado:
        transacao = self._transacao(cobranca)
        meio = self._meio(transacao)
        status = self._tabela(meio).traduzir(transacao.get("transactionStatus"))
        pix = meio == "PIX"
        liquidado = status is StatusCanonico.LIQUIDADO
        motivo = None
        if (
            status is StatusCanonico.BAIXADO
            and pix
            and str(transacao.get("transactionStatus")).upper() == "EXPIRED"
        ):
            motivo = MotivoBaixa.DECURSO_PRAZO
        return TituloRegistrado(
            banco=self.banco,
            id_banco=str(cobranca["id"]),
            status=status,
            seu_numero=cobranca.get("merchantChargeId") or None,
            nosso_numero=(
                None
                if pix
                else (transacao.get("transactionId") or transacao.get("bankSlipId"))
            ),
            linha_digitavel=None if pix else transacao.get("digitalLine") or None,
            codigo_barras=None if pix else transacao.get("barcode") or None,
            pix_copia_cola=transacao.get("qrCode") or None if pix else None,
            # A API nao informa o valor pago: so o da cobranca (`amount`). O Pix e
            # pago pelo valor exato; o boleto sem encargos tambem.
            valor_pago=_reais(transacao.get("amount")) if liquidado else None,
            # Boleto sem QR so se paga pelo codigo de barras; Pix, so por Pix.
            canal_liquidacao=(
                (CanalLiquidacao.PIX if pix else CanalLiquidacao.CODIGO_BARRAS)
                if liquidado
                else None
            ),
            motivo_baixa=motivo,
            resposta_bruta=cobranca,
        )

    async def _consultar_bruto(self, id_banco: str) -> dict[str, Any]:
        dados = await self._requisitar(
            "GET", f"v2/charge/{_id_cobranca(id_banco)}", operacao="consultar"
        )
        return self._cobranca(dados, "consultar")

    async def _buscar_por_referencia(self, referencia: str) -> dict[str, Any] | None:
        consulta = urlencode({"merchantChargeId": referencia})
        try:
            dados = await self._requisitar(
                "GET", f"v2/charge?{consulta}", operacao="buscar_referencia"
            )
        except Validacao as erro:
            nao_achou = erro.status_http == 404 or bool(
                _codigos(erro.resposta_bruta) & _NAO_ENCONTRADA
            )
            if nao_achou:
                return None
            raise
        cobranca = dados.get("charge")
        if isinstance(cobranca, dict) and cobranca.get("id"):
            return cobranca
        return None

    # --- cobranca -----------------------------------------------------------

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        recusados = [
            nome
            for nome, valor in (
                ("multa", enc.multa_percentual),
                ("juros", enc.juros_mensal_percentual),
                ("desconto", enc.desconto_percentual),
            )
            if valor
        ]
        if recusados:
            raise Validacao(
                "safrapay: " + ", ".join(recusados) + " ainda nao suportado: a "
                "documentacao v2 nao da unidade nem exemplo (docs/safrapay-duvidas.md)"
            )
        valor = titulo.valor
        if enc.abatimento:
            # Sem campo de abatimento: o valor cobrado ja sai reduzido.
            valor = valor - enc.abatimento
            if valor <= 0:
                raise Validacao(
                    "safrapay: abatimento maior ou igual ao valor do titulo"
                )
        centavos = _centavos(valor)
        boleto = cfg.meio == "BOLETO"
        if boleto and centavos < _BOLETO_MINIMO_CENTAVOS:
            raise Validacao("safrapay: o boleto minimo e de R$ 5,00")
        transacao: dict[str, Any] = {"amount": centavos}
        cobranca: dict[str, Any] = {
            "merchantChargeId": titulo.chave_idempotencia,
            "source": _ORIGEM[cfg.meio],
            "customer": _cliente(titulo.pagador, com_endereco=boleto),
            "transactions": [transacao],
        }
        if boleto:
            cobranca["deadline"] = titulo.vencimento.isoformat()
            if titulo.mensagens:
                transacao["instructions"] = " ".join(titulo.mensagens)[:_MAX_INSTRUCOES]
            dias = (
                titulo.dias_baixa_automatica
                if titulo.dias_baixa_automatica is not None
                else cfg.dias_baixa_automatica
            )
            if dias is not None:
                cobranca["bankIssuerDaysToCancel"] = dias
        return {"charge": cobranca}

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        existente = await self._buscar_por_referencia(titulo.chave_idempotencia)
        if existente is not None:
            raise Duplicado(
                "safrapay: ja existe cobranca com esta chave de idempotencia",
                id_banco=str(existente["id"]),
                resposta_bruta=existente,
            )
        rota = "v2/charge/boleto" if self._config.meio == "BOLETO" else "v2/charge/pix"
        # Sem chave de idempotencia no Safrapay: repetir o POST depois de um
        # timeout poderia emitir duas cobrancas.
        dados = await self._requisitar(
            "POST",
            rota,
            operacao="registrar",
            json=self._montar_payload(titulo),
            retry=SEM_RETRY,
        )
        registrado = self._montar_registrado(self._cobranca(dados, "registrar"))
        return registrado.model_copy(update={"seu_numero": titulo.seu_numero})

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        return self._montar_registrado(await self._consultar_bruto(id_banco))

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        tipo = instrucao.tipo
        aceitas = self.instrucoes_da_config({"meio": self._config.meio}) or frozenset()
        if tipo not in aceitas:
            raise NaoSuportado(f"safrapay: instrucao {tipo.value} nao suportada")
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        self.exigir(Capacidade.ALTERACAO)
        if instrucao.nova_data_vencimento is None:
            raise Validacao("prorrogacao exige nova_data_vencimento")
        await self._requisitar(
            "PUT",
            "v2/charge/boleto",
            operacao="instrucao",
            json={
                "chargeId": _id_cobranca(id_banco),
                "newDueDate": instrucao.nova_data_vencimento.isoformat(),
            },
        )
        # O banco confirma depois (`PendingNewDeadline`): a consulta diz onde esta.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        """`PUT /v2/charge/cancelation/{id}`. Em cobranca paga isso devolveria o
        dinheiro (estorno do Pix), entao a baixa de titulo liquidado e recusada.
        O cancelamento pode ficar pendente: devolve o que a consulta mostrar."""
        atual = await self.consultar_titulo(id_banco)
        if atual.status is StatusCanonico.LIQUIDADO:
            raise Validacao("safrapay: cobranca paga nao se baixa (seria estorno)")
        if atual.status is StatusCanonico.BAIXADO:
            return atual
        await self._requisitar(
            "PUT",
            f"v2/charge/cancelation/{_id_cobranca(id_banco)}",
            operacao="baixar",
            json={},
        )
        return await self.consultar_titulo(id_banco)

    async def obter_pdf(self, id_banco: str) -> bytes:
        """`GET /v2/charge/{id}/boleto/{transactionId}`: rota publica do gateway,
        sem token (o par de ids e o segredo; nao registrar a URL em log)."""
        cobranca = await self._consultar_bruto(id_banco)
        transacao = self._transacao(cobranca)
        if self._meio(transacao) != "BOLETO":
            raise Validacao("safrapay: cobranca Pix nao tem boleto em PDF")
        numero = transacao.get("transactionId") or transacao.get("bankSlipId")
        if not numero or not str(numero).isalnum():
            raise Validacao("safrapay: cobranca sem o numero do boleto")
        url = f"{self._config.url_base}/v2/charge/{cobranca['id']}/boleto/{numero}"
        try:
            resp = await self._transporte.client.get(url, follow_redirects=True)
        except httpx.TransportError as exc:
            raise Validacao(
                f"safrapay: falha ao baixar o PDF ({type(exc).__name__})"
            ) from exc
        if resp.status_code >= 400 or not resp.content.startswith(b"%PDF"):
            raise Validacao(
                "safrapay: resposta sem PDF do boleto", status_http=resp.status_code
            )
        return resp.content

    # --- webhook -------------------------------------------------------------

    def _conferir_autorizacao(self, cabecalhos: Mapping[str, str] | None) -> None:
        if not self._config.conferir_webhook:
            return
        recebido = {k.lower(): v for k, v in (cabecalhos or {}).items()}.get(
            "authorization", ""
        )
        recebido = str(recebido).strip()
        if recebido.lower().startswith("basic "):
            recebido = recebido[6:].strip()
        esperado = base64.b64encode(self._config.merchant_token.encode()).decode()
        if not recebido or not hmac.compare_digest(
            recebido.encode(), esperado.encode()
        ):
            raise Validacao(
                "safrapay: webhook sem o MerchantToken da conta no Authorization"
            )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Uma cobranca por notificacao (`ChargeId`, `Transactions[]`).

        O tipo sai do `TransactionStatus` numerico. Cartao, status desconhecido e
        corpo sem cobranca devolvem lista vazia, nunca erro: falhas repetidas
        poem o webhook em `Failed` no Safrapay. Nao confirma pagamento: o
        chamador reconsulta.
        """
        self._conferir_autorizacao(cabecalhos)
        dados = _ler_json(corpo)
        if not isinstance(dados, dict):
            raise Validacao("safrapay: webhook fora do formato")
        id_cobranca = dados.get("ChargeId")
        transacoes = dados.get("Transactions")
        if not id_cobranca or not isinstance(transacoes, list) or not transacoes:
            return []
        transacao = transacoes[0]
        if not isinstance(transacao, dict):
            return []
        meio = _tipo_pagamento(transacao) or self._config.meio
        if meio == "OUTRO":
            return []
        codigo = transacao.get("TransactionStatus")
        try:
            tipo = self._tabela(meio).traduzir(None if codigo is None else str(codigo))
        except Validacao:
            return []
        liquidado = tipo is StatusCanonico.LIQUIDADO
        return [
            EventoCanonico(
                banco=self.banco,
                referencia=str(id_cobranca),
                tipo=tipo,
                ocorrido_em=_momento(dados.get("DateTime")),
                valor_pago=_reais(transacao.get("Amount")) if liquidado else None,
                canal_liquidacao=(
                    (
                        CanalLiquidacao.PIX
                        if meio == "PIX"
                        else CanalLiquidacao.CODIGO_BARRAS
                    )
                    if liquidado
                    else None
                ),
                resposta_bruta=mascarar(dados),
            )
        ]
