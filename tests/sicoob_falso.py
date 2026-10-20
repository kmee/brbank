"""Servidor falso do Sicoob v3 sobre httpx.MockTransport, com estado.

Reflete o que o sandbox publico do Sicoob mostrou em 2026-10-20
(docs/sicoob-duvidas.md): corpo do registro e um objeto (lista vira 400),
`codigoEspecieDocumento` e sigla (numero vira 400), `nossoNumero` volta como
inteiro, nosso numero nao numerico na URL ou na query vira 400, baixa,
alteracao e protesto respondem 204 sem corpo, sem header `client_id` vem 401,
erro de negocio traz `mensagens[{codigo, mensagem}]`. A consulta de nosso
numero inexistente responde 204 sem corpo, como o swagger descreve.
"""

import base64
import json
from urllib.parse import parse_qs

import httpx

BASE = "/cobranca-bancaria/v3/boletos"
PDF = b"%PDF-1.4\n% boleto falso\n"
OBRIGATORIOS = (
    "numeroCliente",
    "codigoModalidade",
    "numeroContaCorrente",
    "codigoEspecieDocumento",
    "identificacaoEmissaoBoleto",
    "identificacaoDistribuicaoBoleto",
    "dataEmissao",
    "seuNumero",
    "valor",
    "dataVencimento",
    "tipoDesconto",
    "numeroParcela",
    "pagador",
    "tipoMulta",
    "tipoJurosMora",
)


def _erro(status: int, mensagem: str) -> httpx.Response:
    return httpx.Response(
        status, json={"mensagens": [{"codigo": str(status), "mensagem": mensagem}]}
    )


def _numerico(texto: str) -> bool:
    return texto.lstrip("-").isdigit()


class SicoobFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultima_query: dict[str, str] = {}
        # pdfBoleto exato a devolver; None = base64 limpo de PDF.
        self.pdf_base64: str | None = None

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["situacaoBoleto"] = "Liquidado"
        # tipoHistorico e string no swagger e numero no exemplo do sandbox.
        boleto["listaHistorico"] = [
            {"tipoHistorico": 1, "dataHistorico": "2026-10-01"},
            {
                "tipoHistorico": "6",
                "dataHistorico": "2026-11-10",
                "descricaoHistorico": "LIQUIDACAO - R$ 1.150,50",
            },
        ]

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.ultimo_form = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 300},
        )

    def _registrar(self, corpo) -> httpx.Response:
        if not isinstance(corpo, dict):
            return _erro(400, "corpo deve ser objeto")
        if any(campo not in corpo for campo in OBRIGATORIOS):
            return _erro(400, "campo obrigatorio ausente")
        if not isinstance(corpo["codigoEspecieDocumento"], str):
            return _erro(400, "codigoEspecieDocumento deve ser texto")
        nosso = 1000 + len(self.boletos) + 1
        self.boletos[str(nosso)] = {
            "numeroCliente": corpo["numeroCliente"],
            "codigoModalidade": corpo["codigoModalidade"],
            "codigoEspecieDocumento": corpo["codigoEspecieDocumento"],
            "nossoNumero": nosso,
            "seuNumero": corpo["seuNumero"],
            "valor": corpo["valor"],
            "situacaoBoleto": "Em Aberto",
            "codigoBarras": "75691000090",
            "linhaDigitavel": "75691.00009 01234",
            "qrCode": "000201...",
            "dataVencimento": corpo["dataVencimento"],
            "listaHistorico": [],
        }
        return httpx.Response(200, json={"resultado": self.boletos[str(nosso)]})

    def _consultar(self, request: httpx.Request, pdf: bool) -> httpx.Response:
        nosso = request.url.params.get("nossoNumero", "")
        if not _numerico(nosso):
            return _erro(400, "nossoNumero invalido")
        if nosso not in self.boletos:
            return httpx.Response(204)
        resultado = dict(self.boletos[nosso])
        if pdf:
            resultado["pdfBoleto"] = self.pdf_base64 or base64.b64encode(PDF).decode()
        return httpx.Response(200, json={"resultado": resultado})

    def _alterar(self, request: httpx.Request, caminho: str, corpo) -> httpx.Response:
        partes = caminho.split("/")
        nosso = partes[4] if len(partes) > 4 else ""
        if not _numerico(nosso):
            return httpx.Response(400, json={"httpCode": "400"})
        if not isinstance(corpo, dict) or not {
            "numeroCliente",
            "codigoModalidade",
        } <= set(corpo):
            return _erro(400, "numeroCliente e codigoModalidade obrigatorios")
        if nosso not in self.boletos:
            return _erro(400, "boleto nao encontrado")
        if caminho.endswith("/baixar"):
            self.boletos[nosso]["situacaoBoleto"] = "Baixado"
        elif request.method == "PATCH":
            prorroga = corpo.get("prorrogacaoVencimento")
            if prorroga:
                self.boletos[nosso]["dataVencimento"] = prorroga["dataVencimento"]
        return httpx.Response(204)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = (
            "/cobranca-bancaria/v3"
            + request.url.path.split("/cobranca-bancaria/v3")[-1]
        )
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho.endswith("/openid-connect/token"):
            return self._token(request)
        if "client_id" not in request.headers:
            return httpx.Response(401, json={"httpCode": "401"})
        self.ultima_query = dict(request.url.params)
        corpo = json.loads(request.content) if request.content else None
        if corpo is not None:
            self.ultimo_corpo = corpo
        if request.method == "POST" and caminho == BASE:
            return self._registrar(corpo)
        if request.method == "GET" and caminho == BASE:
            return self._consultar(request, pdf=False)
        if request.method == "GET" and caminho == BASE + "/segunda-via":
            return self._consultar(request, pdf=True)
        return self._alterar(request, caminho, corpo)
