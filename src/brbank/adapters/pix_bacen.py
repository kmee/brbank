"""Pix BACEN generico: API Pix v2 do Banco Central (/cob, /cobv, /webhook).

Baseado no OpenAPI oficial do BACEN (cobranca imediata e com vencimento,
webhook, devolucao). O que muda de PSP para PSP (host, URL do token, estilo de
autenticacao, escopos, cabecalhos e quirks) fica em `PerfilPsp`, declarativo.
Nenhum perfil nasce validado: `validado_sandbox = False` ate rodar contra o
sandbox do banco.

Referencia de comportamento: a documentacao dos PSPs foi consultada nos fontes
do ACBr (ACBrPIXCD, licenca LGPL) apenas como LEITURA, para saber host,
escopos, headers e quirks de cada banco. Nenhum codigo foi portado.
Duvidas e quirks nao implementados: `docs/pix-duvidas.md`.

Decisoes do adapter:

- O `txid` e derivado da `chave_idempotencia` (SHA-256, 32 caracteres), entao
  repetir o registro e seguro: `Duplicado` vira consulta.
- A consulta e a fonte da verdade. `CONCLUIDA` sem nenhum Pix na lista vira
  `Validacao`, nunca liquidacao. O webhook so gera `EventoCanonico`.
- Modalidade `cobv` (padrao) leva multa, juros, desconto e abatimento do
  `Titulo`; `cob` (imediata) recusa encargos.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import (
    Autenticador,
    OAuth2ClientCredentials,
    OAuth2ClientCredentialsBasic,
    OAuth2ClientCredentialsSemSegredo,
    TokenEstatico,
)
from brbank.capacidades import Capacidade
from brbank.erros import Duplicado, NaoSuportado, Validacao
from brbank.mascara import mascarar
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

ESCOPO_LOGICO = "pix"
ESCOPOS_PADRAO = (
    "cob.write",
    "cob.read",
    "cobv.write",
    "cobv.read",
    "pix.write",
    "pix.read",
    "webhook.write",
    "webhook.read",
)
REMOVIDA_PELO_RECEBEDOR = "REMOVIDA_PELO_USUARIO_RECEBEDOR"
_CENTAVO = Decimal("0.01")

TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "ATIVA": StatusCanonico.REGISTRADO,
        "CONCLUIDA": StatusCanonico.LIQUIDADO,
        REMOVIDA_PELO_RECEBEDOR: StatusCanonico.BAIXADO,
        # Expirada ou removida pelo banco: a cobranca nao recebe mais Pix.
        "REMOVIDA_PELO_PSP": StatusCanonico.BAIXADO,
    },
)

AUTENTICACOES = frozenset({"form", "basic", "basic_json", "sem_segredo", "estatico"})


@dataclass(frozen=True)
class PerfilPsp:
    """Particularidades de um PSP sobre a API Pix do BACEN."""

    nome: str
    url_sandbox: str
    url_producao: str
    url_token_sandbox: str
    url_token_producao: str
    # form (id+segredo no corpo), basic (header), basic_json (header + corpo
    # JSON), sem_segredo (so client_id) ou estatico (token fixo do portal).
    autenticacao: str = "form"
    autenticacao_sandbox: str | None = None
    escopos: tuple[str, ...] | None = ESCOPOS_PADRAO
    mtls_producao: bool = True
    mtls_sandbox: bool = True
    http1: bool = False
    cabecalho_client_id: str | None = None
    cabecalho_correlacao: str | None = None
    cabecalho_parceiro: str | None = None
    param_app_key: str | None = None
    metodo_criar_sandbox: str | None = None
    url_revisao: str | None = None
    alias_resposta: Mapping[str, str] = field(default_factory=dict)
    observacoes: tuple[str, ...] = ()
    validado_sandbox: bool = False

    def url_base(self, ambiente: str) -> str:
        return self.url_sandbox if ambiente == "sandbox" else self.url_producao

    def url_token(self, ambiente: str) -> str:
        if ambiente == "sandbox":
            return self.url_token_sandbox
        return self.url_token_producao

    def exige_mtls(self, ambiente: str) -> bool:
        return self.mtls_sandbox if ambiente == "sandbox" else self.mtls_producao

    def modo_auth(self, ambiente: str) -> str:
        if ambiente == "sandbox" and self.autenticacao_sandbox:
            return self.autenticacao_sandbox
        return self.autenticacao


_BB_V2 = "/pix/v2"
_SANTANDER_SANDBOX = "https://pix.santander.com.br/api/v1/sandbox"

_PERFIS = (
    PerfilPsp(
        nome="inter",
        url_sandbox="https://cdpj-sandbox.partners.uatinter.co/pix/v2",
        url_producao="https://cdpj.partners.bancointer.com.br/pix/v2",
        url_token_sandbox="https://cdpj-sandbox.partners.uatinter.co/oauth/v2/token",
        url_token_producao="https://cdpj.partners.bancointer.com.br/oauth/v2/token",
    ),
    PerfilPsp(
        nome="bb",
        url_sandbox="https://api.hm.bb.com.br" + _BB_V2,
        url_producao="https://api-pix.bb.com.br" + _BB_V2,
        url_token_sandbox="https://oauth.hm.bb.com.br/oauth/token",
        url_token_producao="https://oauth.bb.com.br/oauth/token",
        autenticacao="basic",
        cabecalho_parceiro="x-bb-portal-devx-cnpj-parceiro",
        param_app_key="gw-dev-app-key",
        mtls_sandbox=False,
        alias_resposta={"textoImagemQRcode": "pixCopiaECola"},
        observacoes=(
            "API v2; a v1 (api.bb.com.br/pix/v1) esta em docs/pix-duvidas.md",
        ),
    ),
    PerfilPsp(
        nome="sicoob",
        url_sandbox="https://sandbox.sicoob.com.br/sicoob/sandbox/pix/api/v2",
        url_producao="https://api.sicoob.com.br/pix/api/v2",
        url_token_sandbox="",
        url_token_producao=(
            "https://auth.sicoob.com.br/auth/realms/cooperado"
            "/protocol/openid-connect/token"
        ),
        autenticacao="sem_segredo",
        autenticacao_sandbox="estatico",
        cabecalho_client_id="client_id",
        mtls_sandbox=False,
        alias_resposta={"brcode": "pixCopiaECola"},
        observacoes=("sandbox usa o token fixo do portal (token_estatico)",),
    ),
    PerfilPsp(
        nome="sicredi",
        url_sandbox="https://api-pix-h.sicredi.com.br/api/v2",
        url_producao="https://api-pix.sicredi.com.br/api/v2",
        url_token_sandbox="https://api-pix-h.sicredi.com.br/oauth/token",
        url_token_producao="https://api-pix.sicredi.com.br/oauth/token",
        autenticacao="basic",
    ),
    PerfilPsp(
        nome="itau",
        url_sandbox=(
            "https://devportal.itau.com.br/sandboxapi/pix_recebimentos_ext_v2/v2"
        ),
        url_producao="https://secure.api.itau/pix_recebimentos/v2",
        url_token_sandbox="https://devportal.itau.com.br/api/jwt",
        url_token_producao="https://sts.itau.com.br/as/token.oauth2",
        escopos=None,
        mtls_sandbox=False,
        cabecalho_correlacao="x-correlationID",
        observacoes=("sandbox exige o header x-sandbox-token (nao implementado)",),
    ),
    PerfilPsp(
        nome="bradesco",
        url_sandbox="https://qrpix-h.bradesco.com.br/v2",
        url_producao="https://qrpix.bradesco.com.br/v2",
        url_token_sandbox="https://qrpix-h.bradesco.com.br/oauth/token",
        url_token_producao="https://qrpix.bradesco.com.br/oauth/token",
        autenticacao="basic",
        escopos=None,
        http1=True,
    ),
    PerfilPsp(
        nome="santander",
        url_sandbox=_SANTANDER_SANDBOX,
        url_producao="https://trust-pix.santander.com.br/api/v1",
        url_token_sandbox="https://pix.santander.com.br/sandbox/oauth/token"
        "?grant_type=client_credentials",
        url_token_producao="https://trust-pix.santander.com.br/oauth/token"
        "?grant_type=client_credentials",
        escopos=None,
        mtls_sandbox=False,
        metodo_criar_sandbox="POST",
        url_revisao="https://trust-pix.santander.com.br/api/v2",
        observacoes=("PATCH (baixa) usa /api/v2; sandbox cria por POST",),
    ),
    PerfilPsp(
        nome="efi",
        url_sandbox="https://pix-h.api.efipay.com.br/v2",
        url_producao="https://pix.api.efipay.com.br/v2",
        url_token_sandbox="https://pix-h.api.efipay.com.br/oauth/token",
        url_token_producao="https://pix.api.efipay.com.br/oauth/token",
        autenticacao="basic_json",
        escopos=None,
        http1=True,
        cabecalho_parceiro="x-partner-token",
    ),
)

PERFIS: Mapping[str, PerfilPsp] = MappingProxyType({p.nome: p for p in _PERFIS})


def obter_perfil(nome: str) -> PerfilPsp:
    try:
        return PERFIS[nome.lower()]
    except KeyError:
        raise Validacao(
            f"sem perfil Pix para {nome!r} (disponiveis: {sorted(PERFIS)}); "
            "BNB nao consta na referencia consultada, ver docs/pix-duvidas.md"
        ) from None


@dataclass
class PixBacenConfig:
    perfil: str | PerfilPsp
    conta: str
    chave_pix: str = field(repr=False)
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    ambiente: str = "producao"
    # `cobv` (com vencimento, aceita encargos) ou `cob` (imediata).
    modalidade: str = "cobv"
    expiracao_s: int = 86400
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    token_estatico: str | None = field(default=None, repr=False)
    id_parceiro: str | None = None
    app_key: str | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = None


@dataclass(frozen=True)
class DevolucaoPix:
    id: str
    status: str
    valor: Decimal
    rtr_id: str | None = None
    motivo: str | None = field(default=None, repr=False)


def _valor(valor: Decimal) -> str:
    return format(valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP), "f")


def _dinheiro(valor: Any) -> Decimal:
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise Validacao("valor monetario invalido na resposta do PSP") from exc


def _instante(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime_iso(valor)
    except ValueError as exc:
        raise Validacao("data/hora invalida na resposta do PSP") from exc


def gerar_txid(conta: str, chave_idempotencia: str) -> str:
    """32 caracteres [0-9a-f]: dentro de 26-35 alfanumericos do BACEN."""
    base = f"{conta}|{chave_idempotencia}".encode()
    return hashlib.sha256(base).hexdigest()[:32]


def _devedor(pessoa: Pessoa) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "cpf" if pessoa.tipo == "FISICA" else "cnpj": pessoa.documento,
        "nome": pessoa.nome,
    }
    endereco = pessoa.endereco
    if endereco is not None:
        dados.update(
            logradouro=f"{endereco.logradouro}, {endereco.numero}",
            cidade=endereco.cidade,
            uf=endereco.uf,
            cep=endereco.cep,
        )
    if pessoa.email:
        dados["email"] = pessoa.email
    return dados


class PixBacenAdapter(BankAdapter):
    banco: ClassVar[str] = "pix_bacen"
    capacidades: ClassVar[Capacidade] = (
        Capacidade.PIX_COB | Capacidade.PIX_COBV | Capacidade.WEBHOOK | Capacidade.BAIXA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: PixBacenConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if config.ambiente not in ("sandbox", "producao"):
            raise Validacao("ambiente deve ser 'sandbox' ou 'producao'")
        if config.modalidade not in ("cob", "cobv"):
            raise Validacao("modalidade deve ser 'cob' ou 'cobv'")
        perfil = (
            config.perfil
            if isinstance(config.perfil, PerfilPsp)
            else obter_perfil(config.perfil)
        )
        self._config = config
        self.perfil = perfil
        # Dedup e log distinguem o PSP: o mesmo txid pode existir em dois bancos.
        self.banco = f"pix_{perfil.nome}"
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if (
            client is None
            and ssl_context is None
            and perfil.exige_mtls(config.ambiente)
        ):
            raise Validacao(
                f"{perfil.nome} exige certificado mTLS em {config.ambiente}"
            )
        self._escopo = ESCOPO_LOGICO if perfil.escopos else None
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=perfil.url_base(config.ambiente),
            client=client,
            ssl_context=ssl_context,
            autenticador=self._autenticador(),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
            http2=not perfil.http1,
        )

    def _autenticador(self) -> Autenticador:
        cfg, perfil = self._config, self.perfil
        modo = perfil.modo_auth(cfg.ambiente)
        if modo not in AUTENTICACOES:
            raise Validacao(f"autenticacao {modo!r} desconhecida")
        if modo == "estatico":
            if not cfg.token_estatico:
                raise Validacao(f"{perfil.nome} {cfg.ambiente} exige token_estatico")
            return TokenEstatico(cfg.token_estatico)
        if not cfg.client_id:
            raise Validacao("client_id obrigatorio")
        comum: dict[str, Any] = {
            "conta": cfg.conta,
            "url_token": perfil.url_token(cfg.ambiente),
            "escopos": (
                {ESCOPO_LOGICO: " ".join(perfil.escopos)} if perfil.escopos else None
            ),
        }
        if modo == "sem_segredo":
            return OAuth2ClientCredentialsSemSegredo(client_id=cfg.client_id, **comum)
        if not cfg.client_secret:
            raise Validacao("client_secret obrigatorio")
        credenciais = {"client_id": cfg.client_id, "client_secret": cfg.client_secret}
        if modo == "form":
            return OAuth2ClientCredentials(**credenciais, **comum)
        return OAuth2ClientCredentialsBasic(
            **credenciais, corpo_json=modo == "basic_json", **comum
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # --- montagem de requisicao -------------------------------------------

    def _cabecalhos(self) -> dict[str, str]:
        cfg, perfil = self._config, self.perfil
        cabecalhos: dict[str, str] = {}
        if perfil.cabecalho_client_id and cfg.client_id:
            cabecalhos[perfil.cabecalho_client_id] = cfg.client_id
        if perfil.cabecalho_correlacao:
            cabecalhos[perfil.cabecalho_correlacao] = str(uuid.uuid4())
        if perfil.cabecalho_parceiro and cfg.id_parceiro:
            cabecalhos[perfil.cabecalho_parceiro] = cfg.id_parceiro
        return cabecalhos

    def _caminho(self, caminho: str) -> str:
        perfil = self.perfil
        if perfil.param_app_key and self._config.app_key:
            caminho += f"?{perfil.param_app_key}={quote(self._config.app_key)}"
        return caminho

    async def _requisitar(
        self,
        metodo: str,
        caminho: str,
        *,
        operacao: str,
        json: Any = None,
        base_url: str | None = None,
    ) -> httpx.Response:
        return await self._transporte.requisitar(
            metodo,
            self._caminho(caminho),
            operacao=operacao,
            escopo=self._escopo,
            json=json,
            cabecalhos=self._cabecalhos(),
            base_url=base_url,
        )

    def _corpo(self, titulo: Titulo) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        temencargo = any(
            v is not None
            for v in (
                enc.multa_percentual,
                enc.juros_mensal_percentual,
                enc.desconto_percentual,
                enc.abatimento,
            )
        )
        valor: dict[str, Any] = {"original": _valor(titulo.valor)}
        corpo: dict[str, Any] = {
            "valor": valor,
            "chave": cfg.chave_pix,
            "devedor": _devedor(titulo.pagador),
        }
        if titulo.mensagens or titulo.seu_numero:
            itens = [("Documento", titulo.seu_numero)]
            itens += [(f"Mensagem {i}", m) for i, m in enumerate(titulo.mensagens, 1)]
            corpo["infoAdicionais"] = [
                {"nome": nome[:50], "valor": texto[:200]} for nome, texto in itens
            ][:50]
        if cfg.modalidade == "cob":
            if temencargo:
                raise Validacao(
                    "cobranca imediata (cob) nao aceita multa, juros, desconto "
                    "nem abatimento: use a modalidade cobv"
                )
            corpo["calendario"] = {"expiracao": cfg.expiracao_s}
            return corpo
        calendario: dict[str, Any] = {"dataDeVencimento": titulo.vencimento.isoformat()}
        if titulo.dias_baixa_automatica is not None:
            calendario["validadeAposVencimento"] = titulo.dias_baixa_automatica
        corpo["calendario"] = calendario
        if enc.multa_percentual is not None:
            valor["multa"] = {
                "modalidade": 2,
                "valorPerc": _valor(enc.multa_percentual),
            }
        if enc.juros_mensal_percentual is not None:
            valor["juros"] = {
                "modalidade": 3,
                "valorPerc": _valor(enc.juros_mensal_percentual),
            }
        if enc.abatimento is not None:
            valor["abatimento"] = {
                "modalidade": 1,
                "valorPerc": _valor(enc.abatimento),
            }
        if enc.desconto_percentual is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes or 0)
            valor["desconto"] = {
                "modalidade": 2,
                "descontoDataFixa": [
                    {
                        "data": limite.isoformat(),
                        "valorPerc": _valor(enc.desconto_percentual),
                    }
                ],
            }
        return corpo

    # --- leitura de resposta ----------------------------------------------

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                f"resposta do {self.perfil.nome} nao e JSON",
                status_http=resp.status_code,
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                f"resposta do {self.perfil.nome} fora do formato",
                status_http=resp.status_code,
            )
        for origem, destino in self.perfil.alias_resposta.items():
            if origem in dados and destino not in dados:
                dados[destino] = dados.pop(origem)
        return dados

    def _titulo(
        self, dados: dict[str, Any], id_banco: str, seu_numero: str | None = None
    ) -> TituloRegistrado:
        status = self.tabela_status.traduzir(dados.get("status"))
        valor_pago = None
        data_pagamento = None
        if status is StatusCanonico.LIQUIDADO:
            pagos = [p for p in dados.get("pix") or [] if isinstance(p, dict)]
            if not pagos:
                # CONCLUIDA sem Pix: nao ha prova de recebimento, nao liquida.
                raise Validacao(
                    "cobranca CONCLUIDA sem Pix recebido na consulta",
                    resposta_bruta=dados,
                )
            valor_pago = sum((_dinheiro(p.get("valor")) for p in pagos), Decimal(0))
            horarios = [h for h in (_instante(p.get("horario")) for p in pagos) if h]
            data_pagamento = max(horarios).date() if horarios else None
        return TituloRegistrado(
            banco=self.banco,
            id_banco=dados.get("txid") or id_banco,
            status=status,
            seu_numero=seu_numero,
            pix_copia_cola=dados.get("pixCopiaECola"),
            txid=dados.get("txid") or id_banco,
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=dados,
        )

    # --- operacoes ----------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        cfg = self._config
        txid = gerar_txid(cfg.conta, titulo.chave_idempotencia)
        metodo = "PUT"
        if cfg.ambiente == "sandbox" and self.perfil.metodo_criar_sandbox:
            metodo = self.perfil.metodo_criar_sandbox
        try:
            resp = await self._requisitar(
                metodo,
                f"{cfg.modalidade}/{txid}",
                operacao="registrar",
                json=self._corpo(titulo),
            )
        except Duplicado:
            # txid vem da chave de idempotencia: o titulo ja esta no banco.
            return await self.consultar_titulo(txid)
        return self._titulo(self._json(resp), txid, titulo.seu_numero)

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        primaria = self._config.modalidade
        outra = "cob" if primaria == "cobv" else "cobv"
        try:
            resp = await self._requisitar(
                "GET", f"{primaria}/{id_banco}", operacao="consultar"
            )
        except Validacao as erro:
            if erro.status_http != 404:
                raise
            # O titulo pode ter sido emitido na outra modalidade.
            resp = await self._requisitar(
                "GET", f"{outra}/{id_banco}", operacao="consultar"
            )
        return self._titulo(self._json(resp), id_banco)

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        raise NaoSuportado(f"{self.banco}: instrucoes nao implementadas")

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        """Remove a cobranca (PATCH status). O resultado e o estado devolvido."""
        resp = await self._requisitar(
            "PATCH",
            f"{self._config.modalidade}/{id_banco}",
            operacao="baixar",
            json={"status": REMOVIDA_PELO_RECEBEDOR},
            base_url=(
                self.perfil.url_revisao if self._config.ambiente == "producao" else None
            ),
        )
        if resp.content:
            return self._titulo(self._json(resp), id_banco)
        return await self.consultar_titulo(id_banco)

    # --- webhook ------------------------------------------------------------

    async def configurar_webhook(
        self, url_webhook: str, chave: str | None = None
    ) -> None:
        """PUT /webhook/{chave}. O BACEN exige HTTPS e mTLS no receptor."""
        if not url_webhook.startswith("https://"):
            raise Validacao("webhookUrl deve ser HTTPS")
        alvo = quote(chave or self._config.chave_pix, safe="")
        await self._requisitar(
            "PUT",
            f"webhook/{alvo}",
            operacao="webhook",
            json={"webhookUrl": url_webhook},
        )

    async def consultar_webhook(self, chave: str | None = None) -> str | None:
        alvo = quote(chave or self._config.chave_pix, safe="")
        try:
            resp = await self._requisitar("GET", f"webhook/{alvo}", operacao="webhook")
        except Validacao as erro:
            if erro.status_http == 404:
                return None
            raise
        return self._json(resp).get("webhookUrl")

    async def remover_webhook(self, chave: str | None = None) -> None:
        alvo = quote(chave or self._config.chave_pix, safe="")
        await self._requisitar("DELETE", f"webhook/{alvo}", operacao="webhook")

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Callback do Pix BACEN: `{"pix": [{endToEndId, txid, valor, horario}]}`.

        Cada Pix com `txid` vira um evento LIQUIDADO referenciado pelo `txid`
        (o `id_banco` do titulo). Pix sem `txid` (QR estatico) e ignorado, pois
        nao pertence a titulo algum. Repetidos no mesmo corpo colapsam pela
        chave de dedup. Nao confirma pagamento: reconsulte antes de liquidar.
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook Pix nao e JSON") from exc
        if not isinstance(corpo, dict) or not isinstance(corpo.get("pix"), list):
            raise Validacao("webhook Pix sem a lista 'pix'")
        vistos: dict[tuple, EventoCanonico] = {}
        for item in corpo["pix"]:
            if not isinstance(item, dict) or not item.get("endToEndId"):
                raise Validacao("webhook Pix sem endToEndId")
            if not item.get("txid"):
                continue
            evento = EventoCanonico(
                banco=self.banco,
                referencia=item["txid"],
                tipo=StatusCanonico.LIQUIDADO,
                ocorrido_em=_instante(item.get("horario")),
                valor_pago=_dinheiro(item["valor"]) if item.get("valor") else None,
                resposta_bruta=mascarar(item),
            )
            vistos.setdefault(evento.chave_dedup, evento)
        return list(vistos.values())

    # --- devolucao ----------------------------------------------------------

    async def devolver_pix(
        self,
        e2eid: str,
        id_devolucao: str,
        valor: Decimal,
        descricao: str | None = None,
    ) -> DevolucaoPix:
        """PUT /pix/{e2eid}/devolucao/{id}. Nao altera o status do titulo."""
        corpo: dict[str, Any] = {"valor": _valor(valor)}
        if descricao:
            corpo["descricao"] = descricao[:140]
        resp = await self._requisitar(
            "PUT",
            f"pix/{e2eid}/devolucao/{id_devolucao}",
            operacao="devolver",
            json=corpo,
        )
        return self._devolucao(self._json(resp), id_devolucao)

    async def consultar_devolucao(self, e2eid: str, id_devolucao: str) -> DevolucaoPix:
        resp = await self._requisitar(
            "GET", f"pix/{e2eid}/devolucao/{id_devolucao}", operacao="devolucao"
        )
        return self._devolucao(self._json(resp), id_devolucao)

    @staticmethod
    def _devolucao(dados: dict[str, Any], id_devolucao: str) -> DevolucaoPix:
        return DevolucaoPix(
            id=dados.get("id") or id_devolucao,
            status=dados.get("status") or "",
            valor=_dinheiro(dados.get("valor") or "0"),
            rtr_id=dados.get("rtrId"),
            motivo=dados.get("motivo"),
        )


__all__ = [
    "PERFIS",
    "DevolucaoPix",
    "PerfilPsp",
    "PixBacenAdapter",
    "PixBacenConfig",
    "gerar_txid",
    "obter_perfil",
]
