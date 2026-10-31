"""Banco do Brasil, API Extratos v2 (OAuth2 client_credentials, mTLS obrigatorio).

Sem modulo Odoo de origem. Contrato lido no OpenAPI oficial 2.0.1 e na doc do
portal (codigo 80). NAO exercitado: os hosts da v2 exigem certificado de cliente
ate na homologacao ("No client certificate provided"), e a aplicacao de teste da
KMEE ainda nao tem certificado cadastrado. Por isso `validado_sandbox = False`
(docs/bb-duvidas.md, secao Extratos).

Pontos do contrato:

- A v1 sai do ar em 20/11/2026 (CNPJ alfanumerico); so a v2 e suportada.
- Na homologacao o header `x-br-com-bb-ipa-mciteste` e obrigatorio e casa com a
  agencia e a conta da massa de teste; em producao ele nao vai.
- Datas `DDMMAAAA` sem zero a esquerda no dia; periodo de no maximo 31 dias.
- Paginacao por numero de pagina (`numeroPaginaProximo` 0 e a ultima), com 50 a
  120 linhas por pagina; o adapter pede 120.
- Linhas de saldo vem como lancamento: historico 0 e saldo anterior (ou parcial)
  e 999 e saldo final.
- 404 e "conta errada ou sem lancamentos no periodo": vira pagina vazia, porque um
  dia sem movimento nao pode quebrar a importacao.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.auth import OAuth2ClientCredentials, OAuth2ClientCredentialsBasic
from brbank.dda import hash_corpo
from brbank.erros import Validacao
from brbank.extrato import (
    AdapterExtrato,
    CapacidadeExtrato,
    LancamentoExtrato,
    PaginaExtrato,
    SaldoExtrato,
    Sinal,
    TipoSaldo,
)
from brbank.pagamento import ContaBancaria
from brbank.tempo import hoje_brasil
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

URL_SANDBOX = "https://extratos.mtls.api.hm.bb.com.br/v2"
URL_PRODUCAO = "https://extratos.mtls.api.bb.com.br/v2"
TOKEN_SANDBOX = "https://oauth.hm.bb.com.br/oauth/token"
TOKEN_PRODUCAO = "https://oauth.bb.com.br/oauth/token"
ESCOPO = "extrato"
LINHAS_POR_PAGINA = 120
PERIODO_MAXIMO_DIAS = 31

HISTORICO_SALDO = {"0": TipoSaldo.ANTERIOR, "999": TipoSaldo.FINAL}
# Campos que o BB nao muda depois de lancar: base do id do lancamento.
_CAMPOS_ESTAVEIS = (
    "dataLancamento",
    "dataMovimento",
    "codigoAgenciaOrigem",
    "numeroLote",
    "numeroDocumento",
    "codigoHistorico",
    "codigoSubHistorico",
    "valorLancamento",
    "indicadorSinalLancamento",
)


@dataclass
class BBExtratoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    chave_desenvolvedor: str = field(repr=False)
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    ambiente: str = "producao"
    # So na homologacao: identificador da massa de teste (par com agencia e conta).
    mci_teste: str | None = None
    cnpj_parceiro: str | None = None
    credenciais_no_corpo: bool | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente BB invalido: {self.ambiente!r}")
        if self.mci_teste and self.ambiente == "producao":
            raise Validacao("mci_teste so existe na homologacao do BB")


def _data_bb(dia: date) -> str:
    return f"{dia.day}{dia.month:02d}{dia.year}"


def _data_de_bb(valor: Any) -> date | None:
    if not valor:
        return None
    texto = str(valor).zfill(8)
    try:
        return date(int(texto[4:]), int(texto[2:4]), int(texto[:2]))
    except ValueError as exc:
        raise Validacao("data ilegivel no extrato do BB") from exc


def _decimal(valor: Any) -> Decimal:
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise Validacao("valor ilegivel no extrato do BB") from exc


def _texto(valor: Any) -> str | None:
    if valor in (None, "", 0, "0"):
        return None
    return str(valor).strip() or None


class BancoDoBrasilExtratoAdapter(AdapterExtrato):
    banco: ClassVar[str] = "bb"
    capacidades_extrato: ClassVar[CapacidadeExtrato] = (
        CapacidadeExtrato.LANCAMENTOS | CapacidadeExtrato.SALDO
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: BBExtratoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = hoje_brasil,
    ) -> None:
        self._config = config
        self._hoje = hoje
        producao = config.ambiente == "producao"
        ssl_context = None
        if client is None:
            if not config.certificado_pem:
                raise Validacao(
                    "BB extratos exige certificado mTLS (tambem na homologacao)"
                )
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        no_corpo = (
            config.credenciais_no_corpo
            if config.credenciais_no_corpo is not None
            else not producao
        )
        classe = OAuth2ClientCredentials if no_corpo else OAuth2ClientCredentialsBasic
        auth = classe(
            conta=config.conta,
            url_token=TOKEN_PRODUCAO if producao else TOKEN_SANDBOX,
            client_id=config.client_id,
            client_secret=config.client_secret,
            escopos={ESCOPO: "extrato-info"},
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=URL_PRODUCAO if producao else URL_SANDBOX,
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
        cabecalhos = {}
        if self._config.mci_teste:
            cabecalhos["x-br-com-bb-ipa-mciteste"] = self._config.mci_teste
        if self._config.cnpj_parceiro:
            cabecalhos["x-bb-portal-devx-cnpj-parceiro"] = self._config.cnpj_parceiro
        return cabecalhos

    async def listar_lancamentos(
        self,
        conta: ContaBancaria,
        desde: date,
        ate: date,
        cursor: str | None = None,
    ) -> PaginaExtrato:
        if desde > ate:
            raise Validacao("periodo do extrato invertido")
        if (ate - desde).days + 1 > PERIODO_MAXIMO_DIAS:
            raise Validacao(
                f"BB limita o extrato a {PERIODO_MAXIMO_DIAS} dias por consulta"
            )
        if ate > self._hoje():
            raise Validacao("extrato com data futura")
        pagina = cursor or "1"
        if not pagina.isdigit():
            raise Validacao("cursor do extrato BB e o numero da pagina")
        consulta = {
            "gw-dev-app-key": self._config.chave_desenvolvedor,
            "dataInicioSolicitacao": _data_bb(desde),
            "dataFimSolicitacao": _data_bb(ate),
            "numeroPaginaSolicitacao": pagina,
            "quantidadeRegistroPaginaSolicitacao": LINHAS_POR_PAGINA,
        }
        # Agencia e conta sem DV e sem zero a esquerda.
        caminho = (
            f"conta-corrente/agencia/{int(conta.agencia)}/conta/{int(conta.conta)}"
            f"?{urlencode(consulta)}"
        )
        try:
            resp = await self._transporte.requisitar(
                "GET",
                caminho,
                operacao="extrato",
                escopo=ESCOPO,
                cabecalhos=self._cabecalhos(),
            )
        except Validacao as exc:
            if exc.status_http == 404:
                return PaginaExtrato()
            raise
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "extrato do BB nao e JSON", status_http=resp.status_code
            ) from exc
        return self._pagina(dados)

    def _pagina(self, dados: dict[str, Any]) -> PaginaExtrato:
        lancamentos, saldos = [], []
        for linha in dados.get("listaLancamento") or []:
            historico = _texto(linha.get("codigoHistorico")) or "0"
            if historico in HISTORICO_SALDO:
                saldos.append(self._saldo(linha, HISTORICO_SALDO[historico]))
            else:
                lancamentos.append(self._lancamento(linha, historico))
        proxima = dados.get("numeroPaginaProximo") or 0
        return PaginaExtrato(
            lancamentos=tuple(lancamentos),
            saldos=tuple(saldos),
            cursor_proximo=str(proxima) if int(proxima) > 0 else None,
        )

    @staticmethod
    def _saldo(linha: dict[str, Any], tipo: TipoSaldo) -> SaldoExtrato:
        valor = _decimal(linha.get("valorLancamento") or 0)
        if str(linha.get("indicadorSinalLancamento", "C")).upper() == "D":
            valor = -abs(valor)
        data = _data_de_bb(linha.get("dataLancamento"))
        if data is None:
            raise Validacao("linha de saldo sem data no extrato do BB")
        return SaldoExtrato(data=data, tipo=tipo, valor=valor)

    def _lancamento(self, linha: dict[str, Any], historico: str) -> LancamentoExtrato:
        sinal_bruto = str(linha.get("indicadorSinalLancamento") or "").upper()
        if sinal_bruto not in ("C", "D"):
            raise Validacao("lancamento do BB sem sinal C/D", resposta_bruta=linha)
        valor = abs(_decimal(linha.get("valorLancamento")))
        data = _data_de_bb(linha.get("dataLancamento"))
        if data is None or not valor:
            raise Validacao("lancamento do BB sem data ou valor", resposta_bruta=linha)
        estaveis = {k: linha.get(k) for k in _CAMPOS_ESTAVEIS}
        descricao = _texto(linha.get("textoDescricaoHistorico")) or _texto(
            linha.get("textoDescricaoSubHistorico")
        )
        return LancamentoExtrato(
            banco=self.banco,
            id_lancamento=hash_corpo(estaveis),
            id_transacao=_texto(linha.get("textoIdentificadorUnicoTransacao")),
            data=data,
            valor=valor,
            sinal=Sinal(sinal_bruto),
            descricao=descricao or f"historico {historico}",
            historico=historico,
            documento=_texto(linha.get("numeroDocumento")),
            complemento=_texto(linha.get("textoInformacaoComplementar")),
            contrapartida_documento=_texto(
                linha.get(
                    "numeroCadastroPessoaFisicaCadastroNacPessoasJuridicasContrapartida"
                )
            ),
            contrapartida_banco=_texto(linha.get("codigoBancoContrapartida")),
            contrapartida_agencia=_texto(linha.get("codigoAgenciaContrapartida")),
            contrapartida_conta=_texto(linha.get("numeroContaContrapartida")),
            resposta_bruta=linha,
        )
