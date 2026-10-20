"""Servidor falso do webservice SIGCB da Caixa sobre httpx.MockTransport.

Estrutura das respostas tirada dos XSD oficiais do barramento (SERVICO_SAIDA,
CONTROLE_NEGOCIAL, INCLUI_BOLETO, CONSULTA_BOLETO/TITULO). Nao e captura do
ambiente real: textos de erro e codigos de barras sao do servidor falso, e o
texto de SITUACAO apos a baixa nao e conhecido (docs/caixa-duvidas.md).
O servidor confere o hash AUTENTICACAO como a Caixa faria.
"""

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import httpx

from brbank.adapters.caixa import NS_CONSULTA, NS_MANUTENCAO, NS_SOAP, autenticacao

CODIGO_BENEFICIARIO = "862011"
DOCUMENTO_BENEFICIARIO = "11222333000181"
BASE = "/sibar/"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _filho(elemento: ET.Element, *caminho: str) -> ET.Element | None:
    atual: ET.Element | None = elemento
    for nome in caminho:
        if atual is None:
            return None
        atual = next((f for f in atual if _local(f.tag) == nome), None)
    return atual


def _texto(elemento: ET.Element, *caminho: str) -> str | None:
    achado = _filho(elemento, *caminho)
    return None if achado is None else (achado.text or "")


def _xml(tag: str, conteudo) -> str:
    if isinstance(conteudo, dict):
        conteudo = "".join(_xml(k, v) for k, v in conteudo.items())
    return f"<{tag}>{conteudo}</{tag}>"


class CaixaFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str, str]] = []  # (path, soapaction, operacao)
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_xml: ET.Element | None = None
        self.ultimo_corpo: bytes = b""
        self.cod_retorno_barramento = "00"
        self.resposta_crua: httpx.Response | None = None
        # Layout antigo: SITUACAO so no texto do RETORNO, nao no TITULO.
        self.situacao_no_titulo = True

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    # -- controle dos testes -------------------------------------------------

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["SITUACAO"] = "LIQUIDADO"
        boleto["VALOR_PAGO"] = boleto["VALOR"]
        boleto["DATA_HORA_PAGAMENTO"] = "2026-11-10 10:00:00"

    # -- respostas -----------------------------------------------------------

    def _saida(self, namespace: str, operacao: str, dados: str) -> httpx.Response:
        prefixo = "consultacobrancabancaria" if namespace == NS_CONSULTA else "m"
        corpo = (
            f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}"><soapenv:Body>'
            f'<{prefixo}:SERVICO_SAIDA xmlns:{prefixo}="{namespace}" '
            'xmlns:sibar_base="http://caixa.gov.br/sibar">'
            "<sibar_base:HEADER><VERSAO>3.2</VERSAO>"
            "<AUTENTICACAO>hash-devolvido</AUTENTICACAO>"
            f"<OPERACAO>{operacao}</OPERACAO><DATA_HORA>20261101120000</DATA_HORA>"
            "</sibar_base:HEADER>"
            f"<COD_RETORNO>{self.cod_retorno_barramento}</COD_RETORNO>"
            "<ORIGEM_RETORNO>FALSO</ORIGEM_RETORNO>"
            "<MSG_RETORNO>mensagem do barramento falso</MSG_RETORNO>"
            f"<DADOS>{dados}</DADOS>"
            f"</{prefixo}:SERVICO_SAIDA></soapenv:Body></soapenv:Envelope>"
        )
        return httpx.Response(
            200, content=corpo.encode(), headers={"Content-Type": "text/xml"}
        )

    @staticmethod
    def _controle(codigo: str, retorno: str) -> str:
        return _xml(
            "CONTROLE_NEGOCIAL",
            {
                "ORIGEM_RETORNO": "SIGCB",
                "COD_RETORNO": codigo,
                "MENSAGENS": {"RETORNO": retorno},
            },
        )

    def _erro(self, namespace: str, operacao: str, texto: str) -> httpx.Response:
        return self._saida(
            namespace, operacao, self._controle("1", f"(99) SERVIDOR FALSO: {texto}")
        )

    # -- roteamento ----------------------------------------------------------

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.split(BASE, 1)[-1]
        self.ultimo_cabecalho = dict(request.headers)
        self.ultimo_corpo = request.content
        raiz = ET.fromstring(request.content)
        self.ultimo_xml = raiz
        entrada = _filho(raiz, "Body", "SERVICO_ENTRADA")
        assert entrada is not None
        operacao = _texto(entrada, "HEADER", "OPERACAO") or ""
        self.chamadas.append((caminho, request.headers.get("soapaction", ""), operacao))
        if self.resposta_crua is not None:
            return self.resposta_crua
        dados = _filho(entrada, "DADOS", operacao)
        assert dados is not None
        namespace = (
            NS_CONSULTA
            if caminho == "ConsultaCobrancaBancaria/Boleto"
            else NS_MANUTENCAO
        )
        if _texto(dados, "CODIGO_BENEFICIARIO") != CODIGO_BENEFICIARIO.zfill(7):
            return self._erro(namespace, operacao, "BENEFICIARIO NAO HABILITADO")
        if operacao == "INCLUI_BOLETO":
            return self._incluir(entrada, dados)
        nosso = _texto(dados, "NOSSO_NUMERO") or _texto(dados, "TITULO", "NOSSO_NUMERO")
        boleto = self.boletos.get(nosso or "")
        if boleto is None:
            return self._erro(namespace, operacao, "TITULO NAO ENCONTRADO")
        if operacao == "ALTERA_BOLETO":
            return self._alterar(entrada, dados, boleto)
        if not self._hash_ok(entrada, nosso or "", None, None):
            return self._erro(namespace, operacao, "AUTENTICACAO INVALIDA")
        if operacao == "BAIXA_BOLETO":
            # Texto real da situacao apos baixa a pedido: desconhecido.
            boleto["SITUACAO"] = "BAIXA POR DEVOLUCAO"
            return self._saida(
                namespace,
                operacao,
                self._controle("0", "(0) OPERACAO EFETUADA") + "<BAIXA_BOLETO/>",
            )
        titulo = {k: v for k, v in boleto.items() if v is not None}
        if not self.situacao_no_titulo:
            titulo.pop("SITUACAO", None)
        return self._saida(
            namespace,
            operacao,
            self._controle(
                "0",
                f"(0) OPERACAO EFETUADA - SITUACAO DO TITULO = {boleto['SITUACAO']}",
            )
            + _xml("CONSULTA_BOLETO", {"TITULO": titulo, "FLAG_REGISTRO": "S"}),
        )

    def _hash_ok(
        self,
        entrada: ET.Element,
        nosso: str,
        vencimento: date | None,
        valor: Decimal | None,
    ) -> bool:
        esperado = autenticacao(
            CODIGO_BENEFICIARIO, nosso, DOCUMENTO_BENEFICIARIO, vencimento, valor
        )
        return _texto(entrada, "HEADER", "AUTENTICACAO") == esperado

    def _incluir(self, entrada: ET.Element, dados: ET.Element) -> httpx.Response:
        titulo = _filho(dados, "TITULO")
        assert titulo is not None
        nosso = _texto(titulo, "NOSSO_NUMERO") or ""
        vencimento = date.fromisoformat(_texto(titulo, "DATA_VENCIMENTO") or "")
        valor = Decimal(_texto(titulo, "VALOR") or "0")
        if not self._hash_ok(entrada, nosso, vencimento, valor):
            return self._erro(NS_MANUTENCAO, "INCLUI_BOLETO", "AUTENTICACAO INVALIDA")
        if nosso in self.boletos:
            return self._erro(NS_MANUTENCAO, "INCLUI_BOLETO", "NOSSO NUMERO JA EXISTE")
        hibrido = _texto(titulo, "TIPO") == "HIBRIDO"
        pagador = _filho(titulo, "PAGADOR")
        assert pagador is not None
        self.boletos[nosso] = {
            "TIPO": "HIBRIDO" if hibrido else None,
            "NUMERO_DOCUMENTO": _texto(titulo, "NUMERO_DOCUMENTO"),
            "DATA_VENCIMENTO": vencimento.isoformat(),
            "VALOR": f"{valor:.2f}",
            "TIPO_ESPECIE": _texto(titulo, "TIPO_ESPECIE"),
            "FLAG_ACEITE": _texto(titulo, "FLAG_ACEITE"),
            "VALOR_ABATIMENTO": _texto(titulo, "VALOR_ABATIMENTO") or "0.00",
            "PAGADOR": {
                "CPF": _texto(pagador, "CPF"),
                "NOME": _texto(pagador, "NOME"),
            },
            "IDENTIFICACAO_EMPRESA": _texto(titulo, "IDENTIFICACAO_EMPRESA"),
            "CODIGO_BARRAS": "10491000000000150000862011000000000000000001",
            "LINHA_DIGITAVEL": "10498620110000000000000000000001100000000015000",
            "URL": f"https://falso.invalid/imprimir/{CODIGO_BENEFICIARIO}/{nosso}",
            "QRCODE": "00020101021226900014br.gov.bcb.pix-falso" if hibrido else None,
            "SITUACAO": "EM ABERTO",
        }
        boleto = self.boletos[nosso]
        resposta = {
            "CODIGO_BARRAS": boleto["CODIGO_BARRAS"],
            "LINHA_DIGITAVEL": boleto["LINHA_DIGITAVEL"],
            # A Caixa pode devolver 0 aqui (exemplo publico, docs/caixa-duvidas.md).
            "NOSSO_NUMERO": "0",
            "URL": boleto["URL"],
        }
        if hibrido:
            resposta["QRCODE"] = boleto["QRCODE"]
        return self._saida(
            NS_MANUTENCAO,
            "INCLUI_BOLETO",
            self._controle("0", "(0) OPERACAO EFETUADA")
            + _xml("INCLUI_BOLETO", resposta),
        )

    def _alterar(
        self, entrada: ET.Element, dados: ET.Element, boleto: dict
    ) -> httpx.Response:
        titulo = _filho(dados, "TITULO")
        assert titulo is not None
        nova = _texto(titulo, "DATA_VENCIMENTO")
        vencimento = date.fromisoformat(nova or boleto["DATA_VENCIMENTO"])
        nosso = _texto(titulo, "NOSSO_NUMERO") or ""
        if not self._hash_ok(entrada, nosso, vencimento, Decimal(boleto["VALOR"])):
            return self._erro(NS_MANUTENCAO, "ALTERA_BOLETO", "AUTENTICACAO INVALIDA")
        if nova:
            boleto["DATA_VENCIMENTO"] = nova
        abatimento = _texto(titulo, "VALOR_ABATIMENTO")
        if abatimento:
            boleto["VALOR_ABATIMENTO"] = abatimento
        return self._saida(
            NS_MANUTENCAO,
            "ALTERA_BOLETO",
            self._controle("0", "(0) OPERACAO EFETUADA") + "<ALTERA_BOLETO/>",
        )
