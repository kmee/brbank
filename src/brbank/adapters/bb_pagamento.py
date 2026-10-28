"""Banco do Brasil, API Pagamentos em Lote v1 (OAuth2 client_credentials).

Sem modulo Odoo de origem. Contrato lido no OpenAPI oficial 1.2.1 e na doc do
portal (codigo 23), e exercitado na homologacao em 28/10/2026: lotes de boleto e
de Pix aceitos, consulta por tipo e liberacao (docs/bb-duvidas.md, secao
Pagamentos em Lote).

Pontos do contrato:

- Homologacao pelo host comum `api.hm.bb.com.br`, sem mTLS. Producao so no host
  mTLS (`pagamentos-lote.mtls.api.bb.com.br`): sem certificado o adapter recusa.
- `gw-dev-app-key` como query param em toda chamada, como na Cobranca.
- O lote e a requisicao do BB (`numeroRequisicao`, unico por contrato). O numero
  e reservado antes do envio: com ele fixo, o retry do transporte nao duplica.
- Datas como inteiro `ddmmaaaa` sem zero a esquerda no dia; valores como numero.
- Cada tipo tem rota, nomes de lista, id, aceite e erros proprios; a leitura
  aceita as variacoes que o BB devolve (`erros`, `errors`, `errorCodes`).
- A consulta generica `/{id}/solicitacao` so serve a transferencias: o BB
  responde "Modalidade de pagamento invalida" para outros tipos. A
  `/requisicoes/{id}` responde 404 na homologacao.
"""

import asyncio
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.auth import OAuth2ClientCredentials, OAuth2ClientCredentialsBasic
from brbank.erros import NaoSuportado, Validacao
from brbank.pagamento import (
    AdapterPagamento,
    CapacidadePagamento,
    ContaBancaria,
    LotePagamento,
    LoteRegistrado,
    Pagamento,
    PagamentoRegistrado,
    StatusLote,
    StatusPagamento,
    TipoChavePix,
    TipoPagamento,
)
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

URL_SANDBOX = "https://api.hm.bb.com.br/pagamentos-lote/v1"
URL_PRODUCAO = "https://pagamentos-lote.mtls.api.bb.com.br/v1"
TOKEN_SANDBOX = "https://oauth.hm.bb.com.br/oauth/token"
TOKEN_PRODUCAO = "https://oauth.bb.com.br/oauth/token"

ESCOPO = "pagamento"
# Confirmados no OAuth de homologacao em 28/10/2026.
ESCOPOS_BB = (
    "pagamentos-lote.lotes-info",
    "pagamentos-lote.lotes-requisicao",
    "pagamentos-lote.boletos-info",
    "pagamentos-lote.boletos-requisicao",
    "pagamentos-lote.guias-codigo-barras-info",
    "pagamentos-lote.guias-codigo-barras-requisicao",
    "pagamentos-lote.transferencias-info",
    "pagamentos-lote.transferencias-requisicao",
    "pagamentos-lote.transferencias-pix-info",
    "pagamentos-lote.transferencias-pix-requisicao",
    "pagamentos-lote.pix-info",
    "pagamentos-lote.pagamentos-info",
    "pagamentos-lote.cancelar-requisicao",
)


@dataclass(frozen=True)
class _Rotas:
    lote: str
    consulta_lote: str
    pagamento: str


ROTAS = {
    TipoPagamento.BOLETO: _Rotas(
        "lotes-boletos", "lotes-boletos/{}/solicitacao", "boletos/{}"
    ),
    TipoPagamento.GUIA: _Rotas(
        "lotes-guias-codigo-barras",
        "lotes-guias-codigo-barras/{}/solicitacao",
        "guias-codigo-barras/{}",
    ),
    TipoPagamento.PIX: _Rotas(
        "lotes-transferencias-pix", "lotes-transferencias-pix/{}/solicitacao", "pix/{}"
    ),
    TipoPagamento.TRANSFERENCIA: _Rotas(
        "lotes-transferencias", "{}/solicitacao", "transferencias/{}"
    ),
}

