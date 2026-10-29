"""Sicoob (756), API Pagamentos Pix v2 (OAuth2 client_credentials + mTLS).

Sem modulo Odoo de origem. Contrato lido no swagger oficial 2.0.24.14 do portal
developers.sicoob.com.br (API "Pix Pagamentos", 29/10/2026) e exercitado no
sandbox publico no mesmo dia (docs/sicoob-pagamento-duvidas.md).

ESTADO: `validado_sandbox = False`. O sandbox respondeu ao fluxo por chave
(iniciar, confirmar, consultar) e ao webhook, mas a rota do QR devolve sempre o
exemplo de erro 400 do swagger, com qualquer corpo: o pagamento por copia e cola,
que e o motivo deste adapter, segue sem prova contra o banco.

Pontos do contrato:

- Paga um por vez (`LOTE_LOCAL`): nao ha lote no banco. `pagar_lote` envia linha
  a linha e o numero do lote e gerado aqui; `consultar_lote` nao existe.
- Pix copia e cola (`PIX_QRCODE`): `POST /pagamentos/qrcode` executa direto, sem
  confirmacao. O banco le o QR (estatico ou dinamico, imediato ou com
  vencimento), valida o JWS do QR dinamico e confere valor original, encargos
  autorizados e CNPJ do recebedor. Valor em texto com ponto ("10.00").
- Pix por chave (`PIX`): `POST /pagamentos` consulta o DICT e devolve o
  endToEndId e o dono da chave; `POST /pagamentos/confirmacao` paga. Valor em
  texto com VIRGULA ("1,99"): o sandbox recusa o ponto com 400. Pix por dados
  bancarios usa so a confirmacao (`meioIniciacao` MANUAL).
- Nenhuma das rotas tem chave de idempotencia. As chamadas que movem dinheiro
  saem sem retry automatico; sem resposta, a linha volta INDETERMINADO. A flag
  `repeticao` vai sempre falsa (o banco recusa pagamento repetido; o criterio
  dele nao esta documentado).
- `origem` (conta de debito com ISPB, CPF/CNPJ e nome do titular) vai sempre: o
  swagger diz que e opcional na confirmacao, mas o exemplo de erro do proprio
  banco diz que precisa ser preenchida.
"""

import asyncio
import json
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from brbank.adapters.sicoob import URL_TOKEN, _ClientCredentialsSemSegredo, _TokenFixo
from brbank.auth import Autenticador
from brbank.erros import ErroBrbank, Indisponivel, NaoSuportado, Validacao
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

URL_PRODUCAO = "https://api.sicoob.com.br/pix-pagamentos/v2"
URL_SANDBOX = "https://sandbox.sicoob.com.br/sicoob/sandbox/pix-pagamentos/v2"
# ISPB do Banco Sicoob. Cooperativa com ISPB proprio no Pix informa o dela.
ISPB_SICOOB = "02038232"

ESCOPOS_PADRAO = {
    "escrita": "pixpagamentos_escrita",
    "consulta": "pixpagamentos_consulta",
    "webhook": "pixpagamentos_webhook",
}

# `estado` do RetornoPagamento, normalizado sem acento.
ESTADOS = {
    "EM_PROCESSAMENTO": StatusPagamento.EM_PROCESSAMENTO,
    "FINALIZADO": StatusPagamento.PAGO,
    "REJEITADO": StatusPagamento.REJEITADO,
    "NAO_REALIZADO": StatusPagamento.REJEITADO,
}
# `estado` (ou `status`, como no exemplo do swagger) do callback.
ESTADOS_WEBHOOK = {
    "FINALIZADO_SUCESSO": StatusPagamento.PAGO,
    "FINALIZADO_REJEICAO": StatusPagamento.REJEITADO,
}
TIPOS_CONTA = ("CORRENTE", "POUPANCA", "SALARIO", "CONTAPAGAMENTO")

