"""Santander (033), Hub de Pagamentos 1.1.7 (workspace, OAuth2 + mTLS).

Sem modulo Odoo de origem. Contrato lido no OpenAPI oficial do portal
developer.santander.com.br ("Payment Hub" 1.1.7, arquivo
`vx-hub-de-pagamentos_en (3).yaml`), nos guias funcionais (atualizados em
27/10/2026) e nas colecoes Postman oficiais, lidos em 30/10/2026. O que vem de
terceiro ou segue em aberto esta marcado em `docs/santander-pagamento-duvidas.md`.

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Santander: o sandbox
exige aplicacao cadastrada no portal e certificado.

Pontos do contrato:

- Paga um por vez (`LOTE_LOCAL`): nao ha lote no banco. Mas, diferente de outros
  bancos com lote local, ha um passo de autorizacao (`LIBERACAO`): o POST valida
  e deixa o pagamento em READY_TO_PAY, sem mover dinheiro; o dinheiro so anda no
  PATCH com `status: AUTHORIZED`. A autorizacao fica em `autorizar_lote`, que
  recebe o `LoteRegistrado` gravado pelo chamador e devolve o estado por linha.
  `liberar_lote(numero)` so serve na mesma instancia que enviou o lote, porque o
  lote nao existe no banco e o PATCH precisa de rota, id e valor de cada linha.
- `id` escolhido por quem envia: o adapter manda um UUID (formato v4) derivado do
  workspace e da chave de idempotencia da linha. O banco recusa id repetido
  (30082, 40053, 20002), e nesse caso o adapter consulta e devolve o estado real:
  reenviar a mesma linha nao cria outro pagamento. Linha rejeitada continua
  rejeitada com a mesma chave; para tentar de novo, use outra chave.
- As escritas (POST e PATCH) saem sem retry. PATCH sem resposta ou com 2xx
  ilegivel e INDETERMINADO, nunca RECUSADO: conferir antes de autorizar de novo.
  POST sem resposta nao moveu dinheiro, mas o estado do lado do banco e
  desconhecido: tambem volta INDETERMINADO, com o `id_banco` ja preenchido.
- Boleto repetido: o guia diz que um segundo POST com o mesmo codigo e outro id
  descarta o primeiro. Com a mesma chave isso nao acontece; com outra, acontece.
- Agendamento so em GUIA (guia "Transactional View"). Nos outros tipos, data
  diferente de hoje e recusada aqui: sem isso o Pix e a TED, que nao levam data,
  sairiam na hora.
- `finalPayer` e obrigatorio no PATCH de boleto e guia e, em workspace PAYMENTS,
  repete o titular do workspace (guia "Complete boleto payment").
- `debitAccount` e opcional no PATCH, mas sem ele o banco debita a conta principal
  do workspace: o adapter sempre manda a conta do lote.
- Conferencia antes de autorizar: a resposta do POST traz o beneficiario. Documento
  diferente do esperado vira RECUSADO (`DOCUMENTO_DIVERGENTE`), e o pagamento fica
  parado em READY_TO_PAY no banco, sem efeito.
- Valor em texto com ponto; na TED, numero ate 10 milhoes e texto acima disso.
- Webhook: o workspace aceita URL, mas o formato da notificacao nao e publico.
  `WEBHOOK` nao e declarada. Cancelamento: o guia cita, mas o OpenAPI nao tem rota.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from brbank.adapters.santander import CAMINHO_TOKEN, HOST_PRODUCAO, HOST_SANDBOX
from brbank.auth import OAuth2ClientCredentials
from brbank.erros import Duplicado, ErroBrbank, Indisponivel, NaoSuportado, Validacao
from brbank.mascara import MASCARA
from brbank.pagamento import (
    AdapterPagamento,
    CapacidadePagamento,
    ContaBancaria,
    DestinoPix,
    LotePagamento,
    LoteRegistrado,
    Pagamento,
    PagamentoRegistrado,
    StatusLote,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
    status_lote_local,
)
from brbank.transporte import (
    SEM_RETRY,
    LimitadorTaxa,
    PoliticaRetry,
    Transporte,
    contexto_tls,
)

CAMINHO_API = "/management_payments_partners/v1"
AMBIENTES = ("producao", "sandbox")

ROTAS = {
    TipoPagamento.BOLETO: "bank_slip_payments",
    TipoPagamento.GUIA: "barcode_payments",
    TipoPagamento.PIX: "pix_payments",
    TipoPagamento.PIX_QRCODE: "pix_payments",
    TipoPagamento.TRANSFERENCIA: "transfer",
}

# Guia "Transactional View" e enums do OpenAPI. STARTED, PENDING_VALIDATION e
# AUTHORIZED ainda nao terminaram; REFUNDED e a devolucao de um pagamento pago.
ESTADOS = {
    "STARTED": StatusPagamento.EM_PROCESSAMENTO,
    "PENDING_VALIDATION": StatusPagamento.EM_PROCESSAMENTO,
    "READY_TO_PAY": StatusPagamento.ACEITO,
    "AUTHORIZED": StatusPagamento.EM_PROCESSAMENTO,
    "PENDING_CONFIRMATION": StatusPagamento.EM_PROCESSAMENTO,
    "SCHEDULED": StatusPagamento.AGENDADO,
    "PAYED": StatusPagamento.PAGO,
    "REJECTED": StatusPagamento.REJEITADO,
    "REFUNDED": StatusPagamento.DEVOLVIDO,
}

# `_code` de "id ja existe" e "transacao ja registrada" nas tabelas de erro.
CODIGOS_JA_EXISTE = frozenset({"30082", "30088", "40053", "20002", "20064"})

MOTIVO_NAO_ENVIADO = "NAO_ENVIADO"
MOTIVO_SEM_RESPOSTA = "SEM_RESPOSTA"
MOTIVO_DOCUMENTO_DIVERGENTE = "DOCUMENTO_DIVERGENTE"
MOTIVO_VALOR_DIVERGENTE = "VALOR_DIVERGENTE"

TIPOS_DESTINO_TED = ("STR0006", "STR0008")
# Acima disso a TED vai como texto (guia "Initiate TED Transfer").
_TED_LIMITE_NUMERO = Decimal("10000000")
_MAX_NOME_PAGADOR = 50
_MAX_NOME_BENEFICIARIO = 80
_MAX_TAG = 20
_CENTAVO = Decimal("0.01")
_TIPO_CHAVE_API = {
    TipoChavePix.EMAIL: "EMAIL",
    TipoChavePix.ALEATORIA: "EVP",
    TipoChavePix.TELEFONE: "CELULAR",
}

# Chaves em ingles que o `mascarar` comum nao cobre por inteiro.
_CHAVES_PII = frozenset(
    {
        "payer",
        "finalpayer",
        "beneficiary",
        "finalbeneficiary",
        "finalaggregator",
        "name",
        "documentnumber",
        "documentidentifiernumber",
        "dictcode",
        "qrcode",
        "debitaccount",
        "sourceaccount",
        "destinationaccount",
        "remittanceinformation",
    }
)


def _sem_pii(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            chave: (
                MASCARA
                if isinstance(chave, str) and chave.lower() in _CHAVES_PII
                else _sem_pii(item)
            )
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


def _bruta(dados: dict[str, Any]) -> dict[str, Any]:
    # `code` no primeiro nivel e o codigo de barras; em `transaction` e a autenticacao.
    return {**_sem_pii(dados), **({"code": MASCARA} if "code" in dados else {})}


def _so_alfanumerico(texto: Any) -> str:
    return "".join(c for c in str(texto) if c.isalnum()).upper()


def _tipo_documento(documento: str) -> str:
    if len(documento) == 11 and documento.isdigit():
        return "CPF"
    if len(documento) == 14:
        return "CNPJ"
    raise Validacao("documento deve ser CPF (11) ou CNPJ (14)")


@dataclass
class SantanderPagamentoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Id do workspace tipo PAYMENTS, criado antes, com os produtos ligados.
    workspace_id: str
    # Titular do workspace: vai em `finalPayer` no boleto e na guia.
    pagador_documento: str = field(repr=False)
    pagador_nome: str = field(repr=False)
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # A conta de debito vai com o DV colado (ex.: 13037543-1 -> "130375431").
    conta_com_digito: bool = True
    # Municipio IBGE (7 digitos) para o QR com vencimento. Opcional.
    codigo_municipio: str | None = None
    ted_tipo_destino: str = "STR0008"
    ted_finalidade: str = "PAGAMENTO_DE_FORNECEDORES"
    # A TED exige o nome do favorecido, que o modelo `Pagamento` nao tem.
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in AMBIENTES:
            raise Validacao(f"ambiente Santander invalido: {self.ambiente!r}")
        if not self.workspace_id:
            raise Validacao("Santander exige workspace_id (workspace criado antes)")
        documento = _so_alfanumerico(self.pagador_documento)
        if len(documento) not in (11, 14):
            raise Validacao("pagador_documento deve ser CPF (11) ou CNPJ (14)")
        self.pagador_documento = documento
        if self.codigo_municipio is not None and not (
            len(self.codigo_municipio) == 7 and self.codigo_municipio.isdigit()
        ):
            raise Validacao("codigo_municipio e o codigo IBGE de 7 digitos")
        if self.ted_tipo_destino not in TIPOS_DESTINO_TED:
            raise Validacao(f"ted_tipo_destino deve ser um de {TIPOS_DESTINO_TED}")

    @property
    def _host(self) -> str:
        return HOST_SANDBOX if self.ambiente == "sandbox" else HOST_PRODUCAO

    @property
    def url_base(self) -> str:
        return self._host + CAMINHO_API

    @property
    def url_token(self) -> str:
        return self._host + CAMINHO_TOKEN


class _Incerto(Exception):
    """Escrita sem resposta legivel: o estado no banco e desconhecido."""

    def __init__(self, id_banco: str) -> None:
        super().__init__("pagamento sem resposta")
        self.id_banco = id_banco


def _texto_valor(valor: Decimal) -> str:
    return f"{valor:.2f}"


def _valor_ted(valor: Decimal) -> float | str:
    # A API pede numero JSON; a conversao fica restrita a fronteira.
    return _texto_valor(valor) if valor > _TED_LIMITE_NUMERO else float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Santander") from exc


def _data(valor: Any) -> date | None:
    if not valor or not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _corpo_de_erro(erro: ErroBrbank) -> dict[str, Any]:
    return erro.resposta_bruta if isinstance(erro.resposta_bruta, dict) else {}


def _motivos_do_erro(erro: ErroBrbank) -> tuple[str, ...]:
    """Motivos do corpo de erro: `_errors[]._code` e `_message`."""
    corpo = _corpo_de_erro(erro)
    motivos = []
    for item in corpo.get("_errors") or []:
        if isinstance(item, dict) and (item.get("_code") or item.get("_message")):
            motivos.append(
                ": ".join(str(item[k]) for k in ("_code", "_message") if item.get(k))
            )
    if not motivos:
        for chave in ("_details", "_message", "_errorCode"):
            if corpo.get(chave):
                motivos.append(str(corpo[chave]))
                break
    return tuple(motivos) or (erro.mensagem,)


def _id_ja_existe(erro: ErroBrbank) -> bool:
    if isinstance(erro, Duplicado):
        return True
    corpo = _corpo_de_erro(erro)
    codigos = {str(corpo.get("_errorCode"))}
    for item in corpo.get("_errors") or []:
        if isinstance(item, dict):
            codigos.add(str(item.get("_code")))
    return bool(codigos & CODIGOS_JA_EXISTE)


def _status_lote(pagamentos: tuple[PagamentoRegistrado, ...]) -> StatusLote:
    if all(p.status is StatusPagamento.ACEITO for p in pagamentos):
        return StatusLote.PENDENTE_LIBERACAO
    return status_lote_local(pagamentos)


class SantanderPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "santander"
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.BOLETO
        | CapacidadePagamento.GUIA
        | CapacidadePagamento.PIX
        | CapacidadePagamento.PIX_QRCODE
        | CapacidadePagamento.TRANSFERENCIA
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LOTE_LOCAL
        | CapacidadePagamento.LIBERACAO
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: SantanderPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        # Numero do lote -> o que `liberar_lote` precisa, so nesta instancia.
        self._enviados: dict[str, tuple[LoteRegistrado, ContaBancaria]] = {}
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None:
            # O token e amarrado ao certificado tambem no sandbox (guia "mTLS").
            raise Validacao("Santander pagamentos exige certificado mTLS")
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            ssl_context=ssl_context,
            autenticador=OAuth2ClientCredentials(
                conta=config.conta,
                url_token=config.url_token,
                client_id=config.client_id,
                client_secret=config.client_secret,
            ),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # -- chamadas ------------------------------------------------------------

    def _caminho(self, tipo: TipoPagamento, id_banco: str | None = None) -> str:
        base = f"workspaces/{quote(self._config.workspace_id, safe='')}/{ROTAS[tipo]}"
        return f"{base}/{quote(id_banco, safe='')}" if id_banco else base

    async def _chamar(
        self,
        metodo: str,
        caminho: str,
        operacao: str,
        json: Any = None,
        *,
        sem_retry: bool = False,
    ) -> dict[str, Any]:
        resp = await self._transporte.requisitar(
            metodo,
            caminho,
            operacao=operacao,
            json=json,
            cabecalhos={"X-Application-Key": self._config.client_id},
            retry=SEM_RETRY if sem_retry else None,
        )
        if not resp.content:
            return {}
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Santander nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Santander fora do formato", status_http=resp.status_code
            )
        return dados

    # -- montagem --------------------------------------------------------------

    def _id_pagamento(self, chave: str) -> str:
        """UUID no formato v4 (o que a API pede), estavel para a mesma linha."""
        semente = f"{self._config.workspace_id}:{chave}".encode()
        return str(uuid.UUID(bytes=hashlib.sha256(semente).digest()[:16], version=4))

    def _numero_debito(self, conta: ContaBancaria) -> str:
        cheio = (
            conta.conta + conta.digito if self._config.conta_com_digito else conta.conta
        )
        if len(cheio) > 12:
            raise Validacao("santander: conta de debito tem ate 12 digitos")
        # O guia manda completar com zeros; o exemplo "000000000001" e assim.
        return cheio.zfill(12)

    @staticmethod
    def _agencia(conta: ContaBancaria) -> str:
        if len(conta.agencia) > 4:
            raise Validacao("santander: agencia tem ate 4 digitos")
        return conta.agencia.zfill(4)

    def _conta_debito(self, conta: ContaBancaria) -> dict[str, str]:
        return {"branch": self._agencia(conta), "number": self._numero_debito(conta)}

    def _conta_origem_ted(self, conta: ContaBancaria) -> dict[str, str]:
        return {
            "branchCode": self._agencia(conta),
            "accountNumber": self._numero_debito(conta),
        }

    def _conferir_data(self, p: Pagamento) -> None:
        hoje = self._hoje()
        if p.data < hoje:
            raise Validacao("santander: data de pagamento no passado")
        if p.data > hoje and p.tipo is not TipoPagamento.GUIA:
            raise Validacao("santander: agendamento so existe em guia (barcode)")

    @staticmethod
    def _chave_pix(destino: DestinoPix) -> dict[str, str]:
        assert destino.chave is not None and destino.tipo_chave is not None
        chave = destino.chave.strip()
        tipo = destino.tipo_chave
        if tipo is TipoChavePix.DOCUMENTO:
            documento = _so_alfanumerico(chave)
            return {
                "dictCodeType": _tipo_documento(documento),
                "dictCode": documento,
            }
        if tipo is TipoChavePix.TELEFONE:
            digitos = "".join(c for c in chave if c.isdigit())
            if len(digitos) in (10, 11):
                digitos = "55" + digitos
            if not (digitos.startswith("55") and len(digitos) in (12, 13)):
                raise Validacao("chave Pix de telefone deve ter DDD e numero")
            chave = "+" + digitos
        return {"dictCodeType": _TIPO_CHAVE_API[tipo], "dictCode": chave}

    @staticmethod
    def _beneficiario_pix(destino: DestinoPix) -> dict[str, str]:
        conta = destino.conta
        assert conta is not None
        if not destino.documento:
            raise Validacao("Pix por dados bancarios exige o documento do favorecido")
        if not destino.nome:
            raise Validacao("Pix por dados bancarios exige o nome do favorecido")
        if len(conta.agencia) > 4:
            raise Validacao("santander: agencia de destino tem ate 4 digitos")
        dados = {
            "branch": conta.agencia.zfill(4),
            "number": conta.conta + conta.digito.upper(),
            "type": "CONTA_POUPANCA" if conta.poupanca else "CONTA_CORRENTE",
            "documentType": _tipo_documento(destino.documento),
            "documentNumber": destino.documento,
            "name": destino.nome[:_MAX_NOME_BENEFICIARIO],
        }
        # "bankCode ou ISPB, nunca os dois" (guia "Initiate a Pix Payment").
        if conta.ispb:
            dados["ispb"] = conta.ispb
        else:
            dados["bankCode"] = conta.compe or ""
        return dados

    def _destino_ted(self, p: Pagamento) -> dict[str, str]:
        conta = p.conta_credito
        assert conta is not None and p.favorecido_documento
        if not p.favorecido_nome:
            raise Validacao("santander: TED exige o nome do favorecido")
        if not conta.compe:
            raise Validacao("santander: TED exige o codigo COMPE do banco de destino")
        if len(conta.agencia) > 4:
            raise Validacao("santander: agencia de destino tem ate 4 digitos")
        tipo = "PP" if conta.poupanca else ("PG" if conta.ispb else "CC")
        dados = {
            "bankCode": conta.compe,
            "branchCode": conta.agencia,
            "accountNumber": conta.conta + conta.digito.upper(),
            "typeAccount": tipo,
            "legalEntityIdentifier": _tipo_documento(p.favorecido_documento),
            "documentIdentifierNumber": p.favorecido_documento,
            "name": p.favorecido_nome,
            "purpose": self._config.ted_finalidade,
        }
        # ISPB so em conta de pagamento: o banco recusa em CC e PP (20062).
        if tipo == "PG":
            dados["ispbCode"] = conta.ispb or ""
        return dados

    def _corpo_inicio(
        self, p: Pagamento, id_pagamento: str, conta: ContaBancaria
    ) -> dict[str, Any]:
        self._conferir_data(p)
        corpo: dict[str, Any] = {"id": id_pagamento}
        if len(p.chave_idempotencia) <= _MAX_TAG:
            corpo["tags"] = [p.chave_idempotencia]
        if p.tipo in (TipoPagamento.BOLETO, TipoPagamento.GUIA):
            corpo.update(code=p.codigo_barras, paymentDate=p.data.isoformat())
            return corpo
        if p.tipo is TipoPagamento.TRANSFERENCIA:
            corpo.update(
                sourceAccount=self._conta_origem_ted(conta),
                destinationAccount=self._destino_ted(p),
                transferValue=_valor_ted(p.valor),
                destinationType=self._config.ted_tipo_destino,
            )
            return corpo
        corpo["paymentValue"] = _texto_valor(p.valor)
        if p.descricao:
            corpo["remittanceInformation"] = p.descricao
        if p.tipo is TipoPagamento.PIX_QRCODE:
            corpo.update(qrCode=p.brcode, paymentDate=p.data.isoformat())
            if self._config.codigo_municipio:
                corpo["ibgeTownCode"] = int(self._config.codigo_municipio)
            return corpo
        destino = p.pix
        assert destino is not None
        if destino.chave is not None:
            corpo.update(self._chave_pix(destino))
        else:
            corpo["beneficiary"] = self._beneficiario_pix(destino)
        return corpo

    def _corpo_autorizacao(
        self, tipo: TipoPagamento, valor: Decimal | None, conta: ContaBancaria
    ) -> dict[str, Any]:
        if tipo is TipoPagamento.TRANSFERENCIA:
            return {
                "sourceAccount": self._conta_origem_ted(conta),
                "status": "AUTHORIZED",
            }
        if valor is None:
            raise Validacao("santander: linha sem valor nao pode ser autorizada")
        corpo: dict[str, Any] = {
            "paymentValue": _texto_valor(valor),
            "debitAccount": self._conta_debito(conta),
            "status": "AUTHORIZED",
        }
        if tipo in (TipoPagamento.BOLETO, TipoPagamento.GUIA):
            documento = self._config.pagador_documento
            corpo["finalPayer"] = {
                "name": self._config.pagador_nome[:_MAX_NOME_PAGADOR],
                "documentType": _tipo_documento(documento),
                "documentNumber": documento,
            }
        return corpo

    # -- leitura ---------------------------------------------------------------

    def _registrado(
        self, dados: dict[str, Any], chave: str | None, id_banco: str | None = None
    ) -> PagamentoRegistrado:
        identificador = dados.get("id") or id_banco
        if not identificador:
            raise Validacao("resposta do Santander sem id")
        bruto = str(dados.get("status") or "")
        if bruto not in ESTADOS:
            raise Validacao(f"santander: estado de pagamento nao mapeado {bruto!r}")
        transacao = dados.get("transaction") or {}
        if not isinstance(transacao, dict):
            transacao = {}
        valor = transacao.get("value")
        for chave_valor in ("paymentValue", "transferValue", "totalValue"):
            if valor not in (None, ""):
                break
            valor = dados.get(chave_valor)
        detalhe = dados.get("statusDetail") or {}
        motivos = [
            str(m)
            for m in (
                dados.get("rejectCode"),
                dados.get("rejectReason"),
                detalhe.get("detail") if isinstance(detalhe, dict) else None,
            )
            if m
        ]
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(identificador),
            status=ESTADOS[bruto],
            chave_idempotencia=chave,
            valor=_dinheiro(valor),
            data=_data(transacao.get("date"))
            or _data(dados.get("accountingDate"))
            or _data(dados.get("paymentDate")),
            motivos=tuple(motivos),
            autenticacao=transacao.get("code") or None,
            resposta_bruta=_bruta(dados),
        )

    def _recusado(
        self, p: Pagamento, motivos: tuple[str, ...], id_banco: str = ""
    ) -> PagamentoRegistrado:
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusPagamento.RECUSADO,
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=p.data,
            motivos=motivos,
        )

    @staticmethod
    def _documento_confere(p: Pagamento, dados: dict[str, Any]) -> bool:
        esperado = p.beneficiario_documento or (p.pix.documento if p.pix else None)
        if not esperado:
            return True
        vistos = []
        for chave in ("beneficiary", "finalBeneficiary"):
            bloco = dados.get(chave)
            if isinstance(bloco, dict) and bloco.get("documentNumber"):
                vistos.append(_so_alfanumerico(bloco["documentNumber"]).lstrip("0"))
        return not vistos or esperado.lstrip("0") in vistos

    def _lido_inicio(
        self, dados: dict[str, Any], p: Pagamento, id_pagamento: str
    ) -> PagamentoRegistrado:
        """Resposta 2xx do POST (ou a consulta de um id repetido)."""
        try:
            linha = self._registrado(dados, p.chave_idempotencia)
            total = _dinheiro(dados.get("totalValue"))
        except Validacao as exc:
            raise _Incerto(id_pagamento) from exc
        if linha.status is StatusPagamento.REJEITADO:
            return self._recusado(p, linha.motivos, linha.id_banco)
        if linha.status is not StatusPagamento.ACEITO:
            return linha
        if not self._documento_confere(p, dados):
            return self._recusado(p, (MOTIVO_DOCUMENTO_DIVERGENTE,), linha.id_banco)
        # O valor que sera autorizado e o da linha, nao o que o banco ecoa.
        valor = p.valor
        if (
            p.tipo is TipoPagamento.PIX_QRCODE
            and total is not None
            and total != p.valor
        ):
            if not p.pagar_com_juros_multa:
                return self._recusado(p, (MOTIVO_VALOR_DIVERGENTE,), linha.id_banco)
            valor = total
        return linha.model_copy(update={"valor": valor, "data": p.data})

    # -- operacoes -------------------------------------------------------------

    async def reservar_numero_lote(self) -> str:
        """Numero local: o Santander nao tem lote."""
        return uuid.uuid4().hex

    async def _reler_inicio(
        self, p: Pagamento, id_pagamento: str, original: ErroBrbank
    ) -> PagamentoRegistrado:
        try:
            dados = await self._chamar(
                "GET", self._caminho(p.tipo, id_pagamento), "consultar_pagamento"
            )
        except Indisponivel as exc:
            raise _Incerto(id_pagamento) from exc
        except Validacao as exc:
            # "Transacao ja registrada" com outro id: nao ha o que devolver.
            raise original from exc
        return self._lido_inicio(dados, p, id_pagamento)

    async def _iniciar(self, p: Pagamento, conta: ContaBancaria) -> PagamentoRegistrado:
        id_pagamento = self._id_pagamento(p.chave_idempotencia)
        corpo = self._corpo_inicio(p, id_pagamento, conta)
        try:
            dados = await self._chamar(
                "POST",
                self._caminho(p.tipo),
                "iniciar_pagamento",
                corpo,
                sem_retry=True,
            )
        except Indisponivel as exc:
            if exc.status_http == 429:
                raise
            raise _Incerto(id_pagamento) from exc
        except (Validacao, Duplicado) as exc:
            if (exc.status_http or 0) < 400:
                raise _Incerto(id_pagamento) from exc
            if _id_ja_existe(exc):
                return await self._reler_inicio(p, id_pagamento, exc)
            raise
        return self._lido_inicio(dados, p, id_pagamento)

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Inicia linha a linha (POST). Nada e pago antes de `autorizar_lote`.

        Recusa de uma linha nao para as outras. Linha sem resposta (INDETERMINADO)
        ou com 429 para o envio: as seguintes voltam RECUSADO, motivo NAO_ENVIADO.
        """
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        numero = lote.numero or await self.reservar_numero_lote()
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for p in lote.pagamentos:
            if parou:
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO,)))
                continue
            try:
                linhas.append(await self._iniciar(p, lote.conta_debito))
            except _Incerto as exc:
                parou = True
                linhas.append(
                    PagamentoRegistrado(
                        banco=self.banco,
                        id_banco=exc.id_banco,
                        status=StatusPagamento.INDETERMINADO,
                        chave_idempotencia=p.chave_idempotencia,
                        valor=p.valor,
                        data=p.data,
                        motivos=(MOTIVO_SEM_RESPOSTA,),
                    )
                )
            except Indisponivel as exc:
                parou = True
                linhas.append(
                    self._recusado(p, (MOTIVO_NAO_ENVIADO, *_motivos_do_erro(exc)))
                )
            except ErroBrbank as exc:
                linhas.append(self._recusado(p, _motivos_do_erro(exc)))
        pagamentos = tuple(linhas)
        registrado = LoteRegistrado(
            banco=self.banco,
            id_banco=numero,
            tipo=lote.tipo,
            status=_status_lote(pagamentos),
            pagamentos=pagamentos,
        )
        self._enviados[numero] = (registrado, lote.conta_debito)
        return registrado

    async def _autorizar_linha(
        self, linha: PagamentoRegistrado, tipo: TipoPagamento, conta: ContaBancaria
    ) -> PagamentoRegistrado:
        corpo = self._corpo_autorizacao(tipo, linha.valor, conta)
        try:
            dados = await self._chamar(
                "PATCH",
                self._caminho(tipo, linha.id_banco),
                "autorizar_pagamento",
                corpo,
                sem_retry=True,
            )
        except Indisponivel as exc:
            if exc.status_http == 429:
                raise
            raise _Incerto(linha.id_banco) from exc
        except (Validacao, Duplicado) as exc:
            if (exc.status_http or 0) < 400:
                raise _Incerto(linha.id_banco) from exc
            return await self._apos_recusa(linha, tipo, exc)
        try:
            atual = self._registrado(dados, linha.chave_idempotencia)
        except Validacao as exc:
            raise _Incerto(linha.id_banco) from exc
        if atual.status is StatusPagamento.ACEITO:
            # Autorizado e ainda READY_TO_PAY: nao diz se pagou nem se vai pagar.
            raise _Incerto(linha.id_banco)
        return atual.model_copy(update={"data": atual.data or linha.data})

    async def _apos_recusa(
        self, linha: PagamentoRegistrado, tipo: TipoPagamento, erro: ErroBrbank
    ) -> PagamentoRegistrado:
        """O banco respondeu erro ao PATCH: a verdade da linha vem da consulta (um
        saldo insuficiente deixa READY_TO_PAY; um reenvio acha o pagamento ja pago)."""
        motivos = _motivos_do_erro(erro)
        try:
            dados = await self._chamar(
                "GET", self._caminho(tipo, linha.id_banco), "consultar_pagamento"
            )
            atual = self._registrado(dados, linha.chave_idempotencia)
        except ErroBrbank:
            return linha.model_copy(update={"motivos": motivos})
        # Ainda em READY_TO_PAY, o valor a autorizar continua sendo o da linha.
        aceita = atual.status is StatusPagamento.ACEITO
        return atual.model_copy(
            update={
                "motivos": motivos,
                "valor": linha.valor if aceita else atual.valor or linha.valor,
                "data": atual.data or linha.data,
            }
        )

    async def autorizar_lote(
        self, lote: LoteRegistrado, conta_debito: ContaBancaria
    ) -> LoteRegistrado:
        """Autoriza (PATCH AUTHORIZED) as linhas em ACEITO: aqui o dinheiro anda.

        Recebe o lote que `pagar_lote` devolveu, gravado pelo chamador, com a mesma
        conta de debito. Linhas em outro estado passam sem chamada. Linha sem
        resposta volta INDETERMINADO e para o envio; as seguintes continuam ACEITO,
        com motivo NAO_ENVIADO. Erro de montagem (conta invalida, linha sem valor)
        nao chega ao banco: a linha continua ACEITO, com o motivo. Nunca repete
        sozinho, e nada aqui levanta depois de uma linha ter sido paga.
        """
        self._enviados.pop(lote.id_banco, None)
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for linha in lote.pagamentos:
            if linha.status is not StatusPagamento.ACEITO:
                linhas.append(linha)
            elif parou:
                linhas.append(
                    linha.model_copy(update={"motivos": (MOTIVO_NAO_ENVIADO,)})
                )
            else:
                try:
                    linhas.append(
                        await self._autorizar_linha(linha, lote.tipo, conta_debito)
                    )
                except _Incerto:
                    parou = True
                    linhas.append(
                        linha.model_copy(
                            update={
                                "status": StatusPagamento.INDETERMINADO,
                                "motivos": (MOTIVO_SEM_RESPOSTA,),
                            }
                        )
                    )
                except Indisponivel as exc:
                    parou = True
                    linhas.append(
                        linha.model_copy(
                            update={
                                "motivos": (MOTIVO_NAO_ENVIADO, *_motivos_do_erro(exc))
                            }
                        )
                    )
                except ErroBrbank as exc:
                    linhas.append(
                        linha.model_copy(update={"motivos": _motivos_do_erro(exc)})
                    )
        pagamentos = tuple(linhas)
        return LoteRegistrado(
            banco=self.banco,
            id_banco=lote.id_banco,
            tipo=lote.tipo,
            status=_status_lote(pagamentos),
            pagamentos=pagamentos,
        )

    async def liberar_lote(self, id_banco: str) -> None:
        """Atalho de `autorizar_lote` para o lote enviado por esta mesma instancia.

        O contrato nao devolve o estado das linhas: acompanhe por
        `consultar_pagamento`, ou use `autorizar_lote`, que devolve e sobrevive a
        um reinicio do processo. Linha sem resposta levanta `Indisponivel` (nao
        repetivel): conferir antes de tentar de novo.
        """
        enviado = self._enviados.pop(id_banco, None)
        if enviado is None:
            raise Validacao(
                "santander: lote desconhecido nesta instancia; use autorizar_lote "
                "com o LoteRegistrado gravado"
            )
        lote, conta = enviado
        resultado = await self.autorizar_lote(lote, conta)
        if any(p.status is StatusPagamento.INDETERMINADO for p in resultado.pagamentos):
            raise Indisponivel(
                "santander: linha sem resposta na autorizacao; conferir antes de "
                "tentar de novo",
                retryable=False,
            )

    async def consultar_lote(
        self, id_banco: str, tipo: TipoPagamento
    ) -> LoteRegistrado:
        raise NaoSuportado(
            "santander: o lote e local (LOTE_LOCAL); consulte cada pagamento pelo id"
        )

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        if not id_banco:
            raise Validacao("santander: consulta exige o id do pagamento")
        dados = await self._chamar(
            "GET", self._caminho(tipo, id_banco), "consultar_pagamento"
        )
        return self._registrado(dados, None, id_banco)


__all__ = [
    "CAMINHO_API",
    "ESTADOS",
    "ROTAS",
    "SantanderPagamentoAdapter",
    "SantanderPagamentoConfig",
]
