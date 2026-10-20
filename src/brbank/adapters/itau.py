"""Itau (341), API de Cobranca (cash_management v2) e boleto hibrido (Bolecode).

ESTADO: `validado_sandbox = False`. A documentacao oficial do Itau
(devportal.itau.com.br) so abre com login; o contrato foi montado por leitura,
como documentacao, do ACBrBoleto (LGPL, nenhum codigo portado) e de relatos do
forum do ACBr. Nada aqui foi executado contra o Itau. Duvidas e fontes:
`docs/itau-duvidas.md`.

Pontos do contrato:

- Quatro hosts: emissao sem Pix, alteracao e baixa em `cash_management/v2`;
  consulta em `boletoscash/v2`; emissao hibrida (Bolecode) em `recebimentos-pix/v1`;
  token em `sts.itau.com.br`. Todos configuraveis em `ItauConfig`.
- OAuth2 `client_credentials` com id e segredo no formulario, sem escopo, mais
  mTLS. Toda chamada leva `x-itau-apikey` (o client_id) e `x-itau-correlationID`.
- O beneficiario gera o nosso numero (8 digitos). `id_banco` = nosso numero; a
  URL de alteracao e baixa usa agencia (4) + conta (7) + DAC (1) + carteira (3)
  + nosso numero (8).
- Na emissao, valores vao como texto de 17 digitos em centavos e percentuais como
  texto de 12 digitos com 5 casas; na alteracao, como decimal em texto ("2.50").
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
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

URL_COBRANCA = "https://api.itau.com.br/cash_management/v2"
URL_COBRANCA_SANDBOX = (
    "https://sandbox.devportal.itau.com.br/itau-ep9-gtw-cash-management-ext-v2/v2"
)
URL_CONSULTA = "https://secure.api.cloud.itau.com.br/boletoscash/v2"
URL_CONSULTA_SANDBOX = URL_COBRANCA_SANDBOX
# Endpoint novo do Bolecode (o `pix_recebimentos_conciliacoes/v2/boletos_pix` foi
# descontinuado pelo Itau em 2026). Caminho `boletos-pix` visto so em log de
# sandbox publicado no forum do ACBr: ver docs/itau-duvidas.md.
URL_BOLECODE = "https://pix-pj.api.itau.com/recebimentos-pix/v1"
URL_BOLECODE_SANDBOX = (
    "https://sandbox.devportal.itau.com.br/itau-ep9-api-recebimentos-v1-externo/v1"
)
CAMINHO_BOLECODE = "boletos-pix"
URL_TOKEN = "https://sts.itau.com.br/api/oauth/token"
URL_TOKEN_SANDBOX = "https://devportal.itau.com.br/api/jwt"

# situacao_geral_boleto (consulta) e codigo_status (francesinha), segundo o ACBr.
# Fora de proposito: PAGAMENTO DEVOLVIDO, EA (entrada por acerto), TM e TS
# (tarifas): sem significado confirmado, viram Validacao.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM ABERTO": StatusCanonico.REGISTRADO,
        "E": StatusCanonico.REGISTRADO,
        "AGUARDANDO PAGAMENTO": StatusCanonico.REGISTRADO,
        "PAGO": StatusCanonico.LIQUIDADO,
        "PAGA": StatusCanonico.LIQUIDADO,
        "L": StatusCanonico.LIQUIDADO,
        "BAIXA POR TER SIDO LIQUIDADO": StatusCanonico.LIQUIDADO,
        "BL": StatusCanonico.LIQUIDADO,
        "BAIXADO": StatusCanonico.BAIXADO,
        "BAIXADA": StatusCanonico.BAIXADO,
        "B": StatusCanonico.BAIXADO,
        "BA": StatusCanonico.BAIXADO,
        "BC": StatusCanonico.BAIXADO,
    },
)

# codigo_especie do Itau por sigla da especie (tabela do ACBr).
ESPECIES = {
    "DM": "01", "NP": "02", "NS": "03", "ME": "04", "RC": "05", "DS": "08",
    "LC": "09", "DD": "15", "EC": "16", "FS": "17", "PS": "17", "BDP": "18",
    "BP": "18", "CBI": "88", "CC": "89", "CCB": "90", "CD": "91", "CH": "92",
    "CM": "93", "CPS": "94", "DMI": "95", "DSI": "96", "RA": "97", "TA": "98",
    "OU": "99",
}  # fmt: skip

_MOTIVO_BAIXA_LIQUIDADO = "BAIXA POR TER SIDO LIQUIDADO"
_TIPO_MULTA_PERCENTUAL = "02"
_TIPO_MULTA_ISENTO = "03"
_TIPO_JUROS_PERCENTUAL_MENSAL = "90"
_TIPO_DESCONTO_PERCENTUAL_ATE_DATA = "02"
_DIAS_APOS_VENCIMENTO = 1
_TAMANHO_NOSSO_NUMERO = 8


def _centavos17(valor: Decimal) -> str:
    centavos = (valor * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"{int(centavos):017d}"


def _percentual12(valor: Decimal) -> str:
    escalado = (valor * 100000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"{int(escalado):012d}"


def _decimal_texto(valor: Decimal) -> str:
    return str(valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _dinheiro(valor: Any) -> Decimal | None:
    """Valor da resposta: decimal ("150.00", numero) ou 17 digitos em centavos."""
    if valor is None or valor == "":
        return None
    texto = str(valor).strip()
    try:
        if texto.isdigit() and len(texto) == 17:
            return Decimal(texto) / 100
        return Decimal(texto)
    except InvalidOperation:
        return None


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


@dataclass
class ItauConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    agencia: str
    conta_corrente: str
    # DAC (digito da agencia/conta) do Itau.
    digito_conta: str
    carteira: int
    especie: str = "DM"
    # True: boleto hibrido (Bolecode, com Pix); False: boleto CASH sem QR.
    bolecode: bool = True
    chave_pix: str | None = field(default=None, repr=False)
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # Nosso numero (ate 8 digitos). Padrao: seu_numero numerico.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    # None: "efetivacao" em producao; "validacao" (CASH) ou "simulacao"
    # (Bolecode) no sandbox, como no ACBr.
    etapa_processo: str | None = None
    url_cobranca: str | None = None
    url_consulta: str | None = None
    url_bolecode: str | None = None
    caminho_bolecode: str = CAMINHO_BOLECODE
    url_token: str | None = None
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Itau invalido: {self.ambiente!r}")
        for nome, tamanho in (
            ("agencia", 4),
            ("conta_corrente", 7),
            ("digito_conta", 1),
        ):
            valor = getattr(self, nome)
            if not valor.isdigit() or len(valor) > tamanho:
                raise Validacao(f"Itau: {nome} deve ter ate {tamanho} digitos")
        if not 0 < self.carteira <= 999:
            raise Validacao("Itau: carteira deve ter ate 3 digitos")
        if self.especie not in ESPECIES:
            raise Validacao(f"especie de titulo Itau desconhecida: {self.especie!r}")
        if self.bolecode and not self.chave_pix:
            raise Validacao("Itau: Bolecode exige chave_pix")

    @property
    def sandbox(self) -> bool:
        return self.ambiente == "sandbox"

    @property
    def id_beneficiario(self) -> str:
        return self.agencia.zfill(4) + self.conta_corrente.zfill(7) + self.digito_conta

    @property
    def base_cobranca(self) -> str:
        padrao = URL_COBRANCA_SANDBOX if self.sandbox else URL_COBRANCA
        return self.url_cobranca or padrao

    @property
    def base_consulta(self) -> str:
        padrao = URL_CONSULTA_SANDBOX if self.sandbox else URL_CONSULTA
        return self.url_consulta or padrao

    @property
    def base_bolecode(self) -> str:
        padrao = URL_BOLECODE_SANDBOX if self.sandbox else URL_BOLECODE
        return self.url_bolecode or padrao

    @property
    def endereco_token(self) -> str:
        return self.url_token or (URL_TOKEN_SANDBOX if self.sandbox else URL_TOKEN)

    @property
    def etapa(self) -> str:
        if self.etapa_processo:
            return self.etapa_processo
        if not self.sandbox:
            return "efetivacao"
        return "simulacao" if self.bolecode else "validacao"


def _pessoa(pessoa: Pessoa, *, papel: str, exigir_endereco: bool) -> dict[str, Any]:
    if pessoa.tipo == "FISICA":
        tipo = {
            "codigo_tipo_pessoa": "F",
            "numero_cadastro_pessoa_fisica": pessoa.documento,
        }
    else:
        tipo = {
            "codigo_tipo_pessoa": "J",
            "numero_cadastro_nacional_pessoa_juridica": pessoa.documento,
        }
    dados: dict[str, Any] = {
        "pessoa": {"nome_pessoa": pessoa.nome, "tipo_pessoa": tipo}
    }
    endereco = pessoa.endereco
    if endereco is None:
        if exigir_endereco:
            raise Validacao(f"{papel} sem endereco: o Itau exige endereco do pagador")
        return dados
    dados["endereco"] = {
        "nome_logradouro": f"{endereco.logradouro} {endereco.numero}".strip(),
        "nome_bairro": endereco.bairro,
        "nome_cidade": endereco.cidade,
        "sigla_UF": endereco.uf,
        "numero_CEP": endereco.cep,
    }
    return dados


class ItauAdapter(BankAdapter):
    banco: ClassVar[str] = "itau"
    validado_sandbox: ClassVar[bool] = False
    # HIBRIDO so vale com `bolecode=True`: a instancia ajusta em __init__.
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO | Capacidade.ALTERACAO | Capacidade.BAIXA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: ItauConfig,
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
        if not config.bolecode:
            self.capacidades = Capacidade.ALTERACAO | Capacidade.BAIXA
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=config.endereco_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.base_cobranca,
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
        return {
            "x-itau-apikey": self._config.client_id,
            "x-itau-correlationID": self._correlacao(),
        }

    async def _chamar(
        self,
        metodo: str,
        caminho: str,
        operacao: str,
        *,
        json: Any = None,
        base_url: str | None = None,
    ) -> httpx.Response:
        return await self._transporte.requisitar(
            metodo,
            caminho,
            operacao=operacao,
            json=json,
            cabecalhos=self._cabecalhos(),
            base_url=base_url,
        )

    def _nosso_numero(self, titulo: Titulo) -> str:
        gerador = self._config.gerar_nosso_numero
        bruto = str(gerador(titulo)) if gerador else titulo.seu_numero
        if not bruto.isdigit() or len(bruto) > _TAMANHO_NOSSO_NUMERO:
            raise Validacao(
                "Itau exige nosso numero numerico de ate 8 digitos: informe "
                "gerar_nosso_numero ou use seu_numero numerico"
            )
        return bruto.zfill(_TAMANHO_NOSSO_NUMERO)

    def _id_titulo(self, nosso_numero: str) -> str:
        """agencia + conta + DAC + carteira + nosso numero (caminho da alteracao)."""
        if not nosso_numero.isdigit() or len(nosso_numero) > _TAMANHO_NOSSO_NUMERO:
            raise Validacao("Itau: id_banco deve ser o nosso numero de ate 8 digitos")
        return (
            self._config.id_beneficiario
            + f"{self._config.carteira:03d}"
            + nosso_numero.zfill(_TAMANHO_NOSSO_NUMERO)
        )

    def _dado_boleto(self, titulo: Titulo, nosso_numero: str) -> dict[str, Any]:
        cfg = self._config
        if titulo.dias_baixa_automatica is not None:
            raise Validacao(
                "Itau: baixa automatica por dias nao suportada (nome do campo da "
                "instrucao em aberto, ver docs/itau-duvidas.md)"
            )
        enc = titulo.encargos
        instrumento = "boleto_pix" if cfg.bolecode else "boleto"
        dado: dict[str, Any] = {
            "descricao_instrumento_cobranca": instrumento,
            "tipo_boleto": "a vista",
            "codigo_carteira": cfg.carteira,
            "valor_total_titulo": _centavos17(titulo.valor),
            "codigo_especie": ESPECIES[cfg.especie],
            "valor_abatimento": _centavos17(enc.abatimento or Decimal(0)),
            "data_emissao": self._hoje().isoformat(),
            "indicador_pagamento_parcial": False,
            "quantidade_maximo_parcial": 0,
            "desconto_expresso": False,
            "pagador": _pessoa(titulo.pagador, papel="pagador", exigir_endereco=True),
            "dados_individuais_boleto": [
                {
                    "numero_nosso_numero": nosso_numero,
                    "data_vencimento": titulo.vencimento.isoformat(),
                    "valor_titulo": _centavos17(titulo.valor),
                    "texto_uso_beneficiario": "0",
                    "texto_seu_numero": titulo.seu_numero,
                }
            ],
        }
        if titulo.beneficiario:
            dado["sacador_avalista"] = _pessoa(
                titulo.beneficiario, papel="sacador", exigir_endereco=False
            )
        if enc.multa_percentual is not None:
            dado["multa"] = {
                "codigo_tipo_multa": _TIPO_MULTA_PERCENTUAL,
                "percentual_multa": _percentual12(enc.multa_percentual),
                "quantidade_dias_multa": _DIAS_APOS_VENCIMENTO,
            }
        else:
            dado["multa"] = {"codigo_tipo_multa": _TIPO_MULTA_ISENTO}
        if enc.juros_mensal_percentual is not None:
            dado["juros"] = {
                "codigo_tipo_juros": _TIPO_JUROS_PERCENTUAL_MENSAL,
                "quantidade_dias_juros": _DIAS_APOS_VENCIMENTO,
                "percentual_juros": _percentual12(enc.juros_mensal_percentual),
            }
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            dado["desconto"] = {
                "codigo_tipo_desconto": _TIPO_DESCONTO_PERCENTUAL_ATE_DATA,
                "descontos": [
                    {
                        "data_desconto": limite.isoformat(),
                        "percentual_desconto": _percentual12(enc.desconto_percentual),
                    }
                ],
            }
        return dado

    def _montar_payload(self, titulo: Titulo, nosso_numero: str) -> dict[str, Any]:
        cfg = self._config
        beneficiario = {"id_beneficiario": cfg.id_beneficiario}
        dado = self._dado_boleto(titulo, nosso_numero)
        if cfg.bolecode:
            return {
                "etapa_processo_boleto": cfg.etapa,
                "beneficiario": beneficiario,
                "dado_boleto": dado,
                "dados_qrcode": {"chave": cfg.chave_pix},
            }
        return {
            "data": {
                "etapa_processo_boleto": cfg.etapa,
                "codigo_canal_operacao": "API",
                "beneficiario": beneficiario,
                "dado_boleto": dado,
            }
        }

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Itau nao e JSON", status_http=resp.status_code
            ) from exc

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        cfg = self._config
        nosso_numero = self._nosso_numero(titulo)
        payload = self._montar_payload(titulo, nosso_numero)
        if cfg.bolecode:
            resp = await self._chamar(
                "POST",
                cfg.caminho_bolecode,
                "registrar",
                json=payload,
                base_url=cfg.base_bolecode,
            )
        else:
            resp = await self._chamar("POST", "boletos", "registrar", json=payload)
        corpo = self._json(resp)
        raiz = corpo.get("data") if isinstance(corpo, dict) else None
        if not isinstance(raiz, dict):
            raiz = corpo if isinstance(corpo, dict) else {}
        dado = raiz.get("dado_boleto")
        individuais = dado.get("dados_individuais_boleto") if dado else None
        if not isinstance(individuais, list) or not individuais:
            raise Validacao(
                "resposta do Itau sem dados_individuais_boleto",
                status_http=resp.status_code,
                resposta_bruta=corpo,
            )
        individual = individuais[-1]
        qrcode = raiz.get("dados_qrcode") or {}
        devolvido = str(individual.get("numero_nosso_numero") or nosso_numero)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=devolvido,
            status=StatusCanonico.REGISTRADO,
            seu_numero=individual.get("texto_seu_numero") or titulo.seu_numero,
            nosso_numero=devolvido,
            linha_digitavel=individual.get("numero_linha_digitavel"),
            codigo_barras=individual.get("codigo_barras"),
            pix_copia_cola=qrcode.get("emv"),
            txid=qrcode.get("txid"),
            resposta_bruta=corpo if isinstance(corpo, dict) else {},
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        cfg = self._config
        consulta = urlencode(
            {
                "id_beneficiario": cfg.id_beneficiario,
                "codigo_carteira": f"{cfg.carteira:03d}",
                "nosso_numero": id_banco,
                "view": "specific",
            }
        )
        resp = await self._chamar(
            "GET", f"boletos?{consulta}", "consultar", base_url=cfg.base_consulta
        )
        corpo = self._json(resp)
        lista = corpo.get("data") if isinstance(corpo, dict) else None
        if not isinstance(lista, list) or not lista:
            raise Validacao(
                "Itau: consulta sem titulo",
                status_http=resp.status_code,
                resposta_bruta=corpo,
            )
        # O Itau pode reaproveitar o nosso numero depois de liquidado: o ultimo
        # item e o titulo mais recente (comportamento relatado pelo ACBr).
        item = lista[-1]
        dado = item.get("dado_boleto") or {}
        individuais = dado.get("dados_individuais_boleto") or [{}]
        individual = individuais[-1]
        status = self.tabela_status.traduzir(individual.get("situacao_geral_boleto"))
        baixa = dado.get("baixa") or {}
        motivo = str(baixa.get("motivo_baixa") or "").upper()
        if motivo == _MOTIVO_BAIXA_LIQUIDADO:
            status = StatusCanonico.LIQUIDADO
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            valor_pago, data_pagamento = self._liquidacao(dado)
        qrcode = dado.get("qrcode_pix") or {}
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=individual.get("texto_seu_numero"),
            nosso_numero=str(individual.get("numero_nosso_numero") or id_banco),
            linha_digitavel=individual.get("numero_linha_digitavel"),
            codigo_barras=individual.get("codigo_barras"),
            pix_copia_cola=qrcode.get("emv"),
            txid=qrcode.get("txid"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=item,
        )

    @staticmethod
    def _liquidacao(dado: dict[str, Any]) -> tuple[Decimal | None, date | None]:
        """Valor e data vem de `pagamentos_cobranca` (o ultimo pagamento).

        Bolecode pago por Pix (status BL) pode nao trazer pagamentos: valor None.
        """
        pagamentos = dado.get("pagamentos_cobranca") or []
        if not pagamentos:
            return None, None
        ultimo = pagamentos[-1]
        return (
            _dinheiro(ultimo.get("valor_pago_total_cobranca")),
            _data(ultimo.get("data_inclusao_pagamento")),
        )

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
            campo = "data_vencimento"
            corpo: dict[str, Any] = {
                "data_vencimento": instrucao.nova_data_vencimento.isoformat()
            }
        elif tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            campo = "abatimento"
            corpo = {"valor_abatimento": _decimal_texto(instrucao.valor)}
        else:
            # Protesto exige quantidade de dias, que a Instrucao canonica nao tem.
            raise NaoSuportado(f"itau: instrucao {tipo.value} nao implementada")
        await self._chamar(
            "PATCH",
            f"boletos/{self._id_titulo(id_banco)}/{campo}",
            "instrucao",
            json=corpo,
        )
        # A alteracao nao devolve o titulo: o estado vem da reconsulta.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        await self._chamar(
            "PATCH", f"boletos/{self._id_titulo(id_banco)}/baixa", "baixar", json={}
        )
        return TituloRegistrado(
            banco=self.banco, id_banco=id_banco, status=StatusCanonico.BAIXADO
        )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado(
            "itau: webhook de boleto sem contrato conhecido (docs/itau-duvidas.md)"
        )
