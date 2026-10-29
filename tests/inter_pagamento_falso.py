"""Servidor falso do Inter (Banking v2, pagamentos) sobre httpx.MockTransport.

Segue o OpenAPI oficial (swagger-banking-yaml, lido em 29/10/2026): `POST /pix`
com destinatario discriminado por `tipo`, resposta com `tipoRetorno` e
`codigoSolicitacao`, consulta com `transacaoPix`, boleto em `POST /pagamento`
com `valorPagar` em texto e consulta em lista, erro no formato Problema.
"""

import json
import re
import uuid

import httpx

PREFIXO = "/banking/v2/"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _problema(status: int, titulo: str, detalhe: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "title": titulo,
            "status": status,
            "detail": detalhe,
            "timestamp": "2026-10-29T10:00:00Z",
        },
    )


class InterPagamentoFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, str]] = []
        self.corpos: list[tuple[str, dict]] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.escopos_pedidos: list[str] = []
        self.pix: dict[str, dict] = {}
        self.boletos: dict[str, dict] = {}
        self.idempotencia: dict[str, str] = {}
        # Resposta da inclusao do Pix e status das consultas.
        self.tipo_retorno = "PROCESSADO"
        self.status_pix = "PAGO"
        self.status_boleto = "REALIZADO"
        self.falhas: dict[str, list] = {}
        self.webhooks: dict[str, str] = {}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def de_rota(self, rota: str) -> list[dict]:
        return [c for r, c in self.corpos if r == rota]

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == "/oauth/v2/token":
            pedido = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            self.escopos_pedidos.append(pedido.get("scope", ""))
            if not pedido.get("client_secret"):
                return httpx.Response(401, json={"title": "Nao autorizado"})
            return httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        self.cabecalhos.append(dict(request.headers))
        if request.headers.get("authorization") != "Bearer tok":
            return _problema(401, "Nao autorizado", "token")
        rota = caminho.removeprefix(PREFIXO)
        corpo = json.loads(request.content) if request.content else {}
        if corpo:
            self.corpos.append((rota, corpo))
        forcadas = self.falhas.get(rota)
        if forcadas:
            falha = forcadas.pop(0)
            if falha == "timeout":
                raise httpx.ReadTimeout("sem resposta", request=request)
            if falha == 422:
                return _problema(
                    422,
                    "Nao foi possivel processar o QR Code",
                    "QR Code expirado.",
                )
            return _problema(falha, "Erro", f"HTTP {falha}")
        if request.method == "POST" and rota == "pix":
            return self._incluir_pix(corpo, request.headers.get("x-id-idempotente"))
        if request.method == "GET" and rota.startswith("pix/"):
            return self._consultar_pix(rota.split("/")[-1])
        if request.method == "POST" and rota == "pagamento":
            return self._pagar_boleto(corpo)
        if request.method == "GET" and rota == "pagamento":
            codigo = request.url.params.get("codigoTransacao")
            item = self.boletos.get(codigo)
            itens = [{**item, "statusPagamento": self.status_boleto}] if item else []
            return httpx.Response(200, json=itens)
        if request.method == "DELETE" and rota.startswith("pagamento/"):
            if rota.split("/")[-1] not in self.boletos:
                return _problema(404, "Nao encontrado", "pagamento")
            return httpx.Response(204)
        if rota.startswith("webhooks/"):
            return self._webhook(request.method, rota.split("/")[-1], corpo)
        return _problema(404, "Nao encontrado", rota)

    def _incluir_pix(self, corpo: dict, chave: str | None) -> httpx.Response:
        if chave is not None and not _UUID.match(chave):
            return _problema(400, "Requisicao invalida", "x-id-idempotente")
        if not isinstance(corpo.get("valor"), int | float):
            return _problema(400, "Requisicao invalida", "valor")
        destinatario = corpo.get("destinatario") or {}
        obrigatorios = {
            "CHAVE": ("chave",),
            "PIX_COPIA_E_COLA": ("pixCopiaECola",),
            "DADOS_BANCARIOS": (
                "contaCorrente",
                "tipoConta",
                "cpfCnpj",
                "agencia",
                "nome",
                "instituicaoFinanceira",
            ),
        }.get(destinatario.get("tipo"))
        if obrigatorios is None or any(not destinatario.get(c) for c in obrigatorios):
            return _problema(400, "Requisicao invalida", "destinatario")
        if chave in self.idempotencia:
            codigo = self.idempotencia[chave]
        else:
            codigo = str(uuid.uuid4())
            if chave:
                self.idempotencia[chave] = codigo
        self.pix[codigo] = corpo
        return httpx.Response(
            200,
            json={
                "tipoRetorno": self.tipo_retorno,
                "codigoSolicitacao": codigo,
                "dataPagamento": corpo.get("dataPagamento"),
                "dataOperacao": corpo.get("dataPagamento"),
            },
        )

    def _consultar_pix(self, codigo: str) -> httpx.Response:
        corpo = self.pix.get(codigo)
        if corpo is None:
            return _problema(404, "Nao encontrado", "pix")
        transacao = {
            "contaCorrente": "123456",
            "endToEnd": "E00416968202610291000abcdefghijk",
            "valor": corpo["valor"],
            "status": self.status_pix,
            "dataHoraMovimento": "2026-10-29T10:00:01",
            "dataHoraSolicitacao": "2026-10-29T10:00:00",
            "codigoSolicitacao": codigo,
            "recebedor": {"cpfCnpj": "***.338.470-**", "nome": "FULANO"},
            "erros": [],
        }
        if self.status_pix == "FALHA":
            transacao["erros"] = [
                {"codigoErro": "AM04", "descricaoErro": "Saldo insuficiente"}
            ]
        return httpx.Response(
            200,
            json={
                "transacaoPix": transacao,
                "historico": [{"status": self.status_pix}],
            },
        )

    def _pagar_boleto(self, corpo: dict) -> httpx.Response:
        for campo in ("codBarraLinhaDigitavel", "valorPagar", "dataVencimento"):
            if not corpo.get(campo):
                return _problema(400, "Requisicao invalida", campo)
        if not isinstance(corpo["valorPagar"], str):
            return _problema(400, "Requisicao invalida", "valorPagar")
        codigo = str(uuid.uuid4())
        self.boletos[codigo] = {
            "codigoTransacao": codigo,
            "codigoBarra": corpo["codBarraLinhaDigitavel"],
            "dataPagamento": corpo.get("dataPagamento"),
            "valorPago": float(corpo["valorPagar"]),
            "nomeBeneficiario": "BENEFICIARIO",
            "cpfCnpjBeneficiario": corpo.get("cpfCnpjBeneficiario"),
            "autenticacao": "AUT123",
        }
        return httpx.Response(
            200,
            json={
                "quantidadeAprovadores": 0,
                "dataAgendamento": corpo.get("dataPagamento"),
                "statusPagamento": "EMPROCESSAMENTO",
                "codigoTransacao": codigo,
            },
        )

    def _webhook(self, metodo: str, tipo: str, corpo: dict) -> httpx.Response:
        if metodo == "PUT":
            self.webhooks[tipo] = corpo["webhookUrl"]
            return httpx.Response(204)
        if metodo == "DELETE":
            self.webhooks.pop(tipo, None)
            return httpx.Response(204)
        if tipo not in self.webhooks:
            return _problema(404, "Nao encontrado", "webhook")
        return httpx.Response(
            200,
            json={"webhookUrl": self.webhooks[tipo], "criacao": "2026-10-29T10:00:00Z"},
        )
