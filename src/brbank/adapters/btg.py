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
import hmac
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentialsBasic
from brbank.capacidades import Capacidade
from brbank.documento import cnpj_valido, normalizar
from brbank.erros import NaoSuportado, Validacao
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
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return TOKEN_SANDBOX if self.ambiente == "sandbox" else TOKEN_PRODUCAO


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
        **comum: Any,
    ) -> None:
        super().__init__(**comum)
        self._refresh_token = refresh_token
        self._ao_renovar = ao_renovar

    def _formulario(self) -> dict[str, str]:
        return {"grant_type": "refresh_token", "refresh_token": self._refresh_token}

    async def _pedir(self, escopo: str | None, client: httpx.AsyncClient) -> Any:
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


class BtgAdapter(BankAdapter):
    banco: ClassVar[str] = "btg"
    validado_sandbox: ClassVar[bool] = False
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

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook `collections.*`: envelope `{webhookId, event, data}`.

        Com `webhook_secret` configurado, confere o `Authorization` que o BTG
        manda. Mesmo assim reconsulte antes de dar como pago.
        """
        self._conferir_segredo(cabecalhos)
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo, parse_float=Decimal)
            except ValueError as exc:
                raise Validacao("webhook do BTG nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        eventos = []
        for item in itens:
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
