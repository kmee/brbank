"""Servidor falso do Itau (SISPAG v1, pagamentos) sobre httpx.MockTransport.

Segue a colecao Postman "API Pagamentos PIX" e os relatos de producao listados em
`docs/itau-pagamento-duvidas.md` (a especificacao oficial exige login): inclusao em
`POST /transferencias` com campos soltos e `pagador`, resposta com
`status_pagamento` e `cod_pagamento`, recusa 422 com `motivo_recusa`, erro de campo
400 ou 422 com `campos`, consultas embrulhadas em `data`.
"""

import json
import re
import uuid

import httpx

PREFIXO = "/itau-ep9-gtw-sispag-ext/v1/"
CAMINHO_TOKEN = "/api/oauth/jwt"
_VALOR = re.compile(r"^\d+\.\d{2}$")
_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAGADOR = ("tipo_conta", "agencia", "conta", "tipo_pessoa", "documento")
_DADOS_BANCARIOS = (
    "ispb",
    "tipo_identificacao_conta",
    "agencia_recebedor",
    "conta_recebedor",
    "tipo_de_identificacao_do_recebedor",
    "identificacao_recebedor",
)
_RECEBEDOR = {
    "ispb": "99999008",
    "documento": "95752207000129",
    "nome": "RECEBEDOR TESTE",
    "tipo_conta": "Conta Corrente",
    "tipo_pessoa": "J",
    "identificacao_chave": "recebedor@example.com",
}


def _erro(status: int, mensagem: str, campo: str | None = None) -> httpx.Response:
    corpo: dict = {"codigo": str(status), "mensagem": mensagem}
    if campo:
        corpo["campos"] = [
            {"campo": campo, "mensagem": f"{campo} invalido", "valor": "x"}
        ]
    return httpx.Response(status, json=corpo)


class ItauPagamentoFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, str]] = []
        self.corpos: list[dict] = []
        self.cabecalhos: list[dict[str, str]] = []
        self.hosts: list[str] = []
        self.consultas: list[dict[str, str]] = []
        self.pagamentos: dict[str, dict] = {}
        self.status_inclusao = "Sucesso"
        self.status_consulta = "Efetuado"
        self.tamanho_pagina = 50
        # Fila de falhas da proxima inclusao: "timeout", "422", "422-campo",
        # "200-rejeitado", "nao-json", "sem-codigo" ou um status HTTP.
        self.falhas: list = []

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == CAMINHO_TOKEN:
            pedido = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            if pedido.get("grant_type") != "client_credentials" or not pedido.get(
                "client_secret"
            ):
                return httpx.Response(401, json={"mensagem": "nao autorizado"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        self.cabecalhos.append(dict(request.headers))
        self.hosts.append(request.url.host)
        if request.headers.get("authorization") != "Bearer tok":
            return _erro(401, "token")
        if not request.headers.get("x-itau-apikey"):
            return _erro(403, "x-itau-apikey")
        rota = caminho.removeprefix(PREFIXO)
        if request.method == "POST" and rota == "transferencias":
            return self._incluir(request)
        if request.method == "GET" and rota == "pagamentos_sispag":
            return self._listar(request)
        if request.method == "GET" and rota.startswith("pagamentos_sispag/"):
            return self._detalhar(rota.split("/")[-1])
        return _erro(404, rota)

    def _validar(self, corpo: dict) -> httpx.Response | None:
        if not isinstance(corpo.get("valor_pagamento"), str) or not _VALOR.match(
            corpo["valor_pagamento"]
        ):
            return _erro(400, "Erro na validacao", "valor_pagamento")
        if not _DATA.match(str(corpo.get("data_pagamento"))):
            # Relato de producao: datetime completo devolve 500.
            return _erro(500, "Erro de Processamento Interno")
        pagador = corpo.get("pagador") or {}
        if any(not isinstance(pagador.get(c), str) for c in _PAGADOR):
            # Relato: numero no lugar de texto devolve 500.
            return _erro(500, "Erro de Processamento Interno")
        if pagador.get("modulo_sispag") not in ("Fornecedores", "Diversos"):
            return _erro(400, "Erro na validacao", "modulo_sispag")
        formas = [bool(corpo.get("chave")), bool(corpo.get("emv"))]
        formas.append(all(corpo.get(c) for c in _DADOS_BANCARIOS))
        if sum(formas) != 1:
            return _erro(400, "Erro na validacao", "chave")
        if len(corpo.get("referencia_empresa", "")) > 20:
            return _erro(400, "Erro na validacao", "referencia_empresa")
        return None

    def _rejeitado(self, corpo: dict, http: int) -> httpx.Response:
        return httpx.Response(
            http,
            json={
                "status_pagamento": "Rejeitado",
                "tipo_pagamento": "QRCODE",
                "data_pagamento": f"{corpo.get('data_pagamento')}T13:01:02.000Z",
                "valor_pagamento": 1,
                "referencia_empresa": corpo.get("referencia_empresa"),
                "pagador": corpo.get("pagador"),
                "recebedor": _RECEBEDOR,
                "motivo_recusa": [
                    {
                        "codigo": 908,
                        "nome": "Valor do pagamento divergente do valor do qrcode.",
                    }
                ],
            },
        )

    def _incluir(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        self.corpos.append(corpo)
        if self.falhas:
            falha = self.falhas.pop(0)
            if falha == "timeout":
                raise httpx.ReadTimeout("sem resposta", request=request)
            if falha == "422":
                return self._rejeitado(corpo, 422)
            if falha == "200-rejeitado":
                return self._rejeitado(corpo, 200)
            if falha == "422-campo":
                return _erro(422, "Erro na validacao dos campos", "chave")
            if falha == "nao-json":
                return httpx.Response(200, text="<html>ok</html>")
            if falha == "lista":
                return httpx.Response(200, json=[])
            if falha == "sem-codigo":
                return httpx.Response(200, json={"status_pagamento": "Sucesso"})
            return _erro(falha, "falha")
        invalido = self._validar(corpo)
        if invalido is not None:
            return invalido
        codigo = str(uuid.uuid4())
        self.pagamentos[codigo] = corpo
        return httpx.Response(
            200,
            json={
                "status_pagamento": self.status_inclusao,
                "cod_pagamento": codigo,
                "numero_lote": 500001394,
                "numero_lancamento": 22683,
                "tipo_pagamento": "PIX",
                "data_pagamento": f"{corpo['data_pagamento']}T15:03:56.000Z",
                "valor_pagamento": float(corpo["valor_pagamento"]),
                "referencia_empresa": corpo.get("referencia_empresa"),
                "pagador": {**corpo["pagador"], "tipo_conta": "Conta Corrente"},
                "recebedor": _RECEBEDOR,
                "motivo_recusa": [],
            },
        )

    def _detalhar(self, codigo: str) -> httpx.Response:
        corpo = self.pagamentos.get(codigo)
        if corpo is None:
            return _erro(404, "pagamento nao encontrado")
        dados = {
            "dados_debito": {
                "numero_agencia_debito": "0001",
                "numero_conta_debito": "00123456",
                "nome_empresa_debito": "EMPRESA TESTE",
                "cnpj_debito": "95752207000129",
            },
            "dados_pagamento": {
                "id_pagamento": codigo,
                "cpf_cnpj_favorecido": "28133847044",
                "numero_conta_favorecido": "00765432",
                "nome_favorecido": "FULANO",
                "valor_pagamento": corpo["valor_pagamento"],
                "numero_lote": "500001394",
                "referencia_empresa": corpo.get("referencia_empresa"),
                "data_pagamento": corpo["data_pagamento"],
                "status": self.status_consulta,
                "tipo_pagamento": "PIX",
                "motivo_rejeicao": (
                    [{"descricao_motivo": "Pagamento expirado"}]
                    if self.status_consulta == "Não Efetuado"
                    else []
                ),
            },
            "historico_pagamento": [
                {
                    "status": "Inclusão Online",
                    "data": corpo["data_pagamento"],
                    "nome_operador": "OPERADOR",
                    "cod_operador": "999999999",
                    "cpf_operador": "28133847044",
                }
            ],
        }
        return httpx.Response(200, json={"data": dados})

    def _listar(self, request: httpx.Request) -> httpx.Response:
        parametros = dict(request.url.params)
        self.consultas.append(parametros)
        for obrigatorio in (
            "agencia_operacao",
            "conta_operacao",
            "cnpj_empresa",
            "tipo_lista",
        ):
            if not parametros.get(obrigatorio):
                return _erro(400, "Erro na validacao", obrigatorio)
        itens = [
            {
                "id_pagamento": codigo,
                "cpf_cnpj": "28133847044",
                "nome_favorecido": "FULANO",
                "numero_lancamento": "22683",
                "referencia_empresa": corpo.get("referencia_empresa"),
                "data_pagamento": corpo["data_pagamento"],
                "valor_pagamento": corpo["valor_pagamento"],
                "status": self.status_consulta,
                "motivo": "",
                "tipo_pagamento": "PIX",
            }
            for codigo, corpo in self.pagamentos.items()
        ]
        pagina = int(parametros.get("page", 0))
        tamanho = self.tamanho_pagina
        total_paginas = max(1, (len(itens) + tamanho - 1) // tamanho)
        fatia = itens[pagina * tamanho : (pagina + 1) * tamanho]
        return httpx.Response(
            200,
            json={
                "data": {"itens": fatia, "total": "0.00"},
                "pagination": {
                    "page": pagina,
                    "total_pages": total_paginas,
                    "total_elements": len(itens),
                    "page_size": tamanho,
                },
            },
        )
