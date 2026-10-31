"""Santander (033), API de Cobranca "Gestao de Boletos" v2 (workspace, OAuth2 + mTLS).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Santander, porque nao
ha credencial. O contrato vem da documentacao oficial publica do portal
developer.santander.com.br (OpenAPI 1.0.0, guias e colecoes Postman, lidos em
31/10/2026); o que a fonte oficial nao responde esta marcado em
`docs/santander-duvidas.md`, com o caminho do teste.

Pontos do contrato:

- Tudo passa por um *workspace* criado antes, fora do adapter (tipo `BILLING`,
  convenios e, opcionalmente, URL de webhook). Registro e instrucao vao em
  `/workspaces/{id}/bank_slips`; a consulta, em `/bills`, fora do workspace.
- Token `client_credentials` com `client_id` e `client_secret` no formulario; o
  token dura 15 minutos e fica amarrado ao certificado mTLS. Toda chamada de
  negocio leva `X-Application-Key: client_id`.
- O nosso numero (`bankNumber`, ate 13 digitos, sem DV na API) e escolhido por quem
  registra: vem de `SantanderConfig.gerar_nosso_numero` ou do `seu_numero` numerico.
  Sem zeros a esquerda ele e o `id_banco`, porque o webhook e parte das respostas
  trazem o numero completado com zeros.
- Ambientes: `producao` e `teste` usam o host `trust-open`; em `teste` o banco valida
  e responde, mas nao registra nada, entao so resta registrar (sem instrucao, consulta
  ou PDF). `sandbox` usa `trust-sandbox`, com dados ficticios e `environment=PRODUCAO`,
  como na colecao Postman oficial do sandbox.
- Registro e instrucao sao processados na hora, segundo o guia: `EMISSAO_ASSINCRONA`
  e `INSTRUCAO_ASSINCRONA` nao se aplicam. Um registro sem linha digitavel contraria o
  guia e volta como `PENDENTE_REGISTRO`, para a consulta completar depois.
- A consulta junta ate tres respostas: `/bills` por nosso numero (situacao, complemento
  e valor pago), `tipoConsulta=bankslip` (linha digitavel e codigo de barras, com o
  titulo ativo) e `tipoConsulta=settlement` (liquidacao ou baixa). O Pix so vem na
  consulta sonda, que existe ate D+2 do registro.
- Pagamento pelo QR Code do Pix aparece como `Baixado`, com a indicacao de pagamento via
  Pix: o adapter traduz para `LIQUIDADO`. O texto exato da indicacao nao e oficial; sem
  ele o titulo fica `BAIXADO`, nunca liquida por suposicao.
- Pagamento de hoje por linha digitavel fica `Ativo` com complemento "Baixa Operacional"
  ate D+1. Continua `REGISTRADO`: o banco ainda pode estornar.
- Webhook: formato oficial, sem assinatura. O mTLS do envio e opcional e so o servidor
  que recebe pode conferir, entao o evento so manda reconsultar, nunca liquida. So fica
  ativo com `webhook_ativo=True`, porque depende de o workspace ter a URL.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.documento import normalizar_documento
from brbank.erros import Duplicado, Indisponivel, NaoSuportado, Validacao
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

# producao: registro real. teste: mesmo host com `environment` TESTE; o banco valida
# e responde, mas nao registra. sandbox: host proprio, com dados ficticios.
AMBIENTES = ("producao", "teste", "sandbox")

# Situacao (`status`) da consulta, lista oficial; a API escreve em caixa mista
# ("Liquidado Parcialmente"). Qualquer outro valor vira Validacao.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "ATIVO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "BAIXADO": StatusCanonico.BAIXADO,
        # O modelo canonico nao tem pagamento parcial e o titulo segue aberto no
        # banco: nao liquida. So acontece com `paymentType=PARCIAL`, que o adapter
        # nao envia. Valor e data ficam na resposta bruta.
        "LIQUIDADO PARCIALMENTE": StatusCanonico.REGISTRADO,
    },
)

# `function` do webhook. Os dois tipos so mandam reconsultar. O estorno devolve o
# titulo ao estado aberto, se a consulta confirmar.
MENSAGEM_WEBHOOK = "WBHKPAGEST"
TABELA_WEBHOOK = TabelaStatus.criar(
    "2026.10",
    {
        "PAGAMENTO": StatusCanonico.LIQUIDADO,
        "ESTORNO": StatusCanonico.REGISTRADO,
    },
)

# O guia oficial diz que o status Baixado indica "pagamento via PIX" no complemento
# ou na descricao da baixa, sem dar o texto. O ACBr traz "BAIXA DE PAGAMENTO VIA PIX".
INDICACAO_BAIXA_PIX = "PAGAMENTO VIA PIX"

# Sigla -> `documentKind` (enum oficial). Ficam de fora BOLETO_CARTAO_CREDITO,
# BOLETO_PROPOSTA e BOLETO_DEPOSITO_APORTE, que exigem outro tipo de pagamento.
ESPECIES = {
    "DM": "DUPLICATA_MERCANTIL",
    "NS": "DUPLICATA_SERVICO",
    "NP": "NOTA_PROMISSORIA",
    "NPR": "NOTA_PROMISSORIA_RURAL",
    "REC": "RECIBO",
    "AP": "APOLICE_SEGURO",
    "CH": "CHEQUE",
    "NPD": "NOTA_PROMISSORIA_DIRETA",
    "OUTROS": "OUTROS",
}

TIPOS_CHAVE_PIX = ("EMAIL", "CPF", "CNPJ", "CELULAR", "EVP")

# Tamanhos do guia oficial. Nome e endereco sao truncados, como faz o ACBr.
_MAX_NOME = 40
_MAX_ENDERECO = 40
_MAX_BAIRRO = 30
_MAX_CIDADE = 20
_MAX_NOSSO_NUMERO = 13
_MAX_CONVENIO = 9
_MAX_SEU_NUMERO = 15
_MAX_MENSAGENS = 45
_MAX_TEXTO_MENSAGEM = 100
_MAX_DIAS_BAIXA = 99
_MAX_PERCENTUAL_ENCARGO = Decimal(60)
# Consulta sonda: so existe ate D+2 do registro.
_DIAS_SONDA = 2
# Codigo oficial de "Nosso Numero ja registrado" (o banco responde 400).
_CODIGO_JA_REGISTRADO = 1
_SEU_NUMERO_PROIBIDO = re.compile(r"[^0-9A-Za-z ]")

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
        # Bloco de avalista e campos soltos do webhook e da consulta.
        "guarantordata",
        "payername",
        "payerdocumentnumber",
        "agreementdocument",
        "finalbeneficiarydocumentnumber",
        "finalbeneficiaryname",
        # Link assinado do PDF: quem tem o link baixa o boleto.
        "link",
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
    # Id do workspace (tipo BILLING) ja criado; o convenio tem de estar nele.
    workspace_id: str
    # Codigo do convenio, no mesmo formato usado no workspace (com ou sem zeros).
    convenio: str
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    especie: str = "DM"
    # Gera o nosso numero (ate 13 digitos, sem DV). Padrao: seu_numero numerico.
    # Quem garante a unicidade da sequencia e o chamador.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    # Chave Pix do beneficiario: com ela o boleto e hibrido (bolecode).
    chave_pix: str | None = field(default=None, repr=False)
    tipo_chave_pix: str | None = None
    # CPF/CNPJ de quem tem o convenio. O banco recusa beneficiario final igual a ele
    # (ou com a mesma raiz de CNPJ): com este campo, o adapter nao o envia nesse caso.
    documento_beneficiario: str | None = field(default=None, repr=False)
    # O workspace tem URL de webhook cadastrada: liga a capacidade WEBHOOK.
    webhook_ativo: bool = False
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in AMBIENTES:
            raise Validacao(f"ambiente Santander invalido: {self.ambiente!r}")
        if not self.workspace_id:
            raise Validacao("Santander exige workspace_id (workspace criado antes)")
        if not self.convenio.isdigit() or len(self.convenio) > _MAX_CONVENIO:
            raise Validacao("convenio Santander deve ter ate 9 digitos")
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
        if self.documento_beneficiario is not None:
            try:
                self.documento_beneficiario = normalizar_documento(
                    self.documento_beneficiario
                )
            except ValueError as exc:
                raise Validacao("documento_beneficiario invalido") from exc

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
        # A colecao Postman oficial do sandbox manda PRODUCAO. TESTE so existe no host
        # de producao, e exige NSU com prefixo TST.
        return "TESTE" if self.ambiente == "teste" else "PRODUCAO"


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


def _sem_zeros(numero: Any) -> str:
    return str(numero).lstrip("0")


def _mesmo_titular(documento: str, outro: str) -> bool:
    """Mesmo documento ou, em CNPJ, a mesma raiz: o banco recusa os dois casos."""
    if len(documento) == 14 and len(outro) == 14:
        return documento[:8] == outro[:8]
    return documento == outro


def _baixa_por_pix(texto: Any) -> bool:
    return INDICACAO_BAIXA_PIX in " ".join(str(texto or "").upper().split())


def _codigos_de_erro(resposta: Any) -> set[int]:
    """Codigos de `_errors[]._code`; o OpenAPI diz inteiro, o guia mostra "0001"."""
    if isinstance(resposta, str | bytes):
        try:
            resposta = json.loads(resposta)
        except ValueError:
            return set()
    if not isinstance(resposta, dict) or not isinstance(resposta.get("_errors"), list):
        return set()
    codigos = set()
    for item in resposta["_errors"]:
        codigo = str(item.get("_code", "")) if isinstance(item, dict) else ""
        if codigo.isdigit():
            codigos.add(int(codigo))
    return codigos


def _momento_webhook(texto: Any) -> datetime | None:
    """`paymentDate` no formato AAAA-MM-DD-HH.MM.SS.NNNNNN, sem fuso."""
    if not texto:
        return None
    try:
        return datetime.strptime(str(texto), "%Y-%m-%d-%H.%M.%S.%f")
    except ValueError as exc:
        raise Validacao("data do webhook do Santander ilegivel") from exc


def _primeiro(dados: Any) -> dict[str, Any] | None:
    """Objeto unico de uma resposta solta, em lista ou paginada.

    O schema oficial diz que `_content` e lista (as telas confirmam); os exemplos do
    OpenAPI mostram objeto. Os dois valem aqui.
    """
    if isinstance(dados, dict) and isinstance(dados.get("_content"), list | dict):
        dados = dados["_content"]
    if isinstance(dados, list):
        dados = dados[0] if dados else None
    return dados if isinstance(dados, dict) else None


def _primeiro_item(dados: dict[str, Any], chave: str) -> dict[str, Any]:
    lista = dados.get(chave)
    if isinstance(lista, list) and lista and isinstance(lista[0], dict):
        return lista[0]
    return {}


def _fora_da_instancia(config: SantanderConfig) -> Capacidade:
    fora = Capacidade.NENHUMA
    if config.chave_pix is None:
        fora |= Capacidade.HIBRIDO
    if not config.webhook_ativo:
        fora |= Capacidade.WEBHOOK
    if config.ambiente == "sandbox":
        # O guia e as colecoes oficiais so trazem a rota de PDF em producao.
        fora |= Capacidade.PDF
    if config.ambiente == "teste":
        # O guia: registro em TESTE nao aceita instrucao nem consulta. So resta
        # conferir o registro.
        fora |= (
            Capacidade.ALTERACAO
            | Capacidade.BAIXA
            | Capacidade.PDF
            | Capacidade.WEBHOOK
        )
    return fora


class SantanderAdapter(BankAdapter):
    banco: ClassVar[str] = "santander"
    validado_sandbox: ClassVar[bool] = False
    # A instancia remove o que a config e o ambiente nao permitem: ver `__init__`.
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
        config: SantanderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        # Sobrescreve na instancia o que a config e o ambiente nao permitem.
        self.capacidades = type(self).capacidades & ~_fora_da_instancia(  # type: ignore[misc]
            config
        )
        if client is None and not config.certificado_pem:
            raise Validacao(
                "Santander exige certificado mTLS: o token fica preso a ele"
            )
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
        numero = _sem_zeros(bruto)
        if not bruto.isdigit() or len(bruto) > _MAX_NOSSO_NUMERO or not numero:
            raise Validacao(
                "Santander exige nosso numero numerico de 1 a 13 digitos, diferente "
                "de zero: informe gerar_nosso_numero ou use seu_numero numerico"
            )
        return numero

    @staticmethod
    def _seu_numero(titulo: Titulo) -> str | None:
        """So letras, numeros e espaco, ate 15 (guia oficial, nota 42)."""
        texto = _SEU_NUMERO_PROIBIDO.sub("", titulo.seu_numero).strip()
        if len(texto) > _MAX_SEU_NUMERO:
            raise Validacao("seu_numero do Santander tem no maximo 15 caracteres")
        return texto or None

    @staticmethod
    def _mensagens(titulo: Titulo) -> list[str]:
        if len(titulo.mensagens) > _MAX_MENSAGENS:
            raise Validacao("o Santander aceita ate 45 mensagens por boleto")
        return [texto[:_MAX_TEXTO_MENSAGEM] for texto in titulo.mensagens]

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
            "dueDate": titulo.vencimento.isoformat(),
            "issueDate": hoje,
            "nominalValue": _texto_valor(titulo.valor),
            "payer": _pagador(titulo.pagador),
            "documentKind": ESPECIES[cfg.especie],
            "protestType": "SEM_PROTESTO",
            "paymentType": "REGISTRO",
        }
        seu_numero = self._seu_numero(titulo)
        if seu_numero:
            payload["clientNumber"] = seu_numero
        final = titulo.beneficiario
        if final and not (
            cfg.documento_beneficiario
            and _mesmo_titular(final.documento, cfg.documento_beneficiario)
        ):
            payload["beneficiary"] = {
                "name": final.nome[:_MAX_NOME],
                **_documento(final),
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
        for encargo in (enc.multa_percentual, enc.juros_mensal_percentual):
            if encargo is not None and encargo > _MAX_PERCENTUAL_ENCARGO:
                raise Validacao("o Santander limita multa e juros a 60%")
        if enc.multa_percentual is not None:
            payload["finePercentage"] = _texto_valor(enc.multa_percentual)
            # Dias apos o vencimento em que a multa comeca a valer.
            payload["fineQuantityDays"] = "1"
        if enc.juros_mensal_percentual is not None:
            payload["interestPercentage"] = _texto_valor(enc.juros_mensal_percentual)
        if enc.abatimento is not None:
            payload["deductionValue"] = _texto_valor(enc.abatimento)
        if titulo.dias_baixa_automatica is not None:
            # 0: perfil do convenio. 1 a 98: dias. 99: nao baixar.
            if titulo.dias_baixa_automatica > _MAX_DIAS_BAIXA:
                raise Validacao("o Santander aceita ate 99 dias para a baixa")
            payload["writeOffQuantityDays"] = str(titulo.dias_baixa_automatica)
        if cfg.chave_pix is not None:
            payload["key"] = {"type": cfg.tipo_chave_pix, "dictKey": cfg.chave_pix}
        if titulo.mensagens:
            payload["messages"] = self._mensagens(titulo)
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
        except Validacao as erro:
            # O banco responde 400 com o codigo 0001 para nosso numero repetido.
            if _CODIGO_JA_REGISTRADO not in _codigos_de_erro(erro.resposta_bruta):
                raise
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
        id_banco = _sem_zeros(dados.get("bankNumber") or nosso_numero)
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

    async def _sonda(self, id_banco: str, emissao: date | None) -> dict[str, Any]:
        """Repete a resposta do registro, com o Pix. So existe ate D+2 do registro.

        A chave usa o NSU e a data do NSU, que este adapter faz iguais ao nosso numero
        e a data de emissao. Titulo registrado por fora nao tem essa chave: sem sonda.
        """
        if emissao is None or not (0 <= (self._hoje() - emissao).days <= _DIAS_SONDA):
            return {}
        cfg = self._config
        indicador = "T" if cfg.ambiente == "teste" else "P"
        chave = (
            f"{id_banco}.{emissao.isoformat()}.{indicador}.{cfg.convenio}.{id_banco}"
        )
        try:
            resp = await self._chamar(
                "GET",
                f"{self._caminho_workspace()}/{quote(chave, safe='')}",
                "consultar_sonda",
            )
        except Validacao:
            return {}
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
        status = self.tabela_status.traduzir(str(dados.get("status") or "").strip())
        boleto: dict[str, Any] = {}
        sonda: dict[str, Any] = {}
        if status is StatusCanonico.REGISTRADO:
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
        if status is StatusCanonico.BAIXADO and (
            _baixa_por_pix(dados.get("statusComplement"))
            or _baixa_por_pix(baixa.get("writeOffDescription"))
        ):
            status = StatusCanonico.LIQUIDADO
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            # O valor creditado vem liquido da tarifa: para conciliar vale o pago.
            valor_pago = (
                _dinheiro(dados.get("paidValue"))
                or _dinheiro(acerto.get("settlementValue"))
                or _dinheiro(acerto.get("settlementCreditedValue"))
                or _dinheiro(baixa.get("writeOffValue"))
            )
            data_pagamento = _data(acerto.get("settlementDate")) or _data(
                baixa.get("writeOffDate")
            )
        dados_boleto = boleto.get("bankslipData") or boleto.get("bankSlipData") or {}
        # `qrCodeData` so aparece no ACBr; a fonte oficial do Pix e a sonda.
        pix = (boleto.get("qrCodeData") or {}).get("qrCode")
        if (
            not pix
            and status is StatusCanonico.REGISTRADO
            and self.suporta(Capacidade.HIBRIDO)
        ):
            sonda = await self._sonda(id_banco, _data(dados.get("issueDate")))
            pix = sonda.get("qrCodePix")
        bruta = {"consulta": dados}
        if boleto:
            bruta["bankslip"] = boleto
        if liquidacao:
            bruta["settlement"] = liquidacao
        if sonda:
            bruta["sonda"] = sonda
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=dados.get("clientNumber"),
            nosso_numero=_sem_zeros(dados.get("bankNumber") or id_banco),
            linha_digitavel=dados_boleto.get("digitableLine")
            or dados.get("digitableLine"),
            codigo_barras=dados_boleto.get("barCode")
            or dados_boleto.get("barcode")
            or dados.get("barcode"),
            pix_copia_cola=pix,
            txid=sonda.get("txId"),
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
        self.exigir(Capacidade.BAIXA)
        dados = await self._instruir(id_banco, {"operation": "BAIXAR"})
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            resposta_bruta=_sem_pii(dados) if isinstance(dados, dict) else {},
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        """Pede o link do PDF e baixa o arquivo.

        So funciona com o titulo ativo. A rota exige o documento do pagador, que o
        contrato nao traz: vem de `tipoConsulta=bankslip`. O link vale 5 minutos e o
        guia proibe mostra-lo ao cliente final: nao sai daqui nem vai para log.
        """
        self.exigir(Capacidade.PDF)
        boleto = await self._consulta_por_tipo(id_banco, "bankslip")
        documento = str(
            (boleto.get("payerData") or {}).get("payerDocumentNumber") or ""
        )
        if not documento.isdigit():
            # O OpenAPI define o campo como inteiro: CNPJ alfanumerico nao cabe.
            raise Validacao("consulta do Santander sem documento numerico do pagador")
        # Aqui a ordem e nosso numero.convenio, ao contrario da consulta.
        bill_id = quote(f"{id_banco}.{self._config.convenio}", safe="")
        resp = await self._chamar(
            "POST",
            f"bills/{bill_id}/bank_slips",
            "pdf",
            {"payerDocumentNumber": int(documento)},
        )
        dados = self._json(resp)
        link = dados.get("link") if isinstance(dados, dict) else None
        if not isinstance(link, str) or not link.startswith("https://"):
            raise Validacao("resposta do Santander sem link https do PDF")
        return await self._baixar_pdf(link)

    async def _baixar_pdf(self, link: str) -> bytes:
        """O link e assinado e de outro host: vai sem token nem application key."""
        try:
            resp = await self._transporte.client.get(link)
        except httpx.TransportError as exc:
            # Sem encadear: a excecao original pode citar o link.
            raise Indisponivel(
                f"erro de transporte no PDF: {type(exc).__name__}"
            ) from None
        if resp.status_code >= 500:
            raise Indisponivel(
                f"PDF respondeu HTTP {resp.status_code}", status_http=resp.status_code
            )
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            raise Validacao(
                f"download do PDF falhou (HTTP {resp.status_code})",
                status_http=resp.status_code,
            )
        return resp.content

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook de pagamento e estorno do Santander. Sem assinatura.

        O guia chama a notificacao de confirmacao efetiva, mas nada no corpo prova a
        origem (o mTLS e opcional e fica no servidor que recebe). O chamador DEVE
        reconsultar (`consultar_titulo`) antes de dar como pago. O corpo de validacao
        da URL, que o banco envia ao cadastrar, nao gera evento. Outro formato (o guia
        cita um diferente para quem segue a Circular 3.978/2020) e recusado.
        """
        self.exigir(Capacidade.WEBHOOK)
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo, parse_float=Decimal)
            except ValueError as exc:
                raise Validacao("webhook do Santander nao e JSON") from exc
        eventos = []
        for item in corpo if isinstance(corpo, list) else [corpo]:
            evento = self._evento_webhook(item, cabecalhos)
            if evento is not None:
                eventos.append(evento)
        return eventos

    def _evento_webhook(
        self, item: Any, cabecalhos: Mapping[str, str] | None
    ) -> EventoCanonico | None:
        if not isinstance(item, dict):
            raise Validacao("webhook do Santander fora do formato")
        if item.get("message") != MENSAGEM_WEBHOOK:
            if set(item) == {"message"}:
                return None
            raise Validacao("webhook do Santander fora do formato")
        convenio = _sem_zeros(item.get("covenant") or "")
        if convenio and convenio != _sem_zeros(self._config.convenio):
            raise Validacao("webhook do Santander de outro convenio")
        referencia = _sem_zeros(item.get("bankNumber") or "")
        if not referencia:
            raise Validacao("webhook do Santander sem nosso numero")
        tipo = TABELA_WEBHOOK.traduzir(item.get("function"))
        bruta = _sem_pii(item)
        for nome, valor in (cabecalhos or {}).items():
            if nome.lower() == "notification-uuid":
                bruta["notification-uuid"] = valor
        return EventoCanonico(
            banco=self.banco,
            referencia=referencia,
            tipo=tipo,
            ocorrido_em=_momento_webhook(item.get("paymentDate")),
            valor_pago=(
                _dinheiro(item.get("payedValue"))
                if tipo is StatusCanonico.LIQUIDADO
                else None
            ),
            resposta_bruta=bruta,
        )
