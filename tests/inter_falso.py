"""Servidor falso do Inter sobre httpx.MockTransport, com estado."""

import base64
import json

import httpx

PDF = b"%PDF-1.4 falso"


class InterFalso:
    def __init__(self) -> None:
        self.cobrancas: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, id_banco: str) -> None:
        cobranca = self.cobrancas[id_banco]
        cobranca["situacao"] = "PAGO"
        cobranca["valorTotalRecebido"] = cobranca["valorNominal"]
        cobranca["dataSituacao"] = "2026-11-10"

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho == "/oauth/v2/token":
            self.tokens_emitidos += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.tokens_emitidos}",
                    "expires_in": 3600,
                },
            )
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == "/cobranca/v3/cobrancas":
            id_banco = f"cod-{len(self.cobrancas) + 1}"
            self.cobrancas[id_banco] = {
                "situacao": "A_RECEBER",
                "seuNumero": self.ultimo_corpo["seuNumero"],
                "valorNominal": self.ultimo_corpo["valorNominal"],
            }
            return httpx.Response(200, json={"codigoSolicitacao": id_banco})
        partes = caminho.split("/")
        id_banco = partes[4] if len(partes) > 4 else ""
        if id_banco not in self.cobrancas:
            return httpx.Response(404, json={"title": "nao encontrado"})
        cobranca = self.cobrancas[id_banco]
        if caminho.endswith("/cancelar"):
            cobranca["situacao"] = "CANCELADO"
            return httpx.Response(204)
        if caminho.endswith("/pdf"):
            return httpx.Response(200, json={"pdf": base64.b64encode(PDF).decode()})
        return httpx.Response(
            200,
            json={
                "cobranca": cobranca,
                "boleto": {
                    "nossoNumero": "0012345",
                    "codigoBarras": "00190000090",
                    "linhaDigitavel": "00190.00009 01234",
                },
                "pix": {"pixCopiaECola": "000201...", "txid": "tx123"},
            },
        )
