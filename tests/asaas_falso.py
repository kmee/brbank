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
"""

import json

import httpx

CHAVE_API = "$aact_hmlg_chave-falsa-000"
HOST_PDF = "https://sandbox.asaas.com"
PDF = b"%PDF-1.4\n% boleto falso do asaas\n"
MEIOS = ("UNDEFINED", "BOLETO", "CREDIT_CARD", "PIX")
_SITUACOES = {
    "PAGO": ("PAYMENT_RECEIVED", "RECEIVED", False),
    "CONFIRMADO": ("PAYMENT_CONFIRMED", "CONFIRMED", False),
    "VENCIDO": ("PAYMENT_OVERDUE", "OVERDUE", False),
    "REMOVIDO": ("PAYMENT_DELETED", "PENDING", True),
    "ESTORNADO": ("PAYMENT_REFUNDED", "REFUNDED", False),
}


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


class AsaasFalso:
    def __init__(self, *, chave: str = CHAVE_API, com_chave_pix: bool = True) -> None:
        self.chave = chave
        self.com_chave_pix = com_chave_pix
        self.clientes: dict[str, dict] = {}
        self.cobrancas: dict[str, dict] = {}
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

    # --- rotas --------------------------------------------------------------

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
        if corpo.get("billingType") == "CREDIT_CARD":
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
            "subscription": None,
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
        if partes[:3] == ["v3", "sandbox", "payment"] and len(partes) == 5:
            if partes[3] not in self.cobrancas:
                return _erro(404, "invalid_action", "Cobranca nao encontrada.")
            if partes[4] == "confirm":
                self.confirmar(partes[3])
            elif partes[4] == "expire":
                self.vencer(partes[3])
            return httpx.Response(200, json=self.cobrancas[partes[3]])
        return _erro(404, "invalid_action", "Rota desconhecida.")
