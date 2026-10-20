"""Unicred (136), API de Cobranca v2 (titulos de cobranca, portal Sensedia).

ESTADO: `validado_sandbox = False`. Nada aqui rodou contra a Unicred.

Fonte do contrato: as paginas do portal oficial "Unicred Developers"
(autenticacao, emissao, consulta, PDF, baixa e webhook), que exigem login e
foram lidas em copias salvas publicadas em repositorio de terceiros, conferidas
com dois clientes de terceiros (`eduardokum/laravel-boleto`, MIT, e
`guilhermemcr1/unicred-php-sdk`). Os vetores de teste de criptografia do
webhook publicados no portal decifram com a implementacao abaixo. Nao ha modulo
Odoo de origem nem codigo portado. Duvidas em `docs/unicred-duvidas.md`.

Escolhas deliberadas:

- Autenticacao: `POST /oauth2/v2/grant-token` com JSON `nomeUsuario`/`senha` e
  header `apiKey` (a "API key + usuario" do PRD). Toda chamada leva `apiKey`,
  `cooperativa` e `Authorization: Bearer`. O token responde `accessToken` e
  `tokenExpirationTime`, fora do formato OAuth2 da `brbank.auth`: por isso o
  autenticador fica neste modulo.
- Emissao assincrona: o POST devolve so o `id_titulo` (texto puro); o registro
  na CIP termina depois. `registrar_titulo` devolve `PENDENTE_REGISTRO` e o
  `id_titulo` e o `id_banco` de tudo que vem depois.
- Nao enviamos `nossoNumero`: o portal recomenda deixar o banco gerar.
- Sem encargo no titulo, juros e multa vao como isentos (`5` e `3`) para nao
  herdar o padrao do cadastro do cooperado sem que o chamador saiba.
- A baixa e uma instrucao assincrona (201 + `codigoInstrucao`); o resultado
  final vem da consulta ou do webhook.
- Webhook cifrado (AES-256-CBC, PKCS5, base64), autenticado pelo `x-api-key`.
- Status desconhecido levanta `Validacao`, nunca vira "pago".
"""

import asyncio
import base64
import binascii
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as hora
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from brbank.adapter import BankAdapter
from brbank.auth import Autenticador
from brbank.capacidades import Capacidade
from brbank.erros import Indisponivel, NaoSuportado, Validacao
from brbank.mascara import MASCARA, mascarar
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

URL_PRODUCAO = "https://api.e-unicred.com.br"
URL_SANDBOX = "https://api.e-unicred.com.br/homolog"
CAMINHO_TOKEN = "oauth2/v2/grant-token"

# Campo `status` da consulta (`GET .../titulos/{id}/status`).
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "EM_PROCESSAMENTO": StatusCanonico.PENDENTE_REGISTRO,
        "ABERTO": StatusCanonico.REGISTRADO,
        "LIQUIDADO": StatusCanonico.LIQUIDADO,
        "BAIXADO": StatusCanonico.BAIXADO,
    },
)

# Campo `titulo.codigoSituacao` do webhook. `07` (titulo deletado) fica fora de
# proposito: nao ha como saber se equivale a baixa (docs/unicred-duvidas.md).
TABELA_SITUACAO_WEBHOOK = TabelaStatus.criar(
    "2026.10",
    {
        "01": StatusCanonico.REGISTRADO,
        "02": StatusCanonico.BAIXADO,
        "03": StatusCanonico.LIQUIDADO,
        "04": StatusCanonico.LIQUIDADO,
        "05": StatusCanonico.BAIXADO,
        "06": StatusCanonico.BAIXADO,
        "08": StatusCanonico.BAIXADO,
        "99": StatusCanonico.PENDENTE_REGISTRO,
        "100": StatusCanonico.PENDENTE_REGISTRO,
        "101": StatusCanonico.REJEITADO,
    },
)
# `codigoMovimento` que o portal chama de liquidacao (o retorno "Parcial"):
# pago em cartorio, liquidacao normal, em condicional e de titulo descontado.
_MOVIMENTOS_LIQUIDACAO = frozenset({"01", "06", "07", "09"})
_MOVIMENTO_REJEITADO = "03"
_INSTRUCAO_REMESSA = "01"

