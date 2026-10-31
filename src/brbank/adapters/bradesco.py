"""Bradesco (237), API Cobranca do portal Bradesco Developers (OAuth2 + mTLS).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra o Bradesco. O contrato
vem da documentacao oficial, lida sem login na API do Marketplace do portal
(`developers.bradesco.com.br/api/v1/products`): OpenAPI, manual 1.6.4, manual com
QR Code 1.0.5 e collections. Fontes, conflitos e o que segue aberto:
`docs/bradesco-duvidas.md`. Nao ha modulo Odoo de origem nem codigo portado.

Dois produtos, escolhidos por `BradescoConfig.hibrido`:

- "Bank Slip" (cobranca convencional): `boleto/cobranca-*`, valores e percentuais
  decimais em texto ("150.00"); ver `valor_em_centavos`, que e o conflito perigoso.
- "Bank Slip with QR code" (boleto hibrido): `boleto-hibrido/cobranca-*`, valor em
  centavos (o manual fixa: R$ 2,00 = 200), percentuais com 5 casas. A baixa e a mesma
  rota do convencional. Protesto e negativacao por instrucao nao fazem parte desse
  produto; a alteracao exige o txId no cabecalho (ver `obter_txid`).

Token comum: `POST /auth/server-mtls/v2/token`, `client_credentials` com `client_id`
e `client_secret` no formulario, sempre sobre mTLS. Vale por 1 hora.

Quirks oficiais: CPF/CNPJ do beneficiario decomposto em raiz, filial e controle;
`nuNegociacao` de 18 posicoes no registro e `negociacao` de 11 no resto; datas
`DD.MM.AAAA` no registro e `DDMMAAAA` (numero) na consulta e na alteracao; `cdBarras`
vem no padrao de barras EBCDIC ("<NWnn...>"), nao em digitos: o codigo de 44 digitos
sai da linha digitavel. Das 00:00 as 03:00 a API pode ficar indisponivel.

O aviso de webhook nao traz assinatura nem token: so serve para disparar uma consulta
(`parse_webhook` nao devolve valor pago). PDF nao existe na API: a "2a via" e a propria
consulta (linha digitavel e, no hibrido, o QR Code).
"""

import asyncio
import json
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.documento import normalizar_documento
from brbank.erros import Indisponivel, NaoSuportado, Validacao
from brbank.mascara import MASCARA
from brbank.modelos import (
    EventoCanonico,
    Instrucao,
    Pessoa,
    TipoInstrucao,
    Titulo,
    TituloRegistrado,
)
from brbank.status import StatusCanonico, TabelaStatus
from brbank.tempo import hoje_brasil
from brbank.transporte import (
    SEM_RETRY,
    LimitadorTaxa,
    PoliticaRetry,
    Transporte,
    contexto_tls,
)

URL_PRODUCAO = "https://openapi.bradesco.com.br"
URL_SANDBOX = "https://openapisandbox.prebanco.com.br"
CAMINHO_TOKEN = "/auth/server-mtls/v2/token"
CAMINHO_REGISTRO = "boleto/cobranca-registro/v1/cobranca"
CAMINHO_CONSULTA = "boleto/cobranca-consulta/v1/consultar"
CAMINHO_BAIXA = "boleto/cobranca-baixa/v1/baixar"
CAMINHO_ALTERACAO = "boleto/cobranca-altera/v1/alterar"
CAMINHO_PROTESTO = "boleto/cobranca-protesto-negativacao/v1/executar"
# O portal publica a mesma operacao com caminhos diferentes por ambiente.
CAMINHO_WEBHOOK_SANDBOX = "boleto/cobranca-webhook/v1/cadastrar"
CAMINHO_WEBHOOK_PRODUCAO = "boleto/cobranca-webhook/v1/executar"
CAMINHO_HIBRIDO_REGISTRO = "boleto-hibrido/cobranca-registro/v1/gerarBoleto"
CAMINHO_HIBRIDO_CONSULTA = "boleto-hibrido/cobranca-consulta-titulo/v1/consultar"
CAMINHO_HIBRIDO_ALTERACAO = "boleto-hibrido/cobranca-alteracao/v1/alteraBoletoConsulta"

# Tabela 1 do manual: unico codigo aceito no pedido de baixa.
CODIGO_BAIXA_PEDIDO = 57
# ctpoPrzCobr do hibrido: 1 = dias corridos.
_PRAZO_DIAS_CORRIDOS = 1

# Tabela 1 - Status do titulo (manual 1.6.4). 1 a 35 e 99: titulo em aberto, mesmo
# com protesto, sustacao, negativacao ou "pagamento vinculado". Fora da tabela de
# proposito, viram Validacao para decisao humana (docs/bradesco-duvidas.md): 51 no
# convencional, 56, 65 a 73, porque o rotulo oficial nao diz se o dinheiro entrou.
_EM_ABERTO = (*range(1, 13), *range(14, 36), 99)
_PAGOS = (13, 61, 62)
_BAIXADOS_SEM_PAGAMENTO = (52, 53, 54, 55, 57, 58, 59, 60, 63, 64, 98)
_MAPA_STATUS = {
    **{str(c): StatusCanonico.REGISTRADO for c in _EM_ABERTO},
    **{str(c): StatusCanonico.LIQUIDADO for c in _PAGOS},
    **{str(c): StatusCanonico.BAIXADO for c in _BAIXADOS_SEM_PAGAMENTO},
}
TABELA_STATUS = TabelaStatus.criar("2026.10", _MAPA_STATUS)
# Manual com QR Code: 51 = "por acerto (quando o pagamento ocorre via QR Code)".
TABELA_STATUS_HIBRIDO = TabelaStatus.criar(
    "2026.10", {**_MAPA_STATUS, "51": StatusCanonico.LIQUIDADO}
)

# Tabela de especies do manual.
ESPECIES = {
    "CH": 1, "DM": 2, "DMI": 3, "DS": 4, "DSI": 5, "DR": 6, "LC": 7, "NCC": 8,
    "NCE": 9, "NCI": 10, "NCR": 11, "NP": 12, "NPR": 13, "TM": 14, "TS": 15,
    "NS": 16, "RC": 17, "FAT": 18, "ND": 19, "AP": 20, "ME": 21, "PC": 22,
    "DD": 23, "CCB": 24, "FI": 25, "RD": 26, "DRI": 27, "EC": 28, "ECI": 29,
    "CC": 31, "BDP": 32, "OUT": 99,
}  # fmt: skip

_CENTAVO = Decimal("0.01")
_MAX_MENSAGENS = 4
_TAMANHO_MENSAGEM = 80
_TAMANHO_EMAIL = 70
_TAMANHO_URL = 100
# Pagamento a 50 vezes do titulo (ou 1/50) so se explica por erro de escala.
_LIMITE_ESCALA = 50

