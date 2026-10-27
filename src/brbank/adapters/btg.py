"""BTG Pactual (208), BTG Empresas, API Banking - Collections (boleto e bolepix).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o BTG; duvidas em
`docs/btg-duvidas.md`.

Fonte principal: a especificacao OpenAPI oficial publicada no portal
developers.empresas.btgpactual.com (paginas "Criar Cobranca", "Buscar Cobranca",
"Atualizar Cobranca", "Cancelar Cobranca", "Listar Cobrancas", ficha tecnica
"Banking - Collections" e o guia de webhooks), lida em 2026-10-20. O ACBr (LGPL)
foi lido so como documentacao e fala a rota antiga `v1/bank-slips`; nenhum
codigo portado. Nao ha modulo Odoo de origem.

Quirks que moldam o adapter:

- Token so pelo fluxo Authorization Code (o `client_credentials` nao da acesso
  as APIs de Banking). A biblioteca nao faz o fluxo interativo: recebe o
  `refresh_token` ja consentido e o renova (`grant_type=refresh_token`, Basic).
- A alteracao e um PUT com o titulo inteiro (vencimento, expiracao, valor,
  descontos, juros e multa obrigatorios): o adapter consulta, troca o campo e
  devolve o resto como veio.
- `overDueDate` (data de expiracao) e obrigatorio na emissao.
"""

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import ArmazemToken, OAuth2ClientCredentialsBasic
from brbank.capacidades import Capacidade
from brbank.dda import (
    AdapterDda,
    CapacidadeDda,
    DebitoDda,
    EventoDda,
    PaginaDebitos,
    ResumoDda,
    StatusDda,
    TipoEventoDda,
    codigo_barras_da_linha,
    hash_corpo,
)
from brbank.documento import cnpj_valido, normalizar
from brbank.erros import Indisponivel, NaoSuportado, Validacao
from brbank.mascara import MASCARA, mascarar
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

URL_PRODUCAO = "https://api.empresas.btgpactual.com"
URL_SANDBOX = "https://api.sandbox.empresas.btgpactual.com"
TOKEN_PRODUCAO = "https://id.btgpactual.com/oauth2/token"
TOKEN_SANDBOX = "https://id.sandbox.btgpactual.com/oauth2/token"
AUTORIZACAO_PRODUCAO = "https://id.btgpactual.com/oauth2/authorize"
AUTORIZACAO_SANDBOX = "https://id.sandbox.btgpactual.com/oauth2/authorize"
ESCOPO_DDA = "brn:btg:empresas:banking:direct-debit"

# Enum `CollectionStatus` da spec (consulta, criacao e atualizacao). Fora de
# proposito: EXPIRED ("vencida" na ficha, "expirada" no webhook), UPDATED,
# UPDATING, CANCELING, CANCEL_FAILED e RETURNED ("estornada") so aparecem na
# ficha tecnica, sem semantica clara: viram Validacao.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "CREATED": StatusCanonico.REGISTRADO,
        # Vencida e ainda nao cancelada nem paga (ver docs/btg-duvidas.md).
        "OVERDUE": StatusCanonico.REGISTRADO,
        "PROCESSING": StatusCanonico.PENDENTE_REGISTRO,
        "PAID": StatusCanonico.LIQUIDADO,
        "CANCELED": StatusCanonico.BAIXADO,
        "FAILED": StatusCanonico.REJEITADO,
    },
)

# Eventos de webhook da ficha tecnica. A ficha grafa `colections.cancelled`
# (um "l") na tabela e `collections.cancelled` no exemplo: aceitamos os dois.
# `collections.expired` fica fora pelo mesmo motivo do EXPIRED acima.
TABELA_WEBHOOK = TabelaStatus.criar(
    "2026.10",
    {
        "COLLECTIONS.CREATED": StatusCanonico.REGISTRADO,
        "COLLECTIONS.PAID": StatusCanonico.LIQUIDADO,
        "COLLECTIONS.CANCELLED": StatusCanonico.BAIXADO,
        "COLECTIONS.CANCELLED": StatusCanonico.BAIXADO,
    },
)

_NAO_APLICAVEL = {"type": "NOT_APPLICABLE", "value": 0}
# Tamanhos maximos do endereco do pagador na spec (o resto nao tem limite).
_LIMITES_ENDERECO = {"street": 40, "number": 10, "neighborhood": 20, "city": 50}
_LIMITE_DOCUMENTO = 15
_LIMITE_DESCRICAO = 300
# Chaves com PII nas respostas em ingles do BTG que `mascarar` nao conhece.
_CHAVES_PII = frozenset(
    {
        "payer",
        "payee",
        "taxid",
        "name",
        "address",
        "street",
        "zipcode",
        "neighborhood",
        "city",
        "phonenumber",
        "pixkey",
        "description",
        "refreshtoken",
        "idtoken",
    }
)


