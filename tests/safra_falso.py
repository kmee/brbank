"""Servidor falso do Safra (API de Cobranca v1) sobre httpx.MockTransport.

Formato inferido do ACBr e de integracoes de terceiros (docs/safra-duvidas.md);
nao e captura da homologacao real.
"""

import json
from urllib.parse import parse_qs

import httpx

BASE = "/gateway/cobrancas/v1/boletos"
TOKEN = "/gateway/v1/oauth2/token"


def codigo_barras(nosso_numero: str, valor: float) -> str:
    """44 digitos com banco 422 e moeda 9 (DV geral fixo, so para teste)."""
    centavos = f"{round(valor * 100):010d}"
    livre = ("7" + "12345" + "123456789" + nosso_numero.zfill(9) + "2")[:25]
    return "4229" + "1" + "9999" + centavos + livre.ljust(25, "0")


class SafraFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.pedidos_token: list[dict[str, str]] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.ultimo_corpo: dict = {}
        self.ultima_query: dict[str, str] = {}
        self.falhas_503 = 0
        self.resposta_registro: dict | None = None

    @property
    def ultimo_cabecalho(self) -> dict[str, str]:
        return self.cabecalhos[-1]

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["situacao"] = "PAGO"
        boleto["valorTotalRecebimento"] = boleto["valorNominal"]
        boleto["dataHoraSituacao"] = "2026-11-10T10:00:00"

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == TOKEN:
            self.tokens_emitidos += 1
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.pedidos_token.append(form)
            if form.get("grant_type") != "password":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 300},
            )
        self.cabecalhos.append(dict(request.headers))
        if self.falhas_503:
            self.falhas_503 -= 1
            return httpx.Response(503, json={"mensagem": "Ocorreu um erro"})
        if request.method == "POST" and caminho == BASE:
            self.ultimo_corpo = json.loads(request.content)
            return self._registrar()
        if request.method == "GET" and caminho == BASE:
            self.ultima_query = dict(request.url.params)
            numero = self.ultima_query.get("numero", "")
            achado = self.boletos.get(numero)
            if achado is None:
                return httpx.Response(
                    404, json={"codigo": "404", "mensagem": "Boleto nao encontrado"}
                )
            return httpx.Response(200, json={"content": [achado]})
        return httpx.Response(404, json={"codigo": "404", "mensagem": "rota"})

    def _registrar(self) -> httpx.Response:
        if self.resposta_registro is not None:
            return httpx.Response(200, json=self.resposta_registro)
        documento = self.ultimo_corpo["documento"]
        numero = f"{documento['numero']:09d}"
        if numero in self.boletos:
            return httpx.Response(
                422, json={"code": "DUP", "message": "Boleto ja cadastrado"}
            )
        barras = codigo_barras(numero, documento["valor"])
        self.boletos[numero] = {
            "nossoNumero": numero,
            "seuNumero": documento["numeroCliente"],
            "situacao": "EM ABERTO",
            "valorNominal": documento["valor"],
            "codigoBarras": barras,
            "pagador": {"nome": documento["pagador"]["nome"], "cpfCnpj": "52998224725"},
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "numero": documento["numero"],
                    "numeroCliente": documento["numeroCliente"],
                    "codigoBarras": barras,
                }
            },
        )
