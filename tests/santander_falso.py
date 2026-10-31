"""Servidor falso do Santander (Gestao de Boletos v2) sobre httpx.MockTransport.

Segue o OpenAPI, os guias e as telas oficiais (docs/santander-duvidas.md), inclusive
as regras de recusa; nao e captura do ambiente real. Tem estado: registro, instrucao,
consulta e download do PDF.
"""

import json
import re
from urllib.parse import parse_qs, unquote

import httpx

API = "/collection_bill_management/v2"
WORKSPACE = "ws-1"
LINK_PDF = "https://blob.exemplo.test/final/boleto?sig=assinatura-secreta"
COVENANT = "3567206"
LINHA = "03399021995170000062624418101010292330000000199"
BARRAS = "03392923300000001999021951700000622441810101"
QR = "00020101021226920014br.gov.bcb.pix2570pix.santander.com.br/qr/v2/cobv/x"


def erro_400(codigo: str, mensagem: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "_errorCode": 400,
            "_message": "Bad Request",
            "_details": mensagem,
            "_timestamp": "2026-11-01T10:00:00Z",
            "_traceId": "t-1",
            "_errors": [{"_code": codigo, "_field": "x", "_message": mensagem}],
        },
    )


def webhook(
    nosso_numero: str = "18",
    funcao: str = "PAGAMENTO",
    *,
    convenio: str = COVENANT,
) -> bytes:
    """Exemplo do guia oficial, com os numeros completados com zeros."""
    return json.dumps(
        {
            "message": "WBHKPAGEST",
            "function": funcao,
            "paymentType": "PIX",
            "issueDate": "2025-11-24",
            "paymentDate": "2025-11-24-10.46.52.015034",
            "bankCode": "0033",
            "paymentChannel": "INTERNET BANKING",
            "paymentKind": "DEBITO EM CONTA",
            "covenant": convenio.zfill(9),
            "typeOfPersonAgreement": "CNPJ",
            "agreementDocument": "00111222000105",
            "bankNumber": nosso_numero.zfill(13),
            "clientNumber": "EXEMPLO WEBHOOK",
            "participantCode": "",
            "txId": "YKP001234567000000000001814072023",
            "payerDocumentType": "CPF",
            "payerDocumentNumber": "00000015860",
            "payerName": "JOAO DA SILVA",
            "finalBeneficiaryrDocumentType": "",
            "finalBeneficiaryDocumentNumber": "",
            "finalBeneficiaryName": "",
            "dueDate": "2025-11-30",
            "nominalValue": 150.1,
            "payedValue": 150.1,
            "interestValue": 0.0,
            "fine": 0.0,
            "deductionValue": 0.0,
            "rebateValue": 0.0,
            "iofValue": 0.0,
            "creditDate": "2025-11-24",
        }
    ).encode()