MOTIVO_NAO_ENVIADO = "NAO_ENVIADO"
MOTIVO_SEM_RESPOSTA = "SEM_RESPOSTA"
MOTIVO_DOCUMENTO_DIVERGENTE = "DOCUMENTO_DIVERGENTE"


@dataclass
class SicoobPagamentoConfig:
    conta: str
    client_id: str
    # Titular da conta de debito (vai em `origem`).
    pagador_documento: str = field(repr=False)
    pagador_nome: str = field(repr=False)
    ambiente: str = "producao"
    ispb: str = ISPB_SICOOB
    tipo_conta: str = "CORRENTE"
    # O numero da conta vai com o DV colado (ex.: 12345-6 -> "123456").
    conta_com_digito: bool = True
    # Municipio IBGE do pagador (7 digitos): o banco usa para feriado no QR com
    # vencimento. Opcional.
    codigo_municipio: str | None = None
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    escopos: Mapping[str, str] = field(default_factory=lambda: dict(ESCOPOS_PADRAO))
    # Token fixo do sandbox publicado pelo Sicoob no portal; nao vai no codigo.
    token_sandbox: str | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = 2.0

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Sicoob invalido: {self.ambiente!r}")
        if not (len(self.ispb) == 8 and self.ispb.isalnum()):
            raise Validacao("ISPB deve ter 8 posicoes")
        if self.tipo_conta not in TIPOS_CONTA:
            raise Validacao(f"tipo_conta deve ser um de {TIPOS_CONTA}")
        if self.codigo_municipio is not None and not (
            len(self.codigo_municipio) == 7 and self.codigo_municipio.isdigit()
        ):
            raise Validacao("codigo_municipio e o codigo IBGE de 7 digitos")
        documento = "".join(c for c in self.pagador_documento if c.isalnum()).upper()
        if len(documento) not in (11, 14):
            raise Validacao("pagador_documento deve ser CPF (11) ou CNPJ (14)")
        self.pagador_documento = documento


class _Incerto(Exception):
    """Pagamento enviado sem resposta: pode ter sido executado."""

    def __init__(self, e2e: str | None) -> None:
        super().__init__("pagamento sem resposta")
        self.e2e = e2e


def _valor_ponto(valor: Decimal) -> str:
    return f"{valor:.2f}"


def _valor_virgula(valor: Decimal) -> str:
    return f"{valor:.2f}".replace(".", ",")


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Sicoob") from exc


def _normalizar_estado(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return "_".join(sem_acento.upper().replace("_", " ").split())


def _data(valor: Any) -> date | None:
    if not valor or not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _motivos_do_erro(erro: ErroBrbank) -> tuple[str, ...]:
    """Motivos de um Problema (RFC 7807): tipo e razao de cada violacao."""
    bruto = erro.resposta_bruta
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto)
        except ValueError:
            bruto = None
    motivos: list[str] = []
    if isinstance(bruto, dict):
        tipo = str(bruto.get("type") or "").rstrip("/").rsplit("/", 1)[-1]
        if tipo:
            motivos.append(tipo)
        for violacao in bruto.get("violacoes") or []:
            if isinstance(violacao, dict) and violacao.get("razao"):
                motivos.append(str(violacao["razao"]))
        if not motivos and bruto.get("detail"):
            motivos.append(str(bruto["detail"]))
    return tuple(motivos) or (erro.mensagem,)


class SicoobPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "sicoob"
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.PIX
        | CapacidadePagamento.PIX_QRCODE
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LOTE_LOCAL
        | CapacidadePagamento.WEBHOOK
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: SicoobPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        sandbox = config.ambiente == "sandbox"
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None and not sandbox:
            raise Validacao("Sicoob pagamentos exige certificado mTLS em producao")
        auth: Autenticador
        if sandbox and config.token_sandbox:
            auth = _TokenFixo(config.token_sandbox)
        else:
            auth = _ClientCredentialsSemSegredo(
                conta=config.conta,
                url_token=URL_TOKEN,
                client_id=config.client_id,
                client_secret="",
                escopos=config.escopos,
            )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=URL_SANDBOX if sandbox else URL_PRODUCAO,
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
        sem_retry: bool = False,
        aceitar: str = "application/json",
    ) -> dict[str, Any]:
        resp = await self._transporte.requisitar(
            metodo,
            caminho,
            operacao=operacao,
            escopo=escopo,
            json=json,
            cabecalhos={"client_id": self._config.client_id, "Accept": aceitar},
            retry=SEM_RETRY if sem_retry else None,
        )
        if not resp.content:
            return {}
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Sicoob nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Sicoob fora do formato", status_http=resp.status_code
            )
        return dados

    # -- montagem --------------------------------------------------------------

    def _origem(self, conta: ContaBancaria) -> dict[str, str]:
        cfg = self._config
        if len(conta.agencia) > 4:
            raise Validacao("sicoob: agencia (cooperativa) tem ate 4 digitos")
        numero = conta.conta + conta.digito if cfg.conta_com_digito else conta.conta
        return {
            "ispb": conta.ispb or cfg.ispb,
            "cpfCnpj": cfg.pagador_documento,
            "nome": cfg.pagador_nome[:140],
            "conta": numero,
            "agencia": conta.agencia,
            "tipo": "POUPANCA" if conta.poupanca else cfg.tipo_conta,
        }

    def _agendamento(self, dia: date) -> dict[str, str]:
        hoje = self._hoje()
        if dia < hoje:
            raise Validacao("sicoob: data de pagamento no passado")
        return {"dataAgendamento": dia.isoformat()} if dia > hoje else {}

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
    def _destino_manual(destino: DestinoPix) -> dict[str, Any]:
        conta = destino.conta
        assert conta is not None
        if not conta.ispb:
            raise Validacao("sicoob: Pix por dados bancarios exige o ISPB do destino")
        if not destino.documento:
            raise Validacao("Pix por dados bancarios exige o documento do favorecido")
        if len(conta.agencia) > 4:
            raise Validacao("sicoob: agencia de destino tem ate 4 digitos")
        dados = {
            "ispb": conta.ispb,
            "cpfCnpj": destino.documento,
            "conta": conta.conta + conta.digito.upper(),
            "agencia": conta.agencia,
            "tipo": "POUPANCA" if conta.poupanca else "CORRENTE",
        }
        if destino.nome:
            dados["nome"] = destino.nome
        return dados

    # -- leitura ---------------------------------------------------------------

    def _registrado(
        self, dados: dict[str, Any], chave: str | None, id_banco: str | None = None
    ) -> PagamentoRegistrado:
        e2e = dados.get("endToEndId") or id_banco
        if not e2e:
            raise Validacao("resposta do Sicoob sem endToEndId", resposta_bruta=dados)
        bruto = str(dados.get("estado") or "")
        estado = _normalizar_estado(bruto)
        if estado not in ESTADOS:
            raise Validacao(
                f"sicoob: estado de pagamento nao mapeado {bruto!r}",
                resposta_bruta=dados,
            )
        status = ESTADOS[estado]
        agendado = _data(dados.get("dataAgendamento"))
        if (
            status is StatusPagamento.EM_PROCESSAMENTO
            and agendado
            and agendado > self._hoje()
        ):
            status = StatusPagamento.AGENDADO
        detalhe = dados.get("detalheRejeicao")
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(e2e),
            status=status,
            chave_idempotencia=chave,
            valor=_dinheiro(dados.get("valor")),
            data=agendado or _data(dados.get("horario")),
            motivos=(str(detalhe),)
            if detalhe and status is not StatusPagamento.PAGO
            else (),
            resposta_bruta=dados,
        )

    def _recusado(
        self, p: Pagamento, motivos: tuple[str, ...], e2e: str | None = None
    ) -> PagamentoRegistrado:
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=e2e or "",
            status=StatusPagamento.RECUSADO,
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=p.data,
            motivos=motivos,
        )

    # -- pagamento de uma linha ----------------------------------------------

    async def _efetivar(self, caminho: str, corpo: dict[str, Any], e2e: str | None):
        try:
            return await self._chamar(
                "POST", caminho, "pagar", "escrita", corpo, sem_retry=True
            )
        except Indisponivel as exc:
            raise _Incerto(e2e) from exc

    async def _pagar_qrcode(
        self, p: Pagamento, origem: dict[str, str]
    ) -> PagamentoRegistrado:
        assert p.brcode is not None
        corpo: dict[str, Any] = {
            "qrcode": p.brcode,
            "valor": _valor_ponto(p.valor),
            "origem": origem,
            "pagarComJurosMulta": p.pagar_com_juros_multa,
            "repeticao": False,
            **self._agendamento(p.data),
        }
        if self._config.codigo_municipio:
            corpo["codMun"] = self._config.codigo_municipio
        if p.vencimento:
            corpo["dataVencimento"] = p.vencimento.isoformat()
        dados = await self._efetivar("pagamentos/qrcode", corpo, None)
        return self._lido(dados, p, None)

    async def _pagar_pix(
        self, p: Pagamento, origem: dict[str, str]
    ) -> PagamentoRegistrado:
        destino = p.pix
        assert destino is not None
        agenda = self._agendamento(p.data)
        corpo: dict[str, Any] = {
            "valor": _valor_virgula(p.valor),
            "repeticao": False,
            "origem": origem,
            **agenda,
        }
        if p.descricao:
            corpo["descricao"] = p.descricao
        e2e = None
        if destino.chave is not None:
            iniciado = await self._chamar(
                "POST",
                "pagamentos",
                "iniciar",
                "escrita",
                {"chave": self._chave(destino), **agenda},
            )
            e2e = iniciado.get("endToEndId")
            if not e2e:
                raise Validacao("sicoob: chave Pix sem endToEndId na iniciacao")
            dono = (iniciado.get("proprietario") or {}).get("identificador") or ""
            dono = "".join(c for c in str(dono) if c.isalnum()).upper()
            if destino.documento and dono != destino.documento:
                # Chave de outra pessoa: nao confirma.
                return self._recusado(p, (MOTIVO_DOCUMENTO_DIVERGENTE,), e2e)
            corpo.update(endToEndId=e2e, meioIniciacao="CHAVE")
        else:
            corpo.update(meioIniciacao="MANUAL", destino=self._destino_manual(destino))
        dados = await self._efetivar("pagamentos/confirmacao", corpo, e2e)
        return self._lido(dados, p, e2e)

    def _lido(
        self, dados: dict[str, Any], p: Pagamento, e2e: str | None
    ) -> PagamentoRegistrado:
        """Resposta 2xx de um pagamento: o banco aceitou e pode ter pago. Resposta
        sem e2e ou ilegivel nunca vira RECUSADO, vira INDETERMINADO."""
        if not dados.get("endToEndId"):
            raise _Incerto(e2e)
        try:
            return self._registrado(dados, p.chave_idempotencia, e2e)
        except Validacao as exc:
            raise _Incerto(str(dados["endToEndId"])) from exc

    # -- operacoes -------------------------------------------------------------

    async def reservar_numero_lote(self) -> str:
        """Numero local: o Sicoob nao tem lote."""
        return uuid.uuid4().hex

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Envia linha a linha. Recusa de uma linha nao para as outras; linha sem
        resposta (INDETERMINADO) para o envio e as seguintes voltam RECUSADO com
        motivo NAO_ENVIADO."""
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        numero = lote.numero or await self.reservar_numero_lote()
        origem = self._origem(lote.conta_debito)
        pagar = (
            self._pagar_qrcode
            if lote.tipo is TipoPagamento.PIX_QRCODE
            else self._pagar_pix
        )
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for p in lote.pagamentos:
            if parou:
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO,)))
                continue
            try:
                linhas.append(await pagar(p, origem))
            except _Incerto as exc:
                parou = True
                linhas.append(
                    PagamentoRegistrado(
                        banco=self.banco,
                        id_banco=exc.e2e or "",
                        status=StatusPagamento.INDETERMINADO,
                        chave_idempotencia=p.chave_idempotencia,
                        valor=p.valor,
                        data=p.data,
                        motivos=(MOTIVO_SEM_RESPOSTA,),
                    )
                )
            except Indisponivel as exc:
                # Antes de mover dinheiro (iniciacao da chave): nada foi pago.
                parou = True
                linhas.append(
                    self._recusado(p, (MOTIVO_NAO_ENVIADO, *_motivos_do_erro(exc)))
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
            "sicoob: o lote e local (LOTE_LOCAL); consulte cada pagamento pelo "
            "endToEndId"
        )

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        if not id_banco:
            raise Validacao("sicoob: consulta exige o endToEndId")
        dados = await self._chamar(
            "GET", f"pagamentos/{id_banco}", "consultar_pagamento", "consulta"
        )
        return self._registrado(dados, None, id_banco)

    # -- webhook ---------------------------------------------------------------

    async def configurar_webhook_pagamento(self, url: str) -> None:
        if not url.startswith("https://"):
            raise Validacao("webhook do Sicoob exige URL https")
        # O PUT responde 200 sem corpo; com `Accept: application/json` o firewall
        # do Sicoob troca a resposta por uma pagina HTML "Request Rejected" (200),
        # que aqui vira Validacao em vez de sucesso falso.
        await self._chamar(
            "PUT",
            "pagamentos/webhook",
            "webhook",
            "webhook",
            {"webhookUrl": url},
            aceitar="*/*",
        )

    async def consultar_webhook_pagamento(self) -> str | None:
        try:
            dados = await self._chamar(
                "GET", "pagamentos/webhook", "webhook", "webhook"
            )
        except Validacao as exc:
            if exc.status_http == 404:
                return None
            raise
        return dados.get("webhookUrl") or None

    async def remover_webhook_pagamento(self) -> None:
        await self._chamar("DELETE", "pagamentos/webhook", "webhook", "webhook")

    def parse_webhook_pagamento(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[PagamentoRegistrado]:
        """Callback `{webhookUrl}/lancamento`: `{"payload": {...}}` (ou lista).

        O Sicoob nao assina o corpo: a autenticidade fica com o mTLS e com a URL
        nao publica. Quem recebe deve reconsultar antes de dar baixa.
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Sicoob nao e JSON") from exc
        itens: Any = corpo.get("payload", corpo) if isinstance(corpo, dict) else corpo
        if isinstance(itens, dict):
            itens = [itens]
        if not isinstance(itens, list):
            raise Validacao("webhook do Sicoob fora do formato")
        eventos = []
        for item in itens:
            if not isinstance(item, dict) or not item.get("id"):
                raise Validacao("webhook do Sicoob sem id", resposta_bruta=item)
            bruto = str(item.get("estado") or item.get("status") or "")
            status = ESTADOS_WEBHOOK.get(_normalizar_estado(bruto))
            if status is None:
                raise Validacao(
                    f"sicoob: estado de webhook nao mapeado {bruto!r}",
                    resposta_bruta=item,
                )
            detalhe = item.get("detalheRejeicao")
            eventos.append(
                PagamentoRegistrado(
                    banco=self.banco,
                    id_banco=str(item["id"]),
                    status=status,
                    valor=_dinheiro(item.get("valor")),
                    motivos=(str(detalhe),) if detalhe else (),
                    resposta_bruta=item,
                )
            )
        return eventos


__all__ = [
    "ESCOPOS_PADRAO",
    "ISPB_SICOOB",
    "URL_PRODUCAO",
    "URL_SANDBOX",
    "SicoobPagamentoAdapter",
    "SicoobPagamentoConfig",
]
