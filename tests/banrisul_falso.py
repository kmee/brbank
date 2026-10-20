"""Servidor falso da API Cobranca v1 do Banrisul sobre httpx.MockTransport.

Formato tirado da especificacao OpenAPI oficial (1.6.1) e dos exemplos de
resposta do proprio portal; nao e captura do sandbox real. Com estado: guarda
os titulos registrados e aplica baixa, alteracao e liquidacao.
"""

import base64
import json
from urllib.parse import parse_qs

import httpx

BASE = "/cobranca/v1/boletos"
BENEFICIARIO = "0010000001088"
PDF = b"%PDF-1.4 boleto falso"


class BanrisulFalso:
    def __init__(self, *, centralizado: bool = True) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.caminhos_brutos: list[bytes] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultima_query: dict[str, str] = {}
        self.retorno_registro = "02" if centralizado else "01"
        self.sequencia = 6445268
        self.qr_pendente = False

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["situacao_banrisul"] = "L"
        boleto.pop("situacao_pagamento", None)
        boleto["operacoes"] = {
            "data_pagamento": "2026-12-10",
            "data_credito": "2026-12-11",
            "valor_juros": "10.00",
            "valor_pagamento": boleto["valor_nominal"],
            "valor_cobrado": "160.00",
            "valor_creditado_debitado": "160.00",
        }

    @staticmethod
    def _erro(status: int, codigo: str, mensagem: str, complemento: str = ""):
        item = {"codigo": codigo, "mensagem": mensagem}
        if complemento:
            item["complemento"] = complemento
        return httpx.Response(status, json={"ocorrencias": [item]})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.ultimo_form = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": f"tok-{self.tokens_emitidos}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "boletos",
            },
        )

    def _registrar(self, corpo: dict) -> httpx.Response:
        titulo = corpo["titulo"]
        if corpo["ambiente"] == "T":
            resposta = {**titulo, "nosso_numero": "0000000000"}
            return httpx.Response(200, json={"retorno": "04", "titulo": resposta})
        nosso = titulo.get("nosso_numero")
        if nosso is None:
            self.sequencia += 1
            nosso = f"{self.sequencia:010d}"
        if nosso in self.boletos:
            return self._erro(400, "09", "Nosso numero duplicado.")
        boleto = {
            **titulo,
            "nosso_numero": nosso,
            "codigo_barras": "04191861100000150502100100000010000644524040",
            "linha_digitavel": "04192100180000001000906445240408886110000015050",
            "situacao_banrisul": "A",
            "situacao_cip": "A",
            "situacao_pagamento": 1,
            "carteira": 1,
            "beneficiario": {"codigo": BENEFICIARIO, "tipo_pessoa": "J"},
        }
        if titulo.get("hibrido", {}).get("autoriza") == "S":
            if self.qr_pendente:
                boleto["hibrido"] = {"situacao": "P"}
            else:
                boleto["hibrido"] = {
                    "situacao": "A",
                    "txid": "001000000108859458645500095851146",
                    "location": "api-h.banrisul.com.br/pix/qrcode/v2/x",
                    "copia_cola": "00020101021226850014br.gov.bcb.pix",
                }
        self.boletos[nosso] = boleto
        return httpx.Response(
            201, json={"retorno": self.retorno_registro, "titulo": boleto}
        )

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.caminhos_brutos.append(request.url.raw_path)
        if caminho == "/auth/oauth/v2/token":
            return self._token(request)
        self.ultimo_cabecalho = dict(request.headers)
        self.ultima_query = dict(request.url.params)
        self.ultimo_corpo = json.loads(request.content) if request.content else {}
        if request.headers.get("bergs-beneficiario") != BENEFICIARIO:
            return self._erro(
                404, "A6", "Codigo do beneficiario invalido ou encerrado."
            )
        if request.method == "POST" and caminho == BASE:
            return self._registrar(self.ultimo_corpo)
        partes = caminho[len(BASE) + 1 :].split("/")
        nosso = partes[0].zfill(10) if partes[0].isdigit() else partes[0]
        if nosso not in self.boletos:
            return self._erro(404, "00", "Titulo inexistente.", "OC4B0004")
        boleto = self.boletos[nosso]
        homologacao = request.headers.get("bergs-ambiente") == "T"
        retorno = "04" if homologacao else "02"
        resumo = {
            "nosso_numero": nosso,
            "codigo_barras": boleto["codigo_barras"],
            "linha_digitavel": boleto["linha_digitavel"],
            "beneficiario": {"codigo": BENEFICIARIO},
        }
        if request.method == "POST" and partes[1:] == ["baixar"]:
            if not homologacao:
                boleto["situacao_banrisul"] = "A"
                boleto["situacao_pagamento"] = 4
            return httpx.Response(200, json={"retorno": retorno, "titulo": resumo})
        if request.method == "GET" and partes[1:] == ["emitir"]:
            pdf = base64.b64encode(PDF).decode()
            return httpx.Response(
                200, json={"retorno": retorno, "titulo": {"boleto": pdf}}
            )
        if request.method == "PATCH":
            tipo = request.url.params.get("tipo_alteracao")
            alteracao = self.ultimo_corpo.get("titulo", {})
            if not homologacao and tipo == "06":
                boleto["data_vencimento"] = alteracao["data_vencimento"]
            elif not homologacao and tipo == "04":
                novo = alteracao["instrucoes"]["abatimento"]["valor"]
                boleto.setdefault("instrucoes", {})["abatimento"] = {"valor": novo}
            elif tipo not in ("04", "06"):
                return self._erro(400, "00", "Tipo_alteracao invalido.", "OCPI0014")
            return httpx.Response(
                200, json={"retorno": retorno, "titulo": {**resumo, **alteracao}}
            )
        if request.method == "GET" and len(partes) == 1:
            return httpx.Response(200, json={"retorno": "02", "titulo": boleto})
        return self._erro(404, "00", "Rota inexistente no servidor falso.")
