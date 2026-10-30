"""Bradesco (237), pagamentos do portal Bradesco Developers: Pix por chave e por
dados bancarios, boleto de cobranca, guia e conta de consumo com codigo de barras
e TED (OAuth2 client_credentials + mTLS).

Sem modulo Odoo de origem. ESTADO: `validado_sandbox = False`, nada rodou contra o
banco. O sandbox exige organizacao cadastrada no portal e certificado.

CONFIRMADO no portal oficial, sem login, em 30/10/2026 (API publica do Marketplace,
`developers.bradesco.com.br/api/v1/products`): OpenAPI, manual do produto e collection
Postman de cada produto. Rotas, campos, status e codigos deste arquivo vem de la;
conflitos entre esses tres e o que falta: docs/bradesco-pagamento-duvidas.md.

Terceiros: so confirmam que a API existe e e usada (ajuda da Fortes Tecnologia).
Nenhum campo vem deles.

NAO EXISTE na documentacao publica, por isso nao ha codigo: Pix copia e cola
(`PIX_QRCODE`, o produto "Pix - transfers" nao tem campo para o BR Code), aprovacao
em dois passos (`LIBERACAO`), lote no banco, webhook de pagamento, cancelamento de
Pix, TED e guia.

Pontos do contrato:

- Paga um por vez (`LOTE_LOCAL`), e paga de verdade: nao ha passo de liberacao.
- Dois hosts e dois tokens: o Pix em `qrpix.bradesco.com.br`, o resto em
  `openapi.bradesco.com.br`. No sandbox e um host so.
- `id_banco` e opaco, montado aqui para a consulta funcionar sem outro estado: Pix
  = `idtransacao`, boleto = `transactionId`, guia = `idTransacao|AAAA-MM-DD`, TED =
  `chaveUnicaTed` (documento de 7 digitos + DD.MM.AAAA). Conta fora do id: a conta
  da consulta vem da configuracao, uma instancia por conta.
- Idempotencia: Pix, boleto e guia levam um id gerado pelo cliente, que o banco nao
  aceita repetir. O id deriva da conta e de `chave_idempotencia`, entao a linha sem
  resposta volta INDETERMINADO ja com o id para consultar. TED nao tem id do cliente:
  sem resposta, so o extrato resolve.
- As chamadas que movem dinheiro saem com `SEM_RETRY`. Sem resposta, 5xx, 2xx ilegivel
  e os codigos de "ja feito" ou falha interna do banco viram INDETERMINADO, nunca
  RECUSADO.
"""

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from brbank.adapters.bradesco import (
    _TRECHOS_PII,
    CAMINHO_TOKEN,
    URL_PRODUCAO,
    URL_SANDBOX,
    _AuthBradesco,
)
from brbank.documento import normalizar_documento
from brbank.erros import Duplicado, ErroBrbank, Indisponivel, NaoSuportado, Validacao
from brbank.mascara import MASCARA, mascarar_texto
from brbank.pagamento import (
    AdapterPagamento,
    CapacidadePagamento,
    ContaBancaria,
    DestinoPix,
    LotePagamento,
    LoteRegistrado,
    Pagamento,
    PagamentoRegistrado,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
    status_lote_local,
)
from brbank.transporte import (
    SEM_RETRY,
    LimitadorTaxa,
    PoliticaRetry,
    Transporte,
    contexto_tls,
)

URL_PIX_PRODUCAO = "https://qrpix.bradesco.com.br"
# O manual do token Pix (2.0.0, 06/07/2026) e a collection usam este caminho, com
# credenciais no formulario. O OpenAPI do token traz `/v2/oauth/token` com Basic.
CAMINHO_TOKEN_PIX = "/auth/server/oauth/token"

ROTA_PIX_PAGAR = "v1/spi/solicitar-transferencia"
ROTA_PIX_CONSULTA = "v1/spi/consulta/transferencia"
PREFIXO_BOLETO = "boleto/pagamento-cobranca/v1"
ROTA_BOLETO_VALIDAR = (
    f"{PREFIXO_BOLETO}/cobranca-valida-titulo-pagamento/validaTituloPagamento"
)
ROTA_BOLETO_PRE = f"{PREFIXO_BOLETO}/cobranca-pre-efetivacao/pre-efetivacao-pagamento"
ROTA_BOLETO_PAGAR = f"{PREFIXO_BOLETO}/cobranca-efetivacao/solicitacao/executar"
ROTA_BOLETO_CONSULTA = (
    f"{PREFIXO_BOLETO}/cobranca-pagamento-consulta/consulta-pagamento-especifico"
)
ROTA_BOLETO_CANCELAR = (
    f"{PREFIXO_BOLETO}/cobranca-excluir-agendamento/exclusao/executar"
)
PREFIXO_GUIA = "pagamento/arrecadacao-via-codbarras/v1"
ROTA_GUIA_PAGAR = f"{PREFIXO_GUIA}/pagamentoContaConsumo"
ROTA_TED_PAGAR = "transferencia/ted/v1/efetiva"
ROTA_TED_CONSULTA = "transferencia/ted/v1/consulta"

BANCO_BRADESCO = 237
# Tabela de finalidades do manual de TED: 5 = pagamento de fornecedores.
FINALIDADE_FORNECEDOR = 5