@dataclass
class BtgConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Obtido uma vez pelo fluxo Authorization Code, fora da biblioteca.
    refresh_token: str = field(repr=False)
    # CNPJ da empresa titular da conta (`companyId` na URL).
    cnpj_empresa: str
    agencia: str
    # Numero da conta com digito, como a spec pede em `account.number`.
    conta_corrente: str
    ambiente: str = "producao"
    # True emite bolepix (`BANKSLIP_QRCODE`); False, boleto simples.
    hibrido: bool = True
    # Dias apos o vencimento ate a expiracao (`overDueDate`), quando o titulo
    # nao traz `dias_baixa_automatica`. Sem os dois a emissao e recusada.
    dias_expiracao: int | None = None
    # Segredo do webhook (o BTG manda em `Authorization: Bearer <segredo>`).
    webhook_secret: str | None = field(default=None, repr=False)
    # Chamado com o novo refresh_token quando o BTG devolve outro: persista.
    ao_renovar_refresh_token: Callable[[str], Awaitable[None] | None] | None = field(
        default=None, repr=False
    )
    # Header `x-response` exigido pelo sandbox (Wiremock): success ou error.
    resposta_sandbox: str = "success"
    requisicoes_por_segundo: float | None = None
    # Aponta o adapter para outro servidor (simulador, testes). Vazio = ambiente.
    url_base_override: str | None = None
    url_token_override: str | None = None
    # Onde o access token vive entre processos (ver `brbank.auth.ArmazemToken`).
    armazem: ArmazemToken | None = field(default=None, repr=False)
    # Le o refresh token vigente DENTRO da trava de renovacao: o valor da config
    # fica velho quando outro processo ja renovou (o BTG pode rotacionar).
    ler_refresh_token: Callable[[], str] | None = field(default=None, repr=False)
    # Escopo do consentimento do DDA (somado a `openid`).
    escopos_dda: str = ESCOPO_DDA

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente BTG invalido: {self.ambiente!r}")
        cnpj = normalizar(self.cnpj_empresa)
        if not cnpj_valido(cnpj):
            raise Validacao("cnpj_empresa do BTG nao e um CNPJ valido")
        self.cnpj_empresa = cnpj
        if self.dias_expiracao is not None and self.dias_expiracao < 0:
            raise Validacao("dias_expiracao nao pode ser negativo")

    @property
    def url_base(self) -> str:
        if self.url_base_override:
            return self.url_base_override
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        if self.url_token_override:
            return self.url_token_override
        return TOKEN_SANDBOX if self.ambiente == "sandbox" else TOKEN_PRODUCAO

    @property
    def url_autorizacao(self) -> str:
        if self.url_token_override:
            base = self.url_token_override
            return (base[: -len("/token")] if base.endswith("/token") else base) + (
                "/authorize"
            )
        return (
            AUTORIZACAO_SANDBOX if self.ambiente == "sandbox" else AUTORIZACAO_PRODUCAO
        )


