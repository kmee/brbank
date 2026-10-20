"""Servidor falso do BTG Empresas (Banking - Collections) sobre httpx.MockTransport.

Formato tirado da spec OpenAPI publicada no portal do BTG (docs/btg-duvidas.md);
nao e captura do sandbox real. A criacao responde PROCESSING e a consulta seguinte
ja ve CREATED, imitando a emissao assincrona.
"""

import base64
import json
from urllib.parse import parse_qs

import httpx

CLIENT_ID = "app-id"
CLIENT_SECRET = "app-segredo"
REFRESH_INICIAL = "rt-0"


class BtgFalso:
    def __init__(self, *, rotacionar_refresh: bool = True) -> None:
        self.cobrancas: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.refresh_valido = REFRESH_INICIAL
        self.rotacionar_refresh = rotacionar_refresh
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_cabecalho_token: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.pdf = b"%PDF-1.4 falso"

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, collection_id: str, valor: float = 150.0) -> None:
        cobranca = self.cobrancas[collection_id]
        cobranca["status"] = "PAID"
        cobranca["amountPaid"] = valor
        cobranca["paidAt"] = "2026-12-09"
        cobranca["settledAt"] = "2026-12-10"
        cobranca["paymentMethod"] = "BANKSLIP"

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.ultimo_cabecalho_token = dict(request.headers)
        self.ultimo_form = parse_qs(request.content.decode())
        basico = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {basico}":
            return httpx.Response(401, json={"error": "invalid_client"})
        if self.ultimo_form.get("refresh_token") != [self.refresh_valido]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.tokens_emitidos += 1
        if self.rotacionar_refresh:
            self.refresh_valido = f"rt-{self.tokens_emitidos}"
        return httpx.Response(
            200,
            json={
                "access_token": f"tok-{self.tokens_emitidos}",
                "refresh_token": self.refresh_valido,
                "token_type": "Bearer",
                "expires_in": 86400,
                "scope": "openid brn:btg:empresas:banking:collections",
            },
        )

    def _criar(self, corpo: dict) -> httpx.Response:
        collection_id = f"col-{len(self.cobrancas) + 1}"
        hibrido = corpo["type"] == "BANKSLIP_QRCODE"
        detalhe = {
            "documentNumber": corpo["detail"]["documentNumber"],
            "ourNumber": f"0000{len(self.cobrancas) + 1}",
            "barCode": "20891000000000150000050000000000000000000000",
            "digitableLine": "20890050090000000000000000000000000000000150",
        }
        if hibrido:
            detalhe["emv"] = "00020101021226...6304ABCD"
        cobranca = {
            "collectionId": collection_id,
            "type": corpo["type"],
            "amount": corpo["amount"],
            "dueDate": corpo["dueDate"],
            "overDueDate": corpo["overDueDate"],
            "createdAt": "2026-11-01T10:00:00.000Z",
            "updatedAt": "2026-11-01T10:00:00.000Z",
            "payer": corpo["payer"],
            "interest": corpo.get("interest"),
            "fine": corpo.get("fine"),
            "discounts": corpo.get("discounts", []),
            "detail": detalhe,
            "status": "CREATED",
        }
        if "description" in corpo:
            cobranca["description"] = corpo["description"]
        self.cobrancas[collection_id] = cobranca
        resposta = {
            **cobranca,
            "status": "PROCESSING",
            "detail": {"documentNumber": detalhe["documentNumber"]},
        }
        return httpx.Response(201, json=resposta)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        # raw_path: o id vai escapado na URL e o teste confere o escape.
        caminho = request.url.raw_path.decode().split("?")[0]
        self.chamadas.append((request.method, caminho))
        if caminho == "/oauth2/token":
            return self._token(request)
        self.ultimo_cabecalho = dict(request.headers)
        if request.headers.get("authorization", "").split(" ")[-1] != (
            f"tok-{self.tokens_emitidos}"
        ):
            return httpx.Response(401, json={"message": "token invalido"})
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        partes = caminho.strip("/").split("/")
        if partes[1:3] != ["banking", "collections"]:
            return httpx.Response(404)
        if len(partes) == 3 and request.method == "POST":
            return self._criar(self.ultimo_corpo)
        collection_id = partes[3] if len(partes) > 3 else ""
        cobranca = self.cobrancas.get(collection_id)
        if cobranca is None:
            return httpx.Response(
                404,
                json={
                    "Message": "Cobranca nao encontrada",
                    "Details": {"brn": "brn:btg:collections", "name": "NotFound"},
                },
            )
        if request.method == "GET":
            if request.headers.get("accept") == "application/pdf":
                return httpx.Response(
                    200, content=self.pdf, headers={"Content-Type": "application/pdf"}
                )
            return httpx.Response(200, json=cobranca)
        if request.method == "DELETE":
            cobranca["status"] = "CANCELED"
            return httpx.Response(204)
        if request.method == "PUT":
            for campo in (
                "dueDate",
                "overDueDate",
                "amount",
                "discounts",
                "interest",
                "fine",
                "rebate",
                "description",
            ):
                if campo in self.ultimo_corpo:
                    cobranca[campo] = self.ultimo_corpo[campo]
            return httpx.Response(200, json=cobranca)
        return httpx.Response(405)
