"""Servidor falso do C6 Bank (Bolepix v2) sobre httpx.MockTransport, com estado.

Formato montado da especificacao OpenAPI publica do C6 (`bolepix.yaml` e o guia de
migracao, ver docs/c6-duvidas.md); nao e captura do sandbox real.
"""

import base64
import json
from urllib.parse import parse_qs

import httpx

PDF = b"%PDF-1.4 falso"
BASE = "/v2/bank_slips"


class C6Falso:
    def __init__(self, *, pagamentos_na_consulta: bool = False) -> None:
        self.cobrancas: dict[str, dict] = {}
        self.pagamentos: dict[str, list[dict]] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultima_query: dict[str, str] = {}
        self.pagamentos_na_consulta = pagamentos_na_consulta
        self.sem_pix = False
        self.pdf_em_json = False

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, referencia: str) -> None:
        self.cobrancas[referencia]["status"] = "PAID"
        self.pagamentos[referencia] = [
            {
                "amount": "100.00",
                "external_payment_id": "01KVG41MYPFJRCJE4AVK9NCFQ1",
                "payment_date": "2026-11-09",
                "credit_date": "2026-11-10",
                "payment_type": "PIX",
            },
            {
                "amount": "50.50",
                "external_payment_id": "01KVG41MYPFJRCJE4AVK9NCFQ2",
                "payment_date": "2026-11-10",
                "credit_date": "2026-11-11",
                "payment_type": "BANK_SLIP",
            },
        ]

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.ultimo_form = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": f"tok-{self.tokens_emitidos}",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    def _criar(self) -> httpx.Response:
        corpo = self.ultimo_corpo
        referencia = corpo["external_reference_id"]
        if referencia in self.cobrancas:
            # Duplicidade: o C6 devolve a cobranca ja existente.
            return httpx.Response(201, json=self.cobrancas[referencia])
        numero = str(10198966 + len(self.cobrancas))
        pix = corpo["payment_method"].get("pix")
        cobranca = {
            "id": f"01KP9CS9RW5EDMMG30ZVE7AA{len(self.cobrancas):02d}",
            "external_reference_id": referencia,
            "amount": corpo["amount"],
            "due_date": corpo["due_date"],
            "emission_date": "2026-11-01",
            "description": corpo["description"],
            "status": "CREATED",
            "payer": corpo["payer"],
            "payment_method": {
                "bank_slip": {
                    "originator_id": "000006242018",
                    "billing_scheme": corpo["payment_method"]["bank_slip"][
                        "billing_scheme"
                    ],
                    "billing_type": "3",
                    "bar_code": "33693143200000109250000062420180010198966213",
                    "digitable_line": "33690.00009 62420.180010 01989.662133 3",
                    "our_number": numero,
                    "number": referencia,
                },
                "pix": (
                    None
                    if self.sem_pix or not pix
                    else {
                        "qr_code": "00020101021226990014br.gov.bcb.pix",
                        "image_content": "iVBORw0KGgo=",
                        "mime_type": "image/png",
                        "reference": "QRS1TXJ0YVADX5MH56SOJQJXKEOEBMP3B95",
                    }
                ),
            },
        }
        self.cobrancas[referencia] = cobranca
        return httpx.Response(201, json=cobranca)

    def _listar(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        if not any(k.endswith("_from") for k in query):
            return httpx.Response(400, json={"status": 400, "title": "sem datas"})
        conteudo = [
            {**cobranca, "payments": self.pagamentos.get(ref, [])}
            for ref, cobranca in self.cobrancas.items()
            if ref == query.get("external_reference_id")
            and query.get("due_date_from") <= cobranca["due_date"]
            and cobranca["due_date"] <= query.get("due_date_to")
        ]
        return httpx.Response(
            200,
            json={
                "content": conteudo,
                "total_elements": len(conteudo),
                "total_pages": 1,
                "page": 0,
                "size": 20,
            },
        )

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho == "/v1/auth/":
            return self._token(request)
        self.ultima_query = dict(request.url.params)
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == BASE:
            return self._criar()
        if request.method == "GET" and caminho == f"{BASE}/list":
            return self._listar(request)
        partes = caminho.split("/")
        referencia = partes[3] if len(partes) > 3 else ""
        if referencia not in self.cobrancas:
            return httpx.Response(
                404,
                json={
                    "type": "https://developers.c6bank.com.br/v1/error/not_found",
                    "title": "Not Found",
                    "status": 404,
                    "correlation_id": "abc",
                },
            )
        cobranca = self.cobrancas[referencia]
        if caminho.endswith("/cancel"):
            if cobranca["status"] != "CREATED":
                return httpx.Response(422, json={"status": 422, "title": "terminal"})
            cobranca["status"] = "CANCELED"
            return httpx.Response(204)
        if caminho.endswith("/pdf"):
            if self.pdf_em_json:
                return httpx.Response(
                    200, json={"base64_pdf_file": base64.b64encode(PDF).decode()}
                )
            return httpx.Response(
                200, content=PDF, headers={"Content-Type": "application/pdf"}
            )
        if request.method == "PATCH":
            for chave in ("amount", "due_date", "description"):
                if chave in self.ultimo_corpo:
                    cobranca[chave] = self.ultimo_corpo[chave]
            return httpx.Response(200, json=cobranca)
        resposta = dict(cobranca)
        if self.pagamentos_na_consulta and referencia in self.pagamentos:
            resposta["payments"] = self.pagamentos[referencia]
        return httpx.Response(200, json=resposta)
