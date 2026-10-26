"""Servidor falso do Asaas v3 sobre httpx.MockTransport, com estado.

Reflete a OpenAPI oficial lida em 2026-10-21 (docs/asaas-duvidas.md): chave no
header `access_token` (sem ela ou errada, 401 `invalid_access_token`),
`User-Agent` obrigatorio, cliente obrigatorio na cobranca (`invalid_customer`),
cobranca nasce `PENDING` com `nossoNumero`, `bankSlipUrl` e `invoiceUrl`,
linha digitavel em `/identificationField`, QR em `/pixQrCode` (so quando a
conta tem chave Pix ou o meio e PIX), `PUT` exige `billingType`, `value` e
`dueDate` e so vale em `PENDING`/`OVERDUE`, `DELETE` de cobranca paga da 400,
cobranca removida continua legivel com `deleted: true`, erro de negocio em
`errors[{code, description}]`, e `POST /sandbox/payment/{id}/confirm` liquida.
O PDF e servido fora da API, no host do `bankSlipUrl`, sem chave.

Assinaturas: `POST /subscriptions` nasce `ACTIVE`; as cobrancas de cada ciclo
sao geradas por `gerar_ciclos` (o que o Asaas faz 40 dias antes do
vencimento) ou pelo carne (`GET /subscriptions/{id}/paymentBook`, que no
sandbox antecipa a criacao); `PUT` aceita `status`, `nextDueDate`, `cycle`,
`value` e `updatePendingPayments`; `DELETE` remove as cobrancas abertas.
Checkout: `POST /checkouts` exige `imageBase64` por item e `callback`;
`pagar_checkout` simula o pagador e cria a assinatura com `checkoutSession`.
"""

import json
from datetime import date, timedelta

import httpx

CHAVE_API = "$aact_hmlg_chave-falsa-000"
HOST_PDF = "https://sandbox.asaas.com"
PDF = b"%PDF-1.4\n% boleto falso do asaas\n"
MEIOS = ("UNDEFINED", "BOLETO", "CREDIT_CARD", "PIX")
CICLOS = {
    "WEEKLY": 7,
    "BIWEEKLY": 14,
    "MONTHLY": 30,
    "BIMONTHLY": 61,
    "QUARTERLY": 91,
    "SEMIANNUALLY": 182,
    "YEARLY": 365,
}
_SITUACOES = {
    "PAGO": ("PAYMENT_RECEIVED", "RECEIVED", False),
    "CONFIRMADO": ("PAYMENT_CONFIRMED", "CONFIRMED", False),
    "VENCIDO": ("PAYMENT_OVERDUE", "OVERDUE", False),
    "REMOVIDO": ("PAYMENT_DELETED", "PENDING", True),
    "ESTORNADO": ("PAYMENT_REFUNDED", "REFUNDED", False),
}


def _proximo_vencimento(vencimento: str, ciclo: str) -> str:
    # Aproximacao em dias; o teste so precisa de datas crescentes.
    return (date.fromisoformat(vencimento) + timedelta(days=CICLOS[ciclo])).isoformat()


def _erro(status: int, codigo: str, descricao: str) -> httpx.Response:
    return httpx.Response(
        status, json={"errors": [{"code": codigo, "description": descricao}]}
    )


def _lista(itens: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "hasMore": False,
            "totalCount": len(itens),
            "limit": 10,
            "offset": 0,
            "data": itens,
        },
    )


def webhook(
    id_banco: str, situacao: str = "PAGO", *, evento: str | None = None
) -> bytes:
    """Corpo de notificacao de cobranca, como o Asaas envia."""
    nome, status, removida = _SITUACOES.get(situacao, _SITUACOES["PAGO"])
    return json.dumps(
        {
            "id": f"evt_{id_banco}_{status.lower()}",
            "event": evento or nome,
            "dateCreated": "2026-12-09 10:15:30",
            "account": {"id": "conta-falsa", "ownerId": None},
            "payment": {
                "object": "payment",
                "id": id_banco,
                "customer": "cus_000001",
                "value": 150.0,
                "netValue": 148.01,
                "billingType": "BOLETO",
                "status": status,
                "deleted": removida,
                "dueDate": "2026-12-10",
                "paymentDate": "2026-12-09" if status == "RECEIVED" else None,
                "clientPaymentDate": "2026-12-09" if status == "RECEIVED" else None,
                "externalReference": "chave-1",
            },
        }
    ).encode()


