"""Caixa Economica Federal (104), webservice de cobranca SIGCB (SOAP).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra a Caixa. A estrutura
dos XML segue os XSD oficiais publicados pelo barramento da Caixa (WSDL de
`ManutencaoCobrancaBancaria/Boleto/Externo` e `ConsultaCobrancaBancaria/Boleto`);
o hash de autenticacao, as versoes do HEADER e o texto de situacao foram lidos,
como documentacao, do ACBrBoleto (LGPL, nenhum codigo portado). Fontes e
duvidas em `docs/caixa-duvidas.md`.

Nao ha modulo Odoo de origem.

Pontos do contrato:

- SOAP 1.1 document/literal. O envelope e montado aqui com texto controlado
  (cada valor escapado), sem cliente SOAP dinamico. O transporte da biblioteca
  serializa o corpo como JSON; `_CorpoXml` (um `Envelope`) devolve o XML puro.
- Autenticacao: o campo `AUTENTICACAO` do HEADER e o SHA-256 em base64 de
  codigo do beneficiario (7) + nosso numero (17) + vencimento DDMMAAAA (8) +
  valor em centavos (15) + CPF/CNPJ do beneficiario (14). Na baixa e na
  consulta, vencimento e valor vao zerados. Nao ha token nem certificado.
- `id_banco` e o nosso numero de 17 digitos ("14" + 15 digitos), gerado do
  lado do beneficiario: a resposta do INCLUI_BOLETO pode trazer NOSSO_NUMERO 0.
- Erro de negocio volta com HTTP 200 e `CONTROLE_NEGOCIAL/COD_RETORNO` diferente
  de 0; vira `Validacao` com a mensagem mascarada.
- XML de resposta com DTD ou entidade e recusado antes do parse.
"""

import asyncio
import base64
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from xml.sax.saxutils import escape

import httpx

from brbank.adapter import BankAdapter
from brbank.capacidades import Capacidade
from brbank.documento import normalizar_documento
from brbank.envelope import Envelope
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

URL_PRODUCAO = "https://barramento.caixa.gov.br/sibar"
CAMINHO_MANUTENCAO = "ManutencaoCobrancaBancaria/Boleto/Externo"
CAMINHO_CONSULTA = "ConsultaCobrancaBancaria/Boleto"

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_SIBAR = "http://caixa.gov.br/sibar"
NS_MANUTENCAO = "http://caixa.gov.br/sibar/manutencao_cobranca_bancaria/boleto/externo"
NS_CONSULTA = "http://caixa.gov.br/sibar/consulta_cobranca_bancaria/boleto"

# soapAction de cada operacao como esta nos WSDL oficiais (o ACBr manda o nome
# da operacao; ver docs/caixa-duvidas.md).
SOAP_ACTION = {
    "INCLUI_BOLETO": "IncluiBoleto",
    "ALTERA_BOLETO": "AlteraBoleto",
    "BAIXA_BOLETO": "BaixaBoleto",
    "CONSULTA_BOLETO": (
        "http://caixa.gov.br/sibar/consulta_cobranca_bancaria/boleto/NewOperation"
    ),
}

# Versoes do HEADER usadas pelo ACBr: 3.0 boleto convencional, 3.2 hibrido,
# 5.4 consulta. Configuraveis em CaixaConfig.
VERSAO_CONVENCIONAL = "3.0"
VERSAO_HIBRIDO = "3.2"
VERSAO_CONSULTA = "5.4"

SISTEMA_ORIGEM = "SIGCB"
USUARIO_SERVICO_PADRAO = "SGCBS02P"
# Modalidade do nosso numero no SIGCB: "1" registrada, "4" emissao pelo
# beneficiario (o ACBr sempre prefixa "14").
PREFIXO_NOSSO_NUMERO = "14"
CODIGO_MOEDA_REAL = "9"

