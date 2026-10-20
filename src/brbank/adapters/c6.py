"""C6 Bank (336), API Bolepix v2 (boleto com Pix, OAuth2 client_credentials + mTLS).

ESTADO: `validado_sandbox = False`. O contrato vem da especificacao OpenAPI publica
do portal do C6 (`developers.c6bank.com.br/yamls/bolepix.yaml`, versao 1.1.1, e o
guia de migracao `bankslip-migration.yaml`), lida em 2026-10-20, mas nunca foi
executado contra o sandbox do C6. Duvidas em `docs/c6-duvidas.md`.

Nao ha modulo Odoo de origem. O ACBr (LGPL, `ACBrBoletoW_C6.pas`) foi lido so como
documentacao e fala a API v1 (`/v1/bank_slips`, boleto sem Pix), que o C6 substituiu
pela v2. Nenhum codigo foi portado.

Escolhas do adapter:

- Usa a v2 (Bolepix), sempre com `payment_method.pix` (chave aleatoria EVP): e o
  produto "bolecode" do PRD. A v1 (so boleto) fica fora.
- O identificador do titulo no C6 v2 e o `external_reference_id` (26 caracteres
  `[A-Z0-9]`), definido por quem integra. O adapter o deriva da chave de
  idempotencia (`referencia_externa`); o C6 devolve a cobranca existente quando o
  mesmo valor chega de novo, o que torna o registro idempotente no banco.
- A consulta da v2 nao traz pagamentos: para titulo `PAID` o valor e a data vem da
  listagem (`GET /v2/bank_slips/list`) filtrada pela referencia.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado, Validacao
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

log = logging.getLogger("brbank.adapters.c6")

URL_PRODUCAO = "https://baas-api.c6bank.info"
URL_SANDBOX = "https://baas-api-sandbox.c6bank.info"
CAMINHO_TOKEN = "v1/auth/"
CAMINHO_COBRANCAS = "v2/bank_slips"

# Carteiras publicadas na especificacao: 15 em producao, 21 no sandbox.
CARTEIRA_PRODUCAO = "15"
CARTEIRA_SANDBOX = "21"

VALOR_MAXIMO = Decimal("5000000")
SERVICO_WEBHOOK = "BANK_SLIP_PIX"

TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "CREATED": StatusCanonico.REGISTRADO,
        # Pago, mas o dinheiro ainda nao entrou na conta. O C6 manda tratar como
        # estado intermediario e nao como pago definitivo: segue em aberto.
        "WAITING_CONFIRMATION": StatusCanonico.REGISTRADO,
        "PAID": StatusCanonico.LIQUIDADO,
        "CANCELED": StatusCanonico.BAIXADO,
        # Grafia da v1 e da tabela de notificacoes; a v2 escreve CANCELED.
        "CANCELLED": StatusCanonico.BAIXADO,
    },
)

_REFERENCIA = re.compile(r"[A-Z0-9]{26}")
_SO_DIGITOS_10 = re.compile(r"\d{1,10}")
_CHAVE_EVP = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LIMITE_TEXTO = 40
_LIMITE_EMAIL = 70
_LIMITE_DESCRICAO = 100
_LIMITE_ORIGEM = 20
_MAX_INSTRUCOES = 4
_LIMITE_INSTRUCAO = 80
# Chaves com dado pessoal na resposta do C6 (em ingles, fora da lista do mascarar).
_CHAVES_PII = frozenset({"payer"})


def referencia_externa(chave_idempotencia: str) -> str:
    """`external_reference_id` (26 caracteres A-Z/0-9) deterministico pela chave.

    A chave que ja estiver no formato (ex.: um ULID) e usada como veio. As demais
    viram os 26 primeiros caracteres do SHA-256 em base32 (alfabeto A-Z e 2-7).
    """
    if _REFERENCIA.fullmatch(chave_idempotencia):
        return chave_idempotencia
    resumo = hashlib.sha256(chave_idempotencia.encode()).digest()
    return base64.b32encode(resumo).decode()[:26]


@dataclass
class C6Config:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Chave Pix aleatoria (EVP) cadastrada no C6: o QR Code sai dela.
    chave_pix: str = field(repr=False)
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # Carteira; sem valor, 15 em producao e 21 no sandbox (especificacao do C6).
    carteira: str | None = None
    # Cabecalhos opcionais partner-software-name / partner-software-version.
    software_nome: str | None = None
    software_versao: str | None = None
    # Campo `origin` (canal da cobranca, ate 20 caracteres), opcional.
    origem: str | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if not _CHAVE_EVP.fullmatch(self.chave_pix or ""):
            # O C6 cria a cobranca sem Pix, sem erro, quando a chave e invalida.
            raise Validacao("c6: chave_pix deve ser uma chave aleatoria (EVP)")
        if self.origem is not None and len(self.origem) > _LIMITE_ORIGEM:
            raise Validacao(f"c6: origem passa de {_LIMITE_ORIGEM} caracteres")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def carteira_efetiva(self) -> str:
        if self.carteira:
            return self.carteira
        return CARTEIRA_SANDBOX if self.ambiente == "sandbox" else CARTEIRA_PRODUCAO


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation as exc:
        raise Validacao("c6: valor monetario invalido na resposta") from exc


def _data(valor: Any) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError as exc:
        raise Validacao("c6: data invalida na resposta") from exc


def _sem_pii(valor: Any) -> Any:
    """Copia sem o pagador: o `mascarar` so conhece chaves em portugues."""
    if isinstance(valor, dict):
        return {
            chave: "***" if chave in _CHAVES_PII else _sem_pii(item)
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao("pagador sem endereco: o C6 exige endereco e bairro")
    if pessoa.email and len(pessoa.email) > _LIMITE_EMAIL:
        raise Validacao(f"c6: e-mail do pagador passa de {_LIMITE_EMAIL} caracteres")
    logradouro = f"{endereco.logradouro}, {endereco.numero}".strip(", ")
    dados: dict[str, Any] = {
        "name": pessoa.nome.strip()[:_LIMITE_TEXTO],
        "tax_id": pessoa.documento,
        "address": {
            "address": logradouro[:_LIMITE_TEXTO],
            "neighborhood": endereco.bairro.strip()[:_LIMITE_TEXTO],
            "city": endereco.cidade.strip()[:_LIMITE_TEXTO],
            "state": endereco.uf,
            "zip_code": endereco.cep,
        },
    }
    if pessoa.email:
        dados["email"] = pessoa.email
    return dados


def _taxas(titulo: Titulo) -> dict[str, Any]:
    enc = titulo.encargos
    taxas: dict[str, Any] = {}
    if enc.multa_percentual is not None:
        taxas["fine_type"] = "PERCENTAGE"
        taxas["fine_value"] = _numero(enc.multa_percentual)
        taxas["fine_deadline"] = 0
    if enc.juros_mensal_percentual is not None:
        taxas["interest_type"] = "MONTHLY_PERCENTAGE"
        taxas["interest_value"] = _numero(enc.juros_mensal_percentual)
        taxas["interest_deadline"] = 0
    if enc.desconto_percentual is not None:
        # O guia de migracao mapeia o "P" (percentual) da v1 para este nome.
        taxas["discount_type"] = "MONTHLY_PERCENTAGE"
        taxas["first_discount_value"] = _numero(enc.desconto_percentual)
        taxas["first_discount_deadline"] = enc.desconto_dias_antes
    return taxas


def _instrucoes(mensagens: tuple[str, ...]) -> list[str]:
    return [linha[:_LIMITE_INSTRUCAO] for linha in mensagens[:_MAX_INSTRUCOES]]


class C6Adapter(BankAdapter):
    banco: ClassVar[str] = "c6"
    validado_sandbox: ClassVar[bool] = False
    # Sem SO_HIBRIDO: a v2 do C6 emite boleto sem Pix quando a chave falta, e a v1
    # e so boleto. O adapter sempre pede o Pix (chave_pix obrigatoria).
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: C6Config,
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
            url_token=f"{config.url_base}/{CAMINHO_TOKEN}",
            client_id=config.client_id,
            client_secret=config.client_secret,
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

    def _cabecalhos(self, **extra: str) -> dict[str, str]:
        cabecalhos = dict(extra)
        if self._config.software_nome:
            cabecalhos["partner-software-name"] = self._config.software_nome
        if self._config.software_versao:
            cabecalhos["partner-software-version"] = self._config.software_versao
        return cabecalhos

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        if titulo.valor > VALOR_MAXIMO:
            raise Validacao(f"c6: valor maximo da cobranca e R$ {VALOR_MAXIMO}")
        if titulo.encargos.abatimento is not None:
            raise Validacao("c6: a API Bolepix nao tem campo de abatimento")
        boleto: dict[str, Any] = {"billing_scheme": self._config.carteira_efetiva}
        if _SO_DIGITOS_10.fullmatch(titulo.seu_numero):
            boleto["your_number"] = titulo.seu_numero
        if titulo.mensagens:
            boleto["instructions"] = _instrucoes(titulo.mensagens)
        payload: dict[str, Any] = {
            "external_reference_id": referencia_externa(titulo.chave_idempotencia),
            "amount": _numero(titulo.valor),
            "due_date": titulo.vencimento.isoformat(),
            "description": titulo.seu_numero[:_LIMITE_DESCRICAO],
            "payer": _pagador(titulo.pagador),
            "payment_method": {
                "bank_slip": boleto,
                "pix": {"key": self._config.chave_pix, "type": "EVP"},
            },
        }
        if titulo.dias_baixa_automatica is not None:
            payload["days_after_due_date"] = titulo.dias_baixa_automatica
        taxas = _taxas(titulo)
        if taxas:
            payload["fees"] = taxas
        if self._config.origem:
            payload["origin"] = self._config.origem
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do C6 nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao("resposta do C6 fora do formato", status_http=200)
        return dados

    def _registrado(
        self,
        dados: dict[str, Any],
        *,
        id_banco: str,
        status: StatusCanonico,
        seu_numero: str | None = None,
        valor_pago: Decimal | None = None,
        data_pagamento: date | None = None,
    ) -> TituloRegistrado:
        meio = dados.get("payment_method") or {}
        boleto = meio.get("bank_slip") or {}
        pix = meio.get("pix") or {}
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=seu_numero,
            nosso_numero=boleto.get("our_number"),
            linha_digitavel=boleto.get("digitable_line"),
            codigo_barras=boleto.get("bar_code"),
            pix_copia_cola=pix.get("qr_code"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pii(dados),
        )

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        payload = self._montar_payload(titulo)
        resp = await self._transporte.requisitar(
            "POST",
            CAMINHO_COBRANCAS,
            operacao="registrar",
            json=payload,
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        enviado = payload["external_reference_id"]
        id_banco = dados.get("external_reference_id") or enviado
        if id_banco != enviado:
            raise Validacao(
                "c6 devolveu external_reference_id diferente do enviado",
                status_http=resp.status_code,
                resposta_bruta=_sem_pii(dados),
            )
        registrado = self._registrado(
            dados,
            id_banco=id_banco,
            status=StatusCanonico.REGISTRADO,
            seu_numero=titulo.seu_numero,
        )
        if not registrado.pix_copia_cola:
            # Chave recusada pelo C6: a cobranca existe, mas so como boleto.
            log.warning("banco=c6 conta=%s cobranca sem Pix", self._config.conta)
        return registrado

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "GET",
            f"{CAMINHO_COBRANCAS}/{id_banco}",
            operacao="consultar",
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        status = self.tabela_status.traduzir(dados.get("status"))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            pagamentos = dados.get("payments")
            if pagamentos is None:
                pagamentos = await self._pagamentos(id_banco, dados.get("due_date"))
            valor_pago, data_pagamento = self._somar_pagamentos(pagamentos)
        return self._registrado(
            dados,
            id_banco=id_banco,
            status=status,
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
        )

    async def _pagamentos(self, id_banco: str, vencimento: Any) -> list[dict[str, Any]]:
        """Pagamentos de um titulo pela listagem (a consulta da v2 nao os traz).

        A listagem exige um intervalo de datas: usa o vencimento atual do titulo.
        """
        if not vencimento:
            return []
        consulta = urlencode(
            {
                "external_reference_id": id_banco,
                "due_date_from": vencimento,
                "due_date_to": vencimento,
            }
        )
        resp = await self._transporte.requisitar(
            "GET",
            f"{CAMINHO_COBRANCAS}/list?{consulta}",
            operacao="listar",
            cabecalhos=self._cabecalhos(),
        )
        for item in self._json(resp).get("content") or []:
            if isinstance(item, dict) and item.get("external_reference_id") == id_banco:
                return list(item.get("payments") or [])
        return []

    @staticmethod
    def _somar_pagamentos(
        pagamentos: list[dict[str, Any]],
    ) -> tuple[Decimal | None, date | None]:
        """Soma os pagamentos (valor em texto, ex. "123.45"); data = a mais recente."""
        total: Decimal | None = None
        ultima: date | None = None
        for pagamento in pagamentos:
            valor = _dinheiro(pagamento.get("amount"))
            if valor is not None:
                total = valor if total is None else total + valor
            quando = _data(pagamento.get("payment_date") or pagamento.get("date"))
            if quando is not None and (ultima is None or quando > ultima):
                ultima = quando
        return total, ultima

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if tipo is not TipoInstrucao.PRORROGACAO:
            raise NaoSuportado(f"c6: instrucao {tipo.value} nao existe na API Bolepix")
        if instrucao.nova_data_vencimento is None:
            raise Validacao("prorrogacao exige nova_data_vencimento")
        resp = await self._transporte.requisitar(
            "PATCH",
            f"{CAMINHO_COBRANCAS}/{id_banco}",
            operacao="instrucao",
            json={"due_date": instrucao.nova_data_vencimento.isoformat()},
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        return self._registrado(
            dados,
            id_banco=id_banco,
            status=self.tabela_status.traduzir(dados.get("status")),
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._transporte.requisitar(
            "PUT",
            f"{CAMINHO_COBRANCAS}/{id_banco}/cancel",
            operacao="baixar",
            cabecalhos=self._cabecalhos(),
        )
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        resp = await self._transporte.requisitar(
            "GET",
            f"{CAMINHO_COBRANCAS}/{id_banco}/pdf",
            operacao="pdf",
            cabecalhos=self._cabecalhos(Accept="application/pdf"),
        )
        if resp.content.startswith(b"%PDF"):
            return resp.content
        # Formato do corpo nao documentado: aceita tambem JSON com base64 (v1).
        try:
            texto = self._json(resp).get("base64_pdf_file") or ""
            pdf = base64.b64decode(texto, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Validacao("PDF do C6 corrompido") from exc
        if not pdf.startswith(b"%PDF"):
            raise Validacao("resposta do C6 nao contem PDF")
        return pdf

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Notificacao do servico BANK_SLIP_PIX (Bolepix).

        Sem assinatura documentada: o chamador deve reconsultar antes de liquidar.
        A referencia e o `external_reference_id` de `information` (o mesmo id das
        rotas da v2). `information` chega como JSON em texto, as vezes aninhado.
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do C6 nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        return [self._evento(item) for item in itens]

    def _evento(self, item: Any) -> EventoCanonico:
        if not isinstance(item, dict):
            raise Validacao("webhook do C6 fora do formato")
        if item.get("service") != SERVICO_WEBHOOK:
            raise Validacao(
                f"webhook do C6 de servico {item.get('service')!r}: "
                f"so {SERVICO_WEBHOOK} e tratado"
            )
        info = self._informacao(item.get("information"))
        referencia = info.get("external_reference_id")
        if not referencia:
            raise Validacao("webhook do C6 sem external_reference_id")
        tipo = self.tabela_status.traduzir(item.get("status"))
        momento = item.get("date_time")
        try:
            ocorrido_em = datetime_iso(momento) if momento else None
        except ValueError as exc:
            raise Validacao("webhook do C6 com date_time invalido") from exc
        return EventoCanonico(
            banco=self.banco,
            referencia=referencia,
            tipo=tipo,
            ocorrido_em=ocorrido_em,
            valor_pago=(
                _dinheiro(info.get("amount"))
                if tipo is StatusCanonico.LIQUIDADO
                else None
            ),
            resposta_bruta=_sem_pii({**item, "information": info}),
        )

    @staticmethod
    def _informacao(valor: Any) -> dict[str, Any]:
        for _ in range(3):
            if isinstance(valor, str):
                try:
                    valor = json.loads(valor)
                except ValueError as exc:
                    raise Validacao("webhook do C6 com information invalido") from exc
            if isinstance(valor, dict) and set(valor) == {"information"}:
                # Exemplo de pagamento na especificacao: information dentro de
                # information.
                valor = valor["information"]
                continue
            break
        if not isinstance(valor, dict):
            raise Validacao("webhook do C6 sem information")
        return valor
