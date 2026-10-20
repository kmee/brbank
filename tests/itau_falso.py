"""Servidor falso do Itau (cobranca, consulta e Bolecode) sobre httpx.MockTransport.

Formato inferido do ACBr e de relatos do forum (docs/itau-duvidas.md); nao e
captura do ambiente real.
"""

import json
from urllib.parse import parse_qs

import httpx

PREFIXO_COBRANCA = "/cash_management/v2/boletos"
PREFIXO_CONSULTA = "/boletoscash/v2/boletos"


class ItauFalso:
    def __init__(self) -> None:
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.hosts: list[str] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultima_query: dict[str, str] = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def _individual(self, nosso: str) -> dict:
        return self.boletos[nosso]["dado_boleto"]["dados_individuais_boleto"][0]

    def liquidar(self, nosso: str) -> None:
        self._individual(nosso)["situacao_geral_boleto"] = "Paga"
        self.boletos[nosso]["dado_boleto"]["pagamentos_cobranca"] = [
            {
                "data_inclusao_pagamento": "2026-11-10T10:00:00",
                "valor_pago_total_cobranca": "150.50",
            }
        ]

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        self.hosts.append(request.url.host)
        self.ultimo_cabecalho = dict(request.headers)
        if caminho.endswith(("/oauth/token", "/api/jwt")):
            self.tokens_emitidos += 1
            self.ultimo_form = parse_qs(request.content.decode())
            return httpx.Response(
                200,
                json={"access_token": f"tok-{self.tokens_emitidos}", "expires_in": 300},
            )
        self.ultima_query = dict(request.url.params)
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho.endswith("/boletos-pix"):
            return self._registrar(self.ultimo_corpo, bolecode=True)
        if request.method == "POST" and caminho == PREFIXO_COBRANCA:
            return self._registrar(self.ultimo_corpo["data"], bolecode=False)
        if request.method == "GET" and caminho == PREFIXO_CONSULTA:
            nosso = request.url.params.get("nosso_numero", "")
            if nosso not in self.boletos:
                return httpx.Response(
                    404, json={"codigo": "404", "mensagem": "nao encontrado"}
                )
            return httpx.Response(200, json={"data": [self.boletos[nosso]]})
        if request.method == "PATCH" and caminho.startswith(PREFIXO_COBRANCA + "/"):
            return self._alterar(caminho[len(PREFIXO_COBRANCA) + 1 :])
        return httpx.Response(404, json={"codigo": "404", "mensagem": "rota"})

    def _registrar(self, corpo: dict, *, bolecode: bool) -> httpx.Response:
        dado = corpo["dado_boleto"]
        individual = dict(dado["dados_individuais_boleto"][0])
        nosso = individual["numero_nosso_numero"]
        if nosso in self.boletos:
            return httpx.Response(
                422, json={"codigo": "422", "mensagem": "nosso numero em uso"}
            )
        individual.update(
            {
                "id_boleto_individual": f"id-{nosso}",
                "situacao_geral_boleto": "Em Aberto",
                "codigo_barras": "34191000000000",
                "numero_linha_digitavel": "34191.09008 00000",
            }
        )
        registro = {
            "id_boleto": f"b-{nosso}",
            "dado_boleto": {
                "codigo_carteira": dado["codigo_carteira"],
                "descricao_instrumento_cobranca": dado[
                    "descricao_instrumento_cobranca"
                ],
                "dados_individuais_boleto": [individual],
            },
        }
        resposta: dict = {
            "etapa_processo_boleto": corpo["etapa_processo_boleto"],
            "dado_boleto": {"dados_individuais_boleto": [individual]},
        }
        if bolecode:
            qrcode = {"emv": "00020101021226...", "txid": f"tx{nosso}"}
            registro["dado_boleto"]["qrcode_pix"] = qrcode
            resposta["dados_qrcode"] = {**qrcode, "location": "pix.itau/qr"}
        self.boletos[nosso] = registro
        return httpx.Response(200, json={"data": resposta})

    def _alterar(self, resto: str) -> httpx.Response:
        id_titulo, _, campo = resto.partition("/")
        nosso = id_titulo[-8:]
        if nosso not in self.boletos:
            return httpx.Response(404, json={"codigo": "404", "mensagem": "nada"})
        individual = self._individual(nosso)
        if campo == "baixa":
            individual["situacao_geral_boleto"] = "Baixada"
        elif campo == "data_vencimento":
            individual["data_vencimento"] = self.ultimo_corpo["data_vencimento"]
        return httpx.Response(204)