STATUS_PIX = {
    "CONCLUIDO": StatusPagamento.PAGO,
    "EM_PROCESSAMENTO": StatusPagamento.EM_PROCESSAMENTO,
    "REJEITADO": StatusPagamento.REJEITADO,
}
# TABLE 1 do manual de boleto (`situacaoPagamento`).
SITUACAO_BOLETO = {
    1: StatusPagamento.PAGO,
    2: StatusPagamento.CANCELADO,
    3: StatusPagamento.DEVOLVIDO,
    7: StatusPagamento.AGENDADO,
    8: StatusPagamento.CANCELADO,
    9: StatusPagamento.CANCELADO,
    11: StatusPagamento.PAGO,
    12: StatusPagamento.REJEITADO,
}
# `tipoConsulta` da guia: o tipo que encontra a transacao diz o estado.
CONSULTA_GUIA = (
    (3, StatusPagamento.PAGO),
    (4, StatusPagamento.AGENDADO),
    (5, StatusPagamento.REJEITADO),
)
# `statusMensagem` do TED (OpenAPI: "em processamento, processada, devolvida").
STATUS_TED = {
    "EM PROCESSAMENTO": StatusPagamento.EM_PROCESSAMENTO,
    "PROCESSADA": StatusPagamento.PAGO,
    "DEVOLVIDA": StatusPagamento.DEVOLVIDO,
}
# Codigos de sucesso da tabela de retorno do TED.
RETORNOS_TED_OK = frozenset({23, 473, 478, 479, 480})

# Recusa aparente que nao prova que o dinheiro ficou: pagamento ja feito, em
# andamento, ou falha interna do banco no momento do debito.
INCERTOS_PIX = frozenset(
    {"M0240", "M0241", "M0108", "4120", "E4120", "4121", "5001", "E1402"}
)
DUPLICIDADE_BOLETO = frozenset({"CBCA0108", "2135"})
INCERTOS_BOLETO = frozenset({*DUPLICIDADE_BOLETO, "CBCA0037", "2264"})
# 6 = conta ja agendada para debito; 57 = pagamento em duplicidade.
INCERTOS_GUIA = frozenset({"6", "57"})

MOTIVO_NAO_ENVIADO = "NAO_ENVIADO"
MOTIVO_SEM_RESPOSTA = "SEM_RESPOSTA"

_NAMESPACE_IDEMPOTENCIA = uuid.UUID("9b7e4f60-0d3a-4c27-8f11-5a2c6e1d7b42")
_FUSO = ZoneInfo("America/Sao_Paulo")
_MAX_TRANSACTION_ID = 999_999_999
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CAMPOS_DE_CODIGO = (
    "correlationId",
    "causa",
    "codigo",
    "retorno",
    "codigoMotivo",
    "codigoDeErro",
    "codigoDeRetorno",
    "codigoRetorno",
    "codigoMensagem",
    "codigoDaMensagem",
)
_TRECHOS_SENSIVEIS = (
    *_TRECHOS_PII,
    "agencia",
    "barras",
    "cartao",
    "conta",
    "contribuinte",
    "digitavel",
    "documento",
    "favorecido",
    "inscricao",
    "isacdo",
    "placa",
    "portador",
    "ptdor",
    "recebedor",
    "remetente",
    "senha",
    "veiculo",
)


