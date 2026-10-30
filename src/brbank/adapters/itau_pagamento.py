"""Itau (341), API Cash Management / SISPAG v1: pagamento de Pix por chave, por dados
bancarios e copia e cola (OAuth2 client_credentials + mTLS).

ESTADO: `validado_sandbox = False`. Sem modulo Odoo de origem. A especificacao
oficial (devportal.itau.com.br, API `itau-ep9-gtw-sispag-ext`) so abre com login:
nada deste contrato foi lido nela. Vem de terceiros publicos que concordam entre
si: a colecao Postman "API Pagamentos PIX" publicada no GitHub, integracoes que
dizem ter rodado em producao e os conectores Abstra e Hyperswitch. Oficial e
publico so o OAuth2 e o mTLS. Fontes, grau de cada fato e o que falta:
`docs/itau-pagamento-duvidas.md`.

Pontos do contrato (terceiros, ver o documento):

- A inclusao e exclusiva para Pix: `POST /transferencias` com `chave`, com
  `ispb` + dados da conta, ou com `emv`. Boleto, guia e TED so aparecem na
  consulta, entao `BOLETO`, `GUIA` e `TRANSFERENCIA` nao sao declaradas.
- Paga um por vez (`LOTE_LOCAL`); o `numero_lote` que o Itau devolve fica na
  resposta bruta da linha e nao e o id do lote daqui.
- `status_pagamento = Sucesso` so diz que o Itau aceitou a inclusao: o pagamento
  ainda passa por aprovacao manual no Itau Empresas. Nao ha rota conhecida de
  aprovacao, entao `LIBERACAO` nao e declarada e a linha fica `ACEITO`. So a
  consulta (`Efetuado`) leva a `PAGO`.
- Sem chave de idempotencia conhecida: as inclusoes saem sem retry automatico e
  linha sem resposta volta `INDETERMINADO`. A `chave_idempotencia` da linha vai
  (ou seu resumo) em `referencia_empresa`, e `localizar_por_referencia` procura o
  pagamento por ela antes de um reenvio.
"""

import asyncio
import hashlib
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import httpx

from brbank.auth import OAuth2ClientCredentials
from brbank.erros import Duplicado, ErroBrbank, Indisponivel, NaoSuportado, Validacao
from brbank.mascara import mascarar_texto
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

URL_PRODUCAO = "https://api.itau.com.br/sispag/v1"
URL_SANDBOX = "https://sandbox.devportal.itau.com.br/itau-ep9-gtw-sispag-ext/v1"
URL_TOKEN = "https://sts.itau.com.br/api/oauth/token"
URL_TOKEN_SANDBOX = "https://sandbox.devportal.itau.com.br/api/oauth/jwt"

MODULOS_SISPAG = ("Fornecedores", "Diversos")

# status_pagamento da inclusao, sem acento e em minusculas. `agendado` vem de uma
# fonte so; `rejeitado` e `nao incluido` sao recusa expressa do banco.
STATUS_INCLUSAO = {
    "sucesso": StatusPagamento.ACEITO,
    "sucesso (pre-autorizado)": StatusPagamento.ACEITO,
    "agendado": StatusPagamento.AGENDADO,
    "rejeitado": StatusPagamento.RECUSADO,
    "nao incluido": StatusPagamento.RECUSADO,
}
# status na consulta. `Pendente de alteracao/exclusao` fica fora de proposito: nao
# ha como dizer se o pagamento segue valendo.
STATUS_CONSULTA = {
    "pendente de autorizacao": StatusPagamento.ACEITO,
    "pendente de efetivacao": StatusPagamento.EM_PROCESSAMENTO,
    "autorizado": StatusPagamento.EM_PROCESSAMENTO,
    "efetuado": StatusPagamento.PAGO,
    "efetuados": StatusPagamento.PAGO,
    "nao efetuado": StatusPagamento.REJEITADO,
    "nao efetuados": StatusPagamento.REJEITADO,
    "rejeitado": StatusPagamento.REJEITADO,
    "rejeitado em arquivo": StatusPagamento.REJEITADO,
    "cancelado": StatusPagamento.CANCELADO,
    "estornado": StatusPagamento.DEVOLVIDO,
}