_LISTAS = ("lancamentos", "listaTransferencias", "transferencias", "pagamentos")
# `codigoPagamento` vem na consulta de lote de guias (fora do OpenAPI 1.2.1).
_IDS = (
    "codigoIdentificadorPagamento",
    "identificadorPagamento",
    "identificadorTransferencia",
    "codigoPagamento",
)
_ACEITES = ("indicadorAceite", "indicadorMovimentoAceito")
_ERROS = ("erros", "errors", "errorCodes")

# estadoRequisicao (doc do portal, codigo 23).
ESTADOS_LOTE = {
    1: StatusLote.ACEITO,
    2: StatusLote.ACEITO_PARCIAL,
    3: StatusLote.RECUSADO,
    4: StatusLote.PENDENTE_LIBERACAO,
    5: StatusLote.EM_PROCESSAMENTO,
    6: StatusLote.PROCESSADO,
    7: StatusLote.REJEITADO,
    8: StatusLote.PENDENTE_LIBERACAO,
    9: StatusLote.EM_PROCESSAMENTO,
    10: StatusLote.EM_PROCESSAMENTO,
}

# estadoPagamento, normalizado sem acento e com "_". BLOQUEADO fica fora de
# proposito: a doc nao diz se volta a andar ou se e definitivo.
ESTADOS_PAGAMENTO = {
    "CONSISTENTE": StatusPagamento.ACEITO,
    "PENDENTE": StatusPagamento.ACEITO,
    "INCONSISTENTE": StatusPagamento.RECUSADO,
    "AGENDADO": StatusPagamento.AGENDADO,
    "AGUARDANDO_DEBITO": StatusPagamento.EM_PROCESSAMENTO,
    "DEBITADO": StatusPagamento.EM_PROCESSAMENTO,
    "PAGO": StatusPagamento.PAGO,
    "REJEITADO": StatusPagamento.REJEITADO,
    "CANCELADO": StatusPagamento.CANCELADO,
    "DEVOLVIDO": StatusPagamento.DEVOLVIDO,
}

_FORMA_PIX = {
    TipoChavePix.TELEFONE: 1,
    TipoChavePix.EMAIL: 2,
    TipoChavePix.DOCUMENTO: 3,
    TipoChavePix.ALEATORIA: 4,
}
_PIX_POR_CONTA = 5
# tipoPagamento de Pix e transferencia: 126 fornecedores, 127 salarios, 128 diversos.
TIPOS_PAGAMENTO_BB = (126, 127, 128)


@dataclass
class BBPagamentoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    chave_desenvolvedor: str = field(repr=False)
    # Convenio de pagamentos (PGT) do cliente no BB.
    contrato: str
    ambiente: str = "producao"
    tipo_pagamento: int = 126
    # Finalidade da TED (tabela do BB); obrigatoria para TED a outro banco.
    finalidade_ted: int | None = None
    cnpj_parceiro: str | None = None
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    credenciais_no_corpo: bool | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente BB invalido: {self.ambiente!r}")
        if not self.contrato.isdigit():
            raise Validacao("contrato de pagamentos BB deve ser numerico")
        if self.tipo_pagamento not in TIPOS_PAGAMENTO_BB:
            raise Validacao("tipo_pagamento BB deve ser 126, 127 ou 128")


def _data_bb(dia: date) -> int:
    return int(f"{dia.day}{dia.month:02d}{dia.year}")


def _data_de_bb(valor: Any) -> date | None:
    if not valor:
        return None
    texto = str(valor).zfill(8)
    try:
        return date(int(texto[4:]), int(texto[2:4]), int(texto[:2]))
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do BB") from exc


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do BB") from exc


