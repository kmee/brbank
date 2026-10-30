"""Servidor falso do Santander (Hub de Pagamentos 1.1.7) sobre httpx.MockTransport.

Segue o OpenAPI oficial e os guias: token por formulario, `X-Application-Key` em
toda chamada, POST valida e deixa READY_TO_PAY, PATCH com AUTHORIZED efetiva, id
repetido e recusado com o codigo do produto, corpo de erro `_errors`.
"""

import json
import re
from decimal import Decimal

import httpx

PREFIXO = "/management_payments_partners/v1/workspaces/"
CAMINHO_TOKEN = "/auth/oauth/v2/token"
_VALOR = re.compile(r"^\d{1,14}\.\d{2}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CODIGO_ID_REPETIDO = {
    "bank_slip_payments": "30082",
    "barcode_payments": "30082",
    "pix_payments": "40053",
    "transfer": "20002",
}
_CODIGO_ERRO = {
    "bank_slip_payments": "30000",
    "barcode_payments": "30000",
    "pix_payments": "40000",
    "transfer": "20000",
}
BENEFICIARIO = {
    "name": "CEDENTE TESTE",
    "documentType": "CNPJ",
    "documentNumber": "11222333000181",
}


def erro(status: int, codigo_grupo: str, codigo: str, mensagem: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "_errorCode": codigo_grupo,
            "_message": "Bad Request",
            "_details": mensagem[:100],
            "_timestamp": "2026-10-30T10:00:00-03:00",
            "_traceId": "trace-1",
            "_errors": [{"_code": codigo, "_field": "", "_message": mensagem}],
        },
    )


