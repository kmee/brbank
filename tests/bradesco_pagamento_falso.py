"""Servidor falso dos pagamentos do Bradesco sobre httpx.MockTransport.

Segue os OpenAPI, manuais e collections publicos do portal Bradesco Developers
(lidos em 30/10/2026): Pix em `qrpix.bradesco.com.br` com token proprio, boleto em
tres passos, guia em dois (`tipoRegistro`), TED com `chaveUnicaParaApi`, id do
cliente que nao se repete e erros 412 com `correlationId` ou `causa`.
"""

import json
import re

import httpx

URL_BOLETO = "/boleto/pagamento-cobranca/v1/"
URL_GUIA = "/pagamento/arrecadacao-via-codbarras/v1/"
URL_TED = "/transferencia/ted/v1/"
URL_PIX = "/v1/spi/"
_ID_PIX = re.compile(r"^[a-zA-Z0-9]{1,35}$")


def _problema(status: int, codigo: str, detalhe: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": "https://pix.bcb.gov.br/api/v2/error/",
            "title": "Erro",
            "status": status,
            "detail": detalhe,
            "correlationId": codigo,
        },
    )


def _causa(status: int, causa: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "status": status,
            "transacao": "CBCAIAAZ",
            "mensagem": "Falha ao Atender a Requisicao",
            "causa": causa,
        },
    )


class BradescoPagamentoFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, str, str]] = []
        self.corpos: list[tuple[str, dict]] = []
        self.autorizacoes: list[str] = []
        self.tokens: list[tuple[str, str]] = []
        # Rota (host + caminho curto) -> lista de falhas forcadas.
        self.falhas: dict[str, list] = {}
        self.status_pix = "CONCLUIDO"
        self.situacao_boleto = 1
        self.status_ted = "PROCESSADA"
        self.retorno_ted = 23
        self.retorno_guia = 0
        self.ctrl_cip = ""
        self.consulta_cip = "N"
        self.vencimento_boleto = 20261231
        self.quitacao_boleto = None
        self.pix: dict[str, dict] = {}
        self.boletos: dict[str, dict] = {}
        self.guias: dict[str, dict] = {}
        self.guias_por_tipo = {3: True, 4: False, 5: False}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.tratar))

    def de_rota(self, rota: str) -> list[dict]:
        return [c for r, c in self.corpos if r.endswith(rota)]

    def rotas(self) -> list[str]:
        return [c[2] for c in self.chamadas if "token" not in c[2]]

    def tratar(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path
        host = request.url.host
        self.chamadas.append((request.method, host, caminho))
        if caminho.endswith("/token"):
            self.tokens.append((host, caminho))
            pedido = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            if pedido.get("grant_type") != "client_credentials" or not pedido.get(
                "client_secret"
            ):
                return httpx.Response(401, json={"title": "Nao autorizado"})
            return httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        autorizacao = request.headers.get("authorization") or ""
        self.autorizacoes.append(autorizacao)
        if autorizacao not in ("Bearer tok", "tok"):
            return _problema(401, "", "token")
        corpo = json.loads(request.content) if request.content else {}
        if corpo:
            self.corpos.append((caminho, corpo))
        falha = self._falha(caminho, corpo, request)
        if falha is not None:
            return falha
        if caminho.startswith(URL_PIX):
            if host != "qrpix.bradesco.com.br" and "sandbox" not in host:
                return _problema(404, "", "Pix so no host qrpix")
            return self._pix(request.method, caminho, corpo)
        if caminho.startswith(URL_BOLETO):
            return self._boleto(caminho.removeprefix(URL_BOLETO), corpo)
        if caminho.startswith(URL_GUIA):
            return self._guia(request, caminho.removeprefix(URL_GUIA), corpo)
        if caminho.startswith(URL_TED):
            return self._ted(request, caminho.removeprefix(URL_TED), corpo)
        return _problema(404, "", caminho)

    def _falha(self, caminho, corpo, request) -> httpx.Response | None:
        for rota, fila in self.falhas.items():
            if not fila or not caminho.endswith(rota):
                continue
            falha = fila.pop(0)
            if falha == "timeout":
                raise httpx.ReadTimeout("sem resposta", request=request)
            if falha == "html":
                return httpx.Response(200, text="<html>rejeitado</html>")
            if falha == "vazio":
                return httpx.Response(200)
            if isinstance(falha, tuple):
                return falha[0](*falha[1:])
            return httpx.Response(falha, text="erro")
        return None

    # -- Pix -------------------------------------------------------------------

    def _pix(self, metodo: str, caminho: str, corpo: dict) -> httpx.Response:
        if metodo == "POST":
            return self._solicitar_pix(corpo)
        transacao = self.pix.get(caminho.rsplit("/", 1)[-1])
        if not transacao:
            return _problema(404, "5013", "Transferencia nao encontrada")
        return httpx.Response(
            200,
            json={
                **transacao,
                "status": self.status_pix,
                "dataEfetivacao": "2026-10-30T10:00:05.000Z",
            },
        )

    def _solicitar_pix(self, corpo: dict) -> httpx.Response:
        id_transacao = corpo.get("idtransacao") or ""
        if not _ID_PIX.match(id_transacao):
            return _problema(400, "", "idtransacao")
        if not re.fullmatch(r"\d+\.\d{2}", str(corpo.get("valor"))):
            return _problema(400, "", "valor")
        pagador = corpo.get("pagador") or {}
        if pagador.get("tipoChave") != "AGENCIACONTA" or not pagador.get("conta"):
            return _problema(400, "", "pagador")
        recebedor = corpo.get("recebedor") or {}
        tipo = recebedor.get("tipoChave")
        if tipo == "AGENCIACONTA":
            for campo in (
                "cpfCnpj",
                "ispb",
                "agencia",
                "conta",
                "digitoConta",
                "tipoConta",
                "nomeFavorecido",
            ):
                if not recebedor.get(campo):
                    return _problema(400, "", campo)
        elif tipo in ("EMAIL", "TELEFONE", "CPFCNPJ", "EVP"):
            if not recebedor.get("chavePix"):
                return _problema(400, "", "chavePix")
        else:
            return _problema(400, "", "tipoChave")
        if len(corpo.get("descricao") or "") > 30:
            return _problema(400, "", "descricao")
        if id_transacao in self.pix:
            return _problema(412, "M0240", "Transacao ja efetivada")
        resposta = {
            "pagador": {"cpfCnpj": "11222333000181", "agencia": "1", "conta": "12345"},
            "recebedor": recebedor,
            "valor": corpo["valor"],
            "e2e": "E60746948202610301715L2856oOXfn8",
            "idtransacao": id_transacao,
            "dataCriacao": "2026-10-30T10:00:00.000Z",
            "status": self.status_pix,
            "mensagem": "Transacao realizada com sucesso",
        }
        if self.status_pix == "REJEITADO":
            resposta.update(codigoMotivo="1658", motivo="SEM SALDO SUFICIENTE")
        self.pix[id_transacao] = resposta
        return httpx.Response(
            200 if self.status_pix == "CONCLUIDO" else 202, json=resposta
        )

    # -- boleto ----------------------------------------------------------------

    def _boleto(self, rota: str, corpo: dict) -> httpx.Response:
        if rota.endswith("validaTituloPagamento"):
            return self._validar_titulo(corpo)
        if rota.endswith("pre-efetivacao-pagamento"):
            return self._pre(corpo)
        if rota.endswith("solicitacao/executar"):
            return self._efetivar(corpo)
        if rota.endswith("consulta-pagamento-especifico"):
            return self._consultar_boleto(corpo)
        if rota.endswith("exclusao/executar"):
            if corpo.get("versao") != 2:
                return _causa(400, "CBCA0045 - VERSAO INVALIDA")
            if str(corpo.get("idTransacao")) not in self.boletos:
                return _causa(412, "CBCA0211 - ID NAO ENCONTRADO")
            return httpx.Response(
                200,
                json={"status": 200, "causa": "CANCELAMENTO EFETUADO"},
            )
        return _problema(404, "", rota)

    def _validar_titulo(self, corpo: dict) -> httpx.Response:
        entrada = corpo.get("dadosEntrada") or ""
        if (
            corpo.get("tipoEntrada") != 1
            or len(entrada) != 54
            or entrada[:10] != "0" * 10
        ):
            return _causa(400, "CBCA0101 - ENTRADA INVALIDA")
        return httpx.Response(
            200,
            json={
                "status": 200,
                "causa": "CBCA0013 - CONSULTA EFETUADA",
                "codigoRetorno": 0,
                "dataVencimento": self.vencimento_boleto,
                "valorTitulo": 15.0,
                "consultaCip": self.consulta_cip,
                "numeroCtrlCip": self.ctrl_cip,
                "codBarras": entrada[10:],
                "nomeSacado": "PAGADOR FICTICIO",
                "cnpjCpfCedente": 95752207000129,
            },
        )

    def _pre(self, corpo: dict) -> httpx.Response:
        for campo in (
            "agencia",
            "formaCaptura",
            "codigoBarras",
            "dataVencimento",
            "valorTitulo",
            "dataMovimento",
            "dataPagamento",
            "horaTransacao",
            "formaPagamento",
            "bancoDebito",
            "agenciaDebito",
            "contaDebito",
        ):
            if corpo.get(campo) in (None, ""):
                return _causa(400, f"CBCA0100 - {campo}")
        if self.consulta_cip == "S" and corpo.get("numeroCtrlCip") != self.ctrl_cip:
            return _causa(412, "CBCA0089 - CONTROLE CIP")
        return httpx.Response(
            200,
            json={
                "status": 200,
                "causa": "CBCA0013 - CONSULTA EFETUADA",
                "valorCobrado": corpo["valorTitulo"],
            },
        )

    def _efetivar(self, corpo: dict) -> httpx.Response:
        for campo in (
            "transactionId",
            "cnpjCpfPtdor",
            "ctrlCnpjPtdor",
            "indicadorFuncao",
            "codigoAgencia",
        ):
            if corpo.get(campo) in (None, ""):
                return _causa(400, f"CBCA0100 - {campo}")
        id_transacao = str(corpo["transactionId"])
        if len(id_transacao) > 9:
            return _causa(400, "CBCA0100 - transactionId")
        if id_transacao in self.boletos:
            return _causa(412, "CBCA0108 - PAGAMENTO EM DUPLICIDADE")
        self.boletos[id_transacao] = corpo
        return httpx.Response(
            200,
            json={
                "status": 200,
                "transacao": "CBCAIAAZ",
                "mensagem": "Operacao realizada com sucesso",
                "causa": "CBCA0001 - PAGAMENTO EFETUADO",
                "nroProtocolo": 7,
                "valorCobrado": corpo["valorTitulo"],
                "dataQuitacao": self.quitacao_boleto or corpo["dataPagamento"],
                "nomePagador": "PAGADOR FICTICIO",
                "cpfCnpjBenef": 95752207,
            },
        )

    def _consultar_boleto(self, corpo: dict) -> httpx.Response:
        if corpo.get("versaoLayout") != "1" or not corpo.get("numeroConta"):
            return _causa(400, "CBCA0045 - VERSAO INVALIDA")
        if corpo.get("transactionId") not in self.boletos:
            return _causa(412, "CBCA0211 - ID NAO ENCONTRADO")
        return httpx.Response(
            200,
            json={
                "status": 200,
                "causa": "CBCA0221 - CONSULTA EFETUADA COM SUCESSO",
                "situacaoPagamento": self.situacao_boleto,
                "valorPagamento": 15.0,
                "dataInclTit": 20261030,
                "numeroProtocolo": 7,
                "motNaoEfet": 12 if self.situacao_boleto == 12 else 0,
                "descrMotivo": "SEM SALDO" if self.situacao_boleto == 12 else "",
                "isacdo": "PAGADOR FICTICIO",
                "ccgcCpfSacdo": 95752207000129,
            },
        )

    # -- guia ------------------------------------------------------------------

    def _guia(self, request, rota: str, corpo: dict) -> httpx.Response:
        if rota == "pagamentoContaConsumo":
            return self._pagar_guia(corpo)
        params = request.url.params
        if params.get("segmentoConsulta") != "99" or params.get(
            "dataInicial"
        ) != params.get("dataFinal"):
            return _causa(400, "filtro")
        tipo = int(params["tipoConsulta"])
        item = self.guias.get(params.get("idTransacao"))
        if not item or not self.guias_por_tipo.get(tipo):
            return httpx.Response(
                200, json={"retorno": "000", "sqlCode": "000", "regSaida": []}
            )
        return httpx.Response(
            200,
            json={
                "retorno": "000",
                "regSaida": [
                    {
                        "valorDebito": item["valorPrincipal"],
                        "dataPagamento": item["dataDebito"],
                        "autenticacaoBancaria": 45551127,
                        "codigoBarras": item["codigoBarras"],
                    }
                ],
            },
        )

    def _pagar_guia(self, corpo: dict) -> httpx.Response:
        for campo in (
            "agencia",
            "conta",
            "codigoBarras",
            "dataDebito",
            "valorPrincipal",
            "digitoConta",
        ):
            if corpo.get(campo) in (None, ""):
                return httpx.Response(400, json={"mensagem": campo})
        if (
            corpo.get("tipoRegistro") not in (0, 1)
            or len(corpo.get("idTransacao") or "") > 9
        ):
            return httpx.Response(400, json={"mensagem": "tipoRegistro"})
        if corpo["tipoRegistro"] == 1:
            if self.retorno_guia == 0 and corpo["idTransacao"] in self.guias:
                return httpx.Response(
                    422, json={"retorno": 57, "mensagem": "duplicidade"}
                )
            self.guias[corpo["idTransacao"]] = corpo
        resposta = {
            "retorno": self.retorno_guia,
            "sqlCode": 0,
            "tipoRegistro": str(corpo["tipoRegistro"]),
            "valorPago": corpo["valorPrincipal"],
            "dataDebito": corpo["dataDebito"],
            "nomeCliente": "PAGADOR FICTICIO",
        }
        if corpo["tipoRegistro"] == 1:
            resposta["autenticacaoBancaria"] = 45154760
        return httpx.Response(200, json=resposta)

    # -- TED -------------------------------------------------------------------

    def _ted(self, request, rota: str, corpo: dict) -> httpx.Response:
        if rota == "consulta":
            documento = request.url.params.get("numeroDocumento")
            data = request.url.params.get("dataOperacao")
            if documento != "2786114" or not data:
                return httpx.Response(
                    422, json={"codigoRetorno": 4, "mensagem": "REGISTRO INEXISTENTE"}
                )
            return httpx.Response(
                200,
                json={
                    "chaveUnicaTed": f"{documento}{data}",
                    "valorDaTransferencia": 1000.8,
                    "statusMensagem": self.status_ted,
                    "codigoDaDevolucao": 2 if self.status_ted == "DEVOLVIDA" else 0,
                    "descricaoDaDevolucao": "AG.OU CTA DEST. INVALIDA"
                    if self.status_ted == "DEVOLVIDA"
                    else "",
                    "cnpjOuCpfDestinatario": 95752207,
                    "nomeClienteDestinatario": "FAVORECIDO FICTICIO",
                },
            )
        for campo in (
            "agenciaRemetente",
            "bancoDestinatario",
            "contaRemetenteComDigito",
            "numeroInscricao",
            "numeroFilial",
            "numeroControle",
            "nomeClienteDestinatario",
            "valorDaTransferencia",
            "finalidadeDaTransferencia",
            "dataMovimento",
            "tipoDeDoc",
        ):
            if corpo.get(campo) in (None, ""):
                return httpx.Response(400, json={"mensagem": campo})
        if len(corpo["numeroFilial"]) > 5:
            return httpx.Response(400, json={"mensagem": "numeroFilial"})
        if self.retorno_ted != 23:
            return httpx.Response(
                200,
                json={"codigoDeRetorno": self.retorno_ted, "mensagem": "SEM SALDO"},
            )
        return httpx.Response(
            200,
            json={
                "origemDaTransferencia": "APIB",
                "codigoDeRetorno": 23,
                "mensagem": "OPERACAO EFETUADA COM SUCESSO",
                "chaveUnicaParaApi": "27861142026-10-30-11.49.07.904129",
                "dataMovimento": corpo["dataMovimento"],
                "cnpjOuCpfDestinatario": "0051713550000248",
                "nomeClienteDestinatario": corpo["nomeClienteDestinatario"],
            },
        )
