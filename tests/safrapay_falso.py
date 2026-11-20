"""Servidor falso do Safrapay (gateway v2) sobre httpx.MockTransport, com estado.

Reflete a documentacao do portal developers.safrapay.com.br lida em 2026-11-19
(docs/safrapay-duvidas.md): `POST /v2/merchant/auth` com o MerchantToken cru no
`Authorization` devolve `accessToken`; as demais rotas exigem `Bearer` e respondem
401 sem corpo sem ele. Envelope `{success, errors[{errorCode, message, field}],
traceKey}`, com falha de validacao em HTTP 200 e `success: false` quando
`erro_em_200`. Boleto em `POST /v2/charge/boleto` (minimo 500 centavos, endereco e
e-mail do pagador obrigatorios, uma transacao), Pix em `POST /v2/charge/pix` (QR copia
e cola), consulta em `GET /v2/charge/{id}` (id invalido: 71; inexistente: 8) e por
`merchantChargeId`, novo vencimento em `PUT /v2/charge/boleto` (fica
`PendingNewDeadline`), cancelamento em `PUT /v2/charge/cancelation/{id}` (boleto no
mesmo dia: 195, com `regra_dia_seguinte`) e PDF publico em
`GET /v2/charge/{id}/boleto/{transactionId}`.
"""

import base64
import json
import uuid
from datetime import date

import httpx

CHAVE = "mk_chave-falsa-000"
PDF = b"%PDF-1.4\n% boleto falso do safrapay\n"
_STATUS_WEBHOOK = {
    "ABERTO": 7,
    "PAGO": 8,
    "CANCELADO": 5,
    "EXPIRADO": 10,
    "NOVO_VENCIMENTO": 11,
}


def autorizacao_webhook(chave: str = CHAVE) -> str:
    """`Authorization` que o Safrapay manda em cada notificacao."""
    return base64.b64encode(chave.encode()).decode()


def webhook(
    id_banco: str,
    situacao: str = "PAGO",
    *,
    tipo_pagamento: int = 8,
    valor: int = 15000,
) -> bytes:
    """Notificacao `Updated` (WebhookCall, PascalCase), como a documentacao mostra."""
    status = _STATUS_WEBHOOK.get(situacao, 8)
    return json.dumps(
        {
            "ChargeId": id_banco,
            "MerchantId": "91cd6a1b-340d-4a5e-8820-cb814c093cf1",
            "Nsu": "000008330",
            "Customer": {
                "Email": "cliente@exemplo.com",
                "Name": "Cliente Exemplo",
                "Document": "529*****725",
                "DocumentType": 1,
            },
            "Transactions": [
                {
                    "IsApproved": True,
                    "PaymentType": tipo_pagamento,
                    "Amount": valor,
                    "TransactionStatus": status,
                    "TransactionId": "87910e25-a07d-4f8c-9722-01713f24bd9d",
                    "EndToEnd": "E58160789202506111638qqyFmZ02pzq",
                    "PixNsu": "454965989199",
                    "Payer": {
                        "Document": "606*****103",
                        "Name": "PAGADOR EXEMPLO",
                        "IspbCode": "58160789",
                    },
                }
            ],
            "ChargeStatus": 1 if status == 8 else 2,
            "MerchantChargeId": "chave-1",
            "DateTime": "2026-12-09T12:00:42.3684333-03:00",
        }
    ).encode()


def _erro(status: int, codigo: int, mensagem: str, campo: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "success": False,
            "errors": [{"errorCode": codigo, "message": mensagem, "field": campo}],
            "traceKey": str(uuid.uuid4()),
        },
    )


def _ok(**dados) -> httpx.Response:
    return httpx.Response(
        200,
        json={**dados, "success": True, "errors": [], "traceKey": str(uuid.uuid4())},
    )


