"""Servidor falso do Santander (Gestao de Boletos v2) sobre httpx.MockTransport.

Formato inferido do ACBr e de bibliotecas de terceiros (docs/santander-duvidas.md);
nao e captura do ambiente real. Tem estado: registro, instrucao e consulta.
"""

import json
from urllib.parse import parse_qs, unquote

import httpx

API = "/collection_bill_management/v2"
WORKSPACE = "ws-1"


class SantanderFalso:
    def __init__(self, *, devolve_linha: bool = True) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.urls: list[str] = []
        self.tokens_emitidos = 0
        self.pedido_token: dict[str, list[str]] = {}
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.devolve_linha = devolve_linha

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, id_banco: str) -> None:
        boleto = self.boletos[id_banco]
        boleto["status"] = "LIQUIDADO"
        boleto["paidValue"] = boleto["nominalValue"]
        boleto["_settlement"] = {
            "settlementData": [
                {
                    "settlementDate": "2026-11-10",
                    "settlementCreditDate": "2026-11-11",
                    "settlementCreditedValue": "148.50",
                    "settlementDutyValue": "1.50",
                }
            ]
        }

    def liquidar_por_pix(self, id_banco: str) -> None:
        boleto = self.boletos[id_banco]
        boleto["status"] = "BAIXADO"
        boleto["_settlement"] = {
            "writeOffData": [
                {
                    "writeOffDate": "2026-11-09",
                    "writeOffValue": 150.0,
                    "writeOffDescription": "BAIXA DE PAGAMENTO VIA PIX",
                }
            ]
        }

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.pedido_token = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 900},
        )

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.urls.append(str(request.url))
        if caminho == "/auth/oauth/v2/token":
            return self._token(request)
        self.ultimo_cabecalho = dict(request.headers)
        if request.headers.get("X-Application-Key") != "cid":
            return httpx.Response(401, json={"_message": "sem application key"})
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        slips = f"{API}/workspaces/{WORKSPACE}/bank_slips"
        if caminho == slips and request.method == "POST":
            return self._registrar()
        if caminho == slips and request.method == "PATCH":
            return self._instruir()
        if caminho == f"{API}/bills" and request.method == "GET":
            nosso = request.url.params.get("bankNumber", "")
            if nosso not in self.boletos:
                return self._nao_encontrado()
            return httpx.Response(200, json=self._publico(self.boletos[nosso]))
        if caminho.startswith(f"{API}/bills/") and request.method == "GET":
            bill_id = unquote(caminho.rsplit("/", 1)[-1])
            _, _, nosso = bill_id.partition(".")
            if nosso not in self.boletos:
                return self._nao_encontrado()
            return self._por_tipo(
                self.boletos[nosso], request.url.params.get("tipoConsulta")
            )
        return httpx.Response(404, json={"_message": "rota desconhecida"})

    @staticmethod
    def _nao_encontrado() -> httpx.Response:
        return httpx.Response(
            404, json={"_errorCode": 404, "_message": "Boleto nao encontrado"}
        )

    @staticmethod
    def _publico(boleto: dict) -> dict:
        return {k: v for k, v in boleto.items() if not k.startswith("_")}

    def _registrar(self) -> httpx.Response:
        corpo = self.ultimo_corpo
        nosso = corpo["bankNumber"]
        if nosso in self.boletos:
            return httpx.Response(
                409,
                json={
                    "_errorCode": 409,
                    "_message": "Conflict",
                    "_errors": [{"_code": "x", "_message": "ja registrado"}],
                },
            )
        boleto = {
            "covenantCode": corpo["covenantCode"],
            "bankNumber": nosso,
            "clientNumber": corpo["clientNumber"],
            "dueDate": corpo["dueDate"],
            "issueDate": corpo["issueDate"],
            "nominalValue": corpo["nominalValue"],
            "status": "ATIVO",
            "payer": corpo["payer"],
        }
        self.boletos[nosso] = boleto
        resposta = {
            "nsuCode": corpo["nsuCode"],
            "nsuDate": corpo["nsuDate"],
            "environment": corpo["environment"],
            **self._publico(boleto),
        }
        resposta.pop("status")
        if self.devolve_linha:
            resposta["barcode"] = "03391000000000000"
            resposta["digitableLine"] = "03399.00000 00000.000000"
        if "key" in corpo:
            resposta["qrCodePix"] = "00020101021226..."
            resposta["qrCodeUrl"] = "https://pix.exemplo/qr"
        return httpx.Response(201, json=resposta)

    def _instruir(self) -> httpx.Response:
        corpo = self.ultimo_corpo
        boleto = self.boletos.get(corpo.get("bankNumber", ""))
        if boleto is None:
            return self._nao_encontrado()
        operacao = corpo.get("operation")
        if operacao == "BAIXAR":
            boleto["status"] = "BAIXADO"
            boleto["_settlement"] = {
                "writeOffData": [
                    {
                        "writeOffDate": "2026-11-02",
                        "writeOffDescription": "BAIXA POR SOLICITACAO",
                    }
                ]
            }
        elif operacao == "PROTESTAR":
            boleto["_protesto"] = True
        if "dueDate" in corpo:
            boleto["dueDate"] = corpo["dueDate"]
        if "deductionValue" in corpo:
            boleto["deductionValue"] = corpo["deductionValue"]
        return httpx.Response(
            200,
            json={
                "covenantCode": corpo["covenantCode"],
                "bankNumber": boleto["bankNumber"],
            },
        )

    def _por_tipo(self, boleto: dict, tipo: str | None) -> httpx.Response:
        base = {"bankNumber": boleto["bankNumber"], "status": boleto["status"]}
        if tipo == "bankslip":
            return httpx.Response(
                200,
                json={
                    **base,
                    "bankSlipData": {
                        "digitableLine": "03399.00000 00000.000000",
                        "barCode": "03391000000000000",
                    },
                    "payerData": {"payerName": boleto["payer"]["name"]},
                    "qrCodeData": {"qrCode": "00020101021226..."},
                },
            )
        if tipo == "settlement":
            return httpx.Response(
                200,
                json={**base, "settlementData": [], **boleto.get("_settlement", {})},
            )
        return httpx.Response(400, json={"_message": f"tipoConsulta {tipo!r}"})