# SITUACAO do titulo na consulta. Textos lidos do ACBr (que compara por
# substring); aqui a comparacao e exata: texto diferente vira Validacao.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM ABERTO": StatusCanonico.REGISTRADO,
        # Titulo enviado ao cartorio continua devido e nao liquidado.
        "ENVIADO AO CARTORIO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "LIQUIDADO NO CARTORIO": StatusCanonico.LIQUIDADO,
        "TITULO JA PAGO NO DIA": StatusCanonico.LIQUIDADO,
        "BAIXA POR DEVOLUCAO": StatusCanonico.BAIXADO,
        "BAIXA POR PROTESTO": StatusCanonico.BAIXADO,
        # "BAIXA POR ESTORNO" fica fora: significado nao confirmado.
    },
)

_CENTAVO = Decimal("0.01")
_QUATRO_CASAS = Decimal("0.0001")
_SITUACAO_NO_RETORNO = re.compile(r"SITUACAO DO TITULO\s*=\s*(.+)$")
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_FORMATOS_DATA_HORA = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S")

No = tuple[str, "str | list[No]"]


@dataclass
class CaixaConfig:
    conta: str
    # Codigo do beneficiario no SIGCB (ate 7 digitos).
    codigo_beneficiario: str
    # Agencia de vinculo (campo UNIDADE do HEADER).
    agencia: str
    # CPF ou CNPJ do beneficiario: entra no hash de autenticacao.
    documento_beneficiario: str = field(repr=False)
    # Codigo da especie do titulo (TIPO_ESPECIE, tabela da Caixa; ex.: 2 = DM).
    tipo_especie: int
    hibrido: bool = True
    aceite: bool = False
    # NUMERO_DIAS de POS_VENCIMENTO/DEVOLVER quando o titulo nao traz
    # dias_baixa_automatica. 0 e o valor que o ACBr envia.
    dias_devolucao_padrao: int = 0
    usuario_servico: str = USUARIO_SERVICO_PADRAO
    # None: 3.2 com hibrido, 3.0 sem.
    versao_manutencao: str | None = None
    versao_consulta: str = VERSAO_CONSULTA
    url_base: str = URL_PRODUCAO
    # Gera o nosso numero (ate 15 digitos, ou 17 ja com o prefixo "14").
    # Padrao: seu_numero numerico.
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if not self.codigo_beneficiario.isdigit() or len(self.codigo_beneficiario) > 7:
            raise Validacao("caixa: codigo_beneficiario deve ter ate 7 digitos")
        if not self.agencia.isdigit() or len(self.agencia) > 5:
            raise Validacao("caixa: agencia deve ter ate 5 digitos")
        try:
            self.documento_beneficiario = normalizar_documento(
                self.documento_beneficiario
            )
        except ValueError as exc:
            raise Validacao("caixa: documento_beneficiario invalido") from exc
        if not 0 <= self.tipo_especie <= 99:
            raise Validacao("caixa: tipo_especie fora de 0..99")
        if not 0 <= self.dias_devolucao_padrao <= 999:
            raise Validacao("caixa: dias_devolucao_padrao fora de 0..999")
        if len(self.usuario_servico) > 8:
            raise Validacao("caixa: usuario_servico tem no maximo 8 caracteres")

    @property
    def versao(self) -> str:
        if self.versao_manutencao:
            return self.versao_manutencao
        return VERSAO_HIBRIDO if self.hibrido else VERSAO_CONVENCIONAL


class _CorpoXml(Envelope):
    """Leva o XML ja montado pelo transporte, que serializa `json=` em JSON.

    O adapter passa o envelope SOAP como texto; aqui ele volta a ser o XML puro
    em UTF-8. A resposta nao e tocada (`ativo = False`).
    """

    ativo = False

    def cifrar(self, corpo: bytes) -> bytes:
        texto = json.loads(corpo)
        if not isinstance(texto, str):
            raise Validacao("caixa: corpo SOAP deve ser texto XML")
        return texto.encode("utf-8")

    def decifrar(self, corpo: bytes) -> bytes:
        return corpo


def autenticacao(
    codigo_beneficiario: str,
    nosso_numero: str,
    documento_beneficiario: str,
    vencimento: date | None = None,
    valor: Decimal | None = None,
) -> str:
    """Hash do campo AUTENTICACAO: SHA-256 em base64 da cadeia de 61 posicoes."""
    data = vencimento.strftime("%d%m%Y") if vencimento else "0" * 8
    centavos = _centavos(valor) if valor is not None else 0
    texto = (
        f"{int(codigo_beneficiario):07d}{nosso_numero}{data}"
        f"{centavos:015d}{documento_beneficiario.zfill(14)}"
    )
    return base64.b64encode(hashlib.sha256(texto.encode("ascii")).digest()).decode()


