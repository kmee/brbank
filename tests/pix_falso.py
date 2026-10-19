"""Servidor falso da API Pix BACEN (/cob, /cobv, /webhook) com estado.

Imita o PSP do perfil: caminho do token, estilo de autenticacao e alias de
`brcode` (Sicoob). Registra as requisicoes para os testes conferirem headers.
"""

import base64
import json
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from brbank.adapters.pix_bacen import PerfilPsp


class PixFalso:
    def __init__(self, perfil: PerfilPsp, ambiente: str = "producao") -> None:
        self.perfil = perfil
        self.base = urlparse(perfil.url_base(ambiente)).path.rstrip("/")
        self.caminho_token = urlparse(perfil.url_token(ambiente)).path
        self.cobrancas: dict[tuple[str, str], dict] = {}
        self.webhooks: dict[str, str] = {}
        self.devolucoes: dict[str, dict] = {}
        self.requisicoes: list[httpx.Request] = []
        self.pedidos_token: list[httpx.Request] = []
        self.ultimo_corpo: dict = {}
        self.tokens_emitidos = 0

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, txid: str, valor: str | None = None) -> None:
        for (_, id_), cob in self.cobrancas.items():
            if id_ == txid:
                cob["status"] = "CONCLUIDA"
                cob["pix"] = [
                    {
                        "endToEndId": "E123",
                        "txid": txid,
                        "valor": valor or cob["valor"]["original"],
                        "horario": "2026-11-10T12:30:00.000Z",
                    }
                ]
                return
        raise KeyError(txid)

    def _resposta(self, cob: dict, status: int = 200) -> httpx.Response:
        corpo = dict(cob)
        if "brcode" in self.perfil.alias_resposta:
            corpo["brcode"] = corpo.pop("pixCopiaECola")
        return httpx.Response(status, json=corpo)

    def tratar(self, request: httpx.Request) -> httpx.Response:
        self.requisicoes.append(request)
        caminho = request.url.path
        if caminho == self.caminho_token:
            self.pedidos_token.append(request)
            self.tokens_emitidos += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.tokens_emitidos}",
                    "expires_in": 3600,
                },
            )
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"title": "sem token"})
        if not caminho.startswith(self.base + "/"):
            return httpx.Response(404, json={"title": "rota"})
        partes = [unquote(p) for p in caminho[len(self.base) + 1 :].split("/")]
        corpo = json.loads(request.content) if request.content else {}
        recurso = partes[0]
        if recurso in ("cob", "cobv"):
            return self._cobranca(request.method, recurso, partes[1], corpo)
        if recurso == "webhook":
            return self._webhook(request.method, partes[1], corpo)
        if recurso == "pix" and partes[2:3] == ["devolucao"]:
            return self._devolucao(request.method, partes[3], corpo)
        return httpx.Response(404, json={"title": "rota"})

    def _cobranca(self, metodo: str, tipo: str, txid: str, corpo: dict):
        chave = (tipo, txid)
        if metodo in ("PUT", "POST"):
            if any(id_ == txid for _, id_ in self.cobrancas):
                return httpx.Response(409, json={"title": "txid existente"})
            self.ultimo_corpo = corpo
            self.cobrancas[chave] = {
                "txid": txid,
                "status": "ATIVA",
                "valor": corpo["valor"],
                "chave": corpo["chave"],
                "calendario": corpo["calendario"],
                "location": f"pix.example/qr/{txid}",
                "pixCopiaECola": f"000201{txid}",
            }
            return self._resposta(self.cobrancas[chave], 201)
        if chave not in self.cobrancas:
            return httpx.Response(404, json={"title": "nao encontrada"})
        cob = self.cobrancas[chave]
        if metodo == "PATCH":
            cob["status"] = corpo["status"]
        return self._resposta(cob)

    def _webhook(self, metodo: str, chave: str, corpo: dict) -> httpx.Response:
        if metodo == "PUT":
            self.webhooks[chave] = corpo["webhookUrl"]
            return httpx.Response(200, json={"webhookUrl": corpo["webhookUrl"]})
        if chave not in self.webhooks:
            return httpx.Response(404, json={"title": "sem webhook"})
        if metodo == "DELETE":
            del self.webhooks[chave]
            return httpx.Response(204)
        return httpx.Response(200, json={"webhookUrl": self.webhooks[chave]})

    def _devolucao(self, metodo: str, id_: str, corpo: dict) -> httpx.Response:
        if metodo == "PUT":
            self.devolucoes[id_] = {
                "id": id_,
                "rtrId": "D1",
                "valor": corpo["valor"],
                "status": "EM_PROCESSAMENTO",
            }
        if id_ not in self.devolucoes:
            return httpx.Response(404, json={"title": "sem devolucao"})
        return httpx.Response(200, json=self.devolucoes[id_])

    def corpo_token(self, indice: int = 0) -> dict:
        req = self.pedidos_token[indice]
        if "json" in req.headers.get("content-type", ""):
            return json.loads(req.content)
        return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}

    def basic(self, indice: int = 0) -> str | None:
        valor = self.pedidos_token[indice].headers.get("authorization", "")
        if not valor.startswith("Basic "):
            return None
        return base64.b64decode(valor[6:]).decode()
