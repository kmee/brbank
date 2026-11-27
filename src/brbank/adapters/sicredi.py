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
  (abatimento no registro, baixa automatica) e recusado com `Validacao`.
- Juros pelo Manual API da Cobranca Sicredi 3.9 (p. 28-29): `tipoJuros`
  PERCENTUAL, `tipoJurosPercentual` MENSAL e `juros` com o percentual ao mes.
  Sem `dataInicioJuros`/`dataInicioMulta`: com elas o boleto sai sempre
  tradicional, sem QR Code; sem elas o banco conta do dia seguinte ao vencimento.
- Status desconhecido levanta `Validacao`, nunca vira "pago".
- A consulta e a fonte da verdade: baixa e instrucao devolvem o estado consultado.
- `ECOMM` (autenticacao por `token`/`chaveTransacao`, nao OAuth2) ficou fora.
"""

import asyncio
import json
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank._compat import datetime_iso
from brbank.adapter import BankAdapter
from brbank.auth import OAuth2Password
from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado, Validacao
from brbank.mascara import mascarar
from brbank.modelos import (
    CanalLiquidacao,
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
    somar_informados,
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

# Campo `situacao` da consulta, comparado sem acento e em maiuscula: o enum do
# OpenAPI publico da Cobranca 3.9.1 (developers.sicredi.com.br/public, consulta
# por nosso numero) e o guia "Situacoes do boleto". O liquidado vem detalhado
# pelo canal. Protesto, cartorio e negativacao deixam o titulo em aberto (pago
# depois, chega como LIQUIDADO CARTORIO). As letras `L` e `E` vem de um forum
# (nao oficial) e ficam por compatibilidade: ver docs/sicredi-duvidas.md.
TABELA_STATUS = TabelaStatus.criar(
    "2026.11",
    {
        "EM CARTEIRA": StatusCanonico.REGISTRADO,
        "EM CARTEIRA PIX": StatusCanonico.REGISTRADO,
        "VENCIDO": StatusCanonico.REGISTRADO,
        "E": StatusCanonico.REGISTRADO,
        "PROTESTADO": StatusCanonico.REGISTRADO,
        "EM CARTORIO": StatusCanonico.REGISTRADO,
        "AGUARDANDO ENTRADA EM CARTORIO": StatusCanonico.REGISTRADO,
        "AGUARDANDO SUSTACAO DE CARTORIO": StatusCanonico.REGISTRADO,
        "NEGATIVADO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "LIQUIDADO PIX": StatusCanonico.LIQUIDADO,
        "LIQUIDADO REDE": StatusCanonico.LIQUIDADO,
        "LIQUIDADO COMPE": StatusCanonico.LIQUIDADO,
        "LIQUIDADO CARTORIO": StatusCanonico.LIQUIDADO,
        "LIQUIDADO CHEQUE": StatusCanonico.LIQUIDADO,
        "L": StatusCanonico.LIQUIDADO,
        "BAIXADO POR SOLICITACAO": StatusCanonico.BAIXADO,
        "REJEITADO": StatusCanonico.REJEITADO,
    },
)

# Canal pelo sufixo da situacao liquidada. REDE (rede de atendimento) e COMPE
# (outro banco) sao pagamento pela linha digitavel; cheque fica como OUTRO.
CANAIS_LIQUIDACAO = {
    "LIQUIDADO PIX": CanalLiquidacao.PIX,
    "LIQUIDADO REDE": CanalLiquidacao.CODIGO_BARRAS,
    "LIQUIDADO COMPE": CanalLiquidacao.CODIGO_BARRAS,
    "LIQUIDADO CARTORIO": CanalLiquidacao.CARTORIO,
    "LIQUIDADO CHEQUE": CanalLiquidacao.OUTRO,
}

# Campo `movimento` do webhook. Eventos da contratacao no OpenAPI publico 3.9.1
# (LIQUIDACAO_PIX, _REDE, _COMPE_H5, _H6, _H8, _CARTORIO e ESTORNO_LIQUIDACAO_REDE);
# o corpo do aviso nao esta documentado. O estorno so manda reconsultar, como no
# Santander; LIQUIDACAO_COMPE sem sufixo fica por compatibilidade.
TABELA_WEBHOOK = TabelaStatus.criar(
    "2026.11",
    {
        "LIQUIDACAO_PIX": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_REDE": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_COMPE": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_COMPE_H5": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_COMPE_H6": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_COMPE_H8": StatusCanonico.LIQUIDADO,
        "LIQUIDACAO_CARTORIO": StatusCanonico.LIQUIDADO,
        "ESTORNO_LIQUIDACAO_REDE": StatusCanonico.REGISTRADO,
    },
)

_CENTAVO = Decimal("0.01")

# Dominio de `especieDocumento`, obrigatorio no registro (manual da Cobranca 3.9).
ESPECIES = frozenset(
    {
        "DUPLICATA_MERCANTIL_INDICACAO",
        "DUPLICATA_RURAL",
        "NOTA_PROMISSORIA",
        "NOTA_PROMISSORIA_RURAL",
        "NOTA_SEGUROS",
        "RECIBO",
        "LETRA_CAMBIO",
        "NOTA_DEBITO",
        "DUPLICATA_SERVICO_INDICACAO",
        "OUTROS",
        "BOLETO_PROPOSTA",
        "CARTAO_CREDITO",
        "BOLETO_DEPOSITO",
    }
)


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
    especie: str = "DUPLICATA_MERCANTIL_INDICACAO"
    escopo: str = ESCOPO_PADRAO
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.especie not in ESPECIES:
            raise Validacao(f"especie Sicredi desconhecida: {self.especie!r}")

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
    nome_exibicao: ClassVar[str] = "Sicredi (748)"
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.BAIXA
        | Capacidade.ALTERACAO
        | Capacidade.WEBHOOK
        | Capacidade.PDF
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS
    instrucoes: ClassVar[frozenset[TipoInstrucao]] = frozenset(
        {
            TipoInstrucao.BAIXA,
            TipoInstrucao.PRORROGACAO,
            TipoInstrucao.ABATIMENTO,
        }
    )
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
        # Contrato incerto: o abatimento so tem rota de instrucao e a V2 nao tem
        # baixa automatica no registro.
        for nome, valor in (
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
            "especieDocumento": self._config.especie,
            "seuNumero": titulo.seu_numero,
            "dataVencimento": titulo.vencimento.isoformat(),
            "valor": _numero(titulo.valor),
            "pagador": _pagador(titulo.pagador),
        }
        if enc.multa_percentual is not None:
            payload["multa"] = _numero(enc.multa_percentual)
        if enc.juros_mensal_percentual is not None:
            # Manual 3.9: `tipoJurosPercentual` so vale com PERCENTUAL. Sem
            # `dataInicioJuros` de proposito (ver docstring do modulo).
            payload["tipoJuros"] = "PERCENTUAL"
            payload["tipoJurosPercentual"] = "MENSAL"
            payload["juros"] = _numero(enc.juros_mensal_percentual)
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
        situacao = _normalizar(dados.get("situacao"))
        status = self.tabela_status.traduzir(situacao)
        liquidado = status is StatusCanonico.LIQUIDADO
        liquidacao = dados.get("dadosLiquidacao") or {}
        detalhe: dict[str, Any] = {}
        if liquidado:
            # OpenAPI publico da Cobranca Sicredi 3.9.1 (developers.sicredi.com.br
            # /public): `dadosLiquidacao` traz juros, multa, desconto e abatimento
            # do pagamento; `dataPrevisaoPagamento` e a "data prevista de credito
            # na conta do beneficiario". O canal vem na `situacao` ("LIQUIDADO
            # PIX"...); o LIQUIDADO sem detalhe fica sem canal.
            detalhe = {
                "canal_liquidacao": CANAIS_LIQUIDACAO.get(situacao),
                "data_credito": _data(dados.get("dataPrevisaoPagamento")),
                "valor_juros_multa": somar_informados(
                    _dinheiro(liquidacao.get("juros")),
                    _dinheiro(liquidacao.get("multa")),
                ),
                "valor_desconto": _dinheiro(liquidacao.get("desconto")),
                "valor_abatimento": _dinheiro(liquidacao.get("abatimento")),
            }
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
            **detalhe,
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        """PDF do banco (`GET /boletos/pdf?linhaDigitavel=`, OpenAPI 3.9.1).

        A rota pede a linha digitavel, entao o titulo e consultado antes. No
        sandbox o PDF e o de um boleto pre-montado, nao o do titulo enviado.
        """
        titulo = await self.consultar_titulo(id_banco)
        linha = "".join(c for c in titulo.linha_digitavel or "" if c.isdigit())
        if not linha:
            raise Validacao("consulta do Sicredi sem linha digitavel para o PDF")
        resp = await self._transporte.requisitar(
            "GET",
            f"{CAMINHO_BOLETOS}/pdf?{urlencode({'linhaDigitavel': linha})}",
            operacao="pdf",
            escopo=ESCOPO_PADRAO,
            # O contrato da rota exige o Content-Type mesmo sem corpo.
            cabecalhos={**self._cabecalhos(), "Content-Type": "application/json"},
        )
        if not resp.content.startswith(b"%PDF"):
            raise Validacao("resposta do Sicredi nao contem PDF")
        return resp.content

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

        Nao ha como autenticar a origem (a API nao define assinatura): o chamador
        DEVE reconsultar o titulo (`consultar_titulo`) antes de dar como pago. Os
        eventos sao os da contratacao (OpenAPI 3.9.1); o corpo do aviso nao esta
        documentado (ver docs/sicredi-duvidas.md).
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
                ocorrido = datetime_iso(momento) if momento else None
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