_JUROS_TAXA_MENSAL = "3"
_JUROS_ISENTO = "5"
_MULTA_PERCENTUAL = "2"
_MULTA_ISENTA = "3"
_DESCONTO_VALOR_FIXO = 1
_BAIXA_PELO_BENEFICIARIO = "SOLICITACAO_CEDENTE"

# Limites do dicionario de emissao do portal.
_MAX_SEU_NUMERO = 15
_MAX_NOME = 40
_MAX_FANTASIA = 256
_MAX_LOGRADOURO = 60
_MAX_NUMERO = 5
_MAX_COMPLEMENTO = 30
_MAX_BAIRRO = 40
_MAX_CIDADE = 50
_MAX_MENSAGENS = 6
_MAX_MENSAGEM = 80
_ID_TITULO = re.compile(r"[0-9A-Za-z-]{1,64}")
_CENTAVO = Decimal("0.01")


@dataclass
class UnicredConfig:
    """Credenciais e identificacao do beneficiario na Unicred.

    `api_key` e o token da APP criada no portal; `usuario`/`senha` vem da
    Unicred; `id_beneficiario` (UUID do beneficiario) e `variacao_carteira`
    vem da planilha de homologacao/producao; `cooperativa` e o codigo da
    cooperativa (header `cooperativa`). As chaves do webhook sao os dois UUIDs
    que o proprio cooperado cadastra no CobExpress.
    """

    conta: str
    api_key: str = field(repr=False)
    usuario: str
    senha: str = field(repr=False)
    cooperativa: str
    id_beneficiario: str
    variacao_carteira: int
    ambiente: str = "producao"
    chave_autenticacao_webhook: str | None = field(default=None, repr=False)
    chave_criptografia_webhook: str | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        aut, cripto = self.chave_autenticacao_webhook, self.chave_criptografia_webhook
        if aut is None and cripto is None:
            return
        if not aut or not cripto:
            raise Validacao(
                "webhook Unicred exige as duas chaves (autenticacao e cripto)"
            )
        if len(aut) < 16 or len(cripto) < 32:
            raise Validacao("chaves do webhook Unicred: use os UUIDs completos")
        if aut == cripto:
            raise Validacao("chaves do webhook Unicred nao podem ser iguais")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO


@dataclass(frozen=True)
class _Token:
    valor: str
    expira_em: float

    def __repr__(self) -> str:
        return "_Token(***)"


