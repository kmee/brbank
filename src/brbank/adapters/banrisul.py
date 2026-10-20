"""Banrisul (041), API Cobranca v1 (OAuth2 client_credentials, boleto hibrido).

ESTADO: `validado_sandbox = False`. O contrato foi montado a partir da
especificacao OpenAPI oficial publicada no portal de desenvolvedores do
Banrisul (API Cobranca 1.6.1, `developers.banrisul.com.br`), com o ACBr lido
como documentacao de apoio (LGPL, nenhum codigo portado). Nada aqui rodou
contra o sandbox do Banrisul. Duvidas e fontes: `docs/banrisul-duvidas.md`.

Nao ha modulo Odoo de origem.

Pontos do contrato:

- Toda chamada leva o header `bergs-beneficiario` (codigo do beneficiario com
  13 digitos: agencia 4 + codigo 7 + controle 2). Baixa, alteracao e emissao
  levam tambem `bergs-ambiente` (`P` ou `T`); o registro leva `ambiente` no corpo.
- `T` (homologacao) so valida a entrada e devolve `retorno` "04": nada e
  registrado nem alterado. O adapter levanta `Validacao` nesse caso.
- Valores e taxas vao como texto com casas fixas (`"150.00"`), sem float.
- O nosso numero tem 10 digitos: 8 livres e 2 DVs (modulos 10 e 11, algoritmo
  publicado na doc oficial). Sem `gerar_nosso_numero` o banco gera o numero.
- O webhook so traz o nosso numero, sem situacao: nao gera evento canonico.
  `referencias_webhook` devolve os nossos numeros a reconsultar.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from brbank.adapter import BankAdapter
from brbank.auth import OAuth2ClientCredentials
from brbank.capacidades import Capacidade
from brbank.erros import Duplicado, NaoSuportado, Validacao
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

URL_PRODUCAO = "https://api.banrisul.com.br/cobranca/v1"
URL_SANDBOX = "https://apidev.banrisul.com.br/cobranca/v1"
# A especificacao oficial so publica a URL de token do sandbox. A de producao
# vem do ACBr (mesmo caminho no host de producao); nao confirmada.
TOKEN_PRODUCAO = "https://api.banrisul.com.br/auth/oauth/v2/token"
TOKEN_SANDBOX = "https://apidev.banrisul.com.br/auth/oauth/v2/token"
ESCOPO_PADRAO = "boletos"

# Especies aceitas pelo registro (pattern oficial do campo `especie`).
ESPECIES = {
    "02": "Duplicata Mercantil",
    "04": "Duplicata de Servico",
    "31": "Cartao de Credito",
    "32": "Boleto de Proposta",
    "99": "Outros",
}

# Chave = `situacao_banrisul`; para o titulo ativo (A), `A` + `situacao_pagamento`
# (so vem para titulo ativo). Codigos oficiais sem equivalente canonico seguro
# ficam fora e viram Validacao: R (reembolsado), T (transferido para CL) e
# P (protestado). Ver docs/banrisul-duvidas.md.
TABELA_STATUS = TabelaStatus.criar(
    "2026.10",
    {
        "A": StatusCanonico.REGISTRADO,
        "A1": StatusCanonico.REGISTRADO,  # apto para pagamento
        "A2": StatusCanonico.LIQUIDADO,  # pago no dia
        "A3": StatusCanonico.REGISTRADO,  # credito retido: pago, credito pendente
        "A4": StatusCanonico.BAIXADO,  # baixado no dia
        "A5": StatusCanonico.REGISTRADO,  # em processo de devolucao automatica
        "A6": StatusCanonico.REGISTRADO,  # em cartorio
        "A7": StatusCanonico.REGISTRADO,  # pagamento indisponivel
        "A8": StatusCanonico.REGISTRADO,  # negativado e apto para pagamento
        "L": StatusCanonico.LIQUIDADO,
        "B": StatusCanonico.LIQUIDADO,  # "baixado por pagamento" (doc oficial)
        "D": StatusCanonico.BAIXADO,  # baixado por devolucao
    },
)

_RETORNO_HOMOLOGADO = "04"
_RETORNOS_REGISTRO = ("01", "02")
_CODIGO_DUPLICADO = "09"  # FEBRABAN: nosso numero duplicado
_ALTERACAO_ABATIMENTO = "04"
_ALTERACAO_VENCIMENTO = "06"
_CENTAVO = Decimal("0.01")
_DECIMO = Decimal("0.1")
_MAX_MENSAGENS = 9
_MAX_TEXTO_MENSAGEM = 75
_MAX_SEU_NUMERO = 13
_MAX_PRAZO_BAIXA = 99
_CARACTERES_SEU_NUMERO = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 %$*+.,/-&"
)


@dataclass
class BanrisulConfig:
    conta: str
    client_id: str
    client_secret: str = field(repr=False)
    # 13 digitos: agencia (4) + codigo do beneficiario (7) + controle (2).
    codigo_beneficiario: str
    # 02, 04, 31, 32 ou 99 (ver ESPECIES). Sem padrao: depende do convenio.
    especie: str
    ambiente: str = "producao"
    # True manda `T`: o banco so valida (procedimento de homologacao).
    homologacao: bool = False
    aceite: bool = False
    hibrido: bool = True
    # Gera os 8 digitos livres do nosso numero; o adapter calcula os 2 DVs.
    # None: o banco gera o numero (repetir o registro pode duplicar o titulo).
    gerar_nosso_numero: Callable[[Titulo], int | str] | None = field(
        default=None, repr=False
    )
    escopo: str = ESCOPO_PADRAO
    # A doc oficial nao fala em mTLS; o certificado e opcional.
    certificado_pem: bytes | None = field(default=None, repr=False)
    chave_pem: bytes | None = field(default=None, repr=False)
    requisicoes_por_segundo: float | None = None

    def __post_init__(self) -> None:
        if self.ambiente not in ("producao", "sandbox"):
            raise Validacao(f"ambiente Banrisul invalido: {self.ambiente!r}")
        codigo = self.codigo_beneficiario
        if not (codigo.isdigit() and len(codigo) == 13):
            raise Validacao("codigo do beneficiario Banrisul deve ter 13 digitos")
        if self.especie not in ESPECIES:
            raise Validacao(f"especie Banrisul invalida: {self.especie!r}")

    @property
    def url_base(self) -> str:
        return URL_SANDBOX if self.ambiente == "sandbox" else URL_PRODUCAO

    @property
    def url_token(self) -> str:
        return TOKEN_SANDBOX if self.ambiente == "sandbox" else TOKEN_PRODUCAO

    @property
    def codigo_ambiente(self) -> str:
        return "T" if self.homologacao else "P"


def dv_nosso_numero(livre: str) -> str:
    """Os 2 DVs do nosso numero (modulo 10 e modulo 11, doc oficial Banrisul).

    `livre` tem os 8 digitos de escolha do beneficiario. Exemplo oficial:
    00189274 -> "46".
    """
    if not (livre.isdigit() and len(livre) == 8):
        raise Validacao("nosso numero Banrisul: a parte livre tem 8 digitos")
    soma = 0
    for posicao, digito in enumerate(reversed(livre)):
        produto = int(digito) * (2 if posicao % 2 == 0 else 1)
        soma += produto - 9 if produto > 9 else produto
    resto = soma % 10
    dv1 = 0 if resto == 0 else 10 - resto
    while True:
        base = f"{livre}{dv1}"
        soma = sum(
            int(digito) * (2 + posicao % 6)
            for posicao, digito in enumerate(reversed(base))
        )
        resto = soma % 11
        if resto != 1:
            dv2 = 0 if resto == 0 else 11 - resto
            return f"{dv1}{dv2}"
        # Resto 1 invalida o DV do modulo 10: soma 1 (9 vira 0) e refaz.
        dv1 = 0 if dv1 == 9 else dv1 + 1


def _texto_decimal(valor: Decimal, quantum: Decimal, campo: str) -> str:
    """Decimal -> texto com casas fixas; recusa o que precisaria arredondar."""
    fixo = valor.quantize(quantum)
    if fixo != valor:
        raise Validacao(f"banrisul: {campo} {valor} tem casas demais (use {quantum})")
    return str(fixo)


def _dinheiro(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor)).quantize(_CENTAVO)
    except InvalidOperation as exc:
        raise Validacao("valor monetario ilegivel na resposta do Banrisul") from exc


def _data(texto: Any) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError as exc:
        raise Validacao("data ilegivel na resposta do Banrisul") from exc


def _endereco(pessoa: Pessoa, papel: str) -> tuple[str, str]:
    endereco = pessoa.endereco
    if endereco is None:
        raise Validacao(f"banrisul exige endereco do {papel}")
    partes = [endereco.logradouro, endereco.numero, endereco.complemento]
    linha = " ".join(p.strip() for p in partes if p and p.strip())
    # Limite oficial de 40 caracteres; nome e cidade tambem sao cortados.
    return linha[:40], endereco.cep


def _pessoa(pessoa: Pessoa, papel: str) -> dict[str, Any]:
    linha, cep = _endereco(pessoa, papel)
    return {
        "tipo_pessoa": "F" if pessoa.tipo == "FISICA" else "J",
        "cpf_cnpj": pessoa.documento,
        "nome": pessoa.nome.strip()[:40],
        "endereco": linha,
        "cep": cep,
    }


def _ocorrencias(resposta_bruta: Any) -> list[dict[str, Any]]:
    if not isinstance(resposta_bruta, dict):
        return []
    itens = resposta_bruta.get("ocorrencias")
    return [i for i in itens if isinstance(i, dict)] if isinstance(itens, list) else []


class BanrisulAdapter(BankAdapter):
    banco: ClassVar[str] = "banrisul"
    validado_sandbox: ClassVar[bool] = False
    capacidades: ClassVar[Capacidade] = (
        Capacidade.HIBRIDO | Capacidade.ALTERACAO | Capacidade.BAIXA | Capacidade.PDF
    )
    tabela_status: ClassVar[TabelaStatus] = TABELA_STATUS

    def __init__(
        self,
        config: BanrisulConfig,
        *,
        client: httpx.AsyncClient | None = None,
        retry: PoliticaRetry | None = None,
        dormir: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self._config = config
        self._hoje = hoje
        ssl_context = None
        if client is None and config.certificado_pem:
            ssl_context = contexto_tls(config.certificado_pem, config.chave_pem)
        auth = OAuth2ClientCredentials(
            conta=config.conta,
            url_token=config.url_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
            escopos={ESCOPO_PADRAO: config.escopo},
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

    def _cabecalhos(self, *, com_ambiente: bool = False) -> dict[str, str]:
        cab = {"bergs-beneficiario": self._config.codigo_beneficiario}
        if com_ambiente:
            cab["bergs-ambiente"] = self._config.codigo_ambiente
        return cab

    def _nosso_numero(self, titulo: Titulo) -> str | None:
        gerador = self._config.gerar_nosso_numero
        if gerador is None:
            return None
        livre = str(gerador(titulo))
        if not livre.isdigit() or len(livre) > 8:
            raise Validacao("banrisul: gerar_nosso_numero deve dar ate 8 digitos")
        livre = livre.zfill(8)
        return livre + dv_nosso_numero(livre)

    def _instrucoes(self, titulo: Titulo) -> dict[str, Any]:
        enc = titulo.encargos
        instrucoes: dict[str, Any] = {}
        # Sem data: o banco cobra juros e multa a partir do dia seguinte ao
        # vencimento, e a data acompanha uma prorrogacao (doc oficial).
        if enc.juros_mensal_percentual is not None:
            instrucoes["juros"] = {
                "codigo": 2,
                "taxa": _texto_decimal(enc.juros_mensal_percentual, _CENTAVO, "juros"),
            }
        else:
            instrucoes["juros"] = {"codigo": 3}
        if enc.multa_percentual is not None:
            instrucoes["multa"] = {
                "codigo": 2,
                "taxa": _texto_decimal(enc.multa_percentual, _DECIMO, "multa"),
            }
        if enc.desconto_percentual is not None and enc.desconto_dias_antes is not None:
            limite = titulo.vencimento - timedelta(days=enc.desconto_dias_antes)
            instrucoes["desconto"] = {
                "codigo": 2,
                "data": limite.isoformat(),
                "taxa": _texto_decimal(enc.desconto_percentual, _CENTAVO, "desconto"),
            }
        if enc.abatimento is not None:
            instrucoes["abatimento"] = {
                "valor": _texto_decimal(enc.abatimento, _CENTAVO, "abatimento")
            }
        if titulo.dias_baixa_automatica is not None:
            if titulo.dias_baixa_automatica > _MAX_PRAZO_BAIXA:
                raise Validacao("banrisul: prazo de baixa tem no maximo 2 digitos")
            instrucoes["baixa"] = {"codigo": 1, "prazo": titulo.dias_baixa_automatica}
        return instrucoes

    def _montar_payload(self, titulo: Titulo) -> dict[str, Any]:
        cfg = self._config
        seu_numero = titulo.seu_numero
        if len(seu_numero) > _MAX_SEU_NUMERO or not set(seu_numero) <= (
            _CARACTERES_SEU_NUMERO
        ):
            raise Validacao(
                "banrisul: seu_numero tem ate 13 caracteres (letras, digitos e "
                "espaco %$*+.,/-&)"
            )
        if len(titulo.mensagens) > _MAX_MENSAGENS or any(
            len(m) > _MAX_TEXTO_MENSAGEM for m in titulo.mensagens
        ):
            raise Validacao("banrisul: ate 9 mensagens de ate 75 caracteres")
        pagador = _pessoa(titulo.pagador, "pagador")
        endereco = titulo.pagador.endereco
        assert endereco is not None  # garantido por _pessoa
        pagador["cidade"] = endereco.cidade.strip()[:15]
        pagador["uf"] = endereco.uf
        pagador["aceite"] = "A" if cfg.aceite else "N"
        dados: dict[str, Any] = {
            "seu_numero": seu_numero,
            "data_vencimento": titulo.vencimento.isoformat(),
            "valor_nominal": _texto_decimal(titulo.valor, _CENTAVO, "valor"),
            "especie": cfg.especie,
            "data_emissao": self._hoje().isoformat(),
            "pagador": pagador,
            "instrucoes": self._instrucoes(titulo),
            # Obrigatorio; boleto hibrido exige "nao autoriza pagamento parcial".
            "pag_parcial": {"autoriza": 1, "codigo": 3},
        }
        nosso_numero = self._nosso_numero(titulo)
        if nosso_numero:
            dados["nosso_numero"] = nosso_numero
        if titulo.beneficiario:
            dados["sacador"] = _pessoa(titulo.beneficiario, "sacador")
        if titulo.mensagens:
            dados["mensagens"] = [
                {"linha": linha, "texto": texto}
                for linha, texto in enumerate(titulo.mensagens, start=1)
            ]
        if cfg.hibrido:
            dados["hibrido"] = {"autoriza": "S"}
        return {"ambiente": cfg.codigo_ambiente, "titulo": dados}

    # -- leitura -----------------------------------------------------------

    @staticmethod
    def _titulo(resp: httpx.Response) -> dict[str, Any]:
        """Valida `retorno` e devolve o objeto `titulo` da resposta."""
        try:
            dados = resp.json()
        except ValueError as exc:
            raise Validacao(
                "resposta do Banrisul nao e JSON", status_http=resp.status_code
            ) from exc
        titulo = dados.get("titulo") if isinstance(dados, dict) else None
        if not isinstance(titulo, dict):
            raise Validacao(
                "resposta do Banrisul sem titulo",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        if dados.get("retorno") == _RETORNO_HOMOLOGADO:
            raise Validacao(
                "banrisul: ambiente T (homologacao) so validou a requisicao, "
                "nada foi registrado nem alterado",
                status_http=resp.status_code,
                resposta_bruta=dados,
            )
        return {**titulo, "retorno": dados.get("retorno")}

    @staticmethod
    def _pix(t: dict[str, Any]) -> tuple[str | None, str | None]:
        """Copia-e-cola e txid so quando o QR Code foi criado (situacao A)."""
        hibrido = t.get("hibrido")
        if not isinstance(hibrido, dict) or hibrido.get("situacao") != "A":
            return None, None
        return hibrido.get("copia_cola"), hibrido.get("txid")

    @staticmethod
    def _codigo_status(t: dict[str, Any]) -> str:
        situacao = str(t.get("situacao_banrisul") or "").upper()
        pagamento = t.get("situacao_pagamento")
        if situacao == "A" and pagamento not in (None, ""):
            return f"A{pagamento}"
        return situacao

    # -- operacoes ---------------------------------------------------------

    async def registrar_titulo(self, titulo: Titulo) -> TituloRegistrado:
        payload = self._montar_payload(titulo)
        enviado = payload["titulo"].get("nosso_numero")
        try:
            resp = await self._transporte.requisitar(
                "POST",
                "boletos",
                operacao="registrar",
                escopo=ESCOPO_PADRAO,
                json=payload,
                cabecalhos=self._cabecalhos(),
            )
        except Validacao as erro:
            codigos = {str(o.get("codigo")) for o in _ocorrencias(erro.resposta_bruta)}
            if enviado and _CODIGO_DUPLICADO in codigos:
                raise Duplicado(
                    "banrisul: nosso numero ja registrado",
                    id_banco=enviado,
                    status_http=erro.status_http,
                    resposta_bruta=erro.resposta_bruta,
                ) from erro
            raise
        t = self._titulo(resp)
        nosso_numero = t.get("nosso_numero")
        if not nosso_numero or t.get("retorno") not in _RETORNOS_REGISTRO:
            raise Validacao(
                "resposta de registro do Banrisul sem nosso_numero ou retorno valido",
                status_http=resp.status_code,
                resposta_bruta=t,
            )
        copia_cola, txid = self._pix(t)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=str(nosso_numero),
            status=StatusCanonico.REGISTRADO,
            seu_numero=titulo.seu_numero,
            nosso_numero=str(nosso_numero),
            linha_digitavel=t.get("linha_digitavel"),
            codigo_barras=t.get("codigo_barras"),
            pix_copia_cola=copia_cola,
            txid=txid,
            resposta_bruta=t,
        )

    async def consultar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "GET",
            f"boletos/{quote(id_banco, safe='')}",
            operacao="consultar",
            escopo=ESCOPO_PADRAO,
            cabecalhos=self._cabecalhos(),
        )
        t = self._titulo(resp)
        status = self.tabela_status.traduzir(self._codigo_status(t))
        valor_pago, data_pagamento = None, None
        if status is StatusCanonico.LIQUIDADO:
            operacoes = t.get("operacoes")
            if not isinstance(operacoes, dict):
                operacoes = {}
            # `valor_cobrado` e o que o pagador pagou (inclui juros); o
            # `valor_pagamento` da doc pode vir sem o acrescimo. Ver duvidas.
            valor_pago = _dinheiro(operacoes.get("valor_cobrado"))
            if valor_pago is None:
                valor_pago = _dinheiro(operacoes.get("valor_pagamento"))
            data_pagamento = _data(operacoes.get("data_pagamento"))
        copia_cola, txid = self._pix(t)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=status,
            seu_numero=t.get("seu_numero"),
            nosso_numero=str(t.get("nosso_numero") or id_banco),
            linha_digitavel=t.get("linha_digitavel"),
            codigo_barras=t.get("codigo_barras"),
            pix_copia_cola=copia_cola,
            txid=txid,
            valor_pago=valor_pago,
            data_pagamento=data_pagamento,
            resposta_bruta=t,
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
            alteracao = _ALTERACAO_VENCIMENTO
            corpo = {
                "titulo": {
                    "data_vencimento": instrucao.nova_data_vencimento.isoformat()
                }
            }
        elif tipo is TipoInstrucao.ABATIMENTO:
            if instrucao.valor is None:
                raise Validacao("abatimento exige valor")
            # O Banrisul SOMA o valor a um abatimento ja concedido.
            alteracao = _ALTERACAO_ABATIMENTO
            valor = _texto_decimal(instrucao.valor, _CENTAVO, "abatimento")
            corpo = {"titulo": {"instrucoes": {"abatimento": {"valor": valor}}}}
        else:
            raise NaoSuportado(f"banrisul: instrucao {tipo.value} nao existe na API")
        resp = await self._transporte.requisitar(
            "PATCH",
            f"boletos/{quote(id_banco, safe='')}?tipo_alteracao={alteracao}",
            operacao="instrucao",
            escopo=ESCOPO_PADRAO,
            json=corpo,
            cabecalhos=self._cabecalhos(com_ambiente=True),
        )
        self._titulo(resp)
        # A resposta nao traz a situacao: o estado vem da reconsulta.
        return await self.consultar_titulo(id_banco)

    async def baixar_titulo(self, id_banco: str) -> TituloRegistrado:
        resp = await self._transporte.requisitar(
            "POST",
            f"boletos/{quote(id_banco, safe='')}/baixar",
            operacao="baixar",
            escopo=ESCOPO_PADRAO,
            cabecalhos=self._cabecalhos(com_ambiente=True),
        )
        t = self._titulo(resp)
        return TituloRegistrado(
            banco=self.banco,
            id_banco=id_banco,
            status=StatusCanonico.BAIXADO,
            nosso_numero=t.get("nosso_numero"),
            linha_digitavel=t.get("linha_digitavel"),
            codigo_barras=t.get("codigo_barras"),
            resposta_bruta=t,
        )

    async def obter_pdf(self, id_banco: str) -> bytes:
        resp = await self._transporte.requisitar(
            "GET",
            f"boletos/{quote(id_banco, safe='')}/emitir",
            operacao="pdf",
            escopo=ESCOPO_PADRAO,
            cabecalhos=self._cabecalhos(com_ambiente=True),
        )
        t = self._titulo(resp)
        try:
            pdf = base64.b64decode(t.get("boleto") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Validacao("banrisul: PDF em base64 ilegivel") from exc
        if not pdf:
            raise Validacao("banrisul: resposta de emissao sem PDF")
        return pdf

    # -- webhook -----------------------------------------------------------

    def referencias_webhook(self, corpo: bytes | str | list | dict) -> list[str]:
        """Nossos numeros avisados pelo webhook, para reconsultar.

        O webhook do Banrisul so diz que o titulo mudou ao longo do dia (sem a
        situacao nem assinatura; a origem e a faixa 201.55.240.0/20). Recusa
        aviso de outro beneficiario.
        """
        if isinstance(corpo, bytes | str):
            try:
                corpo = json.loads(corpo)
            except ValueError as exc:
                raise Validacao("webhook do Banrisul nao e JSON") from exc
        itens = corpo if isinstance(corpo, list) else [corpo]
        referencias = []
        for item in itens:
            if not isinstance(item, dict) or not item.get("nosso_numero"):
                raise Validacao(
                    "webhook do Banrisul sem nosso_numero", resposta_bruta=item
                )
            codigo = (item.get("beneficiario") or {}).get("codigo")
            if codigo != self._config.codigo_beneficiario:
                raise Validacao("webhook do Banrisul de outro beneficiario")
            referencias.append(str(item["nosso_numero"]))
        return referencias

    def parse_webhook(
        self,
        corpo: bytes | str | list | dict,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> list[EventoCanonico]:
        raise NaoSuportado(
            "banrisul: o webhook nao traz a situacao do titulo; use "
            "referencias_webhook e reconsulte (docs/banrisul-duvidas.md)"
        )
