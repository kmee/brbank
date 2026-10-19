"""Sicredi, API de Cobranca v2 (boleto e boleto hibrido, OAuth2 password).

Escrito a partir do comportamento documentado na leitura do ACBr
(`ACBrBoletoW_Sicredi_APIV2`, `ACBrBoletoRet_Sicredi_APIV2`), usado so como
documentacao (LGPL, nenhum codigo portado). A fonte oficial do Sicredi nao
estava acessivel na escrita: tudo que nao foi confirmado esta em
`docs/sicredi-duvidas.md` e o adapter nasce com `validado_sandbox = False`.

Escolhas deliberadas:

- Nao enviamos `nossoNumero`: o ACBr o calcula no cliente, mas a formula
  (ano, byte e DV) nao esta confirmada. Quem fica com o `nossoNumero` e o banco,
  devolvido no registro e usado como `id_banco`.
- O que o titulo nao traz nao e enviado, e o que tem contrato incerto
  (juros, abatimento no registro, baixa automatica) e recusado com `Validacao`.
- Status desconhecido levanta `Validacao`, nunca vira "pago".
- A consulta e a fonte da verdade: baixa e instrucao devolvem o estado consultado.
- `ECOMM` (autenticacao por `token`/`chaveTransacao`, nao OAuth2) ficou fora.
"""

import asyncio
import json
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2Password
from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado, Validacao
from brbank.mascara import mascarar
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

URL_PRODUCAO = "https://api-parceiro.sicredi.com.br"
URL_SANDBOX = "https://api-parceiro.sicredi.com.br/sb"
CAMINHO_TOKEN = "/auth/openapi/token"
CAMINHO_BOLETOS = "/cobranca/boleto/v1/boletos"
ESCOPO_PADRAO = "cobranca"

# Limites de tamanho que o ACBr aplica antes de enviar (truncar e o que ele faz).
_MAX_NOME = 40
_MAX_ENDERECO = 40
_MAX_CIDADE = 25
_MAX_EMAIL = 40
_MAX_MENSAGENS = 4
_MAX_MENSAGEM = 80

# Campo `situacao` da consulta, comparado sem acento e em maiuscula. As letras
# `L` e `E` vem de um exemplo de forum (nao oficial): ver docs/sicredi-duvidas.md.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM CARTEIRA": StatusCanonico.REGISTRADO,
        "VENCIDO": StatusCanonico.REGISTRADO,
        "E": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "L": StatusCanonico.LIQUIDADO,
        "BAIXADO POR SOLICITACAO": StatusCanonico.BAIXADO,
    },
)

# Campo `movimento` do webhook. Os sufixos REDE, COMPE e PIX vem do
# `tipoLiquidacao` da consulta de liquidados (ACBr); o prefixo e suposicao.
TABELA_WEBHOOK = TabelaStatus.criar(
    "2026.10",
    {
        "LIQUIDACAO_PIX": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_REDE": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_COMPE": StatusCanonico.LIQUIDADO,
    },
)

_CENTAVO = Decimal("0.01")


@dataclass
class SicrediConfig:
    conta: str
    # Usuario e senha do OAuth2 password (codigo de acesso gerado no portal).
    usuario: str
    senha: str = field(repr=False)
    # Chave do aplicativo (`x-api-key`), enviada no token e em toda chamada.
    chave_api: str = field(repr=False)
    codigo_beneficiario: str
    cooperativa: str
    posto: str
    ambiente: str = "producao"
    # Boleto hibrido (boleto com QR Pix). False pede `NORMAL`.
    hibrido: bool = True
    escopo: str = ESCOPO_PADRAO
    requisicoes_por_segundo: float | None = None

    @property
    def url_base(self) -> str:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Sicredi invalido: {self.ambiente!r}")
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Sicredi") from exc


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Sicredi") from exc