def _centavos(valor: Decimal) -> int:
    return int((valor.quantize(_CENTAVO) * 100).to_integral_value())


def _valor(valor: Decimal) -> str:
    return f"{valor.quantize(_CENTAVO):.2f}"


def _percentual(valor: Decimal) -> str:
    quatro = valor.quantize(_QUATRO_CASAS)
    if quatro != valor:
        raise Validacao("caixa: percentual com mais de 4 casas decimais")
    return f"{quatro:.4f}"


def _xml(tag: str, conteudo: "str | list[No]") -> str:
    if isinstance(conteudo, list):
        interno = "".join(_xml(filho, valor) for filho, valor in conteudo)
    else:
        interno = escape(conteudo)
    return f"<{tag}>{interno}</{tag}>"


def _envelope(prefixo: str, namespace: str, header: list[No], dados: list[No]) -> str:
    raiz = f"{prefixo}:SERVICO_ENTRADA"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}"><soapenv:Body>'
        f'<{raiz} xmlns:{prefixo}="{namespace}" xmlns:sib="{NS_SIBAR}">'
        f"{_xml('sib:HEADER', header)}{_xml('DADOS', dados)}</{raiz}>"
        "</soapenv:Body></soapenv:Envelope>"
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _para_dict(elemento: ET.Element) -> Any:
    filhos = list(elemento)
    if not filhos:
        return (elemento.text or "").strip()
    dados: dict[str, Any] = {}
    for filho in filhos:
        chave, valor = _local(filho.tag), _para_dict(filho)
        if chave in dados:
            atual = dados[chave]
            dados[chave] = (
                [*atual, valor] if isinstance(atual, list) else [atual, valor]
            )
        else:
            dados[chave] = valor
    return dados


def _lista(valor: Any) -> list[Any]:
    if valor is None or valor == "":
        return []
    return valor if isinstance(valor, list) else [valor]


def _codigo_zero(codigo: Any) -> bool:
    return isinstance(codigo, str) and codigo.isdigit() and int(codigo) == 0


def _normalizar_situacao(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.upper().split())


def _decimal(texto: Any) -> Decimal | None:
    if not texto or not isinstance(texto, str):
        return None
    try:
        return Decimal(texto)
    except InvalidOperation as exc:
        raise Validacao("caixa: valor ilegivel na resposta") from exc


def _data(texto: Any) -> date | None:
    if not texto or not isinstance(texto, str):
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _data_pagamento(texto: Any) -> date | None:
    """DATA_HORA_PAGAMENTO e texto de 19 posicoes em formato nao documentado."""
    if not texto or not isinstance(texto, str):
        return None
    for formato in _FORMATOS_DATA_HORA:
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            continue
    return None


def _pessoa(pessoa: Pessoa) -> list[No]:
    nome = pessoa.nome.strip()[:40]
    if pessoa.tipo == "FISICA":
        return [("CPF", pessoa.documento), ("NOME", nome)]
    return [("CNPJ", pessoa.documento), ("RAZAO_SOCIAL", nome)]


def _endereco(pessoa: Pessoa) -> list[No]:
    endereco = pessoa.endereco
    if endereco is None:
        return []
    # Limites do XSD: logradouro 40, bairro e cidade 15. A Caixa guarda o
    # texto truncado (ver docs/caixa-duvidas.md).
    logradouro = f"{endereco.logradouro} {endereco.numero}".strip()[:40]
    return [
        (
            "ENDERECO",
            [
                ("LOGRADOURO", logradouro),
                ("BAIRRO", endereco.bairro.strip()[:15]),
                ("CIDADE", endereco.cidade.strip()[:15]),
                ("UF", endereco.uf),
                ("CEP", endereco.cep),
            ],
        )
    ]


