"""Servidor falso da Unicred (cobranca v2) sobre httpx.MockTransport, com estado.

Formato das paginas do portal "Unicred Developers" (docs/unicred-duvidas.md):
emissao devolve o `id_titulo` em texto puro, consulta pode responder 202 sem
corpo enquanto processa, baixa devolve 201 com `codigoInstrucao`. Nao e captura
do ambiente real.
"""

import base64
import json

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

API_KEY = "chave-da-app"
BENEFICIARIO = "B7CEE6BDC9E04C49A7437EC3E750140A"
CHAVE_AUT = "11111111-2222-4333-8444-555555555555"
CHAVE_CRIPTO = "66666666-7777-4888-9999-aaaaaaaaaaaa"
BASE = f"/cobranca/v2/beneficiarios/{BENEFICIARIO}/titulos"


def cifrar_webhook(dados: dict, chave_aut: str, chave_cripto: str) -> bytes:
    """Cifra como a Unicred: AES-256-CBC, PKCS5, IV = 16 da chave de autenticacao."""
    cifra = Cipher(
        algorithms.AES(chave_cripto[:32].encode()), modes.CBC(chave_aut[:16].encode())
    ).encryptor()
    padder = padding.PKCS7(128).padder()
    bloco = padder.update(json.dumps(dados).encode()) + padder.finalize()
    return base64.b64encode(cifra.update(bloco) + cifra.finalize())


def webhook_liquidacao(uuid_titulo: str) -> bytes:
    dados = {
        "uuidInscricaoWebhook": "inscricao-1",
        "uuidRequisicaoWebhook": "requisicao-1",
        "codigoMovimento": "06",
        "codigoInstrucaoOrigem": "00",
        "titulo": {
            "uuidTitulo": uuid_titulo,
            "codigoSituacao": "03",
            "pagador": {"documento": "52998224725", "nomeRazaoSocial": "Maria"},
        },
        "pagamento": {"dataLiquidacao": "2026-12-10", "valorRecebido": 150.00},
    }
    return cifrar_webhook(dados, CHAVE_AUT, CHAVE_CRIPTO)


class UnicredFalso:
    def __init__(self, prefixo: str = "") -> None:
        self.prefixo = prefixo
        self.titulos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.pedido_token: dict = {}
        self.cabecalho_token: dict[str, str] = {}
        # Quantas consultas respondem 202 antes do titulo aparecer.
        self.processando = 0
        self.resposta_emissao = "texto"

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, id_titulo: str) -> None:
        titulo = self.titulos[id_titulo]
        titulo["status"] = "LIQUIDADO"
        titulo["pagamentos"] = [{"data": "2026-12-09", "valor": 150.00}]

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.tokens_emitidos += 1
        self.pedido_token = json.loads(request.content)
        self.cabecalho_token = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "accessToken": f"tok-{self.tokens_emitidos}",
                "tokenExpirationTime": 2700,
                "refreshToken": "r",
                "refreshTokenExpirationTime": 1800,
            },
        )

    def _emitir(self) -> httpx.Response:
        id_titulo = f"{len(self.titulos) + 1:032x}"
        corpo = self.ultimo_corpo
        self.titulos[id_titulo] = {
            "codBarras": "1" * 44,
            "linhaDigitavel": "2" * 46,
            "nossoNumero": f"{len(self.titulos) + 1:011d}",
            "dataDeVencimento": corpo["vencimento"],
            "valor": corpo["valor"],
            "status": "ABERTO",
            "motivo": None,
            "qrCodePix": None,
        }
        if self.resposta_emissao == "json":
            return httpx.Response(200, json={"message": "OK", "id_titulo": id_titulo})
        return httpx.Response(200, text=id_titulo)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.removeprefix(self.prefixo)
        self.chamadas.append((request.method, caminho))
        if caminho == "/oauth2/v2/grant-token":
            return self._token(request)
        self.ultimo_cabecalho = dict(request.headers)
        if request.content:
            self.ultimo_corpo = json.loads(request.content)
        if request.method == "POST" and caminho == BASE:
            return self._emitir()
        partes = caminho.removeprefix(BASE + "/").split("/")
        id_titulo = partes[0]
        if id_titulo not in self.titulos:
            return httpx.Response(
                404,
                json={
                    "httpStatus": 404,
                    "message": "Titulo nao encontrado",
                    "errorCode": "COB005BCORE",
                },
            )
        titulo = self.titulos[id_titulo]
        if caminho.endswith("/status"):
            if self.processando:
                self.processando -= 1
                return httpx.Response(202)
            return httpx.Response(200, json=titulo)
        if caminho.endswith("/baixa"):
            if titulo["status"] != "ABERTO":
                return httpx.Response(
                    422,
                    json={
                        "httpStatus": 422,
                        "message": "A situacao do titulo deve ser Aberto.",
                        "errorCode": "C1",
                    },
                )
            titulo["status"] = "BAIXADO"
            titulo["motivo"] = None
            return httpx.Response(201, json={"codigoInstrucao": "0" * 19 + "1"})
        if request.method == "GET" and len(partes) == 1:
            return httpx.Response(
                200,
                content=b"%PDF-1.4 falso",
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(405)
