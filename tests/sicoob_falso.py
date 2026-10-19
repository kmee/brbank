"""Servidor falso do Sicoob v3 sobre httpx.MockTransport, com estado.

Formato inferido de documentacao de terceiros (docs/sicoob-duvidas.md); nao e
captura do sandbox real.
"""

import json
from urllib.parse import parse_qs

import httpx

BASE = "/cobranca-bancaria/v3/boletos"


class SicoobFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultima_query: dict[str, str] = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["situacaoBoleto"] = "Liquidado"
        boleto["listaHistorico"] = [
            {"tipoHistorico": 1, "dataHistorico": "2026-10-01"},
            {
                "tipoHistorico": 6,
                "dataHistorico": "2026-11-10T10:00:00",
                "descricaoHistorico": "Liquidacao no valor de R$ 1.150,50",
            },
        ]

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.ultimo_form = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 300},
        )

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = (
            "/cobranca-bancaria/v3"
            + request.url.path.split("/cobranca-bancaria/v3")[-1]
        )
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho.endswith("/openid-connect/token"):
            return self._token(request)
        self.ultima_query = dict(request.url.params)
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == BASE:
            nosso = str(1000 + len(self.boletos) + 1)
            corpo = self.ultimo_corpo
            self.boletos[nosso] = {
                "nossoNumero": nosso,
                "seuNumero": corpo["seuNumero"],
                "valor": corpo["valor"],
                "situacaoBoleto": "Em Aberto",
                "codigoBarras": "75691000090",
                "linhaDigitavel": "75691.00009 01234",
                "qrCode": "000201...",
                "dataVencimento": corpo["dataVencimento"],
            }
            return httpx.Response(200, json={"resultado": self.boletos[nosso]})
        if request.method == "GET" and caminho == BASE:
            nosso = request.url.params.get("nossoNumero", "")
            if nosso not in self.boletos:
                return httpx.Response(
                    404, json={"mensagens": [{"codigo": "404", "mensagem": "nada"}]}
                )
            return httpx.Response(200, json={"resultado": self.boletos[nosso]})
        partes = caminho.split("/")
        nosso = partes[4] if len(partes) > 4 else ""
        if nosso not in self.boletos:
            return httpx.Response(404, json={"mensagens": [{"mensagem": "nada"}]})
        if caminho.endswith("/baixar"):
            self.boletos[nosso]["situacaoBoleto"] = "Baixado"
        elif request.method == "PATCH":
            prorroga = self.ultimo_corpo.get("prorrogacaoVencimento")
            if prorroga:
                self.boletos[nosso]["dataVencimento"] = prorroga["dataVencimento"]
        return httpx.Response(204)