def webhook_assinatura(
    id_banco: str, evento: str = "SUBSCRIPTION_CREATED", *, checkout: str | None = None
) -> bytes:
    return json.dumps(
        {
            "id": f"evt_{id_banco}_{evento.lower()}",
            "event": evento,
            "dateCreated": "2026-12-01 09:00:00",
            "account": {"id": "conta-falsa", "ownerId": None},
            "subscription": {
                "object": "subscription",
                "id": id_banco,
                "customer": "cus_000001",
                "value": 99.9,
                "nextDueDate": "2027-01-10",
                "cycle": "MONTHLY",
                "billingType": "CREDIT_CARD",
                "status": (
                    "INACTIVE" if evento == "SUBSCRIPTION_INACTIVATED" else "ACTIVE"
                ),
                "deleted": evento == "SUBSCRIPTION_DELETED",
                "externalReference": "contrato-1",
                "checkoutSession": checkout,
            },
        }
    ).encode()


def webhook_checkout(id_banco: str, evento: str = "CHECKOUT_PAID") -> bytes:
    status = {
        "CHECKOUT_CREATED": "ACTIVE",
        "CHECKOUT_PAID": "PAID",
        "CHECKOUT_CANCELED": "CANCELED",
        "CHECKOUT_EXPIRED": "EXPIRED",
    }[evento]
    return json.dumps(
        {
            "id": f"evt_{id_banco}_{evento.lower()}",
            "event": evento,
            "dateCreated": "2026-12-01 09:05:00",
            "account": {"id": "conta-falsa", "ownerId": None},
            "checkout": {
                "id": id_banco,
                "link": None,
                "status": status,
                "chargeTypes": ["RECURRENT"],
                "billingTypes": ["CREDIT_CARD"],
                "subscription": {"cycle": "MONTHLY", "nextDueDate": "2027-01-10"},
                "customer": "cus_000001",
            },
        }
    ).encode()


