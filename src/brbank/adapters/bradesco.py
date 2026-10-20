"""Bradesco (237), API Cobranca do portal Bradesco Developers (OAuth2 + mTLS).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Bradesco. O
contrato foi montado de fontes de terceiros, porque o portal oficial
(developers.bradesco.com.br) so entrega a especificacao por JavaScript e com
login. Fontes, divergencias e duvidas: `docs/bradesco-duvidas.md`.

Nao ha modulo Odoo de origem. O ACBr (LGPL) foi lido so como documentacao de
endpoints e quirks; nenhum codigo foi portado.

Produto coberto: cobranca convencional (boleto sem QR Code), um microservico
por operacao no host `openapi.bradesco.com.br`:

- token: `POST /auth/server-mtls/v2/token`, `client_credentials` com
  `client_id` e `client_secret` no formulario, sempre sobre mTLS. O fluxo
  legado (JWT assinado + `X-Brad-Signature` por requisicao) NAO e coberto.
- registro: `POST /boleto/cobranca-registro/v1/cobranca`
- consulta: `POST /boleto/cobranca-consulta/v1/consultar` (consulta e POST)
- baixa: `POST /boleto/cobranca-baixa/v1/baixar`

Quirks: CPF/CNPJ do beneficiario decomposto em raiz, filial e controle;
`negociacao` = agencia (4) + conta (7), sem digitos; datas `DD.MM.AAAA` no
registro e `DDMMAAAA` (numero) na consulta; o id do titulo e o nosso numero
gerado pelo banco (`nuTituloGerado`).
"""

import asyncio
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.documento import normalizar_documento
from brbank.erros import NaoSuportado, Validacao
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

URL_PRODUCAO = "https://openapi.bradesco.com.br"
URL_SANDBOX = "https://openapisandbox.prebanco.com.br"
CAMINHO_TOKEN = "/auth/server-mtls/v2/token"
CAMINHO_REGISTRO = "boleto/cobranca-registro/v1/cobranca"
CAMINHO_CONSULTA = "boleto/cobranca-consulta/v1/consultar"
CAMINHO_BAIXA = "boleto/cobranca-baixa/v1/baixar"

# codigoBaixa enviado no pedido de baixa: 57 aparece no ACBr e na collection
# Postman do Bradesco; a consulta depois devolve codStatus 57 "CONFORME SEU PEDIDO".
CODIGO_BAIXA_PEDIDO = 57

# codStatus da consulta (`titulo.codStatus`). So entram codigos com significado
# lido em fonte; o resto vira Validacao (docs/bradesco-duvidas.md, item Status).
# 51 fica de fora de proposito: uma fonte diz "POR ACERTO" (Pix pago, conferido
# no dia seguinte) e outra o trata como baixa sem pagamento.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "1": StatusCanonico.REGISTRADO,  # A VENCER / VENCIDO
        "13": StatusCanonico.LIQUIDADO,  # PAGO NO DIA (pode vir sem valor e data)
        "57": StatusCanonico.BAIXADO,  # CONFORME SEU PEDIDO
        "61": StatusCanonico.LIQUIDADO,  # PAGO
        "62": StatusCanonico.LIQUIDADO,  # PAGO EM CARTORIO
    },
)

# cdEspecieTitulo por sigla, como lido no ACBr (TabelaEspecieDocumentos).
ESPECIES = {
    "CH": 1, "DM": 2, "DMI": 3, "DS": 4, "DSI": 5, "DR": 6, "LC": 7, "NCC": 8,
    "NCE": 9, "NCI": 10, "NCR": 11, "NP": 12, "NPR": 13, "TM": 14, "TS": 15,
    "NS": 16, "RC": 17, "FAT": 18, "ND": 19, "AP": 20, "ME": 21, "PC": 22,
    "DD": 23, "CCB": 24, "FI": 25, "RD": 26, "DRI": 27, "EC": 28, "ECI": 29,
    "CC": 31, "BDP": 32, "OUT": 99,
}  # fmt: skip

_CENTAVO = Decimal("0.01")
_MAX_MENSAGENS = 4
_TAMANHO_MENSAGEM = 80

# Trechos de chave que denunciam dado pessoal na resposta do Bradesco. O
# `mascarar` do core so conhece nomes exatos (nomePagador, cpf...), e a API
# devolve nomeSacado, cnpjSacado, endSacado, cepSacado e afins.
_TRECHOS_PII = (
    "sacad",
    "pagador",
    "avalista",
    "cedente",
    "benef",
    "endced",
    "cpf",
    "cnpj",
    "cgc",
    "nome",
    "endereco",
    "logradouro",
    "cep",
    "bairro",
    "municipio",
    "cidade",
    "email",
    "fone",
    "chave",
    "base64",
)


