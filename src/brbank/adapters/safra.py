"""Safra (422), API de Cobranca do Safra Negocios (OAuth2 password, sem mTLS).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Safra. Sem modulo
Odoo de origem: o contrato foi lido, como documentacao, do ACBr
(`ACBrBoletoW_Safra`, `ACBrBoletoRet_Safra`, LGPL, nenhum codigo portado), de
relatos do forum do ACBr que citam o swagger oficial e de integracoes de
terceiros. O swagger oficial nao e publico. Fontes e duvidas em
`docs/safra-duvidas.md`.

Escopo deliberadamente curto:

- Registra e consulta. Nao ha baixa, alteracao, PDF nem webhook com contrato
  publico (o ACBr so implementa inclusao e consulta, e integradores dizem que
  alteracao e cancelamento nao sao suportados pela API): as capacidades nao sao
  declaradas e as chamadas levantam `NaoSuportado`.
- O nosso numero e do cliente (9 digitos, `documento.numero`) e vira o
  `id_banco`, usado na consulta junto com agencia e conta.
- O que tem contrato divergente entre as fontes (multa, juros, desconto) e
  recusado com `Validacao` em vez de enviado por palpite.
- `Safra-Correlation-ID` novo (UUID) a cada tentativa: o forum do ACBr relata
  401/503 quando o mesmo id se repete.
- A linha digitavel nao vem na resposta do registro (so o codigo de barras): e
  derivada do codigo de barras pela regra FEBRABAN (modulo 10 por campo).
"""

import asyncio
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2Password
from brbank.capacidades import Capacidade
from brbank.erros import Validacao
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte

URL_PRODUCAO = "https://api.safranegocios.com.br/gateway/cobrancas/v1"
URL_SANDBOX = "https://api-hml.safranegocios.com.br/gateway/cobrancas/v1"
TOKEN_PRODUCAO = "https://api.safranegocios.com.br/gateway/v1/oauth2/token"
TOKEN_SANDBOX = "https://api-hml.safranegocios.com.br/gateway/v1/oauth2/token"
CABECALHO_CORRELACAO = "Safra-Correlation-ID"

# Limites que o ACBr (e uma integracao de terceiros) aplicam truncando.
_MAX_NOSSO_NUMERO = 9
_MAX_NUMERO_CLIENTE = 10
_MAX_NOME = 40
_MAX_EMAIL = 50
_MAX_LOGRADOURO = 40
_MAX_BAIRRO = 10
_MAX_CIDADE = 15
_MAX_MENSAGEM = 72
_CODIGO_MOEDA_REAL = 9

# Campo `situacao` da consulta, sem acento, sem espaco e em maiuscula. Valores
# lidos no parser de consulta do ACBr (constantes CANCELADO, EXPIRADO, VENCIDO,
# EMABERTO, PAGO), sem confirmacao no swagger: ver docs/safra-duvidas.md.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EMABERTO": StatusCanonico.REGISTRADO,
        "VENCIDO": StatusCanonico.REGISTRADO,
        "PAGO": StatusCanonico.LIQUIDADO,
        "CANCELADO": StatusCanonico.BAIXADO,
        "EXPIRADO": StatusCanonico.BAIXADO,
    },
)

_CENTAVO = Decimal("0.01")


@dataclass
class SafraConfig:
    conta: str
    # Credenciais de boleto: Client ID, usuario e senha (sem client_secret nem
    # certificado, segundo o ACBr e a Kobana).
    client_id: str
    usuario: str
    senha: str = field(repr=False)
    # Agencia e conta corrente do beneficiario, so digitos; a conta COM o digito.
    agencia: str
    conta_corrente: str
    # Codigo da especie do titulo (ex.: "01" duplicata mercantil, "02" nota
    # promissoria, "09" duplicata de servico, segundo o ACBr).
    especie: str
    ambiente: str = "producao"
    # Gera o nosso numero (ate 9 digitos). Padrao: seu_numero numerico.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    # Posicao das mensagens: "1" recibo do pagador, "2" ficha de compensacao.
    posicao_mensagem: str = "1"
    # Dias para devolucao (baixa por decurso) quando o titulo nao trouxer
    # `dias_baixa_automatica`. None: o campo nao e enviado.
    dias_devolucao: int | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Safra invalido: {self.ambiente!r}")
        for nome in ("agencia", "conta_corrente"):
            if not getattr(self, nome).isdigit():
                raise Validacao(f"Safra: {nome} deve ter so digitos")
        if not (self.especie.isdigit() and len(self.especie) == 2):
            raise Validacao("Safra: especie deve ter 2 digitos (ex.: '01')")
        if self.posicao_mensagem not in ("1", "2"):
            raise Validacao("Safra: posicao_mensagem deve ser '1' ou '2'")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return TOKEN_SANDBOX if self.ambiente == "sandbox" else TOKEN_PRODUCAO


