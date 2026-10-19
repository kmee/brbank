"""Servidor falso do Sicredi (API v2) sobre httpx.MockTransport, com estado."""

import json
from urllib.parse import parse_qs

import httpx

BASE = "/cobranca/boleto/v1/boletos"
CHAVE_API = "chave-api-teste"


class SicrediFalso:
    def __init__(self, ambiente_prefixo: str = "") -> None:
        self.prefixo = ambiente_prefixo
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.pedidos_token: list[dict[str, str]] = []
        self.cabecalhos_token: dict[str, str] = {}
        self.ultimo_cabecalho: dict[str, str] = {}
        self.cabecalhos: list[tuple[str, str, dict[str, str]]] = []
        self.ultimo_corpo: dict = {}
        self.baixa_pendente = False

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["situacao"] = "LIQUIDADO"
        boleto["dadosLiquidacao"] = {
            "valor": boleto["valorNominal"],
            "data": "2026-11-10 10:00:00",
        }

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.removeprefix(self.prefixo)
        self.chamadas.append((request.method, caminho))
        if caminho == "/auth/openapi/token":
            self.cabecalhos_token = dict(request.headers)
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.pedidos_token.append(form)
            if form.get("grant_type") != "password":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{len(self.pedidos_token)}",
                    "expires_in": 300,
                },
            )
        self.ultimo_cabecalho = dict(request.headers)
        self.cabecalhos.append((request.method, caminho, self.ultimo_cabecalho))
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == BASE:
            numero = f"2{len(self.boletos) + 1:08d}"
            self.boletos[numero] = {
                "situacao": "EM CARTEIRA",
                "seuNumero": self.ultimo_corpo["seuNumero"],
                "valorNominal": self.ultimo_corpo["valor"],
                "nossoNumero": numero,
                "linhaDigitavel": "74893.12345 67890.123456 78901.234567 8 1234",
                "codigoBarras": "74898123400000150001234567890123456789012345",
                "txId": "tx123",
                "codigoQrCode": "00020126...",
                "pagador": {"nome": "Maria da Silva", "documento": "52998224725"},
            }
            return httpx.Response(
                200,
                json={
                    "nossoNumero": numero,
                    "linhaDigitavel": self.boletos[numero]["linhaDigitavel"],
                    "codigoBarras": self.boletos[numero]["codigoBarras"],
                    "txid": "tx123",
                    "qrCode": "00020126...",
                },
            )
        if request.method == "GET" and caminho == BASE:
            consulta = parse_qs(request.url.query.decode())
            boleto = self.boletos.get(consulta["nossoNumero"][0])
            if boleto is None:
                return httpx.Response(404, json={"message": "nao encontrado"})
            return httpx.Response(200, json=boleto)
        partes = caminho.removeprefix(BASE + "/").split("/")
        boleto = self.boletos.get(partes[0])
        if boleto is None:
            return httpx.Response(404, json={"message": "nao encontrado"})
        if request.method == "PATCH" and partes[1] == "baixa":
            if not self.baixa_pendente:
                boleto["situacao"] = "BAIXADO POR SOLICITACAO"
            return httpx.Response(200, json={"statusComando": "MOVIMENTO_ENVIADO"})
        if request.method == "PATCH" and partes[1] == "data-vencimento":
            boleto["dataVencimento"] = self.ultimo_corpo["dataVencimento"]
            return httpx.Response(200, json={"statusComando": "MOVIMENTO_ENVIADO"})
        if request.method == "PATCH" and partes[1] == "conceder-abatimento":
            boleto["abatimento"] = self.ultimo_corpo["valorAbatimento"]
            return httpx.Response(200, json={"statusComando": "MOVIMENTO_ENVIADO"})
        return httpx.Response(404, json={"message": "rota"})