MOTIVO_NAO_ENVIADO = "NAO_ENVIADO"
MOTIVO_SEM_RESPOSTA = "SEM_RESPOSTA"

_REFERENCIA_LIMITE = 20
_REFERENCIA_ACEITA = re.compile(r"^[A-Za-z0-9.\-/#]+$")
_MENSAGEM_LIMITE = 100
_MAXIMO_PAGINAS = 50
# Campos que saem na resposta bruta: o Itau ecoa conta, documento, chave e nome em
# campos que o `mascarar` nao conhece, e o historico traz o CPF do operador.
_BRUTA_INCLUSAO = (
    "status_pagamento",
    "cod_pagamento",
    "numero_lote",
    "numero_lancamento",
    "tipo_pagamento",
    "data_pagamento",
    "valor_pagamento",
    "referencia_empresa",
)
_BRUTA_CONSULTA = (
    "id_pagamento",
    "status",
    "motivo",
    "numero_lote",
    "numero_lancamento",
    "tipo_pagamento",
    "modalidade_pagto",
    "data_pagamento",
    "valor_pagamento",
    "referencia_empresa",
    "indicador_motivo",
)


def _normalizar(valor: Any) -> str:
    """Texto sem acento, em minusculas: o Itau devolve `Nao Efetuado` com acento."""
    decomposto = unicodedata.normalize("NFD", str(valor or ""))
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _texto_sispag(texto: str, limite: int) -> str:
    """Texto livre sem acento: o SISPAG e mainframe e devolve nomes sem acento."""
    decomposto = unicodedata.normalize("NFD", texto)
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    limpo = re.sub(r"[^A-Za-z0-9 .,\-/#]", " ", sem_acento)
    return " ".join(limpo.split())[:limite].strip()


def _agencia(agencia: str) -> str:
    sem_zeros = agencia.lstrip("0")
    if len(sem_zeros) > 4:
        raise Validacao("itau: agencia deve ter ate 4 digitos, sem o DV")
    return agencia[-4:].zfill(4)


def _conta(conta: ContaBancaria) -> str:
    """Conta e DV juntos, com zeros a esquerda ate 8 posicoes."""
    return (conta.conta + conta.digito.upper()).zfill(8)


def _tipo_conta(conta: ContaBancaria) -> str:
    return "PP" if conta.poupanca else "CC"


def _tipo_pessoa(documento: str) -> str:
    if len(documento) == 11:
        return "F"
    if len(documento) == 14:
        return "J"
    raise Validacao("itau: documento deve ser CPF ou CNPJ")


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Itau") from exc


def _data(valor: Any) -> date | None:
    if not valor or not isinstance(valor, str):
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _filtrar(dados: Mapping[str, Any], campos: tuple[str, ...]) -> dict[str, Any]:
    return {c: dados[c] for c in campos if c in dados}


def _codigos_de_recusa(dados: Mapping[str, Any]) -> tuple[str, ...]:
    motivos = dados.get("motivo_recusa")
    if not isinstance(motivos, list):
        return ()
    return tuple(
        str(m["codigo"])
        for m in motivos
        if isinstance(m, dict) and m.get("codigo") not in (None, "")
    )


def _motivos_do_erro(erro: ErroBrbank) -> tuple[str, ...]:
    """Motivos de uma recusa HTTP. O corpo chega mascarado, e o `nome` do
    `motivo_recusa` junto: fica o codigo."""
    bruto = erro.resposta_bruta
    motivos: list[str] = []
    if isinstance(bruto, dict):
        motivos.extend(_codigos_de_recusa(bruto))
        if bruto.get("mensagem"):
            motivos.append(str(bruto["mensagem"]))
        campos = bruto.get("campos")
        for campo in campos if isinstance(campos, list) else ():
            if isinstance(campo, dict) and campo.get("mensagem"):
                motivos.append(str(campo["mensagem"]))
    return tuple(motivos) or (erro.mensagem,)