def _normalizar(texto: Any) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.upper().split())


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao("pagador sem endereco: o Sicredi exige o endereco")
    logradouro = endereco.logradouro.strip()
    numero = endereco.numero.strip()
    # O ACBr envia "logradouro,numero" em 40 posicoes, cortando o logradouro.
    sobra = _MAX_ENDERECO - (len(numero) + 1)
    dados: dict[str, Any] = {
        "tipoPessoa": "PESSOA_FISICA" if pessoa.tipo == "FISICA" else "PESSOA_JURIDICA",
        "documento": pessoa.documento,
        "nome": pessoa.nome[:_MAX_NOME],
        "endereco": f"{logradouro[:sobra]},{numero}",
        "cidade": endereco.cidade[:_MAX_CIDADE],
        "uf": endereco.uf,
        "cep": endereco.cep,
    }
    if pessoa.email:
        dados["email"] = pessoa.email[:_MAX_EMAIL]
    return dados


class SicrediAdapter(BankAdapter):
    banco: ClassVar[str] = "sicredi"
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.BAIXA
        | Capacidade.ALTERACAO
        | Capacidade.WEBHOOK
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS
    # So vira True depois de rodar contra o sandbox real do Sicredi.
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: SicrediConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        auth = OAuth2Password(
            conta=config.conta,
            url_token=f"{config.url_base}{CAMINHO_TOKEN}",
            usuario=config.usuario,
            senha=config.senha,
            escopos={ESCOPO_PADRAO: config.escopo},
            cabecalhos_token={"x-api-key": config.chave_api, "context": "COBRANCA"},
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

    def _cabecalhos(self, *, com_beneficiario: bool = False) -> dict[str, str]:
        cab = {
            "x-api-key": self._config.chave_api,
            "cooperativa": self._config.cooperativa,
            "posto": self._config.posto,
        }
        if com_beneficiario:
            cab["codigoBeneficiario"] = self._config.codigo_beneficiario
        return cab

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        enc = titulo.encargos
        # Contrato incerto: a unidade do juros nao esta confirmada, o abatimento
        # so tem rota de instrucao e a V2 nao tem baixa automatica no registro.
        for nome, valor in (
            ("juros", enc.juros_mensal_percentual),
            ("abatimento", enc.abatimento),
            ("baixa automatica", titulo.dias_baixa_automatica),
        ):
            if valor is not None:
                raise Validacao(
                    f"Sicredi: {nome} ainda nao suportado pelo adapter "
                    "(ver docs/sicredi-duvidas.md)"
                )
        payload: dict[str, Any] = {
            "tipoCobranca": "HIBRIDO" if self._config.hibrido else "NORMAL",
            "codigoBeneficiario": self._config.codigo_beneficiario,
            "seuNumero": titulo.seu_numero,
            "dataVencimento": titulo.vencimento.isoformat(),
            "valor": _numero(titulo.valor),
            "pagador": _pagador(titulo.pagador),
        }
        if enc.multa_percentual is not None:
            payload["multa"] = _numero(enc.multa_percentual)
        if enc.desconto_percentual is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes or 0)
            payload["tipoDesconto"] = "PERCENTUAL"
            payload["valorDesconto1"] = _numero(enc.desconto_percentual)
            payload["dataDesconto1"] = limite.isoformat()
        if titulo.mensagens:
            payload["mensagens"] = [
                m.strip()[:_MAX_MENSAGEM] for m in titulo.mensagens[:_MAX_MENSAGENS]
            ]
        return payload

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Sicredi nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Sicredi fora do formato", status_http=resp.status_code
            )
        return dados

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        chave = titulo.chave_idempotencia
        trava = self._travas.setdefault(chave, asyncio.Lock())
        async with trava:
            if chave in self._registrados:
                return self._registrados[chave]
            resp = await self._transporte.requisitar(
                "POST",
                CAMINHO_BOLETOS,
                operacao="registrar",
                escopo=ESCOPO_PADRAO,
                json=self._montar_payload(titulo),
                cabecalhos=self._cabecalhos(),
            )
            dados = self._json(resp)
            nosso_numero = dados.get("nossoNumero")
            if not nosso_numero:
                raise Validacao(
                    "resposta do Sicredi sem nossoNumero",
                    status_http=resp.status_code,
                    resposta_bruta=dados,
                )
            registrado = TituloRegistrado(
                banco=self.banco,
                id_banco=str(nosso_numero),
                status=StatusCanonico.REGISTRADO,
                seu_numero=titulo.seu_numero,
                nosso_numero=str(nosso_numero),
                linha_digitavel=dados.get("linhaDigitavel"),
                codigo_barras=dados.get("codigoBarras"),
                pix_copia_cola=dados.get("qrCode"),
                txid=dados.get("txid"),
                resposta_bruta=dados,
            )
            self._registrados[chave] = registrado
            return registrado

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        consulta = urlencode(
            {
                "codigoBeneficiario": self._config.codigo_beneficiario,
                "nossoNumero": id_banco,
            }
        )
        resp = await self._transporte.requisitar(
            "GET",
            f"{CAMINHO_BOLETOS}?{consulta}",
            operacao="consultar",
            escopo=ESCOPO_PADRAO,
            cabecalhos=self._cabecalhos(),
        )
        dados = self._json(resp)
        status = self.tabela_status.traduzir(_normalizar(dados.get("situacao")))
        liquidado = status is StatusCanonico.LIQUIDADO
        liquidacao = dados.get("dadosLiquidacao") or {}
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=dados.get("seuNumero"),
            nosso_numero=dados.get("nossoNumero") or id_banco,
            linha_digitavel=dados.get("linhaDigitavel"),
            codigo_barras=dados.get("codigoBarras"),
            pix_copia_cola=dados.get("codigoQrCode"),
            txid=dados.get("txId"),
            valor_pago=_dinheiro(liquidacao.get("valor")) if liquidado else None,
            data_pagamento=(
                _data(liquidacao.get("data") or dados.get("dataPagamento"))
                if liquidado
                else None
            ),
            resposta_bruta=dados,
        )

    async def _patch(
        self, id_banco: str, sufixo: str, operacao: str, corpo: dict[str, Any]
    ) -> None:
        await self._transporte.requisitar(
            "PATCH",
            f"{CAMINHO_BOLETOS}/{id_banco}/{sufixo}",
            operacao=operacao,
            escopo=ESCOPO_PADRAO,
            json=corpo,
            cabecalhos=self._cabecalhos(com_beneficiario=True),
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._patch(id_banco, "baixa", "baixar", {})
        # A baixa devolve `statusComando`/`transactionId`: o comando pode nao
        # ter sido processado. A consulta e a fonte da verdade.
        return await self.consultar_titulo(id_banco)

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        if instrucao.tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if instrucao.tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
            await self._patch(
                id_banco,
                "data-vencimento",
                "prorrogar",
                {"dataVencimento": instrucao.nova_data_vencimento.isoformat()},
            )
        elif instrucao.tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            await self._patch(
                id_banco,
                "conceder-abatimento",
                "abater",
                {"valorAbatimento": _numero(instrucao.valor)},
            )
        else:
            raise NaoSuportado(f"sicredi: instrucao {instrucao.tipo.name} nao mapeada")
        return await self.consultar_titulo(id_banco)

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook do Sicredi (so funciona em producao; sem assinatura).

        Nao ha como autenticar a origem: o chamador DEVE reconsultar o titulo
        (`consultar_titulo`) antes de dar como pago. Formato do payload nao
        confirmado em fonte oficial (ver docs/sicredi-duvidas.md).
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Sicredi nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        eventos = []
        for item in itens:
            if not isinstance(item, dict) or not item.get("nossoNumero"):
                raise Validacao("webhook do Sicredi sem nossoNumero")
            momento = item.get("dataEvento")
            try:
                ocorrido = datetime.fromisoformat(momento) if momento else None
            except (ValueError, TypeError) as exc:
                raise Validacao("data do evento do Sicredi ilegivel") from exc
            eventos.append(
                EventoCanonico(
                    banco=self.banco,
                    referencia=str(item["nossoNumero"]),
                    tipo=TABELA_WEBHOOK.traduzir(_normalizar(item.get("movimento"))),
                    ocorrido_em=ocorrido,
                    resposta_bruta=mascarar(item),
                )
            )
        return eventos
