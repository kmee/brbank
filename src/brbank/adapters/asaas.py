"""Asaas, API v3 (gateway: boleto, Pix, fatura hospedada e assinaturas).

Escrito a partir da documentacao publica (docs.asaas.com) e da OpenAPI oficial
(`https://www.asaas.com/openApi/document?version=3&languageCode=pt-BR`) em
2026-10-21. Nao ha modulo Odoo de origem. Duvidas em `docs/asaas-duvidas.md`.

O que muda em relacao a um banco:

- A cobranca e sincrona: nasce `PENDING` ja com linha digitavel, e o Asaas gera
  o nosso numero.
- A mesma cobranca serve boleto e Pix (`billing_type` da configuracao decide;
  `UNDEFINED` deixa o pagador escolher na fatura hospedada).
- Nao ha chave de idempotencia na API: a `chave_idempotencia` vai em
  `externalReference` e e consultada antes de criar; achada, vira `Duplicado`.
- A cobranca exige um cliente (`cus_`) cadastrado antes, e o Asaas nao
  deduplica clientes: o adapter busca pelo CPF/CNPJ antes de criar.
- Evento de webhook desconhecido, ou de outra categoria, devolve lista vazia,
  nunca erro: 15 respostas 400 seguidas pausam a fila do Asaas.
- Assinaturas (`brbank.recorrencia`): o Asaas gera a cobranca de cada ciclo
  (40 dias antes do vencimento, por padrao da conta) e avisa com
  `PAYMENT_CREATED`; cartao recorrente so pelo checkout hospedado, para que
  nenhum dado de cartao passe pela biblioteca.
"""

import asyncio
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import ChaveApi
from brbank.capacidades import Capacidade
from brbank.erros import Duplicado, NaoSuportado, Validacao
from brbank.mascara import mascarar
from brbank.modelos import (
    Encargos,
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.recorrencia import (
    AdapterRecorrencia,
    AlteracaoAssinatura,
    Assinatura,
    AssinaturaRegistrada,
    Checkout,
    Ciclo,
    EventoRecorrencia,
    MeioCobranca,
    StatusAssinatura,
    StatusCheckout,
    TipoEventoRecorrencia,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte

URL_PRODUCAO = "https://api.asaas.com/v3"
URL_SANDBOX = "https://api-sandbox.asaas.com/v3"
URL_CHECKOUT_PRODUCAO = "https://asaas.com/checkoutSession/show?id="
URL_CHECKOUT_SANDBOX = "https://sandbox.asaas.com/checkoutSession/show?id="
# Meios aceitos na configuracao. CREDIT_CARD exige dado de cartao no corpo
# (escopo PCI) e fica fora: cartao entra pela fatura hospedada (UNDEFINED).
MEIOS = ("BOLETO", "PIX", "UNDEFINED")
_MEIOS_COM_BOLETO = ("BOLETO", "UNDEFINED")
_MAX_DESCRICAO = 500
_LIMITE_PAGINA = 100
# A cota do Asaas e de 25.000 requisicoes por 12 h (0,58 req/s de media) e a
# emissao custa 3 a 4 chamadas: 5 req/s segura um lote sem estourar o pico.
_REQUISICOES_POR_SEGUNDO = 5.0
# PNG 1x1 transparente: o checkout exige uma imagem por item.
IMAGEM_PADRAO_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8"
    "AAAAASUVORK5CYII="
)


def _versao() -> str:
    try:
        return version("brbank")
    except PackageNotFoundError:  # pragma: no cover - fonte sem instalar
        return "dev"


USER_AGENT = f"brbank/{_versao()}"

# Decisoes do Mileo em 2026-10-21 (kmee-brain, ANALISE-ASAAS.md): CONFIRMED
# (pago, saldo ainda bloqueado) e liquidado; REFUNDED e baixado, ate existir um
# status canonico de estorno. `deleted: true` e tratado antes da tabela.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "PENDING": StatusCanonico.REGISTRADO,
        "OVERDUE": StatusCanonico.REGISTRADO,
        "DUNNING_REQUESTED": StatusCanonico.REGISTRADO,
        "DUNNING_RECEIVED": StatusCanonico.REGISTRADO,
        "AWAITING_RISK_ANALYSIS": StatusCanonico.PENDENTE_REGISTRO,
        "CONFIRMED": StatusCanonico.LIQUIDADO,
        "RECEIVED": StatusCanonico.LIQUIDADO,
        "RECEIVED_IN_CASH": StatusCanonico.LIQUIDADO,
        "REFUND_REQUESTED": StatusCanonico.LIQUIDADO,
        "REFUND_IN_PROGRESS": StatusCanonico.LIQUIDADO,
        "CHARGEBACK_REQUESTED": StatusCanonico.LIQUIDADO,
        "CHARGEBACK_DISPUTE": StatusCanonico.LIQUIDADO,
        "AWAITING_CHARGEBACK_REVERSAL": StatusCanonico.LIQUIDADO,
        "REFUNDED": StatusCanonico.BAIXADO,
    },
)

