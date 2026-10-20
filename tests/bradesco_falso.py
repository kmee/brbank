"""Servidor falso da API Cobranca do Bradesco sobre httpx.MockTransport, com estado.

Formato inferido de fontes de terceiros (docs/bradesco-duvidas.md); nao e
captura do sandbox real. Os numeros abaixo sao sinteticos, so para o teste.
"""

import json
from urllib.parse import parse_qs

import httpx

REGISTRO = "/boleto/cobranca-registro/v1/cobranca"
CONSULTA = "/boleto/cobranca-consulta/v1/consultar"
BAIXA = "/boleto/cobranca-baixa/v1/baixar"
TOKEN = "/auth/server-mtls/v2/token"


class BradescoFalso:
    def __init__(self) -> None:
        self.titulos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultimo_host = ""

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        titulo = self.titulos[nosso_numero]
        titulo.update(codStatus=61, status="PAGO", dtPagto=10112026, vlrPagto=150.0)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.ultimo_host = request.url.host
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho == TOKEN:
            self.tokens_emitidos += 1
            self.ultimo_form = parse_qs(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.tokens_emitidos}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        corpo = self.ultimo_corpo
        if request.method == "POST" and caminho == REGISTRO:
            nosso = str(90000000000 + len(self.titulos) + 1)
            self.titulos[nosso] = {
                "codStatus": 1,
                "status": "A VENCER / VENCIDO",
                "snumero": corpo["nuCliente"],
                "linhaDig": "23790000000000000000000000000000000000000000000",
                "codBarras": "23790000000000000000000000000000000000000000",
                "dtPagto": 0,
                "vlrPagto": 0.0,
                "nomeSacado": corpo["nomePagador"],
                "cnpjSacado": int(corpo["nuCpfcnpjPagador"]),
            }
            return httpx.Response(
                200,
                json={
                    "nuTituloGerado": int(nosso),
                    "linhaDigitavel": self.titulos[nosso]["linhaDig"],
                    "cdBarras": self.titulos[nosso]["codBarras"],
                    "nomePagador": corpo["nomePagador"],
                },
            )
        nosso = str(corpo.get("nossoNumero", ""))
        if nosso not in self.titulos:
            return httpx.Response(
                404, json={"status": 404, "causa": "TITULO INEXISTENTE"}
            )
        if caminho == CONSULTA:
            return httpx.Response(
                200, json={"status": 200, "titulo": dict(self.titulos[nosso])}
            )
        if caminho == BAIXA:
            self.titulos[nosso].update(codStatus=57, status="CONFORME SEU PEDIDO")
            return httpx.Response(200, json={"status": 200})
        return httpx.Response(404, json={"status": 404})
