"""Servidor falso da API Cobranca do Bradesco sobre httpx.MockTransport, com estado.

Formato dos manuais oficiais 1.6.4 (convencional) e 1.0.5 (QR Code); nao e captura do
sandbox real. Os numeros abaixo sao sinteticos, so para o teste.
"""

import json
from urllib.parse import parse_qs

import httpx

REGISTRO = "/boleto/cobranca-registro/v1/cobranca"
CONSULTA = "/boleto/cobranca-consulta/v1/consultar"
BAIXA = "/boleto/cobranca-baixa/v1/baixar"
ALTERACAO = "/boleto/cobranca-altera/v1/alterar"
PROTESTO = "/boleto/cobranca-protesto-negativacao/v1/executar"
WEBHOOK_SANDBOX = "/boleto/cobranca-webhook/v1/cadastrar"
WEBHOOK_PRODUCAO = "/boleto/cobranca-webhook/v1/executar"
REGISTRO_HIBRIDO = "/boleto-hibrido/cobranca-registro/v1/gerarBoleto"
CONSULTA_HIBRIDO = "/boleto-hibrido/cobranca-consulta-titulo/v1/consultar"
ALTERACAO_HIBRIDO = "/boleto-hibrido/cobranca-alteracao/v1/alteraBoletoConsulta"
TOKEN = "/auth/server-mtls/v2/token"

# Exemplo do manual 1.6.4: a linha com pontos e o codigo de barras em EBCDIC.
LINHA = "23792.85600 95228.000048 92022.265208 2 12110000125599"
LINHA_DIGITOS = "23792856009522800004892022265208212110000125599"
BARRAS_DIGITOS = "23792121100001255992856095228000049202226520"
BARRAS_EBCDIC = "<NWnnwnNnWwNwnnWNwnnWwnNNwnnWWnNnwwNNwNnww>"
EMV = "00020101021226930014BR.GOV.BCB.PIX2571qrpix-h.bradesco.com.br/qr/v2/cobv/x"
TXID = "20261101237092856002265400000000001"


def webhook_pagamento(
    nosso_numero: str = "00000123456",
    produto: str = "09",
    negociacao: str = "28560022654",
    tipo_evento: str = "P",
) -> dict:
    """Aviso de pagamento como no manual (agencia 2856, conta 22654)."""
    return {
        "tipoEvento": tipo_evento,
        "produto": produto,
        "negociacao": negociacao,
        "nossoNumero": nosso_numero,
        "dataRegistro": "2026-11-01",
        "dataVencimento": "2026-12-10",
        "valorBoleto": "0000000000150.00",
        "bancoRecebedor": "237",
        "canalPagamento": "3",
        "dataPagamento": "2026-12-09",
        "horaPagamento": "001508",
        "valorPagamento": "0000000000150.00",
        "valorAbatimento": "0000000000000.00",
        "valorMulta": "0000000000000.00",
        "valorJuros": "0000000000000.00",
        "valorDesc": "0000000000000.00",
    }