# Eventos de cobranca que nao mudam o estado do titulo: nao viram evento.
_EVENTOS_SEM_ESTADO = frozenset(
    {
        "PAYMENT_BANK_SLIP_VIEWED",
        "PAYMENT_CHECKOUT_VIEWED",
        "PAYMENT_ANTICIPATED",
        "PAYMENT_SPLIT_CANCELLED",
        "PAYMENT_SPLIT_DIVERGENCE_BLOCK",
        "PAYMENT_SPLIT_DIVERGENCE_BLOCK_FINISHED",
        "PAYMENT_SPLIT_DONE",
    }
)

CICLOS = {
    Ciclo.SEMANAL: "WEEKLY",
    Ciclo.QUINZENAL: "BIWEEKLY",
    Ciclo.MENSAL: "MONTHLY",
    Ciclo.BIMESTRAL: "BIMONTHLY",
    Ciclo.TRIMESTRAL: "QUARTERLY",
    Ciclo.SEMESTRAL: "SEMIANNUALLY",
    Ciclo.ANUAL: "YEARLY",
}
_CICLOS_DO_ASAAS = {v: k for k, v in CICLOS.items()}
MEIOS_RECORRENCIA = {
    MeioCobranca.BOLETO: "BOLETO",
    MeioCobranca.PIX: "PIX",
    MeioCobranca.CARTAO: "CREDIT_CARD",
    MeioCobranca.INDEFINIDO: "UNDEFINED",
}
_MEIOS_DO_ASAAS = {v: k for k, v in MEIOS_RECORRENCIA.items()}
_MEIOS_CHECKOUT = {MeioCobranca.CARTAO: "CREDIT_CARD", MeioCobranca.PIX: "PIX"}
_STATUS_ASSINATURA = {
    "ACTIVE": StatusAssinatura.ATIVA,
    "INACTIVE": StatusAssinatura.SUSPENSA,
    "EXPIRED": StatusAssinatura.ENCERRADA,
}
_STATUS_CHECKOUT = {
    "ACTIVE": StatusCheckout.ATIVO,
    "PAID": StatusCheckout.PAGO,
    "CANCELED": StatusCheckout.CANCELADO,
    "EXPIRED": StatusCheckout.EXPIRADO,
}
_EVENTOS_RECORRENCIA = {
    "SUBSCRIPTION_CREATED": TipoEventoRecorrencia.CRIADA,
    "SUBSCRIPTION_UPDATED": TipoEventoRecorrencia.ALTERADA,
    "SUBSCRIPTION_INACTIVATED": TipoEventoRecorrencia.SUSPENSA,
    "SUBSCRIPTION_DELETED": TipoEventoRecorrencia.ENCERRADA,
    "CHECKOUT_PAID": TipoEventoRecorrencia.CHECKOUT_PAGO,
    "CHECKOUT_CANCELED": TipoEventoRecorrencia.CHECKOUT_CANCELADO,
    "CHECKOUT_EXPIRED": TipoEventoRecorrencia.CHECKOUT_EXPIRADO,
}


@dataclass
class AsaasConfig:
    """Conta do Asaas.

    `api_key` e a chave gerada no painel (`$aact_prod_...` em producao,
    `$aact_hmlg_...` no sandbox; contas e chaves sao independentes).
    `billing_type` decide o meio de cada cobranca avulsa. `webhook_auth_token`
    e o `authToken` cadastrado no webhook do Asaas; quando preenchido, o
    adapter exige o header `asaas-access-token` igual em cada notificacao.
    `desligar_notificacoes` cria o cliente com `notificationDisabled`, porque
    quem avisa o pagador e o sistema chamador, nao o Asaas.
    `imagem_checkout_base64` e a imagem do item na pagina do checkout (PNG ou
    JPEG em base64); vazia, vai um pixel transparente.
    """

    conta: str
    api_key: str = field(repr=False)
    ambiente: str = "producao"
    billing_type: str = "BOLETO"
    webhook_auth_token: str | None = field(default=None, repr=False)
    desligar_notificacoes: bool = True
    dias_baixa_automatica: int | None = None
    imagem_checkout_base64: str | None = None
    requisicoes_por_segundo: float | None = _REQUISICOES_POR_SEGUNDO

    def __post_init__(self) -> None:
        self.billing_type = (self.billing_type or "").upper()
        if self.billing_type not in MEIOS:
            raise Validacao(
                f"asaas: billing_type deve ser um de {', '.join(MEIOS)}; "
                "cartao entra pela fatura hospedada (UNDEFINED)"
            )
        if not self.api_key or not self.api_key.strip():
            raise Validacao("asaas: api_key vazia")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_checkout(self) -> str:
        return (
            URL_CHECKOUT_SANDBOX
            if self.ambiente == "sandbox"
            else URL_CHECKOUT_PRODUCAO
        )


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None:
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