@dataclass
class BradescoPagamentoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # Titular da conta de debito: portador do boleto e remetente do TED.
    pagador_documento: str = field(repr=False)
    pagador_nome: str = field(repr=False)
    # Conta de debito: a consulta do boleto e da guia precisa dela e o contrato de
    # consulta nao a recebe. Uma instancia por conta.
    agencia: str
    conta_corrente: str = field(repr=False)
    digito_conta: str = field(repr=False)
    digito_agencia: int = 0
    poupanca: bool = False
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # O Pix tem token proprio; a credencial pode ser a mesma ou outra.
    pix_client_id: str | None = None
    pix_client_secret: str | None = field(default=None, repr=False)
    caminho_token_pix: str = CAMINHO_TOKEN_PIX
    # A collection oficial manda o token cru; o manual fala em Bearer.
    token_sem_bearer: bool = False
    # `valorTitulo` da efetivacao do boleto: manual em reais (100.99), OpenAPI e
    # collection em centavos (348974). Reais e o padrao porque o erro de leitura
    # vira recusa por valor divergente, e nao pagamento cem vezes maior.
    boleto_valor_em_centavos: bool = False
    ted_finalidade: int = FINALIDADE_FORNECEDOR
    # Cota nao publicada; o manual proibe consulta em rajada.
    requisicoes_por_segundo: float | None = 1.0

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Bradesco invalido: {self.ambiente!r}")
        if not self.agencia.isdigit() or len(self.agencia) > 5:
            raise Validacao("agencia Bradesco: ate 5 digitos, sem o digito")
        if not self.conta_corrente.isdigit() or len(self.conta_corrente) > 13:
            raise Validacao("conta Bradesco: ate 13 digitos, sem o digito")
        if not self.digito_conta or len(self.digito_conta) > 2:
            raise Validacao("digito da conta Bradesco: 1 ou 2 posicoes")
        if not self.pagador_nome.strip():
            raise Validacao("pagador_nome e obrigatorio")
        try:
            documento = normalizar_documento(self.pagador_documento)
        except ValueError as exc:
            raise Validacao("pagador_documento invalido") from exc
        if not documento.isdigit():
            raise Validacao("Bradesco: CNPJ alfanumerico do pagador sem contrato")
        self.pagador_documento = documento

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_pix(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PIX_PRODUCAO

    @property
    def tipo_conta_guia(self) -> int:
        return 2 if self.poupanca else 1


class _Incerto(Exception):
    """Pagamento enviado sem prova de que ficou ou nao ficou."""

    def __init__(self, id_banco: str | None = None) -> None:
        super().__init__("pagamento sem resposta conclusiva")
        self.id_banco = id_banco


def _sem_pii(valor: Any) -> Any:
    if isinstance(valor, dict):
        saida = {}
        for chave, item in valor.items():
            normal = "".join(c for c in str(chave).lower() if c.isalnum())
            if any(trecho in normal for trecho in _TRECHOS_SENSIVEIS):
                saida[chave] = MASCARA
            else:
                saida[chave] = _sem_pii(item)
        return saida
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Validacao("valor ilegivel na resposta do Bradesco") from exc


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _inteiro(valor: Any) -> int | None:
    try:
        return int(str(valor).strip())
    except ValueError:
        return None


def _aaaammdd(dia: date) -> int:
    return dia.year * 10000 + dia.month * 100 + dia.day


def _de_aaaammdd(valor: Any) -> date | None:
    digitos = str(valor or "")
    if len(digitos) != 8 or not digitos.isdigit() or not digitos.strip("0"):
        return None
    try:
        return date(int(digitos[:4]), int(digitos[4:6]), int(digitos[6:]))
    except ValueError:
        return None


def _de_iso(valor: Any) -> date | None:
    if not valor or not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _partes_documento(documento: str | None) -> tuple[str, str, str]:
    """Raiz (9), filial (4) e controle (2), como o Bradesco decompoe CPF e CNPJ."""
    if not documento or not documento.isdigit() or len(documento) not in (11, 14):
        raise Validacao("Bradesco: documento deve ser CPF ou CNPJ numerico")
    if len(documento) == 14:
        return documento[:8].zfill(9), documento[8:12], documento[12:]
    return documento[:9], "0000", documento[9:]


def _bruto_do_erro(erro: ErroBrbank) -> Any:
    bruto = erro.resposta_bruta
    if isinstance(bruto, str):
        try:
            return json.loads(bruto)
        except ValueError:
            return None
    return bruto


def _codigos(bruto: Any) -> set[str]:
    """Codigos do banco nos campos de codigo. Numero perde o zero a esquerda."""
    if not isinstance(bruto, dict):
        return set()
    achados: set[str] = set()
    for campo in _CAMPOS_DE_CODIGO:
        texto = bruto.get(campo)
        if texto is None:
            continue
        for token in re.findall(r"[A-Z0-9]*\d[A-Z0-9]*", str(texto)):
            achados.add(str(int(token)) if token.isdigit() else token)
    return achados


def _motivos(bruto: Any, reserva: str) -> tuple[str, ...]:
    motivos: list[str] = []
    if isinstance(bruto, dict):
        for campo in (*_CAMPOS_DE_CODIGO, "motivo", "title", "detail", "mensagem"):
            if bruto.get(campo) not in (None, "", 0):
                motivos.append(mascarar_texto(str(bruto[campo])))
        for violacao in bruto.get("violacoes") or []:
            if isinstance(violacao, dict) and violacao.get("razao"):
                motivos.append(mascarar_texto(str(violacao["razao"])))
    return tuple(motivos) or (reserva,)


def _erro_do_envelope(dados: dict[str, Any]) -> ErroBrbank | None:
    """O boleto repete o status HTTP no corpo; pode vir erro com HTTP 200."""
    status = _inteiro(dados.get("status"))
    if status is None or status < 400:
        return None
    mensagem = f"Bradesco recusou a requisicao (status {status})"
    if status >= 500:
        return Indisponivel(mensagem, status_http=status, resposta_bruta=dados)
    return Validacao(mensagem, status_http=status, resposta_bruta=dados)


class BradescoPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "bradesco"
    # TRANSFERENCIA depende de `Pagamento.favorecido_nome`: o TED exige o nome do
    # destinatario e o modelo ainda nao tem onde levar (ver o doc de duvidas).
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.PIX
        | CapacidadePagamento.BOLETO
        | CapacidadePagamento.GUIA
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LOTE_LOCAL
        | CapacidadePagamento.CANCELAMENTO
        | (
            CapacidadePagamento.TRANSFERENCIA
            if "favorecido_nome" in Pagamento.model_fields
            else CapacidadePagamento.NENHUMA
        )
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: BradescoPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        agora: Callable[[], datetime] = lambda: datetime.now(_FUSO),
    ) -> None:
        self._config = config
        self._agora = agora
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None:
            raise Validacao("Bradesco exige certificado mTLS (tambem no sandbox)")
        limitador = LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir)
        comum: dict[str, Any] = {
            "banco": self.banco,
            "conta": config.conta,
            "client": client,
            "ssl_context": ssl_context,
            "limitador": limitador,
            "retry": retry,
            "dormir": dormir,
        }
        self._api = Transporte(
            base_url=config.url_base,
            autenticador=_AuthBradesco(
                sem_bearer=config.token_sem_bearer,
                conta=config.conta,
                url_token=config.url_base + CAMINHO_TOKEN,
                client_id=config.client_id,
                client_secret=config.client_secret,
            ),
            **comum,
        )
        self._pix = Transporte(
            base_url=config.url_pix,
            autenticador=_AuthBradesco(
                sem_bearer=config.token_sem_bearer,
                conta=config.conta,
                url_token=config.url_pix + config.caminho_token_pix,
                client_id=config.pix_client_id or config.client_id,
                client_secret=config.pix_client_secret or config.client_secret,
            ),
            **comum,
        )

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._pix.aclose()

    # -- identificadores -----------------------------------------------------

    def _semente(self, p: Pagamento) -> bytes:
        nome = f"{self._config.conta}:{p.chave_idempotencia}"
        return hashlib.sha256(nome.encode()).digest()

    def id_pix(self, p: Pagamento) -> str:
        """`idtransacao`: 32 hexadecimais estaveis para a mesma conta e linha."""
        nome = f"{self._config.conta}:{p.chave_idempotencia}"
        return uuid.uuid5(_NAMESPACE_IDEMPOTENCIA, nome).hex

    def id_boleto(self, p: Pagamento) -> str:
        """`transactionId`: ate 9 digitos. Chave numerica que cabe vai como esta, e e
        o caminho sem colisao; o resto vira um numero derivado da chave."""
        chave = p.chave_idempotencia
        if chave.isdigit() and 0 < int(chave) <= _MAX_TRANSACTION_ID:
            return str(int(chave))
        numero = int.from_bytes(self._semente(p), "big") % _MAX_TRANSACTION_ID
        return str(numero + 1)

    def id_guia(self, p: Pagamento) -> str:
        """`idTransacao`: ate 9 posicoes (o manual mostra "502R" e "4x99")."""
        numero = int.from_bytes(self._semente(p), "big") % 36**9
        saida = ""
        for _ in range(9):
            numero, resto = divmod(numero, 36)
            saida = _BASE36[resto] + saida
        return saida

    # -- chamadas ------------------------------------------------------------

    async def _ler(
        self,
        transporte: Transporte,
        metodo: str,
        caminho: str,
        operacao: str,
        corpo=None,
    ) -> Any:
        """Chamada que nao move dinheiro: retry padrao, erro vira excecao."""
        resp = await transporte.requisitar(
            metodo, caminho, operacao=operacao, json=corpo
        )
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Bradesco nao e JSON", status_http=resp.status_code
            ) from exc
        if isinstance(dados, dict):
            erro = _erro_do_envelope(dados)
            if erro:
                raise erro
        return dados

    async def _objeto(self, *args: Any) -> dict[str, Any]:
        dados = await self._ler(*args)
        if not isinstance(dados, dict):
            raise Validacao("resposta do Bradesco fora do formato")
        return dados

    async def _passo_previo(
        self, duplicidade: frozenset[str], id_banco: str, *args: Any
    ) -> dict[str, Any]:
        """Passo que nao move dinheiro. Duplicidade aqui diz que a linha ja foi enviada
        antes: nao e recusa, e pagamento para conferir."""
        try:
            return await self._objeto(*args)
        except Validacao as exc:
            if _codigos(_bruto_do_erro(exc)) & duplicidade:
                raise _Incerto(id_banco) from exc
            raise

    async def _mover_dinheiro(
        self,
        transporte: Transporte,
        caminho: str,
        operacao: str,
        corpo: dict[str, Any],
        id_banco: str | None,
        incertos: frozenset[str],
    ) -> dict[str, Any]:
        """POST que paga, sem retry. So deixa passar a recusa clara do banco; todo o
        resto vira `_Incerto`."""
        try:
            resp = await transporte.requisitar(
                "POST", caminho, operacao=operacao, json=corpo, retry=SEM_RETRY
            )
        except (Indisponivel, Duplicado) as exc:
            raise _Incerto(id_banco) from exc
        except Validacao as exc:
            if _codigos(_bruto_do_erro(exc)) & incertos:
                raise _Incerto(id_banco) from exc
            raise
        try:
            dados = resp.json()
        except ValueError as exc:
            raise _Incerto(id_banco) from exc
        if not isinstance(dados, dict) or not dados:
            raise _Incerto(id_banco)
        erro = _erro_do_envelope(dados)
        if isinstance(erro, Indisponivel):
            raise _Incerto(id_banco) from erro
        if erro:
            if _codigos(dados) & incertos:
                raise _Incerto(id_banco) from erro
            raise erro
        return dados

    # -- montagem --------------------------------------------------------------

    def _hoje(self) -> date:
        return self._agora().date()

    def _exigir_conta(self, conta: ContaBancaria) -> None:
        cfg = self._config
        if (conta.agencia.lstrip("0"), conta.conta.lstrip("0")) != (
            cfg.agencia.lstrip("0"),
            cfg.conta_corrente.lstrip("0"),
        ):
            raise Validacao("bradesco: conta de debito diferente da configurada")

    def _so_hoje(self, p: Pagamento, nome: str) -> None:
        # Sem campo de data no Pix e agendamento vedado no TED: pagar hoje o que era
        # para depois seria pior que recusar.
        if p.data != self._hoje():
            raise Validacao(f"bradesco: {nome} nao agenda, so paga na data de hoje")

    @staticmethod
    def _chave(destino: DestinoPix) -> str:
        assert destino.chave is not None and destino.tipo_chave is not None
        chave = destino.chave.strip()
        tipo = destino.tipo_chave
        if tipo is TipoChavePix.TELEFONE:
            digitos = "".join(c for c in chave if c.isdigit())
            if len(digitos) in (10, 11):
                digitos = "55" + digitos
            if not (digitos.startswith("55") and len(digitos) in (12, 13)):
                raise Validacao("chave Pix de telefone deve ter DDD e numero")
            return "+" + digitos
        if tipo is TipoChavePix.DOCUMENTO:
            documento = "".join(c for c in chave if c.isalnum()).upper()
            if len(documento) not in (11, 14):
                raise Validacao("chave Pix de documento deve ser CPF ou CNPJ")
            return documento
        if tipo is TipoChavePix.ALEATORIA:
            return chave.lower()
        return chave

    def _recebedor(self, destino: DestinoPix) -> dict[str, Any]:
        documento = destino.documento
        if documento and not (documento.isdigit() and len(documento) in (11, 14)):
            raise Validacao("bradesco: documento do recebedor Pix deve ser numerico")
        if destino.chave is not None:
            assert destino.tipo_chave is not None
            tipo = {
                TipoChavePix.TELEFONE: "TELEFONE",
                TipoChavePix.EMAIL: "EMAIL",
                # O manual e a collection dizem CPFCNPJ; o OpenAPI, CPF-CNPJ.
                TipoChavePix.DOCUMENTO: "CPFCNPJ",
                TipoChavePix.ALEATORIA: "EVP",
            }[destino.tipo_chave]
            recebedor: dict[str, Any] = {
                "tipoChave": tipo,
                "chavePix": self._chave(destino),
            }
            if documento:
                recebedor["cpfCnpj"] = documento
            if destino.nome:
                recebedor["nomeFavorecido"] = destino.nome[:60]
            return recebedor
        conta = destino.conta
        assert conta is not None
        if not conta.ispb:
            raise Validacao("bradesco: Pix por dados bancarios exige o ISPB do destino")
        if not documento or not destino.nome:
            raise Validacao("bradesco: Pix por dados bancarios exige documento e nome")
        if len(conta.agencia) > 4:
            raise Validacao("bradesco: agencia de destino tem ate 4 digitos")
        recebedor = {
            "tipoChave": "AGENCIACONTA",
            "cpfCnpj": documento,
            "ispb": conta.ispb,
            "agencia": conta.agencia,
            "conta": conta.conta,
            "digitoConta": conta.digito.upper(),
            "tipoConta": "CONTA_POUPANCA" if conta.poupanca else "CONTA_CORRENTE",
            "nomeFavorecido": destino.nome[:60],
        }
        if conta.compe:
            recebedor["banco"] = conta.compe
        return recebedor

    def _linha(
        self,
        p: Pagamento,
        id_banco: str,
        status: StatusPagamento,
        dados: dict[str, Any],
        *,
        motivos: tuple[str, ...] = (),
        autenticacao: Any = None,
        data: date | None = None,
    ) -> PagamentoRegistrado:
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=data or p.data,
            motivos=motivos,
            autenticacao=str(autenticacao) if autenticacao else None,
            resposta_bruta=_sem_pii(dados),
        )

    def _recusado(self, p: Pagamento, motivos: tuple[str, ...]) -> PagamentoRegistrado:
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco="",
            status=StatusPagamento.RECUSADO,
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=p.data,
            motivos=motivos,
        )

    def _no_futuro(self, *datas: date | None) -> bool:
        hoje = self._hoje()
        return any(d is not None and d > hoje for d in datas)

    # -- Pix -------------------------------------------------------------------

    async def _pagar_pix(
        self, p: Pagamento, conta: ContaBancaria
    ) -> PagamentoRegistrado:
        assert p.pix is not None
        self._so_hoje(p, "Pix")
        if len(conta.agencia) > 4:
            raise Validacao("bradesco: agencia do pagador Pix tem ate 4 digitos")
        id_transacao = self.id_pix(p)
        corpo: dict[str, Any] = {
            "idtransacao": id_transacao,
            "pagador": {
                "tipoChave": "AGENCIACONTA",
                "agencia": conta.agencia,
                "conta": conta.conta,
            },
            "recebedor": self._recebedor(p.pix),
            "valor": f"{p.valor:.2f}",
        }
        if p.descricao:
            corpo["descricao"] = p.descricao[:30]
        dados = await self._mover_dinheiro(
            self._pix, ROTA_PIX_PAGAR, "pagar_pix", corpo, id_transacao, INCERTOS_PIX
        )
        status = STATUS_PIX.get(str(dados.get("status")))
        if status is None:
            raise _Incerto(id_transacao)
        return self._linha(
            p,
            id_transacao,
            status,
            dados,
            motivos=self._motivos_pix(dados, status),
            autenticacao=dados.get("e2e"),
        )

    @staticmethod
    def _motivos_pix(dados: dict[str, Any], status: StatusPagamento) -> tuple[str, ...]:
        if status is not StatusPagamento.REJEITADO:
            return ()
        return _motivos(dados, "REJEITADO")

    # -- boleto ----------------------------------------------------------------

    def _campos_boleto(
        self, p: Pagamento, conta: ContaBancaria, vencimento: date, ctrl_cip: str
    ) -> dict[str, Any]:
        agora = self._agora()
        assert p.codigo_barras is not None
        return {
            "formaCaptura": 1,
            "codigoBarras": p.codigo_barras,
            "dataVencimento": _aaaammdd(vencimento),
            "dataMovimento": _aaaammdd(agora.date()),
            "dataPagamento": _aaaammdd(p.data),
            "horaTransacao": int(agora.strftime("%H%M%S")),
            "numeroAutenticacao": "",
            "formaPagamento": 2,
            "bancoDebito": BANCO_BRADESCO,
            "agenciaDebito": int(conta.agencia),
            "digitoAgencia": self._config.digito_agencia,
            "contaDebito": int(conta.conta),
            "digitoConta": conta.digito,
            "linhaExtrato": (p.descricao or "")[:32],
            "numeroCtrlCip": ctrl_cip,
        }

    async def _pagar_boleto(
        self, p: Pagamento, conta: ContaBancaria
    ) -> PagamentoRegistrado:
        assert p.codigo_barras is not None
        cfg = self._config
        if conta.poupanca:
            raise Validacao(
                "bradesco: boleto so debita conta corrente (formaPagamento 2)"
            )
        raiz, filial, controle = _partes_documento(cfg.pagador_documento)
        id_transacao = self.id_boleto(p)
        validado = await self._passo_previo(
            DUPLICIDADE_BOLETO,
            id_transacao,
            self._api,
            "POST",
            ROTA_BOLETO_VALIDAR,
            "validar_boleto",
            {
                "tipoEntrada": 1,
                "dadosEntrada": "0" * 10 + p.codigo_barras,
                "bancoTitulo": int(p.codigo_barras[:3]),
                "agenciaDeb": int(conta.agencia),
                "contaDeb": int(conta.conta),
            },
        )
        if _inteiro(validado.get("codigoRetorno")):
            raise Validacao("Bradesco recusou o titulo", resposta_bruta=validado)
        vencimento = p.vencimento or _de_aaaammdd(validado.get("dataVencimento"))
        if vencimento is None:
            raise Validacao("bradesco: titulo sem vencimento (informe vencimento)")
        ctrl_cip = str(validado.get("numeroCtrlCip") or "")
        if str(validado.get("consultaCip")).upper() == "S" and not ctrl_cip:
            raise Validacao("bradesco: titulo exige numeroCtrlCip e ele nao veio")
        campos = self._campos_boleto(p, conta, vencimento, ctrl_cip)
        await self._passo_previo(
            DUPLICIDADE_BOLETO,
            id_transacao,
            self._api,
            "POST",
            ROTA_BOLETO_PRE,
            "pre_efetivar_boleto",
            {
                **campos,
                "agencia": int(conta.agencia),
                "valorTitulo": _numero(p.valor),
            },
        )
        valor = int(p.valor * 100) if cfg.boleto_valor_em_centavos else _numero(p.valor)
        dados = await self._mover_dinheiro(
            self._api,
            ROTA_BOLETO_PAGAR,
            "pagar_boleto",
            {
                **campos,
                "codigoAgencia": int(conta.agencia),
                "valorTitulo": valor,
                "indicadorFuncao": "1",
                "transactionId": int(id_transacao),
                "tipoIntermediador": "",
                "cnpjCpfPtdor": int(raiz),
                "filialCnpjPtdor": int(filial),
                "ctrlCnpjPtdor": int(controle),
            },
            id_transacao,
            INCERTOS_BOLETO,
        )
        quitacao = _de_aaaammdd(dados.get("dataQuitacao"))
        return self._linha(
            p,
            id_transacao,
            StatusPagamento.AGENDADO
            if self._no_futuro(p.data, quitacao)
            else StatusPagamento.PAGO,
            dados,
            autenticacao=dados.get("nroProtocolo"),
            data=quitacao if self._no_futuro(quitacao) else None,
        )

    # -- guia ------------------------------------------------------------------

    async def _pagar_guia(
        self, p: Pagamento, conta: ContaBancaria
    ) -> PagamentoRegistrado:
        assert p.codigo_barras is not None
        id_transacao = self.id_guia(p)
        id_banco = f"{id_transacao}|{p.data.isoformat()}"
        corpo: dict[str, Any] = {
            "agencia": int(conta.agencia),
            "digitoAgencia": self._config.digito_agencia,
            "conta": int(conta.conta),
            "digitoConta": conta.digito,
            "tipoConta": 2 if conta.poupanca else 1,
            "codigoBarras": p.codigo_barras,
            "dataDebito": p.data.isoformat(),
            "idTransacao": id_transacao,
            "valorPrincipal": _numero(p.valor),
        }
        if p.descricao:
            corpo["descricaoCliente"] = p.descricao[:32]
        validado = await self._passo_previo(
            INCERTOS_GUIA,
            id_banco,
            self._api,
            "POST",
            ROTA_GUIA_PAGAR,
            "validar_guia",
            {**corpo, "tipoRegistro": 0},
        )
        previo = _inteiro(validado.get("retorno"))
        if str(previo) in INCERTOS_GUIA:
            raise _Incerto(id_banco)
        if previo:
            raise Validacao("Bradesco recusou a guia", resposta_bruta=validado)
        dados = await self._mover_dinheiro(
            self._api,
            ROTA_GUIA_PAGAR,
            "pagar_guia",
            {**corpo, "tipoRegistro": 1},
            id_banco,
            INCERTOS_GUIA,
        )
        retorno = _inteiro(dados.get("retorno"))
        if retorno is None or str(retorno) in INCERTOS_GUIA:
            raise _Incerto(id_banco)
        if retorno:
            raise Validacao("Bradesco recusou a guia", resposta_bruta=dados)
        return self._linha(
            p,
            id_banco,
            StatusPagamento.AGENDADO
            if self._no_futuro(p.data)
            else StatusPagamento.PAGO,
            dados,
            autenticacao=dados.get("autenticacaoBancaria"),
        )

    # -- TED -------------------------------------------------------------------

    def _corpo_ted(self, p: Pagamento, conta: ContaBancaria) -> dict[str, Any]:
        destino = p.conta_credito
        assert destino is not None
        cfg = self._config
        self._so_hoje(p, "TED")
        nome = getattr(p, "favorecido_nome", None)
        if not nome:
            raise Validacao("bradesco: TED exige o nome do favorecido")
        if not destino.compe:
            raise Validacao("bradesco: TED exige o COMPE do banco de destino")
        if int(destino.compe) == BANCO_BRADESCO:
            raise Validacao("bradesco: TED nao aceita conta de destino no Bradesco")
        raiz, filial, controle = _partes_documento(p.favorecido_documento)
        for digito in (conta.digito, destino.digito):
            if not digito.isdigit():
                raise Validacao("bradesco: TED leva a conta numerica, com o digito")
        return {
            "identificadorDoTipoDeTransferencia": 1,
            "agenciaRemetente": int(conta.agencia),
            "contaRemetenteComDigito": int(conta.conta + conta.digito),
            "tipoContaRemetente": "PP" if conta.poupanca else "CC",
            "tipoDePessoaRemetente": "J" if len(cfg.pagador_documento) == 14 else "F",
            "nomeClienteRemetente": cfg.pagador_nome[:80],
            "bancoDestinatario": int(destino.compe),
            "agenciaDestinatario": int(destino.agencia),
            "contaDestinatario": int(destino.conta + destino.digito),
            "tipoDeContaDestinatario": "PP" if destino.poupanca else "CC",
            "tipodePessoaDestinatario": "J"
            if len(p.favorecido_documento or "") == 14
            else "F",
            "numeroInscricao": raiz,
            "numeroFilial": filial.zfill(5),
            "numeroControle": controle,
            "nomeClienteDestinatario": nome[:80],
            "valorDaTransferencia": _numero(p.valor),
            "finalidadeDaTransferencia": cfg.ted_finalidade,
            "codigoIdentificadorDaTransferencia": self.id_pix(p)[:25],
            "dataMovimento": p.data.strftime("%d.%m.%Y"),
            "tipoDeDoc": "D"
            if p.favorecido_documento == cfg.pagador_documento
            else "E",
            # Opcionais: o manual manda preencher com zero ou branco.
            "canalPagamento": 0,
            "indicadorDda": "N",
            "tipoDeDocumentoDeBarras": "",
            "numeroCodigoDeBarras": "",
            "valorMulta": 0,
            "valorJuro": 0,
            "valorDescontoOuAbatimento": 0,
            "valorOutrosAcrescimos": 0,
        }

    async def _pagar_ted(
        self, p: Pagamento, conta: ContaBancaria
    ) -> PagamentoRegistrado:
        corpo = self._corpo_ted(p, conta)
        dados = await self._mover_dinheiro(
            self._api, ROTA_TED_PAGAR, "pagar_ted", corpo, None, frozenset()
        )
        retorno = _inteiro(dados.get("codigoDeRetorno"))
        chave = str(dados.get("chaveUnicaParaApi") or "")
        documento = chave[:7]
        id_banco = (
            f"{documento}{corpo['dataMovimento']}"
            if len(documento) == 7 and documento.isdigit()
            else None
        )
        if retorno in RETORNOS_TED_OK and id_banco:
            return self._linha(
                p,
                id_banco,
                StatusPagamento.EM_PROCESSAMENTO,
                dados,
                autenticacao=chave,
            )
        if retorno is not None and retorno not in RETORNOS_TED_OK and not chave:
            raise Validacao("Bradesco recusou o TED", resposta_bruta=dados)
        raise _Incerto(id_banco)

    # -- operacoes -------------------------------------------------------------

    async def reservar_numero_lote(self) -> str:
        """Numero local: os pagamentos vao um a um."""
        return uuid.uuid4().hex

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Envia linha a linha. Recusa de uma linha nao para as outras; linha sem
        resposta (INDETERMINADO) para o envio e as seguintes voltam RECUSADO com
        motivo NAO_ENVIADO. O banco fora do ar antes do passo que paga tambem para o
        envio, mas ai a linha volta RECUSADO: nada saiu da conta."""
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        self._exigir_conta(lote.conta_debito)
        pagar = {
            TipoPagamento.PIX: self._pagar_pix,
            TipoPagamento.BOLETO: self._pagar_boleto,
            TipoPagamento.GUIA: self._pagar_guia,
            TipoPagamento.TRANSFERENCIA: self._pagar_ted,
        }[lote.tipo]
        numero = lote.numero or await self.reservar_numero_lote()
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for p in lote.pagamentos:
            if parou:
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO,)))
                continue
            try:
                linhas.append(await pagar(p, lote.conta_debito))
            except _Incerto as exc:
                parou = True
                linhas.append(
                    PagamentoRegistrado(
                        banco=self.banco,
                        id_banco=exc.id_banco or "",
                        status=StatusPagamento.INDETERMINADO,
                        chave_idempotencia=p.chave_idempotencia,
                        valor=p.valor,
                        data=p.data,
                        motivos=(MOTIVO_SEM_RESPOSTA,),
                    )
                )
            except Indisponivel as exc:
                parou = True
                motivos = _motivos(_bruto_do_erro(exc), exc.mensagem)
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO, *motivos)))
            except ErroBrbank as exc:
                linhas.append(
                    self._recusado(p, _motivos(_bruto_do_erro(exc), exc.mensagem))
                )
        pagamentos = tuple(linhas)
        return LoteRegistrado(
            banco=self.banco,
            id_banco=numero,
            tipo=lote.tipo,
            status=status_lote_local(pagamentos),
            pagamentos=pagamentos,
        )

    async def consultar_lote(
        self, id_banco: str, tipo: TipoPagamento
    ) -> LoteRegistrado:
        raise NaoSuportado("bradesco: o lote e local (LOTE_LOCAL); consulte cada linha")

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        if not id_banco:
            raise Validacao("bradesco: consulta exige o id do pagamento")
        if tipo is TipoPagamento.PIX:
            return await self._consultar_pix(id_banco)
        if tipo is TipoPagamento.BOLETO:
            return await self._consultar_boleto(id_banco)
        if tipo is TipoPagamento.GUIA:
            return await self._consultar_guia(id_banco)
        if tipo is TipoPagamento.TRANSFERENCIA:
            return await self._consultar_ted(id_banco)
        raise NaoSuportado(f"bradesco: consulta de {tipo.value} nao existe")

    async def _consultar_pix(self, id_banco: str) -> PagamentoRegistrado:
        if not id_banco.isalnum():
            raise Validacao("bradesco: id de Pix invalido")
        dados = await self._objeto(
            self._pix, "GET", f"{ROTA_PIX_CONSULTA}/{id_banco}", "consultar_pix"
        )
        status = STATUS_PIX.get(str(dados.get("status")))
        if status is None:
            raise Validacao("bradesco: status de Pix nao mapeado", resposta_bruta=dados)
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            valor=_dinheiro(dados.get("valor")),
            data=_de_iso(dados.get("dataEfetivacao"))
            or _de_iso(dados.get("dataCriacao")),
            motivos=self._motivos_pix(dados, status),
            autenticacao=dados.get("e2e") or None,
            resposta_bruta=_sem_pii(dados),
        )

    async def _consultar_boleto(self, id_banco: str) -> PagamentoRegistrado:
        if not id_banco.isdigit():
            raise Validacao("bradesco: id de boleto invalido")
        cfg = self._config
        dados = await self._objeto(
            self._api,
            "POST",
            ROTA_BOLETO_CONSULTA,
            "consultar_boleto",
            {
                "versaoLayout": "1",
                "numeroBanco": str(BANCO_BRADESCO),
                "numeroAgencia": cfg.agencia,
                "numeroConta": cfg.conta_corrente,
                "numeroProtocolo": "0",
                "dataPagamento": "0",
                "transactionId": id_banco,
            },
        )
        status = SITUACAO_BOLETO.get(_inteiro(dados.get("situacaoPagamento")))
        if status is None:
            raise Validacao(
                "bradesco: situacao de boleto nao mapeada", resposta_bruta=dados
            )
        motivo = dados.get("motNaoEfet")
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            valor=_dinheiro(dados.get("valorPagamento")),
            data=_de_aaaammdd(dados.get("dataInclTit")),
            motivos=tuple(
                mascarar_texto(str(m))
                for m in (motivo, dados.get("descrMotivo"))
                if m not in (None, "", 0)
            ),
            autenticacao=str(dados["numeroProtocolo"])
            if dados.get("numeroProtocolo")
            else None,
            resposta_bruta=_sem_pii(dados),
        )

    async def _consultar_guia(self, id_banco: str) -> PagamentoRegistrado:
        id_transacao, _, dia = id_banco.partition("|")
        if not id_transacao or _de_iso(dia) is None:
            raise Validacao("bradesco: id de guia invalido")
        cfg = self._config
        caminho = (
            f"{PREFIXO_GUIA}/{cfg.agencia}/{cfg.conta_corrente}/{cfg.tipo_conta_guia}"
        )
        for tipo_consulta, status in CONSULTA_GUIA:
            filtro = urlencode(
                {
                    "tipoConsulta": tipo_consulta,
                    "segmentoConsulta": 99,
                    "dataInicial": dia,
                    "dataFinal": dia,
                    "idTransacao": id_transacao,
                }
            )
            try:
                dados = await self._ler(
                    self._api, "GET", f"{caminho}?{filtro}", "consultar_guia"
                )
            except Validacao as exc:
                if exc.status_http in (404, 412, 422):
                    continue
                raise
            # O manual mostra objeto; o OpenAPI, lista.
            envelope = dados[0] if isinstance(dados, list) and dados else dados
            registros = envelope.get("regSaida") if isinstance(envelope, dict) else None
            if not registros:
                continue
            item = registros[0]
            return PagamentoRegistrado(
                banco=self.banco,
                id_banco=id_banco,
                status=status,
                valor=_dinheiro(item.get("valorDebito")),
                data=_de_iso(item.get("dataPagamento"))
                or _de_iso(item.get("dataDebito")),
                autenticacao=str(item["autenticacaoBancaria"])
                if item.get("autenticacaoBancaria")
                else None,
                resposta_bruta=_sem_pii(item),
            )
        raise Validacao("bradesco: guia nao encontrada", status_http=404)

    async def _consultar_ted(self, id_banco: str) -> PagamentoRegistrado:
        documento, data_operacao = id_banco[:-10], id_banco[-10:]
        if not documento.isdigit() or not re.fullmatch(
            r"\d{2}\.\d{2}\.\d{4}", data_operacao
        ):
            raise Validacao("bradesco: id de TED invalido")
        filtro = urlencode(
            {"numeroDocumento": documento, "dataOperacao": data_operacao}
        )
        dados = await self._objeto(
            self._api, "GET", f"{ROTA_TED_CONSULTA}?{filtro}", "consultar_ted"
        )
        bruto = str(dados.get("statusMensagem") or "").strip().upper()
        status = STATUS_TED.get(" ".join(bruto.replace("_", " ").split()))
        if status is None:
            raise Validacao("bradesco: status de TED nao mapeado", resposta_bruta=dados)
        motivos = tuple(
            mascarar_texto(str(m))
            for m in (dados.get("codigoDaDevolucao"), dados.get("descricaoDaDevolucao"))
            if m not in (None, "", 0)
        )
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            valor=_dinheiro(dados.get("valorDaTransferencia")),
            motivos=motivos,
            autenticacao=dados.get("chaveUnicaTed") or None,
            resposta_bruta=_sem_pii(dados),
        )

    async def cancelar_pagamentos(
        self, conta_debito: ContaBancaria, ids_banco: tuple[str, ...]
    ) -> None:
        """Exclui agendamento de boleto. O manual so documenta exclusao para boleto;
        o id de Pix, guia e TED cai na validacao, antes de chamar o banco."""
        self.exigir_pagamento(CapacidadePagamento.CANCELAMENTO)
        self._exigir_conta(conta_debito)
        if not ids_banco:
            raise Validacao("nada para cancelar")
        for id_banco in ids_banco:
            if not (id_banco.isdigit() and len(id_banco) <= 9):
                raise Validacao("bradesco: so agendamento de boleto pode ser cancelado")
        for id_banco in ids_banco:
            await self._objeto(
                self._api,
                "POST",
                ROTA_BOLETO_CANCELAR,
                "cancelar_boleto",
                {
                    "versao": 2,
                    "bancoDebito": BANCO_BRADESCO,
                    "agenciaDebito": int(conta_debito.agencia),
                    "digitoAgencia": self._config.digito_agencia,
                    "contaDebito": int(conta_debito.conta),
                    "digitoConta": conta_debito.digito,
                    "numeroProtocolo": 0,
                    "idTransacao": int(id_banco),
                },
            )


__all__ = [
    "CAMINHO_TOKEN_PIX",
    "URL_PIX_PRODUCAO",
    "BradescoPagamentoAdapter",
    "BradescoPagamentoConfig",
]