class SafrapayFalso:
    def __init__(
        self,
        *,
        chave: str = CHAVE,
        regra_dia_seguinte: bool = False,
        erro_em_200: bool = False,
    ) -> None:
        self.chave = chave
        self.regra_dia_seguinte = regra_dia_seguinte
        self.erro_em_200 = erro_em_200
        self.cobrancas: dict[str, dict] = {}
        self.criadas_hoje: set[str] = set()
        self.tokens_validos: set[str] = set()
        self.tokens_emitidos = 0
        self.chamadas: list[tuple[str, str]] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.ultimo_corpo: dict = {}
        self.falhas_503 = 0
        # Quantos POST responder 503 (as demais rotas seguem normais).
        self.falhas_503_post = 0
        self._sequencia = 93

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    # --- estado controlado pelos testes ------------------------------------

    def _transacao(self, id_banco: str) -> dict:
        return self.cobrancas[id_banco]["transactions"][0]

    def liquidar(self, id_banco: str) -> None:
        self._transacao(id_banco)["transactionStatus"] = "Paid"
        self.cobrancas[id_banco]["chargeStatus"] = "Authorized"

    def expirar(self, id_banco: str) -> None:
        self._transacao(id_banco)["transactionStatus"] = "Expired"

    def definir_status(self, id_banco: str, status: str) -> None:
        self._transacao(id_banco)["transactionStatus"] = status

    def virar_o_dia(self) -> None:
        self.criadas_hoje.clear()

    def revogar_tokens(self) -> None:
        self.tokens_validos.clear()

    # --- rotas ---------------------------------------------------------------

    def _auth(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != self.chave or request.content:
            return httpx.Response(401)
        self.tokens_emitidos += 1
        token = f"jwt-falso-{self.tokens_emitidos}"
        self.tokens_validos.add(token)
        return _ok(accessToken=token, refreshToken=uuid.uuid4().hex)

    def _recusar(self, codigo: int, mensagem: str, campo: str = "") -> httpx.Response:
        return _erro(200 if self.erro_em_200 else 400, codigo, mensagem, campo)

    def _cliente_valido(
        self, cliente: dict, *, com_endereco: bool
    ) -> httpx.Response | None:
        if not isinstance(cliente, dict):
            return self._recusar(1, "A propriedade Customer e obrigatoria.", "customer")
        for campo in ("name", "document", "documentType"):
            if not cliente.get(campo):
                return self._recusar(1, f"Customer.{campo} vazio.", f"customer.{campo}")
        if com_endereco:
            if not cliente.get("email"):
                return self._recusar(
                    1,
                    "A propriedade Customer.Email e obrigatoria ou condicional "
                    "esta vazia.",
                    "customer.email",
                )
            endereco = cliente.get("address") or {}
            obrigatorios = (
                "street",
                "number",
                "neighborhood",
                "city",
                "state",
                "country",
                "zipCode",
            )
            if any(not endereco.get(c) for c in obrigatorios):
                return self._recusar(
                    157, "Endereco do pagador incompleto.", "customer.address"
                )
        return None

    def _criar(self, corpo: dict, meio: str) -> httpx.Response:
        cobranca = (corpo or {}).get("charge")
        if not isinstance(cobranca, dict):
            return self._recusar(1, "A propriedade Charge e obrigatoria.", "charge")
        boleto = meio == "BOLETO"
        if cobranca.get("source") != (7 if boleto else 1):
            return self._recusar(
                45, "Propriedade 'Source' e invalida.", "charge.source"
            )
        recusa = self._cliente_valido(cobranca.get("customer"), com_endereco=boleto)
        if recusa is not None:
            return recusa
        transacoes = cobranca.get("transactions")
        if not isinstance(transacoes, list) or len(transacoes) != 1:
            return self._recusar(
                45, "Informe exatamente uma transacao.", "charge.transactions"
            )
        valor = transacoes[0].get("amount")
        if not isinstance(valor, int) or valor <= 0:
            return self._recusar(15, "Valor invalido.", "transactions.amount")
        id_banco = str(uuid.uuid4())
        if boleto:
            if valor < 500:
                return self._recusar(
                    15, "Valor minimo do boleto e 500.", "transactions.amount"
                )
            vencimento = cobranca.get("deadline")
            if not vencimento:
                return self._recusar(
                    1, "A propriedade Deadline e obrigatoria.", "charge.deadline"
                )
            self._sequencia += 1
            numero = f"{self._sequencia:013d}"
            transacao = {
                "transactionId": numero,
                "digitalLine": "03399000378200000000000009401019686590000001000",
                "barcode": "03396865900000020009000381000000000000940101",
                "bankSlipId": numero,
                "deadline": f"{vencimento}T00:00:00",
                "paymentType": "Boleto",
                "amount": valor,
                "transactionStatus": "PendingPayment",
                "bankIssuerId": "Safra",
                "bankSlipUrl": f"/v2/charge/{id_banco}/boleto/{numero}",
                "creationDateTime": "2026-11-19T23:37:14.6061259Z",
            }
        else:
            transacao = {
                "transactionId": str(uuid.uuid4()),
                "paymentType": "Pix",
                "amount": valor,
                "transactionStatus": "PendingPayment",
                "qrCode": (
                    "00020101021226770014BR.GOV.BCB.PIX0136chave-falsa"
                    "5204000053039865802BR6304ABCD"
                ),
                "qrCodeBase64": "Qk1GTQEAAAAAADYAAAAo",
                "acquirer": "Safrapay",
                "bankIssuerId": "Safra",
            }
        registro = {
            "id": id_banco,
            "merchantChargeId": cobranca.get("merchantChargeId"),
            "chargeStatus": "PreAuthorized",
            "transactions": [transacao],
        }
        self.cobrancas[id_banco] = registro
        self.criadas_hoje.add(id_banco)
        return _ok(charge=registro)

    def _consultar(self, id_texto: str) -> httpx.Response:
        try:
            uuid.UUID(id_texto)
        except ValueError:
            return _erro(400, 71, "Formato de GUID invalido.", "chargeId")
        if id_texto not in self.cobrancas:
            return _erro(400, 8, f"Cobranca com o ID '{id_texto}' nao encontrada.")
        return _ok(charge=self.cobrancas[id_texto])

    def _por_referencia(self, request: httpx.Request) -> httpx.Response:
        referencia = request.url.params.get("merchantChargeId")
        for cobranca in self.cobrancas.values():
            if cobranca.get("merchantChargeId") == referencia:
                return _ok(charge=cobranca)
        return _erro(400, 8, "Cobranca nao encontrada.")

    def _novo_vencimento(self, corpo: dict) -> httpx.Response:
        id_banco = str((corpo or {}).get("chargeId") or "")
        if id_banco not in self.cobrancas:
            return _erro(400, 8, "Cobranca nao encontrada.")
        transacao = self._transacao(id_banco)
        if (
            transacao.get("paymentType") != "Boleto"
            or transacao["transactionStatus"] != "PendingPayment"
        ):
            return self._recusar(
                154, "Nao foi possivel alterar a data de vencimento do boleto"
            )
        nova = str(corpo.get("newDueDate") or "")[:10]
        if not nova or date.fromisoformat(nova) <= date.fromisoformat(
            transacao["deadline"][:10]
        ):
            return self._recusar(
                67, "A data deve ser maior ou igual que a data corrente"
            )
        transacao["deadline"] = f"{nova}T00:00:00"
        transacao["transactionStatus"] = "PendingNewDeadline"
        return _ok(
            bankSlipTransactionResult={
                "bankSlipId": transacao["bankSlipId"],
                "deadline": transacao["deadline"],
                "amount": transacao["amount"],
                "transactionStatus": "PendingNewDeadline",
                "bankIssuerId": "Safra",
                "status": "Pending",
            }
        )

    def _cancelar(self, id_texto: str) -> httpx.Response:
        if id_texto not in self.cobrancas:
            return _erro(400, 8, "Cobranca nao encontrada.")
        transacao = self._transacao(id_texto)
        boleto = transacao.get("paymentType") == "Boleto"
        if boleto and self.regra_dia_seguinte and id_texto in self.criadas_hoje:
            return self._recusar(
                195, "Boleto so pode ser cancelado um dia apos ser criado."
            )
        if boleto and transacao["transactionStatus"] == "Paid":
            return self._recusar(37, "Transacao nao pode ser cancelada.")
        transacao["transactionStatus"] = "Canceled"
        self.cobrancas[id_texto]["chargeStatus"] = "Canceled"
        return _ok(charge=self.cobrancas[id_texto])

    # --- despacho ----------------------------------------------------------------

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.cabecalhos.append(dict(request.headers))
        partes = caminho.strip("/").split("/")
        if partes == ["v2", "merchant", "auth"] and request.method == "POST":
            return self._auth(request)
        if (
            request.method == "GET"
            and len(partes) == 5
            and partes[:2] == ["v2", "charge"]
            and partes[3] == "boleto"
        ):
            cobranca = self.cobrancas.get(partes[2])
            if (
                cobranca
                and cobranca["transactions"][0].get("transactionId") == partes[4]
            ):
                return httpx.Response(
                    200, content=PDF, headers={"Content-Type": "application/pdf"}
                )
            return _erro(400, 0, "Cobranca ou transacao nao encontrada.")
        autorizacao = request.headers.get("authorization", "")
        if (
            not autorizacao.startswith("Bearer ")
            or autorizacao[7:] not in self.tokens_validos
        ):
            return httpx.Response(401)
        if self.falhas_503:
            self.falhas_503 -= 1
            return httpx.Response(503, text="indisponivel")
        if request.method == "POST" and self.falhas_503_post:
            self.falhas_503_post -= 1
            return httpx.Response(503, text="indisponivel")
        corpo = json.loads(request.content) if request.content else None
        if corpo is not None:
            self.ultimo_corpo = corpo
        if partes[:2] != ["v2", "charge"]:
            return _erro(404, 0, "Rota desconhecida.")
        if request.method == "POST" and partes[2:] == ["boleto"]:
            return self._criar(corpo, "BOLETO")
        if request.method == "POST" and partes[2:] == ["pix"]:
            return self._criar(corpo, "PIX")
        if request.method == "PUT" and partes[2:] == ["boleto"]:
            return self._novo_vencimento(corpo)
        if request.method == "PUT" and len(partes) == 4 and partes[2] == "cancelation":
            return self._cancelar(partes[3])
        if request.method == "GET" and len(partes) == 2:
            return self._por_referencia(request)
        if request.method == "GET" and len(partes) == 3:
            return self._consultar(partes[2])
        return _erro(404, 0, "Rota desconhecida.")
