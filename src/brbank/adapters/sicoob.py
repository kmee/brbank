"""Sicoob (756), API de Cobranca Bancaria v3 (boleto hibrido, OAuth2 + mTLS).

ESTADO: `validado_sandbox = True` desde 2026-10-20. O ciclo registrar,
consultar, alterar (prorrogacao, abatimento, protesto), segunda via em PDF e
baixar rodou contra o sandbox publico do Sicoob, que valida o corpo contra o
swagger da v3 e devolve respostas de exemplo fixas. O sandbox nao exercita
OAuth/mTLS, escopos nem transicao de estado: ver `docs/sicoob-duvidas.md`.

Nao ha modulo Odoo de origem. Endpoints, campos e quirks (token sem
client_secret, header `client_id`, datas AAAA-MM-DD, PATCH de alteracao) foram
lidos como documentacao do ACBr (LGPL, nenhum codigo portado) e conferidos no
sandbox e no swagger v3.
"""

import asyncio
import base64
import binascii
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import Autenticador, OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import NaoSuportado, Validacao
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.transporte import LimitadorTaxa, PoliticaRetry, Transporte, contexto_tls

URL_PRODUCAO = "https://api.sicoob.com.br/cobranca-bancaria/v3"
URL_SANDBOX = "https://sandbox.sicoob.com.br/sicoob/sandbox/cobranca-bancaria/v3"
URL_TOKEN = (
    "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
)

# Nao confirmado com o sandbox: so `boletos_inclusao` aparece em fonte de terceiros
# com o nome exato. Configuravel em SicoobConfig.escopos.
ESCOPOS_PADRAO = {
    "incluir": "boletos_inclusao",
    "consultar": "boletos_consulta",
    "alterar": "boletos_alteracao",
}

TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM ABERTO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "BAIXADO": StatusCanonico.BAIXADO,
        "CANCELADO": StatusCanonico.BAIXADO,
        # VENCIDO e EXPIRADO aparecem como constantes na referencia, sem mapeamento
        # conhecido: ficam fora para virar Validacao, nunca um status assumido.
    },
)

_TIPO_DESCONTO_PERCENTUAL_ATE_DATA = 2
_TIPO_MULTA_PERCENTUAL = 2
_TIPO_JUROS_TAXA_MENSAL = 2
_TIPO_JUROS_ISENTO = 3
_TIPO_HISTORICO_LIQUIDACAO = "6"
_CADASTRAR_PIX_SIM = 1
_CADASTRAR_PIX_NAO = 2
_VALOR_EM_DESCRICAO = re.compile(r"R\$\s*([0-9][0-9.,]*)")
_ESPECIE = re.compile(r"[A-Z]{2,3}")
# Tamanhos maximos do swagger v3 (o sandbox nao os confere; a producao sim).
_MAX_SEU_NUMERO = 18
_MAX_MENSAGEM = 40
_MAX_PAGADOR = {"nome": 50, "endereco": 40, "bairro": 30, "cidade": 40}
_MAX_NOME_BENEFICIARIO_FINAL = 40


@dataclass
class SicoobConfig:
    conta: str
    client_id: str
    numero_cliente: int
    numero_conta_corrente: str
    # Sigla da especie (DM, DS, NP, ...): o sandbox recusa numero com HTTP 400.
    codigo_especie_documento: str
    codigo_modalidade: int = 1
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    escopos: Mapping[str, str] = field(default_factory=lambda: dict(ESCOPOS_PADRAO))
    # Token fixo do sandbox publicado pelo Sicoob no portal; nao vai no codigo.
    token_sandbox: str | None = field(default=None, repr=False)
    cadastrar_pix: bool = True
    banco_emite_e_distribui: bool = True
    requisicoes_por_segundo: float | None = 2.0

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


class _ClientCredentialsSemSegredo(OAuth2ClientCredentials):
    """Token do Sicoob: client_id + scope, sem client_secret (a identidade e o mTLS)."""

    def _formulario(self) -> dict[str, str]:
        return {"client_id": self._client_id, "grant_type": "client_credentials"}


class _TokenFixo(Autenticador):
    def __init__(self, token: str) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "_TokenFixo(***)"

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _dinheiro_ptbr(texto: str) -> Decimal | None:
    bruto = texto.rstrip(".,")
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        return Decimal(bruto)
    except InvalidOperation:
        return None


def _pessoa(pessoa: Pessoa, *, papel: str) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao(f"{papel} sem endereco: o Sicoob exige endereco do pagador")
    dados: dict[str, Any] = {
        "numeroCpfCnpj": pessoa.documento,
        "nome": pessoa.nome,
        "endereco": f"{endereco.logradouro} {endereco.numero}".strip(),
        "bairro": endereco.bairro,
        "cidade": endereco.cidade,
        "cep": endereco.cep,
        "uf": endereco.uf,
    }
    for campo, limite in _MAX_PAGADOR.items():
        dados[campo] = dados[campo][:limite]
    if pessoa.email:
        dados["email"] = pessoa.email
    return dados