class CaixaAdapter(BankAdapter):
    banco: ClassVar[str] = "caixa"
    validado_sandbox: ClassVar[bool] = False
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO | Capacidade.ALTERACAO | Capacidade.BAIXA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: CaixaConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
        agora: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._config = config
        self._hoje = hoje
        self._agora = agora
        self._transporte = Transporte(
            banco=self.banco,
            conta=config.conta,
            base_url=config.url_base,
            client=client,
            envelope=_CorpoXml(),
            limitador=LimitadorTaxa(config.requisicoes_por_segundo, dormir=dormir),
            retry=retry,
            dormir=dormir,
        )
        self._registrados: dict[str, TituloRegistrado] = {}
        self._travas: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._transporte.aclose()

    # -- montagem ------------------------------------------------------------

    def _nosso_numero(self, titulo: Titulo) -> str:
        gerador = self._config.gerar_nosso_numero
        bruto = str(gerador(titulo)) if gerador else titulo.seu_numero
        if bruto.isdigit() and len(bruto) == 17:
            if not bruto.startswith(PREFIXO_NOSSO_NUMERO):
                raise Validacao("caixa: nosso numero de 17 digitos deve comecar com 14")
            return bruto
        if not bruto.isdigit() or len(bruto) > 15:
            raise Validacao(
                "caixa exige nosso numero numerico de ate 15 digitos: informe "
                "gerar_nosso_numero ou use seu_numero numerico"
            )
        return PREFIXO_NOSSO_NUMERO + bruto.zfill(15)

    @staticmethod
    def _id_banco(id_banco: str) -> str:
        if not id_banco.isdigit() or len(id_banco) != 17:
            raise Validacao("caixa: id_banco deve ser o nosso numero de 17 digitos")
        return id_banco

    def _header(
        self,
        operacao: str,
        versao: str,
        nosso_numero: str,
        vencimento: date | None = None,
        valor: Decimal | None = None,
    ) -> list[No]:
        cfg = self._config
        return [
            ("VERSAO", versao),
            (
                "AUTENTICACAO",
                autenticacao(
                    cfg.codigo_beneficiario,
                    nosso_numero,
                    cfg.documento_beneficiario,
                    vencimento,
                    valor,
                ),
            ),
            ("USUARIO_SERVICO", cfg.usuario_servico),
            ("OPERACAO", operacao),
            ("SISTEMA_ORIGEM", SISTEMA_ORIGEM),
            ("UNIDADE", cfg.agencia),
            ("DATA_HORA", self._agora().strftime("%Y%m%d%H%M%S")),
        ]

    def _codigo_beneficiario(self) -> No:
        return ("CODIGO_BENEFICIARIO", self._config.codigo_beneficiario.zfill(7))

    def _titulo_inclusao(self, titulo: Titulo, nosso_numero: str) -> list[No]:
        cfg = self._config
        if len(titulo.seu_numero) > 11:
            raise Validacao("caixa: seu_numero vai em NUMERO_DOCUMENTO (ate 11)")
        if len(titulo.mensagens) > 2 or any(len(m) > 40 for m in titulo.mensagens):
            raise Validacao("caixa aceita ate 2 mensagens de 40 caracteres")
        enc = titulo.encargos
        apos_vencimento = (titulo.vencimento + timedelta(days=1)).isoformat()
        itens: list[No] = [("NOSSO_NUMERO", nosso_numero)]
        if cfg.hibrido:
            itens.append(("TIPO", "HIBRIDO"))
        itens += [
            ("NUMERO_DOCUMENTO", titulo.seu_numero),
            ("DATA_VENCIMENTO", titulo.vencimento.isoformat()),
            ("VALOR", _valor(titulo.valor)),
            ("TIPO_ESPECIE", f"{cfg.tipo_especie:02d}"),
            ("FLAG_ACEITE", "S" if cfg.aceite else "N"),
            ("DATA_EMISSAO", self._hoje().isoformat()),
        ]
        if enc.juros_mensal_percentual is not None:
            juros: list[No] = [
                ("TIPO", "TAXA_MENSAL"),
                ("DATA", apos_vencimento),
                ("PERCENTUAL", _percentual(enc.juros_mensal_percentual)),
            ]
        else:
            juros = [("TIPO", "ISENTO"), ("VALOR", "0.00")]
        itens.append(("JUROS_MORA", juros))
        if enc.abatimento is not None:
            itens.append(("VALOR_ABATIMENTO", _valor(enc.abatimento)))
        dias = titulo.dias_baixa_automatica
        if dias is None:
            dias = cfg.dias_devolucao_padrao
        if dias > 999:
            raise Validacao("caixa: dias de devolucao ate 999")
        itens += [
            ("POS_VENCIMENTO", [("ACAO", "DEVOLVER"), ("NUMERO_DIAS", str(dias))]),
            ("CODIGO_MOEDA", CODIGO_MOEDA_REAL),
            ("PAGADOR", _pessoa(titulo.pagador) + _endereco(titulo.pagador)),
        ]
        if titulo.beneficiario is not None:
            itens.append(("SACADOR_AVALISTA", _pessoa(titulo.beneficiario)))
        if enc.multa_percentual is not None:
            itens.append(
                (
                    "MULTA",
                    [
                        ("DATA", apos_vencimento),
                        ("PERCENTUAL", _percentual(enc.multa_percentual)),
                    ],
                )
            )
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            desconto: list[No] = [
                ("DATA", limite.isoformat()),
                ("PERCENTUAL", _percentual(enc.desconto_percentual)),
                ("TIPO", "PERCENTUAL_ATE_DATA"),
            ]
            itens.append(("DESCONTOS", [("DESCONTO", desconto)]))
        itens.append(("IDENTIFICACAO_EMPRESA", titulo.seu_numero))
        if titulo.mensagens:
            mensagens: list[No] = [("MENSAGEM", m) for m in titulo.mensagens]
            itens.append(("FICHA_COMPENSACAO", [("MENSAGENS", mensagens)]))
        return itens

    # -- chamada e leitura -----------------------------------------------------

    async def _chamar(
        self, operacao: str, caminho: str, xml: str, rotulo: str
    ) -> dict[str, Any]:
        resp = await self._transporte.requisitar(
            "POST",
            caminho,
            operacao=rotulo,
            json=xml,
            cabecalhos={
                "Content-Type": "text/xml; charset=utf-8",
                "Accept": "text/xml",
                "SOAPAction": f'"{SOAP_ACTION[operacao]}"',
            },
        )
        return self._dados(resp, operacao)

    @staticmethod
    def _dados(resp: httpx.Response, operacao: str) -> dict[str, Any]:
        """Valida a resposta SOAP e devolve o conteudo de DADOS como dict."""
        status = resp.status_code
        conteudo = resp.content
        if _DTD.search(conteudo):
            raise Validacao("caixa: resposta com DTD recusada", status_http=status)
        try:
            raiz = ET.fromstring(conteudo)
        except ET.ParseError as exc:
            raise Validacao("caixa: resposta nao e XML", status_http=status) from exc
        corpo = next((e for e in raiz.iter() if _local(e.tag) == "Body"), None)
        if corpo is None or len(corpo) == 0:
            raise Validacao("caixa: resposta SOAP sem Body", status_http=status)
        saida = corpo[0]
        if _local(saida.tag) == "Fault":
            falha = _para_dict(saida)
            texto = falha.get("faultstring", "") if isinstance(falha, dict) else ""
            raise Validacao(
                f"caixa: SOAP Fault: {mascarar(texto)}",
                status_http=status,
                resposta_bruta=falha if isinstance(falha, dict) else None,
            )
        servico = _para_dict(saida)
        if not isinstance(servico, dict):
            raise Validacao("caixa: SERVICO_SAIDA vazio", status_http=status)
        if not _codigo_zero(servico.get("COD_RETORNO")):
            raise Validacao(
                f"caixa: barramento recusou {operacao} "
                f"(COD_RETORNO {servico.get('COD_RETORNO')!r}): "
                f"{mascarar(servico.get('MSG_RETORNO') or '')}",
                status_http=status,
                resposta_bruta={
                    k: servico.get(k)
                    for k in ("COD_RETORNO", "ORIGEM_RETORNO", "MSG_RETORNO")
                },
            )
        dados = servico.get("DADOS")
        if not isinstance(dados, dict):
            raise Validacao(f"caixa: {operacao} sem DADOS", status_http=status)
        if dados.get("EXCECAO"):
            raise Validacao(
                f"caixa: excecao em {operacao}: {mascarar(dados['EXCECAO'])}",
                status_http=status,
            )
        controles = _lista(dados.get("CONTROLE_NEGOCIAL"))
        if not controles:
            raise Validacao(
                f"caixa: {operacao} sem CONTROLE_NEGOCIAL",
                status_http=status,
                resposta_bruta=dados,
            )
        for controle in controles:
            if not isinstance(controle, dict) or not _codigo_zero(
                controle.get("COD_RETORNO")
            ):
                retorno = _mensagem(controle)
                raise Validacao(
                    f"caixa recusou {operacao}: {mascarar(retorno)}",
                    status_http=status,
                    resposta_bruta=dados,
                )
        return dados

    # -- operacoes -------------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        chave = titulo.chave_idempotencia
        trava = self._travas.setdefault(chave, asyncio.Lock())
        async with trava:
            if chave in self._registrados:
                return self._registrados[chave]
            nosso_numero = self._nosso_numero(titulo)
            cfg = self._config
            xml = _envelope(
                "manutencaocobrancabancaria",
                NS_MANUTENCAO,
                self._header(
                    "INCLUI_BOLETO",
                    cfg.versao,
                    nosso_numero,
                    titulo.vencimento,
                    titulo.valor,
                ),
                [
                    (
                        "INCLUI_BOLETO",
                        [
                            self._codigo_beneficiario(),
                            ("TITULO", self._titulo_inclusao(titulo, nosso_numero)),
                        ],
                    )
                ],
            )
            dados = await self._chamar(
                "INCLUI_BOLETO", CAMINHO_MANUTENCAO, xml, "registrar"
            )
            inclusao = dados.get("INCLUI_BOLETO")
            if not isinstance(inclusao, dict):
                raise Validacao(
                    "caixa: resposta sem INCLUI_BOLETO", resposta_bruta=dados
                )
            registrado = TituloRegistrado(
                banco=self.banco,
                id_banco=nosso_numero,
                status=StatusCanonico.REGISTRADO,
                seu_numero=titulo.seu_numero,
                nosso_numero=nosso_numero,
                linha_digitavel=inclusao.get("LINHA_DIGITAVEL") or None,
                codigo_barras=inclusao.get("CODIGO_BARRAS") or None,
                pix_copia_cola=inclusao.get("QRCODE") or None,
                resposta_bruta=dados,
            )
            self._registrados[chave] = registrado
            return registrado

    async def _consultar_dados(self, nosso_numero: str) -> dict[str, Any]:
        xml = _envelope(
            "consultacobrancabancaria",
            NS_CONSULTA,
            self._header("CONSULTA_BOLETO", self._config.versao_consulta, nosso_numero),
            [
                (
                    "CONSULTA_BOLETO",
                    [self._codigo_beneficiario(), ("NOSSO_NUMERO", nosso_numero)],
                )
            ],
        )
        dados = await self._chamar(
            "CONSULTA_BOLETO", CAMINHO_CONSULTA, xml, "consultar"
        )
        consulta = dados.get("CONSULTA_BOLETO")
        titulo = consulta.get("TITULO") if isinstance(consulta, dict) else None
        if not isinstance(titulo, dict):
            raise Validacao("caixa: consulta sem TITULO", resposta_bruta=dados)
        return dados

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        nosso_numero = self._id_banco(id_banco)
        dados = await self._consultar_dados(nosso_numero)
        titulo = dados["CONSULTA_BOLETO"]["TITULO"]
        situacao = titulo.get("SITUACAO") or _situacao_do_retorno(dados)
        status = self.tabela_status.traduzir(_normalizar_situacao(situacao or ""))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            valor_pago = _decimal(titulo.get("VALOR_PAGO"))
            data_pagamento = _data_pagamento(titulo.get("DATA_HORA_PAGAMENTO"))
        return TituloRegistrado(
            banco=self.banco,
            id_banco=nosso_numero,
            status=status,
            seu_numero=titulo.get("IDENTIFICACAO_EMPRESA")
            or titulo.get("NUMERO_DOCUMENTO")
            or None,
            nosso_numero=nosso_numero,
            linha_digitavel=titulo.get("LINHA_DIGITAVEL") or None,
            codigo_barras=titulo.get("CODIGO_BARRAS") or None,
            pix_copia_cola=titulo.get("QRCODE") or None,
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=dados,
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        self.exigir(Capacidade.ALTERACAO)
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        nosso_numero = self._id_banco(id_banco)
        nova_data = instrucao.nova_data_vencimento
        abatimento = instrucao.valor
        if tipo is TipoInstrucao.PRORROGACAO:
            if nova_data is None:
                raise Validacao("prorrogacao exige nova_data_vencimento")
        elif tipo is TipoInstrucao.ABATIMENTO:
            if abatimento is None:
                raise Validacao("abatimento exige valor")
        else:
            raise NaoSuportado(f"caixa: instrucao {tipo.value} nao implementada")
        # O hash da alteracao leva vencimento e valor do titulo: le o estado
        # atual antes (ver docs/caixa-duvidas.md).
        dados = await self._consultar_dados(nosso_numero)
        atual = dados["CONSULTA_BOLETO"]["TITULO"]
        valor = _decimal(atual.get("VALOR"))
        vencimento = _data(atual.get("DATA_VENCIMENTO"))
        if valor is None or vencimento is None:
            raise Validacao("caixa: consulta sem VALOR ou DATA_VENCIMENTO para alterar")
        campos: list[No] = [("NOSSO_NUMERO", nosso_numero)]
        if nova_data is not None and tipo is TipoInstrucao.PRORROGACAO:
            vencimento = nova_data
            campos.append(("DATA_VENCIMENTO", nova_data.isoformat()))
        elif abatimento is not None:
            campos.append(("VALOR_ABATIMENTO", _valor(abatimento)))
        xml = _envelope(
            "manutencaocobrancabancaria",
            NS_MANUTENCAO,
            self._header(
                "ALTERA_BOLETO", self._config.versao, nosso_numero, vencimento, valor
            ),
            [("ALTERA_BOLETO", [self._codigo_beneficiario(), ("TITULO", campos)])],
        )
        await self._chamar("ALTERA_BOLETO", CAMINHO_MANUTENCAO, xml, "instrucao")
        return await self.consultar_titulo(nosso_numero)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        nosso_numero = self._id_banco(id_banco)
        xml = _envelope(
            "manutencaocobrancabancaria",
            NS_MANUTENCAO,
            self._header("BAIXA_BOLETO", self._config.versao, nosso_numero),
            [
                (
                    "BAIXA_BOLETO",
                    [self._codigo_beneficiario(), ("NOSSO_NUMERO", nosso_numero)],
                )
            ],
        )
        dados = await self._chamar("BAIXA_BOLETO", CAMINHO_MANUTENCAO, xml, "baixar")
        return TituloRegistrado(
            banco=self.banco,
            id_banco=nosso_numero,
            status=StatusCanonico.BAIXADO,
            nosso_numero=nosso_numero,
            resposta_bruta=dados,
        )

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado("caixa: SIGCB nao tem webhook, use consultar_titulo")


def _mensagem(controle: Any) -> str:
    if not isinstance(controle, dict):
        return ""
    mensagens = controle.get("MENSAGENS")
    if isinstance(mensagens, dict) and mensagens.get("RETORNO"):
        return str(mensagens["RETORNO"])
    return str(controle.get("MSG_RETORNO") or controle.get("COD_RETORNO") or "")


def _situacao_do_retorno(dados: dict[str, Any]) -> str | None:
    """Situacao no texto do retorno: "(0) OPERACAO EFETUADA - SITUACAO DO
    TITULO = EM ABERTO" (layout antigo sem SITUACAO no TITULO)."""
    for controle in _lista(dados.get("CONTROLE_NEGOCIAL")):
        achado = _SITUACAO_NO_RETORNO.search(_mensagem(controle))
        if achado:
            return achado.group(1)
    return None
