"""Servidor falso do Sicoob (Pagamentos Pix v2) sobre httpx.MockTransport, com estado.

Segue o swagger 2.0.24.14 e o que o sandbox respondeu em 29/10/2026: corpo
validado (obrigatorios, valor com ponto no QR e com virgula na confirmacao),
erro no formato Problema (RFC 7807), `estado` com acento (NAO_REALIZADO vem como
"NÃO_REALIZADO"), 401 sem o header `client_id` e o firewall que troca a resposta
do PUT do webhook por HTML quando o pedido traz `Accept: application/json`.
"""

import json
import re
import uuid

import httpx

from brbank.instrumento import crc16_ccitt

PREFIXO = "/pix-pagamentos/v2/"
PREFIXO_SANDBOX = "/sicoob/sandbox/pix-pagamentos/v2/"
CAMINHO_TOKEN = "/auth/realms/cooperado/protocol/openid-connect/token"
_VALOR_PONTO = re.compile(r"^\d{1,16}\.\d{2}$")
_VALOR_VIRGULA = re.compile(r"^[0-9]{1,13}([,][0-9]{1,2})?$")
_ORIGEM_QR = ("ispb", "nome", "cpfCnpj", "conta", "agencia", "tipo")


def _tlv(ident: str, valor: str) -> str:
    return f"{ident}{len(valor):02d}{valor}"


def brcode(valor: str | None = None, url: str | None = None) -> str:
    """BR Code de teste: estatico (chave e-mail) ou dinamico (URL)."""
    conta = _tlv("00", "br.gov.bcb.pix") + (
        _tlv("25", url) if url else _tlv("01", "recebedor@example.com")
    )
    corpo = _tlv("00", "01") + (_tlv("01", "12") if url else "") + _tlv("26", conta)
    corpo += _tlv("52", "0000") + _tlv("53", "986")
    if valor:
        corpo += _tlv("54", valor)
    corpo += _tlv("58", "BR") + _tlv("59", "RECEBEDOR TESTE") + _tlv("60", "BRASILIA")
    corpo += _tlv("62", _tlv("05", "***")) + "6304"
    return corpo + crc16_ccitt(corpo)


def _problema(razao: str, propriedade: str) -> httpx.Response:
    return httpx.Response(
        400,
        headers={"content-type": "application/problem+json"},
        json={
            "type": "https://pix.bcb.gov.br/api/v2/error/RequisicaoInvalida",
            "title": "Requisição inválida.",
            "status": 400,
            "detail": "A requisição possui dados inválidos.",
            "violacoes": [{"razao": razao, "propriedade": propriedade}],
        },
    )


class SicoobPagamentoFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, str]] = []
        self.corpos: list[tuple[str, dict]] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.pedido_token: dict[str, str] = {}
        self.pagamentos: dict[str, dict] = {}
        self.iniciados: dict[str, str] = {}
        # Estado devolvido no pagamento e na consulta.
        self.estado = "EM_PROCESSAMENTO"
        # Documento do dono de toda chave consultada no DICT.
        self.dono_chave = "28133847044"
        self.chaves_inexistentes: set[str] = set()
        # Rota -> lista de respostas forcadas ("timeout", 503, 400...).
        self.falhas: dict[str, list] = {}
        self.webhook: str | None = None

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def de_rota(self, rota: str) -> list[dict]:
        return [c for r, c in self.corpos if r == rota]

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == CAMINHO_TOKEN:
            self.pedido_token = dict(
                p.split("=", 1) for p in request.content.decode().split("&")
            )
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        self.cabecalhos.append(dict(request.headers))
        if not request.headers.get("client_id") or not request.headers.get(
            "authorization"
        ):
            return httpx.Response(
                401,
                json={
                    "httpCode": "401",
                    "httpMessage": "Unauthorized",
                    "moreInformation": "Invalid client id or secret.",
                },
            )
        rota = caminho.removeprefix(PREFIXO_SANDBOX).removeprefix(PREFIXO)
        corpo = json.loads(request.content) if request.content else {}
        if corpo:
            self.corpos.append((rota, corpo))
        forcadas = self.falhas.get(rota)
        if forcadas:
            falha = forcadas.pop(0)
            if falha == "timeout":
                raise httpx.ReadTimeout("sem resposta", request=request)
            return httpx.Response(falha, json={"httpCode": str(falha)})
        if request.method == "POST" and rota == "pagamentos/qrcode":
            return self._qrcode(corpo)
        if request.method == "POST" and rota == "pagamentos":
            return self._iniciar(corpo)
        if request.method == "POST" and rota == "pagamentos/confirmacao":
            return self._confirmar(corpo)
        if rota == "pagamentos/webhook":
            return self._webhook(request.method, corpo, request.headers.get("accept"))
        if request.method == "GET" and rota.startswith("pagamentos/"):
            e2e = rota.split("/")[-1]
            if e2e not in self.pagamentos:
                return httpx.Response(
                    404,
                    json={"type": "NaoEncontrado", "title": "Não Encontrado"},
                )
            return httpx.Response(200, json={**self.pagamentos[e2e], **self._estado()})
        return httpx.Response(404, json={"httpCode": "404"})

    def _estado(self) -> dict:
        estado = "NÃO_REALIZADO" if self.estado == "NAO_REALIZADO" else self.estado
        extra: dict = {"estado": estado}
        if estado in ("REJEITADO", "NÃO_REALIZADO"):
            extra["detalheRejeicao"] = "Saldo insuficiente"
        return extra

    @staticmethod
    def _e2e() -> str:
        return "E02038232" + uuid.uuid4().hex[:23]

    def _retorno(self, corpo: dict, e2e: str, valor) -> dict:
        retorno = {
            "endToEndId": e2e,
            "valor": valor,
            "horario": "2026-10-29T10:00:00.000Z",
            "origem": corpo["origem"],
            "destino": {
                "ispb": "18236120",
                "cpfCnpj": "91300766000147",
                "nome": "RECEBEDOR TESTE",
                "conta": "1234567",
                "agencia": "1",
                "tipo": "CORRENTE",
            },
            **self._estado(),
        }
        if corpo.get("dataAgendamento"):
            retorno["dataAgendamento"] = corpo["dataAgendamento"]
        if corpo.get("descricao"):
            retorno["descricao"] = corpo["descricao"]
        self.pagamentos[e2e] = {
            k: v for k, v in retorno.items() if k not in ("estado", "detalheRejeicao")
        }
        return retorno

    def _qrcode(self, corpo: dict) -> httpx.Response:
        for campo in ("qrcode", "valor", "origem", "pagarComJurosMulta", "repeticao"):
            if campo not in corpo:
                return _problema(f"Campo '{campo}' obrigatório.", campo)
        if len(corpo["qrcode"]) < 70:
            return _problema("QRCode inválido.", "qrcode")
        if not _VALOR_PONTO.match(corpo["valor"]):
            return _problema("Valor inválido.", "valor")
        if any(not corpo["origem"].get(c) for c in _ORIGEM_QR):
            return _problema(
                "Dados do campo 'origem' precisam ser preenchidos manualmente.",
                "origem",
            )
        if "codMun" in corpo and not re.fullmatch(r"\d{7}", corpo["codMun"]):
            return _problema("Código de município inválido.", "codMun")
        retorno = self._retorno(corpo, self._e2e(), corpo["valor"])
        juros = "0.50" if corpo["pagarComJurosMulta"] else "0.00"
        retorno.update(
            txId="7978c0c97ea847e78e8849634473c1f1",
            valorOriginal=corpo["valor"],
            juros=juros,
            multa="0.00",
            desconto="0.00",
            abatimento="0.00",
            tipoQrcode="DINAMICO" if "0112" in corpo["qrcode"][:12] else "ESTATICO",
        )
        return httpx.Response(200, json=retorno)

    def _iniciar(self, corpo: dict) -> httpx.Response:
        chave = corpo.get("chave")
        if not chave:
            return _problema("Campo 'chave' obrigatório.", "chave")
        if chave in self.chaves_inexistentes:
            return httpx.Response(
                404,
                json={
                    "type": "https://pix.bcb.gov.br/api/v2/error/NaoEncontrado",
                    "title": "Não Encontrado",
                    "status": 404,
                    "detail": "Chave não encontrada.",
                },
            )
        e2e = self._e2e()
        self.iniciados[e2e] = chave
        return httpx.Response(
            200,
            json={
                "endToEndId": e2e,
                "chave": chave,
                "tipo": "CPF",
                "proprietario": {
                    "identificador": self.dono_chave,
                    "nome": "FULANO",
                    "tipo": "FISICO",
                },
            },
        )

    def _confirmar(self, corpo: dict) -> httpx.Response:
        if not _VALOR_VIRGULA.match(str(corpo.get("valor", ""))):
            return _problema(
                "Dados do campo 'origem' precisam ser preenchidos manualmente.",
                "origem",
            )
        meio = corpo.get("meioIniciacao")
        if meio == "CHAVE":
            e2e = corpo.get("endToEndId")
            if e2e not in self.iniciados:
                return _problema("endToEndId não iniciado.", "endToEndId")
        elif meio == "MANUAL":
            if not corpo.get("destino", {}).get("ispb"):
                return _problema("Destino obrigatório.", "destino")
            e2e = self._e2e()
        else:
            return _problema("Meio de iniciação inválido.", "meioIniciacao")
        valor = float(corpo["valor"].replace(",", "."))
        return httpx.Response(200, json=self._retorno(corpo, e2e, valor))

    def _webhook(self, metodo: str, corpo: dict, aceita: str | None) -> httpx.Response:
        if metodo == "PUT" and aceita == "application/json":
            # Firewall do sandbox: pagina HTML com HTTP 200.
            return httpx.Response(
                200, text="<html><head><title>Request Rejected</title></head></html>"
            )
        if metodo == "PUT":
            self.webhook = corpo["webhookUrl"]
            return httpx.Response(200)
        if metodo == "DELETE":
            self.webhook = None
            return httpx.Response(204)
        if self.webhook is None:
            return httpx.Response(404, json={"title": "Não Encontrado"})
        return httpx.Response(
            200,
            json={"webhookUrl": self.webhook, "criacao": "2026-10-29T10:00:00Z"},
        )