def _normalizar_estado(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return "_".join(sem_acento.upper().split())


def _primeiro(dados: dict[str, Any], chaves: tuple[str, ...]) -> Any:
    for chave in chaves:
        if chave in dados:
            return dados[chave]
    return None


def _motivos(linha: dict[str, Any]) -> tuple[str, ...]:
    brutos = _primeiro(linha, _ERROS) or []
    return tuple(str(m) for m in brutos if m not in (0, "0", None, ""))


def _documento_pessoa(documento: str) -> tuple[int, int | str]:
    """(tipo, valor): CPF vai como inteiro, CNPJ como texto (aceita alfanumerico)."""
    if len(documento) == 11 and documento.isdigit():
        return 1, int(documento)
    if len(documento) == 14:
        return 2, documento
    raise Validacao("documento deve ser CPF (11) ou CNPJ (14)")


class BancoDoBrasilPagamentoAdapter(AdapterPagamento):
    banco: ClassVar[str] = "bb"
    capacidades_pagamento: ClassVar[CapacidadePagamento] = (
        CapacidadePagamento.BOLETO
        | CapacidadePagamento.GUIA
        | CapacidadePagamento.PIX
        | CapacidadePagamento.TRANSFERENCIA
        | CapacidadePagamento.RESERVA_NUMERO
        | CapacidadePagamento.LIBERACAO
        | CapacidadePagamento.CANCELAMENTO
    )
    validado_sandbox: ClassVar[bool] = True

    def __init__(
        self,
        config: BBPagamentoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        producao = config.ambiente == "producao"
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        if client is None and ssl_context is None and producao:
            raise Validacao("BB pagamentos exige certificado mTLS em producao")
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
            escopos={ESCOPO: " ".join(ESCOPOS_BB)},
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

    # -- chamadas ------------------------------------------------------------

    def _caminho(self, recurso: str, **extras: Any) -> str:
        consulta = {"gw-dev-app-key": self._config.chave_desenvolvedor, **extras}
        return f"{recurso}?{urlencode(consulta)}"

    async def _chamar(
        self, metodo: str, recurso: str, operacao: str, json: Any = None, **extras: Any
    ) -> dict[str, Any]:
        cabecalhos = {}
        if self._config.cnpj_parceiro:
            cabecalhos["x-bb-portal-devx-cnpj-parceiro"] = self._config.cnpj_parceiro
        resp = await self._transporte.requisitar(
            metodo,
            self._caminho(recurso, **extras),
            operacao=operacao,
            escopo=ESCOPO,
            json=json,
            cabecalhos=cabecalhos,
        )
        if not resp.content:
            return {}
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do BB nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do BB fora do formato", status_http=resp.status_code
            )
        return dados

    # -- montagem --------------------------------------------------------------

    def _cabecalho_lote(self, lote: LotePagamento, numero: int) -> dict[str, Any]:
        conta = lote.conta_debito
        contrato = int(self._config.contrato)
        if lote.tipo in (TipoPagamento.BOLETO, TipoPagamento.GUIA):
            return {
                "numeroRequisicao": numero,
                "codigoContrato": contrato,
                "numeroAgenciaDebito": int(conta.agencia),
                "numeroContaCorrenteDebito": int(conta.conta),
                "digitoVerificadorContaCorrenteDebito": conta.digito.upper(),
            }
        chave_contrato = (
            "numeroContrato"
            if lote.tipo is TipoPagamento.PIX
            else ("numeroContratoPagamento")
        )
        return {
            "numeroRequisicao": numero,
            chave_contrato: contrato,
            "agenciaDebito": int(conta.agencia),
            "contaCorrenteDebito": int(conta.conta),
            "digitoVerificadorContaCorrente": conta.digito.upper(),
            "tipoPagamento": self._config.tipo_pagamento,
        }

    @staticmethod
    def _seu_documento(pagamento: Pagamento) -> dict[str, str]:
        # Campo informativo do BB (ate 20); a identidade da linha e a posicao.
        chave = pagamento.chave_idempotencia
        return {"codigoSeuDocumento": chave} if len(chave) <= 20 else {}

    def _linha_boleto(self, p: Pagamento) -> dict[str, Any]:
        assert p.codigo_barras and p.beneficiario_documento  # garantido pelo modelo
        tipo, documento = _documento_pessoa(p.beneficiario_documento)
        linha: dict[str, Any] = {
            "numeroCodigoBarras": p.codigo_barras,
            "dataPagamento": _data_bb(p.data),
            "valorPagamento": _numero(p.valor),
            "codigoTipoBeneficiario": tipo,
            "documentoBeneficiario": documento,
            "valorNominal": _numero(p.valor_nominal or p.valor),
            "valorDesconto": _numero(p.desconto or Decimal(0)),
            "valorMoraMulta": _numero(p.juros_multa or Decimal(0)),
            **self._seu_documento(p),
        }
        if p.descricao:
            linha["descricaoPagamento"] = p.descricao
        if p.nosso_numero:
            linha["codigoNossoDocumento"] = p.nosso_numero
        return linha

    def _linha_guia(self, p: Pagamento) -> dict[str, Any]:
        linha: dict[str, Any] = {
            "codigoBarras": p.codigo_barras,
            "dataPagamento": _data_bb(p.data),
            "valorPagamento": _numero(p.valor),
            **self._seu_documento(p),
        }
        if p.descricao:
            linha["descricaoPagamento"] = p.descricao
        return linha

    def _linha_pix(self, p: Pagamento) -> dict[str, Any]:
        destino = p.pix
        assert destino is not None  # garantido pelo modelo
        linha: dict[str, Any] = {"data": _data_bb(p.data), "valor": _numero(p.valor)}
        if p.descricao:
            linha["descricaoPagamentoInstantaneo"] = p.descricao
        if destino.chave is not None:
            assert destino.tipo_chave is not None
            linha["formaIdentificacao"] = _FORMA_PIX[destino.tipo_chave]
            linha.update(self._chave_pix(destino.tipo_chave, destino.chave))
            return linha
        conta = destino.conta
        assert conta is not None
        if conta.poupanca:
            raise NaoSuportado(
                "bb: Pix para poupanca por dados bancarios nao confirmado"
            )
        if not destino.documento:
            raise Validacao("Pix por dados bancarios exige o documento do favorecido")
        linha.update(
            {
                "formaIdentificacao": _PIX_POR_CONTA,
                "tipoConta": 1,
                "agencia": int(conta.agencia),
                "conta": int(conta.conta),
                "digitoVerificadorConta": conta.digito.upper(),
                **self._banco_destino(conta),
                **self._documento_pix(destino.documento),
            }
        )
        return linha

    @staticmethod
    def _chave_pix(tipo: TipoChavePix, chave: str) -> dict[str, Any]:
        if tipo is TipoChavePix.EMAIL:
            return {"email": chave}
        if tipo is TipoChavePix.ALEATORIA:
            return {"identificacaoAleatoria": chave}
        if tipo is TipoChavePix.DOCUMENTO:
            return BancoDoBrasilPagamentoAdapter._documento_pix(chave)
        digitos = "".join(c for c in chave if c.isdigit())
        if digitos.startswith("55") and len(digitos) in (12, 13):
            digitos = digitos[2:]
        if len(digitos) not in (10, 11):
            raise Validacao("chave Pix de telefone deve ter DDD e numero")
        return {"dddTelefone": int(digitos[:2]), "telefone": int(digitos[2:])}

    @staticmethod
    def _documento_pix(documento: str) -> dict[str, Any]:
        tipo, valor = _documento_pessoa(documento)
        return {"cpf": valor} if tipo == 1 else {"cnpj": valor}

    @staticmethod
    def _banco_destino(conta: ContaBancaria) -> dict[str, Any]:
        if conta.ispb:
            return {"numeroISPB": conta.ispb}
        return {"numeroCOMPE": int(conta.compe or 0)}

    def _linha_transferencia(self, p: Pagamento) -> dict[str, Any]:
        conta = p.conta_credito
        assert conta is not None and p.favorecido_documento
        if conta.poupanca:
            raise NaoSuportado("bb: transferencia para poupanca nao confirmada")
        tipo, documento = _documento_pessoa(p.favorecido_documento)
        linha: dict[str, Any] = {
            "dataTransferencia": _data_bb(p.data),
            "valorTransferencia": _numero(p.valor),
            "agenciaCredito": int(conta.agencia),
            "contaCorrenteCredito": int(conta.conta),
            "digitoVerificadorContaCorrente": conta.digito.upper(),
            ("cpfBeneficiario" if tipo == 1 else "cnpjBeneficiario"): documento,
            **self._banco_destino(conta),
        }
        outro_banco = (conta.compe or "001") != "001" or bool(
            conta.ispb and conta.ispb != "00000000"
        )
        if outro_banco:
            if self._config.finalidade_ted is None:
                raise Validacao("TED para outro banco exige finalidade_ted na config")
            linha["codigoFinalidadeTED"] = self._config.finalidade_ted
        if p.descricao:
            linha["descricaoTransferencia"] = p.descricao
        return linha

    def _corpo(self, lote: LotePagamento, numero: int) -> dict[str, Any]:
        corpo = self._cabecalho_lote(lote, numero)
        if lote.tipo is TipoPagamento.BOLETO:
            corpo["lancamentos"] = [self._linha_boleto(p) for p in lote.pagamentos]
        elif lote.tipo is TipoPagamento.GUIA:
            corpo["lancamentos"] = [self._linha_guia(p) for p in lote.pagamentos]
        elif lote.tipo is TipoPagamento.PIX:
            corpo["listaTransferencias"] = [self._linha_pix(p) for p in lote.pagamentos]
        else:
            corpo["listaTransferencias"] = [
                self._linha_transferencia(p) for p in lote.pagamentos
            ]
        return corpo

    # -- leitura ---------------------------------------------------------------

    def _linha_registrada(
        self, linha: dict[str, Any], chave: str | None
    ) -> PagamentoRegistrado:
        aceite = str(_primeiro(linha, _ACEITES) or "").upper()
        identificador = _primeiro(linha, _IDS)
        if identificador in (None, "", 0):
            raise Validacao(
                "linha do lote sem identificador do BB", resposta_bruta=linha
            )
        valor = _primeiro(linha, ("valorPagamento", "valor", "valorTransferencia"))
        data = _primeiro(linha, ("dataPagamento", "data", "dataTransferencia"))
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(identificador),
            status=StatusPagamento.ACEITO
            if aceite == "S"
            else StatusPagamento.RECUSADO,
            chave_idempotencia=chave,
            valor=_dinheiro(valor),
            data=_data_de_bb(data),
            motivos=_motivos(linha),
            resposta_bruta=linha,
        )

    def _lote_registrado(
        self,
        dados: dict[str, Any],
        id_banco: str,
        tipo: TipoPagamento,
        lote: LotePagamento | None = None,
    ) -> LoteRegistrado:
        estado = _primeiro(dados, ("estadoRequisicao", "codigoEstado"))
        try:
            status = ESTADOS_LOTE[int(estado)]
        except (KeyError, TypeError, ValueError) as exc:
            raise Validacao(
                f"bb: estado de lote desconhecido {estado!r}", resposta_bruta=dados
            ) from exc
        linhas = _primeiro(dados, _LISTAS) or []
        chaves = [p.chave_idempotencia for p in lote.pagamentos] if lote else []
        if lote and len(linhas) != len(chaves):
            raise Validacao(
                "bb: resposta do lote com quantidade de linhas diferente do pedido",
                resposta_bruta=dados,
            )
        return LoteRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            tipo=tipo,
            status=status,
            pagamentos=tuple(
                self._linha_registrada(linha, chaves[i] if chaves else None)
                for i, linha in enumerate(linhas)
            ),
            resposta_bruta=dados,
        )

    # -- operacoes -------------------------------------------------------------

    async def reservar_numero_lote(self) -> str:
        dados = await self._chamar(
            "GET",
            "proximos-numeros-requisicao",
            "reservar_numero",
            quantidadeProximosNumeros=1,
        )
        numeros = dados.get("listaProximosNumeros") or []
        if not numeros:
            raise Validacao("bb: resposta sem proximo numero de requisicao")
        return str(numeros[0])

    async def pagar_lote(self, lote: LotePagamento) -> LoteRegistrado:
        self.exigir_pagamento(CapacidadePagamento[lote.tipo.value])
        if lote.tipo in (TipoPagamento.BOLETO, TipoPagamento.GUIA) and len(
            lote.pagamentos
        ) > (150 if lote.tipo is TipoPagamento.BOLETO else 200):
            raise Validacao("bb: lote acima do limite de linhas do tipo")
        if len(lote.pagamentos) > 320:
            raise Validacao("bb: lote acima de 320 linhas")
        numero = lote.numero or await self.reservar_numero_lote()
        if not numero.isdigit():
            raise Validacao("bb: numero de lote deve ser numerico")
        corpo = self._corpo(lote, int(numero))
        dados = await self._chamar("POST", ROTAS[lote.tipo].lote, "pagar_lote", corpo)
        return self._lote_registrado(dados, numero, lote.tipo, lote)

    async def consultar_lote(
        self, id_banco: str, tipo: TipoPagamento
    ) -> LoteRegistrado:
        dados = await self._chamar(
            "GET", ROTAS[tipo].consulta_lote.format(id_banco), "consultar_lote"
        )
        return self._lote_registrado(dados, id_banco, tipo)

    async def liberar_lote(self, id_banco: str) -> None:
        await self._chamar(
            "POST",
            "liberar-pagamentos",
            "liberar_lote",
            {"numeroRequisicao": int(id_banco), "indicadorFloat": "N"},
        )

    async def consultar_pagamento(
        self, id_banco: str, tipo: TipoPagamento
    ) -> PagamentoRegistrado:
        dados = await self._chamar(
            "GET", ROTAS[tipo].pagamento.format(id_banco), "consultar_pagamento"
        )
        bruto = str(dados.get("estadoPagamento") or "")
        estado = _normalizar_estado(bruto)
        if estado not in ESTADOS_PAGAMENTO:
            raise Validacao(
                f"bb: estado de pagamento nao mapeado {bruto!r}", resposta_bruta=dados
            )
        devolucoes = dados.get("listaDevolucao") or []
        motivos = tuple(
            str(d.get("codigoMotivo"))
            for d in devolucoes
            if isinstance(d, dict) and d.get("codigoMotivo")
        )
        autenticacao = dados.get("codigoAutenticacaoPagamento") or dados.get(
            "autenticacaoPagamento"
        )
        return PagamentoRegistrado(
            banco=self.banco,
            id_banco=str(dados.get("id") or id_banco),
            status=ESTADOS_PAGAMENTO[estado],
            valor=_dinheiro(dados.get("valorPagamento")),
            data=_data_de_bb(dados.get("dataPagamento")),
            motivos=motivos,
            autenticacao=autenticacao or None,
            resposta_bruta=dados,
        )

    async def cancelar_pagamentos(
        self, conta_debito: ContaBancaria, ids_banco: tuple[str, ...]
    ) -> None:
        if not ids_banco:
            raise Validacao("nada para cancelar")
        await self._chamar(
            "POST",
            "cancelar-pagamentos",
            "cancelar_pagamentos",
            {
                "agenciaDebito": int(conta_debito.agencia),
                "contaCorrenteDebito": int(conta_debito.conta),
                "digitoVerificadorContaCorrente": conta_debito.digito.upper(),
                "listaPagamentos": [{"codigoPagamento": i} for i in ids_banco],
            },
        )