class SicoobAdapter(BankAdapter):
    banco: ClassVar[str] = "sicoob"
    validado_sandbox: ClassVar[bool] = True
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO | Capacidade.ALTERACAO | Capacidade.BAIXA | Capacidade.PDF
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: SicoobConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        especie = config.codigo_especie_documento
        if not isinstance(especie, str) or not _ESPECIE.fullmatch(especie):
            raise Validacao(
                "sicoob: codigo_especie_documento e a sigla da especie (ex.: 'DM'); "
                "a API v3 recusa codigo numerico"
            )
        self._config = config
        self._hoje = hoje
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth: Autenticador
        if config.ambiente == "sandbox" and config.token_sandbox:
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
            base_url=config.url_base,
            client=client,
            ssl_context=ssl_context,
            autenticador=auth,
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )

    async def aclose(self) -> None:
        await self._transporte.aclose()

    def _cabecalhos(self) -> dict[str, str]:
        return {"client_id": self._config.client_id}

    def _identificacao(self) -> dict[str, int]:
        return {
            "numeroCliente": self._config.numero_cliente,
            "codigoModalidade": self._config.codigo_modalidade,
        }

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        if titulo.dias_baixa_automatica is not None:
            raise Validacao("sicoob: baixa automatica por dias nao tem campo na API v3")
        if len(titulo.seu_numero) > _MAX_SEU_NUMERO:
            # Truncar quebraria a conciliacao pelo seuNumero: recusa.
            raise Validacao(f"sicoob: seuNumero com mais de {_MAX_SEU_NUMERO} posicoes")
        cfg = self._config
        modo = 1 if cfg.banco_emite_e_distribui else 2
        payload: dict[str, Any] = {
            **self._identificacao(),
            "numeroContaCorrente": int(cfg.numero_conta_corrente),
            "codigoEspecieDocumento": cfg.codigo_especie_documento,
            "dataEmissao": self._hoje().isoformat(),
            "seuNumero": titulo.seu_numero,
            "identificacaoBoletoEmpresa": titulo.seu_numero,
            "identificacaoEmissaoBoleto": modo,
            "identificacaoDistribuicaoBoleto": modo,
            "valor": _numero(titulo.valor),
            "dataVencimento": titulo.vencimento.isoformat(),
            "numeroParcela": 1,
            "aceite": False,
            "pagador": _pessoa(titulo.pagador, papel="pagador"),
            "gerarPdf": False,
            # 0 seria "padrao do contrato", que nao garante a ausencia do QR.
            "codigoCadastrarPIX": (
                _CADASTRAR_PIX_SIM if cfg.cadastrar_pix else _CADASTRAR_PIX_NAO
            ),
        }
        enc = titulo.encargos
        apos_vencimento = (titulo.vencimento + timedelta(days=1)).isoformat()
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            payload["tipoDesconto"] = _TIPO_DESCONTO_PERCENTUAL_ATE_DATA
            payload["dataPrimeiroDesconto"] = limite.isoformat()
            payload["valorPrimeiroDesconto"] = _numero(enc.desconto_percentual)
        else:
            payload["tipoDesconto"] = 0
        if enc.abatimento is not None:
            payload["valorAbatimento"] = _numero(enc.abatimento)
        if enc.juros_mensal_percentual is not None:
            payload["tipoJurosMora"] = _TIPO_JUROS_TAXA_MENSAL
            payload["dataJurosMora"] = apos_vencimento
            payload["valorJurosMora"] = _numero(enc.juros_mensal_percentual)
        else:
            payload["tipoJurosMora"] = _TIPO_JUROS_ISENTO
            payload["valorJurosMora"] = 0
        if enc.multa_percentual is not None:
            payload["tipoMulta"] = _TIPO_MULTA_PERCENTUAL
            payload["dataMulta"] = apos_vencimento
            payload["valorMulta"] = _numero(enc.multa_percentual)
        else:
            payload["tipoMulta"] = 0
            payload["valorMulta"] = 0
        if titulo.beneficiario:
            payload["beneficiarioFinal"] = {
                "nome": titulo.beneficiario.nome[:_MAX_NOME_BENEFICIARIO_FINAL],
                "numeroCpfCnpj": titulo.beneficiario.documento,
            }
        if titulo.mensagens:
            payload["mensagensInstrucao"] = [
                m[:_MAX_MENSAGEM] for m in titulo.mensagens[:5]
            ]
        return payload

    @staticmethod
    def _resultado(resp: httpx.Response) -> dict[str, Any]:
        """Extrai `resultado` (objeto; se vier lista, o primeiro item)."""
        if resp.status_code == 204 or not resp.content:
            raise Validacao("sicoob: resposta sem conteudo", status_http=204)
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Sicoob nao e JSON", status_http=resp.status_code
            ) from exc
        resultado = dados.get("resultado") if isinstance(dados, dict) else None
        if isinstance(resultado, list) and resultado:
            resultado = resultado[0]
        if isinstance(resultado, dict) and isinstance(resultado.get("boleto"), dict):
            resultado = resultado["boleto"]
        if not isinstance(resultado, dict):
            raise Validacao(
                "resposta do Sicoob sem resultado",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        return resultado

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            "boletos",
            operacao="registrar",
            escopo="incluir",
            json=self._montar_payload(titulo),
            cabecalhos=self._cabecalhos(),
        )
        r = self._resultado(resp)
        nosso_numero = r.get("nossoNumero")
        if not nosso_numero:
            raise Validacao(
                "resposta do Sicoob sem nossoNumero",
                status_http=resp.status_code,
                resposta_bruta=r,
            )
        return TituloRegistrado(
            banco=self.banco,
            id_banco=str(nosso_numero),
            status=StatusCanonico.REGISTRADO,
            seu_numero=titulo.seu_numero,
            nosso_numero=str(nosso_numero),
            linha_digitavel=r.get("linhaDigitavel"),
            codigo_barras=r.get("codigoBarras"),
            pix_copia_cola=r.get("qrCode"),
            resposta_bruta=r,
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        consulta = urlencode(
            {
                "numeroCliente": self._config.numero_cliente,
                "codigoModalidade": self._config.codigo_modalidade,
                "nossoNumero": id_banco,
            }
        )
        resp = await self._transporte.requisitar(
            "GET",
            f"boletos?{consulta}",
            operacao="consultar",
            escopo="consultar",
            cabecalhos=self._cabecalhos(),
        )
        r = self._resultado(resp)
        status = self.tabela_status.traduzir(r.get("situacaoBoleto"))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            valor_pago, data_pagamento = self._liquidacao(r)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=r.get("seuNumero"),
            nosso_numero=str(r.get("nossoNumero") or id_banco),
            linha_digitavel=r.get("linhaDigitavel"),
            codigo_barras=r.get("codigoBarras"),
            pix_copia_cola=r.get("qrCode"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=r,
        )

    @staticmethod
    def _liquidacao(r: dict[str, Any]) -> tuple[Decimal | None, date | None]:
        """Valor e data da liquidacao vem do historico (tipoHistorico 6).

        O valor so existe como texto em `descricaoHistorico` ("... - R$ 0,75" no
        exemplo do sandbox); sem o padrao, vem None. `tipoHistorico` e string no
        swagger e numero no exemplo do sandbox: compara pelo texto.
        """
        for item in r.get("listaHistorico") or []:
            if str(item.get("tipoHistorico")) != _TIPO_HISTORICO_LIQUIDACAO:
                continue
            achado = _VALOR_EM_DESCRICAO.search(item.get("descricaoHistorico") or "")
            valor = _dinheiro_ptbr(achado.group(1)) if achado else None
            momento = item.get("dataHistorico")
            try:
                data = date.fromisoformat(momento[:10]) if momento else None
            except ValueError:
                data = None
            return valor, data
        return None, None

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        corpo: dict[str, Any] = dict(self._identificacao())
        caminho = f"boletos/{id_banco}"
        metodo = "PATCH"
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
            corpo["prorrogacaoVencimento"] = {
                "dataVencimento": instrucao.nova_data_vencimento.isoformat()
            }
        elif tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            corpo["abatimento"] = {"valorAbatimento": _numero(instrucao.valor)}
        elif tipo is TipoInstrucao.PROTESTO:
            metodo, caminho = "POST", f"boletos/{id_banco}/protestos"
        else:
            raise NaoSuportado(f"sicoob: instrucao {tipo.value} nao implementada")
        await self._transporte.requisitar(
            metodo,
            caminho,
            operacao="instrucao",
            escopo="alterar",
            json=corpo,
            cabecalhos=self._cabecalhos(),
        )
        # A API nao devolve dados na alteracao: o estado vem da reconsulta.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._transporte.requisitar(
            "POST",
            f"boletos/{id_banco}/baixar",
            operacao="baixar",
            escopo="alterar",
            json=self._identificacao(),
            cabecalhos=self._cabecalhos(),
        )
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        """Segunda via: `GET /boletos/segunda-via?gerarPdf=true`, PDF em base64."""
        consulta = urlencode(
            {
                **self._identificacao(),
                "nossoNumero": id_banco,
                "gerarPdf": "true",
            }
        )
        resp = await self._transporte.requisitar(
            "GET",
            f"boletos/segunda-via?{consulta}",
            operacao="pdf",
            escopo="consultar",
            cabecalhos=self._cabecalhos(),
        )
        bruto = self._resultado(resp).get("pdfBoleto") or ""
        try:
            # Decodificacao tolerante: o exemplo do sandbox traz padding em
            # excesso, que o modo estrito recusa. O cabecalho %PDF decide.
            pdf = base64.b64decode(bruto)
        except (binascii.Error, ValueError) as exc:
            raise Validacao("PDF do Sicoob corrompido") from exc
        if not pdf.startswith(b"%PDF"):
            raise Validacao("resposta do Sicoob nao contem PDF")
        return pdf

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado(
            "sicoob: webhook de boleto nao confirmado (docs/sicoob-duvidas.md)"
        )
