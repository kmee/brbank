"""Santander (033), API de Cobranca "Gestao de Boletos" v2 (workspace, OAuth2 + mTLS).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Santander. O portal
oficial (developer.santander.com.br) entrega a especificacao so por JavaScript e
nao foi legivel na escrita: o contrato vem da leitura do ACBr (LGPL, usado so
como documentacao, nenhum codigo portado) e de bibliotecas de terceiros. Fontes
e duvidas em `docs/santander-duvidas.md`.

Pontos do contrato:

- Tudo passa por um *workspace* criado antes, fora do adapter (tipo `BILLING`,
  convenios e, opcionalmente, URL de webhook). Registro e instrucao vao em
  `/workspaces/{id}/bank_slips`; a consulta, em `/bills`, fora do workspace.
- Token `client_credentials` com `client_id` e `client_secret` no formulario e
  certificado mTLS; toda chamada de negocio leva `X-Application-Key: client_id`.
- O nosso numero (`bankNumber`) e escolhido por quem registra, como no BB:
  vem de `SantanderConfig.gerar_nosso_numero` ou do `seu_numero` numerico. Ele e
  o `id_banco`. Como o adapter ja conhece o numero, um 409 no registro vira
  `Duplicado` com `id_banco`, e o orquestrador reconsulta.
- Valores e percentuais vao como texto com duas casas ("150.00"): o Decimal
  nao passa por float.
- A consulta junta ate tres respostas, porque nenhuma traz tudo: `/bills` por
  nosso numero (situacao e valor pago), `tipoConsulta=bankslip` (linha digitavel
  e Pix, so no boleto hibrido) e `tipoConsulta=settlement` (data e valor da
  liquidacao ou da baixa).
- Boleto pago por Pix aparece como `BAIXADO` com a baixa descrita como
  "BAIXA DE PAGAMENTO VIA PIX": o adapter traduz para `LIQUIDADO`.
- Webhook: o workspace aceita URL e liga o aviso de boleto, mas o formato da
  notificacao nao e publico. `WEBHOOK` nao e declarada e a liquidacao sai por
  polling (`consultar_titulo`), como diz o PRD ("workspace + polling").
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import Duplicado, NaoSuportado, Validacao
from brbank.mascara import MASCARA
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

HOST_PRODUCAO = "https://trust-open.api.santander.com.br"
HOST_SANDBOX = "https://trust-sandbox.api.santander.com.br"
CAMINHO_API = "/collection_bill_management/v2"
CAMINHO_TOKEN = "/auth/oauth/v2/token"

# producao: ambiente oficial. teste: mesmo host de producao com `environment`
# TESTE (o Santander orienta testar assim; o titulo so e registrado, sem
# alteracao nem retorno). sandbox: host proprio, considerado instavel.
AMBIENTES = ("producao", "teste", "sandbox")

# Situacao (`status`) da consulta. So os tres valores que aparecem nas fontes;
# qualquer outro vira Validacao, nunca um status assumido.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "ATIVO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "BAIXADO": StatusCanonico.BAIXADO,
    },
)

# Descricao da baixa que, segundo o ACBr, indica pagamento por Pix.
BAIXA_POR_PIX = "BAIXA DE PAGAMENTO VIA PIX"

# Especie (sigla do CNAB) -> `documentKind`. So as que aparecem nas fontes.
ESPECIES = {
    "DM": "DUPLICATA_MERCANTIL",
    "NP": "NOTA_PROMISSORIA",
    "NS": "DUPLICATA_SERVICO",
    "REC": "RECIBO",
    "OUTROS": "OUTROS",
}

TIPOS_CHAVE_PIX = ("EMAIL", "CPF", "CNPJ", "CELULAR", "EVP")

# Tamanhos que o ACBr aplica antes de enviar (ele trunca; o adapter tambem).
_MAX_NOME = 40
_MAX_ENDERECO = 40
_MAX_BAIRRO = 30
_MAX_CIDADE = 20
_MAX_NOSSO_NUMERO = 12

_CENTAVO = Decimal("0.01")
_CEM = Decimal(100)

# Chaves em ingles que o `mascarar` (pensado para nomes em portugues) nao
# conhece. Somem da resposta bruta antes de ela sair do adapter.
_CHAVES_PII = frozenset(
    {
        "payer",
        "payerdata",
        "beneficiary",
        "name",
        "documentnumber",
        "address",
        "neighborhood",
        "city",
        "zipcode",
        "key",
        "dictkey",
        "qrcodepix",
        "qrcode",
        "qrcodedata",
        "qrcodeurl",
        "messages",
    }
)


def _sem_pii(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            chave: (
                MASCARA
                if isinstance(chave, str)
                and "".join(c for c in chave.lower() if c.isalnum()) in _CHAVES_PII
                else _sem_pii(item)
            )
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


@dataclass
class SantanderConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Id do workspace (tipo BILLING) ja criado no Santander para este convenio.
    workspace_id: str
    # Codigo do convenio (`covenantCode` / `beneficiaryCode`).
    convenio: str
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    especie: str = "DM"
    # Gera o nosso numero (ate 12 digitos, sem DV). Padrao: seu_numero numerico.
    # Quem garante a unicidade da sequencia e o chamador.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    # Chave Pix do beneficiario: com ela o boleto e hibrido (bolecode).
    chave_pix: str | None = field(default=None, repr=False)
    tipo_chave_pix: str | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in AMBIENTES:
            raise Validacao(f"ambiente Santander invalido: {self.ambiente!r}")
        if not self.workspace_id:
            raise Validacao("Santander exige workspace_id (workspace criado antes)")
        if not self.convenio.isdigit():
            raise Validacao("convenio Santander deve ser numerico")
        if self.especie not in ESPECIES:
            raise Validacao(
                f"especie de titulo Santander desconhecida: {self.especie!r}"
            )
        if (self.chave_pix is None) != (self.tipo_chave_pix is None):
            raise Validacao("chave_pix e tipo_chave_pix vao juntos")
        if self.tipo_chave_pix is not None and self.tipo_chave_pix not in (
            TIPOS_CHAVE_PIX
        ):
            raise Validacao(f"tipo de chave Pix invalido: {self.tipo_chave_pix!r}")

    @property
    def _host(self) -> str:
        return HOST_SANDBOX if self.ambiente == "sandbox" else HOST_PRODUCAO

    @property
    def url_base(self) -> str:
        return self._host + CAMINHO_API

    @property
    def url_token(self) -> str:
        return self._host + CAMINHO_TOKEN

    @property
    def ambiente_payload(self) -> str:
        # O ACBr manda PRODUCAO no sandbox; terceiros mandam TESTE. Seguimos o
        # lado seguro: so producao registra como PRODUCAO (ver duvidas).
        return "PRODUCAO" if self.ambiente == "producao" else "TESTE"


def _texto_valor(valor: Decimal) -> str:
    return format(valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP), "f")


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Santander") from exc


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Santander") from exc


def _documento(pessoa: Pessoa) -> dict[str, str]:
    return {
        "documentType": "CPF" if pessoa.tipo == "FISICA" else "CNPJ",
        "documentNumber": pessoa.documento,
    }


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    end = pessoa.endereco
    if end is None:
        raise Validacao("pagador sem endereco: o Santander exige o endereco")
    return {
        "name": pessoa.nome[:_MAX_NOME],
        **_documento(pessoa),
        "address": f"{end.logradouro} {end.numero}".strip()[:_MAX_ENDERECO],
        "neighborhood": end.bairro[:_MAX_BAIRRO],
        "city": end.cidade[:_MAX_CIDADE],
        "state": end.uf,
        "zipCode": f"{end.cep[:5]}-{end.cep[5:]}",
    }


def _primeiro(dados: Any) -> dict[str, Any] | None:
    """Objeto unico de uma resposta que pode vir solta, em lista ou paginada."""
    if isinstance(dados, dict) and isinstance(dados.get("_content"), list):
        dados = dados["_content"]
    if isinstance(dados, list):
        dados = dados[0] if dados else None
    return dados if isinstance(dados, dict) else None


def _primeiro_item(dados: dict[str, Any], chave: str) -> dict[str, Any]:
    lista = dados.get(chave)
    if isinstance(lista, list) and lista and isinstance(lista[0], dict):
        return lista[0]
    return {}


class SantanderAdapter(BankAdapter):
    banco: ClassVar[str] = "santander"
    validado_sandbox: ClassVar[bool] = False
    # HIBRIDO depende da chave Pix na config: a instancia sem chave a remove.
    # EMISSAO_ASSINCRONA: o registro pode voltar sem linha digitavel (o sandbox
    # faz isso, segundo o ACBr); nesse caso o titulo fica PENDENTE_REGISTRO.
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.EMISSAO_ASSINCRONA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: SantanderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        if config.chave_pix is None:
            # Sobrescreve na instancia: o boleto so e hibrido com chave Pix.
            sem_pix = type(self).capacidades & ~Capacidade.HIBRIDO
            self.capacidades = sem_pix  # type: ignore[misc]
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=config.url_token,
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

    # -- montagem ----------------------------------------------------------

    def _cabecalhos(self) -> dict[str, str]:
        return {"X-Application-Key": self._config.client_id}

    def _caminho_workspace(self) -> str:
        return f"workspaces/{quote(self._config.workspace_id, safe='')}/bank_slips"

    async def _chamar(
        self, metodo: str, caminho: str, operacao: str, json: Any = None
    ) -> httpx.Response:
        return await self._transporte.requisitar(
            metodo,
            caminho,
            operacao=operacao,
            json=json,
            cabecalhos=self._cabecalhos(),
        )

    def _nosso_numero(self, titulo: Titulo) -> str:
        gerador = self._config.gerar_nosso_numero
        bruto = str(gerador(titulo)) if gerador else titulo.seu_numero
        if not bruto.isdigit() or len(bruto) > _MAX_NOSSO_NUMERO:
            raise Validacao(
                "Santander exige nosso numero numerico de ate 12 digitos: "
                "informe gerar_nosso_numero ou use seu_numero numerico"
            )
        return bruto

    def _montar_payload(self, titulo: Titulo, nosso_numero: str) -> dict[str, Any]:
        cfg = self._config
        hoje = self._hoje().isoformat()
        nsu = ("TST" + nosso_numero) if cfg.ambiente == "teste" else nosso_numero
        payload: dict[str, Any] = {
            "environment": cfg.ambiente_payload,
            "nsuCode": nsu,
            "nsuDate": hoje,
            "covenantCode": cfg.convenio,
            "bankNumber": nosso_numero,
            "clientNumber": titulo.seu_numero,
            "dueDate": titulo.vencimento.isoformat(),
            "issueDate": hoje,
            "nominalValue": _texto_valor(titulo.valor),
            "payer": _pagador(titulo.pagador),
            "documentKind": ESPECIES[cfg.especie],
            "protestType": "SEM_PROTESTO",
            "paymentType": "REGISTRO",
        }
        if titulo.beneficiario:
            payload["beneficiary"] = {
                "name": titulo.beneficiario.nome,
                **_documento(titulo.beneficiario),
            }
        enc = titulo.encargos
        if enc.desconto_percentual is not None:
            assert enc.desconto_dias_antes is not None  # garantido pelo modelo
            # A API so conhece desconto em valor: o percentual vira valor fixo
            # sobre o nominal, com a data limite = vencimento - dias.
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            valor = titulo.valor * enc.desconto_percentual / _CEM
            payload["discount"] = {
                "type": "VALOR_DATA_FIXA",
                "discountOne": {
                    "value": _texto_valor(valor),
                    "limitDate": limite.isoformat(),
                },
            }
        if enc.multa_percentual is not None:
            payload["finePercentage"] = _texto_valor(enc.multa_percentual)
            # Multa a partir do dia seguinte ao vencimento, como nos outros bancos.
            payload["fineQuantityDays"] = "1"
        else:
            payload["finePercentage"] = "0.00"
            payload["fineQuantityDays"] = "0"
        payload["interestPercentage"] = _texto_valor(
            enc.juros_mensal_percentual or Decimal(0)
        )
        if enc.abatimento is not None:
            payload["deductionValue"] = _texto_valor(enc.abatimento)
        if titulo.dias_baixa_automatica is not None:
            payload["writeOffQuantityDays"] = str(titulo.dias_baixa_automatica)
        if cfg.chave_pix is not None:
            payload["key"] = {"type": cfg.tipo_chave_pix, "dictKey": cfg.chave_pix}
        if titulo.mensagens:
            payload["messages"] = list(titulo.mensagens)
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Santander nao e JSON", status_http=resp.status_code
            ) from exc

    # -- operacoes ---------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        nosso_numero = self._nosso_numero(titulo)
        try:
            resp = await self._chamar(
                "POST",
                self._caminho_workspace(),
                "registrar",
                self._montar_payload(titulo, nosso_numero),
            )
        except Duplicado as erro:
            # O nosso numero e nosso: quem captura reconsulta por ele.
            raise Duplicado(
                erro.mensagem,
                id_banco=nosso_numero,
                status_http=erro.status_http,
                resposta_bruta=erro.resposta_bruta,
            ) from erro
        dados = _primeiro(self._json(resp))
        if dados is None:
            raise Validacao(
                "resposta do Santander sem o boleto", status_http=resp.status_code
            )
        id_banco = str(dados.get("bankNumber") or nosso_numero)
        linha = dados.get("digitableLine")
        barras = dados.get("barCode") or dados.get("barcode")
        status = (
            StatusCanonico.REGISTRADO
            if linha or barras
            else StatusCanonico.PENDENTE_REGISTRO
        )
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=titulo.seu_numero,
            nosso_numero=id_banco,
            linha_digitavel=linha,
            codigo_barras=barras,
            pix_copia_cola=dados.get("qrCodePix"),
            txid=dados.get("txId"),
            resposta_bruta=_sem_pii(dados),
        )

    async def _consulta_por_tipo(self, id_banco: str, tipo: str) -> dict[str, Any]:
        bill_id = quote(f"{self._config.convenio}.{id_banco}", safe="")
        resp = await self._chamar(
            "GET",
            f"bills/{bill_id}?{urlencode({'tipoConsulta': tipo})}",
            f"consultar_{tipo}",
        )
        return _primeiro(self._json(resp)) or {}

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        consulta = urlencode(
            {"beneficiaryCode": self._config.convenio, "bankNumber": id_banco}
        )
        resp = await self._chamar("GET", f"bills?{consulta}", "consultar")
        dados = _primeiro(self._json(resp))
        if dados is None:
            raise Validacao(
                "consulta do Santander sem o boleto", status_http=resp.status_code
            )
        status = self.tabela_status.traduzir(dados.get("status"))
        boleto: dict[str, Any] = {}
        if self.suporta(Capacidade.HIBRIDO):
            boleto = await self._consulta_por_tipo(id_banco, "bankslip")
        liquidacao: dict[str, Any] = {}
        if status in (StatusCanonico.LIQUIDADO, StatusCanonico.BAIXADO):
            liquidacao = await self._consulta_por_tipo(id_banco, "settlement")
        acerto = _primeiro_item(liquidacao, "settlementData") or _primeiro_item(
            dados, "settlementData"
        )
        baixa = _primeiro_item(liquidacao, "writeOffData") or _primeiro_item(
            dados, "writeOffData"
        )
        if (
            status is StatusCanonico.BAIXADO
            and str(baixa.get("writeOffDescription") or "").upper() == BAIXA_POR_PIX
        ):
            status = StatusCanonico.LIQUIDADO
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            valor_pago = (
                _dinheiro(dados.get("paidValue"))
                or _dinheiro(acerto.get("settlementCreditedValue"))
                or _dinheiro(baixa.get("writeOffValue"))
            )
            data_pagamento = _data(acerto.get("settlementDate")) or _data(
                baixa.get("writeOffDate")
            )
        dados_boleto = boleto.get("bankSlipData") or boleto.get("bankslipData") or {}
        qr = boleto.get("qrCodeData") or {}
        bruta = {"consulta": dados}
        if boleto:
            bruta["bankslip"] = boleto
        if liquidacao:
            bruta["settlement"] = liquidacao
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=dados.get("clientNumber"),
            nosso_numero=str(dados.get("bankNumber") or id_banco),
            linha_digitavel=dados_boleto.get("digitableLine")
            or dados.get("digitableLine"),
            codigo_barras=dados_boleto.get("barCode")
            or dados_boleto.get("barcode")
            or dados.get("barcode"),
            pix_copia_cola=qr.get("qrCode"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pii(bruta),
        )

    async def _instruir(self, id_banco: str, campos: dict[str, Any]) -> Any:
        corpo = {
            "covenantCode": self._config.convenio,
            "bankNumber": id_banco,
            **campos,
        }
        resp = await self._chamar(
            "PATCH", self._caminho_workspace(), "instrucao", corpo
        )
        return self._json(resp)

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
            campos: dict[str, Any] = {
                "dueDate": instrucao.nova_data_vencimento.isoformat()
            }
        elif tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            campos = {"deductionValue": _texto_valor(instrucao.valor)}
        elif tipo is TipoInstrucao.PROTESTO:
            campos = {"operation": "PROTESTAR"}
        else:
            raise NaoSuportado(f"santander: instrucao {tipo.value} nao implementada")
        await self._instruir(id_banco, campos)
        # A consulta e a fonte da verdade do estado apos a alteracao.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        dados = await self._instruir(id_banco, {"operation": "BAIXAR"})
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            resposta_bruta=_sem_pii(dados) if isinstance(dados, dict) else {},
        )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado(
            "santander: formato do webhook do workspace nao e publico; use "
            "consultar_titulo (docs/santander-duvidas.md)"
        )
