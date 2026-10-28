"""Banco do Brasil, API de Cobranca v2 (OAuth2 client_credentials, sem mTLS).

Sem modulo Odoo de origem. O comportamento foi levantado por leitura, como
documentacao, do ACBrBoleto (LGPL, nenhum codigo portado) e confrontado com a
doc publica quando acessivel. Validado na homologacao do BB em 28/10/2026:
registro, consulta, prorrogacao, abatimento e baixa (docs/bb-duvidas.md).

Pontos do contrato:

- A chave de desenvolvedor `gw-dev-app-key` vai como query param em TODA
  chamada de negocio (nao e header). Ela fica fora do repr e nunca e logada:
  o transporte so loga operacao, status e latencia.
- Datas no formato `DD.MM.YYYY`; valores como numero JSON (conversao so na
  fronteira, o resto e Decimal).
- A referencia do titulo (`id_banco`) e o `numeroTituloCliente` de 20 digitos:
  "000" + convenio (7, com zeros a esquerda) + nosso numero (10).
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import Indisponivel, NaoSuportado, Validacao
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte

URL_PRODUCAO = "https://api.bb.com.br/cobrancas/v2"
# Aplicacao "Em teste" do portal usa a homologacao (hm). O host api.sandbox
# serve certificado de outro dominio desde 07/2026 (docs/bb-duvidas.md).
URL_SANDBOX = "https://api.hm.bb.com.br/cobrancas/v2"
TOKEN_PRODUCAO = "https://oauth.bb.com.br/oauth/token"
TOKEN_SANDBOX = "https://oauth.hm.bb.com.br/oauth/token"

# Recusa de instrucao com o boleto registrado ha menos de 30 minutos: repetir
# depois funciona, entao vira Indisponivel e nao Validacao.
CODIGO_EM_PROCESSAMENTO = "4923876"

# Confirmados no OAuth de homologacao em 28/10/2026.
ESCOPOS = {
    "escrita": "cobrancas.boletos-requisicao",
    "leitura": "cobrancas.boletos-info",
}

# Codigos do ACBr para a especie do titulo (codigoTipoTitulo).
TIPOS_TITULO = {
    "CH": 1, "DM": 2, "DMI": 3, "DS": 4, "DSI": 5, "DR": 6, "LC": 7, "NCC": 8,
    "NCE": 9, "NCI": 10, "NCR": 11, "NP": 12, "NPR": 13, "TM": 14, "TS": 15,
    "NS": 16, "RC": 17, "FAT": 18, "ND": 19, "AP": 20, "ME": 21, "PC": 22,
}  # fmt: skip

_REGISTRADO = StatusCanonico.REGISTRADO
# codigoEstadoTituloCobranca. Fora da tabela de proposito: 10, 11, 12, 14 a 19
# e 21 (pago em cartorio, em liquidacao, agendado, creditado, pago parcial):
# sem confirmacao oficial do que cada um significa, liquidar ou baixar por
# palpite e pior que recusar (ver docs/bb-duvidas.md).
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "1": _REGISTRADO,
        "2": _REGISTRADO,
        "3": _REGISTRADO,
        "4": _REGISTRADO,
        "5": _REGISTRADO,
        "6": StatusCanonico.LIQUIDADO,
        "7": StatusCanonico.BAIXADO,
        "8": _REGISTRADO,
        "9": _REGISTRADO,
        "13": _REGISTRADO,
        "80": StatusCanonico.PENDENTE_REGISTRO,
    },
)

_CENTAVO = Decimal("0.01")


@dataclass
class BBConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Chave de desenvolvedor do app no portal BB (query param gw-dev-app-key).
    chave_desenvolvedor: str = field(repr=False)
    convenio: str
    carteira: int
    variacao_carteira: int
    ambiente: str = "producao"
    # CNPJ da software house parceira (header x-bb-portal-devx-cnpj-parceiro).
    cnpj_parceiro: str | None = None
    indicador_pix: bool = False
    especie: str = "DM"
    # Gera o nosso numero (ate 10 digitos) do titulo. Padrao: seu_numero numerico.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    # None: sandbox manda as credenciais no corpo e producao usa Basic (ACBr).
    credenciais_no_corpo: bool | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente BB invalido: {self.ambiente!r}")
        if not self.convenio.isdigit() or len(self.convenio) not in (6, 7):
            raise Validacao("convenio BB deve ter 6 ou 7 digitos numericos")
        if self.especie not in TIPOS_TITULO:
            raise Validacao(f"especie de titulo BB desconhecida: {self.especie!r}")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return TOKEN_SANDBOX if self.ambiente == "sandbox" else TOKEN_PRODUCAO

    @property
    def _credenciais_no_corpo(self) -> bool:
        if self.credenciais_no_corpo is not None:
            return self.credenciais_no_corpo
        return self.ambiente == "sandbox"


class _AuthBB(OAuth2ClientCredentials):
    """client_credentials do BB: Basic no header ou credenciais no corpo."""

    def __init__(self, *, no_corpo: bool, **kw: Any) -> None:
        super().__init__(**kw)
        self._no_corpo = no_corpo

    def _formulario(self) -> dict[str, str]:
        dados = super()._formulario()
        if self._no_corpo:
            return dados
        return {"grant_type": dados["grant_type"]}

    def _cabecalhos_pedido(self) -> dict[str, str]:
        cabecalhos = super()._cabecalhos_pedido()
        if self._no_corpo:
            return cabecalhos
        bruto = f"{self._client_id}:{self._client_secret}".encode()
        basic = "Basic " + base64.b64encode(bruto).decode()
        return {**cabecalhos, "Authorization": basic}


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do BB") from exc


def _em_processamento(bruto: Any) -> bool:
    if isinstance(bruto, str):
        return CODIGO_EM_PROCESSAMENTO in bruto
    erros = bruto.get("errors") if isinstance(bruto, dict) else None
    return any(
        str(e.get("code", "")).startswith(CODIGO_EM_PROCESSAMENTO)
        for e in erros or ()
        if isinstance(e, dict)
    )


def _fmt(dia: date) -> str:
    return dia.strftime("%d.%m.%Y")


def _data(texto: Any) -> date | None:
    if not texto or str(texto).strip() in ("", "00.00.0000"):
        return None
    try:
        return datetime.strptime(str(texto), "%d.%m.%Y").date()
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do BB") from exc


def _inscricao(pessoa: Pessoa) -> dict[str, Any]:
    if not pessoa.documento.isdigit():
        raise Validacao("BB exige CPF/CNPJ numerico (CNPJ alfanumerico nao cabe)")
    return {
        "tipoInscricao": 1 if pessoa.tipo == "FISICA" else 2,
        "numeroInscricao": int(pessoa.documento),
        "nome": pessoa.nome,
    }


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    end = pessoa.endereco
    if end is None:
        raise Validacao("pagador sem endereco: o BB exige o endereco")
    return {
        **_inscricao(pessoa),
        "endereco": f"{end.logradouro} {end.numero}".strip(),
        "cep": int(end.cep),
        "cidade": end.cidade,
        "bairro": end.bairro,
        "uf": end.uf,
    }


class BancoDoBrasilAdapter(BankAdapter):
    banco: ClassVar[str] = "bb"
    capacidades: ClassVar[Capacidade] = Capacidade.ALTERACAO | Capacidade.BAIXA
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS
    validado_sandbox: ClassVar[bool] = True

    def __init__(
        self,
        config: BBConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        auth = _AuthBB(
            no_corpo=config._credenciais_no_corpo,
            conta=config.conta,
            url_token=config.url_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
            escopos=ESCOPOS,
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

    # -- montagem de requisicao ------------------------------------------

    def _caminho(self, recurso: str, **extras: str) -> str:
        consulta = {"gw-dev-app-key": self._config.chave_desenvolvedor, **extras}
        return f"{recurso}?{urlencode(consulta)}"

    def _cabecalhos(self) -> dict[str, str]:
        if self._config.cnpj_parceiro:
            return {"x-bb-portal-devx-cnpj-parceiro": self._config.cnpj_parceiro}
        return {}

    def _convenio_int(self) -> int:
        return int(self._config.convenio)

    async def _chamar(
        self,
        metodo: str,
        caminho: str,
        operacao: str,
        escopo: str,
        json: Any = None,
    ) -> httpx.Response:
        try:
            return await self._transporte.requisitar(
                metodo,
                caminho,
                operacao=operacao,
                escopo=escopo,
                json=json,
                cabecalhos=self._cabecalhos(),
            )
        except Validacao as exc:
            if _em_processamento(exc.resposta_bruta):
                raise Indisponivel(
                    "bb: boleto registrado ha menos de 30 minutos, repetir depois",
                    status_http=exc.status_http,
                    resposta_bruta=exc.resposta_bruta,
                ) from exc
            raise

    def _nosso_numero(self, titulo: Titulo) -> str:
        gerador = self._config.gerar_nosso_numero
        bruto = str(gerador(titulo)) if gerador else titulo.seu_numero
        if not bruto.isdigit() or len(bruto) > 10:
            raise Validacao(
                "BB exige nosso numero numerico de ate 10 digitos: informe "
                "gerar_nosso_numero ou use seu_numero numerico"
            )
        return "000" + self._config.convenio.zfill(7) + bruto.zfill(10)

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        if titulo.dias_baixa_automatica is not None:
            raise Validacao(
                "BB: baixa automatica ainda nao suportada pelo adapter "
                "(equivalencia com numeroDiasLimiteRecebimento em aberto)"
            )
        mensagem = " ".join(titulo.mensagens).upper()
        if len(mensagem) > 30:
            raise Validacao("BB aceita no maximo 30 caracteres de mensagem")
        payload: dict[str, Any] = {
            "numeroConvenio": self._convenio_int(),
            "numeroCarteira": cfg.carteira,
            "numeroVariacaoCarteira": cfg.variacao_carteira,
            "codigoModalidade": 1,
            "dataEmissao": _fmt(self._hoje()),
            "dataVencimento": _fmt(titulo.vencimento),
            "valorOriginal": _numero(titulo.valor),
            "valorAbatimento": _numero(enc.abatimento or Decimal(0)),
            "quantidadeDiasProtesto": 0,
            "codigoAceite": "N",
            "codigoTipoTitulo": TIPOS_TITULO[cfg.especie],
            "descricaoTipoTitulo": cfg.especie,
            "numeroTituloBeneficiario": titulo.seu_numero.upper()[:15],
            "numeroTituloCliente": self._nosso_numero(titulo),
            "mensagemBloquetoOcorrencia": mensagem,
            "pagador": _pagador(titulo.pagador),
            "indicadorPix": "S" if cfg.indicador_pix else "N",
        }
        if enc.desconto_percentual is not None:
            assert enc.desconto_dias_antes is not None  # garantido pelo modelo
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            payload["desconto"] = {
                "tipo": 2,
                "dataExpiracao": _fmt(limite),
                "porcentagem": _numero(enc.desconto_percentual),
            }
        if enc.juros_mensal_percentual is not None:
            payload["jurosMora"] = {
                "tipo": 2,
                "porcentagem": _numero(enc.juros_mensal_percentual),
            }
        if enc.multa_percentual is not None:
            payload["multa"] = {
                "tipo": 2,
                "data": _fmt(titulo.vencimento + timedelta(days=1)),
                "porcentagem": _numero(enc.multa_percentual),
            }
        if titulo.beneficiario:
            payload["beneficiarioFinal"] = _inscricao(titulo.beneficiario)
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do BB nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do BB fora do formato", status_http=resp.status_code
            )
        return dados

    # -- operacoes ---------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        chave = titulo.chave_idempotencia
        trava = self._travas.setdefault(chave, asyncio.Lock())
        async with trava:
            if chave in self._registrados:
                return self._registrados[chave]
            resp = await self._chamar(
                "POST",
                self._caminho("boletos"),
                "registrar",
                "escrita",
                self._montar_payload(titulo),
            )
            dados = self._json(resp)
            numero = dados.get("numero")
            if not numero:
                raise Validacao(
                    "resposta do BB sem numero do boleto",
                    status_http=resp.status_code,
                    resposta_bruta=dados,
                )
            qr = dados.get("qrCode") or {}
            # O BB devolve linha digitavel e codigo de barras na propria
            # resposta: o titulo ja nasce registrado.
            registrado = TituloRegistrado(
                banco=self.banco,
                id_banco=str(numero),
                status=StatusCanonico.REGISTRADO,
                seu_numero=titulo.seu_numero,
                nosso_numero=str(numero),
                linha_digitavel=dados.get("linhaDigitavel"),
                codigo_barras=dados.get("codigoBarraNumerico"),
                pix_copia_cola=qr.get("emv"),
                txid=qr.get("txId"),
                resposta_bruta=dados,
            )
            self._registrados[chave] = registrado
            return registrado

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._chamar(
            "GET",
            self._caminho(f"boletos/{id_banco}", numeroConvenio=self._config.convenio),
            "consultar",
            "leitura",
        )
        dados = self._json(resp)
        status = self.tabela_status.traduzir(
            str(dados.get("codigoEstadoTituloCobranca"))
        )
        liquidado = status is StatusCanonico.LIQUIDADO
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=dados.get("numeroTituloCedenteCobranca"),
            nosso_numero=dados.get("id") or id_banco,
            linha_digitavel=dados.get("codigoLinhaDigitavel"),
            codigo_barras=dados.get("textoCodigoBarrasTituloCobranca"),
            valor_pago=_dinheiro(dados.get("valorPagoSacado")) if liquidado else None,
            data_pagamento=(
                _data(dados.get("dataRecebimentoTitulo")) if liquidado else None
            ),
            resposta_bruta=dados,
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        corpo: dict[str, Any] = {"numeroConvenio": self._convenio_int()}
        if instrucao.tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if instrucao.tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
            corpo["indicadorNovaDataVencimento"] = "S"
            corpo["alteracaoData"] = {
                "novaDataVencimento": _fmt(instrucao.nova_data_vencimento)
            }
        elif instrucao.tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            atual = await self._consultar_bruto(id_banco)
            ja_tem = (_dinheiro(atual.get("valorAbatimentoTituloCobranca")) or 0) > 0
            valor = _numero(instrucao.valor)
            if ja_tem:
                corpo["indicadorAlterarAbatimento"] = "S"
                corpo["alteracaoAbatimento"] = {"novoValorAbatimento": valor}
            else:
                corpo["indicadorIncluirAbatimento"] = "S"
                corpo["abatimento"] = {"valorAbatimento": valor}
        else:
            raise NaoSuportado(f"bb: instrucao {instrucao.tipo.value} nao implementada")
        await self._chamar(
            "PATCH",
            self._caminho(f"boletos/{id_banco}"),
            "instrucao",
            "escrita",
            corpo,
        )
        # A consulta e a fonte da verdade do estado apos a alteracao.
        return await self.consultar_titulo(id_banco)

    async def _consultar_bruto(self, id_banco: str) -> dict[str, Any]:
        return (await self.consultar_titulo(id_banco)).resposta_bruta

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._chamar(
            "POST",
            self._caminho(f"boletos/{id_banco}/baixar"),
            "baixar",
            "escrita",
            {"numeroConvenio": self._convenio_int()},
        )
        dados = self._json(resp) if resp.content else {}
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            resposta_bruta=dados,
        )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado("bb: sem webhook no contrato conhecido, use polling")