@dataclass
class BradescoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # CPF ou CNPJ (numerico) do beneficiario dono do contrato de cobranca.
    documento_beneficiario: str = field(repr=False)
    agencia: str
    conta_corrente: str
    # idProduto/produto (carteira), ex.: 9.
    carteira: int
    especie: str = "DM"
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # Fontes divergem: ACBr e SDKs mandam "Bearer <token>"; a collection
    # Postman e uma integracao em producao mandam o token cru.
    token_sem_bearer: bool = False
    # Fontes divergem: ACBr e SDK mandam centavos (15000); uma integracao em
    # producao manda texto com duas casas ("150.00").
    valor_em_centavos: bool = True
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Bradesco invalido: {self.ambiente!r}")
        if not self.agencia.isdigit() or len(self.agencia) > 4:
            raise Validacao("agencia Bradesco: ate 4 digitos, sem o digito")
        if not self.conta_corrente.isdigit() or len(self.conta_corrente) > 7:
            raise Validacao("conta Bradesco: ate 7 digitos, sem o digito")
        if not 0 < self.carteira <= 999:
            raise Validacao("carteira (idProduto) Bradesco invalida")
        if self.especie not in ESPECIES:
            raise Validacao(
                f"especie de titulo Bradesco desconhecida: {self.especie!r}"
            )
        try:
            documento = normalizar_documento(self.documento_beneficiario)
        except ValueError as exc:
            raise Validacao("documento do beneficiario invalido") from exc
        if not documento.isdigit():
            raise Validacao(
                "Bradesco: CNPJ alfanumerico do beneficiario sem contrato conhecido"
            )
        self.documento_beneficiario = documento

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return self.url_base + CAMINHO_TOKEN


class _AuthBradesco(OAuth2ClientCredentials):
    """client_credentials com credenciais no formulario (mTLS na mesma conexao).

    `sem_bearer` manda o token cru no `Authorization`, como a collection
    Postman do Bradesco.
    """

    def __init__(self, *, sem_bearer: bool, **kw: Any) -> None:
        super().__init__(**kw)
        self._sem_bearer = sem_bearer

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        token = await self._token(escopo, client)
        return {"Authorization": token if self._sem_bearer else f"Bearer {token}"}


def _texto(valor: str, tamanho: int) -> str:
    """Sem acento (ACBr e a integracao de producao tiram) e no tamanho do campo."""
    decomposto = unicodedata.normalize("NFKD", valor)
    limpo = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(limpo.split())[:tamanho]


def _decompor(documento: str) -> tuple[str, str, str]:
    """Raiz, filial e controle. CNPJ: 8 + 4 + 2. CPF: 9 + "0" + 2."""
    if len(documento) == 14:
        return documento[:8], documento[8:12], documento[12:]
    return documento[:9], "0", documento[9:]


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _fmt(dia: date) -> str:
    return dia.strftime("%d.%m.%Y")


def _data_ddmmaaaa(valor: Any) -> date | None:
    """DDMMAAAA como numero ou texto; pode perder o zero a esquerda. 0 = vazio."""
    digitos = "".join(c for c in str(valor or "") if c.isdigit())
    if not digitos or not digitos.strip("0"):
        return None
    texto = digitos.zfill(8)
    try:
        return date(int(texto[4:8]), int(texto[2:4]), int(texto[:2]))
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Bradesco") from exc


def _dinheiro(valor: Any) -> Decimal | None:
    """`vlrPagto` em reais (ver duvidas). 0 ou vazio = sem pagamento."""
    if valor is None or valor == "":
        return None
    try:
        decimal = Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor ilegivel na resposta do Bradesco") from exc
    return decimal or None


def _codigo(valor: Any) -> str | None:
    """codStatus como texto canonico ("01" e 1 viram "1")."""
    if valor is None or str(valor).strip() == "":
        return None
    texto = str(valor).strip()
    return str(int(texto)) if texto.isdigit() else texto


def _sem_pii(valor: Any) -> Any:
    if isinstance(valor, dict):
        saida = {}
        for chave, item in valor.items():
            normal = "".join(c for c in str(chave).lower() if c.isalnum())
            if any(trecho in normal for trecho in _TRECHOS_PII):
                saida[chave] = MASCARA
            else:
                saida[chave] = _sem_pii(item)
        return saida
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


def _endereco_obrigatorio(pessoa: Pessoa, papel: str) -> Any:
    if pessoa.endereco is None:
        raise Validacao(f"{papel} sem endereco: o Bradesco exige o endereco")
    return pessoa.endereco