class _CapturaResposta:
    """Repassa o POST do token e guarda a resposta (para ler o refresh_token)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.resposta: httpx.Response | None = None

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.resposta = await self._client.post(*args, **kwargs)
        return self.resposta


class _RefreshTokenBasic(OAuth2ClientCredentialsBasic):
    """`grant_type=refresh_token` com Basic, guardando o refresh_token novo.

    O BTG pode devolver outro refresh_token a cada renovacao; o valor novo
    passa a valer na memoria e vai para `ao_renovar` (o chamador persiste).
    """

    def __init__(
        self,
        *,
        refresh_token: str,
        ao_renovar: Callable[[str], Awaitable[None] | None] | None = None,
        ler_refresh_token: Callable[[], str] | None = None,
        **comum: Any,
    ) -> None:
        super().__init__(**comum)
        self._refresh_token = refresh_token
        self._ao_renovar = ao_renovar
        self._ler_refresh_token = ler_refresh_token

    def _formulario(self) -> dict[str, str]:
        return {"grant_type": "refresh_token", "refresh_token": self._refresh_token}

    async def _pedir(self, escopo: str | None, client: httpx.AsyncClient) -> Any:
        if self._ler_refresh_token is not None:
            # Dentro da trava: o valor em memoria pode estar velho.
            vigente = await asyncio.to_thread(self._ler_refresh_token)
            if vigente:
                self._refresh_token = vigente
        captura = _CapturaResposta(client)
        token = await super()._pedir(escopo, captura)  # type: ignore[arg-type]
        try:
            novo = captura.resposta.json().get("refresh_token")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            novo = None
        if isinstance(novo, str) and novo and novo != self._refresh_token:
            self._refresh_token = novo
            if self._ao_renovar is not None:
                retorno = self._ao_renovar(novo)
                if inspect.isawaitable(retorno):
                    await retorno
        return token


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _para_json(valor: Any) -> Any:
    """Decimal -> numero JSON, recursivo (eco de dados lidos com Decimal)."""
    if isinstance(valor, Decimal):
        return _numero(valor)
    if isinstance(valor, dict):
        return {chave: _para_json(item) for chave, item in valor.items()}
    if isinstance(valor, list):
        return [_para_json(item) for item in valor]
    return valor


def _decimal(valor: Any) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation as exc:
        raise Validacao("valor ilegivel na resposta do BTG") from exc


def _data(valor: Any) -> date | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do BTG") from exc


def _sem_pii(valor: Any) -> Any:
    """Tira as chaves com PII do BTG e passa o resto por `mascarar`."""

    def limpar(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                chave: (
                    MASCARA
                    if isinstance(chave, str)
                    and chave.lower().replace("_", "") in _CHAVES_PII
                    else limpar(sub)
                )
                for chave, sub in item.items()
            }
        if isinstance(item, list):
            return [limpar(sub) for sub in item]
        return item

    return mascarar(limpar(valor))


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "name": pessoa.nome,
        "taxId": pessoa.documento,
        "personType": "F" if pessoa.tipo == "FISICA" else "J",
    }
    if pessoa.email:
        dados["email"] = pessoa.email
    endereco = pessoa.endereco
    if endereco is not None:
        campos = {
            "street": endereco.logradouro,
            "number": endereco.numero,
            "neighborhood": endereco.bairro,
            "city": endereco.cidade,
        }
        dados["address"] = {
            **{
                chave: texto.strip()[: _LIMITES_ENDERECO[chave]]
                for chave, texto in campos.items()
            },
            "state": endereco.uf,
            "zipCode": endereco.cep,
        }
        if endereco.complemento:
            dados["address"]["complement"] = endereco.complemento
    return dados


# Estados do debito no BTG (consulta e webhook; `PAID` so no webhook). So `PAGO`
# fecha a divida: os estados de pagamento em curso seguem abertos para o Odoo
# (docs/btg-dda-duvidas.md). O estado original fica na `resposta_bruta`.
_STATUS_DDA = {
    "CREATED": StatusDda.ABERTO,
    "OVERDUE": StatusDda.ABERTO,
    "PAYMENT_PENDING_APPROVAL": StatusDda.ABERTO,
    "PAYMENT_PROCESSING": StatusDda.ABERTO,
    "SCHEDULED": StatusDda.ABERTO,
    "PAYMENT_CONFIRMED": StatusDda.PAGO,
    "PAID": StatusDda.PAGO,
}
_PREFIXO_WEBHOOK_DDA = "authorized-direct-debits."
_EVENTOS_DDA = {
    "enabled": TipoEventoDda.DDA_ATIVADO,
    "disabled": TipoEventoDda.DDA_DESATIVADO,
    "debit-added": TipoEventoDda.DEBITO_NOVO,
    "debit-paid": TipoEventoDda.DEBITO_PAGO,
}
# `status` de um DDA ligado (resposta de ativar e desativar).
_STATUS_ATIVACAO = frozenset({"ENABLED", "PENDING_ENABLED"})


def _data_dda(valor: Any) -> date | None:
    """Data do debito; a sentinela `0001-01-01` (sem data) vira None."""
    achada = _data(valor)
    return None if achada is not None and achada.year <= 1 else achada


class BtgAdapter(BankAdapter, AdapterDda):
    banco: ClassVar[str] = "btg"
    validado_sandbox: ClassVar[bool] = False
    capacidades_dda: ClassVar[CapacidadeDda] = (
        CapacidadeDda.ATIVACAO
        | CapacidadeDda.CONSULTA
        | CapacidadeDda.RESUMO
        | CapacidadeDda.ALTERACAO
        | CapacidadeDda.WEBHOOK
    )
    # Itens por pagina na listagem de debitos (o BTG nao documenta o maximo).
    tamanho_pagina_dda: int = 100
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
        | Capacidade.EMISSAO_ASSINCRONA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: BtgConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        auth = _RefreshTokenBasic(
            conta=config.conta,
            url_token=config.url_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
            refresh_token=config.refresh_token,
            ao_renovar=config.ao_renovar_refresh_token,
            ler_refresh_token=config.ler_refresh_token,
            armazem=config.armazem,
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

    async def aclose(self) -> None:
        await self._transporte.aclose()

    def _cabecalhos(self, **extra: str) -> dict[str, str]:
        cab = dict(extra)
        if self._config.ambiente == "sandbox":
            cab["x-response"] = self._config.resposta_sandbox
        return cab

    def _caminho(self, id_banco: str | None = None) -> str:
        caminho = f"{self._config.cnpj_empresa}/banking/collections"
        if id_banco is not None:
            caminho += "/" + quote(id_banco, safe="")
        return caminho

    def _expiracao(self, titulo: Titulo) -> date:
        dias = titulo.dias_baixa_automatica
        if dias is None:
            dias = self._config.dias_expiracao
        if dias is None:
            raise Validacao(
                "btg: overDueDate e obrigatorio; informe dias_baixa_automatica "
                "no titulo ou dias_expiracao na config"
            )
        return titulo.vencimento + timedelta(days=dias)

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        if len(titulo.seu_numero) > _LIMITE_DOCUMENTO:
            raise Validacao(
                f"btg: seu_numero vai em detail.documentNumber "
                f"(maximo {_LIMITE_DOCUMENTO} caracteres)"
            )
        cfg = self._config
        payload: dict[str, Any] = {
            "type": "BANKSLIP_QRCODE" if cfg.hibrido else "BANKSLIP",
            "amount": _numero(titulo.valor),
            "dueDate": titulo.vencimento.isoformat(),
            "overDueDate": self._expiracao(titulo).isoformat(),
            "payer": _pagador(titulo.pagador),
            "account": {"number": cfg.conta_corrente, "branch": cfg.agencia},
            "detail": {"documentNumber": titulo.seu_numero},
        }
        if titulo.mensagens:
            descricao = " ".join(m.strip() for m in titulo.mensagens if m.strip())
            if len(descricao) > _LIMITE_DESCRICAO:
                raise Validacao(
                    f"btg: mensagens somam mais de {_LIMITE_DESCRICAO} caracteres"
                )
            if descricao:
                payload["description"] = descricao
        enc = titulo.encargos
        inicio = (titulo.vencimento + timedelta(days=1)).isoformat()
        if enc.juros_mensal_percentual is not None:
            payload["interest"] = {
                "type": "PERCENTAGE_PER_MONTH",
                "value": _numero(enc.juros_mensal_percentual),
                "startDate": inicio,
            }
        else:
            payload["interest"] = dict(_NAO_APLICAVEL)
        if enc.multa_percentual is not None:
            payload["fine"] = {
                "type": "PERCENTAGE",
                "value": _numero(enc.multa_percentual),
                "startDate": inicio,
            }
        else:
            payload["fine"] = dict(_NAO_APLICAVEL)
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            payload["discounts"] = [
                {
                    "type": "PERCENTAGE",
                    "value": _numero(enc.desconto_percentual),
                    "limitDate": limite.isoformat(),
                }
            ]
        if enc.abatimento is not None:
            payload["rebate"] = {
                "type": "FIXED_VALUE",
                "value": _numero(enc.abatimento),
            }
        # `titulo.beneficiario` nao tem campo na API (sem sacador avalista).
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = json.loads(resp.content, parse_float=Decimal)
        except ValueError as exc:
            raise Validacao(
                "resposta do BTG nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do BTG nao e um objeto", status_http=resp.status_code
            )
        return dados

    def _registrado(
        self,
        dados: dict[str, Any],
        *,
        status: StatusCanonico,
        id_banco: str | None = None,
        seu_numero: str | None = None,
    ) -> TituloRegistrado:
        identificador = dados.get("collectionId") or id_banco
        if not identificador:
            raise Validacao(
                "resposta do BTG sem collectionId", resposta_bruta=_sem_pii(dados)
            )
        detalhe = dados.get("detail") if isinstance(dados.get("detail"), dict) else {}
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            valor_pago = _decimal(dados.get("amountPaid"))
            data_pagamento = _data(dados.get("paidAt"))
        return TituloRegistrado(
            banco=self.banco,
            id_banco=str(identificador),
            status=status,
            seu_numero=detalhe.get("documentNumber") or seu_numero,
            nosso_numero=detalhe.get("ourNumber"),
            linha_digitavel=detalhe.get("digitableLine"),
            codigo_barras=detalhe.get("barCode"),
            pix_copia_cola=detalhe.get("emv"),
            txid=detalhe.get("txId"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pii(dados),
        )

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            self._caminho(),
            operacao="registrar",
            json=self._montar_payload(titulo),
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        # A criacao pode voltar PROCESSING: o titulo ainda nao esta registrado.
        # Sem status, tratamos como pendente (o chamador reconsulta).
        situacao = dados.get("status")
        status = (
            self.tabela_status.traduzir(situacao)
            if situacao
            else StatusCanonico.PENDENTE_REGISTRO
        )
        return self._registrado(dados, status=status, seu_numero=titulo.seu_numero)

    async def _buscar(self, id_banco: str) -> dict[str, Any]:
        resp = await self._transporte.requisitar(
            "GET",
            self._caminho(id_banco),
            operacao="consultar",
            cabecalhos=self._cabecalhos(),
        )
        return self._json(resp)

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        dados = await self._buscar(id_banco)
        status = self.tabela_status.traduzir(dados.get("status"))
        return self._registrado(dados, status=status, id_banco=id_banco)

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
        elif tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
        else:
            raise NaoSuportado(f"btg: instrucao {tipo.value} nao implementada")
        atual = await self._buscar(id_banco)
        corpo = self._corpo_alteracao(atual)
        if tipo is TipoInstrucao.PRORROGACAO:
            self._prorrogar(corpo, instrucao.nova_data_vencimento)  # type: ignore[arg-type]
        else:
            corpo["rebate"] = {
                "type": "FIXED_VALUE",
                "value": _numero(instrucao.valor),  # type: ignore[arg-type]
            }
        resp = await self._transporte.requisitar(
            "PUT",
            self._caminho(id_banco),
            operacao="instrucao",
            json=_para_json(corpo),
            cabecalhos=self._cabecalhos(),
        )
        if not resp.content:
            return await self.consultar_titulo(id_banco)
        dados = self._json(resp)
        status = self.tabela_status.traduzir(dados.get("status"))
        return self._registrado(dados, status=status, id_banco=id_banco)

    @staticmethod
    def _corpo_alteracao(atual: dict[str, Any]) -> dict[str, Any]:
        """O PUT exige o titulo inteiro: parte do estado atual do banco."""
        faltando = [c for c in ("dueDate", "overDueDate", "amount") if not atual.get(c)]
        if faltando:
            raise Validacao(
                f"btg: consulta sem {', '.join(faltando)} para montar a alteracao",
                resposta_bruta=_sem_pii(atual),
            )
        corpo: dict[str, Any] = {
            "dueDate": atual["dueDate"],
            "overDueDate": atual["overDueDate"],
            "amount": atual["amount"],
            "discounts": atual.get("discounts") or [dict(_NAO_APLICAVEL)],
            "interest": atual.get("interest") or dict(_NAO_APLICAVEL),
            "fine": atual.get("fine") or dict(_NAO_APLICAVEL),
        }
        if atual.get("description"):
            corpo["description"] = atual["description"]
        if atual.get("rebate"):
            corpo["rebate"] = atual["rebate"]
        return corpo

    @staticmethod
    def _prorrogar(corpo: dict[str, Any], nova: date) -> None:
        """Novo vencimento, mantendo o prazo de expiracao e o inicio dos encargos
        relativos ao vencimento (descontos ficam com a data que tinham)."""
        antigo = _data(corpo["dueDate"])
        expiracao = _data(corpo["overDueDate"])
        if antigo is None or expiracao is None:
            raise Validacao("btg: datas da cobranca ilegiveis para prorrogar")
        deslocamento = nova - antigo
        corpo["dueDate"] = nova.isoformat()
        corpo["overDueDate"] = (expiracao + deslocamento).isoformat()
        for chave in ("interest", "fine"):
            encargo = dict(corpo[chave])
            inicio = _data(encargo.get("startDate"))
            if inicio is not None:
                encargo["startDate"] = (inicio + deslocamento).isoformat()
            corpo[chave] = encargo

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._transporte.requisitar(
            "DELETE",
            self._caminho(id_banco),
            operacao="baixar",
            cabecalhos=self._cabecalhos(),
        )
        # 204 sem corpo. O cancelamento pode seguir assincrono (CANCELING).
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        resp = await self._transporte.requisitar(
            "GET",
            self._caminho(id_banco),
            operacao="pdf",
            cabecalhos=self._cabecalhos(Accept="application/pdf"),
        )
        if not resp.content.startswith(b"%PDF"):
            raise Validacao("resposta do BTG nao contem PDF")
        return resp.content

    def _conferir_segredo(self, cabecalhos: Mapping[str, str] | None) -> None:
        segredo = self._config.webhook_secret
        if not segredo:
            return
        recebido = ""
        for chave, valor in (cabecalhos or {}).items():
            if chave.lower() == "authorization":
                recebido = valor
                break
        if not hmac.compare_digest(recebido.encode(), f"Bearer {segredo}".encode()):
            raise Validacao("webhook do BTG com segredo ausente ou invalido")

    @staticmethod
    def _itens_webhook(corpo: bytes | str | list | dict) -> list[Any]:
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo, parse_float=Decimal)
            except ValueError as exc:
                raise Validacao("webhook do BTG nao e JSON") from exc
        return corpo if isinstance(corpo, list) else [corpo]

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook `collections.*`: envelope `{webhookId, event, data}`.

        Com `webhook_secret` configurado, confere o `Authorization` que o BTG
        manda. Mesmo assim reconsulte antes de dar como pago. Eventos de outras
        familias (ex.: `authorized-direct-debits.*`, que vem pela mesma URL) sao
        ignorados: quem os trata e `parse_webhook_dda`.
        """
        self._conferir_segredo(cabecalhos)
        eventos = []
        for item in self._itens_webhook(corpo):
            nome = item.get("event") if isinstance(item, dict) else None
            if isinstance(nome, str) and not _familia_cobranca(nome):
                continue
            dados = item.get("data") if isinstance(item, dict) else None
            if not isinstance(dados, dict) or not dados.get("collectionId"):
                raise Validacao("webhook do BTG sem data.collectionId")
            tipo = TABELA_WEBHOOK.traduzir(item.get("event"))
            momento = dados.get("updatedAt")
            try:
                ocorrido = datetime_iso(momento) if momento else None
            except (ValueError, TypeError) as exc:
                raise Validacao("data do evento do BTG ilegivel") from exc
            eventos.append(
                EventoCanonico(
                    banco=self.banco,
                    referencia=str(dados["collectionId"]),
                    tipo=tipo,
                    ocorrido_em=ocorrido,
                    valor_pago=(
                        _decimal(dados.get("amountPaid"))
                        if tipo is StatusCanonico.LIQUIDADO
                        else None
                    ),
                    resposta_bruta=_sem_pii(item),
                )
            )
        return eventos

    # --- DDA ---------------------------------------------------------------

    @staticmethod
    def _cnpj_dda(cnpj: str) -> str:
        normalizado = normalizar(cnpj)
        if not cnpj_valido(normalizado):
            raise Validacao("btg: cnpj do DDA nao e um CNPJ valido")
        return normalizado

    def _caminho_dda(self, cnpj: str, *partes: str) -> str:
        return "/".join((self._cnpj_dda(cnpj), "banking", "direct-debit", *partes))

    def _debito(self, item: Any, cnpj_pagador: str | None) -> DebitoDda:
        """Um debito da consulta ou do webhook (as duas formas do `payee`)."""
        if not isinstance(item, dict):
            raise Validacao("debito do BTG nao e um objeto")
        if cnpj_pagador is None:
            raise Validacao("debito do BTG sem o CNPJ do pagador (owner.taxId)")
        payee = item.get("payee") if isinstance(item.get("payee"), dict) else {}
        banco_payee = payee.get("bank") if isinstance(payee.get("bank"), dict) else {}
        codigo_banco = payee.get("bankCode") or banco_payee.get("code")
        situacao = item.get("status")
        if situacao is not None and situacao not in _STATUS_DDA:
            raise Validacao(
                f"status de debito do BTG desconhecido: {situacao!r}",
                resposta_bruta=_sem_pii(item),
            )
        valor = _decimal(item.get("amount"))
        identificador = item.get("id")
        if valor is None or not identificador:
            raise Validacao(
                "debito do BTG sem id ou amount", resposta_bruta=_sem_pii(item)
            )
        linha = str(item.get("digitableLine") or "")
        try:
            atualizado = (
                datetime_iso(item["updatedAt"]) if item.get("updatedAt") else None
            )
            return DebitoDda(
                banco=self.banco,
                id_banco=str(identificador),
                cnpj_pagador=cnpj_pagador,
                linha_digitavel=linha,
                codigo_barras=codigo_barras_da_linha(linha),
                beneficiario_documento=str(
                    payee.get("document") or payee.get("taxId") or ""
                ),
                beneficiario_nome=str(
                    payee.get("socialName") or payee.get("fantasyName") or ""
                ),
                beneficiario_fantasia=payee.get("fantasyName") or None,
                banco_emissor=str(codigo_banco) if codigo_banco else None,
                valor=valor,
                vencimento=_data_dda(item.get("dueDate")),
                data_limite=_data_dda(item.get("expirationDate")),
                status=_STATUS_DDA[situacao] if situacao else StatusDda.ABERTO,
                atualizado_em=atualizado,
                resposta_bruta=_sem_pii(item),
            )
        except (ValueError, TypeError):
            # Sem encadear: a mensagem do pydantic carrega o dado de entrada.
            raise Validacao(
                "debito do BTG com dado ilegivel", resposta_bruta=_sem_pii(item)
            ) from None

    async def ativar_dda(self, cnpj: str) -> None:
        resp = await self._transporte.requisitar(
            "POST",
            self._caminho_dda(cnpj),
            operacao="dda_ativar",
            cabecalhos=self._cabecalhos(),
        )
        self._conferir_ativacao(resp, ligado=True)

    async def desativar_dda(self, cnpj: str) -> None:
        resp = await self._transporte.requisitar(
            "DELETE",
            self._caminho_dda(cnpj),
            operacao="dda_desativar",
            cabecalhos=self._cabecalhos(),
        )
        self._conferir_ativacao(resp, ligado=False)

    def _conferir_ativacao(self, resp: httpx.Response, *, ligado: bool) -> None:
        """A resposta traz `data.status`; sem corpo, o 2xx basta."""
        if not resp.content:
            return
        dados = self._json(resp)
        situacao = (dados.get("data") or {}).get("status")
        if situacao is None:
            return
        if (situacao in _STATUS_ATIVACAO) != ligado:
            raise Validacao(
                f"btg: DDA ficou {situacao} depois de "
                f"{'ativar' if ligado else 'desativar'}",
                resposta_bruta=_sem_pii(dados),
            )

    async def listar_debitos(
        self,
        cnpj: str,
        desde: date | None = None,
        cursor: str | None = None,
    ) -> PaginaDebitos:
        """Uma pagina da listagem. O cursor e o numero da proxima pagina.

        `desde` filtra por vencimento minimo (`minDueDate`): a consulta nao tem
        filtro por data de criacao (docs/btg-dda-duvidas.md).
        """
        pagina = 1
        if cursor:
            try:
                pagina = int(cursor)
            except ValueError:
                raise Validacao("btg: cursor de debitos invalido") from None
            if pagina < 1:
                raise Validacao("btg: cursor de debitos invalido")
        tamanho = self.tamanho_pagina_dda
        consulta: dict[str, Any] = {"pageNumber": pagina, "pageSize": tamanho}
        if desde is not None:
            consulta["minDueDate"] = desde.isoformat()
        cnpj_pagador = self._cnpj_dda(cnpj)
        resp = await self._transporte.requisitar(
            "GET",
            f"{self._caminho_dda(cnpj, 'debits')}?{urlencode(consulta)}",
            operacao="dda_listar",
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        bruto = dados.get("data")
        if not isinstance(bruto, list):
            raise Validacao(
                "listagem de debitos do BTG sem data", resposta_bruta=_sem_pii(dados)
            )
        itens = tuple(self._debito(item, cnpj_pagador) for item in bruto)
        proximo = None
        # Pagina curta e a ultima; o `_links.next` do exemplo oficial e lixo.
        if itens and len(itens) >= tamanho and _tem_proximo(dados):
            proximo = str(pagina + 1)
        return PaginaDebitos(itens=itens, cursor_proximo=proximo)

    async def resumo_debitos(self, cnpj: str) -> ResumoDda:
        resp = await self._transporte.requisitar(
            "GET",
            self._caminho_dda(cnpj, "debits", "summary"),
            operacao="dda_resumo",
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        resumo = dados.get("data")
        if not isinstance(resumo, dict):
            raise Validacao(
                "resumo de debitos do BTG sem data", resposta_bruta=_sem_pii(dados)
            )

        def item(chave: str) -> tuple[int, Decimal]:
            bloco = resumo.get(chave)
            if not isinstance(bloco, dict):
                return 0, Decimal(0)
            quantia = _decimal(bloco.get("amount")) or Decimal(0)
            return int(bloco.get("count") or 0), quantia

        total, valor_total = item("total")
        abertos = sum(
            item(chave)[0]
            for chave in (
                "totalCreated",
                "totalOverdue",
                "totalPendingApproval",
                "totalScheduled",
            )
        )
        try:
            return ResumoDda(
                quantidade=total,
                valor_total=valor_total,
                por_status={StatusDda.ABERTO: abertos},
            )
        except ValueError:
            raise Validacao(
                "resumo de debitos do BTG ilegivel", resposta_bruta=_sem_pii(dados)
            ) from None

    async def alterar_debito(
        self, cnpj: str, id_banco: str, **campos: Any
    ) -> DebitoDda:
        """Unico campo que o BTG deixa mudar: `oculto` (mostrar ou ocultar)."""
        desconhecidos = sorted(set(campos) - {"oculto"})
        if desconhecidos:
            raise NaoSuportado(
                f"btg: alterar_debito so aceita 'oculto', recebeu {desconhecidos}"
            )
        if not isinstance(campos.get("oculto"), bool):
            raise Validacao("btg: alterar_debito exige oculto=True ou False")
        cnpj_pagador = self._cnpj_dda(cnpj)
        resp = await self._transporte.requisitar(
            "PATCH",
            self._caminho_dda(cnpj, "debits"),
            operacao="dda_alterar",
            json={"items": [{"id": id_banco, "hidden": campos["oculto"]}]},
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        bruto = dados.get("data")
        if isinstance(bruto, list):
            bruto = next(
                (b for b in bruto if isinstance(b, dict) and b.get("id") == id_banco),
                None,
            )
        return self._debito(bruto, cnpj_pagador)

    def parse_webhook_dda(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoDda]:
        """Webhook `authorized-direct-debits.*` (enabled, disabled, debit-added,
        debit-paid). Os demais eventos, inclusive os de cobranca, sao ignorados.

        O `debit-added` ja traz o boleto: nao precisa reconsultar. O `debit-paid`
        so muda o estado do boleto (nunca a contabilidade).
        """
        self._conferir_segredo(cabecalhos)
        eventos = []
        for item in self._itens_webhook(corpo):
            if not isinstance(item, dict):
                raise Validacao("webhook do BTG nao e um objeto")
            sufixo = _sufixo_dda(item.get("event"))
            tipo = _EVENTOS_DDA.get(sufixo) if sufixo else None
            if tipo is None:
                continue
            dados = item.get("data")
            if not isinstance(dados, dict):
                raise Validacao("webhook de DDA do BTG sem data")
            cnpj = _cnpj_do_evento(dados)
            debitos = dados.get("debits") or []
            if not isinstance(debitos, list):
                raise Validacao("webhook de DDA do BTG com debits invalido")
            momento = dados.get("date") or dados.get("updatedAt") or item.get("date")
            try:
                ocorrido = datetime_iso(momento) if momento else None
            except (ValueError, TypeError):
                raise Validacao("data do evento de DDA do BTG ilegivel") from None
            identificador = item.get("eventId") or item.get("id")
            eventos.append(
                EventoDda(
                    banco=self.banco,
                    id_evento=str(identificador or hash_corpo(item)),
                    tipo=tipo,
                    cnpj_pagador=cnpj,
                    debitos=tuple(self._debito(d, cnpj) for d in debitos),
                    ocorrido_em=ocorrido,
                    resposta_bruta=_sem_pii(item),
                )
            )
        return eventos


def _familia_cobranca(evento: str) -> bool:
    nome = evento.strip().lower().rsplit("/", 1)[-1]
    return nome.startswith(("collections.", "colections."))


def _sufixo_dda(evento: Any) -> str | None:
    """`debit-added` de `authorized-direct-debits.debit-added`, com ou sem prefixo."""
    if not isinstance(evento, str):
        return None
    nome = evento.strip().lower()
    posicao = nome.find(_PREFIXO_WEBHOOK_DDA)
    if posicao < 0:
        return None
    return nome[posicao + len(_PREFIXO_WEBHOOK_DDA) :]


def _cnpj_do_evento(dados: dict[str, Any]) -> str | None:
    """CNPJ do pagador: `owner.taxId` ou o prefixo do `accountId`."""
    dono = dados.get("owner") if isinstance(dados.get("owner"), dict) else {}
    candidato = dono.get("taxId") or dono.get("document")
    if not candidato and isinstance(dados.get("accountId"), str):
        candidato = dados["accountId"].split("-", 1)[0]
    if not isinstance(candidato, str):
        return None
    normalizado = normalizar(candidato)
    return normalizado if len(normalizado) == 14 else None


def _tem_proximo(dados: dict[str, Any]) -> bool:
    links = dados.get("_links")
    return isinstance(links, dict) and bool(links.get("next"))


# --- OAuth (Authorization Code com PKCE) --------------------------------------


def gerar_pkce() -> tuple[str, str]:
    """`(code_verifier, code_challenge)` do PKCE S256 (RFC 7636)."""
    verificador = secrets.token_urlsafe(64)
    desafio = (
        base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verificador, desafio


def url_autorizacao(
    config: BtgConfig,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    escopos: str | Iterable[str],
) -> str:
    """URL do consentimento (BTG Id) para o usuario abrir no navegador.

    `openid` e obrigatorio e entra sozinho. PKCE sempre S256.
    """
    lista = escopos.split() if isinstance(escopos, str) else list(escopos)
    if "openid" not in lista:
        lista.insert(0, "openid")
    consulta = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(lista),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.url_autorizacao}?{urlencode(consulta, quote_via=quote)}"


async def trocar_codigo(
    config: BtgConfig,
    codigo: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Troca o `code` do consentimento por tokens (Basic + PKCE).

    Devolve `{"refresh_token", "access_token", "expires_in"}`. O chamador
    persiste o refresh token (no Odoo, na conexao).
    """
    basico = base64.b64encode(
        f"{config.client_id}:{config.client_secret}".encode()
    ).decode()
    dono = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        try:
            resp = await http.post(
                config.url_token,
                data={
                    "grant_type": "authorization_code",
                    "code": codigo,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Authorization": f"Basic {basico}"},
            )
        except httpx.TransportError as exc:
            raise Indisponivel(
                f"erro de transporte ao trocar o codigo: {type(exc).__name__}"
            ) from exc
    finally:
        if dono:
            await http.aclose()
    if resp.status_code == 429 or resp.status_code >= 500:
        raise Indisponivel(
            "BTG indisponivel ao trocar o codigo",
            status_http=resp.status_code,
            resposta_bruta=resp.text,
        )
    if resp.status_code >= 400:
        raise Validacao(
            "BTG recusou o codigo de autorizacao",
            status_http=resp.status_code,
            resposta_bruta=resp.text,
        )
    try:
        corpo = resp.json()
        refresh = corpo["refresh_token"]
        acesso = corpo["access_token"]
    except (ValueError, KeyError, TypeError):
        raise Validacao(
            "resposta do BTG sem refresh_token e access_token",
            status_http=resp.status_code,
        ) from None
    return {
        "refresh_token": refresh,
        "access_token": acesso,
        "expires_in": int(corpo.get("expires_in") or 3600),
    }