class AsaasFalso:
    def __init__(self, *, chave: str = CHAVE_API, com_chave_pix: bool = True) -> None:
        self.chave = chave
        self.com_chave_pix = com_chave_pix
        self.clientes: dict[str, dict] = {}
        self.cobrancas: dict[str, dict] = {}
        self.assinaturas: dict[str, dict] = {}
        self.checkouts: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultima_query: dict[str, str] = {}
        # Quantas respostas 429 devolver antes de voltar a atender.
        self.falhas_429 = 0

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    # --- estado controlado pelos testes ----------------------------------

    def cadastrar_cliente(self, documento: str, **dados) -> str:
        cliente_id = f"cus_{len(self.clientes) + 1:06d}"
        self.clientes[cliente_id] = {
            "object": "customer",
            "id": cliente_id,
            "cpfCnpj": documento,
            "deleted": False,
            **dados,
        }
        return cliente_id

    def confirmar(self, id_banco: str) -> None:
        cobranca = self.cobrancas[id_banco]
        cobranca["originalValue"] = cobranca["value"]
        cobranca["status"] = "RECEIVED"
        cobranca["paymentDate"] = "2026-12-09"
        cobranca["clientPaymentDate"] = "2026-12-09"

    def vencer(self, id_banco: str) -> None:
        self.cobrancas[id_banco]["status"] = "OVERDUE"

    def gerar_ciclos(self, assinatura_id: str, quantidade: int) -> list[str]:
        """Cria as proximas cobrancas da assinatura, como o Asaas faz 40 dias
        antes de cada vencimento (ou pelo carne, no sandbox)."""
        assinatura = self.assinaturas[assinatura_id]
        criadas = []
        for _ in range(quantidade):
            if assinatura["status"] != "ACTIVE" or assinatura["deleted"]:
                break
            vencimento = assinatura["nextDueDate"]
            resp = self._criar_cobranca(
                {
                    "customer": assinatura["customer"],
                    "billingType": assinatura["billingType"],
                    "value": assinatura["value"],
                    "dueDate": vencimento,
                    "description": assinatura.get("description"),
                    "subscription": assinatura_id,
                }
            )
            cobranca_id = json.loads(resp.content)["id"]
            criadas.append(cobranca_id)
            assinatura["nextDueDate"] = _proximo_vencimento(
                vencimento, assinatura["cycle"]
            )
        return criadas

    def pagar_checkout(self, checkout_id: str) -> str:
        """Simula o pagador concluindo o checkout: cria a assinatura ligada."""
        checkout = self.checkouts[checkout_id]
        checkout["status"] = "PAID"
        dados = checkout["customerData"] or {}
        cliente_id = self.cadastrar_cliente(
            dados.get("cpfCnpj", "00000000000"), name=dados.get("name", "")
        )
        resp = self._criar_assinatura(
            {
                "customer": cliente_id,
                "billingType": checkout["billingTypes"][0],
                "cycle": checkout["subscription"]["cycle"],
                "value": checkout["items"][0]["value"],
                "nextDueDate": checkout["subscription"]["nextDueDate"][:10],
                "endDate": (checkout["subscription"].get("endDate") or "")[:10] or None,
                "description": checkout["items"][0]["name"],
                "_checkoutSession": checkout_id,
                "_cartao": True,
            }
        )
        return json.loads(resp.content)["id"]

    # --- clientes -------------------------------------------------------------

    def _clientes(self, request: httpx.Request) -> httpx.Response:
        documento = request.url.params.get("cpfCnpj")
        referencia = request.url.params.get("externalReference")
        itens = [
            c
            for c in self.clientes.values()
            if (not documento or c["cpfCnpj"] == documento)
            and (not referencia or c.get("externalReference") == referencia)
        ]
        return _lista(itens)

    def _criar_cliente(self, corpo) -> httpx.Response:
        if not isinstance(corpo, dict) or not corpo.get("name"):
            return _erro(400, "invalid_name", "O nome deve ser informado.")
        if not corpo.get("cpfCnpj"):
            return _erro(400, "invalid_cpfCnpj", "O CPF ou CNPJ deve ser informado.")
        cliente_id = self.cadastrar_cliente(
            corpo["cpfCnpj"], **{k: v for k, v in corpo.items() if k != "cpfCnpj"}
        )
        return httpx.Response(200, json=self.clientes[cliente_id])

    # --- cobrancas ------------------------------------------------------------

    def _listar_cobrancas(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        itens = [
            c
            for c in self.cobrancas.values()
            if all(
                not params.get(chave) or str(c.get(chave)) == params.get(chave)
                for chave in ("externalReference", "customer", "status", "subscription")
            )
        ]
        return _lista(itens)

    def _criar_cobranca(self, corpo) -> httpx.Response:
        if not isinstance(corpo, dict):
            return _erro(400, "invalid_object", "Corpo invalido.")
        if corpo.get("customer") not in self.clientes:
            return _erro(400, "invalid_customer", "Cliente invalido ou nao informado.")
        if corpo.get("billingType") not in MEIOS:
            return _erro(400, "invalid_billingType", "Forma de pagamento invalida.")
        if corpo.get("billingType") == "CREDIT_CARD" and not corpo.get("subscription"):
            return _erro(400, "invalid_creditCard", "Dados do cartao nao informados.")
        if not corpo.get("value") or corpo["value"] <= 0:
            return _erro(400, "invalid_value", "O campo value deve ser informado.")
        if not corpo.get("dueDate"):
            return _erro(400, "invalid_dueDate", "A data de vencimento e obrigatoria.")
        if len(corpo.get("description") or "") > 500:
            return _erro(400, "invalid_description", "Maximo de 500 caracteres.")
        numero = len(self.cobrancas) + 1
        cobranca_id = f"pay_{numero:012d}"
        com_boleto = corpo["billingType"] in ("BOLETO", "UNDEFINED")
        cobranca = {
            "object": "payment",
            "id": cobranca_id,
            "dateCreated": "2026-11-01",
            "customer": corpo["customer"],
            "subscription": corpo.get("subscription"),
            "value": corpo["value"],
            "netValue": round(corpo["value"] - 1.99, 2),
            "originalValue": None,
            "description": corpo.get("description"),
            "billingType": corpo["billingType"],
            "status": "PENDING",
            "dueDate": corpo["dueDate"],
            "originalDueDate": corpo["dueDate"],
            "paymentDate": None,
            "clientPaymentDate": None,
            "invoiceUrl": f"https://sandbox.asaas.com/i/{numero:012d}",
            "invoiceNumber": f"{numero:08d}",
            "externalReference": corpo.get("externalReference"),
            "deleted": False,
            "nossoNumero": f"{numero:08d}" if com_boleto else None,
            "bankSlipUrl": f"{HOST_PDF}/b/pdf/{numero:012d}" if com_boleto else None,
            "discount": corpo.get("discount"),
            "fine": corpo.get("fine"),
            "interest": corpo.get("interest"),
            "daysAfterDueDateToRegistrationCancellation": corpo.get(
                "daysAfterDueDateToRegistrationCancellation"
            ),
        }
        self.cobrancas[cobranca_id] = cobranca
        return httpx.Response(200, json=cobranca)

    def _cobranca(self, request: httpx.Request, partes: list[str], corpo):
        cobranca_id = partes[2]
        cobranca = self.cobrancas.get(cobranca_id)
        if cobranca is None:
            return _erro(404, "invalid_action", "Cobranca nao encontrada.")
        acao = partes[3] if len(partes) > 3 else ""
        metodo = request.method
        if metodo == "GET" and not acao:
            return httpx.Response(200, json=cobranca)
        if metodo == "GET" and acao == "identificationField":
            if cobranca["billingType"] == "PIX":
                return _erro(400, "invalid_action", "Cobranca sem boleto.")
            return httpx.Response(
                200,
                json={
                    "identificationField": "23793.38128 60007.827136 95000.063305 1 "
                    "84340000015000",
                    "nossoNumero": cobranca["nossoNumero"],
                    "barCode": "23791843400000150003381286000782713950006330",
                },
            )
        if metodo == "GET" and acao == "pixQrCode":
            if cobranca["billingType"] != "PIX" and not self.com_chave_pix:
                return _erro(404, "invalid_action", "Conta sem chave Pix.")
            return httpx.Response(
                200,
                json={
                    "encodedImage": "iVBORw0KGgo=",
                    "payload": f"00020101021226{cobranca_id}5204000053039865802BR",
                    "expirationDate": "2027-12-10 23:59:59",
                    "description": cobranca.get("description"),
                },
            )
        if metodo == "PUT" and not acao:
            if not isinstance(corpo, dict) or any(
                campo not in corpo for campo in ("billingType", "value", "dueDate")
            ):
                return _erro(400, "invalid_object", "billingType, value e dueDate.")
            if cobranca["status"] not in ("PENDING", "OVERDUE") or cobranca["deleted"]:
                return _erro(400, "invalid_action", "Cobranca nao pode ser alterada.")
            cobranca.update(
                {chave: corpo[chave] for chave in ("billingType", "value", "dueDate")}
            )
            for chave in ("description", "discount", "fine", "interest"):
                if chave in corpo:
                    cobranca[chave] = corpo[chave]
            return httpx.Response(200, json=cobranca)
        if metodo == "DELETE" and not acao:
            if cobranca["status"] in ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"):
                return _erro(
                    400, "invalid_action", "Cobranca paga nao pode ser removida."
                )
            cobranca["deleted"] = True
            return httpx.Response(200, json={"deleted": True, "id": cobranca_id})
        if metodo == "POST" and acao == "restore":
            cobranca["deleted"] = False
            return httpx.Response(200, json=cobranca)
        return _erro(404, "invalid_action", "Rota desconhecida.")

    # --- assinaturas ----------------------------------------------------------

    def _listar_assinaturas(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        itens = [
            a
            for a in self.assinaturas.values()
            if all(
                not params.get(chave) or str(a.get(chave)) == params.get(chave)
                for chave in ("externalReference", "customer", "status", "billingType")
            )
        ]
        return _lista(itens)

    def _criar_assinatura(self, corpo) -> httpx.Response:
        if not isinstance(corpo, dict):
            return _erro(400, "invalid_object", "Corpo invalido.")
        if corpo.get("customer") not in self.clientes:
            return _erro(400, "invalid_customer", "Cliente invalido ou nao informado.")
        if corpo.get("billingType") not in MEIOS:
            return _erro(400, "invalid_billingType", "Forma de pagamento invalida.")
        if corpo.get("billingType") == "CREDIT_CARD" and not (
            corpo.get("creditCard") or corpo.get("_cartao")
        ):
            return _erro(400, "invalid_creditCard", "Dados do cartao nao informados.")
        if corpo.get("cycle") not in CICLOS:
            return _erro(400, "invalid_cycle", "Periodicidade invalida.")
        if not corpo.get("value") or corpo["value"] <= 0:
            return _erro(400, "invalid_value", "O campo value deve ser informado.")
        if not corpo.get("nextDueDate"):
            return _erro(400, "invalid_nextDueDate", "Informe o vencimento.")
        numero = len(self.assinaturas) + 1
        assinatura_id = f"sub_{numero:012d}"
        assinatura = {
            "object": "subscription",
            "id": assinatura_id,
            "dateCreated": "2026-11-01",
            "customer": corpo["customer"],
            "paymentLink": None,
            "billingType": corpo["billingType"],
            "cycle": corpo["cycle"],
            "value": corpo["value"],
            "nextDueDate": corpo["nextDueDate"],
            "endDate": corpo.get("endDate"),
            "description": corpo.get("description"),
            "status": "ACTIVE",
            "discount": corpo.get("discount"),
            "fine": corpo.get("fine"),
            "interest": corpo.get("interest"),
            "deleted": False,
            "maxPayments": corpo.get("maxPayments"),
            "externalReference": corpo.get("externalReference"),
            "checkoutSession": corpo.get("_checkoutSession"),
        }
        self.assinaturas[assinatura_id] = assinatura
        return httpx.Response(200, json=assinatura)

    def _assinatura(self, request: httpx.Request, partes: list[str], corpo):
        assinatura_id = partes[2]
        assinatura = self.assinaturas.get(assinatura_id)
        if assinatura is None:
            return _erro(404, "invalid_action", "Assinatura nao encontrada.")
        acao = partes[3] if len(partes) > 3 else ""
        metodo = request.method
        if metodo == "GET" and not acao:
            return httpx.Response(200, json=assinatura)
        if metodo == "GET" and acao == "payments":
            status = request.url.params.get("status")
            itens = [
                c
                for c in self.cobrancas.values()
                if c.get("subscription") == assinatura_id
                and (not status or c["status"] == status)
            ]
            return _lista(itens)
        if metodo == "GET" and acao == "paymentBook":
            mes = int(request.url.params.get("month", "12"))
            ano = int(request.url.params.get("year", "2027"))
            limite = f"{ano:04d}-{mes:02d}-31"
            while (
                assinatura["status"] == "ACTIVE"
                and not assinatura["deleted"]
                and assinatura["nextDueDate"] <= limite
            ):
                self.gerar_ciclos(assinatura_id, 1)
            return httpx.Response(200, content=PDF)
        if metodo == "PUT" and not acao:
            if not isinstance(corpo, dict):
                return _erro(400, "invalid_object", "Corpo invalido.")
            reativando = (
                corpo.get("status") == "ACTIVE" and assinatura["status"] != "ACTIVE"
            )
            if reativando and not corpo.get("nextDueDate"):
                return _erro(400, "invalid_nextDueDate", "Informe o novo vencimento.")
            if "cycle" in corpo and corpo["cycle"] not in CICLOS:
                return _erro(400, "invalid_cycle", "Periodicidade invalida.")
            for chave in (
                "billingType",
                "status",
                "nextDueDate",
                "cycle",
                "endDate",
                "description",
                "externalReference",
                "discount",
                "fine",
                "interest",
                "value",
            ):
                if chave in corpo:
                    assinatura[chave] = corpo[chave]
            if corpo.get("updatePendingPayments"):
                for cobranca in self.cobrancas.values():
                    if (
                        cobranca.get("subscription") == assinatura_id
                        and cobranca["status"] in ("PENDING", "OVERDUE")
                        and not cobranca["deleted"]
                    ):
                        for chave in ("value", "billingType", "description"):
                            if chave in corpo:
                                cobranca[chave] = corpo[chave]
            return httpx.Response(200, json=assinatura)
        if metodo == "DELETE" and not acao:
            assinatura["deleted"] = True
            for cobranca in self.cobrancas.values():
                if cobranca.get("subscription") == assinatura_id and cobranca[
                    "status"
                ] in ("PENDING", "OVERDUE"):
                    cobranca["deleted"] = True
            return httpx.Response(200, json={"deleted": True, "id": assinatura_id})
        return _erro(404, "invalid_action", "Rota desconhecida.")

    # --- checkout -------------------------------------------------------------

    def _criar_checkout(self, corpo) -> httpx.Response:
        if not isinstance(corpo, dict):
            return _erro(400, "invalid_object", "Corpo invalido.")
        for campo in ("billingTypes", "chargeTypes", "callback", "items"):
            if not corpo.get(campo):
                return _erro(400, f"invalid_{campo}", f"Informe {campo}.")
        dados = corpo.get("customerData")
        if dados and any(
            not dados.get(c)
            for c in ("phone", "address", "addressNumber", "postalCode", "province")
        ):
            return _erro(
                400, "invalid_object", "O campo phoneNumber deve ser informado."
            )
        if "RECURRENT" in corpo["chargeTypes"] and corpo["billingTypes"] != [
            "CREDIT_CARD"
        ]:
            return _erro(400, "invalid_object", "RECURRENT so aceita CREDIT_CARD.")
        if any(not item.get("imageBase64") for item in corpo["items"]):
            return _erro(400, "invalid_items", "Cada item exige imageBase64.")
        if any(not corpo["callback"].get(c) for c in ("successUrl", "cancelUrl")):
            return _erro(400, "invalid_callback", "successUrl e cancelUrl.")
        if "RECURRENT" in corpo["chargeTypes"] and not corpo.get("subscription"):
            return _erro(400, "invalid_subscription", "Checkout recorrente sem dados.")
        if set(corpo["billingTypes"]) - {"CREDIT_CARD", "PIX"}:
            return _erro(400, "invalid_billingTypes", "So cartao e Pix.")
        numero = len(self.checkouts) + 1
        checkout_id = f"00000000-0000-4000-8000-{numero:012d}"
        checkout = {
            "id": checkout_id,
            "link": f"https://sandbox.asaas.com/checkoutSession/show?id={checkout_id}",
            "status": "ACTIVE",
            "billingTypes": corpo["billingTypes"],
            "chargeTypes": corpo["chargeTypes"],
            "minutesToExpire": corpo.get("minutesToExpire"),
            "externalReference": corpo.get("externalReference"),
            "callback": corpo["callback"],
            "items": [
                {k: v for k, v in item.items() if k != "imageBase64"}
                for item in corpo["items"]
            ],
            "customerData": corpo.get("customerData"),
            "subscription": corpo.get("subscription"),
            "installment": corpo.get("installment"),
            "split": corpo.get("splits"),
        }
        self.checkouts[checkout_id] = checkout
        return httpx.Response(200, json=checkout)

    # --- despacho ---------------------------------------------------------------

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if request.url.host == "sandbox.asaas.com" and caminho.startswith("/b/pdf/"):
            numero = caminho.rsplit("/", 1)[-1]
            if any(c["id"].endswith(numero) for c in self.cobrancas.values()):
                return httpx.Response(200, content=PDF)
            return httpx.Response(404, text="nao encontrado")
        if request.headers.get("access_token") != self.chave:
            return _erro(401, "invalid_access_token", "A chave de API e invalida.")
        agente = request.headers.get("user-agent", "")
        if not agente or agente.startswith("python-httpx"):
            return _erro(400, "invalid_user_agent", "Informe o User-Agent.")
        if self.falhas_429:
            self.falhas_429 -= 1
            return httpx.Response(
                429,
                headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "5"},
                json={"errors": [{"code": "rate_limit", "description": "Aguarde."}]},
            )
        self.ultima_query = dict(request.url.params)
        corpo = json.loads(request.content) if request.content else None
        if corpo is not None:
            self.ultimo_corpo = corpo
        partes = caminho.strip("/").split("/")
        if partes[:2] == ["v3", "customers"] and len(partes) == 2:
            if request.method == "GET":
                return self._clientes(request)
            if request.method == "POST":
                return self._criar_cliente(corpo)
        if partes[:2] == ["v3", "payments"]:
            if len(partes) == 2 and request.method == "GET":
                return self._listar_cobrancas(request)
            if len(partes) == 2 and request.method == "POST":
                return self._criar_cobranca(corpo)
            return self._cobranca(request, partes, corpo)
        if partes[:2] == ["v3", "subscriptions"]:
            if len(partes) == 2 and request.method == "GET":
                return self._listar_assinaturas(request)
            if len(partes) == 2 and request.method == "POST":
                return self._criar_assinatura(corpo)
            return self._assinatura(request, partes, corpo)
        if partes[:2] == ["v3", "checkouts"]:
            if len(partes) == 2 and request.method == "POST":
                return self._criar_checkout(corpo)
            if len(partes) == 4 and partes[3] == "cancel":
                checkout = self.checkouts.get(partes[2])
                if checkout is None:
                    return _erro(404, "invalid_action", "Checkout nao encontrado.")
                checkout["status"] = "CANCELED"
                return httpx.Response(200, json=checkout)
        if partes[:3] == ["v3", "sandbox", "payment"] and len(partes) == 5:
            if partes[3] not in self.cobrancas:
                return _erro(404, "invalid_action", "Cobranca nao encontrada.")
            if partes[4] == "confirm":
                self.confirmar(partes[3])
            elif partes[4] == "expire":
                self.vencer(partes[3])
            return httpx.Response(200, json=self.cobrancas[partes[3]])
        return _erro(404, "invalid_action", "Rota desconhecida.")