def _momento(texto: Any) -> datetime | None:
    """`dateCreated` chega como "AAAA-MM-DD HH:MM:SS", sem fuso (hora de
    Brasilia, pelo que a documentacao mostra); fica naive."""
    if not texto:
        return None
    try:
        return datetime_iso(str(texto))
    except ValueError:
        return None


def _descricao(seu_numero: str, mensagens: Sequence[str]) -> str:
    """`description` guarda o numero do chamador na primeira linha."""
    return "\n".join((seu_numero, *mensagens))[:_MAX_DESCRICAO]


def _primeira_linha(dados: Mapping[str, Any]) -> str | None:
    descricao = str(dados.get("description") or "")
    return descricao.splitlines()[0] if descricao else None


def _encargos_payload(enc: Encargos) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if enc.multa_percentual is not None:
        payload["fine"] = {"value": _numero(enc.multa_percentual), "type": "PERCENTAGE"}
    if enc.juros_mensal_percentual is not None:
        payload["interest"] = {"value": _numero(enc.juros_mensal_percentual)}
    if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
        payload["discount"] = {
            "value": _numero(enc.desconto_percentual),
            "type": "PERCENTAGE",
            "dueDateLimitDays": enc.desconto_dias_antes,
        }
    return payload


def _endereco_payload(pessoa: Pessoa) -> dict[str, Any]:
    dados: dict[str, Any] = {}
    if pessoa.email:
        dados["email"] = pessoa.email
    endereco = pessoa.endereco
    if endereco is not None:
        dados["address"] = endereco.logradouro
        dados["addressNumber"] = endereco.numero
        dados["province"] = endereco.bairro
        dados["postalCode"] = endereco.cep
        if endereco.complemento:
            dados["complement"] = endereco.complemento[:255]
    return dados


def _cliente_payload(pessoa: Pessoa, desligar_notificacoes: bool) -> dict[str, Any]:
    return {
        "name": pessoa.nome,
        "cpfCnpj": pessoa.documento,
        "notificationDisabled": desligar_notificacoes,
        **_endereco_payload(pessoa),
    }


