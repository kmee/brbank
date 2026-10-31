"""Banco Inter, API Banking v2: pagamento de Pix (chave, dados bancarios, copia e
cola) e de boleto e guia com codigo de barras (OAuth2 client_credentials + mTLS).

Sem modulo Odoo de origem. Contrato lido no OpenAPI oficial do portal
developers.inter.co (`redocusaurus/swagger-banking-yaml.yaml` e
`swagger-token-yaml.yaml`, lidos em 29/10/2026) e no changelog (Pix copia e cola
em 25/10/2023). Nada rodou contra o banco: `validado_sandbox = False`, porque o
sandbox exige integracao cadastrada e certificado (docs/inter-pagamento-duvidas.md).

Pontos do contrato:

- Paga um por vez (`LOTE_LOCAL`). O Inter tem lote proprio
  (`POST /pagamento/lote`, 2 a 150 boletos ou DARF, sem Pix), ainda nao usado.
- `POST /pix` com `destinatario.tipo` CHAVE, DADOS_BANCARIOS ou PIX_COPIA_E_COLA;
  `valor` e numero e e obrigatorio ate no copia e cola. Responde
  `codigoSolicitacao` (o id daqui) e `tipoRetorno` APROVACAO, PROCESSADO ou
  AGENDADO; o endToEnd so vem na consulta e no webhook.
- O Pix aceita `x-id-idempotente` (UUID). O adapter manda um UUID derivado da
  conta e da chave de idempotencia da linha: o mesmo pagamento reenviado leva a
  mesma chave. O que o Inter faz com a chave repetida nao esta documentado, entao
  as chamadas que movem dinheiro saem sem retry automatico mesmo assim.
- Boleto e guia em `POST /pagamento` (sem chave de idempotencia): `valorPagar`
  em texto, `dataVencimento` obrigatoria (de `Pagamento.vencimento` ou do fator
  de vencimento do codigo de barras).
- Aprovacao: conforme a conta, o pagamento fica aguardando aprovacao no Internet
  Banking (`ACEITO` aqui). Nao ha rota de aprovacao: `LIBERACAO` nao e declarada.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.auth import OAuth2ClientCredentials
from brbank.erros import ErroBrbank, Indisponivel, NaoSuportado, Validacao
from brbank.instrumento import vencimento_do_boleto
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
from brbank.tempo import hoje_brasil
from brbank.transporte import (
    SEM_RETRY,
    LimitadorTaxa,
    PoliticaRetry,
    Transporte,
    contexto_tls,
)

URL_PRODUCAO = "https://cdpj.partners.bancointer.com.br"
URL_SANDBOX = "https://cdpj-sandbox.partners.uatinter.co"
PREFIXO = "banking/v2"

ESCOPOS = {
    "pix_escrita": "pagamento-pix.write",
    "pix_consulta": "pagamento-pix.read",
    "boleto_escrita": "pagamento-boleto.write",
    "boleto_consulta": "pagamento-boleto.read",
    "webhook_escrita": "webhook-banking.write",
    "webhook_consulta": "webhook-banking.read",
}
TIPOS_WEBHOOK = ("pix-pagamento", "boleto-pagamento")
_NAMESPACE_IDEMPOTENCIA = uuid.UUID("6f1c9a52-6d1e-4b0e-9a57-2f1f3b8e0c11")

TIPO_RETORNO_PIX = {
    "APROVACAO": StatusPagamento.ACEITO,
    "PROCESSADO": StatusPagamento.EM_PROCESSAMENTO,
    "AGENDADO": StatusPagamento.AGENDADO,
}
# Status do Pix na consulta (lista da descricao do OpenAPI, sem enum formal) e os
# nomes antigos que o SDK oficial ainda conhece. PARCIALMENTE_* ficam fora de
# proposito: nao ha como dizer o que foi pago.
STATUS_PIX = {
    "CRIADO": StatusPagamento.EM_PROCESSAMENTO,
    "AGUARDANDO_APROVACAO": StatusPagamento.ACEITO,
    "APROVADO": StatusPagamento.EM_PROCESSAMENTO,
    "REPROVADO": StatusPagamento.CANCELADO,
    "EXPIRADO": StatusPagamento.CANCELADO,
    "CANCELADO": StatusPagamento.CANCELADO,
    "AGENDAMENTO_CANCELADO": StatusPagamento.CANCELADO,
    "FALHA": StatusPagamento.REJEITADO,
    "CANCELADO_SEM_SALDO": StatusPagamento.REJEITADO,
    "NAO_DEBITADO": StatusPagamento.REJEITADO,
    "AGENDADO": StatusPagamento.AGENDADO,
    "DEBITADO": StatusPagamento.EM_PROCESSAMENTO,
    "ENVIADO": StatusPagamento.EM_PROCESSAMENTO,
    "PAGO": StatusPagamento.PAGO,
    "TRANSACAO_CRIADA": StatusPagamento.EM_PROCESSAMENTO,
    "TRANSACAO_APROVADA": StatusPagamento.EM_PROCESSAMENTO,
    "PIX_ENVIADO": StatusPagamento.EM_PROCESSAMENTO,
    "PIX_PAGO": StatusPagamento.PAGO,
}
# SituacaoPagamento (boleto e guia).
STATUS_BOLETO = {
    "EMPROCESSAMENTO": StatusPagamento.EM_PROCESSAMENTO,
    "AGUARDANDO_APROVACAO": StatusPagamento.ACEITO,
    "APROVADO": StatusPagamento.EM_PROCESSAMENTO,
    "AGENDADO": StatusPagamento.AGENDADO,
    "REALIZADO": StatusPagamento.PAGO,
    "CANCELADO": StatusPagamento.CANCELADO,
    "AGENDADO_CANCELADO": StatusPagamento.CANCELADO,
    "REPROVADO": StatusPagamento.CANCELADO,
    "APROVACAO_EXPIRADA": StatusPagamento.CANCELADO,
    "ERRO": StatusPagamento.REJEITADO,
    "NAO_COMPENSADO": StatusPagamento.REJEITADO,
}
# Callback pix-pagamento. PROCESSADO aparece no exemplo do proprio OpenAPI,
# fora do enum (EFETIVADO, ERRO, REPROVADO, APROVACAO_EXPIRADA, CANCELADO).
STATUS_WEBHOOK_PIX = {
    "EFETIVADO": StatusPagamento.PAGO,
    "PROCESSADO": StatusPagamento.EM_PROCESSAMENTO,
    "ERRO": StatusPagamento.REJEITADO,
    "REPROVADO": StatusPagamento.CANCELADO,
    "APROVACAO_EXPIRADA": StatusPagamento.CANCELADO,
    "CANCELADO": StatusPagamento.CANCELADO,
}
STATUS_WEBHOOK_BOLETO = {
    "REALIZADO": StatusPagamento.PAGO,
    "AGENDADO": StatusPagamento.AGENDADO,
    "CANCELADO": StatusPagamento.CANCELADO,
    "ERRO": StatusPagamento.REJEITADO,
    "REPROVADO": StatusPagamento.CANCELADO,
    "APROVACAO_EXPIRADA": StatusPagamento.CANCELADO,
}

MOTIVO_NAO_ENVIADO = "NAO_ENVIADO"
MOTIVO_SEM_RESPOSTA = "SEM_RESPOSTA"
_PIX = (TipoPagamento.PIX, TipoPagamento.PIX_QRCODE)


@dataclass
class InterPagamentoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # `x-conta-corrente`: so quando a integracao tem mais de uma conta.
    conta_corrente: str | None = None
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # POST /pix: 60 por minuto, no maximo 1 por segundo.
    requisicoes_por_segundo: float | None = 1.0

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Inter invalido: {self.ambiente!r}")
        if self.conta_corrente is not None and not (
            self.conta_corrente.isdigit() and not self.conta_corrente.startswith("0")
        ):
            raise Validacao("conta_corrente: so digitos com o DV, sem zero a esquerda")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


class _Incerto(Exception):
    """Pagamento enviado sem resposta legivel: pode ter sido executado."""

    def __init__(self, id_banco: str | None = None) -> None:
        super().__init__("pagamento sem resposta")
        self.id_banco = id_banco


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Inter") from exc


def _data(valor: Any) -> date | None:
    if not valor or not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _motivos_do_erro(erro: ErroBrbank) -> tuple[str, ...]:
    """Motivos de um Problema do Inter: titulo, detalhe e cada violacao."""
    bruto = erro.resposta_bruta
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto)
        except ValueError:
            bruto = None
    motivos: list[str] = []
    if isinstance(bruto, dict):
        for chave in ("title", "detail"):
            if bruto.get(chave):
                motivos.append(str(bruto[chave]))
        for violacao in bruto.get("violacoes") or []:
            if isinstance(violacao, dict) and violacao.get("razao"):
                motivos.append(str(violacao["razao"]))
    return tuple(motivos) or (erro.mensagem,)


def _motivos_da_lista(erros: Any) -> tuple[str, ...]:
    if not isinstance(erros, list):
        return ()
    return tuple(
        str(e.get("codigoErro") or e.get("descricaoErro"))
        for e in erros
        if isinstance(e, dict) and (e.get("codigoErro") or e.get("descricaoErro"))
    )


class InterPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "inter"
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.PIX
        | CapacidadePagamento.PIX_QRCODE
        | CapacidadePagamento.BOLETO
        | CapacidadePagamento.GUIA
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LOTE_LOCAL
        | CapacidadePagamento.CANCELAMENTO
        | CapacidadePagamento.WEBHOOK
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: InterPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = hoje_brasil,
    ) -> None:
        self._config = config
        self._hoje = hoje
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None:
            raise Validacao("Inter exige certificado mTLS (tambem no sandbox)")
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=f"{config.url_base}/oauth/v2/token",
            client_id=config.client_id,
            client_secret=config.client_secret,
            escopos=ESCOPOS,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=f"{config.url_base}/{PREFIXO}",
            client=client,
            ssl_context=ssl_context,
            autenticador=auth,
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # -- chamadas ------------------------------------------------------------

    async def _chamar(
        self,
        metodo: str,
        caminho: str,
        operacao: str,
        escopo: str,
        json: Any = None,
        *,
        cabecalhos: Mapping[str, str] | None = None,
        sem_retry: bool = False,
    ) -> Any:
        extra = dict(cabecalhos or {})
        if self._config.conta_corrente:
            extra["x-conta-corrente"] = self._config.conta_corrente
        resp = await self._transporte.requisitar(
            metodo,
            caminho,
            operacao=operacao,
            escopo=escopo,
            json=json,
            cabecalhos=extra,
            retry=SEM_RETRY if sem_retry else None,
        )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Inter nao e JSON", status_http=resp.status_code
            ) from exc

    async def _objeto(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        dados = await self._chamar(*args, **kwargs)
        if not isinstance(dados, dict):
            raise Validacao("resposta do Inter fora do formato")
        return dados

    def chave_idempotente(self, pagamento: Pagamento) -> str:
        """`x-id-idempotente` do Pix: estavel para a mesma conta e linha."""
        nome = f"{self._config.conta}:{pagamento.chave_idempotencia}"
        return str(uuid.uuid5(_NAMESPACE_IDEMPOTENCIA, nome))

    # -- montagem --------------------------------------------------------------

    def _data_pagamento(self, dia: date) -> str:
        if dia < self._hoje():
            raise Validacao("inter: data de pagamento no passado")
        return dia.isoformat()

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

    @staticmethod
    def _dados_bancarios(destino: DestinoPix) -> dict[str, Any]:
        conta = destino.conta
        assert conta is not None
        if not conta.ispb:
            raise Validacao("inter: Pix por dados bancarios exige o ISPB do destino")
        if not destino.documento or not destino.nome:
            raise Validacao("inter: Pix por dados bancarios exige documento e nome")
        return {
            "tipo": "DADOS_BANCARIOS",
            "contaCorrente": conta.conta + conta.digito.upper(),
            "tipoConta": "CONTA_POUPANCA" if conta.poupanca else "CONTA_CORRENTE",
            "cpfCnpj": destino.documento,
            "agencia": conta.agencia,
            "nome": destino.nome,
            "instituicaoFinanceira": {"ispb": conta.ispb},
        }

    def _corpo_pix(self, p: Pagamento) -> dict[str, Any]:
        if p.tipo is TipoPagamento.PIX_QRCODE:
            destinatario: dict[str, Any] = {
                "tipo": "PIX_COPIA_E_COLA",
                "pixCopiaECola": p.brcode,
            }
        else:
            assert p.pix is not None
            if p.pix.chave is not None:
                destinatario = {"tipo": "CHAVE", "chave": self._chave(p.pix)}
            else:
                destinatario = self._dados_bancarios(p.pix)
        corpo: dict[str, Any] = {
            # A API exige numero JSON; a conversao fica restrita a fronteira.
            "valor": float(p.valor),
            "dataPagamento": self._data_pagamento(p.data),
            "destinatario": destinatario,
        }
        if p.descricao:
            corpo["descricao"] = p.descricao
        return corpo

    def _corpo_boleto(self, p: Pagamento) -> dict[str, Any]:
        assert p.codigo_barras is not None
        vencimento = p.vencimento or vencimento_do_boleto(
            p.codigo_barras, hoje=self._hoje()
        )
        if vencimento is None:
            raise Validacao("inter: pagamento sem vencimento (informe vencimento)")
        corpo: dict[str, Any] = {
            "codBarraLinhaDigitavel": p.codigo_barras,
            "valorPagar": f"{p.valor:.2f}",
            "dataPagamento": self._data_pagamento(p.data),
            "dataVencimento": vencimento.isoformat(),
        }
        documento = p.beneficiario_documento
        # O OpenAPI so aceita CPF ou CNPJ numericos aqui.
        if documento and documento.isdigit() and len(documento) in (11, 14):
            corpo["cpfCnpjBeneficiario"] = documento
        return corpo

    # -- leitura ---------------------------------------------------------------

    def _pix_incluido(self, dados: dict[str, Any], p: Pagamento) -> PagamentoRegistrado:
        codigo = dados.get("codigoSolicitacao")
        if not codigo:
            # Incluiu (200) sem identificador: nao da para dizer que nao pagou.
            raise _Incerto
        bruto = str(dados.get("tipoRetorno") or "")
        if bruto not in TIPO_RETORNO_PIX:
            raise Validacao(
                f"inter: tipoRetorno nao mapeado {bruto!r}", resposta_bruta=dados
            )
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(codigo),
            status=TIPO_RETORNO_PIX[bruto],
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=_data(dados.get("dataPagamento")) or p.data,
            resposta_bruta=dados,
        )

    def _boleto_incluido(
        self, dados: dict[str, Any], p: Pagamento
    ) -> PagamentoRegistrado:
        codigo = dados.get("codigoTransacao")
        if not codigo:
            raise _Incerto
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(codigo),
            status=self._traduzir(STATUS_BOLETO, dados.get("statusPagamento"), dados),
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=_data(dados.get("dataAgendamento")) or p.data,
            resposta_bruta=dados,
        )

    @staticmethod
    def _traduzir(
        tabela: Mapping[str, StatusPagamento], bruto: Any, dados: Any
    ) -> StatusPagamento:
        texto = str(bruto or "").upper()
        if texto not in tabela:
            raise Validacao(
                f"inter: status de pagamento nao mapeado {bruto!r}",
                resposta_bruta=dados,
            )
        return tabela[texto]

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

    # -- operacoes -------------------------------------------------------------

    async def _pagar(self, p: Pagamento) -> PagamentoRegistrado:
        if p.tipo in _PIX:
            corpo = self._corpo_pix(p)
            caminho, escopo = "pix", "pix_escrita"
            cabecalhos = {"x-id-idempotente": self.chave_idempotente(p)}
        else:
            corpo = self._corpo_boleto(p)
            caminho, escopo, cabecalhos = "pagamento", "boleto_escrita", {}
        try:
            dados = await self._objeto(
                "POST",
                caminho,
                "pagar",
                escopo,
                corpo,
                cabecalhos=cabecalhos,
                sem_retry=True,
            )
        except Indisponivel as exc:
            raise _Incerto from exc
        # 2xx: o banco aceitou e pode ter pago. Resposta ilegivel vira
        # INDETERMINADO, nunca RECUSADO.
        try:
            if p.tipo in _PIX:
                return self._pix_incluido(dados, p)
            return self._boleto_incluido(dados, p)
        except Validacao as exc:
            codigo = dados.get("codigoSolicitacao") or dados.get("codigoTransacao")
            raise _Incerto(str(codigo) if codigo else None) from exc

    async def reservar_numero_lote(self) -> str:
        """Numero local: os pagamentos vao um a um."""
        return uuid.uuid4().hex

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Envia linha a linha. Recusa de uma linha nao para as outras; linha sem
        resposta (INDETERMINADO) para o envio e as seguintes voltam RECUSADO com
        motivo NAO_ENVIADO."""
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        numero = lote.numero or await self.reservar_numero_lote()
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for p in lote.pagamentos:
            if parou:
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO,)))
                continue
            try:
                linhas.append(await self._pagar(p))
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
            except ErroBrbank as exc:
                linhas.append(self._recusado(p, _motivos_do_erro(exc)))
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
        raise NaoSuportado(
            "inter: o lote e local (LOTE_LOCAL); consulte cada pagamento"
        )

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        if not id_banco:
            raise Validacao("inter: consulta exige o codigo do pagamento")
        if tipo in _PIX:
            dados = await self._objeto(
                "GET", f"pix/{id_banco}", "consultar_pagamento", "pix_consulta"
            )
            transacao = dados.get("transacaoPix") or {}
            if not isinstance(transacao, dict):
                raise Validacao("resposta do Inter sem transacaoPix")
            return PagamentoRegistrado(
                banco=self.banco,
                id_banco=str(transacao.get("codigoSolicitacao") or id_banco),
                status=self._traduzir(STATUS_PIX, transacao.get("status"), dados),
                valor=_dinheiro(transacao.get("valor")),
                data=_data(transacao.get("dataHoraMovimento")),
                motivos=_motivos_da_lista(transacao.get("erros")),
                autenticacao=transacao.get("endToEnd") or None,
                resposta_bruta=dados,
            )
        consulta = urlencode({"codigoTransacao": id_banco})
        itens = await self._chamar(
            "GET", f"pagamento?{consulta}", "consultar_pagamento", "boleto_consulta"
        )
        if not isinstance(itens, list) or not itens:
            raise Validacao("inter: pagamento nao encontrado", status_http=404)
        item = itens[0]
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(item.get("codigoTransacao") or id_banco),
            status=self._traduzir(STATUS_BOLETO, item.get("statusPagamento"), item),
            valor=_dinheiro(item.get("valorPago")),
            data=_data(item.get("dataPagamento")),
            autenticacao=item.get("autenticacao") or None,
            resposta_bruta=item,
        )

    async def cancelar_pagamentos(
        self, conta_debito: ContaBancaria, ids_banco: tuple[str, ...]
    ) -> None:
        """Cancela boleto ou guia agendados (`DELETE /pagamento/{codigoTransacao}`).
        O Pix agendado nao tem rota de cancelamento."""
        if not ids_banco:
            raise Validacao("nada para cancelar")
        for id_banco in ids_banco:
            await self._chamar(
                "DELETE", f"pagamento/{id_banco}", "cancelar", "boleto_escrita"
            )

    # -- webhook ---------------------------------------------------------------

    @staticmethod
    def _tipo_webhook(tipo: str) -> str:
        if tipo not in TIPOS_WEBHOOK:
            raise Validacao(f"tipo de webhook do Inter deve ser um de {TIPOS_WEBHOOK}")
        return tipo

    async def configurar_webhook_pagamento(
        self, url: str, tipo: str = "pix-pagamento"
    ) -> None:
        if not url.startswith("https://"):
            raise Validacao("webhook do Inter exige URL https")
        await self._chamar(
            "PUT",
            f"webhooks/{self._tipo_webhook(tipo)}",
            "webhook",
            "webhook_escrita",
            {"webhookUrl": url},
        )

    async def consultar_webhook_pagamento(
        self, tipo: str = "pix-pagamento"
    ) -> str | None:
        try:
            dados = await self._objeto(
                "GET",
                f"webhooks/{self._tipo_webhook(tipo)}",
                "webhook",
                "webhook_consulta",
            )
        except Validacao as exc:
            if exc.status_http == 404:
                return None
            raise
        return dados.get("webhookUrl") or None

    async def remover_webhook_pagamento(self, tipo: str = "pix-pagamento") -> None:
        await self._chamar(
            "DELETE",
            f"webhooks/{self._tipo_webhook(tipo)}",
            "webhook",
            "webhook_escrita",
        )

    def parse_webhook_pagamento(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[PagamentoRegistrado]:
        """Callback do Inter: lista JSON de pix-pagamento ou boleto-pagamento.

        Sem assinatura no corpo: a autenticidade fica com o mTLS (o Inter manda o
        `ca.crt` para o recebedor exigir o certificado dele).
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Inter nao e JSON") from exc
        itens = [corpo] if isinstance(corpo, dict) else corpo
        if not isinstance(itens, list):
            raise Validacao("webhook do Inter fora do formato")
        eventos = []
        for item in itens:
            if not isinstance(item, dict):
                raise Validacao("webhook do Inter fora do formato")
            if item.get("codigoSolicitacao"):
                id_banco = item["codigoSolicitacao"]
                status = self._traduzir(STATUS_WEBHOOK_PIX, item.get("status"), item)
                valor, autenticacao = item.get("valor"), item.get("endToEnd")
            elif item.get("codigoTransacao"):
                id_banco = item["codigoTransacao"]
                status = self._traduzir(STATUS_WEBHOOK_BOLETO, item.get("status"), item)
                valor = item.get("valorPago") or item.get("valorAgendado")
                autenticacao = item.get("nsu")
            else:
                raise Validacao("webhook do Inter sem codigo", resposta_bruta=item)
            eventos.append(
                PagamentoRegistrado(
                    banco=self.banco,
                    id_banco=str(id_banco),
                    status=status,
                    valor=_dinheiro(valor),
                    data=_data(item.get("dataPagamento"))
                    or _data(item.get("dataHoraMovimento")),
                    motivos=_motivos_da_lista(item.get("erros")),
                    autenticacao=str(autenticacao) if autenticacao else None,
                    resposta_bruta=item,
                )
            )
        return eventos


__all__ = [
    "ESCOPOS",
    "URL_PRODUCAO",
    "URL_SANDBOX",
    "InterPagamentoAdapter",
    "InterPagamentoConfig",
]