class _AuthSafra(OAuth2Password):
    """OAuth2 password com `client_id` no formulario.

    Tambem poe um `Safra-Correlation-ID` novo em cada chamada de negocio: os
    cabecalhos do autenticador sao montados a cada tentativa do transporte, e
    o Safra pede um id unico por requisicao.
    """

    def __init__(self, *, client_id: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._client_id = client_id

    def _formulario(self) -> dict[str, str]:
        return {"client_id": self._client_id, **super()._formulario()}

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        base = await super().cabecalhos(escopo, client)
        return {**base, CABECALHO_CORRELACAO: str(uuid.uuid4())}


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Safra") from exc


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Safra") from exc


def _situacao(texto: Any) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in sem_acento if c.isalnum()).upper()


def _modulo10(numero: str) -> int:
    soma = 0
    for posicao, digito in enumerate(reversed(numero)):
        produto = int(digito) * (2 if posicao % 2 == 0 else 1)
        soma += produto // 10 + produto % 10
    return (10 - soma % 10) % 10


def linha_digitavel(codigo_barras: str | None) -> str | None:
    """Linha digitavel (47 digitos) a partir do codigo de barras FEBRABAN (44).

    Devolve None se o codigo nao tiver 44 digitos: melhor sem linha que errada.
    """
    if not codigo_barras or len(codigo_barras) != 44 or not codigo_barras.isdigit():
        return None
    livre = codigo_barras[19:]
    campo1 = codigo_barras[:4] + livre[:5]
    campo2 = livre[5:15]
    campo3 = livre[15:25]
    return (
        f"{campo1}{_modulo10(campo1)}"
        f"{campo2}{_modulo10(campo2)}"
        f"{campo3}{_modulo10(campo3)}"
        f"{codigo_barras[4]}{codigo_barras[5:19]}"
    )


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao("pagador sem endereco: o Safra exige o endereco")
    partes = (endereco.logradouro, endereco.numero, endereco.complemento)
    logradouro = ", ".join(p.strip() for p in partes if p and p.strip())
    dados: dict[str, Any] = {
        "nome": pessoa.nome[:_MAX_NOME].strip(),
        "tipoPessoa": "F" if pessoa.tipo == "FISICA" else "J",
        "numeroDocumento": pessoa.documento,
        "endereco": {
            "logradouro": logradouro[:_MAX_LOGRADOURO].strip(),
            "bairro": endereco.bairro[:_MAX_BAIRRO].strip(),
            "cidade": endereco.cidade[:_MAX_CIDADE].strip(),
            "uf": endereco.uf,
            "cep": endereco.cep,
        },
    }
    if pessoa.email:
        dados["email"] = pessoa.email[:_MAX_EMAIL]
    return dados