class BradescoFalso:
    def __init__(self) -> None:
        self.titulos: dict[str, dict] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.tokens_emitidos = 0
        self.ultimo_cabecalho: dict[str, str] = {}
        self.ultimo_corpo: dict = {}
        self.corpos: list[dict] = []
        self.ultimo_form: dict[str, list[str]] = {}
        self.ultimo_host = ""
        # Fator aplicado ao valor recebido no registro, para simular leitura errada.
        self.eco_valor: int | None = None

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def liquidar(self, nosso_numero: str) -> None:
        titulo = self.titulos[nosso_numero]
        titulo.update(codStatus=61, status="PAGO", dtPagto=10112026, vlrPagto=150.0)

    def _novo(self, corpo: dict, *, valor: int, numero: int = 0) -> str:
        nosso = str(numero or 90000000000 + len(self.titulos) + 1)
        self.titulos[nosso] = {
            "codStatus": 1,
            "status": "A VENCER / VENCIDO",
            "snumero": corpo.get("nuCliente") or corpo.get("ctitloCliCdent"),
            "linhaDig": LINHA,
            "codBarras": BARRAS_EBCDIC,
            "dataVencto": "10/12/2026",
            "valMoeda": valor,
            "valAbat": 0,
            "dtPagto": 0,
            "vlrPagto": 0.0,
            "nomeSacado": corpo.get("nomePagador") or corpo.get("isacdoTitloCobr"),
            "cnpjSacado": 52998224725,
        }
        return nosso

    def _registrar(self, corpo: dict) -> httpx.Response:
        bruto = str(corpo["vlNominalTitulo"])
        valor = int(bruto.replace(".", "")) if "." in bruto else int(bruto)
        numero = int(corpo.get("nuTitulo") or 0)
        nosso = self._novo(corpo, valor=valor, numero=numero)
        return httpx.Response(
            200,
            json={
                "nuTituloGerado": int(nosso),
                "linhaDigitavel": LINHA,
                "cdBarras": BARRAS_EBCDIC,
                "vlTitulo": self.eco_valor if self.eco_valor is not None else valor,
                "codStatus10": 1,
                "nomePagador": corpo["nomePagador"],
            },
        )

    def _registrar_hibrido(self, corpo: dict) -> httpx.Response:
        valor = int(corpo["vnmnalTitloCobr"])
        numero = int(corpo.get("ctitloCobrCdent") or 0)
        nosso = self._novo(corpo, valor=valor, numero=numero)
        self.titulos[nosso]["semvQrcode"] = EMV
        return httpx.Response(
            200,
            json={
                "statusHttp": 200,
                "ctitloCobrCdent": int(nosso),
                "valMoeda10": valor,
                "linhaDig10": LINHA,
                "codBarras10": "",
                "wqrcdPdraoMercd": EMV,
                "iconcPgtoSpi": TXID,
                "ialiasAdsaoCta": "chave-pix-do-contrato",
                "nomeSacado10": corpo["isacdoTitloCobr"],
            },
        )

    def _alterar(self, corpo: dict) -> httpx.Response:
        titulo = self.titulos[str(corpo["nossoNumero"])]
        dados = corpo["dadosTitulo"]
        vencimento = str(dados["vencimento"]["dataVencimento"])
        if vencimento != "0":
            texto = vencimento.zfill(8)
            titulo["dataVencto"] = f"{texto[:2]}/{texto[2:4]}/{texto[4:]}"
        abatimento = str(dados["abatimento"]["valorAbatimento"])
        if abatimento != "0":
            titulo["valAbat"] = int(abatimento.replace(".", ""))
        return httpx.Response(
            200,
            json={
                "status": 200,
                "transacao": "CBTTIAGP",
                "mensagem": "CBTT0445 - ALTERACAO EFETUADA",
            },
        )

    def _alterar_hibrido(self, corpo: dict) -> httpx.Response:
        titulo = self.titulos[str(corpo["chave"]["nossoNumero"])]
        dados = corpo["dadosTitulo"]
        texto = str(dados["dataVencimento"]).zfill(8)
        titulo["dataVencto"] = f"{texto[:2]}/{texto[2:4]}/{texto[4:]}"
        if dados["valorAbatimentoTitulo"]:
            titulo["valAbat"] = dados["valorAbatimentoTitulo"]
        return httpx.Response(
            200, json={"codigo": "CBTT0445", "mensagem": "ALTERACAO EFETUADA"}
        )

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.ultimo_host = request.url.host
        self.chamadas.append((request.method, caminho))
        self.ultimo_cabecalho = dict(request.headers)
        if caminho == TOKEN:
            self.tokens_emitidos += 1
            self.ultimo_form = parse_qs(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.tokens_emitidos}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        corpo: dict = json.loads(request.content) if request.content else {}
        self.ultimo_corpo = corpo
        self.corpos.append(corpo)
        if caminho == REGISTRO:
            return self._registrar(corpo)
        if caminho == REGISTRO_HIBRIDO:
            return self._registrar_hibrido(corpo)
        if caminho in (WEBHOOK_SANDBOX, WEBHOOK_PRODUCAO):
            return httpx.Response(
                200, json={"utilizaWebhook": "S", "urlEnvio": corpo["urlEnvio"]}
            )
        nosso = str(
            corpo.get("nossoNumero") or corpo.get("chave", {}).get("nossoNumero")
        )
        if nosso not in self.titulos:
            return httpx.Response(
                404, json={"status": 404, "causa": "TITULO INEXISTENTE"}
            )
        if caminho == CONSULTA:
            return httpx.Response(
                200, json={"status": 200, "titulo": dict(self.titulos[nosso])}
            )
        if caminho == CONSULTA_HIBRIDO:
            return httpx.Response(200, json=dict(self.titulos[nosso]))
        if caminho == BAIXA:
            self.titulos[nosso].update(codStatus=57, status="CONFORME SEU PEDIDO")
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "transacao": "CBTTIAGQ",
                    "mensagem": "CBTT0532 - SOLICITACAO DE BAIXA EFETUADA",
                    "dados": {"status": 57, "statusAnterior": 1},
                },
            )
        if caminho == ALTERACAO:
            return self._alterar(corpo)
        if caminho == ALTERACAO_HIBRIDO:
            return self._alterar_hibrido(corpo)
        if caminho == PROTESTO:
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "transacao": "CBTTIAGU",
                    "causa": "CBTT1000 - SOLICITACAO DE PROTESTO EFETUADA COM SUCESSO.",
                    "situacaoAtual": 4,
                    "situacaoAnterior": 1,
                },
            )
        return httpx.Response(404, json={"status": 404})