@dataclass
class ItauPagamentoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # CPF ou CNPJ do titular da conta de debito (`pagador.documento`).
    documento_pagador: str = field(repr=False)
    # Fornecedores e o unico dominio de entrada que a especificacao lista, segundo
    # uma integracao que a leu; Diversos aparece em outra fonte.
    modulo_sispag: str = "Fornecedores"
    # `x-itau-flowID`: identifica a funcionalidade de negocio na aplicacao.
    fluxo: str = "BRBANK_PAGAMENTO"
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    url_base: str | None = None
    # So a inclusao: uma integracao relata que o suporte recomendou
    # https://api.gateway.itau.com.br/sispag/v1 e que a consulta falha nesse host.
    url_inclusao: str | None = None
    url_token: str | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Itau invalido: {self.ambiente!r}")
        if self.modulo_sispag not in MODULOS_SISPAG:
            raise Validacao(f"modulo_sispag deve ser um de {MODULOS_SISPAG}")
        self.documento_pagador = re.sub(
            r"[^0-9A-Za-z]", "", self.documento_pagador
        ).upper()
        _tipo_pessoa(self.documento_pagador)

    @property
    def sandbox(self) -> bool:
        return self.ambiente == "sandbox"

    @property
    def base(self) -> str:
        return self.url_base or (URL_SANDBOX if self.sandbox else URL_PRODUCAO)

    @property
    def endereco_token(self) -> str:
        return self.url_token or (URL_TOKEN_SANDBOX if self.sandbox else URL_TOKEN)


class _Incerto(Exception):
    """Pagamento enviado sem resposta legivel: pode ter sido incluido."""

    def __init__(self, id_banco: str | None = None) -> None:
        super().__init__("pagamento sem resposta")
        self.id_banco = id_banco


class ItauPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "itau"
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.PIX
        | CapacidadePagamento.PIX_QRCODE
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LOTE_LOCAL
    )
    validado_sandbox: ClassVar[bool] = False

    def __init__(
        self,
        config: ItauPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
        correlacao: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._config = config
        self._hoje = hoje
        self._correlacao = correlacao
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None and not config.sandbox:
            raise Validacao("Itau exige certificado mTLS em producao")
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=config.endereco_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.base,
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

    def _cabecalhos(self) -> dict[str, str]:
        return {
            "x-itau-apikey": self._config.client_id,
            "x-itau-flowID": self._config.fluxo,
            "x-itau-correlationID": self._correlacao(),
        }

    async def _ler(self, caminho: str, operacao: str) -> dict[str, Any]:
        resp = await self._transporte.requisitar(
            "GET", caminho, operacao=operacao, cabecalhos=self._cabecalhos()
        )
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Itau nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao("resposta do Itau fora do formato")
        return dados

    # -- montagem --------------------------------------------------------------

    def referencia_empresa(self, pagamento: Pagamento) -> str:
        """`referencia_empresa` da linha: a propria chave quando cabe (20 posicoes,
        sem caractere especial), senao um resumo dela. Estavel para a mesma linha."""
        chave = pagamento.chave_idempotencia
        if len(chave) <= _REFERENCIA_LIMITE and _REFERENCIA_ACEITA.match(chave):
            return chave
        resumo = hashlib.sha256(chave.encode()).hexdigest()
        return resumo[:_REFERENCIA_LIMITE].upper()

    def _pagador(self, conta_debito: ContaBancaria) -> dict[str, str]:
        documento = self._config.documento_pagador
        return {
            "tipo_conta": _tipo_conta(conta_debito),
            "agencia": _agencia(conta_debito.agencia),
            "conta": _conta(conta_debito),
            "tipo_pessoa": _tipo_pessoa(documento),
            "documento": documento,
            "modulo_sispag": self._config.modulo_sispag,
        }

    def _data_pagamento(self, dia: date) -> str:
        if dia < self._hoje():
            raise Validacao("itau: data de pagamento no passado")
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
    def _dados_bancarios(destino: DestinoPix) -> dict[str, str]:
        conta = destino.conta
        assert conta is not None
        if not conta.ispb:
            raise Validacao("itau: Pix por dados bancarios exige o ISPB do destino")
        if not destino.documento:
            raise Validacao("itau: Pix por dados bancarios exige o documento")
        return {
            "ispb": conta.ispb,
            "tipo_identificacao_conta": _tipo_conta(conta),
            "agencia_recebedor": _agencia(conta.agencia),
            "conta_recebedor": _conta(conta),
            "tipo_de_identificacao_do_recebedor": _tipo_pessoa(destino.documento),
            "identificacao_recebedor": destino.documento,
        }

    def _corpo(self, p: Pagamento, conta_debito: ContaBancaria) -> dict[str, Any]:
        corpo: dict[str, Any] = {
            "valor_pagamento": f"{p.valor:.2f}",
            "data_pagamento": self._data_pagamento(p.data),
            "referencia_empresa": self.referencia_empresa(p),
            "pagador": self._pagador(conta_debito),
        }
        if p.tipo is TipoPagamento.PIX_QRCODE:
            if p.pagar_com_juros_multa:
                # Sem campo para isso: o valor divergente do QR e recusado (908).
                raise Validacao("itau: sem pagamento com encargos somados pelo banco")
            corpo["emv"] = p.brcode
        else:
            assert p.pix is not None
            if p.pix.chave is not None:
                corpo["chave"] = self._chave(p.pix)
            else:
                corpo.update(self._dados_bancarios(p.pix))
        mensagem = _texto_sispag(p.descricao or "", _MENSAGEM_LIMITE)
        if mensagem:
            corpo["informacoes_entre_usuarios"] = mensagem
        return corpo

    # -- leitura ---------------------------------------------------------------

    def _incluido(self, dados: dict[str, Any], p: Pagamento) -> PagamentoRegistrado:
        codigo = dados.get("cod_pagamento")
        status = STATUS_INCLUSAO.get(_normalizar(dados.get("status_pagamento")))
        if status is None:
            raise _Incerto(str(codigo) if codigo else None)
        if status is not StatusPagamento.RECUSADO and not codigo:
            # Incluiu sem identificador: nao da para dizer que nao vai pagar.
            raise _Incerto
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(codigo or ""),
            status=status,
            chave_idempotencia=p.chave_idempotencia,
            valor=p.valor,
            data=_data(dados.get("data_pagamento")) or p.data,
            motivos=_codigos_de_recusa(dados),
            resposta_bruta=_filtrar(dados, _BRUTA_INCLUSAO),
        )

    @staticmethod
    def _traduzir(bruto: Any, dados: dict[str, Any]) -> StatusPagamento:
        status = STATUS_CONSULTA.get(_normalizar(bruto))
        if status is None:
            raise Validacao(
                f"itau: status de pagamento nao mapeado {bruto!r}",
                resposta_bruta=dados,
            )
        return status

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

    def _detalhe(self, id_banco: str, dados: dict[str, Any]) -> PagamentoRegistrado:
        raiz = dados.get("data") if isinstance(dados.get("data"), dict) else dados
        detalhe = raiz.get("dados_pagamento")
        if not isinstance(detalhe, dict):
            raise Validacao("resposta do Itau sem dados_pagamento")
        bruta = _filtrar(detalhe, _BRUTA_CONSULTA)
        motivos = detalhe.get("motivo_rejeicao")
        descricoes = tuple(
            mascarar_texto(str(m["descricao_motivo"]))
            for m in (motivos if isinstance(motivos, list) else ())
            if isinstance(m, dict) and m.get("descricao_motivo")
        )
        historico = raiz.get("historico_pagamento")
        if isinstance(historico, list):
            bruta["historico_pagamento"] = [
                _filtrar(h, ("status", "data"))
                for h in historico
                if isinstance(h, dict)
            ]
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(detalhe.get("id_pagamento") or id_banco),
            status=self._traduzir(detalhe.get("status"), bruta),
            valor=_dinheiro(detalhe.get("valor_pagamento")),
            data=_data(detalhe.get("data_pagamento")),
            motivos=descricoes,
            resposta_bruta=bruta,
        )

    def _item_da_lista(self, item: dict[str, Any]) -> PagamentoRegistrado:
        bruta = _filtrar(item, _BRUTA_CONSULTA)
        motivo = item.get("motivo")
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(item.get("id_pagamento") or ""),
            status=self._traduzir(item.get("status"), bruta),
            valor=_dinheiro(item.get("valor_pagamento")),
            data=_data(item.get("data_pagamento")),
            motivos=(mascarar_texto(str(motivo)),) if motivo else (),
            resposta_bruta=bruta,
        )

    # -- operacoes -------------------------------------------------------------

    async def _pagar(
        self, p: Pagamento, conta_debito: ContaBancaria
    ) -> PagamentoRegistrado:
        corpo = self._corpo(p, conta_debito)
        try:
            resp = await self._transporte.requisitar(
                "POST",
                "transferencias",
                operacao="pagar",
                json=corpo,
                cabecalhos=self._cabecalhos(),
                base_url=self._config.url_inclusao,
                retry=SEM_RETRY,
            )
        except (Indisponivel, Duplicado) as exc:
            # Sem resposta, 5xx ou 409 (ja existe?): pode ter sido incluido.
            raise _Incerto from exc
        try:
            dados = resp.json()
        except ValueError as exc:
            raise _Incerto from exc
        if not isinstance(dados, dict):
            raise _Incerto
        return self._incluido(dados, p)

    async def reservar_numero_lote(self) -> str:
        """Numero local: os pagamentos vao um a um."""
        return uuid.uuid4().hex

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        """Envia linha a linha. Recusa de uma linha nao para as outras; linha sem
        resposta (INDETERMINADO) para o envio e as seguintes voltam RECUSADO com
        motivo NAO_ENVIADO. Aceite nao e pagamento: falta a aprovacao no Itau
        Empresas, que a API nao faz."""
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        numero = lote.numero or await self.reservar_numero_lote()
        linhas: list[PagamentoRegistrado] = []
        parou = False
        for p in lote.pagamentos:
            if parou:
                linhas.append(self._recusado(p, (MOTIVO_NAO_ENVIADO,)))
                continue
            try:
                linhas.append(await self._pagar(p, lote.conta_debito))
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
        raise NaoSuportado("itau: o lote e local (LOTE_LOCAL); consulte cada pagamento")

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        """Detalhe pelo `cod_pagamento`. A consulta cobre toda forma de pagamento do
        SISPAG, nao so o que entrou por esta API."""
        if not id_banco:
            raise Validacao("itau: consulta exige o codigo do pagamento")
        dados = await self._ler(
            f"pagamentos_sispag/{quote(id_banco, safe='')}", "consultar_pagamento"
        )
        return self._detalhe(id_banco, dados)

    async def localizar_por_referencia(
        self, conta_debito: ContaBancaria, pagamento: Pagamento
    ) -> tuple[PagamentoRegistrado, ...]:
        """Procura a linha na lista do SISPAG, pela `referencia_empresa` e pela data.

        Para resolver um INDETERMINADO, ja que a API nao tem chave de idempotencia
        conhecida. Lista vazia nao prova que nada foi incluido: nao se sabe em
        quanto tempo a inclusao aparece na lista.
        """
        referencia = self.referencia_empresa(pagamento)
        filtros = {
            "agencia_operacao": _agencia(conta_debito.agencia),
            "conta_operacao": _conta(conta_debito),
            "cnpj_empresa": self._config.documento_pagador,
            "referencia_empresa": referencia,
            "data_inicial": pagamento.data.isoformat(),
            "data_final": pagamento.data.isoformat(),
            "tipo_lista": "Detalhada",
            "status": "TD",
        }
        achados: list[PagamentoRegistrado] = []
        for pagina in range(_MAXIMO_PAGINAS):
            consulta = urlencode({**filtros, "page": pagina})
            dados = await self._ler(f"pagamentos_sispag?{consulta}", "localizar")
            raiz = dados.get("data")
            itens = raiz.get("itens") if isinstance(raiz, dict) else None
            if not isinstance(itens, list):
                raise Validacao("resposta do Itau sem data.itens")
            # A checagem local vale tambem se o servidor ignorar o filtro.
            achados.extend(
                self._item_da_lista(item)
                for item in itens
                if isinstance(item, dict)
                and str(item.get("referencia_empresa") or "").strip().upper()
                == referencia.upper()
            )
            paginacao = dados.get("pagination")
            if not isinstance(paginacao, dict):
                break
            # A colecao usa total_pages; um relato de producao viu totalPages.
            total = paginacao.get("total_pages", paginacao.get("totalPages"))
            if not isinstance(total, int) or pagina + 1 >= total:
                break
        return tuple(achados)


__all__ = [
    "URL_PRODUCAO",
    "URL_SANDBOX",
    "URL_TOKEN",
    "URL_TOKEN_SANDBOX",
    "ItauPagamentoAdapter",
    "ItauPagamentoConfig",
]