class SantanderFalso:
    def __init__(
        self,
        *,
        devolve_linha: bool = True,
        duplicado_409: bool = False,
        raiz_original: str | None = None,
    ) -> None:
        self.raiz_original = raiz_original
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.urls: list[str] = []
        self.tokens_emitidos = 0
        self.pedido_token: dict[str, list[str]] = {}
        self.ultimo_cabecalho: dict[str, str] = {}
        self.cabecalho_download: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.devolve_linha = devolve_linha
        self.duplicado_409 = duplicado_409
        self.resposta_pdf = httpx.Response(200, content=b"%PDF-1.4 boleto")

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def pagar_hoje(self, id_banco: str) -> None:
        """Pagamento por linha digitavel no mesmo dia: Ativo + Baixa Operacional."""
        self.boletos[id_banco]["statusComplement"] = "Baixa Operacional"

    def liquidar(self, id_banco: str) -> None:
        """D+1 do pagamento por linha digitavel ou codigo de barras."""
        boleto = self.boletos[id_banco]
        boleto["status"] = "Liquidado"
        boleto["statusComplement"] = ""
        boleto["paidValue"] = boleto["nominalValue"]
        boleto["_settlement"] = {
            "settlementData": [
                {
                    "settlementDescription": "LIQUIDACAO INTERNET BANKING",
                    "settlementDate": "2026-11-10",
                    "settlementValue": 150.0,
                    "settlementCreditDate": "2026-11-11",
                    "settlementCreditedValue": 148.5,
                    "settlementDutyValue": 1.5,
                }
            ],
            "writeOffData": [
                {
                    "writeOffDescription": "",
                    "writeOffDate": None,
                    "writeOffValue": 0.0,
                    "writeOffDutyValue": 0.0,
                }
            ],
        }

    def liquidar_por_pix(self, id_banco: str) -> None:
        boleto = self.boletos[id_banco]
        boleto["status"] = "Baixado"
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
        if request.url.host == "blob.exemplo.test":
            self.cabecalho_download = dict(request.headers)
            return self.resposta_pdf
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
        if caminho.startswith(f"{slips}/") and request.method == "GET":
            return self._sonda(unquote(caminho.rsplit("/", 1)[-1]))
        if caminho == f"{API}/bills" and request.method == "GET":
            nosso = request.url.params.get("bankNumber", "")
            if nosso not in self.boletos:
                return self._nao_encontrado()
            return httpx.Response(
                200,
                json={
                    "_pageable": {"_moreElements": False},
                    "_content": [self._simples(self.boletos[nosso])],
                },
            )
        if caminho.endswith("/bank_slips") and request.method == "POST":
            return self._pdf(request)
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
    def _simples(boleto: dict) -> dict:
        """`bankNumber` e `beneficiaryCode` em numero, como nas telas do guia."""
        return {
            "returnCode": "000 - Consulta realizada com sucesso",
            "documentNumber": "87626170000164",
            "beneficiaryCode": int(boleto["covenantCode"]),
            "bankNumber": int(boleto["bankNumber"]),
            "clientNumber": boleto.get("clientNumber", ""),
            "dueDate": boleto["dueDate"],
            "nominalValue": boleto["nominalValue"],
            "issueDate": boleto["issueDate"],
            "participantCode": "",
            "status": boleto["status"],
            "statusComplement": boleto.get("statusComplement", ""),
            **({"paidValue": boleto["paidValue"]} if "paidValue" in boleto else {}),
        }

    def _validar_registro(self, corpo: dict) -> httpx.Response | None:
        ambiente = corpo["environment"]
        if ambiente not in ("PRODUCAO", "TESTE"):
            return erro_400("1080", "Ambiente invalido")
        nsu = str(corpo["nsuCode"])
        if ambiente == "TESTE" and not nsu.startswith("TST"):
            return erro_400("1081", "NSU de teste deve comecar com TST")
        if ambiente == "PRODUCAO" and not nsu.isdigit():
            return erro_400("1082", "NSU de producao deve ser numerico")
        if not re.fullmatch(r"[0-9]{1,13}", corpo["bankNumber"]):
            return erro_400("00431", "Nosso numero nao numerico")
        cliente = corpo.get("clientNumber", "")
        if not re.fullmatch(r"[0-9a-zA-Z ]{0,15}", cliente):
            return erro_400("0901", "clientNumber so aceita alfanumericos")
        final = corpo.get("beneficiary")
        if (
            final
            and self.raiz_original
            and final["documentNumber"].startswith(self.raiz_original)
        ):
            return erro_400("00491", "Raiz igual a do beneficiario original")
        if not re.fullmatch(r"[0-9]{1,13}\.[0-9]{2}", corpo["nominalValue"]):
            return erro_400("0903", "Valor com 2 decimais")
        return None

    def _registrar(self) -> httpx.Response:
        corpo = self.ultimo_corpo
        recusa = self._validar_registro(corpo)
        if recusa is not None:
            return recusa
        nosso = corpo["bankNumber"]
        if nosso in self.boletos:
            if self.duplicado_409:
                return httpx.Response(409, json={"_message": "Conflict"})
            return erro_400("0001", "Nosso Numero ja registrado")
        resposta = dict(corpo)
        resposta["entryDate"] = corpo["nsuDate"]
        if self.devolve_linha:
            resposta["barcode"] = BARRAS
            resposta["digitableLine"] = LINHA
        if "key" in corpo:
            resposta["qrCodePix"] = QR
            resposta["qrCodeUrl"] = "pix.santander.com.br/qr/v2/cobv/x"
            resposta["txId"] = "TX" + nosso.zfill(30)
        if corpo["environment"] == "TESTE":
            # O guia: valida e responde, mas nao registra nada.
            return httpx.Response(201, json=resposta)
        self.boletos[nosso] = {
            **corpo,
            "status": "Ativo",
            "_sonda": resposta,
        }
        return httpx.Response(201, json=resposta)

    def _sonda(self, chave: str) -> httpx.Response:
        partes = chave.split(".")
        if len(partes) != 5:
            return erro_400("2084", "bankslip_id invalido")
        nsu, data, ambiente, convenio, nosso = partes
        boleto = self.boletos.get(nosso)
        if (
            boleto is None
            or ambiente != "P"
            or nsu != boleto["nsuCode"]
            or data != boleto["nsuDate"]
            or convenio != boleto["covenantCode"]
        ):
            return self._nao_encontrado()
        return httpx.Response(200, json=boleto["_sonda"])

    def _instruir(self) -> httpx.Response:
        corpo = self.ultimo_corpo
        boleto = self.boletos.get(corpo.get("bankNumber", ""))
        if boleto is None:
            return self._nao_encontrado()
        operacao = corpo.get("operation")
        if operacao not in (None, "BAIXAR", "PROTESTAR", "CANCELAR_PROTESTO"):
            return erro_400("005", "Tipo de solicitacao invalido")
        if operacao and set(corpo) - {"covenantCode", "bankNumber", "operation"}:
            return erro_400("003", "Protesto e baixa vao separados")
        if operacao == "BAIXAR":
            boleto["status"] = "Baixado"
            boleto["_settlement"] = {
                "writeOffData": [
                    {
                        "writeOffDate": "2026-11-02",
                        "writeOffDescription": "BAIXA CONFORME INSTRUCAO",
                        "writeOffValue": 0.0,
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
                "message": "Alteracao realizada com sucesso",
            },
        )

    def _pdf(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("trust-sandbox"):
            return httpx.Response(404, json={"_message": "so em producao"})
        bill_id = unquote(request.url.path.rsplit("/", 2)[-2])
        nosso, _, convenio = bill_id.partition(".")
        boleto = self.boletos.get(nosso)
        if boleto is None or convenio != boleto["covenantCode"]:
            return self._nao_encontrado()
        if boleto["status"] != "Ativo":
            return erro_400("00176", "Boleto deve estar ativo")
        documento = self.ultimo_corpo.get("payerDocumentNumber")
        if not isinstance(documento, int) or (
            documento != int(boleto["payer"]["documentNumber"])
        ):
            return erro_400("1001", "Documento do pagador invalido")
        return httpx.Response(200, json={"link": LINK_PDF})

    def _por_tipo(self, boleto: dict, tipo: str | None) -> httpx.Response:
        base = self._simples(boleto)
        if tipo == "bankslip":
            return httpx.Response(
                200,
                json={
                    **base,
                    "bankslipData": {
                        "portfolio": 1,
                        "digitableLine": LINHA,
                        "barCode": BARRAS,
                    },
                    "payerData": {
                        "payerDocumentType": 1,
                        "payerDocumentNumber": boleto["payer"]["documentNumber"].zfill(
                            15
                        ),
                        "payerName": boleto["payer"]["name"],
                    },
                    "guarantorData": {"guarantorName": "LOJA AVALISTA"},
                },
            )
        if tipo == "settlement":
            return httpx.Response(
                200,
                json={**base, "settlementData": [], **boleto.get("_settlement", {})},
            )
        return httpx.Response(400, json={"_message": f"tipoConsulta {tipo!r}"})