class SafraAdapter(BankAdapter):
    banco: ClassVar[str] = "safra"
    # So registro e consulta: baixa, alteracao, PDF e webhook sem contrato
    # publico (ver docs/safra-duvidas.md).
    capacidades: ClassVar[Capacidade] = Capacidade.NENHUMA
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS
    # So vira True depois de rodar contra a homologacao real do Safra.
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: SafraConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        auth = _AuthSafra(
            conta=config.conta,
            url_token=config.url_token,
            client_id=config.client_id,
            usuario=config.usuario,
            senha=config.senha,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            autenticador=auth,
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )
        self._registrados: dict[str, TituloRegistrado] = {}
        self._travas: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # -- montagem ------------------------------------------------------------

    def _nosso_numero(self, titulo: Titulo) -> str:
        gerador = self._config.gerar_nosso_numero
        bruto = str(gerador(titulo)) if gerador else titulo.seu_numero
        if not bruto.isdigit() or len(bruto) > _MAX_NOSSO_NUMERO:
            raise Validacao(
                "Safra exige nosso numero numerico de ate 9 digitos: informe "
                "gerar_nosso_numero ou use seu_numero numerico"
            )
        return bruto.zfill(_MAX_NOSSO_NUMERO)

    def _montar_payload(self, titulo: Titulo, nosso_numero: str) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        # Multa e juros tem dois formatos incompativeis nas fontes e o desconto
        # nao tem a semantica do percentual confirmada: recusar e mais seguro.
        for nome, valor in (
            ("multa", enc.multa_percentual),
            ("juros", enc.juros_mensal_percentual),
            ("desconto", enc.desconto_percentual),
        ):
            if valor is not None:
                raise Validacao(
                    f"Safra: {nome} ainda nao suportado pelo adapter "
                    "(ver docs/safra-duvidas.md)"
                )
        documento: dict[str, Any] = {
            "numero": int(nosso_numero),
            "numeroCliente": titulo.seu_numero[:_MAX_NUMERO_CLIENTE],
            "especie": cfg.especie,
            "dataVencimento": titulo.vencimento.isoformat(),
            "valor": _numero(titulo.valor),
            "codigoMoeda": _CODIGO_MOEDA_REAL,
            "pagador": _pagador(titulo.pagador),
        }
        dias = titulo.dias_baixa_automatica
        if dias is None:
            dias = cfg.dias_devolucao
        if dias is not None:
            documento["diasDevolucao"] = dias
        if enc.abatimento is not None:
            documento["valorAbatimento"] = _numero(enc.abatimento)
        if titulo.mensagens:
            documento["mensagens"] = [
                {
                    "posicao": cfg.posicao_mensagem,
                    "descricao": m.strip()[:_MAX_MENSAGEM],
                }
                for m in titulo.mensagens
            ]
        return {
            "agencia": int(cfg.agencia),
            "conta": int(cfg.conta_corrente),
            "documento": documento,
        }

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Safra nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Safra fora do formato", status_http=resp.status_code
            )
        return dados

    # -- operacoes -------------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        chave = titulo.chave_idempotencia
        trava = self._travas.setdefault(chave, asyncio.Lock())
        async with trava:
            if chave in self._registrados:
                return self._registrados[chave]
            nosso_numero = self._nosso_numero(titulo)
            resp = await self._transporte.requisitar(
                "POST",
                "boletos",
                operacao="registrar",
                json=self._montar_payload(titulo, nosso_numero),
            )
            bruto = self._json(resp)
            dados = bruto.get("data")
            if not isinstance(dados, dict) or not dados.get("codigoBarras"):
                raise Validacao(
                    "resposta do Safra sem data.codigoBarras",
                    status_http=resp.status_code,
                    resposta_bruta=bruto,
                )
            devolvido = str(dados.get("numero") or nosso_numero)
            if devolvido.isdigit() and int(devolvido) != int(nosso_numero):
                raise Validacao(
                    "Safra devolveu nosso numero diferente do enviado",
                    status_http=resp.status_code,
                    resposta_bruta=bruto,
                )
            codigo_barras = str(dados["codigoBarras"])
            registrado = TituloRegistrado(
                banco=self.banco,
                id_banco=nosso_numero,
                status=StatusCanonico.REGISTRADO,
                seu_numero=titulo.seu_numero,
                nosso_numero=nosso_numero,
                codigo_barras=codigo_barras,
                linha_digitavel=linha_digitavel(codigo_barras),
                resposta_bruta=bruto,
            )
            self._registrados[chave] = registrado
            return registrado

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        consulta = urlencode(
            {
                "agencia": self._config.agencia,
                "conta": str(int(self._config.conta_corrente)),
                "numero": id_banco,
            }
        )
        resp = await self._transporte.requisitar(
            "GET", f"boletos?{consulta}", operacao="consultar"
        )
        bruto = self._json(resp)
        item = self._escolher(bruto, id_banco, resp.status_code)
        status = self.tabela_status.traduzir(_situacao(item.get("situacao")))
        liquidado = status is StatusCanonico.LIQUIDADO
        codigo_barras = item.get("codigoBarras")
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=item.get("seuNumero"),
            nosso_numero=id_banco,
            codigo_barras=codigo_barras,
            linha_digitavel=item.get("linhaDigitavel")
            or linha_digitavel(codigo_barras),
            valor_pago=(
                _dinheiro(item.get("valorTotalRecebimento")) if liquidado else None
            ),
            data_pagamento=(_data(item.get("dataHoraSituacao")) if liquidado else None),
            resposta_bruta=item,
        )

    @staticmethod
    def _escolher(
        bruto: dict[str, Any], id_banco: str, status_http: int
    ) -> dict[str, Any]:
        """O titulo pedido dentro de `content` (lista, como le o ACBr)."""
        itens = bruto.get("content")
        if not isinstance(itens, list):
            raise Validacao(
                "consulta do Safra sem lista content",
                status_http=status_http,
                resposta_bruta=bruto,
            )
        for item in itens:
            numero = (
                str(item.get("nossoNumero") or "") if isinstance(item, dict) else ""
            )
            if numero.isdigit() and id_banco.isdigit() and int(numero) == int(id_banco):
                return item
        raise Validacao(
            f"titulo {id_banco} nao encontrado na consulta do Safra",
            status_http=status_http,
            resposta_bruta=bruto,
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        self.exigir(Capacidade.BAIXA)
        raise AssertionError("inalcancavel: BAIXA nao declarada")  # pragma: no cover

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        raise AssertionError(
            "inalcancavel: ALTERACAO nao declarada"
        )  # pragma: no cover

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        self.exigir(Capacidade.WEBHOOK)
        raise AssertionError("inalcancavel: WEBHOOK nao declarada")  # pragma: no cover