# Trechos de chave que denunciam dado pessoal na resposta do Bradesco. O `mascarar`
# do core so conhece nomes exatos, e a API devolve nomeSacado10, cnpjSacado, semvQrcode.
_TRECHOS_PII = (
    "sacad",
    "pagador",
    "avalista",
    "cedente",
    "benef",
    "endced",
    "cpf",
    "cnpj",
    "cgc",
    "nome",
    "endereco",
    "logradouro",
    "cep",
    "bairro",
    "municipio",
    "cidade",
    "email",
    "fone",
    "chave",
    "base64",
    "semv",
    "wqrcd",
    "ialias",
    "ilink",
)


@dataclass
class BradescoConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # CPF ou CNPJ (numerico) do beneficiario dono do contrato de cobranca.
    documento_beneficiario: str = field(repr=False)
    agencia: str
    conta_corrente: str
    # idProduto (carteira), ate 2 posicoes, ex.: 9.
    carteira: int
    especie: str = "DM"
    ambiente: str = "producao"
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    # True: boleto com QR Code (produto proprio no portal, com assinatura separada).
    hibrido: bool = False
    # True: o beneficiario cadastrou o webhook (certificado OV/EV enviado ao banco).
    webhook: bool = False
    # Manuais falam em Bearer; collections e o exemplo do OpenAPI mandam o token cru.
    token_sem_bearer: bool = False
    # None = padrao do produto. Convencional: texto "150.00" (manual e collection).
    # O OpenAPI so aceita digitos, mas centavos a um banco que le reais registraria
    # 100 vezes o valor, e texto a um banco que quer digitos e recusado.
    # Hibrido: centavos.
    valor_em_centavos: bool | None = None
    # None = padrao do produto. Convencional: 2 (a descricao do campo manda somar 1 ao
    # numero de dias; FAQ e exemplos dizem 1). Hibrido: 1 (inicia depois de N dias).
    dias_encargos: int | None = None
    # O manual nao fixa a escala de `vlrPagto`: tipo decimal (reais), ao contrario de
    # todos os outros valores da consulta, que sao inteiros em centavos.
    vlr_pagto_em_centavos: bool = False
    # Nosso numero do beneficiario (ate 11 digitos). Sem ele o banco gera, e o registro
    # nao pode ser repetido apos falha de rede: sai com uma tentativa so.
    gerar_nosso_numero: Callable[[Titulo], int] | None = field(default=None, repr=False)
    # Hibrido: txId guardado no registro, para alterar o titulo em outro processo.
    obter_txid: Callable[[str], str | None] | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Bradesco invalido: {self.ambiente!r}")
        if not self.agencia.isdigit() or len(self.agencia) > 4:
            raise Validacao("agencia Bradesco: ate 4 digitos, sem o digito")
        if not self.conta_corrente.isdigit() or len(self.conta_corrente) > 7:
            raise Validacao("conta Bradesco: ate 7 digitos, sem o digito")
        if not 0 < self.carteira <= 99:
            raise Validacao("carteira (idProduto) Bradesco: ate 2 posicoes")
        if self.especie not in ESPECIES:
            raise Validacao(
                f"especie de titulo Bradesco desconhecida: {self.especie!r}"
            )
        if self.dias_encargos is not None and self.dias_encargos < 1:
            raise Validacao("dias_encargos Bradesco: minimo 1")
        try:
            documento = normalizar_documento(self.documento_beneficiario)
        except ValueError as exc:
            raise Validacao("documento do beneficiario invalido") from exc
        if not documento.isdigit():
            raise Validacao(
                "Bradesco: o OpenAPI de cobranca ainda so aceita digitos no "
                "documento do beneficiario"
            )
        self.documento_beneficiario = documento
        if self.valor_em_centavos is None:
            self.valor_em_centavos = self.hibrido
        if self.dias_encargos is None:
            self.dias_encargos = 1 if self.hibrido else 2

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return self.url_base + CAMINHO_TOKEN


class _AuthBradesco(OAuth2ClientCredentials):
    """client_credentials com credenciais no formulario (mTLS na mesma conexao).

    `sem_bearer` manda o token cru no `Authorization`, como as collections oficiais.
    """

    def __init__(self, *, sem_bearer: bool, **kw: Any) -> None:
        super().__init__(**kw)
        self._sem_bearer = sem_bearer

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        token = await self._token(escopo, client)
        return {"Authorization": token if self._sem_bearer else f"Bearer {token}"}


def _sem_acento(valor: str) -> str:
    decomposto = unicodedata.normalize("NFKD", valor)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _texto(valor: str, tamanho: int) -> str:
    """Sem acento e no tamanho do campo, preservando a pontuacao."""
    return " ".join(_sem_acento(valor).split())[:tamanho].strip()


def _alfanumerico(valor: str, tamanho: int) -> str:
    """So letras, digitos e espaco: "." "-" "/" e afins sao recusados pelo banco."""
    limpo = "".join(c if c.isalnum() else " " for c in _sem_acento(valor))
    return " ".join(limpo.split())[:tamanho].strip()


def _decompor(documento: str) -> tuple[str, str, str]:
    """Raiz, filial e controle. CNPJ: 8 + 4 + 2. CPF: 9 + "0" + 2."""
    if len(documento) == 14:
        return documento[:8], documento[8:12], documento[12:]
    return documento[:9], "0", documento[9:]


def _conferir_casas(valor: Decimal, casas: int, campo: str) -> None:
    """O banco trunca o excesso; aqui e recusa, para nao mudar o valor em silencio."""
    if valor != valor.quantize(Decimal(1).scaleb(-casas)):
        raise Validacao(f"bradesco: {campo} aceita ate {casas} casas decimais")


def _centavos(valor: Decimal, campo: str = "valor") -> int:
    _conferir_casas(valor, 2, campo)
    return int((valor * 100).to_integral_value())


def _fmt(dia: date) -> str:
    return dia.strftime("%d.%m.%Y")


def _ddmmaaaa(dia: date) -> int:
    return int(dia.strftime("%d%m%Y"))


def _data_ddmmaaaa(valor: Any) -> date | None:
    """DDMMAAAA como numero ou texto; pode perder o zero a esquerda. 0 = vazio."""
    digitos = "".join(c for c in str(valor or "") if c.isdigit())
    if not digitos or not digitos.strip("0"):
        return None
    texto = digitos.zfill(8)
    try:
        return date(int(texto[4:8]), int(texto[2:4]), int(texto[:2]))
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Bradesco") from exc