class _TokenUnicred(Autenticador):
    """Token da Unicred (grant `password` em JSON, header `apiKey`), com cache.

    Um token por conta, renovado com margem antes de `tokenExpirationTime`.
    O `refreshToken` nao e usado: pedir um token novo tem o mesmo efeito e
    evita um segundo endpoint nao documentado na pagina de autenticacao.
    """

    def __init__(
        self,
        *,
        conta: str,
        url_token: str,
        usuario: str,
        senha: str,
        api_key: str,
        margem_s: float = 60.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conta = conta
        self._url_token = url_token
        self._usuario = usuario
        self._senha = senha
        self._api_key = api_key
        self._margem = margem_s
        self._relogio = relogio
        self._token: _Token | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"_TokenUnicred(conta={self.conta!r})"

    async def cabecalhos(
        self, escopo: str | None, client: httpx.AsyncClient
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._valor(client)}"}

    async def invalidar(self, escopo: str | None) -> None:
        self._token = None

    async def _valor(self, client: httpx.AsyncClient) -> str:
        atual = self._token
        if atual and atual.expira_em > self._relogio():
            return atual.valor
        async with self._lock:
            atual = self._token
            if atual and atual.expira_em > self._relogio():
                return atual.valor
            self._token = await self._pedir(client)
            return self._token.valor

    async def _pedir(self, client: httpx.AsyncClient) -> _Token:
        try:
            resp = await client.post(
                self._url_token,
                json={"nomeUsuario": self._usuario, "senha": self._senha},
                headers={"apiKey": self._api_key, "Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise Indisponivel(
                f"erro de transporte ao obter token: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise Indisponivel(
                "Unicred indisponivel ao obter token",
                status_http=resp.status_code,
                resposta_bruta=resp.text,
            )
        if resp.status_code >= 400:
            raise Validacao(
                "Unicred recusou as credenciais (usuario, senha ou apiKey)",
                status_http=resp.status_code,
                resposta_bruta=resp.text,
            )
        try:
            corpo = resp.json()
            valor = corpo["accessToken"]
            duracao = int(corpo.get("tokenExpirationTime") or 300)
        except (ValueError, KeyError, TypeError) as exc:
            raise Validacao(
                "resposta de token da Unicred sem accessToken",
                status_http=resp.status_code,
            ) from exc
        if not valor:
            raise Validacao("resposta de token da Unicred sem accessToken")
        vida = max(duracao - self._margem, self._margem)
        return _Token(str(valor), self._relogio() + vida)


def _numero(valor: Decimal) -> float:
    # A API exige numero JSON; a conversao fica restrita a fronteira.
    return float(valor)


def _texto(valor: str, limite: int) -> str:
    return valor.strip()[:limite]


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def _data(valor: Any) -> date | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _json(resp: httpx.Response) -> Any:
    try:
        return json.loads(resp.content, parse_float=Decimal)
    except ValueError as exc:
        raise Validacao(
            "resposta da Unicred nao e JSON", status_http=resp.status_code
        ) from exc


def _sem_pagador(valor: Any) -> Any:
    """Tira o bloco `pagador` (o `mascarar` nao conhece `nomeRazaoSocial`)."""
    if isinstance(valor, dict):
        return {
            chave: MASCARA if chave == "pagador" else _sem_pagador(item)
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sem_pagador(item) for item in valor]
    return valor


def decifrar_webhook(
    corpo: bytes | str, chave_autenticacao: str, chave_criptografia: str
) -> bytes:
    """Decifra o corpo do webhook como o portal descreve.

    AES-256-CBC com PKCS5, texto em base64; IV = 16 primeiros caracteres da
    chave de autenticacao (a do `x-api-key`), chave = 32 primeiros caracteres
    da chave de criptografia.
    """
    try:
        texto = corpo.decode() if isinstance(corpo, bytes) else corpo
        cifra = Cipher(
            algorithms.AES(chave_criptografia[:32].encode()),
            modes.CBC(chave_autenticacao[:16].encode()),
        ).decryptor()
        bloco = cifra.update(base64.b64decode(texto.strip(), validate=True))
        bloco += cifra.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(bloco) + unpadder.finalize()
    except (binascii.Error, ValueError) as exc:
        raise Validacao("webhook da Unicred ilegivel (chave errada?)") from exc


def _pagador(pessoa: Pessoa) -> dict[str, Any]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao("pagador sem endereco: a Unicred exige endereco do pagador")
    numero = endereco.numero.strip()
    if len(numero) > _MAX_NUMERO:
        raise Validacao(
            f"unicred: numero do endereco do pagador passa de {_MAX_NUMERO} caracteres"
        )
    juridica = len(pessoa.documento) == 14
    dados: dict[str, Any] = {
        "nomeRazaoSocial": _texto(pessoa.nome, _MAX_NOME),
        "tipoPessoa": "J" if juridica else "F",
        "tipoDocumento": "CNPJ" if juridica else "CPF",
        "numeroDocumento": pessoa.documento,
    }
    if juridica:
        # Obrigatorio para pessoa juridica; o modelo canonico nao tem nome
        # fantasia, entao vai a razao social (docs/unicred-duvidas.md).
        dados["nomeFantasia"] = _texto(pessoa.nome, _MAX_FANTASIA)
    if pessoa.email:
        dados["email"] = pessoa.email.strip()
    end: dict[str, Any] = {"logradouro": _texto(endereco.logradouro, _MAX_LOGRADOURO)}
    if numero:
        end["numero"] = numero
    if endereco.complemento.strip():
        end["complemento"] = _texto(endereco.complemento, _MAX_COMPLEMENTO)
    if endereco.bairro.strip():
        end["bairro"] = _texto(endereco.bairro, _MAX_BAIRRO)
    end["cidade"] = _texto(endereco.cidade, _MAX_CIDADE)
    end["uf"] = endereco.uf
    end["cep"] = endereco.cep
    dados["endereco"] = end
    return dados


def _pagamento(dados: dict[str, Any]) -> tuple[Decimal | None, date | None]:
    """Valor e data do pagamento do grupo `pagamentos` da consulta.

    O portal mostra o grupo de forma ambigua (objeto ou lista): aceita os dois.
    Com mais de um pagamento, soma os valores e fica com a data mais recente.
    """
    bruto = dados.get("pagamentos")
    itens = bruto if isinstance(bruto, list) else [bruto] if bruto else []
    valores, datas = [], []
    for item in itens:
        if not isinstance(item, dict):
            continue
        valor = _dinheiro(item.get("valor"))
        if valor is not None:
            valores.append(valor)
        quando = _data(item.get("data"))
        if quando is not None:
            datas.append(quando)
    return (sum(valores, Decimal(0)) if valores else None), (
        max(datas) if datas else None
    )


class UnicredAdapter(BankAdapter):
    banco: ClassVar[str] = "unicred"
    validado_sandbox: ClassVar[bool] = False
    capacidades: ClassVar[Capacidade] = (
        Capacidade.BAIXA
        | Capacidade.PDF
        | Capacidade.WEBHOOK
        | Capacidade.EMISSAO_ASSINCRONA
        | Capacidade.INSTRUCAO_ASSINCRONA
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: UnicredConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._config = config
        auth = _TokenUnicred(
            conta=config.conta,
            url_token=f"{config.url_base}/{CAMINHO_TOKEN}",
            usuario=config.usuario,
            senha=config.senha,
            api_key=config.api_key,
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

    def __repr__(self) -> str:
        return f"UnicredAdapter(conta={self._config.conta!r})"

    async def aclose(self) -> None:
        await self._transporte.aclose()

    def _cabecalhos(self) -> dict[str, str]:
        return {"cooperativa": self._config.cooperativa, "apiKey": self._config.api_key}

    def _titulos(self) -> str:
        return f"cobranca/v2/beneficiarios/{self._config.id_beneficiario}/titulos"

    def _caminho(self, id_banco: str, sufixo: str = "") -> str:
        if not _ID_TITULO.fullmatch(id_banco or ""):
            raise Validacao("unicred: id do titulo invalido")
        return f"{self._titulos()}/{id_banco}{sufixo}"

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        if len(titulo.seu_numero) > _MAX_SEU_NUMERO:
            raise Validacao(f"unicred: seuNumero passa de {_MAX_SEU_NUMERO} caracteres")
        enc = titulo.encargos
        if enc.abatimento is not None:
            raise Validacao("unicred: abatimento nao tem campo na emissao")
        if titulo.dias_baixa_automatica is not None:
            raise Validacao("unicred: baixa automatica por dias nao tem campo na API")
        apos_vencimento = (titulo.vencimento + timedelta(days=1)).isoformat()
        payload: dict[str, Any] = {
            "beneficiarioVariacaoCarteira": self._config.variacao_carteira,
            "seuNumero": titulo.seu_numero,
            "valor": _numero(titulo.valor),
            "vencimento": titulo.vencimento.isoformat(),
            "pagador": _pagador(titulo.pagador),
        }
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            # A Unicred so aceita desconto em valor fixo ate a data limite.
            valor = (titulo.valor * enc.desconto_percentual / 100).quantize(
                _CENTAVO, rounding=ROUND_HALF_UP
            )
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            payload["desconto"] = {
                "indicador": _DESCONTO_VALOR_FIXO,
                "dataLimite": limite.isoformat(),
                "valor": _numero(valor),
            }
        if enc.juros_mensal_percentual is not None:
            payload["juros"] = {
                "codigo": _JUROS_TAXA_MENSAL,
                "dataInicio": apos_vencimento,
                "valor": _numero(enc.juros_mensal_percentual),
            }
        else:
            payload["juros"] = {"codigo": _JUROS_ISENTO}
        if enc.multa_percentual is not None:
            payload["multa"] = {
                "codigo": _MULTA_PERCENTUAL,
                "dataInicio": apos_vencimento,
                "valor": _numero(enc.multa_percentual),
            }
        else:
            payload["multa"] = {"codigo": _MULTA_ISENTA}
        if titulo.mensagens:
            payload["mensagensFichaCompensacao"] = [
                _texto(linha, _MAX_MENSAGEM)
                for linha in titulo.mensagens[:_MAX_MENSAGENS]
            ]
        return payload

    @staticmethod
    def _id_emitido(resp: httpx.Response) -> str:
        """`id_titulo` da emissao: texto puro ou, segundo o portal, JSON."""
        texto = resp.text.strip()
        if texto.startswith("{"):
            dados = _json(resp)
            texto = str(dados.get("id_titulo") or "") if isinstance(dados, dict) else ""
        texto = texto.strip().strip('"')
        if not _ID_TITULO.fullmatch(texto):
            raise Validacao(
                "resposta da Unicred sem id_titulo",
                status_http=resp.status_code,
                resposta_bruta=resp.text,
            )
        return texto

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            self._titulos(),
            operacao="registrar",
            json=self._montar_payload(titulo),
            cabecalhos=self._cabecalhos(),
        )
        id_banco = self._id_emitido(resp)
        # A Unicred so confirma o recebimento: o registro na CIP termina depois.
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.PENDENTE_REGISTRO,
            seu_numero=titulo.seu_numero,
            resposta_bruta={"id_titulo": id_banco},
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "GET",
            self._caminho(id_banco, "/status"),
            operacao="consultar",
            cabecalhos={**self._cabecalhos(), "cache-control": "no-cache"},
        )
        # 202 sem corpo: o titulo ainda esta em processamento.
        if resp.status_code == 202 or not resp.content.strip():
            return TituloRegistrado(
                banco=self.banco,
                id_banco=id_banco,
                status=StatusCanonico.PENDENTE_REGISTRO,
            )
        dados = _json(resp)
        if not isinstance(dados, dict):
            raise Validacao(
                "resposta da Unicred fora do formato", status_http=resp.status_code
            )
        status = self.tabela_status.traduzir(dados.get("status"))
        valor_pago, data_pagamento = None, None
        # Em ABERTO o grupo `pagamentos` so aparece em titulo de multiplos
        # pagamentos (especie 31): nao e liquidacao.
        if status is StatusCanonico.LIQUIDADO:
            valor_pago, data_pagamento = _pagamento(dados)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            nosso_numero=dados.get("nossoNumero"),
            linha_digitavel=dados.get("linhaDigitavel"),
            codigo_barras=dados.get("codBarras"),
            pix_copia_cola=dados.get("qrCodePix"),
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=_sem_pagador(dados),
        )

    async def enviar_instrucao(
        self, id_banco: str, instrucao: Instrucao
    ) -> TituloRegistrado:
        if instrucao.tipo is TipoInstrucao.BAIXA:
            return await self.baixar_titulo(id_banco)
        # Alteracao de vencimento so aparece em cliente de terceiros, nao nas
        # paginas oficiais lidas: ALTERACAO fica sem declarar.
        raise NaoSuportado(
            f"unicred nao declara ALTERACAO (instrucao {instrucao.tipo.value})"
        )

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        """Pede a baixa (`SOLICITACAO_CEDENTE`, sem justificativa).

        A Unicred aceita a instrucao (201) e a processa depois: a consulta pode
        mostrar o titulo ABERTO por um tempo. O resultado final vem da consulta
        ou do webhook.
        """
        resp = await self._transporte.requisitar(
            "POST",
            self._caminho(id_banco, "/baixa"),
            operacao="baixar",
            json={"tipo": _BAIXA_PELO_BENEFICIARIO},
            cabecalhos=self._cabecalhos(),
        )
        bruta: dict[str, Any] = {}
        if resp.content.strip():
            dados = _json(resp)
            if isinstance(dados, dict):
                bruta = dados
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            resposta_bruta=bruta,
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        """PDF do boleto, gerado sob demanda enquanto o titulo esta ABERTO."""
        resp = await self._transporte.requisitar(
            "GET",
            self._caminho(id_banco),
            operacao="pdf",
            cabecalhos={**self._cabecalhos(), "cache-control": "no-cache"},
        )
        if resp.status_code == 202:
            raise Indisponivel("unicred: PDF ainda em processamento", status_http=202)
        if not resp.content.startswith(b"%PDF"):
            raise Validacao(
                "resposta da Unicred nao contem PDF", status_http=resp.status_code
            )
        return resp.content

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        """Webhook de retorno da Unicred: um movimento cifrado por chamada.

        Quando `cabecalhos` vem, o `x-api-key` tem de ser a chave de
        autenticacao configurada. O chamador responde 202 com um UUID em texto
        puro em ate 5 segundos e deduplica por `uuidRequisicaoWebhook` (fica
        na `resposta_bruta`). O evento nao confirma pagamento: reconsulte.
        """
        cfg = self._config
        aut = cfg.chave_autenticacao_webhook
        if not aut or not cfg.chave_criptografia_webhook:
            raise Validacao("unicred: configure as chaves do webhook")
        if not isinstance(corpo, bytes | str):
            raise Validacao("webhook da Unicred chega cifrado, em texto")
        if cabecalhos is not None:
            recebida = {k.lower(): v for k, v in cabecalhos.items()}.get("x-api-key")
            if not recebida or not hmac.compare_digest(recebida, aut):
                raise Validacao("webhook da Unicred com x-api-key desconhecido")
        try:
            claro = decifrar_webhook(corpo, aut, cfg.chave_criptografia_webhook)
            dados = json.loads(claro, parse_float=Decimal)
        except ValueError as exc:
            raise Validacao("webhook da Unicred nao e JSON") from exc
        if not isinstance(dados, dict):
            raise Validacao("webhook da Unicred fora do formato")
        titulo = dados.get("titulo")
        if not isinstance(titulo, dict) or not titulo.get("uuidTitulo"):
            raise Validacao("webhook da Unicred sem titulo.uuidTitulo")
        movimento = str(dados.get("codigoMovimento") or "")
        instrucao = str(dados.get("codigoInstrucaoOrigem") or "")
        pagamento = dados.get("pagamento")
        pagamento = pagamento if isinstance(pagamento, dict) else {}
        valor_pago, ocorrido = None, None
        if movimento in _MOVIMENTOS_LIQUIDACAO:
            tipo = StatusCanonico.LIQUIDADO
            valor_pago = _dinheiro(pagamento.get("valorRecebido"))
            quando = _data(pagamento.get("dataLiquidacao"))
            ocorrido = datetime.combine(quando, hora()) if quando else None
        elif movimento == _MOVIMENTO_REJEITADO and instrucao == _INSTRUCAO_REMESSA:
            tipo = StatusCanonico.REJEITADO
        else:
            tipo = TABELA_SITUACAO_WEBHOOK.traduzir(titulo.get("codigoSituacao"))
        return [
            EventoCanonico(
                banco=self.banco,
                referencia=str(titulo["uuidTitulo"]),
                tipo=tipo,
                ocorrido_em=ocorrido,
                valor_pago=valor_pago,
                resposta_bruta=mascarar(_sem_pagador(dados)),
            )
        ]
