"""Servidor falso do BB (Pagamentos em Lote v1) sobre httpx.MockTransport, com estado.

As respostas seguem o que a homologacao devolveu em 28/10/2026, inclusive as
variacoes de nome por tipo (lista, id, aceite e erros).
"""

import json

import httpx

CHAVE_DEV = "chave-dev-falsa"
PREFIXO = "/pagamentos-lote/v1/"

_LISTA_POST = {
    "lotes-boletos": ("lancamentos", "codigoIdentificadorPagamento", "indicadorAceite"),
    "lotes-guias-codigo-barras": (
        "lancamentos",
        "codigoIdentificadorPagamento",
        "indicadorAceite",
    ),
    "lotes-transferencias-pix": (
        "listaTransferencias",
        "identificadorPagamento",
        "indicadorMovimentoAceito",
    ),
    "lotes-transferencias": (
        "transferencias",
        "identificadorTransferencia",
        "indicadorAceite",
    ),
}
# Consulta do lote de guias chama o id de `codigoPagamento`.
_ID_CONSULTA = {"lotes-guias-codigo-barras": "codigoPagamento"}


class BBPagamentoFalso:
    def __init__(self) -> None:
        self.proximo = 26545
        self.lotes: dict[str, dict] = {}
        self.pagamentos: dict[str, str] = {}
        self.chamadas: list[tuple[str, str]] = []
        self.corpos: list[dict] = []
        self.ultimo_cabecalho: dict[str, str] = {}
        self.pedido_token: dict[str, str] = {}
        self.recusar: set[int] = set()
        self.estado_pagamento = "CONSISTENTE"

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        self.chamadas.append((request.method, caminho))
        if caminho == "/oauth/token":
            self.pedido_token = dict(
                p.split("=", 1) for p in request.content.decode().split("&")
            )
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        self.ultimo_cabecalho = dict(request.headers)
        if request.url.params.get("gw-dev-app-key") != CHAVE_DEV:
            return httpx.Response(401, json={"error": "chave"})
        corpo = json.loads(request.content) if request.content else {}
        if corpo:
            self.corpos.append(corpo)
        rota = caminho.removeprefix(PREFIXO)
        if rota == "proximos-numeros-requisicao":
            numero, self.proximo = self.proximo, self.proximo + 2
            return httpx.Response(200, json={"listaProximosNumeros": [numero]})
        if request.method == "POST" and rota in _LISTA_POST:
            return self._criar(rota, corpo)
        if rota == "liberar-pagamentos":
            self.lotes[str(corpo["numeroRequisicao"])]["estadoRequisicao"] = 9
            return httpx.Response(
                200, json={"mensagemRetorno": "Liberação efetuada com sucesso"}
            )
        if rota == "cancelar-pagamentos":
            return httpx.Response(200, json={"mensagemRetorno": "ok"})
        if rota.endswith("/solicitacao"):
            partes = rota.split("/")
            lote = self.lotes.get(partes[-2])
            if lote is None:
                return httpx.Response(404, json={"errors": [{"codigo": 404}]})
            return httpx.Response(200, json=lote)
        id_pagamento = rota.split("/")[-1]
        if id_pagamento in self.pagamentos:
            return httpx.Response(
                200,
                json={
                    "id": int(id_pagamento),
                    "estadoPagamento": self.estado_pagamento,
                    "dataPagamento": 28102026,
                    "valorPagamento": 15.0,
                    "codigoAutenticacaoPagamento": "",
                    "listaDevolucao": [{"codigoMotivo": 295}]
                    if self.estado_pagamento == "REJEITADO"
                    else [],
                },
            )
        return httpx.Response(404, json={"errors": [{"codigo": 404}]})

    def _criar(self, rota: str, corpo: dict) -> httpx.Response:
        numero = corpo["numeroRequisicao"]
        lista, chave_id, chave_aceite = _LISTA_POST[rota]
        entrada = corpo.get("lancamentos") or corpo.get("listaTransferencias") or []
        linhas = []
        for i, item in enumerate(entrada, 1):
            id_pagamento = f"900{numero}731030{i:03d}"
            recusada = i in self.recusar
            self.pagamentos[id_pagamento] = rota
            linhas.append(
                {
                    **item,
                    chave_id: id_pagamento
                    if rota == "lotes-boletos"
                    else int(id_pagamento),
                    chave_aceite: "N" if recusada else "S",
                    "erros": [7, 0] if recusada else [],
                }
            )
        estado = (
            3
            if all(i in self.recusar for i in range(1, len(entrada) + 1))
            else (2 if self.recusar else 1)
        )
        resposta = {
            "numeroRequisicao": numero,
            "estadoRequisicao": estado,
            lista: linhas,
        }
        consulta_id = _ID_CONSULTA.get(rota)
        guardado = {
            "estadoRequisicao": estado,
            lista: [
                {
                    **{k: v for k, v in linha.items() if k != chave_id},
                    consulta_id: str(linha[chave_id]),
                }
                if consulta_id
                else linha
                for linha in linhas
            ],
        }
        if rota == "lotes-guias-codigo-barras":
            resposta = {
                "numeroRequisicao": numero,
                "codigoEstado": estado,
                lista: linhas,
            }
        self.lotes[str(numero)] = guardado
        return httpx.Response(201, json=resposta)