class AsaasAdapter(BankAdapter, AdapterRecorrencia):
    banco: ClassVar[str] = "asaas"
    # Hibrido depende de a conta ter chave Pix cadastrada (o adapter nao
    # enxerga isso): sem chave, o QR vem vazio e o boleto segue sem Pix.
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
        | Capacidade.PIX_COB
        | Capacidade.PIX_COBV
        | Capacidade.RECORRENCIA
        | Capacidade.CHECKOUT
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: AsaasConfig,
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
            autenticador=ChaveApi("access_token", config.api_key),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )
        # Documento -> id do cliente no Asaas, dentro de uma mesma instancia:
        # um lote de titulos do mesmo pagador nao reconsulta a cada titulo.
        self._clientes: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # --- infraestrutura -------------------------------------------------

    @staticmethod
    def _cabecalhos() -> dict[str, str]:
        # User-Agent e obrigatorio para contas criadas desde 06/2024.
        return {"User-Agent": USER_AGENT}

    async def _requisitar(
        self, metodo: str, caminho: str, *, operacao: str, json: Any = None
    ) -> httpx.Response:
        """`Transporte.requisitar` com o erro do Asaas legivel.

        O corpo de erro e `{"errors": [{"code", "description"}]}`; o codigo
        e o que o chamador consegue tratar, por isso vai na mensagem.
        """
        try:
            return await self._transporte.requisitar(
                metodo,
                caminho,
                operacao=operacao,
                json=json,
                cabecalhos=self._cabecalhos(),
            )
        except Validacao as erro:
            detalhe = _descrever_erros(erro.resposta_bruta)
            if not detalhe:
                raise
            raise Validacao(
                f"asaas recusou {operacao}: {detalhe}",
                status_http=erro.status_http,
                resposta_bruta=erro.resposta_bruta,
            ) from erro

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Asaas nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao("resposta do Asaas fora do formato", status_http=200)
        return dados

    async def _listar(
        self, recurso: str, filtros: Mapping[str, Any], *, limite: int
    ) -> list[dict[str, Any]]:
        """Pagina `recurso` ate `limite` itens (ou ate `hasMore` virar falso)."""
        itens: list[dict[str, Any]] = []
        deslocamento = 0
        while True:
            consulta = urlencode(
                {
                    **filtros,
                    "limit": min(_LIMITE_PAGINA, limite - len(itens)),
                    "offset": deslocamento,
                }
            )
            resp = await self._requisitar(
                "GET", f"{recurso}?{consulta}", operacao=f"listar_{recurso}"
            )
            dados = self._json(resp)
            pagina = [i for i in dados.get("data") or [] if isinstance(i, dict)]
            itens.extend(pagina)
            deslocamento += len(pagina)
            if not pagina or not dados.get("hasMore") or len(itens) >= limite:
                return itens[:limite]

    async def _listar_um(
        self, recurso: str, filtros: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        itens = await self._listar(recurso, filtros, limite=1)
        return itens[0] if itens else None

    def _conferir_token_webhook(self, cabecalhos: Mapping[str, str] | None) -> None:
        esperado = self._config.webhook_auth_token
        if not esperado:
            return
        recebido = {k.lower(): v for k, v in (cabecalhos or {}).items()}.get(
            "asaas-access-token"
        )
        if not recebido or not hmac.compare_digest(recebido, esperado):
            raise Validacao("asaas: webhook com asaas-access-token desconhecido")

    @staticmethod
    def _corpo_webhook(corpo: bytes | str | list | dict) -> dict[str, Any]:
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Asaas nao e JSON") from exc
        if not isinstance(corpo, dict):
            raise Validacao("webhook do Asaas fora do formato")
        return corpo

    # --- cliente ----------------------------------------------------------

    async def _garantir_cliente(self, pessoa: Pessoa) -> str:
        """Id do cliente no Asaas para o documento do pagador.

        O Asaas nao deduplica: busca por `cpfCnpj` antes de criar. Dois
        titulos do mesmo pagador emitidos em paralelo, antes de o primeiro
        `POST` responder, ainda podem criar dois clientes; a duplicata e
        inofensiva para a cobranca.
        """
        documento = pessoa.documento
        if documento in self._clientes:
            return self._clientes[documento]
        achado = await self._listar_um("customers", {"cpfCnpj": documento})
        if achado and achado.get("id"):
            cliente_id = str(achado["id"])
        else:
            resp = await self._requisitar(
                "POST",
                "customers",
                operacao="criar_cliente",
                json=_cliente_payload(pessoa, self._config.desligar_notificacoes),
            )
            cliente_id = str(self._json(resp).get("id") or "")
            if not cliente_id:
                raise Validacao(
                    "resposta do Asaas sem id do cliente", status_http=resp.status_code
                )
        self._clientes[documento] = cliente_id
        return cliente_id

    # --- cobranca ---------------------------------------------------------

    def _montar_payload(self, titulo: Titulo, cliente_id: str) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        valor = titulo.valor
        if enc.abatimento is not None:
            # O Asaas nao tem abatimento: o valor cobrado ja sai reduzido.
            valor = valor - enc.abatimento
            if valor <= 0:
                raise Validacao("asaas: abatimento maior ou igual ao valor do titulo")
        payload: dict[str, Any] = {
            "customer": cliente_id,
            "billingType": cfg.billing_type,
            "value": _numero(valor),
            "dueDate": titulo.vencimento.isoformat(),
            "description": _descricao(titulo.seu_numero, titulo.mensagens),
            "externalReference": titulo.chave_idempotencia,
            **_encargos_payload(enc),
        }
        dias = (
            titulo.dias_baixa_automatica
            if titulo.dias_baixa_automatica is not None
            else cfg.dias_baixa_automatica
        )
        if dias is not None and cfg.billing_type in _MEIOS_COM_BOLETO:
            payload["daysAfterDueDateToRegistrationCancellation"] = dias
        return payload

    def _status(
        self, dados: Mapping[str, Any], *, estrito: bool
    ) -> StatusCanonico | None:
        if dados.get("deleted") is True:
            return StatusCanonico.BAIXADO
        try:
            return self.tabela_status.traduzir(dados.get("status"))
        except Validacao:
            if estrito:
                raise
            return None

    async def _linha_digitavel(self, id_banco: str) -> dict[str, Any]:
        resp = await self._requisitar(
            "GET",
            f"payments/{id_banco}/identificationField",
            operacao="linha_digitavel",
        )
        return self._json(resp)

    async def _pix(self, id_banco: str, *, obrigatorio: bool) -> str | None:
        """Copia e cola do QR dinamico. Para boleto o QR existe so quando a
        conta tem chave Pix: a recusa vira None, nao erro."""
        try:
            resp = await self._requisitar(
                "GET", f"payments/{id_banco}/pixQrCode", operacao="pix_qrcode"
            )
        except Validacao:
            if obrigatorio:
                raise
            return None
        return self._json(resp).get("payload") or None

    async def _montar_registrado(
        self, id_banco: str, dados: dict[str, Any], *, detalhar: bool = True
    ) -> TituloRegistrado:
        """`detalhar` busca linha digitavel e QR do titulo em aberto (duas
        chamadas a mais); a listagem de uma assinatura dispensa."""
        status = self._status(dados, estrito=True)
        meio = str(dados.get("billingType") or self._config.billing_type)
        linha: dict[str, Any] = {}
        pix = None
        aberto = status in (StatusCanonico.REGISTRADO, StatusCanonico.PENDENTE_REGISTRO)
        if detalhar and aberto:
            if meio in _MEIOS_COM_BOLETO:
                linha = await self._linha_digitavel(id_banco)
            pix = await self._pix(id_banco, obrigatorio=(meio == "PIX"))
        liquidado = status is StatusCanonico.LIQUIDADO
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=_primeira_linha(dados),
            nosso_numero=linha.get("nossoNumero") or dados.get("nossoNumero") or None,
            linha_digitavel=linha.get("identificationField") or None,
            codigo_barras=linha.get("barCode") or None,
            pix_copia_cola=pix,
            valor_pago=_dinheiro(dados.get("value")) if liquidado else None,
            data_pagamento=(
                _data(dados.get("clientPaymentDate") or dados.get("paymentDate"))
                if liquidado
                else None
            ),
            resposta_bruta=dados,
        )

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        existente = await self._listar_um(
            "payments", {"externalReference": titulo.chave_idempotencia}
        )
        if existente and existente.get("id"):
            raise Duplicado(
                "asaas: ja existe cobranca com esta chave de idempotencia",
                id_banco=str(existente["id"]),
                resposta_bruta=existente,
            )
        cliente_id = await self._garantir_cliente(titulo.pagador)
        resp = await self._requisitar(
            "POST",
            "payments",
            operacao="registrar",
            json=self._montar_payload(titulo, cliente_id),
        )
        dados = self._json(resp)
        id_banco = dados.get("id")
        if not id_banco:
            raise Validacao(
                "resposta do Asaas sem id da cobranca",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        registrado = await self._montar_registrado(str(id_banco), dados)
        return registrado.model_copy(update={"seu_numero": titulo.seu_numero})

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._requisitar(
            "GET", f"payments/{id_banco}", operacao="consultar"
        )
        return await self._montar_registrado(id_banco, self._json(resp))

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if tipo not in (TipoInstrucao.PRORROGACAO, TipoInstrucao.ABATIMENTO):
            raise NaoSuportado(f"asaas: instrucao {tipo.value} nao implementada")
        atual = self._json(
            await self._requisitar("GET", f"payments/{id_banco}", operacao="consultar")
        )
        # O PUT exige os tres campos, mesmo quando so um muda.
        corpo: dict[str, Any] = {
            "billingType": atual.get("billingType"),
            "value": atual.get("value"),
            "dueDate": atual.get("dueDate"),
        }
        if tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
            corpo["dueDate"] = instrucao.nova_data_vencimento.isoformat()
        else:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            valor_atual = _dinheiro(atual.get("value")) or Decimal(0)
            novo = valor_atual - instrucao.valor
            if novo <= 0:
                raise Validacao("asaas: abatimento maior ou igual ao valor da cobranca")
            corpo["value"] = _numero(novo)
        await self._requisitar(
            "PUT", f"payments/{id_banco}", operacao="instrucao", json=corpo
        )
        # Linha digitavel e QR mudam com valor e vencimento: reconsulta.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        """`DELETE` remove a cobranca (restauravel no Asaas). Cobranca paga e
        recusada pelo Asaas com 400: estorno e outro fluxo."""
        await self._requisitar("DELETE", f"payments/{id_banco}", operacao="baixar")
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        """O PDF vem de `bankSlipUrl`, URL publica fora da API: baixada com o
        cliente HTTP, sem a chave da conta."""
        dados = self._json(
            await self._requisitar("GET", f"payments/{id_banco}", operacao="consultar")
        )
        url = dados.get("bankSlipUrl")
        if not url:
            raise Validacao("asaas: cobranca sem boleto (bankSlipUrl vazio)")
        try:
            resp = await self._transporte.client.get(
                url, headers=self._cabecalhos(), follow_redirects=True
            )
        except httpx.TransportError as exc:
            raise Validacao(
                f"asaas: falha ao baixar o PDF ({type(exc).__name__})"
            ) from exc
        if resp.status_code >= 400 or not resp.content.startswith(b"%PDF"):
            raise Validacao(
                "resposta do Asaas nao contem PDF", status_http=resp.status_code
            )
        return resp.content

    # --- webhook de cobranca -----------------------------------------------

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Um evento por requisicao, `{id, event, dateCreated, payment{...}}`.

        O tipo canonico sai do `payment.status` (e de `deleted`), nao do nome
        do evento: assim um evento novo do Asaas com status conhecido ainda e
        aproveitado, e um status novo so e ignorado. Nao confirma pagamento:
        o chamador reconsulta. Cobranca gerada por assinatura traz
        `id_recorrencia`.
        """
        self._conferir_token_webhook(cabecalhos)
        corpo = self._corpo_webhook(corpo)
        evento = str(corpo.get("event") or "")
        pagamento = corpo.get("payment")
        if (
            not evento.startswith("PAYMENT_")
            or evento in _EVENTOS_SEM_ESTADO
            or not isinstance(pagamento, dict)
            or not pagamento.get("id")
        ):
            return []
        tipo = self._status(pagamento, estrito=False)
        if tipo is None:
            return []
        return [
            EventoCanonico(
                banco=self.banco,
                referencia=str(pagamento["id"]),
                tipo=tipo,
                ocorrido_em=_momento(corpo.get("dateCreated")),
                valor_pago=(
                    _dinheiro(pagamento.get("value"))
                    if tipo is StatusCanonico.LIQUIDADO
                    else None
                ),
                id_recorrencia=(
                    str(pagamento["subscription"])
                    if pagamento.get("subscription")
                    else None
                ),
                resposta_bruta=mascarar(corpo),
            )
        ]

    # --- assinaturas (brbank.recorrencia) ---------------------------------

    def _assinatura_registrada(self, dados: dict[str, Any]) -> AssinaturaRegistrada:
        if dados.get("deleted") is True:
            status = StatusAssinatura.ENCERRADA
        else:
            status = _STATUS_ASSINATURA.get(str(dados.get("status") or "").upper())
            if status is None:
                raise Validacao(
                    f"asaas: status de assinatura {dados.get('status')!r} desconhecido",
                    resposta_bruta=dados,
                )
        return AssinaturaRegistrada(
            banco=self.banco,
            id_banco=str(dados.get("id") or ""),
            status=status,
            seu_numero=_primeira_linha(dados),
            valor=_dinheiro(dados.get("value")),
            ciclo=_CICLOS_DO_ASAAS.get(str(dados.get("cycle") or "")),
            proximo_vencimento=_data(dados.get("nextDueDate")),
            data_fim=_data(dados.get("endDate")),
            meio=_MEIOS_DO_ASAAS.get(str(dados.get("billingType") or "")),
            id_checkout=str(dados["checkoutSession"])
            if dados.get("checkoutSession")
            else None,
            resposta_bruta=dados,
        )

    async def _consultar_assinatura_bruta(self, id_banco: str) -> dict[str, Any]:
        resp = await self._requisitar(
            "GET", f"subscriptions/{id_banco}", operacao="consultar_assinatura"
        )
        return self._json(resp)

    async def criar_assinatura(self, assinatura: Assinatura) -> AssinaturaRegistrada:
        if assinatura.meio is MeioCobranca.CARTAO:
            raise NaoSuportado(
                "asaas: assinatura de cartao so pelo checkout hospedado "
                "(criar_checkout); dado de cartao nao passa pela biblioteca"
            )
        existente = await self._listar_um(
            "subscriptions", {"externalReference": assinatura.chave_idempotencia}
        )
        if existente and existente.get("id"):
            raise Duplicado(
                "asaas: ja existe assinatura com esta chave de idempotencia",
                id_banco=str(existente["id"]),
                resposta_bruta=existente,
            )
        cliente_id = await self._garantir_cliente(assinatura.pagador)
        payload: dict[str, Any] = {
            "customer": cliente_id,
            "billingType": MEIOS_RECORRENCIA[assinatura.meio],
            "value": _numero(assinatura.valor),
            "nextDueDate": assinatura.primeiro_vencimento.isoformat(),
            "cycle": CICLOS[assinatura.ciclo],
            "description": _descricao(assinatura.seu_numero, assinatura.mensagens),
            "externalReference": assinatura.chave_idempotencia,
            **_encargos_payload(assinatura.encargos),
        }
        if assinatura.data_fim is not None:
            payload["endDate"] = assinatura.data_fim.isoformat()
        if assinatura.maximo_cobrancas is not None:
            payload["maxPayments"] = assinatura.maximo_cobrancas
        resp = await self._requisitar(
            "POST", "subscriptions", operacao="criar_assinatura", json=payload
        )
        dados = self._json(resp)
        if not dados.get("id"):
            raise Validacao(
                "resposta do Asaas sem id da assinatura",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        registrada = self._assinatura_registrada(dados)
        return registrada.model_copy(update={"seu_numero": assinatura.seu_numero})

    async def consultar_assinatura(self, id_banco: str) -> AssinaturaRegistrada:
        return self._assinatura_registrada(
            await self._consultar_assinatura_bruta(id_banco)
        )

    async def alterar_assinatura(
        self, id_banco: str, alteracao: AlteracaoAssinatura
    ) -> AssinaturaRegistrada:
        """`PUT /subscriptions/{id}`. Por padrao vale so para as proximas
        cobrancas; `aplicar_nas_pendentes` leva tambem as ja geradas.

        `value` nao consta no schema de atualizacao da OpenAPI, mas o FAQ diz
        que o valor muda: e enviado, e o sandbox decide (docs/asaas-duvidas.md).
        """
        if alteracao.meio is MeioCobranca.CARTAO:
            raise NaoSuportado("asaas: trocar para cartao exige o checkout hospedado")
        corpo: dict[str, Any] = {}
        if alteracao.valor is not None:
            corpo["value"] = _numero(alteracao.valor)
        if alteracao.ciclo is not None:
            corpo["cycle"] = CICLOS[alteracao.ciclo]
        if alteracao.proximo_vencimento is not None:
            corpo["nextDueDate"] = alteracao.proximo_vencimento.isoformat()
        if alteracao.data_fim is not None:
            corpo["endDate"] = alteracao.data_fim.isoformat()
        if alteracao.meio is not None:
            corpo["billingType"] = MEIOS_RECORRENCIA[alteracao.meio]
        if alteracao.encargos is not None:
            corpo.update(_encargos_payload(alteracao.encargos))
        if alteracao.mensagens is not None:
            atual = await self._consultar_assinatura_bruta(id_banco)
            corpo["description"] = _descricao(
                _primeira_linha(atual) or id_banco, alteracao.mensagens
            )
        if not corpo:
            raise Validacao("asaas: alteracao de assinatura sem nenhum campo")
        if alteracao.aplicar_nas_pendentes:
            corpo["updatePendingPayments"] = True
        await self._requisitar(
            "PUT",
            f"subscriptions/{id_banco}",
            operacao="alterar_assinatura",
            json=corpo,
        )
        return await self.consultar_assinatura(id_banco)

    async def suspender_assinatura(self, id_banco: str) -> AssinaturaRegistrada:
        await self._requisitar(
            "PUT",
            f"subscriptions/{id_banco}",
            operacao="suspender_assinatura",
            json={"status": "INACTIVE"},
        )
        return await self.consultar_assinatura(id_banco)

    async def reativar_assinatura(
        self, id_banco: str, proximo_vencimento: date
    ) -> AssinaturaRegistrada:
        # O Asaas exige um novo nextDueDate para voltar a ACTIVE.
        await self._requisitar(
            "PUT",
            f"subscriptions/{id_banco}",
            operacao="reativar_assinatura",
            json={"status": "ACTIVE", "nextDueDate": proximo_vencimento.isoformat()},
        )
        return await self.consultar_assinatura(id_banco)

    async def encerrar_assinatura(self, id_banco: str) -> AssinaturaRegistrada:
        """`DELETE` encerra e remove as cobrancas pendentes e vencidas."""
        await self._requisitar(
            "DELETE", f"subscriptions/{id_banco}", operacao="encerrar_assinatura"
        )
        return AssinaturaRegistrada(
            banco=self.banco, id_banco=id_banco, status=StatusAssinatura.ENCERRADA
        )

    async def listar_cobrancas(self, id_banco: str) -> list[TituloRegistrado]:
        itens = await self._listar(
            f"subscriptions/{id_banco}/payments", {}, limite=10 * _LIMITE_PAGINA
        )
        return [
            await self._montar_registrado(str(item["id"]), item, detalhar=False)
            for item in itens
            if item.get("id")
        ]

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
        """`POST /checkouts` com `chargeTypes: RECURRENT`: o pagador informa o
        cartao na pagina do Asaas e a assinatura nasce la (eventos
        `CHECKOUT_PAID` e `SUBSCRIPTION_CREATED`, ligados por
        `checkoutSession`). Aceita cartao e Pix, nunca boleto."""
        try:
            meios_asaas = [_MEIOS_CHECKOUT[meio] for meio in meios]
        except KeyError as exc:
            raise Validacao("asaas: o checkout aceita so cartao e Pix") from exc
        if not meios_asaas:
            raise Validacao("asaas: checkout sem meio de pagamento")
        if not 10 <= minutos_validade <= 1440:
            raise Validacao("asaas: minutos_validade entre 10 e 1440")
        callback: dict[str, str] = {
            "successUrl": url_sucesso,
            "cancelUrl": url_cancelamento,
        }
        if url_expiracao:
            callback["expiredUrl"] = url_expiracao
        assinatura_payload: dict[str, Any] = {
            "cycle": CICLOS[assinatura.ciclo],
            # A documentacao mostra data e hora neste campo.
            "nextDueDate": f"{assinatura.primeiro_vencimento.isoformat()} 00:00:00",
        }
        if assinatura.data_fim is not None:
            assinatura_payload["endDate"] = (
                f"{assinatura.data_fim.isoformat()} 00:00:00"
            )
        payload: dict[str, Any] = {
            "billingTypes": meios_asaas,
            "chargeTypes": ["RECURRENT"],
            "minutesToExpire": minutos_validade,
            "externalReference": assinatura.chave_idempotencia,
            "callback": callback,
            "items": [
                {
                    "name": assinatura.seu_numero,
                    "description": "\n".join(assinatura.mensagens) or None,
                    "quantity": 1,
                    "value": _numero(assinatura.valor),
                    "imageBase64": self._config.imagem_checkout_base64
                    or IMAGEM_PADRAO_BASE64,
                }
            ],
            "customerData": {
                "name": assinatura.pagador.nome,
                "cpfCnpj": assinatura.pagador.documento,
                **_endereco_payload(assinatura.pagador),
            },
            "subscription": assinatura_payload,
        }
        resp = await self._requisitar(
            "POST", "checkouts", operacao="criar_checkout", json=payload
        )
        dados = self._json(resp)
        id_banco = dados.get("id")
        if not id_banco:
            raise Validacao(
                "resposta do Asaas sem id do checkout",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        status = _STATUS_CHECKOUT.get(str(dados.get("status") or "ACTIVE").upper())
        return Checkout(
            banco=self.banco,
            id_banco=str(id_banco),
            url=dados.get("link") or f"{self._config.url_checkout}{id_banco}",
            status=status or StatusCheckout.ATIVO,
            resposta_bruta=dados,
        )

    def parse_webhook_recorrencia(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoRecorrencia]:
        """`SUBSCRIPTION_*` (objeto `subscription`) e `CHECKOUT_*` (objeto
        `checkout`). O resto, inclusive `PAYMENT_*`, devolve lista vazia."""
        self._conferir_token_webhook(cabecalhos)
        corpo = self._corpo_webhook(corpo)
        evento = str(corpo.get("event") or "")
        tipo = _EVENTOS_RECORRENCIA.get(evento)
        if tipo is None:
            return []
        chave = "checkout" if evento.startswith("CHECKOUT_") else "subscription"
        objeto = corpo.get(chave)
        if not isinstance(objeto, dict) or not objeto.get("id"):
            return []
        id_checkout = (
            str(objeto["id"])
            if chave == "checkout"
            else (
                str(objeto["checkoutSession"])
                if objeto.get("checkoutSession")
                else None
            )
        )
        return [
            EventoRecorrencia(
                banco=self.banco,
                referencia=str(objeto["id"]),
                tipo=tipo,
                ocorrido_em=_momento(corpo.get("dateCreated")),
                id_checkout=id_checkout,
                resposta_bruta=mascarar(corpo),
            )
        ]


def _descrever_erros(bruta: Any) -> str:
    """`code: description` de cada item de `errors`, ou texto vazio."""
    dados = bruta
    if isinstance(dados, str):
        try:
            dados = json.loads(dados)
        except ValueError:
            return ""
    if not isinstance(dados, dict):
        return ""
    erros = dados.get("errors")
    if not isinstance(erros, list):
        return ""
    partes = []
    for item in erros:
        if not isinstance(item, dict):
            continue
        codigo = str(item.get("code") or "?")
        descricao = str(item.get("description") or "").strip()
        partes.append(f"{codigo}: {descricao}" if descricao else codigo)
    return "; ".join(partes)