class SantanderPagamentoFalso:
    def __init__(self, workspace: str = "ws-1") -> None:
        self.workspace = workspace
        self.chamadas: list[tuple[str, str]] = []
        self.corpos: list[tuple[str, str, dict]] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.pedido_token: dict[str, str] = {}
        self.pagamentos: dict[str, dict] = {}
        # Estado devolvido pelo POST e pelo PATCH.
        self.estado_post = "READY_TO_PAY"
        self.estado_patch = "PAYED"
        # Total calculado pelo banco (QR com encargos); None = o valor enviado.
        self.total: str | None = None
        self.beneficiario = dict(BENEFICIARIO)
        # "METODO rota" -> lista de falhas ("timeout", "html", "vazio", 503, 429,
        # ou dict com status, grupo, codigo e mensagem).
        self.falhas: dict[str, list] = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def de(self, metodo: str) -> list[dict]:
        return [c for m, _, c in self.corpos if m == metodo]

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == CAMINHO_TOKEN:
            self.pedido_token = dict(
                p.split("=", 1) for p in request.content.decode().split("&")
            )
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        self.cabecalhos.append(dict(request.headers))
        if not request.headers.get("x-application-key") or not request.headers.get(
            "authorization"
        ):
            return httpx.Response(401, json={"_errorCode": "401", "_message": "x"})
        if not caminho.startswith(PREFIXO + self.workspace + "/"):
            return httpx.Response(404, json={"_errorCode": "404", "_message": "x"})
        partes = caminho.removeprefix(PREFIXO + self.workspace + "/").split("/")
        rota, id_ = partes[0], (partes[1] if len(partes) > 1 else None)
        corpo = json.loads(request.content) if request.content else {}
        if corpo:
            self.corpos.append((request.method, rota, corpo))
        forcadas = self.falhas.get(f"{request.method} {rota}")
        if forcadas:
            falha = forcadas.pop(0)
            if falha == "timeout":
                raise httpx.ReadTimeout("sem resposta", request=request)
            if falha == "html":
                return httpx.Response(200, text="<html>ok</html>")
            if falha == "vazio":
                return httpx.Response(200)
            if isinstance(falha, dict):
                return erro(
                    falha.get("status", 400),
                    _CODIGO_ERRO[rota],
                    falha["codigo"],
                    falha["mensagem"],
                )
            return httpx.Response(falha, json={"_errorCode": str(falha)})
        if request.method == "POST" and id_ is None:
            return self._iniciar(rota, corpo)
        if request.method == "PATCH" and id_:
            return self._efetivar(rota, id_, corpo)
        if request.method == "GET" and id_:
            if id_ not in self.pagamentos:
                return erro(404, _CODIGO_ERRO[rota], "40055", "Record does not exist")
            return httpx.Response(200, json=self.pagamentos[id_])
        return httpx.Response(405, json={"_errorCode": "405"})

    def _invalido(self, rota: str, mensagem: str) -> httpx.Response:
        return erro(400, _CODIGO_ERRO[rota], "99999", mensagem)

    def _iniciar(self, rota: str, corpo: dict) -> httpx.Response:
        id_ = corpo.get("id")
        if not id_ or not _UUID4.match(id_):
            return self._invalido(rota, "Payment identifier with invalid content")
        if id_ in self.pagamentos:
            return erro(
                400,
                _CODIGO_ERRO[rota],
                _CODIGO_ID_REPETIDO[rota],
                "The ID provided already exists in the database",
            )
        problema = {
            "bank_slip_payments": self._checar_codigo,
            "barcode_payments": self._checar_codigo,
            "pix_payments": self._checar_pix,
            "transfer": self._checar_ted,
        }[rota](corpo)
        if problema:
            return self._invalido(rota, problema)
        registro = {
            "id": id_,
            "workspaceId": self.workspace,
            "status": self.estado_post,
            "rejectCode": "30105" if self.estado_post == "REJECTED" else None,
            "rejectReason": "Saldo" if self.estado_post == "REJECTED" else None,
            "tags": corpo.get("tags", []),
            "payer": {
                "name": "PAGADOR",
                "documentType": "CNPJ",
                "documentNumber": "91300766000147",
            },
            "transaction": {"value": None, "code": None, "date": None},
        }
        if rota == "transfer":
            registro["transferValue"] = corpo["transferValue"]
            registro["destinationAccount"] = corpo["destinationAccount"]
        else:
            registro.update(
                beneficiary=self.beneficiario,
                paymentValue=None,
                debitAccount={"branch": "0001", "number": "130375431"},
            )
        if rota in ("bank_slip_payments", "barcode_payments"):
            registro.update(code=corpo["code"], totalValue=35.45)
        if rota == "pix_payments":
            valor = corpo["paymentValue"]
            registro.update(
                paymentValue=float(valor),
                totalValue=self.total or float(valor),
                remittanceInformation=corpo.get("remittanceInformation"),
                dictCode=corpo.get("dictCode"),
                qrCode=corpo.get("qrCode"),
            )
        self.pagamentos[id_] = registro
        return httpx.Response(201, json=registro)

    @staticmethod
    def _checar_codigo(corpo: dict) -> str | None:
        if not re.fullmatch(r"\d{44}", corpo.get("code", "")):
            return "Barcode with invalid content"
        return None

    @staticmethod
    def _checar_pix(corpo: dict) -> str | None:
        if not _VALOR.match(str(corpo.get("paymentValue", ""))):
            return "Payment amount with invalid content"
        meios = [c for c in ("qrCode", "dictCode", "beneficiary") if c in corpo]
        if len(meios) != 1:
            return "Exatamente um meio: qrCode, dictCode ou beneficiary"
        if "dictCode" in corpo and corpo.get("dictCodeType") not in (
            "CPF",
            "CNPJ",
            "CELULAR",
            "EMAIL",
            "EVP",
        ):
            return "Pix key type with invalid content"
        beneficiario = corpo.get("beneficiary")
        if beneficiario:
            for campo in ("branch", "number", "type", "documentType", "name"):
                if not beneficiario.get(campo):
                    return f"beneficiary.{campo} is required"
            if ("bankCode" in beneficiario) == ("ispb" in beneficiario):
                return "bankCode ou ispb, nunca os dois"
            if beneficiario["type"] not in (
                "CONTA_CORRENTE",
                "CONTA_POUPANCA",
                "CONTA_PAGAMENTO",
            ):
                return "Beneficiary account type with invalid or incorrect content"
        return None

    @staticmethod
    def _checar_ted(corpo: dict) -> str | None:
        if not corpo.get("sourceAccount", {}).get("accountNumber"):
            return "sourceAccount is required"
        if corpo.get("destinationType") not in ("STR0006", "STR0008"):
            return "DestinationType with invalid content"
        destino = corpo.get("destinationAccount") or {}
        for campo in (
            "bankCode",
            "branchCode",
            "accountNumber",
            "typeAccount",
            "legalEntityIdentifier",
            "documentIdentifierNumber",
            "name",
            "purpose",
        ):
            if not destino.get(campo):
                return f"destinationAccount.{campo} is required"
        tipo = destino["typeAccount"]
        if tipo in ("CC", "PP") and destino.get("ispbCode"):
            return "ISPB code is not allowed for CC and PP account types."
        if tipo == "PG" and not destino.get("ispbCode"):
            return "ISPB code is required for PG account type."
        return None

    def _efetivar(self, rota: str, id_: str, corpo: dict) -> httpx.Response:
        registro = self.pagamentos.get(id_)
        if registro is None:
            return erro(404, _CODIGO_ERRO[rota], "40055", "Record does not exist")
        if registro["status"] != "READY_TO_PAY":
            return erro(400, _CODIGO_ERRO[rota], "40046", "Pix is not ready to pay.")
        if corpo.get("status") != "AUTHORIZED":
            return self._invalido(rota, "Status with invalid content")
        if rota == "transfer":
            if not corpo.get("sourceAccount"):
                return self._invalido(rota, "sourceAccount is required")
            valor = registro["transferValue"]
        else:
            if not _VALOR.match(str(corpo.get("paymentValue", ""))):
                return self._invalido(rota, "Payment amount with invalid content")
            valor = float(Decimal(corpo["paymentValue"]))
            if not corpo.get("debitAccount", {}).get("number"):
                return self._invalido(rota, "debitAccount e fixo neste falso")
        if rota in ("bank_slip_payments", "barcode_payments") and not corpo.get(
            "finalPayer", {}
        ).get("documentNumber"):
            return erro(400, "30000", "30072", "Final payer is mandatory")
        registro.update(status=self.estado_patch)
        registro["paymentValue"] = valor
        if self.estado_patch in ("PAYED", "PENDING_CONFIRMATION"):
            registro["transaction"] = {
                "value": valor,
                "code": "VX00012209131051260932",
                "date": "2026-10-30T10:46:41-03:00",
                "key": "NIO-1",
            }
        if self.estado_patch == "REJECTED":
            registro.update(rejectCode="30105", rejectReason="Saldo insuficiente")
        return httpx.Response(200, json=registro)
