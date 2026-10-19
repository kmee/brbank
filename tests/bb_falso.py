"""Servidor falso do BB (Cobranca v2) sobre httpx.MockTransport, com estado."""

import json

import httpx

CHAVE_DEV = "chave-dev-falsa"


class BBFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.urls: list[str] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.pedido_token: dict[str, str] = {}
        self.cabecalho_token: dict[str, str] = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, id_banco: str) -> None:
        boleto = self.boletos[id_banco]
        boleto["codigoEstadoTituloCobranca"] = 6
        boleto["valorPagoSacado"] = boleto["valorOriginalTituloCobranca"]
        boleto["dataRecebimentoTitulo"] = "10.11.2026"

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.urls.append(str(request.url))
        if caminho == "/oauth/token":
            self.tokens_emitidos += 1
            self.pedido_token = dict(
                p.split("=", 1) for p in request.content.decode().split("&")
            )
            self.cabecalho_token = dict(request.headers)
            return httpx.Response(
                200,
                json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 600},
            )
        self.ultimo_cabecalho = dict(request.headers)
        if request.url.params.get("gw-dev-app-key") != CHAVE_DEV:
            return httpx.Response(401, json={"error": "chave"})
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == "/cobrancas/v2/boletos":
            numero = self.ultimo_corpo["numeroTituloCliente"]
            self.boletos[numero] = {
                "id": numero,
                "codigoEstadoTituloCobranca": 1,
                "numeroTituloCedenteCobranca": self.ultimo_corpo[
                    "numeroTituloBeneficiario"
                ],
                "valorOriginalTituloCobranca": self.ultimo_corpo["valorOriginal"],
                "dataVencimentoTituloCobranca": self.ultimo_corpo["dataVencimento"],
                "codigoLinhaDigitavel": "00190000090 01234",
                "textoCodigoBarrasTituloCobranca": "00190000090",
                "valorAbatimentoTituloCobranca": 0,
            }
            return httpx.Response(
                201,
                json={
                    "numero": numero,
                    "linhaDigitavel": "00190000090 01234",
                    "codigoBarraNumerico": "00190000090",
                    "qrCode": {"url": "pix.example/x", "txId": "tx1", "emv": "000201"},
                },
            )
        partes = caminho.split("/")
        id_banco = partes[4] if len(partes) > 4 else ""
        if id_banco not in self.boletos:
            return httpx.Response(404, json={"erros": [{"mensagem": "nao achou"}]})
        boleto = self.boletos[id_banco]
        if caminho.endswith("/baixar"):
            boleto["codigoEstadoTituloCobranca"] = 7
            return httpx.Response(
                200, json={"numeroContratoCobranca": "1", "dataBaixa": "11.11.2026"}
            )
        if request.method == "PATCH":
            alt = self.ultimo_corpo.get("alteracaoData")
            if alt:
                boleto["dataVencimentoTituloCobranca"] = alt["novaDataVencimento"]
            ab = self.ultimo_corpo.get("abatimento") or self.ultimo_corpo.get(
                "alteracaoAbatimento"
            )
            if ab:
                boleto["valorAbatimentoTituloCobranca"] = next(iter(ab.values()))
            return httpx.Response(200, json={"dataAtualizacao": "11.11.2026"})
        return httpx.Response(200, json=boleto)