def _dinheiro(valor: Any) -> Decimal | None:
    """Valor decimal da resposta. 0 ou vazio = sem pagamento."""
    if valor is None or valor == "":
        return None
    try:
        decimal = Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor ilegivel na resposta do Bradesco") from exc
    return decimal or None


def _centavos_da_resposta(valor: Any) -> int | None:
    """Valores da resposta sao inteiros em centavos; com ponto, reais."""
    if valor is None or valor == "":
        return None
    texto = str(valor)
    try:
        decimal = Decimal(texto)
    except InvalidOperation as exc:
        raise Validacao("valor ilegivel na resposta do Bradesco") from exc
    return int(decimal * 100) if "." in texto else int(decimal)


def _codigo(valor: Any) -> str | None:
    """codStatus como texto canonico ("01" e 1 viram "1")."""
    if valor is None or str(valor).strip() == "":
        return None
    texto = str(valor).strip()
    return str(int(texto)) if texto.isdigit() else texto


def _digitos(valor: Any) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def _codigo_de_barras(valor: Any, linha: str | None) -> str | None:
    """44 digitos, derivados da linha: `cdBarras` vem no padrao de barras EBCDIC."""
    if linha and len(linha) == 47:
        return linha[:4] + linha[32:47] + linha[4:9] + linha[10:20] + linha[21:31]
    texto = str(valor or "")
    return texto if len(texto) == 44 and texto.isdigit() else None


def _sem_pii(valor: Any) -> Any:
    if isinstance(valor, dict):
        saida = {}
        for chave, item in valor.items():
            normal = "".join(c for c in str(chave).lower() if c.isalnum())
            if any(trecho in normal for trecho in _TRECHOS_PII):
                saida[chave] = MASCARA
            else:
                saida[chave] = _sem_pii(item)
        return saida
    if isinstance(valor, list):
        return [_sem_pii(item) for item in valor]
    return valor


def _endereco_obrigatorio(pessoa: Pessoa, papel: str) -> Any:
    if pessoa.endereco is None:
        raise Validacao(f"{papel} sem endereco: o Bradesco exige o endereco")
    return pessoa.endereco


def _exigir_digitos(pessoa: Pessoa, papel: str) -> None:
    if not pessoa.documento.isdigit():
        raise Validacao(
            f"Bradesco: o OpenAPI de cobranca ainda so aceita digitos no {papel}"
        )


def _indicador(pessoa: Pessoa) -> int:
    return 1 if pessoa.tipo == "FISICA" else 2


