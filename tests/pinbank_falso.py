"""Servidor falso do PinBank sobre httpx.MockTransport, com payload cifrado."""

import json

import httpx

from brbank.envelope import EnvelopeAesCbc

SENHA = "senha-de-16bytes"
BASE = "/services/api"


class PinbankFalso:
    def __init__(self, senha: str = SENHA) -> None:
        self.envelope = EnvelopeAesCbc(senha.encode())
        self.boletos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.ultimo_corpo_bruto: bytes = b""
        self.baixa_pendente = False

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        boleto = self.boletos[nosso_numero]
        boleto["Status"] = "LIQUIDADO"
        boleto["ValorPago"] = boleto["Valor"]
        boleto["DataPagamento"] = "20261110"

    def _resposta(self, dados: dict | None, status: int = 200) -> httpx.Response:
        if dados is None:
            return httpx.Response(status, json={"Message": "sem dados"})
        cifrado = self.envelope.cifrar(json.dumps({"Data": dados}).encode())
        return httpx.Response(status, content=cifrado)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.removeprefix(BASE)
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho == "/token":
            self.tokens_emitidos += 1
            form = dict(p.split("=") for p in request.content.decode().split("&"))
            if form.get("grant_type") != "password":
                return httpx.Response(400, json={"Message": "grant invalido"})
            return httpx.Response(
                200,
                json={"access_token": f"tok-{self.tokens_emitidos}", "expire_in": 3600},
            )
        self.ultimo_corpo_bruto = request.content
        envelope = json.loads(request.content)
        corpo = json.loads(self.envelope.decifrar(json.dumps(envelope).encode()))
        self.ultimo_corpo = corpo
        dados = corpo["Data"]
        if caminho == "/CashIn/GerarBoletoEncrypted":
            numero = f"{1000 + len(self.boletos)}"
            self.boletos[numero] = {
                "Status": "PENDENTE",
                "Valor": dados["Valor"],
                "ValorPago": 0,
                "DataPagamento": "",
                "NossoNumero": numero,
                "LinhaDigitavel": "12345.67890 12345.678901",
                "CodigoBarras": "12345678901234567890",
                "DadosPagador": {"Nome": "Maria da Silva", "CpfCnpj": 52998224725},
            }
            return self._resposta(
                {
                    "NossoNumero": numero,
                    "LinhaDigitavel": "12345.67890 12345.678901",
                    "CodigoBarras": "12345678901234567890",
                    "Base64": "JVBERi0=" * 100,
                }
            )
        boleto = self.boletos.get(str(dados.get("NossoNumero")))
        if boleto is None:
            return httpx.Response(404, json={"Message": "boleto nao encontrado"})
        if caminho == "/CashIn/SolicitarBaixaBoletoEncrypted":
            if not self.baixa_pendente:
                boleto["Status"] = "BAIXA"
            return self._resposta({"Mensagem": "solicitado"})
        if boleto["Status"] == "PENDENTE" and caminho.endswith("Consultar"):
            pass
        return self._resposta(boleto)