class BradescoAdapter(BankAdapter):
    banco: ClassVar[str] = "bradesco"
    validado_sandbox: ClassVar[bool] = False
    # Sem ALTERACAO: o corpo do PUT de alteracao da cobranca convencional nao
    # tem fonte confiavel (docs/bradesco-duvidas.md). Sem PDF e sem webhook.
    capacidades: ClassVar[Capacidade] = Capacidade.BAIXA
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: BradescoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = _AuthBradesco(
            sem_bearer=config.token_sem_bearer,
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

    def _negociacao(self) -> int:
        """agencia (4) + conta (7), sem digitos: consulta e baixa."""
        cfg = self._config
        return int(cfg.agencia.zfill(4) + cfg.conta_corrente.zfill(7))

    def _nu_negociacao(self) -> str:
        """agencia (4) + 7 zeros + conta (7) = 18 posicoes: registro."""
        cfg = self._config
        return cfg.agencia.zfill(4) + "0" * 7 + cfg.conta_corrente.zfill(7)

    def _valor(self, valor: Decimal) -> int | str:
        if self._config.valor_em_centavos:
            return int((valor * 100).to_integral_value())
        return f"{valor:.2f}"

    @staticmethod
    def _nosso_numero(id_banco: str) -> int | str:
        # Numero quando possivel (exemplos de consulta); id fora do formato vai
        # como veio e o banco recusa.
        return int(id_banco) if id_banco.isdigit() else id_banco

    def _pagador(self, pessoa: Pessoa) -> dict[str, Any]:
        end = _endereco_obrigatorio(pessoa, "pagador")
        if not pessoa.documento.isdigit():
            raise Validacao("Bradesco: CNPJ alfanumerico do pagador sem contrato")
        return {
            "nomePagador": _texto(pessoa.nome, 70),
            "logradouroPagador": _texto(end.logradouro, 40),
            "nuLogradouroPagador": _texto(end.numero, 10),
            "bairroPagador": _texto(end.bairro, 40),
            "municipioPagador": _texto(end.cidade, 30),
            "ufPagador": end.uf,
            "cepPagador": end.cep[:5],
            "complementoCepPagador": end.cep[5:],
            "cdIndCpfcnpjPagador": 1 if pessoa.tipo == "FISICA" else 2,
            "nuCpfcnpjPagador": pessoa.documento,
        }

    @staticmethod
    def _sacador_avalista(pessoa: Pessoa) -> dict[str, Any]:
        if not pessoa.documento.isdigit():
            raise Validacao("Bradesco: CNPJ alfanumerico do avalista sem contrato")
        dados: dict[str, Any] = {
            "nomeSacadorAvalista": _texto(pessoa.nome, 40),
            "cdIndCpfcnpjSacadorAvalista": 1 if pessoa.tipo == "FISICA" else 2,
            "nuCpfcnpjSacadorAvalista": pessoa.documento,
        }
        end = pessoa.endereco
        if end is not None:
            dados.update(
                {
                    "logradouroSacadorAvalista": _texto(
                        f"{end.logradouro} {end.numero}", 40
                    ),
                    "cepSacadorAvalista": end.cep[:5],
                    "complementoCepSacadorAvalista": end.cep[5:],
                    "bairroSacadorAvalista": _texto(end.bairro, 40),
                    "municipioSacadorAvalista": _texto(end.cidade, 40),
                    "ufSacadorAvalista": end.uf,
                }
            )
        return dados

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        enc = titulo.encargos
        if titulo.dias_baixa_automatica is not None:
            raise Validacao("bradesco: baixa automatica por dias sem campo conhecido")
        if enc.desconto_percentual is not None or enc.abatimento is not None:
            raise Validacao(
                "bradesco: desconto e abatimento no registro sem campo confirmado "
                "(docs/bradesco-duvidas.md)"
            )
        cfg = self._config
        raiz, filial, controle = _decompor(cfg.documento_beneficiario)
        payload: dict[str, Any] = {
            "nuCPFCNPJ": raiz,
            "filialCPFCNPJ": filial,
            "ctrlCPFCNPJ": controle,
            "idProduto": cfg.carteira,
            "nuNegociacao": self._nu_negociacao(),
            "nuCliente": _texto(titulo.seu_numero, 25),
            "dtEmissaoTitulo": _fmt(self._hoje()),
            "dtVencimentoTitulo": _fmt(titulo.vencimento),
            "tpVencimento": 0,
            "vlNominalTitulo": self._valor(titulo.valor),
            "cdEspecieTitulo": ESPECIES[cfg.especie],
            **self._pagador(titulo.pagador),
        }
        # Encargo so vai quando existe; percentual exige os dias apos o vencimento
        # (CBTT0505 "INFORME TODOS OS CAMPOS PARA MULTA"). Inicio = 1 dia depois.
        if enc.multa_percentual is not None:
            payload["percentualMulta"] = _numero(enc.multa_percentual)
            payload["qtdeDiasMulta"] = 1
        if enc.juros_mensal_percentual is not None:
            payload["percentualJuros"] = _numero(enc.juros_mensal_percentual)
            payload["qtdeDiasJuros"] = 1
        if titulo.beneficiario:
            payload.update(self._sacador_avalista(titulo.beneficiario))
        if titulo.mensagens:
            payload["listaMsgs"] = [
                {"mensagem": _texto(m, _TAMANHO_MENSAGEM)}
                for m in titulo.mensagens[:_MAX_MENSAGENS]
            ]
        return payload

    def _identificacao(self) -> dict[str, Any]:
        """Corpo comum de consulta: CPF/CNPJ decomposto, produto e negociacao."""
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        return {
            "cpfCnpj": {
                "cpfCnpj": int(raiz),
                "filial": int(filial),
                "controle": int(controle),
            },
            "produto": self._config.carteira,
            "negociacao": self._negociacao(),
        }

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Bradesco nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Bradesco fora do formato", status_http=resp.status_code
            )
        # Algumas descricoes da spec embrulham o corpo em `data`.
        interno = dados.get("data")
        return interno if isinstance(interno, dict) else dados

    @staticmethod
    def _recusa(dados: dict[str, Any], resp: httpx.Response, motivo: str) -> Validacao:
        """Erro de negocio em HTTP 2xx: o envelope traz `status` e `causa`."""
        status = dados.get("status")
        status_http = status if isinstance(status, int) else resp.status_code
        return Validacao(
            f"bradesco: {motivo}",
            status_http=status_http,
            resposta_bruta=_sem_pii(dados),
        )

    # -- operacoes ---------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            CAMINHO_REGISTRO,
            operacao="registrar",
            json=self._montar_payload(titulo),
        )
        dados = self._json(resp)
        gerado = dados.get("nuTituloGerado")
        if not gerado:
            raise self._recusa(dados, resp, "registro sem nuTituloGerado")
        nosso_numero = str(gerado)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=nosso_numero,
            status=StatusCanonico.REGISTRADO,
            seu_numero=titulo.seu_numero,
            nosso_numero=nosso_numero,
            linha_digitavel=dados.get("linhaDigitavel"),
            codigo_barras=dados.get("cdBarras"),
            resposta_bruta=_sem_pii(dados),
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        corpo = {
            **self._identificacao(),
            "nossoNumero": self._nosso_numero(id_banco),
            "sequencia": 0,
            "status": 0,
        }
        resp = await self._transporte.requisitar(
            "POST", CAMINHO_CONSULTA, operacao="consultar", json=corpo
        )
        dados = self._json(resp)
        titulo = dados.get("titulo")
        if not isinstance(titulo, dict):
            raise self._recusa(dados, resp, "consulta sem bloco titulo")
        status = self.tabela_status.traduzir(_codigo(titulo.get("codStatus")))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            # 13 "PAGO NO DIA" pode vir sem valor e sem data: ficam None.
            valor_pago = _dinheiro(titulo.get("vlrPagto"))
            data_pagamento = _data_ddmmaaaa(titulo.get("dtPagto"))
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=titulo.get("snumero"),
            nosso_numero=id_banco,
            linha_digitavel=titulo.get("linhaDig"),
            codigo_barras=titulo.get("codBarras"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pii(dados),
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        if instrucao.tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        raise NaoSuportado(
            f"bradesco: instrucao {instrucao.tipo.value} sem contrato confirmado "
            "(docs/bradesco-duvidas.md)"
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        # Tipos como na collection Postman do Bradesco (cpfCnpj em texto).
        corpo = {
            "cpfCnpj": {"cpfCnpj": raiz, "filial": filial, "controle": controle},
            "produto": self._config.carteira,
            "negociacao": self._negociacao(),
            "nossoNumero": id_banco,
            "sequencia": 0,
            "codigoBaixa": CODIGO_BAIXA_PEDIDO,
        }
        resp = await self._transporte.requisitar(
            "POST", CAMINHO_BAIXA, operacao="baixar", json=corpo
        )
        dados = self._json(resp)
        status = dados.get("status")
        if isinstance(status, int) and status >= 400:
            raise self._recusa(dados, resp, "baixa recusada")
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            nosso_numero=id_banco,
            resposta_bruta=_sem_pii(dados),
        )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado(
            "bradesco: formato do aviso de webhook desconhecido "
            "(docs/bradesco-duvidas.md); use a consulta"
        )