class BradescoAdapter(BankAdapter):
    banco: ClassVar[str] = "bradesco"
    validado_sandbox: ClassVar[bool] = False
    # HIBRIDO e WEBHOOK dependem da config: a instancia remove o que nao se aplica.
    # Sem PDF: a API nao tem. Protesto e negativacao entram em `enviar_instrucao`.
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO
        | Capacidade.ALTERACAO
        | Capacidade.BAIXA
        | Capacidade.WEBHOOK
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: BradescoConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = hoje_brasil,
    ) -> None:
        self._config = config
        self._hoje = hoje
        # txId dos titulos hibridos registrados por esta instancia.
        self._txids: dict[str, str] = {}
        capacidades = type(self).capacidades
        if not config.hibrido:
            capacidades &= ~Capacidade.HIBRIDO
        else:
            self.tabela_status = TABELA_STATUS_HIBRIDO
        if not config.webhook:
            capacidades &= ~Capacidade.WEBHOOK
        self.capacidades = capacidades
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = _AuthBradesco(
            sem_bearer=config.token_sem_bearer,
            conta=config.conta,
            url_token=config.url_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
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

    # -- montagem ----------------------------------------------------------

    def _negociacao(self) -> int:
        """agencia (4) + conta (7), sem digitos: tudo menos o registro."""
        cfg = self._config
        return int(cfg.agencia.zfill(4) + cfg.conta_corrente.zfill(7))

    def _nu_negociacao(self) -> str:
        """agencia (4) + 7 zeros + conta (7) = 18 posicoes: registro.

        Em texto: 18 digitos nao cabem num numero IEEE sem perder precisao.
        """
        cfg = self._config
        return cfg.agencia.zfill(4) + "0" * 7 + cfg.conta_corrente.zfill(7)

    def _valor(self, valor: Decimal, campo: str = "valor") -> int | str:
        centavos = _centavos(valor, campo)
        return centavos if self._config.valor_em_centavos else f"{valor:.2f}"

    def _percentual(self, valor: Decimal, campo: str) -> str:
        """Texto "2.00"; modo digitos: 3 casas sem ponto, como no FAQ (10% = 10000)."""
        if self._config.valor_em_centavos:
            _conferir_casas(valor, 3, campo)
            return str(int(valor * 1000))
        _conferir_casas(valor, 2, campo)
        return f"{valor:.2f}"

    def _percentual_hibrido(self, valor: Decimal, campo: str) -> str:
        """Manual com QR Code: sempre 5 casas (2% = 2.00000)."""
        _conferir_casas(valor, 5, campo)
        return f"{valor:.5f}"

    @staticmethod
    def _nosso_numero(id_banco: str) -> int | str:
        # Numero quando possivel; id fora do formato vai como veio e o banco recusa.
        return int(id_banco) if id_banco.isdigit() else id_banco

    def _gerado(self, titulo: Titulo) -> int | None:
        gerar = self._config.gerar_nosso_numero
        if gerar is None:
            return None
        numero = gerar(titulo)
        if not 0 < numero <= 99999999999:
            raise Validacao("bradesco: nosso numero aceita ate 11 digitos")
        return numero

    def _pagador(self, pessoa: Pessoa) -> dict[str, Any]:
        end = _endereco_obrigatorio(pessoa, "pagador")
        _exigir_digitos(pessoa, "pagador")
        dados: dict[str, Any] = {
            "nomePagador": _alfanumerico(pessoa.nome, 70),
            "logradouroPagador": _alfanumerico(end.logradouro, 40),
            "nuLogradouroPagador": _alfanumerico(end.numero, 10),
            "bairroPagador": _alfanumerico(end.bairro, 40),
            "municipioPagador": _alfanumerico(end.cidade, 30),
            "ufPagador": end.uf,
            "cepPagador": end.cep[:5],
            "complementoCepPagador": end.cep[5:],
            "cdIndCpfcnpjPagador": _indicador(pessoa),
            "nuCpfcnpjPagador": pessoa.documento,
        }
        if end.complemento:
            dados["complementoLogradouroPagador"] = _alfanumerico(end.complemento, 15)
        if pessoa.email and len(pessoa.email) <= _TAMANHO_EMAIL:
            dados["endEletronicoPagador"] = pessoa.email
        return dados

    @staticmethod
    def _sacador_avalista(pessoa: Pessoa) -> dict[str, Any]:
        _exigir_digitos(pessoa, "avalista")
        dados: dict[str, Any] = {
            "nomeSacadorAvalista": _alfanumerico(pessoa.nome, 40),
            "cdIndCpfcnpjSacadorAvalista": _indicador(pessoa),
            "nuCpfcnpjSacadorAvalista": pessoa.documento,
        }
        end = pessoa.endereco
        if end is not None:
            dados.update(
                {
                    "logradouroSacadorAvalista": _alfanumerico(end.logradouro, 40),
                    "nuLogradouroSacadorAvalista": _alfanumerico(end.numero, 10),
                    "complementoLogradouroSacadorAvalista": _alfanumerico(
                        end.complemento, 15
                    ),
                    "cepSacadorAvalista": end.cep[:5],
                    "complementoCepSacadorAvalista": end.cep[5:],
                    "bairroSacadorAvalista": _alfanumerico(end.bairro, 40),
                    "municipioSacadorAvalista": _alfanumerico(end.cidade, 30),
                    "ufSacadorAvalista": end.uf,
                }
            )
        return dados

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        cfg = self._config
        enc = titulo.encargos
        raiz, filial, controle = _decompor(cfg.documento_beneficiario)
        payload: dict[str, Any] = {
            "nuCPFCNPJ": raiz,
            "filialCPFCNPJ": filial,
            "ctrlCPFCNPJ": controle,
            "idProduto": cfg.carteira,
            "nuNegociacao": self._nu_negociacao(),
            # Mantem a pontuacao: o exemplo oficial usa ".", o hibrido "019690-1-1".
            "nuCliente": _texto(titulo.seu_numero, 25),
            "dtEmissaoTitulo": _fmt(self._hoje()),
            "dtVencimentoTitulo": _fmt(titulo.vencimento),
            "tpVencimento": 0,
            "vlNominalTitulo": self._valor(titulo.valor),
            "cdEspecieTitulo": ESPECIES[cfg.especie],
            **self._pagador(titulo.pagador),
        }
        numero = self._gerado(titulo)
        if numero is not None:
            payload["nuTitulo"] = numero
        # O manual pede os dias junto com o percentual (CBTT0505).
        if enc.multa_percentual:
            payload["percentualMulta"] = self._percentual(enc.multa_percentual, "multa")
            payload["qtdeDiasMulta"] = cfg.dias_encargos
        if enc.juros_mensal_percentual:
            payload["percentualJuros"] = self._percentual(
                enc.juros_mensal_percentual, "juros"
            )
            payload["qtdeDiasJuros"] = cfg.dias_encargos
        if enc.desconto_percentual:
            payload["percentualDesconto1"] = self._percentual(
                enc.desconto_percentual, "desconto"
            )
            payload["dataLimiteDesconto1"] = _fmt(self._limite_desconto(titulo))
        if enc.abatimento:
            payload["vlAbatimento"] = self._valor(enc.abatimento, "abatimento")
        if titulo.dias_baixa_automatica:
            payload["tipoPrazoDecursoTres"] = titulo.dias_baixa_automatica
        if titulo.beneficiario:
            payload.update(self._sacador_avalista(titulo.beneficiario))
        if titulo.mensagens:
            payload["listaMsgs"] = [
                {"mensagem": _texto(m, _TAMANHO_MENSAGEM)}
                for m in titulo.mensagens[:_MAX_MENSAGENS]
            ]
        return payload

    @staticmethod
    def _limite_desconto(titulo: Titulo) -> date:
        dias = titulo.encargos.desconto_dias_antes or 0
        return titulo.vencimento - timedelta(days=dias)

    def _montar_payload_hibrido(self, titulo: Titulo) -> dict[str, Any]:
        """Campos e fixos da collection oficial: o manual pede todos, zerados."""
        cfg = self._config
        enc = titulo.encargos
        pagador = titulo.pagador
        end = _endereco_obrigatorio(pagador, "pagador")
        _exigir_digitos(pagador, "pagador")
        raiz, filial, controle = _decompor(cfg.documento_beneficiario)
        email = pagador.email or ""
        payload: dict[str, Any] = {
            "ctitloCobrCdent": self._gerado(titulo) or 0,
            "registrarTitulo": 1,
            "codUsuario": "APISERVIC",
            "nroCpfCnpjBenef": int(raiz),
            "filCpfCnpjBenef": int(filial),
            "digCpfCnpjBenef": int(controle),
            "tipoAcesso": 2,
            "cpssoaJuridContr": 0,
            "ctpoContrNegoc": 0,
            "nseqContrNegoc": 0,
            "cidtfdProdCobr": cfg.carteira,
            "cnegocCobr": self._nu_negociacao(),
            "filler": "",
            "eNseqContrNegoc": 0,
            "tipoRegistro": 1,
            "codigoBanco": 237,
            "cprodtServcOper": 0,
            "ctitloCliCdent": _texto(titulo.seu_numero, 25),
            "demisTitloCobr": _fmt(self._hoje()),
            "dvctoTitloCobr": _fmt(titulo.vencimento),
            "cidtfdTpoVcto": 0,
            "cindcdEconmMoeda": 6,
            "vnmnalTitloCobr": self._valor(titulo.valor),
            "qmoedaNegocTitlo": 0,
            "cespceTitloCobr": ESPECIES[cfg.especie],
            "cindcdAceitSacdo": "N",
            "ctpoProteTitlo": 0,
            "ctpoPrzProte": 0,
            "ctpoProteDecurs": 0,
            "ctpoPrzDecurs": 0,
            "cctrlPartcTitlo": 0,
            "cformaEmisPplta": 2,
            "cindcdPgtoParcial": "N",
            "qtdePgtoParcial": 0,
            "qtdDecurPrz": titulo.dias_baixa_automatica or 0,
            "codNegativacao": 0,
            "diasNegativacao": 0,
            "filler1": "",
            "ptxJuroVcto": 0,
            "vdiaJuroMora": 0,
            "qdiaInicJuro": 0,
            "pmultaAplicVcto": 0,
            "vmultaAtrsoPgto": 0,
            "qdiaInicMulta": 0,
            "pdescBonifPgto01": 0,
            "vdescBonifPgto01": 0,
            "dlimDescBonif1": "",
            "pdescBonifPgto02": 0,
            "vdescBonifPgto02": 0,
            "dlimDescBonif2": "",
            "pdescBonifPgto03": 0,
            "vdescBonifPgto03": 0,
            "dlimDescBonif3": "",
            "ctpoPrzCobr": 0,
            "pdescBonifPgto": 0,
            "vdescBonifPgto": 0,
            "dlimBonifPgto": "",
            "vabtmtTitloCobr": 0,
            "viofPgtoTitlo": 0,
            "filler2": "",
            "isacdoTitloCobr": _alfanumerico(pagador.nome, 70),
            "elogdrSacdoTitlo": _alfanumerico(end.logradouro, 40),
            "enroLogdrSacdo": _alfanumerico(end.numero, 10),
            "ecomplLogdrSacdo": _alfanumerico(end.complemento, 15),
            "ccepSacdoTitlo": int(end.cep[:5]),
            "ccomplCepSacdo": int(end.cep[5:]),
            "ebairoLogdrSacdo": _alfanumerico(end.bairro, 40),
            "imunSacdoTitlo": _alfanumerico(end.cidade, 30),
            "csglUfSacdo": end.uf,
            "indCpfCnpjSacdo": _indicador(pagador),
            "nroCpfCnpjSacdo": int(pagador.documento),
            "renderEletrSacdo": email if len(email) <= _TAMANHO_EMAIL else "",
            "cdddFoneSacdo": 0,
            "cfoneSacdoTitlo": 0,
            "bancoCentProt": 0,
            "agenciaDvCentPr": 0,
            "isacdrAvalsTitlo": "",
            "elogdrSacdrAvals": "",
            "enroLogdrSacdr": "",
            "ecomplLogdrSacdr": "",
            "ccepSacdrTitlo": 0,
            "ccomplCepSacdr": 0,
            "ebairoLogdrSacdr": "",
            "imunSacdrAvals": "",
            "csglUfSacdr": "",
            "indCpfCnpjSacdr": 0,
            "nroCpfCnpjSacdr": 0,
            "renderEletrSacdr": "",
            "cdddFoneSacdr": 0,
            "cfoneSacdrTitlo": 0,
            "filler3": "0",
            "fase": 1,
            "cindcdCobrMisto": "S",
            # O manual manda deixar em branco: a chave Pix e a do contrato de cobranca.
            "ialiasAdsaoCta": "",
            "iconcPgtoSpi": "",
            "caliasAdsaoCta": "",
            "ilinkGeracQrcd": "",
            "wqrcdPdraoMercd": "",
            "validadeAposVencimento": "",
            "filler4": "",
            "idLoc": "",
        }
        if enc.multa_percentual:
            payload["pmultaAplicVcto"] = self._percentual_hibrido(
                enc.multa_percentual, "multa"
            )
            payload["qdiaInicMulta"] = cfg.dias_encargos
        if enc.juros_mensal_percentual:
            payload["ptxJuroVcto"] = self._percentual_hibrido(
                enc.juros_mensal_percentual, "juros"
            )
            payload["qdiaInicJuro"] = cfg.dias_encargos
        if enc.desconto_percentual:
            payload["pdescBonifPgto01"] = self._percentual_hibrido(
                enc.desconto_percentual, "desconto"
            )
            payload["dlimDescBonif1"] = _fmt(self._limite_desconto(titulo))
            payload["ctpoPrzCobr"] = _PRAZO_DIAS_CORRIDOS
        if enc.abatimento:
            payload["vabtmtTitloCobr"] = self._valor(enc.abatimento, "abatimento")
        if titulo.beneficiario:
            payload.update(self._sacador_hibrido(titulo.beneficiario))
        return payload

    @staticmethod
    def _sacador_hibrido(pessoa: Pessoa) -> dict[str, Any]:
        _exigir_digitos(pessoa, "avalista")
        end = _endereco_obrigatorio(pessoa, "sacador avalista")
        return {
            "isacdrAvalsTitlo": _alfanumerico(pessoa.nome, 40),
            "elogdrSacdrAvals": _alfanumerico(end.logradouro, 40),
            "enroLogdrSacdr": _alfanumerico(end.numero, 10),
            "ecomplLogdrSacdr": _alfanumerico(end.complemento, 15),
            "ccepSacdrTitlo": int(end.cep[:5]),
            "ccomplCepSacdr": int(end.cep[5:]),
            "ebairoLogdrSacdr": _alfanumerico(end.bairro, 40),
            "imunSacdrAvals": _alfanumerico(end.cidade, 40),
            "csglUfSacdr": end.uf,
            "indCpfCnpjSacdr": _indicador(pessoa),
            "nroCpfCnpjSacdr": int(pessoa.documento),
        }

    def _identificacao(self) -> dict[str, Any]:
        """Corpo comum: CPF/CNPJ decomposto em numeros, produto e negociacao."""
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        return {
            "cpfCnpj": {
                "cpfCnpj": int(raiz),
                "filial": int(filial),
                "controle": int(controle),
            },
            "produto": self._config.carteira,
            "negociacao": self._negociacao(),
        }

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Bradesco nao e JSON", status_http=resp.status_code
            ) from exc
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta do Bradesco fora do formato", status_http=resp.status_code
            )
        return dados

    @staticmethod
    def _recusa(dados: dict[str, Any], resp: httpx.Response, motivo: str) -> Validacao:
        """Erro de negocio em HTTP 2xx: o envelope traz `status` e `causa`."""
        status = dados.get("status", dados.get("statusHttp"))
        status_http = status if isinstance(status, int) else resp.status_code
        return Validacao(
            f"bradesco: {motivo}",
            status_http=status_http,
            resposta_bruta=_sem_pii(dados),
        )

    def _conferir_envelope(
        self, dados: dict[str, Any], resp: httpx.Response, motivo: str
    ) -> None:
        status = dados.get("status")
        if isinstance(status, int) and status >= 400:
            raise self._recusa(dados, resp, motivo)

    # -- registro ----------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        hibrido = self._config.hibrido
        corpo = (
            self._montar_payload_hibrido(titulo)
            if hibrido
            else self._montar_payload(titulo)
        )
        # Sem nosso numero proprio, repetir apos timeout pode registrar dois titulos.
        retry = None if self._config.gerar_nosso_numero else SEM_RETRY
        resp = await self._transporte.requisitar(
            "POST",
            CAMINHO_HIBRIDO_REGISTRO if hibrido else CAMINHO_REGISTRO,
            operacao="registrar",
            json=corpo,
            retry=retry,
        )
        dados = self._json(resp)
        campo_id = "ctitloCobrCdent" if hibrido else "nuTituloGerado"
        gerado = dados.get(campo_id)
        if not gerado:
            raise self._recusa(dados, resp, f"registro sem {campo_id}")
        nosso_numero = str(gerado)
        self._conferir_valor_registrado(
            titulo, dados.get("valMoeda10" if hibrido else "vlTitulo"), dados, resp
        )
        txid = dados.get("iconcPgtoSpi") or None
        if txid:
            self._txids[nosso_numero] = txid
        linha = _digitos(dados.get("linhaDig10" if hibrido else "linhaDigitavel"))
        barras = dados.get("codBarras10" if hibrido else "cdBarras")
        return TituloRegistrado(
            banco=self.banco,
            id_banco=nosso_numero,
            status=StatusCanonico.REGISTRADO,
            seu_numero=titulo.seu_numero,
            nosso_numero=nosso_numero,
            linha_digitavel=linha or None,
            codigo_barras=_codigo_de_barras(barras, linha),
            pix_copia_cola=dados.get("wqrcdPdraoMercd") or None,
            txid=txid,
            resposta_bruta=_sem_pii(dados),
        )

    def _conferir_valor_registrado(
        self,
        titulo: Titulo,
        valor_resposta: Any,
        dados: dict[str, Any],
        resp: httpx.Response,
    ) -> None:
        """Defesa do conflito de escala: o banco ecoa o valor em centavos no retorno.

        Divergiu, o titulo existe com o valor errado: erro com o nosso numero, para
        baixar, em vez de um boleto que cobra outro valor.
        """
        registrado = _centavos_da_resposta(valor_resposta)
        if registrado and registrado != _centavos(titulo.valor):
            raise self._recusa(
                dados,
                resp,
                "titulo registrado com valor divergente do pedido; baixe o nosso "
                f"numero {dados.get('nuTituloGerado') or dados.get('ctitloCobrCdent')} "
                "e confira `valor_em_centavos`",
            )

    # -- consulta ----------------------------------------------------------

    async def _buscar(self, id_banco: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Devolve (titulo, resposta completa)."""
        if self._config.hibrido:
            raiz, filial, controle = _decompor(self._config.documento_beneficiario)
            corpo: dict[str, Any] = {
                "cpfCnpjUsuario": int(raiz),
                "filialCnpjUsuario": int(filial),
                "controleCpfCnpjUsuario": int(controle),
                "idProduto": self._config.carteira,
                "contaProduto": self._negociacao(),
                "nomePersonalizado": "",
                "nossoNumero": self._nosso_numero(id_banco),
                "seqTitulo": 0,
                "status": 0,
            }
            caminho = CAMINHO_HIBRIDO_CONSULTA
        else:
            corpo = {
                **self._identificacao(),
                "nossoNumero": self._nosso_numero(id_banco),
                "sequencia": 0,
                "status": 0,
            }
            caminho = CAMINHO_CONSULTA
        resp = await self._transporte.requisitar(
            "POST", caminho, operacao="consultar", json=corpo
        )
        dados = self._json(resp)
        # Convencional: bloco `titulo`. Hibrido: campos na raiz do corpo.
        titulo = dados.get("titulo")
        if not isinstance(titulo, dict):
            titulo = dados if "codStatus" in dados else None
        if titulo is None:
            raise self._recusa(dados, resp, "consulta sem bloco titulo")
        return titulo, dados

    def _valor_pago(self, titulo: dict[str, Any]) -> Decimal | None:
        """`vlrPagto` sem escala oficial: valor longe demais do titulo e recusado."""
        pago = _dinheiro(titulo.get("vlrPagto"))
        if pago is None:
            return None
        if self._config.vlr_pagto_em_centavos:
            pago = (pago / 100).quantize(_CENTAVO)
        nominal = _centavos_da_resposta(
            titulo.get("valMoeda") or titulo.get("valorMoedaBol")
        )
        if nominal:
            razao = pago * 100 / nominal
            parcial = titulo.get("permitePgtoParcial") == "S"
            if razao >= _LIMITE_ESCALA or (
                not parcial and razao <= Decimal(1) / _LIMITE_ESCALA
            ):
                raise Validacao(
                    "bradesco: vlrPagto fora de escala em relacao ao valor do titulo; "
                    "confira `vlr_pagto_em_centavos` antes de liquidar"
                )
        return pago

    def _montar_consultado(
        self, id_banco: str, titulo: dict[str, Any], dados: dict[str, Any]
    ) -> TituloRegistrado:
        status = self.tabela_status.traduzir(_codigo(titulo.get("codStatus")))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            # 13 "PAGO NO DIA" pode vir sem valor e sem data: ficam None.
            valor_pago = self._valor_pago(titulo)
            data_pagamento = _data_ddmmaaaa(titulo.get("dtPagto"))
        linha = _digitos(titulo.get("linhaDig"))
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=titulo.get("snumero"),
            nosso_numero=id_banco,
            linha_digitavel=linha or None,
            codigo_barras=_codigo_de_barras(titulo.get("codBarras"), linha),
            pix_copia_cola=titulo.get("semvQrcode") or None,
            txid=self._txids.get(id_banco),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pii(dados),
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        titulo, dados = await self._buscar(id_banco)
        return self._montar_consultado(id_banco, titulo, dados)

    # -- instrucoes --------------------------------------------------------

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        tipo = instrucao.tipo
        if tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        if tipo is TipoInstrucao.PRORROGACAO:
            if instrucao.nova_data_vencimento is None:
                raise Validacao("bradesco: prorrogacao exige nova_data_vencimento")
            return await self._alterar(
                id_banco, vencimento=instrucao.nova_data_vencimento
            )
        if tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("bradesco: abatimento exige valor")
            return await self._alterar(id_banco, abatimento=instrucao.valor)
        if tipo in (TipoInstrucao.PROTESTO, TipoInstrucao.NEGATIVACAO):
            if self._config.hibrido:
                raise NaoSuportado(
                    f"bradesco: {tipo.value} por instrucao nao faz parte do produto "
                    "com QR Code (docs/bradesco-duvidas.md)"
                )
            return await self._protestar(id_banco, tipo)
        raise NaoSuportado(f"bradesco: instrucao {tipo.value} sem contrato")

    async def _alterar(
        self,
        id_banco: str,
        *,
        vencimento: date | None = None,
        abatimento: Decimal | None = None,
    ) -> TituloRegistrado:
        if not id_banco.isdigit():
            raise Validacao("bradesco: nosso numero deve ser numerico")
        if self._config.hibrido:
            resp = await self._alterar_hibrido(id_banco, vencimento, abatimento)
        else:
            resp = await self._alterar_convencional(id_banco, vencimento, abatimento)
        dados = self._json(resp)
        self._conferir_envelope(dados, resp, "alteracao recusada")
        return await self._conferir_alteracao(id_banco, vencimento, abatimento, dados)

    def _corpo_alteracao_convencional(
        self, id_banco: str, vencimento: date | None, abatimento: Decimal | None
    ) -> dict[str, Any]:
        """O manual pede o layout inteiro, so com o que muda preenchido."""
        documento_vazio = {"cpfCnpj": 0, "filial": 0, "controle": 0}
        return {
            **self._identificacao(),
            "nossoNumero": int(id_banco),
            "dadosPagador": {
                "sacado": "",
                "cpfCnpjSacado": dict(documento_vazio),
                "endereco": "",
                "cep": 0,
                "sufixo": 0,
                "nomeSacador": "",
                "aceite": "",
                "cpfCnpjSacador": dict(documento_vazio),
                "emailSacado": "",
            },
            "dadosTitulo": {
                "seuNumero": "",
                "dataEmissao": 0,
                "especie": "",
                "vencimento": {
                    "dataVencimento": _ddmmaaaa(vencimento) if vencimento else 0,
                    "tipoVencimento": 0,
                },
                "protesto": {"codInstrucaoProtesto": 0, "qtdeDiasProtesto": 0},
                "decurso": {"codDecursoPrazo": 0, "diasDecursoPrazo": 0},
                "abatimento": {
                    "tipoAbatimento": 1 if abatimento else 0,
                    "valorAbatimento": (
                        self._valor(abatimento, "abatimento") if abatimento else 0
                    ),
                },
                "dataDesc1": 0,
                "valDesc1": 0,
                "codValDe1": 0,
                "tipoDesc1": 0,
                "dataDesc2": 0,
                "valDesc2": 0,
                "codValDe2": 0,
                "tipoDesc2": 0,
                "dataDesc3": 0,
                "valDesc3": 0,
                "codValDe3": 0,
                "tipoDesc3": 0,
                "codigoControleParticipante": "",
                "indicadorAvisoSacado": "",
                "comissaoPermanencia": {
                    "diasComissaoPermanencia": 0,
                    "valorComissaoPermanencia": 0,
                    "codigoComissaoPermanencia": 0,
                },
                "codigoMulta": 0,
                "diasMulta": 0,
                "valorMulta": 0,
                "codigoNegativacao": 0,
                "diasNegativacao": 0,
                "pagamentoParcial": "",
                "qtdePagamentoParcial": 0,
            },
        }

    async def _alterar_convencional(
        self, id_banco: str, vencimento: date | None, abatimento: Decimal | None
    ) -> httpx.Response:
        return await self._transporte.requisitar(
            "PUT",
            CAMINHO_ALTERACAO,
            operacao="alterar",
            json=self._corpo_alteracao_convencional(id_banco, vencimento, abatimento),
        )

    def _txid(self, id_banco: str) -> str:
        txid = self._txids.get(id_banco)
        if not txid and self._config.obter_txid:
            txid = self._config.obter_txid(id_banco)
        if not txid:
            raise Validacao(
                "bradesco: a alteracao do boleto com QR Code exige o txId do registro; "
                "informe `obter_txid` (TituloRegistrado.txid)"
            )
        return txid

    async def _alterar_hibrido(
        self, id_banco: str, vencimento: date | None, abatimento: Decimal | None
    ) -> httpx.Response:
        txid = self._txid(id_banco)
        # A rota exige a data de vencimento (a original, se nao muda) e o manual mostra
        # o valor do titulo no corpo: os dois vem da consulta.
        titulo, _ = await self._buscar(id_banco)
        data = vencimento or _data_barra(titulo.get("dataVencto"))
        if data is None:
            raise Validacao("bradesco: consulta sem data de vencimento")
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        ja_tem_abatimento = bool(_centavos_da_resposta(titulo.get("valAbat")))
        corpo = {
            "codUsuario": "OPENAPI",
            "vnmnalTitloCobr": _centavos_da_resposta(titulo.get("valMoeda")) or 0,
            "chave": {
                "cnpjCpf": int(raiz),
                "filial": int(filial),
                "controle": controle,
                "idprod": self._config.carteira,
                "ctaprod": self._negociacao(),
                "nossoNumero": id_banco,
            },
            "dadosTitulo": {
                "seuNumero": "",
                "dataEmissao": 0,
                "especie": "",
                "dataVencimento": _ddmmaaaa(data),
                "codVencimento": 0,
                "codInstrucaoProtesto": 0,
                "diasProtesto": 0,
                "codDecurso": 0,
                "diasDecurso": 0,
                # 1 = incluir, 3 = alterar.
                "codAbatimento": (3 if ja_tem_abatimento else 1) if abatimento else 0,
                "valorAbatimentoTitulo": (
                    self._valor(abatimento, "abatimento") if abatimento else 0
                ),
                "dataPrimeiroDesc": 0,
                "valorPrimeiroDesc": 0,
                "codPrimeiroDesc": 0,
                "acaoPrimeiroDesc": 0,
                "dataSegundoDesc": 0,
                "valorSegundoDesc": 0,
                "codSegundoDesc": 0,
                "acaoSegundoDesc": 0,
                "dataTerceiroDesc": 0,
                "valorTerceiroDesc": 0,
                "codTerceiroDesc": 0,
                "acaoTerceiroDesc": 0,
                "controleParticipante": "",
                "idAvisoSacado": "S",
                "diasAposVencidoJuros": 0,
                "valorJuros": 0,
                "codJuros": 0,
                "diasAposVencimentoMulta": 0,
                "valorMulta": 0,
                "codMulta": 0,
                "codNegativacao": 0,
                "diasNegativacao": 0,
                "codPagamentoParcial": "N",
                "qtdePagamentosParciais": 0,
                "sacado": "",
                "cgcCpfSacado": "0",
                "endereco": "",
                "cep": 0,
                "cepSuf": 0,
                "sacadorAvalista": "",
                "aceite": "0",
                "cgcCpfAvalista": "0",
            },
        }
        return await self._transporte.requisitar(
            "POST",
            CAMINHO_HIBRIDO_ALTERACAO,
            operacao="alterar",
            json=corpo,
            cabecalhos={"txId": txid},
        )

    async def _conferir_alteracao(
        self,
        id_banco: str,
        vencimento: date | None,
        abatimento: Decimal | None,
        dados_alteracao: dict[str, Any],
    ) -> TituloRegistrado:
        """A resposta so diz "alteracao efetuada": a consulta prova o que ficou gravado.

        E a defesa contra escala errada no abatimento, que nao e recusada pelo banco
        enquanto couber no valor do titulo.
        """
        try:
            titulo, dados = await self._buscar(id_banco)
        except Indisponivel:
            return TituloRegistrado(
                banco=self.banco,
                id_banco=id_banco,
                status=StatusCanonico.REGISTRADO,
                nosso_numero=id_banco,
                resposta_bruta=_sem_pii(dados_alteracao),
            )
        if vencimento is not None:
            gravada = _data_barra(titulo.get("dataVencto"))
            if gravada is not None and gravada != vencimento:
                raise Validacao(
                    "bradesco: alteracao aceita, mas a consulta traz outro vencimento",
                    resposta_bruta=_sem_pii(dados),
                )
        if abatimento is not None:
            gravado = _centavos_da_resposta(titulo.get("valAbat"))
            if gravado is not None and gravado != _centavos(abatimento, "abatimento"):
                raise Validacao(
                    "bradesco: alteracao aceita, mas a consulta traz outro abatimento; "
                    "confira `valor_em_centavos`",
                    resposta_bruta=_sem_pii(dados),
                )
        return self._montar_consultado(id_banco, titulo, dados)

    async def _protestar(self, id_banco: str, tipo: TipoInstrucao) -> TituloRegistrado:
        """Tipos do OpenAPI e do exemplo de sucesso da collection: tudo em texto.

        Funcoes: 1 + P = protesto comum; 3 = negativacao (parametro em branco).
        O manual cita so 1 e 2; a funcao 3 esta no OpenAPI e no nome do produto.
        """
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        protesto = tipo is TipoInstrucao.PROTESTO
        corpo = {
            "cpfCnpj": {
                "cpfCnpj": raiz.zfill(9),
                "filial": filial,
                "controle": controle,
            },
            "codigoProduto": str(self._config.carteira),
            "contaProduto": str(self._negociacao()).zfill(11),
            "nossoNumero": id_banco,
            "codigoFuncao": "1" if protesto else "3",
            "parmFuncao": "P" if protesto else "",
        }
        resp = await self._transporte.requisitar(
            "POST", CAMINHO_PROTESTO, operacao=tipo.value.lower(), json=corpo
        )
        dados = self._json(resp)
        self._conferir_envelope(dados, resp, f"{tipo.value.lower()} recusado")
        # O comando e processado a noite: `situacaoAtual` pode vir 0 no sucesso.
        status = self.tabela_status.mapa.get(
            _codigo(dados.get("situacaoAtual")) or "", StatusCanonico.REGISTRADO
        )
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            nosso_numero=id_banco,
            resposta_bruta=_sem_pii(dados),
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        # Tipos do OpenAPI: cpfCnpj em texto, o resto em numero.
        corpo = {
            "cpfCnpj": {"cpfCnpj": raiz, "filial": filial, "controle": controle},
            "produto": self._config.carteira,
            "negociacao": self._negociacao(),
            "nossoNumero": self._nosso_numero(id_banco),
            "sequencia": 0,
            "codigoBaixa": CODIGO_BAIXA_PEDIDO,
        }
        resp = await self._transporte.requisitar(
            "POST", CAMINHO_BAIXA, operacao="baixar", json=corpo
        )
        dados = self._json(resp)
        self._conferir_envelope(dados, resp, "baixa recusada")
        interno = dados.get("dados")
        codigo = _codigo(interno.get("status")) if isinstance(interno, dict) else None
        # 57 no sucesso; 11 "COM BAIXA SOLICITADA" deixa o titulo em aberto.
        status = self.tabela_status.mapa.get(codigo or "", StatusCanonico.BAIXADO)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            nosso_numero=id_banco,
            resposta_bruta=_sem_pii(dados),
        )

    # -- webhook -----------------------------------------------------------

    async def cadastrar_webhook(
        self, url: str = "", *, operacao: str = "I", ativo: bool = True
    ) -> dict[str, Any]:
        """Cadastro do aviso de pagamento: I inclui, A altera, C consulta, E exclui.

        Vale para os dois produtos. Em producao, o banco so envia depois de receber o
        certificado publico SSL por e-mail (docs/bradesco-duvidas.md).
        """
        self.exigir(Capacidade.WEBHOOK)
        if operacao not in ("I", "A", "C", "E"):
            raise Validacao("bradesco: operacao de webhook deve ser I, A, C ou E")
        if operacao in ("I", "A") and (
            not url.startswith("https://") or len(url) > _TAMANHO_URL
        ):
            raise Validacao("bradesco: urlEnvio deve ser https com ate 100 caracteres")
        raiz, filial, controle = _decompor(self._config.documento_beneficiario)
        corpo = {
            "documento": {"cpfCnpj": raiz, "filial": filial, "controle": controle},
            "versaoLayout": "1",
            "tipoCadastro": operacao,
            "utilizaWebhook": "S" if ativo else "N",
            "urlEnvio": url,
            "tipoAviso": "1",
        }
        caminho = (
            CAMINHO_WEBHOOK_SANDBOX
            if self._config.ambiente == "sandbox"
            else CAMINHO_WEBHOOK_PRODUCAO
        )
        resp = await self._transporte.requisitar(
            "POST", caminho, operacao="webhook", json=corpo
        )
        dados = self._json(resp)
        self._conferir_envelope(dados, resp, "cadastro de webhook recusado")
        return _sem_pii(dados)

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Aviso de pagamento em evento para reconsulta.

        O layout oficial nao tem assinatura nem token, e o manual so pede TLS e o
        certificado do banco como confiavel. Sem autenticidade provada aqui, o valor
        do aviso NAO vai para `valor_pago`: quem recebe consulta o titulo e so entao
        liquida. Aviso de outra carteira ou negociacao e recusado.
        """
        self.exigir(Capacidade.WEBHOOK)
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("aviso do Bradesco nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        eventos = []
        for item in itens:
            if not isinstance(item, dict):
                raise Validacao("aviso do Bradesco fora do formato")
            if item.get("tipoEvento") != "P":
                continue
            eventos.append(self._evento_de_pagamento(item))
        return eventos

    def _evento_de_pagamento(self, item: dict[str, Any]) -> EventoCanonico:
        nosso = str(item.get("nossoNumero") or "").strip()
        if not nosso:
            raise Validacao("aviso do Bradesco sem nosso numero")
        if _digitos(item.get("produto")).lstrip("0") != str(
            self._config.carteira
        ) or _digitos(item.get("negociacao")).lstrip("0") != str(
            self._negociacao()
        ).lstrip("0"):
            raise Validacao("aviso do Bradesco de outra carteira ou negociacao")
        return EventoCanonico(
            banco=self.banco,
            referencia=str(int(nosso)) if nosso.isdigit() else nosso,
            tipo=StatusCanonico.LIQUIDADO,
            ocorrido_em=_momento(item.get("dataPagamento"), item.get("horaPagamento")),
            resposta_bruta=_sem_pii(item),
        )


def _data_barra(valor: Any) -> date | None:
    """DD/MM/AAAA da consulta; vazio = sem data."""
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor), "%d/%m/%Y").date()
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Bradesco") from exc


def _momento(data: Any, hora: Any) -> datetime | None:
    """Data AAAA-MM-DD e hora HHMMSS do aviso; ilegivel nao derruba o aviso."""
    try:
        return datetime.strptime(f"{data} {hora}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return None
